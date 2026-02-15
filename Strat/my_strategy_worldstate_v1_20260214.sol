// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// World-state adaptive strategy:
/// - estimates arb pressure and realized volatility from first trade each step
/// - estimates retail pressure from non-arb flow
/// - shifts a base fee band by regime, then applies inventory skew
contract Strategy is AMMStrategyBase {
    uint256 private constant A_ARB = 8e16;      // 0.08
    uint256 private constant A_VOL = 25e16;     // 0.25
    uint256 private constant A_RETAIL = 6e16;   // 0.06
    uint256 private constant A_PRICE = 5e16;    // 0.05

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
        return "WorldState_BandShift_v1_20260214";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(82);
        uint256 p0 = _safePrice(initialY, initialX, 100 * WAD);

        slots[0] = open;
        slots[1] = open;
        slots[2] = 0;
        slots[3] = 0;
        slots[4] = WAD / 2;
        slots[5] = bpsToWad(9);   // ~0.09% prior
        slots[6] = WAD / 3;
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
        if (bidPrev == 0) bidPrev = bpsToWad(82);
        if (askPrev == 0) askPrev = bpsToWad(82);

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
        if (volHat == 0) volHat = bpsToWad(9);
        uint256 retailHat = slots[6];
        if (retailHat == 0) retailHat = WAD / 3;
        uint256 lastArbP = slots[7];
        if (lastArbP == 0) lastArbP = pHat;

        uint256 spotPre = _safePrice(yPre, xPre, pHat);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pHat);

        bool probableArb = false;
        uint256 pBoundary = spotPost;

        // Arb can only be first touch on a step for a given AMM.
        if (stepTrades == 1) {
            uint256 feeUsed = trade.isBuy ? bidPrev : askPrev;
            uint256 gamma = _gammaFromFee(feeUsed);

            // If this trade is arb, post spot should lie near no-arb boundary.
            pBoundary = trade.isBuy ? wmul(spotPost, gamma) : wdiv(spotPost, gamma);
            uint256 relBoundary = _relDiff(pBoundary, spotPre);
            bool directionOk = trade.isBuy ? (spotPre > pBoundary) : (spotPre < pBoundary);

            if (directionOk && relBoundary < bpsToWad(180)) {
                probableArb = true;
            }
        }

        if (probableArb) {
            arbProb = _ewma(arbProb, WAD, A_ARB);
            volHat = _ewma(volHat, _relDiff(pBoundary, lastArbP), A_VOL);
            pHat = _ewma(pHat, pBoundary, A_PRICE + 4e16);
            lastArbP = pBoundary;
            retailHat = _ewma(retailHat, 0, A_RETAIL);
        } else {
            arbProb = _ewma(arbProb, 0, A_ARB);
            retailHat = _ewma(retailHat, WAD, A_RETAIL);
            pHat = _ewma(pHat, spotPost, A_PRICE);

            if (stepTrades > 1) {
                retailHat = _ewma(retailHat, WAD, 20e16);
            }
        }

        uint256 tradeCount = slots[9] + 1;
        uint256 baseBps = _baseFromState(volHat, arbProb, retailHat, tradeCount);

        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        // Inventory skew around pHat.
        uint256 valueX = wmul(pHat, trade.reserveX);
        bool longX = valueX > trade.reserveY;
        uint256 invNum = _absDiff(valueX, trade.reserveY);
        uint256 invDen = valueX + trade.reserveY + 1;
        uint256 invRatio = wdiv(invNum, invDen);

        uint256 tilt = 2;
        if (invRatio > bpsToWad(700)) tilt = 10;
        else if (invRatio > bpsToWad(500)) tilt = 7;
        else if (invRatio > bpsToWad(300)) tilt = 5;
        else if (invRatio > bpsToWad(150)) tilt = 3;

        if (longX) {
            bidBps += tilt;
            if (askBps > tilt) askBps -= tilt;
        } else {
            askBps += tilt;
            if (bidBps > tilt) bidBps -= tilt;
        }

        // First touch looked retail: extract a bit more on same-step follow-through.
        if (!probableArb && stepTrades == 1) {
            bidBps += 3;
            askBps += 3;
        }

        // If stale relative to our pHat, tighten to re-clear quickly.
        uint256 stale = _relDiff(spotPost, pHat);
        if (stale > bpsToWad(60)) {
            if (bidBps > 18) bidBps -= 18;
            if (askBps > 18) askBps -= 18;
        } else if (stale > bpsToWad(35)) {
            if (bidBps > 10) bidBps -= 10;
            if (askBps > 10) askBps -= 10;
        }

        bidBps = _clampBps(bidBps, 20, 180);
        askBps = _clampBps(askBps, 20, 180);

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
        uint256 base = tradeCount < 250 ? 74 : 82;

        // sigma range in this game is around 8.8 to 10.1 bps per step.
        if (volHat > bpsToWad(106) / 10) {
            if (base > 16) base -= 16;
        } else if (volHat > bpsToWad(100) / 10) {
            if (base > 9) base -= 9;
        } else if (volHat < bpsToWad(88) / 10) {
            base += 24;
        } else if (volHat < bpsToWad(94) / 10) {
            base += 12;
        }

        if (arbProb > 62e16) {
            if (base > 14) base -= 14;
        } else if (arbProb > 52e16) {
            if (base > 8) base -= 8;
        } else if (arbProb < 34e16) {
            base += 12;
        } else if (arbProb < 42e16) {
            base += 6;
        }

        if (retailHat > 70e16) {
            base += 8;
        } else if (retailHat < 45e16) {
            if (base > 6) base -= 6;
        }

        return _clampBps(base, 25, 170);
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
