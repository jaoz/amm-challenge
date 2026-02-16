// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

/// @notice Direct-controller version:
/// - no latent normalizer state
/// - no candidate scoring loop
/// - deterministic mid/skew fee update + safety constraints
contract Strategy is AMMStrategyBase {
    uint256 private constant OPEN_BPS = 29;
    uint256 private constant PRIOR_BPS = 37;

    uint256 private constant MIN_BPS = 0;
    uint256 private constant MAX_BPS = 1000;
    uint256 private constant TARGET_MAX_BPS = 220;
    uint256 private constant MAX_JUMP_BPS = 20;
    uint256 private constant MAX_ASYM_BPS = 30;
    uint256 private constant MAX_SKEW_BPS = 30;

    uint256 private constant SHIELD_TRIGGER_BPS = 10;
    uint256 private constant SHIELD_BUFFER_BPS = 1;

    uint256 private constant RETAIL_MEAN_Y = 20 * WAD;

    uint256 private constant A_LAMBDA = 10e16;
    uint256 private constant A_LAMBDA_SAME = 28e16;
    uint256 private constant A_ARB = 11e16;
    uint256 private constant A_ARB_SAME = 22e16;
    uint256 private constant A_VOL = 6e16;
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
    // 11 reserved
    // 12-14 reserved

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

        bool probableArb = _classifyProbableArb(
            trade.isBuy,
            stepTrades,
            trade.amountY,
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
            pStep = ((WAD - ANCHOR_W) * midNow + ANCHOR_W * pAnchor) / WAD;
            anchoredThisStep = true;
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

        (int256 invSigned, uint256 invAbs) = _inventorySigned(pStep, trade.reserveX, trade.reserveY);

        (uint256 reqBidBps, uint256 reqAskBps) = _requiredNoArbFeesBps(pStep, spotPost);
        bool bidShieldActive = reqBidBps > SHIELD_TRIGGER_BPS;
        bool askShieldActive = reqAskBps > SHIELD_TRIGGER_BPS;
        uint256 bidFloor = bidShieldActive ? clamp(reqBidBps + SHIELD_BUFFER_BPS, MIN_BPS, MAX_BPS) : MIN_BPS;
        uint256 askFloor = askShieldActive ? clamp(reqAskBps + SHIELD_BUFFER_BPS, MIN_BPS, MAX_BPS) : MIN_BPS;

        uint256 midTarget = _midTargetBps(lambdaHat, arbHat, volHat, stepTrades, probableArb);
        uint256 fairTilt = _fairTiltBps(staleMag);
        int256 fairSign = 0;
        if (spotPost > pStep) fairSign = int256(fairTilt);
        else if (spotPost < pStep) fairSign = -int256(fairTilt);

        uint256 invTilt = _invTiltBps(invAbs);
        int256 invSign = 0;
        if (invSigned > 0) invSign = int256(invTilt);
        else if (invSigned < 0) invSign = -int256(invTilt);

        int256 skewTarget = _clampSigned(
            (fairSign * 5 + invSign * 6) / 11,
            -int256(MAX_SKEW_BPS),
            int256(MAX_SKEW_BPS)
        );

        uint256 bidTarget = _clampSignedToUint(int256(midTarget) + skewTarget, MIN_BPS, MAX_BPS);
        uint256 askTarget = _clampSignedToUint(int256(midTarget) - skewTarget, MIN_BPS, MAX_BPS);

        (uint256 bestBid, uint256 bestAsk) = _applyCandidateConstraints(
            bidTarget,
            askTarget,
            bidPrevBps,
            askPrevBps,
            bidShieldActive,
            askShieldActive,
            bidFloor,
            askFloor
        );

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

        return (bidOut, askOut);
    }

    function getName() external pure override returns (string memory) {
        return "PowellDirectNoShare_best_worker_2";
    }

    function _classifyProbableArb(
        bool isBuy,
        uint256 stepTrades,
        uint256 amountY,
        uint256 spotPre,
        uint256 spotPost,
        uint256 pStep,
        uint256 bidFeeUsed,
        uint256 askFeeUsed
    ) internal pure returns (bool) {
        if (stepTrades >= 2) {
            return false;
        }

        uint256 anchor = isBuy
            ? wmul(spotPost, _gammaFromFee(bidFeeUsed))
            : wdiv(spotPost, _gammaFromFee(askFeeUsed));

        bool directionOk = isBuy ? (spotPost <= spotPre) : (spotPost >= spotPre);
        uint256 relAnchor = _relDiff(anchor, pStep);
        uint256 relMove = _relDiff(spotPost, spotPre);

        bool smallAbs = amountY <= SIZE_SMALL_ABS_Y;
        uint256 relCut = wmul(RETAIL_MEAN_Y, SIZE_SMALL_REL_WAD);
        bool smallRel = amountY <= relCut;
        bool sizeSignal = smallAbs || smallRel;

        uint256 anchorGate = bpsToWad(ARB_ANCHOR_GATE_BPS);
        uint256 moveFloor = bpsToWad(ARB_MOVE_FLOOR_BPS);
        bool baseArb = directionOk && relAnchor <= anchorGate && relMove >= moveFloor;
        if (!sizeSignal) {
            return baseArb;
        }

        uint256 gateMult = 100;
        uint256 moveMult = 100;

        if (smallAbs || smallRel) {
            gateMult = 140;
            moveMult = 65;
        }
        uint256 relaxedGate = (anchorGate * gateMult) / 100;
        uint256 relaxedMove = (moveFloor * moveMult) / 100;
        bool relaxedArb = directionOk && relAnchor <= relaxedGate && relMove >= relaxedMove;
        return baseArb || relaxedArb;
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

        return (clamp(b, MIN_BPS, MAX_BPS), clamp(a, MIN_BPS, MAX_BPS));
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
