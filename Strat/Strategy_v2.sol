// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

contract Strategy is AMMStrategyBase {
    uint256 private constant COMPETITOR_BPS = 30;
    uint256 private constant PRIOR_BPS = 37;
    uint256 private constant OPEN_BPS = PRIOR_BPS;
    uint256 private constant MIN_BPS = 12;
    uint256 private constant MAX_BPS = 108;
    uint256 private constant MAX_JUMP_BPS = 6;
    uint256 private constant MAX_ASYM_BPS = 22;
    uint256 private constant MAX_SKEW_BPS = 15;

    uint256 private constant A_LAMBDA = 15e16;
    uint256 private constant A_ARB = 18e16;
    uint256 private constant A_VOL = 8e16;
    uint256 private constant A_SHARE_BASE = 12e16;
    uint256 private constant A_KAPPA = 9e16;
    uint256 private constant A_ROUTE = 9e16;
    uint256 private constant A_STEP_SMOOTH = 4e16;

    uint256 private constant K_SIGMA_MULT = 2;
    uint256 private constant EPS_WEAK_BPS = 24;
    uint256 private constant COLLAPSE_BUF_BPS = 14;
    uint256 private constant ANCHOR_WEIGHT_ARB = 72e16;

    uint256 private constant RETAIL_MEAN_Y = 20 * WAD;
    uint256 private constant KAPPA_INIT = 10e14;
    uint256 private constant KAPPA_MIN = 2e14;
    uint256 private constant KAPPA_MAX = 30e14;
    uint256 private constant KAPPA_PERTURB_MIN_BPS = 1;
    uint256 private constant KAPPA_PERTURB_MAX_BPS = 3;
    uint256 private constant LOG_BLEND_STEPS = 6;

    // slot map
    // 0  bid fee (wad)
    // 1  ask fee (wad)
    // 2  packed step state: timestamp|anchor|stepTrades
    // 3  lambda_hat
    // 4  arb_hat
    // 5  vol_hat
    // 6  p_low
    // 7  p_high
    // 8  p_step
    // 9  prev reserve x
    // 10 prev reserve y
    // 11 s_buy_base
    // 12 s_sell_base
    // 13 kappa_buy
    // 14 kappa_sell
    // 15 r_buy_hat
    // 16 r_sell_hat
    // 17 prev bid quote (bps)
    // 18 prev ask quote (bps)

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(OPEN_BPS);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        // Keep initialization writes low to stay under the 250k gas cap.
        slots[0] = open;
        slots[1] = open;
        slots[2] = _packStepState(0, 0, false);
        slots[3] = 78e16;
        slots[4] = 44e16;
        slots[5] = bpsToWad(95) / 10;
        slots[8] = p0;
        slots[9] = initialX;
        slots[10] = initialY;
        slots[11] = 50e16;
        slots[17] = OPEN_BPS;
        slots[18] = OPEN_BPS;

        return (open, open);
    }

    function afterSwap(TradeInfo calldata trade)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 bidPrev = slots[0];
        uint256 askPrev = slots[1];
        if (bidPrev == 0) bidPrev = bpsToWad(OPEN_BPS);
        if (askPrev == 0) askPrev = bpsToWad(OPEN_BPS);

        uint256 bidPrevBps = wadToBps(bidPrev);
        uint256 askPrevBps = wadToBps(askPrev);
        if (bidPrevBps == 0) bidPrevBps = OPEN_BPS;
        if (askPrevBps == 0) askPrevBps = OPEN_BPS;
        uint256 prevBidQuoteBps = slots[17];
        if (prevBidQuoteBps == 0) prevBidQuoteBps = bidPrevBps;
        uint256 prevAskQuoteBps = slots[18];
        if (prevAskQuoteBps == 0) prevAskQuoteBps = askPrevBps;

        (uint256 lastTs, uint256 stepTrades, bool anchoredThisStep) = _unpackStepState(slots[2]);

        uint256 lambdaHat = slots[3];
        if (lambdaHat == 0) lambdaHat = 78e16;
        uint256 arbHat = slots[4];
        if (arbHat == 0) arbHat = 44e16;
        uint256 volHat = slots[5];
        if (volHat == 0) volHat = bpsToWad(95) / 10;

        uint256 pLow = slots[6];
        uint256 pHigh = slots[7];
        uint256 pStep = slots[8];
        if (pStep == 0) pStep = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);
        if (pLow == 0 || pHigh == 0) {
            uint256 width = bpsToWad(30);
            pLow = wmul(pStep, WAD - width);
            pHigh = wmul(pStep, WAD + width);
        }

        uint256 xPre = slots[9];
        uint256 yPre = slots[10];
        if (xPre == 0 || yPre == 0) {
            xPre = trade.reserveX;
            yPre = trade.reserveY;
        }

        uint256 sBuyBase = slots[11];
        if (sBuyBase == 0) sBuyBase = 50e16;
        uint256 sSellBase = slots[12];
        if (sSellBase == 0) sSellBase = 50e16;
        uint256 kappaBuy = slots[13];
        if (kappaBuy == 0) kappaBuy = KAPPA_INIT;
        uint256 kappaSell = slots[14];
        if (kappaSell == 0) kappaSell = KAPPA_INIT;
        uint256 rBuyHat = slots[15];
        if (rBuyHat == 0) rBuyHat = WAD;
        uint256 rSellHat = slots[16];
        if (rSellHat == 0) rSellHat = WAD;

        uint256 deltaT = 0;
        bool newStep = trade.timestamp != lastTs;
        if (newStep) {
            deltaT = trade.timestamp > lastTs ? (trade.timestamp - lastTs) : 1;

            uint256 lambdaObs = _lambdaObservation(deltaT, stepTrades);
            uint256 avgShare = (sBuyBase + sSellBase) / 2;
            lambdaHat = _ewma(lambdaHat, _applyShareCorrection(lambdaObs, avgShare), A_LAMBDA);

            if (!anchoredThisStep) {
                pStep = _geomMeanOrFallback(pLow, pHigh, pStep);
            }

            uint256 kSigma = clamp(volHat * K_SIGMA_MULT, bpsToWad(6), bpsToWad(40));
            uint256 loMul = kSigma >= WAD ? 1 : (WAD - kSigma);
            uint256 hiMul = WAD + kSigma;
            pLow = wmul(pLow, loMul);
            pHigh = wmul(pHigh, hiMul);
            if (pLow >= pHigh) {
                uint256 center = pStep;
                uint256 resetW = bpsToWad(30);
                pLow = wmul(center, WAD - resetW);
                pHigh = wmul(center, WAD + resetW);
            }

            stepTrades = 0;
            anchoredThisStep = false;
            lastTs = trade.timestamp;
        }

        stepTrades += 1;

        uint256 pBefore = pStep;
        uint256 spotPre = _safePrice(yPre, xPre, pStep);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pStep);

        uint256 gammaBid = _gammaFromFee(bidPrev);
        uint256 gammaAsk = _gammaFromFee(askPrev);
        uint256 strongL = wmul(spotPost, gammaBid);
        uint256 strongU = wdiv(spotPost, gammaAsk);
        uint256 pAnchor = trade.isBuy ? strongL : strongU;

        bool highConfidenceArb = _isHighConfidenceArb(
            trade.isBuy,
            stepTrades,
            deltaT,
            trade.amountY,
            trade.reserveY,
            spotPre,
            spotPost,
            pStep,
            pAnchor
        );

        if (highConfidenceArb) {
            if (strongL > pLow) pLow = strongL;
            if (strongU < pHigh) pHigh = strongU;
        } else {
            uint256 weakL = wmul(spotPost, _gammaFromFee(bidPrev));
            uint256 weakU = wdiv(spotPost, _gammaFromFee(askPrev));
            uint256 eps = bpsToWad(EPS_WEAK_BPS);
            uint256 lAdj = wmul(weakL, WAD - eps);
            uint256 uAdj = wmul(weakU, WAD + eps);
            if (lAdj > pLow) pLow = lAdj;
            if (uAdj < pHigh) pHigh = uAdj;
        }

        if (pLow > pHigh) {
            uint256 midCollapse = _geomMeanOrFallback(strongL, strongU, pStep);
            uint256 buf = bpsToWad(COLLAPSE_BUF_BPS);
            pLow = wmul(midCollapse, WAD - buf);
            pHigh = wmul(midCollapse, WAD + buf);
        }

        uint256 midNow = _geomMeanOrFallback(pLow, pHigh, pStep);
        if (highConfidenceArb && !anchoredThisStep) {
            pStep = _logBlendApprox(midNow, pAnchor, ANCHOR_WEIGHT_ARB);
            anchoredThisStep = true;
        } else if (anchoredThisStep) {
            pStep = _ewma(pStep, midNow, A_STEP_SMOOTH);
        } else {
            pStep = midNow;
        }
        if (pStep < pLow) pStep = pLow;
        if (pStep > pHigh) pStep = pHigh;

        arbHat = _ewma(arbHat, highConfidenceArb ? WAD : 0, A_ARB);
        uint256 volObs = highConfidenceArb ? _relDiff(pAnchor, pBefore) : _relDiff(spotPost, spotPre);
        volHat = _ewma(volHat, volObs, A_VOL);
        volHat = clamp(volHat, bpsToWad(4) / 10, bpsToWad(140) / 10);

        uint256 shareObs = _shareObservation(trade.amountY, deltaT, stepTrades);
        if (trade.isBuy) {
            uint256 feeDeltaBps = absDiff(bidPrevBps, prevBidQuoteBps);
            (sSellBase, kappaSell, rSellHat) = _updateSideState(
                sSellBase,
                kappaSell,
                rSellHat,
                bidPrevBps,
                shareObs,
                feeDeltaBps
            );
        } else {
            uint256 feeDeltaBps = absDiff(askPrevBps, prevAskQuoteBps);
            (sBuyBase, kappaBuy, rBuyHat) = _updateSideState(
                sBuyBase,
                kappaBuy,
                rBuyHat,
                askPrevBps,
                shareObs,
                feeDeltaBps
            );
        }

        uint256 staleMag = _relDiff(spotPost, pStep);
        int256 fairSkew = _fairSkewBps(spotPost, pStep, staleMag);
        (int256 invSkew, uint256 invAbs, int256 invSigned) = _inventorySkew(
            pStep,
            trade.reserveX,
            trade.reserveY
        );

        uint256 midTarget = _midTargetBps(lambdaHat, arbHat, volHat);
        int256 skewTarget = _clampSigned((fairSkew * 4 + invSkew * 6) / 10, -int256(MAX_SKEW_BPS), int256(MAX_SKEW_BPS));

        int256 bestScore = type(int256).min;
        uint256 bestBid = bidPrevBps;
        uint256 bestAsk = askPrevBps;
        int256[3] memory midOffsets = [int256(-4), int256(0), int256(4)];
        int256[3] memory skewOffsets = [int256(-3), int256(0), int256(3)];

        for (uint256 i = 0; i < 3; i++) {
            for (uint256 j = 0; j < 3; j++) {
                uint256 candMid = _clampSignedToUint(int256(midTarget) + midOffsets[i], MIN_BPS, MAX_BPS);
                int256 candSkew = _clampSigned(skewTarget + skewOffsets[j], -int256(MAX_SKEW_BPS), int256(MAX_SKEW_BPS));

                uint256 candBid = _clampSignedToUint(int256(candMid) + candSkew, MIN_BPS, MAX_BPS);
                uint256 candAsk = _clampSignedToUint(int256(candMid) - candSkew, MIN_BPS, MAX_BPS);

                candBid = _limitJumpBps(candBid, bidPrevBps, MAX_JUMP_BPS);
                candAsk = _limitJumpBps(candAsk, askPrevBps, MAX_JUMP_BPS);
                (candBid, candAsk) = _enforceAsymmetry(candBid, candAsk, MAX_ASYM_BPS);

                int256 score = _scoreCandidate(
                    candBid,
                    candAsk,
                    bidPrev,
                    askPrev,
                    lambdaHat,
                    arbHat,
                    volHat,
                    sBuyBase,
                    sSellBase,
                    kappaBuy,
                    kappaSell,
                    rBuyHat,
                    rSellHat,
                    staleMag,
                    invAbs,
                    invSigned,
                    trade.reserveY
                );

                if (score > bestScore) {
                    bestScore = score;
                    bestBid = candBid;
                    bestAsk = candAsk;
                }
            }
        }

        bestBid = _limitJumpBps(bestBid, bidPrevBps, MAX_JUMP_BPS);
        bestAsk = _limitJumpBps(bestAsk, askPrevBps, MAX_JUMP_BPS);
        (bestBid, bestAsk) = _enforceAsymmetry(bestBid, bestAsk, MAX_ASYM_BPS);

        uint256 bidOut = clampFee(bpsToWad(bestBid));
        uint256 askOut = clampFee(bpsToWad(bestAsk));

        slots[0] = bidOut;
        slots[1] = askOut;
        slots[2] = _packStepState(lastTs, stepTrades, anchoredThisStep);
        slots[3] = lambdaHat;
        slots[4] = arbHat;
        slots[5] = volHat;
        slots[6] = pLow;
        slots[7] = pHigh;
        slots[8] = pStep;
        slots[9] = trade.reserveX;
        slots[10] = trade.reserveY;
        slots[11] = sBuyBase;
        slots[12] = sSellBase;
        slots[13] = kappaBuy;
        slots[14] = kappaSell;
        slots[15] = rBuyHat;
        slots[16] = rSellHat;
        slots[17] = bestBid;
        slots[18] = bestAsk;

        return (bidOut, askOut);
    }

    function getName() external pure override returns (string memory) {
        return "MeanEdge_AdaptiveBelief_v2";
    }

    function _scoreCandidate(
        uint256 bidBps,
        uint256 askBps,
        uint256 bidPrev,
        uint256 askPrev,
        uint256 lambdaHat,
        uint256 arbHat,
        uint256 volHat,
        uint256 sBuyBase,
        uint256 sSellBase,
        uint256 kappaBuy,
        uint256 kappaSell,
        uint256 rBuyHat,
        uint256 rSellHat,
        uint256 staleMag,
        uint256 invAbs,
        int256 invSigned,
        uint256 reserveY
    ) internal pure returns (int256) {
        uint256 bidFee = bpsToWad(bidBps);
        uint256 askFee = bpsToWad(askBps);
        uint256 avgFee = (bidFee + askFee) / 2;
        uint256 prevAvg = (bidPrev + askPrev) / 2;

        uint256 sBuyHat = _predictShare(sBuyBase, kappaBuy, rBuyHat, askBps);
        uint256 sSellHat = _predictShare(sSellBase, kappaSell, rSellHat, bidBps);

        uint256 retailY = wmul(lambdaHat, RETAIL_MEAN_Y);
        uint256 sideRetailY = retailY / 2;
        uint256 capturedBuyY = wmul(sideRetailY, sBuyHat);
        uint256 capturedSellY = wmul(sideRetailY, sSellHat);

        uint256 retailEdge = wmul(capturedBuyY, askFee) + wmul(capturedSellY, bidFee);

        uint256 tox = staleMag + (volHat / 2) + bpsToWad(5);
        uint256 arbBase = wmul(wmul(reserveY, arbHat), tox);
        uint256 feeShield = WAD + avgFee * 7;
        uint256 arbCost = wdiv(arbBase, feeShield);

        int256 edgeNorm = 0;
        if (reserveY > 0) {
            edgeNorm = (int256(retailEdge) - int256(arbCost)) * int256(WAD) / int256(reserveY);
        }

        uint256 spreadToNorm = absDiff(avgFee, bpsToWad(COMPETITOR_BPS));
        uint256 feeJump = absDiff(avgFee, prevAvg);
        int256 valueProxy = _valueProxy(
            arbHat,
            staleMag,
            invAbs,
            invSigned,
            lambdaHat,
            spreadToNorm,
            feeJump
        );

        return edgeNorm + valueProxy;
    }

    function _valueProxy(
        uint256 arbHat,
        uint256 staleMag,
        uint256 invAbs,
        int256 invSigned,
        uint256 lambdaHat,
        uint256 spreadToNorm,
        uint256 feeJump
    ) internal pure returns (int256) {
        int256 v = 0;
        v += int256(lambdaHat) * 20;
        v -= int256(arbHat) * 28;
        v -= int256(staleMag) * 34;
        v -= int256(invAbs) * 18;
        v -= int256(spreadToNorm) * 2;
        v -= int256(feeJump) * 14;

        if (invSigned > 0) {
            v -= invSigned * 4;
        } else if (invSigned < 0) {
            v += (-invSigned) * 3;
        }

        return v / 30000;
    }

    function _updateSideState(
        uint256 baseShare,
        uint256 kappa,
        uint256 routeHat,
        uint256 feeBps,
        uint256 obsShare,
        uint256 feeDeltaBps
    ) internal pure returns (uint256, uint256, uint256) {
        uint256 pred = _shareFromFee(baseShare, kappa, feeBps);
        bool perturbationWindow = feeDeltaBps >= KAPPA_PERTURB_MIN_BPS
            && feeDeltaBps <= KAPPA_PERTURB_MAX_BPS;

        if (pred > obsShare) {
            uint256 err = pred - obsShare;
            baseShare = baseShare > err / 2 ? (baseShare - err / 2) : 1e16;
            if (perturbationWindow && feeBps < COMPETITOR_BPS && kappa > KAPPA_MIN + err / 6) {
                kappa -= err / 6;
            }
        } else {
            uint256 err = obsShare - pred;
            baseShare = clamp(baseShare + err / 2, 1e16, 99e16);
            if (perturbationWindow && feeBps <= COMPETITOR_BPS) {
                kappa = clamp(kappa + err / 6, KAPPA_MIN, KAPPA_MAX);
            }
        }

        uint256 baseObs = _removeFeeDelta(obsShare, kappa, feeBps);
        baseShare = _ewma(baseShare, clamp(baseObs, 5e16, 95e16), A_SHARE_BASE);

        routeHat = _ewma(routeHat, _shareToRoute(obsShare), A_ROUTE);
        if (perturbationWindow) {
            kappa = _ewma(kappa, KAPPA_INIT, A_KAPPA / 8);
        } else {
            kappa = _ewma(kappa, KAPPA_INIT, A_KAPPA / 3);
        }
        kappa = clamp(kappa, KAPPA_MIN, KAPPA_MAX);

        return (baseShare, kappa, routeHat);
    }

    function _predictShare(
        uint256 baseShare,
        uint256 kappa,
        uint256 routeHat,
        uint256 feeBps
    ) internal pure returns (uint256) {
        uint256 byFee = _shareFromFee(baseShare, kappa, feeBps);
        uint256 byRoute = _routeToShare(routeHat);
        return clamp((byFee * 7 + byRoute * 3) / 10, 1e16, 99e16);
    }

    function _shareFromFee(uint256 baseShare, uint256 kappa, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        if (feeBps <= COMPETITOR_BPS) {
            return clamp(baseShare + kappa * (COMPETITOR_BPS - feeBps), 1e16, 99e16);
        }
        uint256 down = kappa * (feeBps - COMPETITOR_BPS);
        if (down >= baseShare) return 1e16;
        return clamp(baseShare - down, 1e16, 99e16);
    }

    function _removeFeeDelta(uint256 obsShare, uint256 kappa, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        if (feeBps <= COMPETITOR_BPS) {
            uint256 up = kappa * (COMPETITOR_BPS - feeBps);
            return up >= obsShare ? 1e16 : (obsShare - up);
        }
        return clamp(obsShare + kappa * (feeBps - COMPETITOR_BPS), 1e16, 99e16);
    }

    function _midTargetBps(uint256 lambdaHat, uint256 arbHat, uint256 volHat)
        internal
        pure
        returns (uint256)
    {
        int256 mid = int256(uint256(PRIOR_BPS));
        mid += (int256(lambdaHat) - int256(75e16)) / int256(25e15);
        mid -= (int256(arbHat) - int256(45e16)) / int256(3e16);

        uint256 volBps = wadToBps(volHat);
        mid -= int256(volBps) - 9;

        return _clampSignedToUint(mid, MIN_BPS, MAX_BPS);
    }

    function _inventorySkew(uint256 pStep, uint256 reserveX, uint256 reserveY)
        internal
        pure
        returns (int256 skew, uint256 invAbs, int256 invSigned)
    {
        uint256 valueX = wmul(pStep, reserveX);
        uint256 total = valueX + reserveY + 1;

        if (valueX >= reserveY) {
            invAbs = wdiv(valueX - reserveY, total);
            invSigned = int256(invAbs);
            skew = int256(_invTilt(invAbs));
        } else {
            invAbs = wdiv(reserveY - valueX, total);
            invSigned = -int256(invAbs);
            skew = -int256(_invTilt(invAbs));
        }
    }

    function _fairSkewBps(uint256 spot, uint256 pStep, uint256 staleMag)
        internal
        pure
        returns (int256)
    {
        uint256 staleBps = wadToBps(staleMag);
        uint256 mag = 0;
        if (staleBps > 40) mag = 8;
        else if (staleBps > 24) mag = 5;
        else if (staleBps > 10) mag = 3;

        if (mag == 0) return 0;
        return spot >= pStep ? int256(mag) : -int256(mag);
    }

    function _invTilt(uint256 invAbs) internal pure returns (uint256) {
        uint256 b = wadToBps(invAbs);
        if (b > 900) return 9;
        if (b > 620) return 7;
        if (b > 360) return 5;
        if (b > 180) return 3;
        return 0;
    }

    function _lambdaObservation(uint256 deltaT, uint256 prevStepTrades)
        internal
        pure
        returns (uint256)
    {
        uint256 gapObs;
        if (deltaT <= 1) gapObs = 92e16;
        else if (deltaT == 2) gapObs = 77e16;
        else if (deltaT == 3) gapObs = 65e16;
        else if (deltaT <= 5) gapObs = 53e16;
        else if (deltaT <= 9) gapObs = 42e16;
        else gapObs = 30e16;

        uint256 burstObs;
        if (prevStepTrades >= 4) burstObs = 108e16;
        else if (prevStepTrades == 3) burstObs = 94e16;
        else if (prevStepTrades == 2) burstObs = 80e16;
        else burstObs = 64e16;

        return (gapObs * 65 + burstObs * 35) / 100;
    }

    function _applyShareCorrection(uint256 lambdaObs, uint256 avgShareBase)
        internal
        pure
        returns (uint256)
    {
        uint256 shareFloor = clamp(avgShareBase, 20e16, 95e16);
        return clamp(wdiv(lambdaObs, shareFloor), 25e16, 140e16);
    }

    function _shareObservation(uint256 amountY, uint256 deltaT, uint256 stepTrades)
        internal
        pure
        returns (uint256)
    {
        uint256 sizeRatio = clamp(wdiv(amountY, RETAIL_MEAN_Y), 20e16, 220e16);
        uint256 obs = 38e16 + (sizeRatio * 18) / 100;

        if (deltaT > 1) {
            uint256 pen = (deltaT - 1) * 5e16;
            if (pen > 25e16) pen = 25e16;
            obs = obs > pen ? (obs - pen) : 6e16;
        }
        if (stepTrades >= 2) obs += 9e16;

        return clamp(obs, 6e16, 96e16);
    }

    function _isHighConfidenceArb(
        bool isBuy,
        uint256 stepTrades,
        uint256 deltaT,
        uint256 amountY,
        uint256 reserveY,
        uint256 spotPre,
        uint256 spotPost,
        uint256 pStep,
        uint256 pAnchor
    ) internal pure returns (bool) {
        uint256 score = 0;
        if (stepTrades == 1) score += 1;

        bool directionOk = isBuy ? (spotPost <= spotPre) : (spotPost >= spotPre);
        if (directionOk) score += 1;

        uint256 tol = bpsToWad(80 + (deltaT > 12 ? 24 : (deltaT * 2)));
        if (_relDiff(pAnchor, pStep) <= tol) score += 1;

        uint256 rel = reserveY == 0 ? 0 : wdiv(amountY, reserveY);
        if (rel >= bpsToWad(2)) score += 1;

        if (stepTrades > 2 && score > 0) score -= 1;
        return score >= 3;
    }

    function _enforceAsymmetry(uint256 bidBps, uint256 askBps, uint256 maxAsym)
        internal
        pure
        returns (uint256, uint256)
    {
        uint256 diff = absDiff(bidBps, askBps);
        if (diff <= maxAsym) return (bidBps, askBps);

        uint256 mid = (bidBps + askBps) / 2;
        uint256 half = maxAsym / 2;
        if (bidBps >= askBps) {
            bidBps = mid + half;
            askBps = mid > half ? (mid - half) : MIN_BPS;
        } else {
            askBps = mid + half;
            bidBps = mid > half ? (mid - half) : MIN_BPS;
        }
        return (clamp(bidBps, MIN_BPS, MAX_BPS), clamp(askBps, MIN_BPS, MAX_BPS));
    }

    function _limitJumpBps(uint256 target, uint256 prev, uint256 maxJump)
        internal
        pure
        returns (uint256)
    {
        if (target > prev + maxJump) return prev + maxJump;
        if (target + maxJump < prev) return prev - maxJump;
        return target;
    }

    function _gammaFromFee(uint256 feeWad) internal pure returns (uint256) {
        uint256 f = clampFee(feeWad);
        return f >= WAD ? 1 : (WAD - f);
    }

    function _safePrice(uint256 y, uint256 x, uint256 fallbackP) internal pure returns (uint256) {
        if (x == 0) return fallbackP;
        return wdiv(y, x);
    }

    function _geomMeanOrFallback(uint256 a, uint256 b, uint256 fallbackP)
        internal
        pure
        returns (uint256)
    {
        if (a == 0 || b == 0) return fallbackP;
        return sqrt(a * b);
    }

    function _relDiff(uint256 a, uint256 b) internal pure returns (uint256) {
        if (b == 0) return 0;
        return wdiv(absDiff(a, b), b);
    }

    function _shareToRoute(uint256 share) internal pure returns (uint256) {
        uint256 s = clamp(share, 1e16, 99e16);
        return wdiv(s, WAD - s);
    }

    function _routeToShare(uint256 route) internal pure returns (uint256) {
        if (route == 0) return 50e16;
        return wdiv(route, WAD + route);
    }

    function _logBlendApprox(uint256 a, uint256 b, uint256 wWad)
        internal
        pure
        returns (uint256)
    {
        if (a == 0) return b;
        if (b == 0) return a;
        if (wWad == 0) return a;
        if (wWad >= WAD) return b;

        uint256 low = a;
        uint256 high = b;
        uint256 frac = wWad;

        for (uint256 i = 0; i < LOG_BLEND_STEPS; i++) {
            uint256 mid = _geomMeanOrFallback(low, high, low);
            frac = frac * 2;
            if (frac >= WAD) {
                low = mid;
                frac -= WAD;
            } else {
                high = mid;
            }
        }

        return _geomMeanOrFallback(low, high, low);
    }

    function _ewma(uint256 oldV, uint256 obs, uint256 alpha) internal pure returns (uint256) {
        if (oldV == 0) return obs;
        uint256 beta = WAD - alpha;
        return (oldV * beta + obs * alpha) / WAD;
    }

    function _packStepState(uint256 timestamp, uint256 stepTrades, bool anchored)
        internal
        pure
        returns (uint256)
    {
        uint256 t = timestamp << 32;
        uint256 a = anchored ? (uint256(1) << 31) : 0;
        uint256 s = stepTrades & 0x7fffffff;
        return t | a | s;
    }

    function _unpackStepState(uint256 state)
        internal
        pure
        returns (uint256 timestamp, uint256 stepTrades, bool anchored)
    {
        timestamp = state >> 32;
        uint256 low = state & 0xffffffff;
        anchored = (low & (uint256(1) << 31)) != 0;
        stepTrades = low & 0x7fffffff;
    }

    function _clampSigned(int256 x, int256 lo, int256 hi) internal pure returns (int256) {
        if (x < lo) return lo;
        if (x > hi) return hi;
        return x;
    }

    function _clampSignedToUint(int256 x, uint256 lo, uint256 hi) internal pure returns (uint256) {
        if (x < int256(lo)) return lo;
        if (x > int256(hi)) return hi;
        return uint256(x);
    }
}
