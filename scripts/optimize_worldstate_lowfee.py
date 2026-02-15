#!/usr/bin/env python3
"""Long-run parameter search for low-fee WorldState strategy variants.

Search objective:
- maximize robust edge (avg + p10 + p05)
- target average fee around 35-36 bps
- prefer higher retail flow / controlled arb flow
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import time
from dataclasses import dataclass, asdict
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
    open_bps: int
    arb_gate_bps: int
    quote_min_bps: int
    quote_max_bps: int
    base_early_bps: int
    base_late_bps: int
    vol_hi1: int
    vol_hi2: int
    vol_lo1: int
    vol_lo2: int
    cut1: int
    cut2: int
    boost1: int
    boost2: int
    arb_hi_pct: int
    arb_lo_pct: int
    arb_cut: int
    arb_boost: int
    ret_hi_pct: int
    ret_lo_pct: int
    ret_boost: int
    ret_cut: int
    base_min_bps: int
    base_max_bps: int
    a_arb: int
    a_vol: int
    a_retail: int
    a_price_arb: int
    a_price_spot: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search low-fee parameter sets for WorldState strategy.")
    parser.add_argument(
        "--base-strategy",
        default="Strat/my_strategy_worldstate_v6_20260215.sol",
        help="Base strategy file to mutate.",
    )
    parser.add_argument("--hours", type=float, default=4.0, help="Search duration in hours.")
    parser.add_argument("--quick-sims", type=int, default=48, help="Sims for quick evaluations.")
    parser.add_argument("--refine-sims", type=int, default=220, help="Sims for refine evaluations.")
    parser.add_argument(
        "--refine-every",
        type=int,
        default=8,
        help="Run one refine evaluation every N quick evaluations.",
    )
    parser.add_argument("--target-fee-bps", type=float, default=35.5, help="Target average fee in bps.")
    parser.add_argument(
        "--out-prefix",
        default="Strat/search_v6_lowfee",
        help="Prefix for log/checkpoint files.",
    )
    parser.add_argument("--seed", type=int, default=20260215, help="RNG seed.")
    return parser.parse_args()


def apply_params(base_src: str, params: Params, name: str) -> str:
    """Apply parameter replacements to the v6 source."""
    src = base_src
    repl = [
        ('return "WorldState_BandShift_v6_20260215";', f'return "{name}";'),
        ("uint256 private constant A_ARB = 10e16;", f"uint256 private constant A_ARB = {params.a_arb}e16;"),
        ("uint256 private constant A_VOL = 22e16;", f"uint256 private constant A_VOL = {params.a_vol}e16;"),
        ("uint256 private constant A_RETAIL = 7e16;", f"uint256 private constant A_RETAIL = {params.a_retail}e16;"),
        ("uint256 private constant A_PRICE_ARB = 18e16;", f"uint256 private constant A_PRICE_ARB = {params.a_price_arb}e16;"),
        ("uint256 private constant A_PRICE_SPOT = 4e16;", f"uint256 private constant A_PRICE_SPOT = {params.a_price_spot}e16;"),
        ("uint256 open = bpsToWad(78);", f"uint256 open = bpsToWad({params.open_bps});"),
        (
            "if (bidPrev == 0) bidPrev = bpsToWad(78);",
            f"if (bidPrev == 0) bidPrev = bpsToWad({params.open_bps});",
        ),
        (
            "if (askPrev == 0) askPrev = bpsToWad(78);",
            f"if (askPrev == 0) askPrev = bpsToWad({params.open_bps});",
        ),
        (
            "if (directionOk && relToHat <= bpsToWad(120)) {",
            f"if (directionOk && relToHat <= bpsToWad({params.arb_gate_bps})) {{",
        ),
        (
            "bidBps = _clampBps(bidBps, 60, 104);",
            f"bidBps = _clampBps(bidBps, {params.quote_min_bps}, {params.quote_max_bps});",
        ),
        (
            "askBps = _clampBps(askBps, 60, 104);",
            f"askBps = _clampBps(askBps, {params.quote_min_bps}, {params.quote_max_bps});",
        ),
        (
            "uint256 base = tradeCount < 180 ? 79 : 80;",
            f"uint256 base = tradeCount < 180 ? {params.base_early_bps} : {params.base_late_bps};",
        ),
        (
            "} else if (volHat > bpsToWad(96) / 10) {",
            f"}} else if (volHat > bpsToWad({params.vol_hi2}) / 10) {{",
        ),
        (
            "if (volHat > bpsToWad(103) / 10) {",
            f"if (volHat > bpsToWad({params.vol_hi1}) / 10) {{",
        ),
        ("if (base > 8) base -= 8;", f"if (base > {params.cut1}) base -= {params.cut1};"),
        ("if (base > 5) base -= 5;", f"if (base > {params.cut2}) base -= {params.cut2};"),
        (
            "} else if (volHat < bpsToWad(88) / 10) {",
            f"}} else if (volHat < bpsToWad({params.vol_lo1}) / 10) {{",
        ),
        ("base += 18;", f"base += {params.boost1};"),
        (
            "} else if (volHat < bpsToWad(93) / 10) {",
            f"}} else if (volHat < bpsToWad({params.vol_lo2}) / 10) {{",
        ),
        ("base += 5;", f"base += {params.boost2};"),
        (
            "if (arbProb > 64e16) {",
            f"if (arbProb > {params.arb_hi_pct}e16) {{",
        ),
        ("if (base > 5) base -= 5;", f"if (base > {params.arb_cut}) base -= {params.arb_cut};"),
        (
            "} else if (arbProb < 38e16) {",
            f"}} else if (arbProb < {params.arb_lo_pct}e16) {{",
        ),
        ("base += 9;", f"base += {params.arb_boost};"),
        (
            "if (retailHat > 68e16) {",
            f"if (retailHat > {params.ret_hi_pct}e16) {{",
        ),
        ("base += 4;", f"base += {params.ret_boost};"),
        (
            "} else if (retailHat < 42e16) {",
            f"}} else if (retailHat < {params.ret_lo_pct}e16) {{",
        ),
        ("if (base > 2) base -= 2;", f"if (base > {params.ret_cut}) base -= {params.ret_cut};"),
        (
            "return _clampBps(base, 68, 108);",
            f"return _clampBps(base, {params.base_min_bps}, {params.base_max_bps});",
        ),
    ]
    for old, new in repl:
        if old not in src:
            raise ValueError(f"Pattern not found during replacement: {old!r}")
        src = src.replace(old, new, 1)
    return src


def valid_params(p: Params) -> bool:
    if p.quote_min_bps >= p.quote_max_bps:
        return False
    if p.base_min_bps > p.base_max_bps:
        return False
    if not (p.base_min_bps <= p.base_early_bps <= p.base_max_bps):
        return False
    if not (p.base_min_bps <= p.base_late_bps <= p.base_max_bps):
        return False
    if p.vol_lo1 >= p.vol_lo2 or p.vol_lo2 >= p.vol_hi2 or p.vol_hi2 >= p.vol_hi1:
        return False
    if p.arb_lo_pct >= p.arb_hi_pct:
        return False
    if p.ret_lo_pct >= p.ret_hi_pct:
        return False
    if p.open_bps < p.quote_min_bps - 8 or p.open_bps > p.quote_max_bps + 8:
        return False
    return True


def random_params(rng: random.Random) -> Params:
    return Params(
        open_bps=rng.choice([24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50]),
        arb_gate_bps=rng.choice([100, 110, 120, 130, 140, 150]),
        quote_min_bps=rng.choice([8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30]),
        quote_max_bps=rng.choice([40, 44, 48, 52, 56, 60, 64, 68, 72, 76, 80]),
        base_early_bps=rng.choice(list(range(24, 49))),
        base_late_bps=rng.choice(list(range(26, 53))),
        vol_hi1=rng.choice([101, 103, 105, 107]),
        vol_hi2=rng.choice([92, 94, 96, 98, 100]),
        vol_lo1=rng.choice([82, 84, 86, 88, 90]),
        vol_lo2=rng.choice([88, 90, 92, 94]),
        cut1=rng.choice([6, 7, 8, 9, 10, 11]),
        cut2=rng.choice([3, 4, 5, 6]),
        boost1=rng.choice([8, 10, 12, 14, 16, 18]),
        boost2=rng.choice([3, 4, 5, 6, 7, 8]),
        arb_hi_pct=rng.choice([56, 58, 60, 62, 64, 66]),
        arb_lo_pct=rng.choice([30, 32, 34, 36, 38, 40, 42, 44]),
        arb_cut=rng.choice([3, 4, 5, 6]),
        arb_boost=rng.choice([4, 5, 6, 7, 8, 9, 10]),
        ret_hi_pct=rng.choice([60, 62, 64, 66, 68, 70, 72, 74]),
        ret_lo_pct=rng.choice([32, 34, 36, 38, 40, 42, 44, 46, 48]),
        ret_boost=rng.choice([3, 4, 5, 6, 7, 8]),
        ret_cut=rng.choice([1, 2, 3, 4]),
        base_min_bps=rng.choice([20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40]),
        base_max_bps=rng.choice([52, 56, 60, 64, 68, 72, 76, 80, 84, 88, 92, 96, 100, 104]),
        a_arb=rng.choice([8, 9, 10, 11, 12]),
        a_vol=rng.choice([18, 20, 22, 24, 26, 28]),
        a_retail=rng.choice([5, 6, 7, 8, 9, 10]),
        a_price_arb=rng.choice([14, 16, 18, 20, 22, 24]),
        a_price_spot=rng.choice([2, 3, 4, 5, 6, 7]),
    )


def mutate_params(rng: random.Random, parent: Params) -> Params:
    child = Params(**asdict(parent))
    fields = list(asdict(child).keys())
    n_mut = rng.randint(3, 8)
    for _ in range(n_mut):
        k = rng.choice(fields)
        setattr(child, k, getattr(random_params(rng), k))
    return child


def compute_objective(metrics: dict[str, float], target_fee_bps: float) -> float:
    avg = metrics["avg_edge"]
    p10 = metrics["p10_edge"]
    p05 = metrics["p05_edge"]
    fee = metrics["avg_fee_bps"]
    retail = metrics["retail_volume_y"]
    arb = metrics["arb_volume_y"]

    robust_edge = 0.58 * avg + 0.27 * p10 + 0.15 * p05

    fee_gap = abs(fee - target_fee_bps)
    fee_penalty = 2.8 * fee_gap
    if fee > target_fee_bps + 4.0:
        fee_penalty += 3.5 * (fee - (target_fee_bps + 4.0))
    if fee < target_fee_bps - 4.0:
        fee_penalty += 1.8 * ((target_fee_bps - 4.0) - fee)

    flow_term = 0.00030 * retail - 0.00024 * arb
    return robust_edge + flow_term - fee_penalty


def evaluate(
    source: str,
    n_simulations: int,
    compiler: SolidityCompiler,
    baseline: EVMStrategyAdapter,
    n_workers: int,
) -> dict[str, float] | None:
    compilation = compiler.compile(source)
    if not compilation.success:
        return None

    user = EVMStrategyAdapter(bytecode=compilation.bytecode, abi=compilation.abi)
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
    retail_vol = mean(sim.retail_volume_y["submission"] for sim in result.simulation_results)
    arb_vol = mean(sim.arb_volume_y["submission"] for sim in result.simulation_results)

    metrics = {
        "avg_edge": float(result.total_edge_a / Decimal(sims)),
        "min_edge": edges[0],
        "p05_edge": edges[p05_idx],
        "p10_edge": edges[p10_idx],
        "median_edge": edges[sims // 2],
        "max_edge": edges[-1],
        "avg_fee_bps": avg_fee_bps,
        "retail_volume_y": retail_vol,
        "arb_volume_y": arb_vol,
    }

    del result
    gc.collect()
    return metrics


def write_json_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)

    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out_prefix.with_suffix(".progress.jsonl")
    best_json_path = out_prefix.with_suffix(".best.json")
    best_sol_path = out_prefix.with_suffix(".best.sol")

    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    n_workers = resolve_n_workers()

    # Seed around current v6 and low-fee priors.
    seeds = [
        Params(
            open_bps=78,
            arb_gate_bps=120,
            quote_min_bps=60,
            quote_max_bps=104,
            base_early_bps=79,
            base_late_bps=80,
            vol_hi1=103,
            vol_hi2=96,
            vol_lo1=88,
            vol_lo2=93,
            cut1=8,
            cut2=5,
            boost1=18,
            boost2=5,
            arb_hi_pct=64,
            arb_lo_pct=38,
            arb_cut=5,
            arb_boost=9,
            ret_hi_pct=68,
            ret_lo_pct=42,
            ret_boost=4,
            ret_cut=2,
            base_min_bps=68,
            base_max_bps=108,
            a_arb=10,
            a_vol=22,
            a_retail=7,
            a_price_arb=18,
            a_price_spot=4,
        ),
        Params(
            open_bps=40,
            arb_gate_bps=130,
            quote_min_bps=18,
            quote_max_bps=72,
            base_early_bps=38,
            base_late_bps=40,
            vol_hi1=103,
            vol_hi2=96,
            vol_lo1=88,
            vol_lo2=93,
            cut1=8,
            cut2=5,
            boost1=16,
            boost2=6,
            arb_hi_pct=62,
            arb_lo_pct=36,
            arb_cut=4,
            arb_boost=8,
            ret_hi_pct=66,
            ret_lo_pct=40,
            ret_boost=5,
            ret_cut=2,
            base_min_bps=28,
            base_max_bps=84,
            a_arb=10,
            a_vol=22,
            a_retail=7,
            a_price_arb=18,
            a_price_spot=4,
        ),
    ]

    quick_records: list[dict[str, Any]] = []
    refined_records: list[dict[str, Any]] = []
    best_refined: dict[str, Any] | None = None

    deadline = time.time() + args.hours * 3600.0
    iteration = 0

    print(
        f"Starting search: hours={args.hours}, quick_sims={args.quick_sims}, refine_sims={args.refine_sims}, "
        f"target_fee_bps={args.target_fee_bps}",
        flush=True,
    )
    print(f"Progress log: {progress_path}", flush=True)

    while time.time() < deadline:
        iteration += 1

        if iteration <= len(seeds):
            params = seeds[iteration - 1]
        else:
            # 30% global sample, 70% mutate from top quick candidates.
            if not quick_records or rng.random() < 0.30:
                params = random_params(rng)
            else:
                top_pool = sorted(quick_records, key=lambda r: r["quick_score"], reverse=True)[: max(3, min(20, len(quick_records)))]
                parent = Params(**top_pool[rng.randrange(len(top_pool))]["params"])
                params = mutate_params(rng, parent)

        if not valid_params(params):
            continue

        cand_name = f"WorldState_BandShift_search_{iteration:05d}"
        source = apply_params(base_source, params, cand_name)
        metrics = evaluate(
            source=source,
            n_simulations=args.quick_sims,
            compiler=compiler,
            baseline=baseline,
            n_workers=n_workers,
        )
        if metrics is None:
            continue

        quick_score = compute_objective(metrics, target_fee_bps=args.target_fee_bps)
        rec = {
            "iteration": iteration,
            "name": cand_name,
            "params": asdict(params),
            "quick_metrics": metrics,
            "quick_score": quick_score,
            "timestamp": time.time(),
        }
        quick_records.append(rec)
        write_json_line(progress_path, {"event": "quick_eval", **rec})

        best_quick = max(quick_records, key=lambda r: r["quick_score"])
        print(
            f"[{iteration}] quick score={quick_score:.2f} edge={metrics['avg_edge']:.2f} "
            f"p10={metrics['p10_edge']:.2f} fee={metrics['avg_fee_bps']:.2f} best_quick={best_quick['name']}:{best_quick['quick_score']:.2f}",
            flush=True,
        )

        if iteration % max(1, args.refine_every) != 0:
            continue

        # Refine the strongest quick candidate not yet refined.
        refined_names = {r["name"] for r in refined_records}
        refine_candidates = [r for r in sorted(quick_records, key=lambda r: r["quick_score"], reverse=True) if r["name"] not in refined_names]
        if not refine_candidates:
            continue

        pick = refine_candidates[0]
        refine_name = pick["name"] + "_refine"
        refine_source = apply_params(base_source, Params(**pick["params"]), refine_name)
        refine_metrics = evaluate(
            source=refine_source,
            n_simulations=args.refine_sims,
            compiler=compiler,
            baseline=baseline,
            n_workers=n_workers,
        )
        if refine_metrics is None:
            continue

        refine_score = compute_objective(refine_metrics, target_fee_bps=args.target_fee_bps)
        rrec = {
            "iteration": iteration,
            "name": pick["name"],
            "params": pick["params"],
            "refine_metrics": refine_metrics,
            "refine_score": refine_score,
            "timestamp": time.time(),
        }
        refined_records.append(rrec)
        write_json_line(progress_path, {"event": "refine_eval", **rrec})

        print(
            f"[{iteration}] REFINE {pick['name']} score={refine_score:.2f} edge={refine_metrics['avg_edge']:.2f} "
            f"p10={refine_metrics['p10_edge']:.2f} fee={refine_metrics['avg_fee_bps']:.2f}",
            flush=True,
        )

        if best_refined is None or refine_score > best_refined["refine_score"]:
            best_refined = rrec
            best_source = apply_params(base_source, Params(**pick["params"]), "WorldState_BandShift_search_best")
            best_sol_path.write_text(best_source, encoding="utf-8")
            best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
            print(
                f"[{iteration}] NEW BEST: edge={refine_metrics['avg_edge']:.2f} p10={refine_metrics['p10_edge']:.2f} "
                f"fee={refine_metrics['avg_fee_bps']:.2f} -> {best_sol_path}",
                flush=True,
            )

    summary = {
        "iterations": iteration,
        "quick_evals": len(quick_records),
        "refine_evals": len(refined_records),
        "best_refined": best_refined,
        "target_fee_bps": args.target_fee_bps,
        "quick_sims": args.quick_sims,
        "refine_sims": args.refine_sims,
    }
    write_json_line(progress_path, {"event": "search_complete", **summary})
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
