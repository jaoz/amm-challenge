// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// Tight world-state adaptation around the empirically strong fee band.
contract Strategy is AMMStrategyBase {
    uint256 private constant A_ARB = 10e16;       // 0.10
    uint256 private constant A_VOL = 22e16;       // 0.22
    uint256 private constant A_RETAIL = 7e16;     // 0.07
    uint256 private constant A_PRICE_ARB = 18e16; // 0.18
    uint256 private constant A_PRICE_SPOT = 4e16; // 0.04

    // slots
    //  0 bid fee (WAD)
    //  1 ask fee (WAD)
    //  2 last timestamp
    //  3 step trade count
    //  4 arb probability EWMA (WAD)
    //  5 volatility EWMA (WAD)
    //  6 retail pressure EWMA (WAD)
    //  7 last arb-implied fair price (WAD)
    //  8 pHat (WAD)
    //  9 total trade count
    // 10 previous reserveX (WAD)
    // 11 previous reserveY (WAD)

    function getName() external pure override returns (string memory) {
        return "WorldState_BandShift_v4_20260214";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(80);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        slots[0] = open;
        slots[1] = open;
        slots[2] = 0;
        slots[3] = 0;
        slots[4] = WAD / 2;
        slots[5] = bpsToWad(95) / 10; // 9.5 bps prior
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
        if (bidPrev == 0) bidPrev = bpsToWad(80);
        if (askPrev == 0) askPrev = bpsToWad(80);

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

            if (directionOk && relToHat <= bpsToWad(115)) {
                probableArb = true;
            }
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
                retailHat = _ewma(retailHat, WAD, 16e16);
            }
        }

        uint256 tradeCount = slots[9] + 1;
        uint256 baseBps = _baseFromState(volHat, arbProb, retailHat, tradeCount);

        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        // Small inventory skew.
        uint256 valueX = wmul(pHat, trade.reserveX);
        bool longX = valueX > trade.reserveY;
        uint256 invNum = _absDiff(valueX, trade.reserveY);
        uint256 invDen = valueX + trade.reserveY + 1;
        uint256 invRatio = wdiv(invNum, invDen);

        uint256 tilt = 1;
        if (invRatio > bpsToWad(550)) tilt = 3;
        else if (invRatio > bpsToWad(300)) tilt = 2;

        if (longX) {
            bidBps += tilt;
            if (askBps > tilt) askBps -= tilt;
        } else {
            askBps += tilt;
            if (bidBps > tilt) bidBps -= tilt;
        }

        if (probableArb) {
            if (bidBps > 2) bidBps -= 2;
            if (askBps > 2) askBps -= 2;
        } else if (stepTrades == 1) {
            bidBps += 1;
            askBps += 1;
        }

        bidBps = _clampBps(bidBps, 68, 108);
        askBps = _clampBps(askBps, 68, 108);

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

    function _baseFromState(
        uint256 volHat,
        uint256 arbProb,
        uint256 retailHat,
        uint256 tradeCount
    ) internal pure returns (uint256) {
        uint256 base = tradeCount < 180 ? 78 : 80;

        // sigma around 8.8-10.1 bps.
        if (volHat > bpsToWad(103) / 10) {
            if (base > 8) base -= 8;
        } else if (volHat > bpsToWad(98) / 10) {
            if (base > 4) base -= 4;
        } else if (volHat < bpsToWad(89) / 10) {
            base += 13;
        } else if (volHat < bpsToWad(93) / 10) {
            base += 6;
        }

        if (arbProb > 62e16) {
            if (base > 4) base -= 4;
        } else if (arbProb < 38e16) {
            base += 6;
        }

        if (retailHat > 72e16) {
            base += 5;
        } else if (retailHat < 42e16) {
            if (base > 2) base -= 2;
        }

        return _clampBps(base, 70, 104);
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
