// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// Strategy: track MM2 reserves continuously, estimate fair proxy pHat,
/// set fees as a nonlinear function of staleness delta = |p1-pHat|/pHat:
///   - very high fees only when extremely aligned
///   - quickly collapse fees as staleness grows to avoid delayed arb
/// Cheap: no Babylonian sqrt loops, no grid search.
contract Strategy is AMMStrategyBase {
    uint256 private constant COMP_FEE_BPS = 30;
    uint256 private constant MAX_FEE_BPS  = 1000;

    // EWMA params (WAD)
    uint256 private constant ALPHA = 2e17;         // 0.2
    uint256 private constant ONE_MINUS_ALPHA = 8e17;

    // Optional throttle: recompute fees every N swaps (still update MM2 + pHat every swap)
    uint256 private constant REPRICE_EVERY = 2;

    // Storage slots:
    //  0 bidFee (WAD)
    //  1 askFee (WAD)
    //  2 lastX1 pre-trade (raw)
    //  3 lastY1 pre-trade (raw)
    //  4 estX2 competitor (raw)
    //  5 estY2 competitor (raw)
    //  6 ewmaYTotal (raw)
    //  7 ewmaXTotal (raw)
    //  8 pHat (WAD)
    //  9 counter (raw)
    function getName() external pure override returns (string memory) {
        return "MM2Track+DeltaBandedFees";
    }

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 base = clampFee(bpsToWad(COMP_FEE_BPS));
        slots[0] = base;
        slots[1] = base;

        slots[2] = initialX;
        slots[3] = initialY;

        // competitor prior = us
        slots[4] = initialX;
        slots[5] = initialY;

        // bootstrap totals
        slots[6] = initialY / 50;
        slots[7] = initialX / 50;

        // pHat init
        slots[8] = wdiv(initialY, initialX);

        slots[9] = 0;

        return (base, base);
    }

    function afterSwap(TradeInfo calldata trade)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {
        uint256 ctr = slots[9] + 1;
        slots[9] = ctr;

        uint256 x1Pre = slots[2];
        uint256 y1Pre = slots[3];

        uint256 x2 = slots[4];
        uint256 y2 = slots[5];

        uint256 bid = slots[0];
        uint256 ask = slots[1];
        if (bid == 0) bid = bpsToWad(COMP_FEE_BPS);
        if (ask == 0) ask = bpsToWad(COMP_FEE_BPS);

        // trade.isBuy == true => X-in; false => Y-in
        bool isYIn = !trade.isBuy;

        // ------------------------------------------------------------
        // 1) Track MM2 inventory continuously (invert routing + simulate)
        //    Uses cheap r-approx (no sqrt loops).
        // ------------------------------------------------------------
        if (isYIn) {
            (uint256 Ytotal, uint256 dy2) =
                _inferTotalAndOtherFill_Yin_Approx(y1Pre, y2, bid, trade.amountY);

            (x2, y2) = _applySwap_Yin(x2, y2, dy2);

            slots[6] = _ewmaRaw(slots[6], Ytotal);
        } else {
            (uint256 Xtotal, uint256 dx2) =
                _inferTotalAndOtherFill_Xin_Approx(x1Pre, x2, ask, trade.amountX);

            (x2, y2) = _applySwap_Xin(x2, y2, dx2);

            slots[7] = _ewmaRaw(slots[7], Xtotal);
        }

        slots[4] = x2;
        slots[5] = y2;

        // ------------------------------------------------------------
        // 2) Update pHat (EWMA blend of our price + MM2 price)
        // ------------------------------------------------------------
        uint256 p1 = wdiv(trade.reserveY, trade.reserveX);
        uint256 p2 = (x2 == 0) ? p1 : wdiv(y2, x2);
        uint256 pBlend = (p1 + p2) / 2;
        slots[8] = _ewmaWad(slots[8], pBlend);
        uint256 pHat = slots[8];

        // Update pre-trade reserves for next call
        slots[2] = trade.reserveX;
        slots[3] = trade.reserveY;

        // If throttled, keep fees unchanged on off-steps
        if (REPRICE_EVERY > 1 && (ctr % REPRICE_EVERY != 0)) {
            return (slots[0], slots[1]);
        }

        // ------------------------------------------------------------
        // 3) Compute staleness delta and pick banded fee level
        // ------------------------------------------------------------
        uint256 delta = wdiv(_absDiff(p1, pHat), pHat); // WAD

        uint256 baseBps = _feeFromDeltaBps(delta);

        // ------------------------------------------------------------
        // 4) Small inventory tilt (mean revert) without breaking arb-clearing
        // ------------------------------------------------------------
        // valueX in Y = pHat * x
        uint256 valueX = wmul(pHat, trade.reserveX);
        bool longX = valueX > trade.reserveY;

        // tilt increases mildly when very aligned (because retail edge dominates there)
        uint256 tilt = 0;
        if (delta < WAD / 667) tilt = 10;          // delta < 0.15%
        else if (delta < WAD / 286) tilt = 6;      // delta < 0.35%
        else tilt = 3;

        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        if (longX) {
            // long X => encourage Y-in (trader buys X): lower bid; discourage X-in: raise ask
            if (bidBps > tilt) bidBps -= tilt;
            askBps += tilt;
        } else {
            if (askBps > tilt) askBps -= tilt;
            bidBps += tilt;
        }

        // clamp to [0,1000]
        if (bidBps > MAX_FEE_BPS) bidBps = MAX_FEE_BPS;
        if (askBps > MAX_FEE_BPS) askBps = MAX_FEE_BPS;

        uint256 bidFeeWad = clampFee(bpsToWad(bidBps));
        uint256 askFeeWad = clampFee(bpsToWad(askBps));

        slots[0] = bidFeeWad;
        slots[1] = askFeeWad;

        return (bidFeeWad, askFeeWad);
    }

    /* ============================================================
       Fee schedule vs staleness delta (WAD)
       ============================================================ */

    function _feeFromDeltaBps(uint256 deltaWad) internal pure returns (uint256) {
        // Ultra-aligned: delta < 0.06% -> very high retail capture
        if (deltaWad < WAD / 1667) return 190; // ~0.06%
        // Aligned: <0.15%
        if (deltaWad < WAD / 667)  return 70;  // ~0.15%
        // Slightly stale: <0.35%
        if (deltaWad < WAD / 286)  return 22;  // ~0.35%
        // Stale: collapse fees to let arb clear quickly
        return 9;
    }

    /* ============================================================
       Cheap routing inversion (no sqrt loops)
       ============================================================ */

    // Approx sqrt(g1/g2) ≈ 1 + (g1-g2)/(2g2)
    function _sqrtRatioApprox(uint256 g1, uint256 g2) internal pure returns (uint256) {
        if (g2 == 0) return WAD;
        if (g1 == g2) return WAD;

        if (g1 > g2) {
            uint256 delta = wdiv(g1 - g2, g2);
            return WAD + (delta / 2);
        } else {
            uint256 delta = wdiv(g2 - g1, g2);
            uint256 half = delta / 2;
            return (WAD > half) ? (WAD - half) : 1;
        }
    }

    // r ≈ (y1/y2) * sqrt(g1/g2)
    function _rApproxWad(uint256 y1, uint256 y2, uint256 g1, uint256 g2) internal pure returns (uint256) {
        if (y2 == 0) return WAD;
        uint256 yRatio = wdiv(y1, y2);
        uint256 s = _sqrtRatioApprox(g1, g2);
        return wmul(yRatio, s);
    }

    // Inversion for Y-in:
    // Y = (dy1*g1 + dy1*r*g2 - r*y2 + y1) / (r*g2)
    // Using rApprox; needs only (y1,y2,fee1,dy1).
    function _inferTotalAndOtherFill_Yin_Approx(
        uint256 y1,
        uint256 y2,
        uint256 fee1Wad,
        uint256 dy1
    ) internal pure returns (uint256 Ytotal, uint256 dy2) {
        if (dy1 == 0) return (0, 0);

        uint256 g2 = WAD - bpsToWad(COMP_FEE_BPS);
        uint256 g1 = WAD - clampFee(fee1Wad);

        uint256 r = _rApproxWad(y1, y2, g1, g2);

        uint256 t1 = wmul(dy1, g1);
        uint256 t2 = wmul(wmul(dy1, r), g2);
        uint256 t3 = wmul(r, y2);

        // Fallback if numerically weird
        if (t1 + t2 + y1 <= t3) {
            Ytotal = dy1 * 2;
        } else {
            uint256 num = (t1 + t2 + y1) - t3;
            uint256 den = wmul(r, g2);
            Ytotal = (den == 0) ? (dy1 * 2) : wdiv(num, den);
            if (Ytotal < dy1) Ytotal = dy1;
        }

        dy2 = Ytotal > dy1 ? (Ytotal - dy1) : 0;
    }

    // For X-in, swap x<->y logic (only need x1,x2,fee,dx1)
    function _inferTotalAndOtherFill_Xin_Approx(
        uint256 x1,
        uint256 x2,
        uint256 fee1Wad,
        uint256 dx1
    ) internal pure returns (uint256 Xtotal, uint256 dx2) {
        // Symmetry: use the same function by treating X like Y
        return _inferTotalAndOtherFill_Yin_Approx(x1, x2, fee1Wad, dx1);
    }

    /* ============================================================
       Competitor swap simulation (constant product, fee-on-input)
       ============================================================ */

    function _applySwap_Yin(uint256 x, uint256 y, uint256 dyIn)
        internal
        pure
        returns (uint256 xNew, uint256 yNew)
    {
        if (dyIn == 0 || x == 0 || y == 0) return (x, y);

        uint256 g2 = WAD - bpsToWad(COMP_FEE_BPS);

        uint256 k = x * y;
        uint256 yEff = y + wmul(g2, dyIn);
        uint256 xAfter = k / yEff;
        uint256 xOut = x - xAfter;

        xNew = x - xOut;
        yNew = y + dyIn;
    }

    function _applySwap_Xin(uint256 x, uint256 y, uint256 dxIn)
        internal
        pure
        returns (uint256 xNew, uint256 yNew)
    {
        (yNew, xNew) = _applySwap_Yin(y, x, dxIn);
    }

    /* ============================================================
       EWMAs
       ============================================================ */

    function _ewmaRaw(uint256 oldV, uint256 obs) internal pure returns (uint256) {
        if (oldV == 0) return obs == 0 ? 1 : obs;
        return (oldV * ONE_MINUS_ALPHA) / WAD + (obs * ALPHA) / WAD;
    }

    function _ewmaWad(uint256 oldV, uint256 obs) internal pure returns (uint256) {
        if (oldV == 0) return obs == 0 ? 1 : obs;
        return (oldV * ONE_MINUS_ALPHA) / WAD + (obs * ALPHA) / WAD;
    }

    function _absDiff(uint256 a, uint256 b) internal pure returns (uint256) {
        return a > b ? a - b : b - a;
    }
}