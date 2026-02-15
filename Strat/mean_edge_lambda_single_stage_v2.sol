// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

contract Strategy is AMMStrategyBase {
    uint256 private constant A_ARB = 12e16;
    uint256 private constant A_VOL_ARB = 16e16;
    uint256 private constant A_VOL_SPOT = 4e16;
    uint256 private constant A_PRICE_ARB = 19e16;
    uint256 private constant A_PRICE_SPOT = 5e16;

    uint256 private constant A_LAMBDA = 18e16;
    uint256 private constant A_GAP = 14e16;
    uint256 private constant A_FLOW = 12e16;

    uint256 private constant OPEN_BPS = 58;
    uint256 private constant QUOTE_MIN_BPS = 14;
    uint256 private constant QUOTE_MAX_BPS = 82;
    uint256 private constant MAX_JUMP_BPS = 6;

    uint256 private constant FAIR_T1 = 8;
    uint256 private constant FAIR_T2 = 20;
    uint256 private constant FAIR_T3 = 34;
    uint256 private constant FAIR_S1 = 1;
    uint256 private constant FAIR_S2 = 2;
    uint256 private constant FAIR_S3 = 3;

    uint256 private constant INV_T1 = 300;
    uint256 private constant INV_T2 = 500;
    uint256 private constant INV_T3 = 800;
    uint256 private constant INV_S1 = 2;
    uint256 private constant INV_S2 = 4;
    uint256 private constant INV_S3 = 6;
    uint256 private constant INV_CONFLICT_DIV = 2;

    uint256 private constant ARB_RELAX = 5;
    uint256 private constant RETAIL_CONF_BUMP = 3;

    function getName() external pure override returns (string memory) {
        return "MeanEdge_LambdaSingleStage_v2";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(OPEN_BPS);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        slots[0] = open; // last bid fee (WAD)
        slots[1] = open; // last ask fee (WAD)
        slots[2] = 0; // last timestamp
        slots[3] = 0; // current step trade count
        slots[4] = 0; // arb probability EWMA
        slots[5] = 0; // vol EWMA
        slots[6] = 0; // lambda_hat
        slots[7] = p0; // last arb-implied fair
        slots[8] = p0; // fair price estimate
        slots[9] = 0; // reserved
        slots[10] = initialX; // previous reserve x
        slots[11] = initialY; // previous reserve y
        slots[12] = 0; // retail-classified trades in current step
        slots[13] = 0; // arb-classified trades in current step
        slots[14] = 0; // gap arrival score EWMA
        slots[15] = 0; // per-step flow score EWMA
        return (open, open);
    }

    function afterSwap(TradeInfo calldata trade)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 xPre = slots[10];
        uint256 yPre = slots[11];
        if (xPre == 0 || yPre == 0) {
            xPre = trade.reserveX;
            yPre = trade.reserveY;
        }

        uint256 pHat = slots[8];
        if (pHat == 0) pHat = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);

        uint256 lastTs = slots[2];
        uint256 stepTrades = slots[3];
        uint256 stepRetail = slots[12];
        uint256 stepArb = slots[13];

        uint256 bidPrevBps = wadToBps(slots[0]);
        uint256 askPrevBps = wadToBps(slots[1]);
        if (bidPrevBps == 0) bidPrevBps = OPEN_BPS;
        if (askPrevBps == 0) askPrevBps = OPEN_BPS;
        uint256 midPrevBps = (bidPrevBps + askPrevBps) / 2;

        uint256 lambdaHat = slots[6];
        if (lambdaHat == 0) lambdaHat = 58e16;
        uint256 gapHat = slots[14];
        if (gapHat == 0) gapHat = 58e16;
        uint256 flowHat = slots[15];
        if (flowHat == 0) flowHat = 58e16;

        if (trade.timestamp != lastTs) {
            if (lastTs != 0 || stepTrades > 0) {
                uint256 dt = trade.timestamp > lastTs ? (trade.timestamp - lastTs) : 1;
                gapHat = _ewma(gapHat, _gapObs(dt), A_GAP);
                flowHat = _ewma(flowHat, _flowObs(stepTrades), A_FLOW);
                uint256 stepObs = _stepLambdaObs(stepRetail, stepArb, stepTrades, dt);
                lambdaHat = _ewma(lambdaHat, _applyShareCorrection(stepObs, midPrevBps), A_LAMBDA);
            }
            lastTs = trade.timestamp;
            stepTrades = 0;
            stepRetail = 0;
            stepArb = 0;
        }

        stepTrades += 1;

        uint256 arbProb = slots[4];
        if (arbProb == 0) arbProb = WAD / 2;
        uint256 volHat = slots[5];
        if (volHat == 0) volHat = bpsToWad(95) / 10;
        uint256 lastArbP = slots[7];
        if (lastArbP == 0) lastArbP = pHat;

        uint256 spotPre = _safePrice(yPre, xPre, pHat);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pHat);

        bool probableArb = false;
        uint256 pObs = spotPost;

        if (stepTrades == 1) {
            uint256 feeUsed = trade.isBuy ? slots[0] : slots[1];
            if (feeUsed == 0) feeUsed = bpsToWad(OPEN_BPS);
            uint256 gamma = _gammaFromFee(feeUsed);
            pObs = trade.isBuy ? wmul(spotPost, gamma) : wdiv(spotPost, gamma);

            bool directionOk = trade.isBuy ? (spotPre > pObs) : (spotPre < pObs);
            uint256 relToHat = _relDiff(pObs, pHat);
            if (directionOk && relToHat <= bpsToWad(98)) probableArb = true;
        }

        if (probableArb) {
            stepArb += 1;
            arbProb = _ewma(arbProb, WAD, A_ARB);
            volHat = _ewma(volHat, _relDiff(pObs, lastArbP), A_VOL_ARB);
            pHat = _ewma(pHat, pObs, A_PRICE_ARB);
            lastArbP = pObs;
            lambdaHat = _ewma(lambdaHat, 24e16, A_LAMBDA);
        } else {
            stepRetail += 1;
            arbProb = _ewma(arbProb, 0, A_ARB);
            volHat = _ewma(volHat, _relDiff(spotPost, spotPre), A_VOL_SPOT);
            pHat = _ewma(pHat, spotPost, A_PRICE_SPOT);
            uint256 tradeObs = _tradeLambdaObs(stepTrades);
            lambdaHat = _ewma(lambdaHat, _applyShareCorrection(tradeObs, midPrevBps), A_LAMBDA);
        }

        uint256 lambdaProxy = (lambdaHat * 6 + gapHat * 2 + flowHat * 2) / 10;

        uint256 baseBps = _baseFromState(lambdaProxy, arbProb, volHat);
        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        uint256 stale = _relDiff(spotPost, pHat);
        bool fairPreferSellX = spotPost < pHat;
        uint256 fairTilt = _fairTiltBps(stale);
        if (fairTilt > 0) {
            if (fairPreferSellX) {
                if (askBps > fairTilt) askBps -= fairTilt;
                bidBps += fairTilt;
            } else {
                if (bidBps > fairTilt) bidBps -= fairTilt;
                askBps += fairTilt;
            }
        }

        uint256 valueX = wmul(pHat, trade.reserveX);
        bool invPreferSellX = valueX > trade.reserveY;
        uint256 invRatio = wdiv(_absDiff(valueX, trade.reserveY), valueX + trade.reserveY + 1);
        uint256 invTilt = _inventoryTiltBps(invRatio);

        if (invTilt > 0 && invPreferSellX != fairPreferSellX) {
            invTilt /= INV_CONFLICT_DIV;
        }
        if (invTilt > 0) {
            if (invPreferSellX) {
                if (askBps > invTilt) askBps -= invTilt;
                bidBps += invTilt;
            } else {
                if (bidBps > invTilt) bidBps -= invTilt;
                askBps += invTilt;
            }
        }

        if (probableArb) {
            if (bidBps > ARB_RELAX) bidBps -= ARB_RELAX;
            if (askBps > ARB_RELAX) askBps -= ARB_RELAX;
        } else if (stepTrades > 1) {
            bidBps += RETAIL_CONF_BUMP;
            askBps += RETAIL_CONF_BUMP;
        }

        bidBps = _limitJump(bidBps, bidPrevBps);
        askBps = _limitJump(askBps, askPrevBps);
        bidBps = _clampBps(bidBps, QUOTE_MIN_BPS, QUOTE_MAX_BPS);
        askBps = _clampBps(askBps, QUOTE_MIN_BPS, QUOTE_MAX_BPS);

        uint256 bidOut = clampFee(bpsToWad(bidBps));
        uint256 askOut = clampFee(bpsToWad(askBps));

        slots[0] = bidOut;
        slots[1] = askOut;
        slots[2] = lastTs;
        slots[3] = stepTrades;
        slots[4] = arbProb;
        slots[5] = volHat;
        slots[6] = lambdaHat;
        slots[7] = lastArbP;
        slots[8] = pHat;
        slots[10] = trade.reserveX;
        slots[11] = trade.reserveY;
        slots[12] = stepRetail;
        slots[13] = stepArb;
        slots[14] = gapHat;
        slots[15] = flowHat;
        return (bidOut, askOut);
    }

    function _baseFromState(uint256 lambdaProxy, uint256 arbProb, uint256 volHat)
        internal
        pure
        returns (uint256)
    {
        uint256 base = 54;

        // Primary driver: inferred retail intensity.
        if (lambdaProxy > 88e16) {
            base += 22;
        } else if (lambdaProxy > 80e16) {
            base += 16;
        } else if (lambdaProxy > 72e16) {
            base += 10;
        } else if (lambdaProxy > 64e16) {
            base += 5;
        } else if (lambdaProxy < 40e16) {
            if (base > 14) base -= 14;
        } else if (lambdaProxy < 50e16) {
            if (base > 8) base -= 8;
        } else if (lambdaProxy < 58e16) {
            if (base > 4) base -= 4;
        }

        // Secondary driver: arb toxicity.
        if (arbProb > 67e16) {
            if (base > 8) base -= 8;
        } else if (arbProb > 58e16) {
            if (base > 4) base -= 4;
        } else if (arbProb < 34e16) {
            base += 8;
        } else if (arbProb < 44e16) {
            base += 4;
        }

        // Tertiary driver: light volatility correction.
        if (volHat > bpsToWad(103) / 10) {
            if (base > 2) base -= 2;
        } else if (volHat < bpsToWad(86) / 10) {
            base += 2;
        }

        return _clampBps(base, QUOTE_MIN_BPS, QUOTE_MAX_BPS);
    }

    function _tradeLambdaObs(uint256 stepTrades) internal pure returns (uint256) {
        if (stepTrades >= 4) return WAD;
        if (stepTrades == 3) return 95e16;
        if (stepTrades == 2) return 88e16;
        return 63e16;
    }

    function _stepLambdaObs(uint256 stepRetail, uint256 stepArb, uint256 stepTrades, uint256 dt)
        internal
        pure
        returns (uint256)
    {
        if (stepTrades == 0) {
            if (dt > 5) return 16e16;
            if (dt > 3) return 28e16;
            if (dt > 1) return 40e16;
            return 50e16;
        }
        if (stepArb == 0) {
            if (stepRetail >= 3) return 97e16;
            if (stepRetail == 2) return 86e16;
            return 68e16;
        }
        if (stepRetail > stepArb) return 56e16;
        return 22e16;
    }

    function _applyShareCorrection(uint256 obs, uint256 midPrevBps) internal pure returns (uint256) {
        if (midPrevBps <= 30) return obs;
        if (midPrevBps <= 40) return (obs * 110) / 100;
        if (midPrevBps <= 52) return (obs * 125) / 100;
        return (obs * 140) / 100;
    }

    function _gapObs(uint256 dt) internal pure returns (uint256) {
        if (dt <= 1) return 92e16;
        if (dt == 2) return 74e16;
        if (dt == 3) return 60e16;
        if (dt <= 5) return 46e16;
        return 28e16;
    }

    function _flowObs(uint256 stepTrades) internal pure returns (uint256) {
        if (stepTrades >= 4) return WAD;
        if (stepTrades == 3) return 90e16;
        if (stepTrades == 2) return 78e16;
        if (stepTrades == 1) return 62e16;
        return 26e16;
    }

    function _fairTiltBps(uint256 staleWad) internal pure returns (uint256) {
        if (staleWad > bpsToWad(FAIR_T3)) return FAIR_S3;
        if (staleWad > bpsToWad(FAIR_T2)) return FAIR_S2;
        if (staleWad > bpsToWad(FAIR_T1)) return FAIR_S1;
        return 0;
    }

    function _inventoryTiltBps(uint256 invRatioWad) internal pure returns (uint256) {
        if (invRatioWad > bpsToWad(INV_T3)) return INV_S3;
        if (invRatioWad > bpsToWad(INV_T2)) return INV_S2;
        if (invRatioWad > bpsToWad(INV_T1)) return INV_S1;
        return 0;
    }

    function _limitJump(uint256 target, uint256 prev) internal pure returns (uint256) {
        if (target > prev + MAX_JUMP_BPS) return prev + MAX_JUMP_BPS;
        if (target + MAX_JUMP_BPS < prev) return prev - MAX_JUMP_BPS;
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

    function _ewma(uint256 oldV, uint256 obs, uint256 alpha) internal pure returns (uint256) {
        if (oldV == 0) return obs;
        uint256 beta = WAD - alpha;
        return (oldV * beta) / WAD + (obs * alpha) / WAD;
    }

    function _relDiff(uint256 a, uint256 b) internal pure returns (uint256) {
        if (b == 0) return 0;
        return wdiv(_absDiff(a, b), b);
    }

    function _absDiff(uint256 a, uint256 b) internal pure returns (uint256) {
        return a > b ? a - b : b - a;
    }

    function _clampBps(uint256 value, uint256 minBps, uint256 maxBps) internal pure returns (uint256) {
        if (value < minBps) return minBps;
        if (value > maxBps) return maxBps;
        return value;
    }
}
