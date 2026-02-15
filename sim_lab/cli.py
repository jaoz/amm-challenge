"""CLI for deterministic Python simulation, tracing, and optimization."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

from sim_lab.config import OptimizationConfig, StrategyParams, WorldConfig
from sim_lab.optimizer import run_deterministic_optimization
from sim_lab.simulator import (
    DeterministicSimulator,
    write_estimation_analysis_json,
    write_estimation_diagnostics_csv,
    write_event_trace_csv,
    write_price_plot_png,
    write_step_trace_csv,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic Python AMM strategy simulator.")
    sub = parser.add_subparsers(dest="command", required=True)

    trace = sub.add_parser("trace", help="Run one deterministic simulation and export traces.")
    trace.add_argument("--seed", type=int, default=20260215, help="Simulation seed.")
    trace.add_argument("--steps", type=int, default=3_000, help="Steps for this trace run.")
    trace.add_argument("--out-dir", default="sim_lab/out", help="Output directory.")
    trace.add_argument(
        "--center-fee-bps",
        type=float,
        default=37.0,
        help="Opening center fee in bps.",
    )
    trace.add_argument(
        "--no-plot",
        action="store_true",
        help="Skip PNG plot generation (CSV only).",
    )

    optimize = sub.add_parser("optimize", help="Run deterministic train/validation/test optimization.")
    optimize.add_argument("--iterations", type=int, default=18, help="Number of candidate iterations.")
    optimize.add_argument("--keep-top", type=int, default=5, help="Top train candidates promoted to validation.")
    optimize.add_argument("--train-sims", type=int, default=24, help="Number of train simulations.")
    optimize.add_argument("--val-sims", type=int, default=60, help="Number of validation simulations.")
    optimize.add_argument("--test-sims", type=int, default=120, help="Number of test simulations.")
    optimize.add_argument("--train-steps", type=int, default=1_500, help="Steps per train simulation.")
    optimize.add_argument("--val-steps", type=int, default=3_000, help="Steps per validation simulation.")
    optimize.add_argument("--test-steps", type=int, default=5_000, help="Steps per test simulation.")
    optimize.add_argument("--seed", type=int, default=20260215, help="Master RNG seed.")
    optimize.add_argument("--z-alpha", type=float, default=1.64, help="One-sided z threshold for paired deltas.")
    optimize.add_argument("--min-effect", type=float, default=1.0, help="Minimum paired mean edge delta.")
    optimize.add_argument("--out-dir", default="sim_lab/out", help="Output directory.")

    return parser


def _run_trace(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    strategy_params = replace(StrategyParams(), center_fee_bps=float(args.center_fee_bps))
    world = WorldConfig(n_steps=int(args.steps))
    simulator = DeterministicSimulator(world=world, strategy_params=strategy_params)

    result = simulator.run(
        seed=int(args.seed),
        n_steps=int(args.steps),
        capture_steps=True,
        capture_events=True,
        run_id=f"trace-{run_ts}-{args.seed}-{args.steps}",
    )

    prefix = out_dir / f"trace_ts_{run_ts}_seed_{args.seed}_steps_{args.steps}"
    step_path = write_step_trace_csv(result, prefix.with_name(prefix.name + "_steps.csv"))
    event_path = write_event_trace_csv(result, prefix.with_name(prefix.name + "_events.csv"))
    plot_path = None
    if not args.no_plot:
        plot_path = write_price_plot_png(result, prefix.with_name(prefix.name + "_prices.png"))
    diag_csv_path = write_estimation_diagnostics_csv(
        result,
        prefix.with_name(prefix.name + "_estimation_diagnostics.csv"),
    )
    diag_json_path = write_estimation_analysis_json(
        result,
        prefix.with_name(prefix.name + "_estimation_analysis.json"),
    )

    summary_path = prefix.with_name(prefix.name + "_summary.json")
    summary_path.write_text(json.dumps(result.summary(), indent=2, sort_keys=True), encoding="utf-8")

    print(f"Run ID: {result.run_id}")
    print(f"Seed: {result.seed}")
    print(f"Steps: {result.n_steps}")
    print(f"Edge (submission): {result.total_edge_submission:.4f}")
    print(f"Edge (normalizer): {result.total_edge_normalizer:.4f}")
    print(f"Avg fee bps (submission): {result.avg_fee_bps_submission:.4f}")
    print(f"Step trace: {step_path}")
    print(f"Event trace: {event_path}")
    print(f"Summary: {summary_path}")
    print(f"Estimation diagnostics CSV: {diag_csv_path}")
    print(f"Estimation analysis JSON: {diag_json_path}")
    if plot_path is None:
        print("Plot: skipped or matplotlib unavailable")
    else:
        print(f"Plot: {plot_path}")
    return 0


def _run_optimize(args: argparse.Namespace) -> int:
    config = OptimizationConfig(
        iterations=int(args.iterations),
        keep_top=int(args.keep_top),
        n_train=int(args.train_sims),
        n_val=int(args.val_sims),
        n_test=int(args.test_sims),
        train_steps=int(args.train_steps),
        val_steps=int(args.val_steps),
        test_steps=int(args.test_steps),
        seed=int(args.seed),
        z_alpha=float(args.z_alpha),
        min_effect=float(args.min_effect),
        output_dir=str(args.out_dir),
    )
    summary = run_deterministic_optimization(config)

    print(f"Run ID: {summary.run_id}")
    print(f"Winner: {summary.winner.candidate_id}")
    print(f"Validation mean edge: {summary.winner.val_eval.mean_edge:.4f}")
    print(f"Test mean edge: {summary.test_eval.mean_edge:.4f}")
    print(f"Pass flag: {summary.pass_flag}")
    print(f"Run log: {summary.log_path}")
    print(f"Summary file: {summary.summary_path}")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "trace":
        return _run_trace(args)
    if args.command == "optimize":
        return _run_optimize(args)
    parser.error(f"Unsupported command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
