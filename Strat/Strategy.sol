// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

contract Strategy is AMMStrategyBase {
    uint256 private constant NORM_BPS = 30;
    uint256 private constant OPEN_BPS = 44;
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
    uint256 private constant KAPPA_INIT = 10e14; // share change per 1 bps fee delta
    uint256 private constant KAPPA_MIN = 2e14;
    uint256 private constant KAPPA_MAX = 30e14;

    // slots
    // 0 bid fee (wad)
    // 1 ask fee (wad)
    // 2 last timestamp
    // 3 step trades
    // 4 lambda_hat (wad)
    // 5 arb_hat (wad)
    // 6 vol_hat (wad rel-move, ~sigma)
    // 7 p_low
    // 8 p_high
    // 9 p_step
    // 10 p_prev
    // 11 anchored_this_step (0/1)
    // 12 prev reserve x
    // 13 prev reserve y
    // 14 s_buy_base
    // 15 s_sell_base
    // 16 kappa_buy
    // 17 kappa_sell
    // 18 r_buy_hat
    // 19 r_sell_hat
    // 20 prev ask fee bps (buy-side share perturbation)
    // 21 prev bid fee bps (sell-side share perturbation)
    // 22 prev buy-side share observation
    // 23 prev sell-side share observation

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(OPEN_BPS);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);
        uint256 width = bpsToWad(25);

        slots[0] = open;
        slots[1] = open;
        slots[2] = 0;
        slots[3] = 0;
        slots[4] = 78e16;
        slots[5] = 44e16;
        slots[6] = bpsToWad(95) / 10;
        slots[7] = wmul(p0, WAD - width);
        slots[8] = wmul(p0, WAD + width);
        slots[9] = p0;
        slots[10] = p0;
        slots[11] = 0;
        slots[12] = initialX;
        slots[13] = initialY;
        slots[14] = 50e16;
        slots[15] = 50e16;
        slots[16] = KAPPA_INIT;
        slots[17] = KAPPA_INIT;
        slots[18] = WAD;
        slots[19] = WAD;
        slots[20] = OPEN_BPS;
        slots[21] = OPEN_BPS;
        slots[22] = 50e16;
        slots[23] = 50e16;

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

        uint256 lastTs = slots[2];
        uint256 stepTrades = slots[3];
        uint256 lambdaHat = slots[4];
        if (lambdaHat == 0) lambdaHat = 78e16;
        uint256 arbHat = slots[5];
        if (arbHat == 0) arbHat = 44e16;
        uint256 volHat = slots[6];
        if (volHat == 0) volHat = bpsToWad(95) / 10;
        uint256 pLow = slots[7];
        uint256 pHigh = slots[8];
        uint256 pStep = slots[9];
        uint256 pPrev = slots[10];
        if (pStep == 0) pStep = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);
        if (pLow == 0 || pHigh == 0) {
            pLow = wmul(pStep, WAD - bpsToWad(30));
            pHigh = wmul(pStep, WAD + bpsToWad(30));
        }
        if (pPrev == 0) pPrev = pStep;
        uint256 anchoredThisStep = slots[11];

        uint256 xPre = slots[12];
        uint256 yPre = slots[13];
        if (xPre == 0 || yPre == 0) {
            xPre = trade.reserveX;
            yPre = trade.reserveY;
        }

        uint256 sBuyBase = slots[14];
        if (sBuyBase == 0) sBuyBase = 50e16;
        uint256 sSellBase = slots[15];
        if (sSellBase == 0) sSellBase = 50e16;
        uint256 kappaBuy = slots[16];
        if (kappaBuy == 0) kappaBuy = KAPPA_INIT;
        uint256 kappaSell = slots[17];
        if (kappaSell == 0) kappaSell = KAPPA_INIT;
        uint256 rBuyHat = slots[18];
        if (rBuyHat == 0) rBuyHat = WAD;
        uint256 rSellHat = slots[19];
        if (rSellHat == 0) rSellHat = WAD;

        uint256 prevAskFeeBpsForShare = slots[20];
        if (prevAskFeeBpsForShare == 0) prevAskFeeBpsForShare = askPrevBps;
        uint256 prevBidFeeBpsForShare = slots[21];
        if (prevBidFeeBpsForShare == 0) prevBidFeeBpsForShare = bidPrevBps;
        uint256 prevBuyShareObs = slots[22];
        if (prevBuyShareObs == 0) prevBuyShareObs = 50e16;
        uint256 prevSellShareObs = slots[23];
        if (prevSellShareObs == 0) prevSellShareObs = 50e16;

        bool newStep = trade.timestamp != lastTs;
        uint256 deltaT = 0;
        if (newStep) {
            deltaT = trade.timestamp > lastTs ? (trade.timestamp - lastTs) : 1;
            if (anchoredThisStep == 0) {
                pStep = _geomMeanOrFallback(pLow, pHigh, pStep);
            }
            pPrev = pStep;

            uint256 kSigma = clamp(volHat * K_SIGMA_MULT, bpsToWad(6), bpsToWad(40));
            uint256 loMul = kSigma >= WAD ? 1 : (WAD - kSigma);
            uint256 hiMul = WAD + kSigma;
            pLow = wmul(pLow, loMul);
            pHigh = wmul(pHigh, hiMul);
            if (pLow == 0 || pHigh == 0 || pLow >= pHigh) {
                uint256 center = pStep;
                uint256 resetW = bpsToWad(30);
                pLow = wmul(center, WAD - resetW);
                pHigh = wmul(center, WAD + resetW);
            }

            uint256 lambdaObs = _lambdaObservation(deltaT, stepTrades);
            uint256 avgShareBase = (sBuyBase + sSellBase) / 2;
            uint256 lambdaCorrected = _applyShareCorrection(lambdaObs, avgShareBase);
            lambdaHat = _ewma(lambdaHat, lambdaCorrected, A_LAMBDA);

            stepTrades = 0;
            anchoredThisStep = 0;
            lastTs = trade.timestamp;
        }

        stepTrades += 1;

        uint256 spotPre = _safePrice(yPre, xPre, pStep);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pStep);

        uint256 gammaBidUsed = _gammaFromFee(bidPrev);
        uint256 gammaAskUsed = _gammaFromFee(askPrev);
        uint256 strongL = wmul(spotPost, gammaBidUsed);
        uint256 strongU = wdiv(spotPost, gammaAskUsed);
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
            uint256 mid = _geomMeanOrFallback(strongL, strongU, pStep);
            uint256 buf = bpsToWad(COLLAPSE_BUF_BPS);
            pLow = wmul(mid, WAD - buf);
            pHigh = wmul(mid, WAD + buf);
        }

        uint256 midNow = _geomMeanOrFallback(pLow, pHigh, pStep);
        if (highConfidenceArb && anchoredThisStep == 0) {
            pStep = ((WAD - ANCHOR_WEIGHT_ARB) * midNow + ANCHOR_WEIGHT_ARB * pAnchor) / WAD;
            anchoredThisStep = 1;
        } else if (anchoredThisStep == 1) {
            pStep = _ewma(pStep, midNow, A_STEP_SMOOTH);
        } else {
            pStep = midNow;
        }

        if (pStep < pLow) pStep = pLow;
        if (pStep > pHigh) pStep = pHigh;

        arbHat = _ewma(arbHat, highConfidenceArb ? WAD : 0, A_ARB);
        uint256 volObs = highConfidenceArb ? _relDiff(pAnchor, pPrev) : _relDiff(spotPost, spotPre);
        volHat = _ewma(volHat, volObs, A_VOL);
        volHat = clamp(volHat, bpsToWad(4) / 10, bpsToWad(140) / 10);

        uint256 shareObs = _shareObservation(trade.amountY, deltaT, stepTrades);
        if (trade.isBuy) {
            (sSellBase, kappaSell, rSellHat, prevBidFeeBpsForShare, prevSellShareObs) = _updateSideState(
                sSellBase,
                kappaSell,
                rSellHat,
                bidPrevBps,
                prevBidFeeBpsForShare,
                prevSellShareObs,
                shareObs
            );
        } else {
            (sBuyBase, kappaBuy, rBuyHat, prevAskFeeBpsForShare, prevBuyShareObs) = _updateSideState(
                sBuyBase,
                kappaBuy,
                rBuyHat,
                askPrevBps,
                prevAskFeeBpsForShare,
                prevBuyShareObs,
                shareObs
            );
        }

        uint256 midTargetBps = _midTargetBps(lambdaHat, arbHat, volHat);
        int256 fairSkew = _fairSkewBps(spotPost, pStep, _relDiff(spotPost, pStep));
        (int256 invSkew, uint256 invAbs, int256 invSigned) = _inventorySkew(
            pStep,
            trade.reserveX,
            trade.reserveY
        );
        int256 skewTarget = (fairSkew * 4 + invSkew * 6) / 10;
        skewTarget = _clampSigned(skewTarget, -int256(MAX_SKEW_BPS), int256(MAX_SKEW_BPS));

        int256 bestScore = type(int256).min;
        uint256 bestBidBps = bidPrevBps;
        uint256 bestAskBps = askPrevBps;

        int256[3] memory midOffsets = [int256(-4), int256(0), int256(4)];
        int256[3] memory skewOffsets = [int256(-3), int256(0), int256(3)];

        for (uint256 i = 0; i < 3; i++) {
            for (uint256 j = 0; j < 3; j++) {
                uint256 candMid = _clampSignedToUint(
                    int256(midTargetBps) + midOffsets[i],
                    MIN_BPS,
                    MAX_BPS
                );
                int256 candSkew = _clampSigned(
                    skewTarget + skewOffsets[j],
                    -int256(MAX_SKEW_BPS),
                    int256(MAX_SKEW_BPS)
                );

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
                    _relDiff(spotPost, pStep),
                    invAbs,
                    invSigned,
                    trade.reserveY
                );

                if (score > bestScore) {
                    bestScore = score;
                    bestBidBps = candBid;
                    bestAskBps = candAsk;
                }
            }
        }

        bestBidBps = _limitJumpBps(bestBidBps, bidPrevBps, MAX_JUMP_BPS);
        bestAskBps = _limitJumpBps(bestAskBps, askPrevBps, MAX_JUMP_BPS);
        (bestBidBps, bestAskBps) = _enforceAsymmetry(bestBidBps, bestAskBps, MAX_ASYM_BPS);

        uint256 bidOut = clampFee(bpsToWad(bestBidBps));
        uint256 askOut = clampFee(bpsToWad(bestAskBps));

        slots[0] = bidOut;
        slots[1] = askOut;
        slots[2] = lastTs;
        slots[3] = stepTrades;
        slots[4] = lambdaHat;
        slots[5] = arbHat;
        slots[6] = volHat;
        slots[7] = pLow;
        slots[8] = pHigh;
        slots[9] = pStep;
        slots[10] = pPrev;
        slots[11] = anchoredThisStep;
        slots[12] = trade.reserveX;
        slots[13] = trade.reserveY;
        slots[14] = sBuyBase;
        slots[15] = sSellBase;
        slots[16] = kappaBuy;
        slots[17] = kappaSell;
        slots[18] = rBuyHat;
        slots[19] = rSellHat;
        slots[20] = prevAskFeeBpsForShare;
        slots[21] = prevBidFeeBpsForShare;
        slots[22] = prevBuyShareObs;
        slots[23] = prevSellShareObs;

        return (bidOut, askOut);
    }

    function getName() external pure override returns (string memory) {
        return "MeanEdge_AdaptiveBelief_v1";
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

        uint256 spreadToNorm = absDiff(avgFee, bpsToWad(NORM_BPS));
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
        // Proxy for continuation value on explicit features:
        // [arb_hat, stale_mag, inv_abs, inv_signed, lambda_hat, spread_to_norm, fee_jump].
        int256 v = 0;
        v += int256(lambdaHat) * 20;
        v -= int256(arbHat) * 28;
        v -= int256(staleMag) * 34;
        v -= int256(invAbs) * 18;
        v -= int256(spreadToNorm) * 10;
        v -= int256(feeJump) * 14;

        if (invSigned > 0) {
            v -= (invSigned * 4);
        } else if (invSigned < 0) {
            v += (-invSigned * 3);
        }

        return v / 30000;
    }

    function _updateSideState(
        uint256 baseShare,
        uint256 kappa,
        uint256 routeHat,
        uint256 feeBpsUsed,
        uint256 prevFeeBps,
        uint256 prevObsShare,
        uint256 obsShare
    ) internal pure returns (uint256, uint256, uint256, uint256, uint256) {
        uint256 baseObs = _removeFeeDelta(obsShare, kappa, feeBpsUsed);
        baseShare = _ewma(baseShare, clamp(baseObs, 5e16, 95e16), A_SHARE_BASE);

        if (prevFeeBps > 0 && prevObsShare > 0) {
            uint256 feeDelta = absDiff(feeBpsUsed, prevFeeBps);
            if (feeDelta > 0 && feeDelta <= 8) {
                uint256 slopeObs = absDiff(obsShare, prevObsShare) / feeDelta;
                slopeObs = clamp(slopeObs, KAPPA_MIN, KAPPA_MAX);

                bool feeDown = feeBpsUsed < prevFeeBps;
                bool shareUp = obsShare >= prevObsShare;
                bool directionConsistent = (feeDown && shareUp) || (!feeDown && !shareUp);

                if (directionConsistent) {
                    kappa = _ewma(kappa, slopeObs, A_KAPPA);
                } else {
                    kappa = _ewma(kappa, KAPPA_MIN, A_KAPPA / 2);
                }
            }
        }

        kappa = clamp(kappa, KAPPA_MIN, KAPPA_MAX);
        routeHat = _ewma(routeHat, _shareToRoute(obsShare), A_ROUTE);
        return (baseShare, kappa, routeHat, feeBpsUsed, obsShare);
    }

    function _predictShare(
        uint256 baseShare,
        uint256 kappa,
        uint256 routeHat,
        uint256 feeBps
    ) internal pure returns (uint256) {
        uint256 byFee = _shareFromFee(baseShare, kappa, feeBps);
        uint256 byRoute = _routeToShare(routeHat);
        uint256 mixed = (byFee * 7 + byRoute * 3) / 10;
        return clamp(mixed, 1e16, 99e16);
    }

    function _shareFromFee(uint256 baseShare, uint256 kappa, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        if (feeBps <= NORM_BPS) {
            uint256 up = kappa * (NORM_BPS - feeBps);
            return clamp(baseShare + up, 1e16, 99e16);
        }
        uint256 down = kappa * (feeBps - NORM_BPS);
        if (down >= baseShare) return 1e16;
        return clamp(baseShare - down, 1e16, 99e16);
    }

    function _removeFeeDelta(uint256 obsShare, uint256 kappa, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        if (feeBps <= NORM_BPS) {
            uint256 up = kappa * (NORM_BPS - feeBps);
            if (up >= obsShare) return 1e16;
            return obsShare - up;
        }
        uint256 down = kappa * (feeBps - NORM_BPS);
        return clamp(obsShare + down, 1e16, 99e16);
    }

    function _midTargetBps(uint256 lambdaHat, uint256 arbHat, uint256 volHat)
        internal
        pure
        returns (uint256)
    {
        int256 mid = int256(uint256(OPEN_BPS));

        mid += (int256(lambdaHat) - int256(75e16)) / int256(25e15); // primary lambda driver
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
            uint256 num = valueX - reserveY;
            invAbs = wdiv(num, total);
            invSigned = int256(invAbs);
            skew = int256(_invTilt(invAbs));
        } else {
            uint256 num = reserveY - valueX;
            invAbs = wdiv(num, total);
            invSigned = -int256(invAbs);
            skew = -int256(_invTilt(invAbs));
        }
    }

    function _fairSkewBps(uint256 spot, uint256 pStep, uint256 staleMag)
        internal
        pure
        returns (int256)
    {
        uint256 mag = 0;
        uint256 staleBps = wadToBps(staleMag);
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
        uint256 corrected = wdiv(lambdaObs, shareFloor);
        return clamp(corrected, 25e16, 140e16);
    }

    function _shareObservation(uint256 amountY, uint256 deltaT, uint256 stepTrades)
        internal
        pure
        returns (uint256)
    {
        uint256 sizeRatio = wdiv(amountY, RETAIL_MEAN_Y);
        sizeRatio = clamp(sizeRatio, 20e16, 220e16);

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

        bidBps = clamp(bidBps, MIN_BPS, MAX_BPS);
        askBps = clamp(askBps, MIN_BPS, MAX_BPS);
        return (bidBps, askBps);
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

    function _geomMeanOrFallback(uint256 a, uint256 b, uint256 fallbackP) internal pure returns (uint256) {
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

    function _ewma(uint256 oldV, uint256 obs, uint256 alpha) internal pure returns (uint256) {
        if (oldV == 0) return obs;
        uint256 beta = WAD - alpha;
        return (oldV * beta + obs * alpha) / WAD;
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
