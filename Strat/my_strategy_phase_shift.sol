// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// Volatility-regime strategy:
/// - keep generally high fees (retail capture)
/// - adapt run-level base fee from arb-implied volatility
/// - apply small inventory tilt
contract Strategy is AMMStrategyBase {
    // EWMA alphas
    uint256 private constant A_ARB_P = 30e16;   // 0.30
    uint256 private constant A_ARB_VOL = 20e16; // 0.20
    uint256 private constant A_SPOT_P = 5e16;   // 0.05

    // slots
    //  0 bidFee (WAD)
    //  1 askFee (WAD)
    //  2 lastX pre-trade
    //  3 lastY pre-trade
    //  4 pHat (WAD)
    //  5 volHat (WAD)
    //  6 lastArbPrice (WAD)
    //  7 lastTimestamp
    //  8 stepTradeCount
    //  9 prevStepHadRetail (0/1)
    // 10 stepRetailCount
    // 11 tradeCount

    function getName() external pure override returns (string memory) {
        return "PhaseShift_VolRegime_Opt";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 open = bpsToWad(80);
        slots[0] = open;
        slots[1] = open;
        slots[2] = initialX;
        slots[3] = initialY;
        slots[4] = _safePrice(initialY, initialX, 100 * WAD);
        slots[5] = bpsToWad(9); // sigma prior
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

        uint256 xPre = slots[2];
        uint256 yPre = slots[3];
        if (xPre == 0 || yPre == 0) {
            xPre = trade.reserveX;
            yPre = trade.reserveY;
        }

        uint256 pHat = slots[4];
        if (pHat == 0) pHat = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);
        uint256 volHat = slots[5];
        if (volHat == 0) volHat = bpsToWad(9);
        uint256 lastArb = slots[6];
        if (lastArb == 0) lastArb = pHat;

        uint256 lastTs = slots[7];
        uint256 stepTrades = slots[8];
        uint256 prevStepRetail = slots[9];
        uint256 stepRetail = slots[10];
        uint256 tradeCount = slots[11] + 1;

        if (trade.timestamp != lastTs) {
            prevStepRetail = stepRetail > 0 ? 1 : 0;
            lastTs = trade.timestamp;
            stepTrades = 0;
            stepRetail = 0;
        }
        stepTrades += 1;

        uint256 spotPre = _safePrice(yPre, xPre, pHat);
        uint256 spotPost = _safePrice(trade.reserveY, trade.reserveX, pHat);

        bool isArb = false;
        if (stepTrades == 1) {
            uint256 feeUsed = trade.isBuy ? bidPrev : askPrev;
            uint256 gamma = _gammaFromFee(feeUsed);
            uint256 pObs = trade.isBuy ? wmul(spotPost, gamma) : wdiv(spotPost, gamma);

            bool directionOk = trade.isBuy ? (spotPre > pObs) : (spotPre < pObs);
            uint256 relMove = _relDiff(pObs, pHat);
            uint256 gate = prevStepRetail == 1 ? bpsToWad(70) : bpsToWad(24);

            if (directionOk && relMove <= gate) {
                isArb = true;
                volHat = _ewma(volHat, _relDiff(pObs, lastArb), A_ARB_VOL);
                pHat = _ewma(pHat, pObs, A_ARB_P);
                lastArb = pObs;
            }
        }

        if (!isArb) {
            stepRetail += 1;
            pHat = _ewma(pHat, spotPost, A_SPOT_P);
        }

        uint256 baseBps = _baseFromVol(volHat, tradeCount);
        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        // Mild inventory tilt.
        uint256 vx = wmul(pHat, trade.reserveX);
        bool longX = vx > trade.reserveY;
        uint256 invNum = _absDiff(vx, trade.reserveY);
        uint256 invDen = vx + trade.reserveY + 1;
        uint256 invRatio = wdiv(invNum, invDen);

        uint256 tilt = 2;
        if (invRatio > bpsToWad(500)) tilt = 6;
        else if (invRatio > bpsToWad(300)) tilt = 4;

        if (longX) {
            bidBps += tilt;
            if (askBps > tilt) askBps -= tilt;
        } else {
            askBps += tilt;
            if (bidBps > tilt) bidBps -= tilt;
        }

        // If first trade was retail, modestly raise for possible follow-on retail this step.
        if (!isArb && stepTrades == 1) {
            bidBps += 4;
            askBps += 4;
        }

        bidBps = _clampBps(bidBps, 8, 180);
        askBps = _clampBps(askBps, 8, 180);

        uint256 bidOut = clampFee(bpsToWad(bidBps));
        uint256 askOut = clampFee(bpsToWad(askBps));

        slots[0] = bidOut;
        slots[1] = askOut;
        slots[2] = trade.reserveX;
        slots[3] = trade.reserveY;
        slots[4] = pHat;
        slots[5] = volHat;
        slots[6] = lastArb;
        slots[7] = lastTs;
        slots[8] = stepTrades;
        slots[9] = prevStepRetail;
        slots[10] = stepRetail;
        slots[11] = tradeCount;

        return (bidOut, askOut);
    }

    function _baseFromVol(uint256 volHat, uint256 tradeCount) internal pure returns (uint256) {
        if (tradeCount < 400) return 74;

        if (volHat < bpsToWad(88) / 10) return 104; // <0.088%
        if (volHat < bpsToWad(94) / 10) return 94;  // <0.094%
        if (volHat < bpsToWad(100) / 10) return 84; // <0.100%
        return 74;
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
