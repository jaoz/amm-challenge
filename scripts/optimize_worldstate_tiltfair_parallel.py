#!/usr/bin/env python3
"""Parallel high-dimensional optimizer for fair-aware WorldState strategy.

Modes:
- launch: start N worker processes in parallel
- worker: run evolutionary search in one process
- collect: aggregate best worker result
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

from amm_competition.competition.config import BASELINE_VARIANCE, build_base_config, resolve_n_workers
from amm_competition.competition.match import MatchRunner
from amm_competition.evm.adapter import EVMStrategyAdapter
from amm_competition.evm.baseline import load_vanilla_strategy
from amm_competition.evm.compiler import SolidityCompiler


@dataclass
class Params:
    # EWMA constants in e16 units
    a_arb: int
    a_vol: int
    a_retail: int
    a_price_arb: int
    a_price_spot: int

    # Opening/default bands (bps)
    open_bps: int
    base_early_bps: int
    base_late_bps: int
    base_min_bps: int
    base_max_bps: int
    quote_min_bps: int
    quote_max_bps: int

    # State gates / regime thresholds
    arb_gate_bps: int
    vol_hi1: int
    vol_hi2: int
    vol_lo1: int
    vol_lo2: int
    arb_hi_pct: int
    arb_lo_pct: int
    ret_hi_pct: int
    ret_lo_pct: int

    # Base-shift magnitudes
    cut1: int
    cut2: int
    boost1: int
    boost2: int
    arb_cut: int
    arb_boost: int
    ret_boost: int
    ret_cut: int

    # Fair-mispricing tilt thresholds/sizes (bps)
    fair_t1: int
    fair_t2: int
    fair_t3: int
    fair_s1: int
    fair_s2: int
    fair_s3: int

    # Inventory tilt thresholds/sizes (bps)
    inv_t1: int
    inv_t2: int
    inv_t3: int
    inv_s1: int
    inv_s2: int
    inv_s3: int
    inv_conflict_div: int

    # Step nudges
    first_retail_bump: int
    arb_relax: int
    multi_step_retail_alpha: int


BOUNDS: dict[str, tuple[int, int]] = {
    "a_arb": (7, 14),
    "a_vol": (16, 30),
    "a_retail": (4, 12),
    "a_price_arb": (12, 28),
    "a_price_spot": (2, 8),
    "open_bps": (16, 72),
    "base_early_bps": (18, 76),
    "base_late_bps": (18, 78),
    "base_min_bps": (8, 52),
    "base_max_bps": (44, 130),
    "quote_min_bps": (6, 52),
    "quote_max_bps": (34, 140),
    "arb_gate_bps": (90, 180),
    "vol_hi1": (100, 110),
    "vol_hi2": (90, 102),
    "vol_lo1": (80, 92),
    "vol_lo2": (86, 98),
    "arb_hi_pct": (52, 72),
    "arb_lo_pct": (24, 48),
    "ret_hi_pct": (58, 80),
    "ret_lo_pct": (30, 54),
    "cut1": (5, 14),
    "cut2": (2, 8),
    "boost1": (6, 24),
    "boost2": (2, 12),
    "arb_cut": (2, 9),
    "arb_boost": (3, 14),
    "ret_boost": (2, 12),
    "ret_cut": (1, 7),
    "fair_t1": (4, 14),
    "fair_t2": (10, 28),
    "fair_t3": (18, 44),
    "fair_s1": (1, 5),
    "fair_s2": (2, 9),
    "fair_s3": (3, 14),
    "inv_t1": (120, 360),
    "inv_t2": (260, 700),
    "inv_t3": (420, 950),
    "inv_s1": (1, 5),
    "inv_s2": (2, 9),
    "inv_s3": (3, 14),
    "inv_conflict_div": (1, 4),
    "first_retail_bump": (0, 6),
    "arb_relax": (0, 8),
    "multi_step_retail_alpha": (10, 24),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parallel optimizer for fair-aware world-state strategy.")
    parser.add_argument("--mode", choices=["launch", "worker", "collect"], default="launch")
    parser.add_argument(
        "--base-strategy",
        default="Strat/my_strategy_worldstate_tiltfair_v1_20260215.sol",
        help="Seed strategy path; workers evaluate this source as the initial incumbent.",
    )
    parser.add_argument("--hours", type=float, default=4.0)
    parser.add_argument("--target-fee-bps", type=float, default=35.5)
    parser.add_argument("--quick-sims", type=int, default=40)
    parser.add_argument("--refine-sims", type=int, default=180)
    parser.add_argument("--refine-every", type=int, default=6)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260215)
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument("--out-dir", default="")
    return parser.parse_args()


def random_params(rng: random.Random) -> Params:
    values: dict[str, int] = {}
    for k, (lo, hi) in BOUNDS.items():
        values[k] = rng.randint(lo, hi)
    p = Params(**values)
    if not valid_params(p):
        return random_params(rng)
    return p


def mutate_params(rng: random.Random, parent: Params) -> Params:
    child = Params(**asdict(parent))
    n_mut = rng.randint(4, 12)
    keys = list(BOUNDS.keys())
    for _ in range(n_mut):
        k = rng.choice(keys)
        lo, hi = BOUNDS[k]
        span = hi - lo
        step = max(1, int(span * rng.choice([0.04, 0.08, 0.12, 0.18])))
        if rng.random() < 0.60:
            v = getattr(child, k) + rng.randint(-step, step)
        else:
            v = rng.randint(lo, hi)
        v = max(lo, min(hi, v))
        setattr(child, k, v)
    if not valid_params(child):
        return random_params(rng)
    return child


def valid_params(p: Params) -> bool:
    if p.quote_min_bps >= p.quote_max_bps:
        return False
    if p.base_min_bps >= p.base_max_bps:
        return False
    if not (p.base_min_bps <= p.base_early_bps <= p.base_max_bps):
        return False
    if not (p.base_min_bps <= p.base_late_bps <= p.base_max_bps):
        return False
    if p.open_bps < max(0, p.quote_min_bps - 12) or p.open_bps > p.quote_max_bps + 12:
        return False
    if not (p.vol_lo1 < p.vol_lo2 < p.vol_hi2 < p.vol_hi1):
        return False
    if not (p.arb_lo_pct < p.arb_hi_pct):
        return False
    if not (p.ret_lo_pct < p.ret_hi_pct):
        return False
    if not (p.fair_t1 < p.fair_t2 < p.fair_t3):
        return False
    if not (p.inv_t1 < p.inv_t2 < p.inv_t3):
        return False
    if not (p.fair_s1 <= p.fair_s2 <= p.fair_s3):
        return False
    if not (p.inv_s1 <= p.inv_s2 <= p.inv_s3):
        return False
    return True


def strategy_source(p: Params, name: str) -> str:
    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {{AMMStrategyBase}} from "./AMMStrategyBase.sol";
import {{TradeInfo}} from "./IAMMStrategy.sol";

contract Strategy is AMMStrategyBase {{
    uint256 private constant A_ARB = {p.a_arb}e16;
    uint256 private constant A_VOL = {p.a_vol}e16;
    uint256 private constant A_RETAIL = {p.a_retail}e16;
    uint256 private constant A_PRICE_ARB = {p.a_price_arb}e16;
    uint256 private constant A_PRICE_SPOT = {p.a_price_spot}e16;

    uint256 private constant FAIR_T1 = {p.fair_t1};
    uint256 private constant FAIR_T2 = {p.fair_t2};
    uint256 private constant FAIR_T3 = {p.fair_t3};
    uint256 private constant FAIR_S1 = {p.fair_s1};
    uint256 private constant FAIR_S2 = {p.fair_s2};
    uint256 private constant FAIR_S3 = {p.fair_s3};

    uint256 private constant INV_T1 = {p.inv_t1};
    uint256 private constant INV_T2 = {p.inv_t2};
    uint256 private constant INV_T3 = {p.inv_t3};
    uint256 private constant INV_S1 = {p.inv_s1};
    uint256 private constant INV_S2 = {p.inv_s2};
    uint256 private constant INV_S3 = {p.inv_s3};
    uint256 private constant INV_CONFLICT_DIV = {p.inv_conflict_div};

    uint256 private constant FIRST_RETAIL_BUMP = {p.first_retail_bump};
    uint256 private constant ARB_RELAX = {p.arb_relax};
    uint256 private constant MULTI_STEP_RETAIL_ALPHA = {p.multi_step_retail_alpha}e16;

    function getName() external pure override returns (string memory) {{
        return "{name}";
    }}

    function afterInitialize(uint256 initialX, uint256 initialY)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {{
        uint256 open = bpsToWad({p.open_bps});
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
    }}

    function afterSwap(TradeInfo calldata trade)
        external
        override
        returns (uint256 bidFee, uint256 askFee)
    {{
        uint256 bidPrev = slots[0];
        uint256 askPrev = slots[1];
        if (bidPrev == 0) bidPrev = bpsToWad({p.open_bps});
        if (askPrev == 0) askPrev = bpsToWad({p.open_bps});

        uint256 xPre = slots[10];
        uint256 yPre = slots[11];
        if (xPre == 0 || yPre == 0) {{
            xPre = trade.reserveX;
            yPre = trade.reserveY;
        }}

        uint256 pHat = slots[8];
        if (pHat == 0) pHat = _safePrice(trade.reserveY, trade.reserveX, 100 * WAD);

        uint256 lastTs = slots[2];
        uint256 stepTrades = slots[3];
        if (trade.timestamp != lastTs) {{
            lastTs = trade.timestamp;
            stepTrades = 0;
        }}
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

        if (stepTrades == 1) {{
            uint256 feeUsed = trade.isBuy ? bidPrev : askPrev;
            uint256 gamma = _gammaFromFee(feeUsed);
            pObs = trade.isBuy ? wmul(spotPost, gamma) : wdiv(spotPost, gamma);
            bool directionOk = trade.isBuy ? (spotPre > pObs) : (spotPre < pObs);
            uint256 relToHat = _relDiff(pObs, pHat);
            if (directionOk && relToHat <= bpsToWad({p.arb_gate_bps})) probableArb = true;
        }}

        if (probableArb) {{
            arbProb = _ewma(arbProb, WAD, A_ARB);
            volHat = _ewma(volHat, _relDiff(pObs, lastArbP), A_VOL);
            pHat = _ewma(pHat, pObs, A_PRICE_ARB);
            lastArbP = pObs;
            retailHat = _ewma(retailHat, 0, A_RETAIL);
        }} else {{
            arbProb = _ewma(arbProb, 0, A_ARB);
            retailHat = _ewma(retailHat, WAD, A_RETAIL);
            pHat = _ewma(pHat, spotPost, A_PRICE_SPOT);
            if (stepTrades > 1) {{
                retailHat = _ewma(retailHat, WAD, MULTI_STEP_RETAIL_ALPHA);
            }}
        }}

        uint256 tradeCount = slots[9] + 1;
        uint256 baseBps = _baseFromState(volHat, arbProb, retailHat, tradeCount);
        uint256 bidBps = baseBps;
        uint256 askBps = baseBps;

        uint256 stale = _relDiff(spotPost, pHat);
        bool fairPreferSellX = spotPost < pHat;
        uint256 fairTilt = _fairTiltBps(stale);
        if (fairTilt > 0) {{
            if (fairPreferSellX) {{
                if (askBps > fairTilt) askBps -= fairTilt;
                bidBps += fairTilt;
            }} else {{
                if (bidBps > fairTilt) bidBps -= fairTilt;
                askBps += fairTilt;
            }}
        }}

        uint256 valueX = wmul(pHat, trade.reserveX);
        bool longX = valueX > trade.reserveY;
        bool invPreferSellX = longX;
        uint256 invNum = _absDiff(valueX, trade.reserveY);
        uint256 invDen = valueX + trade.reserveY + 1;
        uint256 invRatio = wdiv(invNum, invDen);
        uint256 invTilt = _inventoryTiltBps(invRatio);

        if (invTilt > 0 && invPreferSellX != fairPreferSellX) {{
            invTilt /= INV_CONFLICT_DIV;
        }}

        if (invTilt > 0) {{
            if (invPreferSellX) {{
                if (askBps > invTilt) askBps -= invTilt;
                bidBps += invTilt;
            }} else {{
                if (bidBps > invTilt) bidBps -= invTilt;
                askBps += invTilt;
            }}
        }}

        if (!probableArb && stepTrades == 1) {{
            bidBps += FIRST_RETAIL_BUMP;
            askBps += FIRST_RETAIL_BUMP;
        }} else if (probableArb) {{
            if (bidBps > ARB_RELAX) bidBps -= ARB_RELAX;
            if (askBps > ARB_RELAX) askBps -= ARB_RELAX;
        }}

        bidBps = _clampBps(bidBps, {p.quote_min_bps}, {p.quote_max_bps});
        askBps = _clampBps(askBps, {p.quote_min_bps}, {p.quote_max_bps});

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
    }}

    function _baseFromState(uint256 volHat, uint256 arbProb, uint256 retailHat, uint256 tradeCount)
        internal pure returns (uint256)
    {{
        uint256 base = tradeCount < 180 ? {p.base_early_bps} : {p.base_late_bps};

        if (volHat > bpsToWad({p.vol_hi1}) / 10) {{
            if (base > {p.cut1}) base -= {p.cut1};
        }} else if (volHat > bpsToWad({p.vol_hi2}) / 10) {{
            if (base > {p.cut2}) base -= {p.cut2};
        }} else if (volHat < bpsToWad({p.vol_lo1}) / 10) {{
            base += {p.boost1};
        }} else if (volHat < bpsToWad({p.vol_lo2}) / 10) {{
            base += {p.boost2};
        }}

        if (arbProb > {p.arb_hi_pct}e16) {{
            if (base > {p.arb_cut}) base -= {p.arb_cut};
        }} else if (arbProb < {p.arb_lo_pct}e16) {{
            base += {p.arb_boost};
        }}

        if (retailHat > {p.ret_hi_pct}e16) {{
            base += {p.ret_boost};
        }} else if (retailHat < {p.ret_lo_pct}e16) {{
            if (base > {p.ret_cut}) base -= {p.ret_cut};
        }}

        return _clampBps(base, {p.base_min_bps}, {p.base_max_bps});
    }}

    function _fairTiltBps(uint256 staleWad) internal pure returns (uint256) {{
        if (staleWad > bpsToWad(FAIR_T3)) return FAIR_S3;
        if (staleWad > bpsToWad(FAIR_T2)) return FAIR_S2;
        if (staleWad > bpsToWad(FAIR_T1)) return FAIR_S1;
        return 0;
    }}

    function _inventoryTiltBps(uint256 invRatioWad) internal pure returns (uint256) {{
        if (invRatioWad > bpsToWad(INV_T3)) return INV_S3;
        if (invRatioWad > bpsToWad(INV_T2)) return INV_S2;
        if (invRatioWad > bpsToWad(INV_T1)) return INV_S1;
        return 0;
    }}

    function _gammaFromFee(uint256 feeWad) internal pure returns (uint256) {{
        uint256 f = clampFee(feeWad);
        return f >= WAD ? 1 : (WAD - f);
    }}

    function _safePrice(uint256 y, uint256 x, uint256 fallbackP) internal pure returns (uint256) {{
        if (x == 0) return fallbackP;
        return wdiv(y, x);
    }}

    function _ewma(uint256 oldV, uint256 obs, uint256 alpha) internal pure returns (uint256) {{
        if (oldV == 0) return obs;
        uint256 beta = WAD - alpha;
        return (oldV * beta) / WAD + (obs * alpha) / WAD;
    }}

    function _relDiff(uint256 a, uint256 b) internal pure returns (uint256) {{
        if (b == 0) return 0;
        return wdiv(_absDiff(a, b), b);
    }}

    function _absDiff(uint256 a, uint256 b) internal pure returns (uint256) {{
        return a > b ? a - b : b - a;
    }}

    function _clampBps(uint256 value, uint256 minBps, uint256 maxBps) internal pure returns (uint256) {{
        if (value < minBps) return minBps;
        if (value > maxBps) return maxBps;
        return value;
    }}
}}
"""


def compute_objective(metrics: dict[str, float], target_fee_bps: float) -> float:
    # Optimize strictly for mean edge.
    _ = target_fee_bps
    return metrics["avg_edge"]


def evaluate(
    source: str,
    n_simulations: int,
    compiler: SolidityCompiler,
    baseline: EVMStrategyAdapter,
    n_workers: int,
) -> dict[str, float] | None:
    comp = compiler.compile(source)
    if not comp.success:
        return None
    user = EVMStrategyAdapter(bytecode=comp.bytecode, abi=comp.abi)
    runner = MatchRunner(
        n_simulations=n_simulations,
        config=build_base_config(seed=None),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
    )
    result = runner.run_match(user, baseline, store_results=True)
    sims = len(result.simulation_results)
    if sims == 0:
        return None

    edges = sorted(float(sim.edges["submission"]) for sim in result.simulation_results)
    p05_idx = max(0, math.floor(0.05 * sims) - 1)
    p10_idx = max(0, math.floor(0.10 * sims) - 1)
    avg_fee_bps = (
        mean((sim.average_fees["submission"][0] + sim.average_fees["submission"][1]) * 0.5 for sim in result.simulation_results)
        * 10000
    )
    retail = mean(sim.retail_volume_y["submission"] for sim in result.simulation_results)
    arb = mean(sim.arb_volume_y["submission"] for sim in result.simulation_results)

    metrics = {
        "avg_edge": float(result.total_edge_a / Decimal(sims)),
        "min_edge": edges[0],
        "p05_edge": edges[p05_idx],
        "p10_edge": edges[p10_idx],
        "median_edge": edges[sims // 2],
        "max_edge": edges[-1],
        "avg_fee_bps": avg_fee_bps,
        "retail_volume_y": retail,
        "arb_volume_y": arb,
    }
    del result
    gc.collect()
    return metrics


def write_json_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def worker_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = args.worker_id
    rng = random.Random(args.seed + wid * 1_000_003)
    base_strategy_path = Path(args.base_strategy)
    base_source: str | None = None

    progress_path = out_dir / f"worker_{wid}.progress.jsonl"
    best_json_path = out_dir / f"worker_{wid}.best.json"
    best_sol_path = out_dir / f"worker_{wid}.best.sol"
    summary_path = out_dir / f"worker_{wid}.summary.json"

    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    n_workers = resolve_n_workers()

    seeds = [
        Params(
            a_arb=10, a_vol=24, a_retail=6, a_price_arb=16, a_price_spot=5,
            open_bps=40, base_early_bps=36, base_late_bps=40, base_min_bps=18, base_max_bps=88,
            quote_min_bps=12, quote_max_bps=100,
            arb_gate_bps=130, vol_hi1=104, vol_hi2=96, vol_lo1=86, vol_lo2=92,
            arb_hi_pct=58, arb_lo_pct=34, ret_hi_pct=70, ret_lo_pct=40,
            cut1=10, cut2=5, boost1=16, boost2=7, arb_cut=6, arb_boost=7, ret_boost=5, ret_cut=3,
            fair_t1=8, fair_t2=16, fair_t3=28, fair_s1=2, fair_s2=4, fair_s3=7,
            inv_t1=220, inv_t2=450, inv_t3=700, inv_s1=2, inv_s2=4, inv_s3=7, inv_conflict_div=2,
            first_retail_bump=1, arb_relax=2, multi_step_retail_alpha=16,
        ),
        Params(
            a_arb=11, a_vol=24, a_retail=5, a_price_arb=14, a_price_spot=6,
            open_bps=28, base_early_bps=32, base_late_bps=36, base_min_bps=16, base_max_bps=78,
            quote_min_bps=10, quote_max_bps=82,
            arb_gate_bps=120, vol_hi1=105, vol_hi2=96, vol_lo1=84, vol_lo2=92,
            arb_hi_pct=56, arb_lo_pct=30, ret_hi_pct=72, ret_lo_pct=38,
            cut1=11, cut2=5, boost1=16, boost2=7, arb_cut=3, arb_boost=7, ret_boost=4, ret_cut=4,
            fair_t1=7, fair_t2=15, fair_t3=26, fair_s1=2, fair_s2=4, fair_s3=7,
            inv_t1=180, inv_t2=380, inv_t3=640, inv_s1=2, inv_s2=4, inv_s3=6, inv_conflict_div=2,
            first_retail_bump=1, arb_relax=2, multi_step_retail_alpha=16,
        ),
    ]

    quick: list[dict[str, Any]] = []
    refined: list[dict[str, Any]] = []
    best_refined: dict[str, Any] | None = None
    deadline = time.time() + args.hours * 3600.0
    iteration = 0

    try:
        base_source = base_strategy_path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[worker {wid}] warning: failed to read base strategy '{base_strategy_path}': {exc}", flush=True)

    print(
        f"[worker {wid}] start hours={args.hours} quick={args.quick_sims} refine={args.refine_sims} objective=avg_edge",
        flush=True,
    )
    if base_source is not None:
        base_metrics = evaluate(base_source, args.refine_sims, compiler, baseline, n_workers)
        if base_metrics is not None:
            base_score = compute_objective(base_metrics, args.target_fee_bps)
            base_rec = {
                "iteration": 0,
                "name": "BASE_STRATEGY",
                "params": None,
                "refine_metrics": base_metrics,
                "refine_score": base_score,
                "timestamp": time.time(),
                "base_strategy": str(base_strategy_path),
            }
            best_refined = base_rec
            best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
            best_sol_path.write_text(base_source, encoding="utf-8")
            write_json_line(progress_path, {"event": "base_refine_seed", **base_rec})
            print(
                f"[worker {wid}] base seed loaded edge={base_metrics['avg_edge']:.2f} score={base_score:.2f}",
                flush=True,
            )
        else:
            print(
                f"[worker {wid}] warning: base strategy compile/eval failed, continuing without base incumbent",
                flush=True,
            )

    while time.time() < deadline:
        iteration += 1
        if iteration <= len(seeds):
            params = seeds[iteration - 1]
        else:
            if not quick or rng.random() < 0.25:
                params = random_params(rng)
            else:
                top_pool = sorted(quick, key=lambda x: x["quick_score"], reverse=True)[: max(4, min(24, len(quick)))]
                parent = Params(**top_pool[rng.randrange(len(top_pool))]["params"])
                params = mutate_params(rng, parent)

        if not valid_params(params):
            continue

        name = f"WorldState_TiltFair_w{wid}_{iteration:06d}"
        source = strategy_source(params, name)
        metrics = evaluate(source, args.quick_sims, compiler, baseline, n_workers)
        if metrics is None:
            continue

        quick_score = compute_objective(metrics, args.target_fee_bps)
        qrec = {
            "iteration": iteration,
            "name": name,
            "params": asdict(params),
            "quick_metrics": metrics,
            "quick_score": quick_score,
            "timestamp": time.time(),
        }
        quick.append(qrec)
        write_json_line(progress_path, {"event": "quick", **qrec})

        best_quick = max(quick, key=lambda x: x["quick_score"])
        print(
            f"[worker {wid}] it={iteration} quick score={quick_score:.2f} edge={metrics['avg_edge']:.2f} "
            f"p10={metrics['p10_edge']:.2f} fee={metrics['avg_fee_bps']:.2f} best={best_quick['name']}:{best_quick['quick_score']:.2f}",
            flush=True,
        )

        if iteration % max(1, args.refine_every) != 0:
            continue

        refined_names = {r["name"] for r in refined}
        pool = [r for r in sorted(quick, key=lambda x: x["quick_score"], reverse=True) if r["name"] not in refined_names]
        if not pool:
            continue

        pick = pool[0]
        p_obj = Params(**pick["params"])
        r_source = strategy_source(p_obj, pick["name"] + "_refined")
        r_metrics = evaluate(r_source, args.refine_sims, compiler, baseline, n_workers)
        if r_metrics is None:
            continue
        r_score = compute_objective(r_metrics, args.target_fee_bps)
        rrec = {
            "iteration": iteration,
            "name": pick["name"],
            "params": pick["params"],
            "refine_metrics": r_metrics,
            "refine_score": r_score,
            "timestamp": time.time(),
        }
        refined.append(rrec)
        write_json_line(progress_path, {"event": "refine", **rrec})
        print(
            f"[worker {wid}] it={iteration} REFINE score={r_score:.2f} edge={r_metrics['avg_edge']:.2f} "
            f"p10={r_metrics['p10_edge']:.2f} fee={r_metrics['avg_fee_bps']:.2f}",
            flush=True,
        )

        if best_refined is None or r_score > best_refined["refine_score"]:
            best_refined = rrec
            best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
            best_sol_path.write_text(strategy_source(p_obj, f"WorldState_TiltFair_best_worker_{wid}"), encoding="utf-8")
            print(
                f"[worker {wid}] NEW BEST refine score={r_score:.2f} edge={r_metrics['avg_edge']:.2f} "
                f"fee={r_metrics['avg_fee_bps']:.2f}",
                flush=True,
            )

    summary = {
        "worker_id": wid,
        "iterations": iteration,
        "quick_evals": len(quick),
        "refine_evals": len(refined),
        "best_refined": best_refined,
        "target_fee_bps": args.target_fee_bps,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_json_line(progress_path, {"event": "complete", **summary})
    print(f"[worker {wid}] complete", flush=True)
    return 0


def launch_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else Path("Strat") / (
        "tiltfair_search_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    script = Path(__file__).resolve()
    procs: list[dict[str, Any]] = []
    for wid in range(args.workers):
        stdout_path = out_dir / f"worker_{wid}.stdout.log"
        stderr_path = out_dir / f"worker_{wid}.stderr.log"
        cmd = [
            sys.executable,
            str(script),
            "--mode",
            "worker",
            "--worker-id",
            str(wid),
            "--base-strategy",
            str(args.base_strategy),
            "--hours",
            str(args.hours),
            "--target-fee-bps",
            str(args.target_fee_bps),
            "--quick-sims",
            str(args.quick_sims),
            "--refine-sims",
            str(args.refine_sims),
            "--refine-every",
            str(args.refine_every),
            "--seed",
            str(args.seed + wid * 100_003),
            "--out-dir",
            str(out_dir),
        ]
        out_f = stdout_path.open("w", encoding="utf-8")
        err_f = stderr_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=Path.cwd(), stdout=out_f, stderr=err_f)
        procs.append(
            {
                "worker_id": wid,
                "pid": proc.pid,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "command": cmd,
            }
        )

    manifest = {
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(out_dir),
        "workers": args.workers,
        "hours": args.hours,
        "target_fee_bps": args.target_fee_bps,
        "quick_sims": args.quick_sims,
        "refine_sims": args.refine_sims,
        "refine_every": args.refine_every,
        "processes": procs,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Manifest: {manifest_path}")
    return 0


def collect_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    bests = []
    for path in sorted(out_dir.glob("worker_*.best.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bests.append((path, data))
        except Exception:
            continue
    if not bests:
        print(f"No worker best files found in {out_dir}")
        return 1

    def score(entry: tuple[Path, dict[str, Any]]) -> float:
        return float(entry[1].get("refine_score", float("-inf")))

    best_path, best_data = max(bests, key=score)
    best_worker = best_path.stem.split(".")[0]
    worker_id = best_worker.split("_")[1]
    worker_sol = out_dir / f"worker_{worker_id}.best.sol"
    merged_json = out_dir / "best_overall.json"
    merged_sol = out_dir / "best_overall.sol"

    merged_json.write_text(json.dumps(best_data, indent=2, sort_keys=True), encoding="utf-8")
    if worker_sol.exists():
        merged_sol.write_text(worker_sol.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Best worker: {worker_id}")
    print(f"Score: {best_data.get('refine_score')}")
    print(f"Edge: {best_data.get('refine_metrics', {}).get('avg_edge')}")
    print(f"Fee bps: {best_data.get('refine_metrics', {}).get('avg_fee_bps')}")
    print(f"Wrote: {merged_json}")
    if merged_sol.exists():
        print(f"Wrote: {merged_sol}")
    return 0


def main() -> int:
    args = parse_args()
    if args.mode == "worker":
        return worker_main(args)
    if args.mode == "collect":
        return collect_main(args)
    return launch_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
