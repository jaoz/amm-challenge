// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

contract Strategy is AMMStrategyBase {
    uint256 private constant A_ARB = 9e16;
    uint256 private constant A_VOL = 19e16;
    uint256 private constant A_RETAIL = 5e16;
    uint256 private constant A_PRICE_ARB = 14e16;
    uint256 private constant A_PRICE_SPOT = 2e16;

    uint256 private constant FAIR_T1 = 4;
    uint256 private constant FAIR_T2 = 17;
    uint256 private constant FAIR_T3 = 32;
    uint256 private constant FAIR_S1 = 1;
    uint256 private constant FAIR_S2 = 4;
    uint256 private constant FAIR_S3 = 7;

    uint256 private constant INV_T1 = 301;
    uint256 private constant INV_T2 = 380;
    uint256 private constant INV_T3 = 584;
    uint256 private constant INV_S1 = 2;
    uint256 private constant INV_S2 = 4;
    uint256 private constant INV_S3 = 5;
    uint256 private constant INV_CONFLICT_DIV = 3;

    uint256 private constant FIRST_RETAIL_BUMP = 1;
    uint256 private constant ARB_RELAX = 8;
    uint256 private constant MULTI_STEP_RETAIL_ALPHA = 16e16;

    function getName() external pure override returns (string memory) {
        return "WorldState_TiltFair_best_worker_0";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(28);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        slots[0] = open;
        slots[1] = open;
        slots[2] = 0;
        slots[3] = 0;
        slots[4] = WAD / 2;
        slots[5] = bpsToWad(95) / 10;
        slots[6] = WAD / 2;
        slots[7] = p0;
        slots[8] = p0;
        slots[9] = 0;
        slots[10] = initialX;
        slots[11] = initialY;
        return (open, open);
    }

    function afterSwap(TradeInfo calldata trade)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 bidPrev = slots[0];
        uint256 askPrev = slots[1];
        if (bidPrev == 0) bidPrev = bpsToWad(28);
        if (askPrev == 0) askPrev = bpsToWad(28);

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
        if (trade.timestamp != lastTs) {
            lastTs = trade.timestamp;
            stepTrades = 0;
        }
        stepTrades += 1;

        uint256 arbProb = slots[4];
        if (arbProb == 0) arbProb = WAD / 2;
        uint256 volHat = slots[5];
        if (volHat == 0) volHat = bpsToWad(95) / 10;
        uint256 retailHat = slots[6];
        if (retailHat == 0) retailHat = WAD / 2;
        uint256 lastArbP = slots[7];
        if (lastArbP == 0) lastArbP = pHat;

        uint256 spotPre = _safePrice(yPre, xPre, pHat);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pHat);
        bool probableArb = false;
        uint256 pObs = spotPost;

        if (stepTrades == 1) {
            uint256 feeUsed = trade.isBuy ? bidPrev : askPrev;
            uint256 gamma = _gammaFromFee(feeUsed);
            pObs = trade.isBuy ? wmul(spotPost, gamma) : wdiv(spotPost, gamma);
            bool directionOk = trade.isBuy ? (spotPre > pObs) : (spotPre < pObs);
            uint256 relToHat = _relDiff(pObs, pHat);
            if (directionOk && relToHat <= bpsToWad(161)) probableArb = true;
        }

        if (probableArb) {
            arbProb = _ewma(arbProb, WAD, A_ARB);
            volHat = _ewma(volHat, _relDiff(pObs, lastArbP), A_VOL);
            pHat = _ewma(pHat, pObs, A_PRICE_ARB);
            lastArbP = pObs;
            retailHat = _ewma(retailHat, 0, A_RETAIL);
        } else {
            arbProb = _ewma(arbProb, 0, A_ARB);
            retailHat = _ewma(retailHat, WAD, A_RETAIL);
            pHat = _ewma(pHat, spotPost, A_PRICE_SPOT);
            if (stepTrades > 1) {
                retailHat = _ewma(retailHat, WAD, MULTI_STEP_RETAIL_ALPHA);
            }
        }

        uint256 tradeCount = slots[9] + 1;
        uint256 baseBps = _baseFromState(volHat, arbProb, retailHat, tradeCount);
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
        bool longX = valueX > trade.reserveY;
        bool invPreferSellX = longX;
        uint256 invNum = _absDiff(valueX, trade.reserveY);
        uint256 invDen = valueX + trade.reserveY + 1;
        uint256 invRatio = wdiv(invNum, invDen);
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

        if (!probableArb && stepTrades == 1) {
            bidBps += FIRST_RETAIL_BUMP;
            askBps += FIRST_RETAIL_BUMP;
        } else if (probableArb) {
            if (bidBps > ARB_RELAX) bidBps -= ARB_RELAX;
            if (askBps > ARB_RELAX) askBps -= ARB_RELAX;
        }

        bidBps = _clampBps(bidBps, 35, 82);
        askBps = _clampBps(askBps, 35, 82);

        uint256 bidOut = clampFee(bpsToWad(bidBps));
        uint256 askOut = clampFee(bpsToWad(askBps));

        slots[0] = bidOut;
        slots[1] = askOut;
        slots[2] = lastTs;
        slots[3] = stepTrades;
        slots[4] = arbProb;
        slots[5] = volHat;
        slots[6] = retailHat;
        slots[7] = lastArbP;
        slots[8] = pHat;
        slots[9] = tradeCount;
        slots[10] = trade.reserveX;
        slots[11] = trade.reserveY;
        return (bidOut, askOut);
    }

    function _baseFromState(uint256 volHat, uint256 arbProb, uint256 retailHat, uint256 tradeCount)
        internal pure returns (uint256)
    {
        uint256 base = tradeCount < 180 ? 75 : 33;

        if (volHat > bpsToWad(109) / 10) {
            if (base > 12) base -= 12;
        } else if (volHat > bpsToWad(91) / 10) {
            if (base > 7) base -= 7;
        } else if (volHat < bpsToWad(83) / 10) {
            base += 18;
        } else if (volHat < bpsToWad(86) / 10) {
            base += 6;
        }

        if (arbProb > 55e16) {
            if (base > 2) base -= 2;
        } else if (arbProb < 29e16) {
            base += 9;
        }

        if (retailHat > 73e16) {
            base += 4;
        } else if (retailHat < 36e16) {
            if (base > 3) base -= 3;
        }

        return _clampBps(base, 14, 78);
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
