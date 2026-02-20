// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AMMStrategyBase} from "./AMMStrategyBase.sol";
import {TradeInfo} from "./IAMMStrategy.sol";

// Structural fixes applied to islandGA10h champion (20260217):
//   Fix 1: Opening quote 30bps-5 (was 5 bps — bled edge on first arb)
//   Fix 2: sizeHat unconditional update (was only for trades > 20bps threshold)
//   Fix 5: Two-sided lognormal arb size scorer (relaxes ARB_TR_MIN for tiny/huge prints)
//   Fix 6: pHat gap-aware gate widening + alpha boost after no-trade gaps
//   Fix 3: toxEma proper EMA blend (was 0.051 = instantaneous; now 0.78)
//   Fix 4: lambdaHat gap-handling verified correct (elapsedRaw denominator covers gaps)
//   Fix 7: STALE_DIR_COEF + STALE_ATTRACT_FRAC added to mutable list for optimizer
contract Strategy is AMMStrategyBase {
    // --- decay / update constants ---
    uint256 constant ELAPSED_CAP = 8;
    uint256 constant SIGNAL_THRESHOLD = 2000000000000000; // ~20 bps of reserve
    uint256 constant DIR_DECAY = 800000000000000000; // 0.80
    uint256 constant ACT_DECAY = 700000000000000000; // 0.70
    uint256 constant SIZE_DECAY = 700000000000000000; // 0.70
    uint256 constant TOX_DECAY = 903492922768028618; // 0.91
    uint256 constant SIGMA_DECAY = 767455897236310988; // 0.824
    uint256 constant LAMBDA_DECAY = 992215351636490007; // 0.99
    uint256 constant SIZE_BLEND_DECAY = 818000000000000000; // 0.818
    uint256 constant SIZE_SMALL_DECAY = 980000000000000000; // 0.98  [FIX 2] sub-threshold blend
    uint256 constant TOX_BLEND_DECAY = 780000000000000000; // 0.78  [FIX 3] proper EMA (was 0.051)
    uint256 constant ACT_BLEND_DECAY = 985000000000000000; // 0.985
    uint256 constant PHAT_ALPHA = 241505108527551136; // 0.26
    uint256 constant PHAT_ALPHA_RETAIL = 57772256735008410; // 0.05
    uint256 constant DIR_IMPACT_MULT = 2;

    // --- [FIX 6] Gap-aware pHat update ---
    uint256 constant GAP_PHAT_ALPHA_BOOST = 30000000000000000; // 0.03 per elapsed step beyond 1
    uint256 constant GAP_GATE_PER_STEP = 250000000000000000; // 0.25 WAD widening per elapsed step

    // --- [FIX 1] Opening Quote: 30 bps - 5 units ---
    uint256 constant OPENING_QUOTE = 2999999999999995; // 30 bps = 3e15; minus 5 LSB

    // --- Adaptive Shock Gate ---
    uint256 constant GATE_SIGMA_MULT = 7384137185306734914;
    uint256 constant MIN_GATE = 30764403862407015; // 0.03 WAD

    // --- Cubic Toxicity ---
    uint256 constant TOX_CUBIC_COEF = 810754395559500107;

    // --- Trade-Tox Boost ---
    uint256 constant TRADE_TOX_BOOST = 296649693411330284;

    // --- Asymmetric Stale Dir [FIX 7: added to mutable list] ---
    uint256 constant STALE_ATTRACT_FRAC = 1124000000000000000; // 1.124

    // --- state caps ---
    uint256 constant RET_CAP = 194234434259551500; // 10%
    uint256 constant TOX_CAP = 200000000000000000; // 20%
    uint256 constant TRADE_RATIO_CAP = 200000000000000000; // 20%
    uint256 constant LAMBDA_CAP = 5000000000000000000; // max 5 trades/step estimate
    uint256 constant STEP_COUNT_CAP = 64; // guardrail

    // --- arb classifier / tail harvest / shield ---
    uint256 constant ARB_TOX_MIN = 2481389578163771; // 25 bps
    uint256 constant ARB_TR_MIN = 4291845493562231; // 40 bps of reserveY (mid-size trades)
    uint256 constant ARB_TR_MIN_SMALL = 500000000000000; // 5 bps   [FIX 5] for arb-size-like prints
    uint256 constant ARB_SIZE_HIGH_MULT = 3000000000000000000; // 3x sizeHat [FIX 5] large = arb-like
    uint256 constant ARB_SIZE_LOW_FRAC = 250000000000000000; // 0.25x sizeHat [FIX 5] tiny = arb-like
    uint256 constant ARB_RET_MIN = 3164556962025316; // 33 bps vs pHat

    uint256 constant SHIELD_TRIGGER = 3528246674170962; // ~30 bps
    uint256 constant SHIELD_BUFFER = 306000920220486; // 10 bps

    uint256 constant TAIL_KNEE_RAW = 4302346170530126; // 50 bps (raw tradeRatio knee)
    uint256 constant TAIL_CAP_RAW = 82295229542497536; // 10% of reserveY (keep tail signal)
    uint256 constant TAIL_QUAD_COEF = 250022599054286204; // convex tail fee
    uint256 constant TAIL_CUBIC_COEF = 70996480944354524; // extra convexity
    uint256 constant TAIL_RETAIL_MULT_SAMESTEP = WAD + (WAD / 2); // 1.5x

    // --- fee model constants ---
    uint256 constant BASE_FEE = 495936726328247;
    uint256 constant SIGMA_COEF = 111034113997309465; // 0.111
    uint256 constant LAMBDA_COEF = 1341580072605457;
    uint256 constant FLOW_SIZE_COEF = 402210817848412601;
    uint256 constant TOX_COEF = 20647797205597185;
    uint256 constant TOX_QUAD_COEF = 3220200000000000000;
    uint256 constant ACT_COEF = 10851200000000000000;
    uint256 constant DIR_COEF = 1973333925646282;
    uint256 constant DIR_TOX_COEF = 13300000000000000;
    uint256 constant STALE_DIR_COEF = 694900000000000000; // [FIX 7] in mutable list
    uint256 constant SIGMA_TOX_COEF = 40617877182290761;
    uint256 constant TAIL_KNEE = 15200000000000000;
    uint256 constant TAIL_SLOPE_PROTECT = 799480378006438484; // 0.93
    uint256 constant TAIL_SLOPE_ATTRACT = 799480378006438484; // 0.955

    // slots[0] = bid fee
    // slots[1] = ask fee
    // slots[2] = last timestamp
    // slots[3] = dirState (centered at WAD, [0, 2*WAD])
    // slots[4] = actEma
    // slots[5] = pHat
    // slots[6] = sigmaHat
    // slots[7] = lambdaHat
    // slots[8] = sizeHat
    // slots[9] = toxEma
    // slots[10] = stepTradeCount (raw integer)

    // [FIX 1] Open at 30 bps - 5 units to be just inside the 30 bps normalizer
    function afterInitialize(uint256 initialX, uint256 initialY) external override returns (uint256, uint256) {
        slots[0] = OPENING_QUOTE;
        slots[1] = OPENING_QUOTE;
        slots[2] = 0;
        slots[3] = WAD; // neutral direction
        slots[4] = 0;
        slots[5] = initialX > 0 ? wdiv(initialY, initialX) : 100 * WAD;
        slots[6] = 950000000000000; // 0.095% initial sigma guess
        slots[7] = 800000000000000000; // 0.8 initial arrival-rate guess
        slots[8] = 2000000000000000; // 0.2% reserve-size ratio guess
        slots[9] = 0;
        slots[10] = 0;
        return (OPENING_QUOTE, OPENING_QUOTE);
    }

    function afterSwap(TradeInfo calldata trade) external override returns (uint256, uint256) {
        uint256 prevBidFee = slots[0];
        uint256 prevAskFee = slots[1];
        uint256 lastTs = slots[2];
        uint256 dirState = slots[3];
        uint256 actEma = slots[4];
        uint256 pHat = slots[5];
        uint256 sigmaHat = slots[6];
        uint256 lambdaHat = slots[7];
        uint256 sizeHat = slots[8];
        uint256 toxEma = slots[9];
        uint256 stepTradeCount = slots[10];

        // [FIX 6] Hoist elapsed to function scope so gate/alpha can use it
        bool isNewStep = trade.timestamp > lastTs;
        uint256 elapsedRaw = isNewStep ? (trade.timestamp - lastTs) : 0;
        uint256 elapsed = elapsedRaw > ELAPSED_CAP ? ELAPSED_CAP : elapsedRaw;

        if (isNewStep) {
            dirState = _decayCentered(dirState, DIR_DECAY, elapsed);
            actEma = wmul(actEma, _powWad(ACT_DECAY, elapsed));
            sizeHat = wmul(sizeHat, _powWad(SIZE_DECAY, elapsed));
            toxEma = wmul(toxEma, _powWad(TOX_DECAY, elapsed));

            // [FIX 4] lambdaInst = stepTradeCount / elapsedRaw already accounts for zero-trade
            // intermediate steps (elapsedRaw > 1 means we missed steps → lower inst rate).
            // This is correct; no additional change needed.
            if (stepTradeCount > 0 && elapsedRaw > 0) {
                uint256 lambdaInst = (stepTradeCount * WAD) / elapsedRaw;
                if (lambdaInst > LAMBDA_CAP) lambdaInst = LAMBDA_CAP;
                lambdaHat = wmul(lambdaHat, LAMBDA_DECAY) + wmul(lambdaInst, WAD - LAMBDA_DECAY);
            }

            stepTradeCount = 0;
        }

        bool firstInStep = stepTradeCount == 0;

        uint256 spot = trade.reserveX > 0 ? wdiv(trade.reserveY, trade.reserveX) : pHat;
        if (pHat == 0) pHat = spot;

        uint256 feeUsed = trade.isBuy ? prevBidFee : prevAskFee;
        uint256 gamma = feeUsed < WAD ? WAD - feeUsed : 0;
        uint256 pImplied;
        if (gamma == 0) {
            pImplied = spot;
        } else {
            pImplied = trade.isBuy ? wmul(spot, gamma) : wdiv(spot, gamma);
        }

        uint256 retLocal = 0;
        {
            uint256 ret = pHat > 0 ? wdiv(absDiff(pImplied, pHat), pHat) : 0;
            retLocal = ret;

            // [FIX 6] Gap-aware gate: widen after no-trade gaps (price diffuses during gap)
            uint256 adaptiveGate = wmul(sigmaHat, GATE_SIGMA_MULT);
            if (adaptiveGate < MIN_GATE) adaptiveGate = MIN_GATE;
            if (isNewStep && elapsed > 1) {
                // Widen proportionally to gap length, capped by ELAPSED_CAP
                adaptiveGate = wmul(adaptiveGate, WAD + (elapsed - 1) * GAP_GATE_PER_STEP);
            }

            // [FIX 6] Gap-aware alpha: be more responsive after long no-trade gaps
            uint256 alpha;
            if (firstInStep) {
                if (isNewStep && elapsed > 1) {
                    uint256 boost = (elapsed - 1) * GAP_PHAT_ALPHA_BOOST;
                    alpha = PHAT_ALPHA + boost;
                    if (alpha > WAD / 2) alpha = WAD / 2;
                } else {
                    alpha = PHAT_ALPHA;
                }
            } else {
                alpha = PHAT_ALPHA_RETAIL;
            }

            if (ret <= adaptiveGate) {
                pHat = wmul(pHat, WAD - alpha) + wmul(pImplied, alpha);
            }
            if (firstInStep) {
                if (ret > RET_CAP) ret = RET_CAP;
                sigmaHat = wmul(sigmaHat, SIGMA_DECAY) + wmul(ret, WAD - SIGMA_DECAY);
            }
        }

        uint256 tradeRatioRaw = trade.reserveY > 0 ? wdiv(trade.amountY, trade.reserveY) : 0;

        uint256 tradeRatio = tradeRatioRaw;
        if (tradeRatio > TRADE_RATIO_CAP) tradeRatio = TRADE_RATIO_CAP;

        if (tradeRatio > SIGNAL_THRESHOLD) {
            uint256 push = tradeRatio * DIR_IMPACT_MULT;
            if (push > WAD / 4) push = WAD / 4;

            if (trade.isBuy) {
                dirState = dirState + push;
                if (dirState > 2 * WAD) dirState = 2 * WAD;
            } else {
                dirState = dirState > push ? dirState - push : 0;
            }

            actEma = wmul(actEma, ACT_BLEND_DECAY) + wmul(tradeRatio, WAD - ACT_BLEND_DECAY);

            // [FIX 2] Large trade: update sizeHat with blend decay (as before)
            sizeHat = wmul(sizeHat, SIZE_BLEND_DECAY) + wmul(tradeRatio, WAD - SIZE_BLEND_DECAY);
            if (sizeHat > WAD) sizeHat = WAD;
        } else {
            // [FIX 2] Small trade: still update sizeHat, but with a slower decay
            // Previously invisible to sizeHat, causing systematic over-estimation
            sizeHat = wmul(sizeHat, SIZE_SMALL_DECAY) + wmul(tradeRatio, WAD - SIZE_SMALL_DECAY);
            if (sizeHat > WAD) sizeHat = WAD;
        }

        uint256 tox = pHat > 0 ? wdiv(absDiff(spot, pHat), pHat) : 0;
        if (tox > TOX_CAP) tox = TOX_CAP;
        // [FIX 3] toxEma is now a proper EMA (TOX_BLEND_DECAY=0.78 vs old 0.051≈instantaneous)
        toxEma = wmul(toxEma, TOX_BLEND_DECAY) + wmul(tox, WAD - TOX_BLEND_DECAY);
        uint256 toxSignal = toxEma;

        // [FIX 5] Two-sided lognormal arb size scorer: pass sizeHat into classifier
        bool probableArb = _classifyProbableArb(
            trade.isBuy,
            firstInStep,
            stepTradeCount,
            spot,
            pHat,
            tox,
            retLocal,
            tradeRatioRaw,
            sizeHat
        );

        stepTradeCount = stepTradeCount + 1;
        if (stepTradeCount > STEP_COUNT_CAP) stepTradeCount = STEP_COUNT_CAP;

        uint256 flowSize = wmul(lambdaHat, sizeHat);
        uint256 fBase =
            BASE_FEE + wmul(SIGMA_COEF, sigmaHat) + wmul(LAMBDA_COEF, lambdaHat) + wmul(FLOW_SIZE_COEF, flowSize);
        uint256 fMid = fBase + wmul(TOX_COEF, toxSignal) + wmul(TOX_QUAD_COEF, wmul(toxSignal, toxSignal))
            + wmul(ACT_COEF, actEma);

        fMid = fMid + wmul(SIGMA_TOX_COEF, wmul(sigmaHat, toxSignal));

        {
            uint256 toxCubed = wmul(toxSignal, wmul(toxSignal, toxSignal));
            fMid = fMid + wmul(TOX_CUBIC_COEF, toxCubed);
        }

        uint256 dirDev;
        bool sellPressure;
        if (dirState >= WAD) {
            dirDev = dirState - WAD;
            sellPressure = true;
        } else {
            dirDev = WAD - dirState;
            sellPressure = false;
        }

        uint256 skew = wmul(DIR_COEF, dirDev) + wmul(DIR_TOX_COEF, wmul(dirDev, toxSignal));

        uint256 bidFee;
        uint256 askFee;
        if (sellPressure) {
            bidFee = fMid + skew;
            askFee = fMid > skew ? fMid - skew : 0;
        } else {
            askFee = fMid + skew;
            bidFee = fMid > skew ? fMid - skew : 0;
        }

        // Directional protection using stale-price sign [FIX 7: STALE_DIR_COEF now mutable]
        {
            uint256 staleShift = wmul(STALE_DIR_COEF, toxSignal);
            uint256 attractShift = wmul(staleShift, STALE_ATTRACT_FRAC);
            if (spot >= pHat) {
                bidFee = bidFee + staleShift;
                askFee = askFee > attractShift ? askFee - attractShift : 0;
            } else {
                askFee = askFee + staleShift;
                bidFee = bidFee > attractShift ? bidFee - attractShift : 0;
            }
        }

        // Trade-aligned toxicity boost
        {
            bool tradeAligned = (trade.isBuy && spot >= pHat) || (!trade.isBuy && spot < pHat);
            if (tradeAligned) {
                uint256 tradeBoost = wmul(TRADE_TOX_BOOST, tradeRatio);
                if (trade.isBuy) {
                    bidFee = bidFee + tradeBoost;
                } else {
                    askFee = askFee + tradeBoost;
                }
            }
        }

        // Convex tail capture: harvest rare large prints when not probable arb
        {
            bool retailHarvest = (!probableArb) || (stepTradeCount >= 2);

            if (retailHarvest) {
                uint256 tr = tradeRatioRaw;
                if (tr > TAIL_CAP_RAW) tr = TAIL_CAP_RAW;

                if (tr > TAIL_KNEE_RAW) {
                    uint256 tail = tr - TAIL_KNEE_RAW;
                    uint256 tail2 = wmul(tail, tail);
                    uint256 tail3 = wmul(tail2, tail);

                    uint256 boost = wmul(TAIL_QUAD_COEF, tail2) + wmul(TAIL_CUBIC_COEF, tail3);

                    if (stepTradeCount >= 2) {
                        boost = wmul(boost, TAIL_RETAIL_MULT_SAMESTEP);
                    }

                    if (trade.isBuy) {
                        bidFee = bidFee + boost;
                    } else {
                        askFee = askFee + boost;
                    }
                }
            }
        }

        // No-arb fee floor shield
        {
            (uint256 bidReq, uint256 askReq) = _requiredNoArbFloors(pHat, spot);

            if (bidReq > SHIELD_TRIGGER) {
                uint256 floor = bidReq + SHIELD_BUFFER;
                if (bidFee < floor) bidFee = floor;
            }
            if (askReq > SHIELD_TRIGGER) {
                uint256 floor = askReq + SHIELD_BUFFER;
                if (askFee < floor) askFee = floor;
            }
        }

        // Asymmetric tail compression
        if (sellPressure) {
            bidFee = clampFee(_compressTailWithSlope(bidFee, TAIL_SLOPE_PROTECT));
            askFee = clampFee(_compressTailWithSlope(askFee, TAIL_SLOPE_ATTRACT));
        } else {
            askFee = clampFee(_compressTailWithSlope(askFee, TAIL_SLOPE_PROTECT));
            bidFee = clampFee(_compressTailWithSlope(bidFee, TAIL_SLOPE_ATTRACT));
        }

        slots[0] = bidFee;
        slots[1] = askFee;
        slots[2] = trade.timestamp;
        slots[3] = dirState;
        slots[4] = actEma;
        slots[5] = pHat;
        slots[6] = sigmaHat;
        slots[7] = lambdaHat;
        slots[8] = sizeHat;
        slots[9] = toxEma;
        slots[10] = stepTradeCount;

        return (bidFee, askFee);
    }

    // [FIX 5] Two-sided lognormal arb size scorer via sizeHat as retail mode proxy.
    // Both very small (< 0.25x mode) and very large (> 3x mode) prints are arb-like.
    // Relaxes ARB_TR_MIN from 40 bps to 5 bps when size signal is arb-like.
    function _classifyProbableArb(
        bool isBuy,
        bool firstInStep,
        uint256 stepTradeCount,
        uint256 spot,
        uint256 pHat,
        uint256 tox,
        uint256 ret,
        uint256 tradeRatioRaw,
        uint256 sizeHat
    ) internal pure returns (bool) {
        if (!firstInStep) return false;
        if (stepTradeCount != 0) return false;
        if (tox < ARB_TOX_MIN) return false;
        if (ret < ARB_RET_MIN) return false;

        // Two-sided size check: tiny prints and huge prints are both arb-like
        bool sizeArbLike = false;
        if (sizeHat > 0 && tradeRatioRaw > 0) {
            if (tradeRatioRaw > wmul(sizeHat, ARB_SIZE_HIGH_MULT)) {
                sizeArbLike = true; // large print: > 3x retail mode
            } else if (tradeRatioRaw < wmul(sizeHat, ARB_SIZE_LOW_FRAC)) {
                sizeArbLike = true; // tiny print: < 0.25x retail mode
            }
        }
        uint256 effectiveTrMin = sizeArbLike ? ARB_TR_MIN_SMALL : ARB_TR_MIN;
        if (tradeRatioRaw < effectiveTrMin) return false;

        // Arb-aligned: trade moves spot toward pHat
        bool arbAligned = (isBuy && spot < pHat) || (!isBuy && spot > pHat);
        return arbAligned;
    }

    function _requiredNoArbFloors(uint256 pHat, uint256 spot) internal pure returns (uint256 bidFloor, uint256 askFloor) {
        bidFloor = 0;
        askFloor = 0;
        if (pHat == 0 || spot == 0) return (0, 0);

        if (spot > pHat) {
            uint256 ratio = wdiv(pHat, spot);
            bidFloor = ratio < WAD ? (WAD - ratio) : 0;
        } else if (spot < pHat) {
            uint256 ratio = wdiv(spot, pHat);
            askFloor = ratio < WAD ? (WAD - ratio) : 0;
        }
    }

    function _compressTailWithSlope(uint256 fee, uint256 slope) internal pure returns (uint256) {
        if (fee <= TAIL_KNEE) return fee;
        return TAIL_KNEE + wmul(fee - TAIL_KNEE, slope);
    }

    function _powWad(uint256 factor, uint256 exp) internal pure returns (uint256 result) {
        result = WAD;
        while (exp > 0) {
            if (exp & 1 == 1) result = wmul(result, factor);
            factor = wmul(factor, factor);
            exp >>= 1;
        }
    }

    function _decayCentered(uint256 centered, uint256 decayFactor, uint256 elapsed) internal pure returns (uint256) {
        uint256 mul = _powWad(decayFactor, elapsed);
        if (centered >= WAD) {
            return WAD + wmul(centered - WAD, mul);
        }
        uint256 below = wmul(WAD - centered, mul);
        return below < WAD ? WAD - below : 0;
    }

    function getName() external pure override returns (string memory) {
        return "Theo1_v2_Fix1256374_OpenQ30bps_SizeHatAll_ToxEMA78_GapPhat_ArbSizeScore_20260218";
    }
}
