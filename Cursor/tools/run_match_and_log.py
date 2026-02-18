#!/usr/bin/env python3
"""Run a strategy match and log structured metrics to JSONL."""

from __future__ import annotations

import argparse
import json
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean

import amm_sim_rs

from amm_competition.competition.config import (
    BASELINE_SETTINGS,
    BASELINE_VARIANCE,
    build_base_config,
    resolve_n_workers,
)
from amm_competition.competition.match import MatchRunner
from amm_competition.evm.adapter import EVMStrategyAdapter
from amm_competition.evm.baseline import load_vanilla_strategy
from amm_competition.evm.compiler import SolidityCompiler
from amm_competition.evm.validator import SolidityValidator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run baseline match for a strategy and append metrics to a JSONL run log."
    )
    parser.add_argument(
        "strategy",
        nargs="?",
        default="Strat/Strategy.sol",
        help="Path to strategy .sol file (default: Strat/Strategy.sol).",
    )
    parser.add_argument(
        "--log-path",
        default="logs/match_runs.jsonl",
        help="Output JSONL path (default: logs/match_runs.jsonl).",
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=None,
        help="Number of simulations (default: baseline config).",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=None,
        help="Steps per simulation (default: baseline config).",
    )
    parser.add_argument(
        "--stage",
        choices=["initial", "train", "validation", "test"],
        default="initial",
        help="Experiment stage label for the log entry.",
    )
    parser.add_argument(
        "--seed-set-id",
        default="S_train",
        help="Seed-set label (e.g. S_train, S_val, S_test).",
    )
    parser.add_argument(
        "--seed-offset",
        type=int,
        default=0,
        help="Seed offset to keep seed sets disjoint (default: 0).",
    )
    parser.add_argument(
        "--eval-seed",
        type=int,
        default=None,
        help=(
            "Convenience: set seed_offset to (eval_seed * seed_offset_scale + seed_offset). "
            "Matches the optimizer's per-seed diversity convention."
        ),
    )
    parser.add_argument(
        "--seed-offset-scale",
        type=int,
        default=1_000_000,
        help="Multiplier for --eval-seed when deriving seed_offset (default: 1_000_000).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run id (default: auto-generated UUID).",
    )
    parser.add_argument(
        "--params-json",
        default="{}",
        help="JSON object string with tunable parameters used for this run.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker count override (default: auto).",
    )
    return parser.parse_args()


def quantile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[idx]


def parse_params_json(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def build_config(n_steps: int) -> amm_sim_rs.SimulationConfig:
    base = build_base_config(seed=None)
    if n_steps == base.n_steps:
        return base

    return amm_sim_rs.SimulationConfig(
        n_steps=n_steps,
        initial_price=base.initial_price,
        initial_x=base.initial_x,
        initial_y=base.initial_y,
        gbm_mu=base.gbm_mu,
        gbm_sigma=base.gbm_sigma,
        gbm_dt=base.gbm_dt,
        retail_arrival_rate=base.retail_arrival_rate,
        retail_mean_size=base.retail_mean_size,
        retail_size_sigma=base.retail_size_sigma,
        retail_buy_prob=base.retail_buy_prob,
        seed=base.seed,
    )


def main() -> int:
    args = parse_args()
    strategy_path = Path(args.strategy)
    if not strategy_path.exists():
        print(f"Error: Strategy file not found: {strategy_path}")
        return 1

    source = strategy_path.read_text(encoding="utf-8")
    start_ts = datetime.now(timezone.utc).isoformat()

    validator = SolidityValidator()
    validation = validator.validate(source)
    if not validation.valid:
        print("Validation failed:")
        for err in validation.errors:
            print(f"  - {err}")
        return 1

    compiler = SolidityCompiler()
    comp = compiler.compile(source)
    if not comp.success:
        print("Compilation failed:")
        for err in (comp.errors or []):
            print(f"  - {err}")
        return 1

    user_strategy = EVMStrategyAdapter(bytecode=comp.bytecode, abi=comp.abi)
    strategy_name = user_strategy.get_name()
    baseline = load_vanilla_strategy()

    n_sims = args.simulations if args.simulations is not None else BASELINE_SETTINGS.n_simulations
    n_steps = args.steps if args.steps is not None else BASELINE_SETTINGS.n_steps
    n_workers = args.workers if args.workers is not None else resolve_n_workers()

    effective_seed_offset = args.seed_offset
    if args.eval_seed is not None:
        effective_seed_offset = int(args.eval_seed) * int(args.seed_offset_scale) + int(args.seed_offset)

    runner = MatchRunner(
        n_simulations=n_sims,
        config=build_config(n_steps),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
        seed_offset=effective_seed_offset,
    )

    result = runner.run_match(user_strategy, baseline, store_results=True)
    end_ts = datetime.now(timezone.utc).isoformat()

    sims = result.simulation_results
    if not sims:
        print("No simulation results were returned; nothing logged.")
        return 1

    edges = sorted(float(sim.edges.get("submission", 0.0)) for sim in sims)
    mean_edge = float(result.total_edge_a / Decimal(len(sims)))
    p10_edge = quantile(edges, 0.10)
    p90_edge = quantile(edges, 0.90)
    max_edge = edges[-1]
    avg_fee_bps = mean(
        (sim.average_fees["submission"][0] + sim.average_fees["submission"][1]) * 0.5 * 10000
        for sim in sims
    )
    arb_volume_y = mean(sim.arb_volume_y.get("submission", 0.0) for sim in sims)
    retail_volume_y = mean(sim.retail_volume_y.get("submission", 0.0) for sim in sims)

    run_id = args.run_id if args.run_id else str(uuid.uuid4())
    record = {
        "run_id": run_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "stage": args.stage,
        "strategy_path": str(strategy_path),
        "strategy_name": strategy_name,
        "params_json": parse_params_json(args.params_json),
        "seed_set_id": args.seed_set_id,
        "seed_offset": effective_seed_offset,
        "eval_seed": args.eval_seed,
        "seed_offset_scale": args.seed_offset_scale,
        "n_simulations": n_sims,
        "n_steps": n_steps,
        "mean_edge": mean_edge,
        "p10_edge": p10_edge,
        "p90_edge": p90_edge,
        "max_edge": max_edge,
        "avg_fee_bps": avg_fee_bps,
        "arb_volume_y": arb_volume_y,
        "retail_volume_y": retail_volume_y,
        # Current Python API does not expose per-trade arb/retail counts.
        "arb_trade_count": None,
        "retail_trade_count": None,
        "trade_count_note": "arb/retail trade counts unavailable in current API; placeholders logged",
    }

    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")

    print(f"Strategy: {strategy_name}")
    print(f"Mean edge: {mean_edge:.4f}")
    print(f"P10/P90 edge: {p10_edge:.4f} / {p90_edge:.4f}")
    print(f"Max edge: {max_edge:.4f}")
    print(f"Avg fee (bps): {avg_fee_bps:.4f}")
    print(f"Mean arb volume Y: {arb_volume_y:.4f}")
    print(f"Mean retail volume Y: {retail_volume_y:.4f}")
    print(f"Logged run to: {log_path}")
    print(f"Run ID: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
