// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// @notice Base adaptive strategy derived from sim_lab/strategy.py with gas-safe approximations:
/// - timestamp-order leak: stepTrades >= 2 is forced retail
/// - hidden fair-price interval filter (pLow/pHigh/pStep)
/// - lightweight retail share/order estimators (no latent normalizer state)
/// - continuous candidate scoring with no-arb side shield override
contract Strategy is AMMStrategyBase {
    uint256 private constant NORM_BPS = 30;
    uint256 private constant OPEN_BPS = 29;
    uint256 private constant PRIOR_BPS = 37;

    uint256 private constant MIN_BPS = 0;
    uint256 private constant MAX_BPS = 1000;
    uint256 private constant TARGET_MAX_BPS = 120;
    uint256 private constant MAX_JUMP_BPS = 4;
    uint256 private constant MAX_ASYM_BPS = 18;
    uint256 private constant MAX_SKEW_BPS = 22;

    uint256 private constant SHIELD_TRIGGER_BPS = 35;
    uint256 private constant SHIELD_BUFFER_BPS = 14;

    uint256 private constant MIN_ROUTE_AMOUNT = 1e14; // 1e-4 in token-Y WAD terms
    uint256 private constant RETAIL_MEAN_Y = 20 * WAD;

    uint256 private constant A_LAMBDA = 10e16;
    uint256 private constant A_LAMBDA_SAME = 28e16;
    uint256 private constant A_ARB = 11e16;
    uint256 private constant A_ARB_SAME = 22e16;
    uint256 private constant A_VOL = 6e16;
    uint256 private constant A_ORDER = 20e16;
    uint256 private constant A_BUY_PROB = 9e16;
    uint256 private constant A_SHARE = 10e16;
    uint256 private constant A_WEAK = 22e16;
    uint256 private constant A_MID = 12e16;
    uint256 private constant A_MID_SAME = 2e16;

    uint256 private constant EPS_WEAK_BPS = 90;
    uint256 private constant COLLAPSE_BUF_BPS = 35;
    uint256 private constant ANCHOR_W = 68e16;

    uint256 private constant ARB_ANCHOR_GATE_BPS = 240;
    uint256 private constant ARB_MOVE_FLOOR_BPS = 8;
    uint256 private constant SIZE_SMALL_ABS_Y = 12e17; // 1.2 Y
    uint256 private constant SIZE_SMALL_REL_WAD = 6e16; // 6%

    uint256 private constant SHARE_KAPPA_PER_BPS = 12e14;

    uint256 private constant STEP_MASK = 0x1fffffff;
    uint256 private constant FLAG_ANCHORED = 1 << 30;

    // slot map
    // 0  bid fee (wad)
    // 1  ask fee (wad)
    // 2  packed step state: timestamp|anchored|stepTrades
    // 3  lambda_hat
    // 4  arb_hat
    // 5  vol_hat
    // 6  p_low
    // 7  p_high
    // 8  p_step
    // 9  prev reserve x
    // 10 prev reserve y
    // 11 order_size_hat (Y)
    // 12 buy_prob_hat
    // 13 capture_share_buy_hat
    // 14 capture_share_sell_hat

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(OPEN_BPS);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        slots[0] = open;
        slots[1] = open;
        slots[2] = _packStepState(0, 0, false);
        slots[3] = 90e16;
        slots[4] = 50e16;
        slots[5] = bpsToWad(10);
        slots[8] = p0;
        slots[9] = initialX;
        slots[10] = initialY;

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

        (uint256 lastTs, uint256 stepTrades, bool anchoredThisStep) = _unpackStepState(slots[2]);

        uint256 lambdaHat = slots[3];
        if (lambdaHat == 0) lambdaHat = 90e16;
        uint256 arbHat = slots[4];
        if (arbHat == 0) arbHat = 50e16;
        uint256 volHat = slots[5];
        if (volHat == 0) volHat = bpsToWad(10);

        uint256 pLow = slots[6];
        uint256 pHigh = slots[7];
        uint256 pStep = slots[8];
        if (pStep == 0) pStep = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);
        if (pLow == 0 || pHigh == 0 || pLow >= pHigh) {
            uint256 resetW = bpsToWad(40);
            pLow = wmul(pStep, WAD - resetW);
            pHigh = wmul(pStep, WAD + resetW);
        }

        uint256 ourX = slots[9];
        uint256 ourY = slots[10];
        if (ourX == 0 || ourY == 0) {
            ourX = trade.reserveX;
            ourY = trade.reserveY;
        }

        uint256 orderSizeHat = slots[11];
        if (orderSizeHat == 0) orderSizeHat = RETAIL_MEAN_Y;
        uint256 buyProbHat = slots[12];
        if (buyProbHat == 0) buyProbHat = 50e16;
        uint256 shareBuyHat = slots[13];
        if (shareBuyHat == 0) shareBuyHat = 50e16;
        uint256 shareSellHat = slots[14];
        if (shareSellHat == 0) shareSellHat = 50e16;

        uint256 deltaT = 1;
        bool newStep = trade.timestamp != lastTs;
        if (newStep) {
            deltaT = trade.timestamp > lastTs ? (trade.timestamp - lastTs) : 1;

            lambdaHat = _ewma(lambdaHat, _lambdaObservation(deltaT, stepTrades), A_LAMBDA);

            if (!anchoredThisStep) {
                pStep = _geomMeanOrFallback(pLow, pHigh, pStep);
            }

            uint256 stepScale = deltaT > 6 ? 4 : (1 + deltaT / 2);
            uint256 kSigma = clamp(volHat * stepScale, bpsToWad(4), bpsToWad(45));
            uint256 loMul = kSigma >= WAD ? 1 : (WAD - kSigma);
            pLow = wmul(pLow, loMul);
            pHigh = wmul(pHigh, WAD + kSigma);
            if (pLow >= pHigh) {
                uint256 rw = bpsToWad(45);
                pLow = wmul(pStep, WAD - rw);
                pHigh = wmul(pStep, WAD + rw);
            }

            stepTrades = 0;
            anchoredThisStep = false;
            lastTs = trade.timestamp;
        }

        stepTrades += 1;
        if (stepTrades >= 2) {
            lambdaHat = _ewma(lambdaHat, _sameStepLambdaObservation(stepTrades), A_LAMBDA_SAME);
        }

        uint256 spotPre = _safePrice(ourY, ourX, pStep);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pStep);

        (bool probableArb, uint256 tailTier) = _classifyProbableArb(
            trade.isBuy,
            stepTrades,
            trade.amountY,
            orderSizeHat,
            spotPre,
            spotPost,
            pStep,
            bidPrev,
            askPrev
        );

        uint256 gammaBid = _gammaFromFee(bidPrev);
        uint256 gammaAsk = _gammaFromFee(askPrev);
        uint256 lStrong = wmul(spotPost, gammaBid);
        uint256 uStrong = wdiv(spotPost, gammaAsk);
        uint256 pAnchor = trade.isBuy ? lStrong : uStrong;

        if (probableArb) {
            if (lStrong > pLow) pLow = lStrong;
            if (uStrong < pHigh) pHigh = uStrong;
        } else {
            uint256 eps = bpsToWad(EPS_WEAK_BPS);
            uint256 lAdj = wmul(lStrong, WAD - eps);
            uint256 uAdj = wmul(uStrong, WAD + eps);
            if (lAdj > pLow) pLow = _ewma(pLow, lAdj, A_WEAK);
            if (uAdj < pHigh) pHigh = _ewma(pHigh, uAdj, A_WEAK);
        }

        if (pLow > pHigh) {
            uint256 midCollapse = _geomMeanOrFallback(lStrong, uStrong, pStep);
            uint256 buf = bpsToWad(COLLAPSE_BUF_BPS);
            pLow = wmul(midCollapse, WAD - buf);
            pHigh = wmul(midCollapse, WAD + buf);
        }

        uint256 midNow = _geomMeanOrFallback(pLow, pHigh, pStep);
        if (probableArb && !anchoredThisStep) {
            uint256 anchorW = ANCHOR_W + tailTier * 6e16;
            if (anchorW > 90e16) anchorW = 90e16;
            pStep = ((WAD - anchorW) * midNow + anchorW * pAnchor) / WAD;
            anchoredThisStep = true;
        } else if (anchoredThisStep) {
            // Keep anchored estimate fixed for the rest of this timestamp.
        } else {
            uint256 smooth = stepTrades >= 2 ? A_MID_SAME : A_MID;
            pStep = _ewma(pStep, midNow, smooth);
        }
        if (pStep < pLow) pStep = pLow;
        if (pStep > pHigh) pStep = pHigh;

        uint256 staleMag = _relDiff(spotPost, pStep);
        uint256 arbObs = probableArb ? WAD : 0;
        uint256 arbAlpha = stepTrades >= 2 ? A_ARB_SAME : A_ARB;
        arbHat = _ewma(arbHat, arbObs, arbAlpha);
        uint256 volObs = probableArb ? _relDiff(pAnchor, pStep) : _relDiff(spotPost, spotPre);
        volHat = _ewma(volHat, volObs, A_VOL);
        volHat = clamp(volHat, bpsToWad(2), bpsToWad(120));

        uint256 inferredOrderY = trade.amountY;
        if (!probableArb) {
            uint256 sizeRatio = orderSizeHat > MIN_ROUTE_AMOUNT ? wdiv(trade.amountY, orderSizeHat) : WAD;
            sizeRatio = clamp(sizeRatio, 25e16, 260e16);
            uint256 shareObs = clamp(34e16 + (sizeRatio * 16) / 100, 8e16, 95e16);
            if (stepTrades >= 2) {
                shareObs = clamp(shareObs + 8e16, 8e16, 95e16);
            }

            if (!trade.isBuy) {
                inferredOrderY = trade.amountY;
                shareBuyHat = _ewma(shareBuyHat, shareObs, A_SHARE);
                buyProbHat = _ewma(buyProbHat, WAD, A_BUY_PROB);
            } else {
                uint256 yEquiv = wmul(trade.amountX, pStep);
                if (yEquiv > trade.amountY) inferredOrderY = yEquiv;
                shareSellHat = _ewma(shareSellHat, shareObs, A_SHARE);
                buyProbHat = _ewma(buyProbHat, 0, A_BUY_PROB);
            }
        } else {
            // Pull share priors gently back to center on probable arb callbacks.
            shareBuyHat = _ewma(shareBuyHat, 50e16, 3e16);
            shareSellHat = _ewma(shareSellHat, 50e16, 3e16);
        }

        if (inferredOrderY < MIN_ROUTE_AMOUNT) inferredOrderY = MIN_ROUTE_AMOUNT;
        orderSizeHat = _ewma(orderSizeHat, inferredOrderY, A_ORDER);
        orderSizeHat = clamp(orderSizeHat, 5e17, 80 * WAD);
        buyProbHat = clamp(buyProbHat, 8e16, 92e16);

        (int256 invSigned, uint256 invAbs) = _inventorySigned(pStep, trade.reserveX, trade.reserveY);

        (uint256 reqBidBps, uint256 reqAskBps) = _requiredNoArbFeesBps(pStep, spotPost);
        bool bidShieldActive = reqBidBps > SHIELD_TRIGGER_BPS;
        bool askShieldActive = reqAskBps > SHIELD_TRIGGER_BPS;
        uint256 bidFloor = bidShieldActive ? clamp(reqBidBps + SHIELD_BUFFER_BPS, MIN_BPS, MAX_BPS) : MIN_BPS;
        uint256 askFloor = askShieldActive ? clamp(reqAskBps + SHIELD_BUFFER_BPS, MIN_BPS, MAX_BPS) : MIN_BPS;

        bool zeroBidOk = askShieldActive && !bidShieldActive && invSigned < 0;
        bool zeroAskOk = bidShieldActive && !askShieldActive && invSigned > 0;

        uint256 midTarget = _midTargetBps(lambdaHat, arbHat, volHat, stepTrades, probableArb);
        uint256 fairTilt = _fairTiltBps(staleMag);
        int256 fairSign = 0;
        if (spotPost > pStep) fairSign = int256(fairTilt);
        else if (spotPost < pStep) fairSign = -int256(fairTilt);

        uint256 invTilt = _invTiltBps(invAbs);
        int256 invSign = 0;
        if (invSigned > 0) invSign = int256(invTilt);
        else if (invSigned < 0) invSign = -int256(invTilt);

        int256 shareSkew = (int256(shareSellHat) - int256(shareBuyHat)) / int256(14e16);
        shareSkew = _clampSigned(shareSkew, -3, 3);
        int256 skewTarget = _clampSigned(
            (fairSign * 5 + invSign * 6) / 11 + shareSkew,
            -int256(MAX_SKEW_BPS),
            int256(MAX_SKEW_BPS)
        );

        int256 shareBias = 0;

        int256 bestScore = type(int256).min;
        uint256 bestBid = bidPrevBps;
        uint256 bestAsk = askPrevBps;

        int256[3] memory midOffsets = [int256(-3), int256(0), int256(3)];
        int256[3] memory skewOffsets = [int256(-4), int256(0), int256(4)];

        for (uint256 i = 0; i < 3; i++) {
            for (uint256 j = 0; j < 3; j++) {
                uint256 candMid = _clampSignedToUint(
                    int256(midTarget) + midOffsets[i], MIN_BPS, TARGET_MAX_BPS
                );
                int256 candSkew = _clampSigned(
                    skewTarget + skewOffsets[j],
                    -int256(MAX_SKEW_BPS),
                    int256(MAX_SKEW_BPS)
                );

                uint256 candBid = _clampSignedToUint(int256(candMid) + candSkew, MIN_BPS, MAX_BPS);
                uint256 candAsk = _clampSignedToUint(int256(candMid) - candSkew, MIN_BPS, MAX_BPS);

                (candBid, candAsk) = _applyCandidateConstraints(
                    candBid,
                    candAsk,
                    bidPrevBps,
                    askPrevBps,
                    bidShieldActive,
                    askShieldActive,
                    bidFloor,
                    askFloor
                );

                int256 score = _scoreCandidate(
                    candBid,
                    candAsk,
                    bidPrevBps,
                    askPrevBps,
                    lambdaHat,
                    arbHat,
                    volHat,
                    orderSizeHat,
                    buyProbHat,
                    shareBuyHat,
                    shareSellHat,
                    shareBias,
                    spotPost,
                    pStep,
                    invAbs,
                    invSigned
                );

                if (score > bestScore) {
                    bestScore = score;
                    bestBid = candBid;
                    bestAsk = candAsk;
                } else if (score == bestScore) {
                    uint256 currDist = absDiff(bestBid, NORM_BPS) + absDiff(bestAsk, NORM_BPS);
                    uint256 nextDist = absDiff(candBid, NORM_BPS) + absDiff(candAsk, NORM_BPS);
                    if (nextDist < currDist) {
                        bestBid = candBid;
                        bestAsk = candAsk;
                    }
                }
            }
        }

        if (zeroBidOk) {
            (uint256 zBid, uint256 zAsk) = _applyCandidateConstraints(
                0,
                bestAsk,
                bidPrevBps,
                askPrevBps,
                bidShieldActive,
                askShieldActive,
                bidFloor,
                askFloor
            );
            int256 zScore = _scoreCandidate(
                zBid,
                zAsk,
                bidPrevBps,
                askPrevBps,
                lambdaHat,
                arbHat,
                volHat,
                orderSizeHat,
                buyProbHat,
                shareBuyHat,
                shareSellHat,
                shareBias,
                spotPost,
                pStep,
                invAbs,
                invSigned
            );
            if (zScore > bestScore) {
                bestScore = zScore;
                bestBid = zBid;
                bestAsk = zAsk;
            }
        }

        if (zeroAskOk) {
            (uint256 zBid2, uint256 zAsk2) = _applyCandidateConstraints(
                bestBid,
                0,
                bidPrevBps,
                askPrevBps,
                bidShieldActive,
                askShieldActive,
                bidFloor,
                askFloor
            );
            int256 zScore2 = _scoreCandidate(
                zBid2,
                zAsk2,
                bidPrevBps,
                askPrevBps,
                lambdaHat,
                arbHat,
                volHat,
                orderSizeHat,
                buyProbHat,
                shareBuyHat,
                shareSellHat,
                shareBias,
                spotPost,
                pStep,
                invAbs,
                invSigned
            );
            if (zScore2 > bestScore) {
                bestBid = zBid2;
                bestAsk = zAsk2;
            }
        }

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
        slots[11] = orderSizeHat;
        slots[12] = buyProbHat;
        slots[13] = shareBuyHat;
        slots[14] = shareSellHat;

        return (bidOut, askOut);
    }

    function getName() external pure override returns (string memory) {
        return "PowellBase_AdaptiveGasSafe_v1";
    }

    function _scoreCandidate(
        uint256 bidBps,
        uint256 askBps,
        uint256 curBidBps,
        uint256 curAskBps,
        uint256 lambdaHat,
        uint256 arbHat,
        uint256 volHat,
        uint256 orderSizeHat,
        uint256 buyProbHat,
        uint256 shareBuyHat,
        uint256 shareSellHat,
        int256 shareBias,
        uint256 spot,
        uint256 pStep,
        uint256 invAbs,
        int256 invSigned
    ) internal pure returns (int256) {
        uint256 bidFee = bpsToWad(bidBps);
        uint256 askFee = bpsToWad(askBps);

        uint256 buyProb = clamp(buyProbHat, 8e16, 92e16);
        uint256 sellProb = WAD - buyProb;
        uint256 orderY = clamp(orderSizeHat, 5e17, 80 * WAD);

        uint256 shareBuy = _predictShare(shareBuyHat, askBps, true, shareBias);
        uint256 shareSell = _predictShare(shareSellHat, bidBps, false, shareBias);

        uint256 yBuyCap = wmul(wmul(orderY, buyProb), shareBuy);
        uint256 ySellCap = wmul(wmul(orderY, sellProb), shareSell);
        uint256 retailEdge = wmul(yBuyCap, askFee) + wmul(ySellCap, bidFee);
        retailEdge = wmul(retailEdge, clamp(lambdaHat, 25e16, 250e16));

        uint256 spread = (bidFee + askFee) / 2;
        uint256 stale = _relDiff(spot, pStep);
        uint256 tox = stale + (volHat / 2);
        uint256 arbBase = wmul(wmul(orderY, arbHat), tox);
        uint256 arbShield = WAD + spread * 7;
        uint256 arbCost = wdiv(arbBase, arbShield);

        uint256 invPenalty = wmul(wmul(orderY, invAbs), 12e16);

        uint256 spreadToNorm = absDiff(spread, bpsToWad(NORM_BPS));
        uint256 feeJump = bpsToWad(absDiff(bidBps, curBidBps) + absDiff(askBps, curAskBps));
        uint256 lambdaGap = absDiff(lambdaHat, WAD);

        uint256 vFactor = wmul(arbHat, 3e15)
            + wmul(stale, 12e15)
            + wmul(invAbs, 7e15)
            + wmul(lambdaGap, 1e15)
            + wmul(spreadToNorm, 6e15)
            + wmul(feeJump, 4e15);

        if (invSigned > 0 && askBps > bidBps) {
            vFactor += wmul(uint256(invSigned), bpsToWad(askBps - bidBps));
        } else if (invSigned < 0 && bidBps > askBps) {
            vFactor += wmul(uint256(-invSigned), bpsToWad(bidBps - askBps));
        }

        uint256 valuePenalty = wmul(orderY, vFactor);
        uint256 expectedShare = wmul(buyProb, shareBuy) + wmul(sellProb, shareSell);
        uint256 shareBonus = wmul(wmul(orderY, expectedShare), 2e14);

        return int256(retailEdge + shareBonus) - int256(arbCost) - int256(invPenalty) - int256(valuePenalty);
    }

    function _predictShare(uint256 baseShare, uint256 feeBps, bool buySide, int256 shareBias)
        internal
        pure
        returns (uint256)
    {
        int256 s = int256(clamp(baseShare, 5e16, 95e16));
        int256 delta = int256(NORM_BPS) - int256(feeBps);
        s += delta * int256(SHARE_KAPPA_PER_BPS);
        if (buySide) s += shareBias;
        else s -= shareBias;

        if (s < int256(1e16)) return 1e16;
        if (s > int256(99e16)) return 99e16;
        return uint256(s);
    }

    function _classifyProbableArb(
        bool isBuy,
        uint256 stepTrades,
        uint256 amountY,
        uint256 orderSizeHat,
        uint256 spotPre,
        uint256 spotPost,
        uint256 pStep,
        uint256 bidFeeUsed,
        uint256 askFeeUsed
    ) internal pure returns (bool, uint256) {
        if (stepTrades >= 2) {
            return (false, 0);
        }

        uint256 anchor = isBuy
            ? wmul(spotPost, _gammaFromFee(bidFeeUsed))
            : wdiv(spotPost, _gammaFromFee(askFeeUsed));

        bool directionOk = isBuy ? (spotPost <= spotPre) : (spotPost >= spotPre);
        uint256 relAnchor = _relDiff(anchor, pStep);
        uint256 relMove = _relDiff(spotPost, spotPre);

        uint256 ratio = orderSizeHat > MIN_ROUTE_AMOUNT ? wdiv(amountY, orderSizeHat) : WAD;
        bool smallAbs = amountY <= SIZE_SMALL_ABS_Y;
        bool smallRel = ratio <= SIZE_SMALL_REL_WAD;

        uint256 tailTier = _sizeTailTier(ratio);
        bool sizeSignal = smallAbs || smallRel || tailTier > 0;

        uint256 anchorGate = bpsToWad(ARB_ANCHOR_GATE_BPS);
        uint256 moveFloor = bpsToWad(ARB_MOVE_FLOOR_BPS);
        bool baseArb = directionOk && relAnchor <= anchorGate && relMove >= moveFloor;
        if (!sizeSignal) {
            return (baseArb, tailTier);
        }

        uint256 gateMult = 100;
        uint256 moveMult = 100;

        if (smallAbs || smallRel) {
            gateMult = 140;
            moveMult = 65;
        }
        if (tailTier == 1) {
            if (gateMult < 165) gateMult = 165;
            if (moveMult > 55) moveMult = 55;
        } else if (tailTier == 2) {
            if (gateMult < 205) gateMult = 205;
            if (moveMult > 40) moveMult = 40;
        }

        uint256 relaxedGate = (anchorGate * gateMult) / 100;
        uint256 relaxedMove = (moveFloor * moveMult) / 100;
        bool relaxedArb = directionOk && relAnchor <= relaxedGate && relMove >= relaxedMove;
        return (baseArb || relaxedArb, tailTier);
    }

    function _sizeTailTier(uint256 ratioWad) internal pure returns (uint256) {
        if (ratioWad <= 45e16 || ratioWad >= 230e16) return 2;
        if (ratioWad <= 65e16 || ratioWad >= 170e16) return 1;
        return 0;
    }

    function _midTargetBps(
        uint256 lambdaHat,
        uint256 arbHat,
        uint256 volHat,
        uint256 stepTrades,
        bool probableArb
    ) internal pure returns (uint256) {
        int256 mid = int256(PRIOR_BPS);
        mid += (int256(lambdaHat) - int256(95e16)) / int256(8e16);
        mid += (int256(arbHat) - int256(45e16)) / int256(6e16);

        uint256 volBps = wadToBps(volHat);
        mid += (int256(volBps) - 9) / 2;

        if (stepTrades >= 2) mid -= 2;
        if (probableArb) mid += 2;
        return _clampSignedToUint(mid, 8, TARGET_MAX_BPS);
    }

    function _fairTiltBps(uint256 staleWad) internal pure returns (uint256) {
        if (staleWad > bpsToWad(30)) return 7;
        if (staleWad > bpsToWad(16)) return 4;
        if (staleWad > bpsToWad(8)) return 2;
        return 0;
    }

    function _invTiltBps(uint256 invAbsWad) internal pure returns (uint256) {
        uint256 b = wadToBps(invAbsWad);
        if (b > 900) return 9;
        if (b > 620) return 7;
        if (b > 360) return 5;
        if (b > 180) return 3;
        return 0;
    }

    function _requiredNoArbFeesBps(uint256 pStep, uint256 spot)
        internal
        pure
        returns (uint256 bidReqBps, uint256 askReqBps)
    {
        if (pStep == 0 || spot == 0) return (0, 0);

        uint256 pOverSpot = wdiv(pStep, spot);
        uint256 spotOverP = wdiv(spot, pStep);
        uint256 bidReq = pOverSpot < WAD ? (WAD - pOverSpot) : 0;
        uint256 askReq = spotOverP < WAD ? (WAD - spotOverP) : 0;

        if (bidReq > MAX_FEE) bidReq = MAX_FEE;
        if (askReq > MAX_FEE) askReq = MAX_FEE;
        return (wadToBps(bidReq), wadToBps(askReq));
    }

    function _inventorySigned(uint256 pStep, uint256 reserveX, uint256 reserveY)
        internal
        pure
        returns (int256 invSigned, uint256 invAbs)
    {
        uint256 valueX = wmul(pStep, reserveX);
        uint256 total = valueX + reserveY + 1;
        if (valueX >= reserveY) {
            invAbs = wdiv(valueX - reserveY, total);
            invSigned = int256(invAbs);
        } else {
            invAbs = wdiv(reserveY - valueX, total);
            invSigned = -int256(invAbs);
        }
    }

    function _lambdaObservation(uint256 deltaT, uint256 prevStepTrades)
        internal
        pure
        returns (uint256)
    {
        uint256 gapObs;
        if (deltaT <= 1) gapObs = 100e16;
        else if (deltaT == 2) gapObs = 78e16;
        else if (deltaT == 3) gapObs = 62e16;
        else if (deltaT <= 5) gapObs = 50e16;
        else if (deltaT <= 9) gapObs = 40e16;
        else gapObs = 28e16;

        uint256 burstObs;
        if (prevStepTrades >= 4) burstObs = 160e16;
        else if (prevStepTrades == 3) burstObs = 130e16;
        else if (prevStepTrades == 2) burstObs = 105e16;
        else burstObs = 80e16;

        return clamp((gapObs * 7 + burstObs * 3) / 10, 25e16, 250e16);
    }

    function _sameStepLambdaObservation(uint256 stepTrades) internal pure returns (uint256) {
        if (stepTrades >= 5) return 250e16;
        if (stepTrades == 4) return 220e16;
        if (stepTrades == 3) return 185e16;
        if (stepTrades == 2) return 145e16;
        return 110e16;
    }

    function _applyCandidateConstraints(
        uint256 bidBps,
        uint256 askBps,
        uint256 bidPrevBps,
        uint256 askPrevBps,
        bool bidShieldActive,
        bool askShieldActive,
        uint256 bidFloor,
        uint256 askFloor
    ) internal pure returns (uint256, uint256) {
        uint256 b = clamp(bidBps, MIN_BPS, MAX_BPS);
        uint256 a = clamp(askBps, MIN_BPS, MAX_BPS);

        if (bidShieldActive) {
            if (b < bidFloor) b = bidFloor;
        } else {
            b = _limitJumpBps(b, bidPrevBps, MAX_JUMP_BPS);
        }

        if (askShieldActive) {
            if (a < askFloor) a = askFloor;
        } else {
            a = _limitJumpBps(a, askPrevBps, MAX_JUMP_BPS);
        }

        if (!bidShieldActive && !askShieldActive) {
            (b, a) = _enforceAsymmetry(b, a, MAX_ASYM_BPS);
        }

        b = clamp(b, MIN_BPS, MAX_BPS);
        a = clamp(a, MIN_BPS, MAX_BPS);
        return (b, a);
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

    function _ewma(uint256 oldV, uint256 obs, uint256 alpha) internal pure returns (uint256) {
        if (oldV == 0) return obs;
        uint256 a = clamp(alpha, 0, WAD);
        uint256 beta = WAD - a;
        return (oldV * beta + obs * a) / WAD;
    }

    function _packStepState(uint256 timestamp, uint256 stepTrades, bool anchored)
        internal
        pure
        returns (uint256)
    {
        uint256 t = timestamp << 32;
        uint256 a = anchored ? FLAG_ANCHORED : 0;
        uint256 s = stepTrades & STEP_MASK;
        return t | a | s;
    }

    function _unpackStepState(uint256 state)
        internal
        pure
        returns (uint256 timestamp, uint256 stepTrades, bool anchored)
    {
        timestamp = state >> 32;
        uint256 low = state & 0xffffffff;
        anchored = (low & FLAG_ANCHORED) != 0;
        stepTrades = low & STEP_MASK;
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
