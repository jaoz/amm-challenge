#!/usr/bin/env python3
"""End-to-end Stage A/B/C pipeline for Theo1 inside Cursor tooling.

This script runs:
1) Stage A search (local optimizer launch + wait + collect)
2) Stage A paired trigger check vs incumbent
3) Stage B statistical gate on fresh seeds
4) Stage C final validation on broader seeds

All stages are automated in one command so no manual stage switching is needed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import math
import multiprocessing
import os
import random
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

from amm_competition.competition.config import BASELINE_VARIANCE, build_base_config
from amm_competition.competition.match import MatchRunner
from amm_competition.evm.adapter import EVMStrategyAdapter
from amm_competition.evm.baseline import load_vanilla_strategy
from amm_competition.evm.compiler import SolidityCompiler


DEFAULT_MUTABLE = (
    "BASE_FEE,MIN_GATE,GATE_SIGMA_MULT,RET_CAP,PHAT_ALPHA_RETAIL,PHAT_ALPHA,"
    "SIGMA_COEF,LAMBDA_COEF,FLOW_SIZE_COEF,TOX_COEF,TOX_QUAD_COEF,TOX_CUBIC_COEF,"
    "SHIELD_TRIGGER,SHIELD_BUFFER,DIR_TOX_COEF,SIGMA_TOX_COEF,"
    # Fix 2: sub-threshold sizeHat blend rate
    "SIZE_SMALL_DECAY,"
    # Fix 3: toxEma smoothing (was 0.051=instantaneous, now tunable around 0.78)
    "TOX_BLEND_DECAY,"
    # Fix 6: gap-aware pHat alpha boost per elapsed step
    "GAP_PHAT_ALPHA_BOOST,"
    # Fix 7: stale-price directional coefficients (reduce double-count with dirState)
    "STALE_DIR_COEF,STALE_ATTRACT_FRAC,"
    # Existing high-impact term now exposed for tuning
    "TRADE_TOX_BOOST"
)


@dataclass
class DeltaSummary:
    n_seeds: int
    mean_delta: float
    lcb95_mean_delta: float
    ucb95_mean_delta: float
    p10_delta: float
    min_delta: float


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def append_log(path: Path, message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_seed_spec(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            start = int(a.strip())
            end = int(b.strip())
            lo = min(start, end)
            hi = max(start, end)
            for seed in range(lo, hi + 1):
                if seed not in seen:
                    seen.add(seed)
                    out.append(seed)
            continue
        seed = int(token)
        if seed not in seen:
            seen.add(seed)
            out.append(seed)
    return out


def quantile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[idx]


def bootstrap_ci_mean(values: list[float], n_bootstrap: int, rng_seed: int) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(rng_seed)
    n = len(values)
    means: list[float] = []
    for _ in range(max(100, n_bootstrap)):
        sample_sum = 0.0
        for _j in range(n):
            sample_sum += values[rng.randrange(n)]
        means.append(sample_sum / n)
    means.sort()
    return quantile(means, 0.025), quantile(means, 0.975)


def summarize_deltas(deltas: list[float], n_bootstrap: int, rng_seed: int) -> DeltaSummary:
    sd = sorted(deltas)
    lcb, ucb = bootstrap_ci_mean(deltas, n_bootstrap=n_bootstrap, rng_seed=rng_seed)
    return DeltaSummary(
        n_seeds=len(deltas),
        mean_delta=mean(deltas) if deltas else 0.0,
        lcb95_mean_delta=lcb,
        ucb95_mean_delta=ucb,
        p10_delta=quantile(sd, 0.10),
        min_delta=sd[0] if sd else 0.0,
    )


def compile_adapter(compiler: SolidityCompiler, source: str) -> EVMStrategyAdapter:
    comp = compiler.compile(source)
    if not comp.success:
        errs = "; ".join(comp.errors or ["compile_failed"])
        raise RuntimeError(f"Compilation failed: {errs}")
    return EVMStrategyAdapter(bytecode=comp.bytecode, abi=comp.abi)


def eval_seed_edge(
    strategy: EVMStrategyAdapter,
    baseline: EVMStrategyAdapter,
    seed: int,
    n_simulations: int,
    n_workers: int,
    seed_offset_scale: int,
) -> float:
    runner = MatchRunner(
        n_simulations=n_simulations,
        config=build_base_config(seed=None),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
        seed_offset=seed * seed_offset_scale,
    )
    result = runner.run_match(strategy, baseline, store_results=True)
    sims = len(result.simulation_results)
    if sims == 0:
        raise RuntimeError(f"Zero simulations returned for seed {seed}")
    return float(result.total_edge_a / Decimal(sims))


def evaluate_paired_deltas(
    base_source: str,
    candidate_source: str,
    seeds: list[int],
    *,
    n_simulations: int,
    n_workers: int,
    seed_offset_scale: int,
    n_bootstrap: int,
    rng_seed: int,
) -> dict[str, Any]:
    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    incumbent = compile_adapter(compiler, base_source)
    candidate = compile_adapter(compiler, candidate_source)

    records: list[dict[str, float]] = []
    deltas: list[float] = []
    for seed in seeds:
        base_edge = eval_seed_edge(
            incumbent,
            baseline,
            seed,
            n_simulations=n_simulations,
            n_workers=n_workers,
            seed_offset_scale=seed_offset_scale,
        )
        cand_edge = eval_seed_edge(
            candidate,
            baseline,
            seed,
            n_simulations=n_simulations,
            n_workers=n_workers,
            seed_offset_scale=seed_offset_scale,
        )
        delta = cand_edge - base_edge
        deltas.append(delta)
        records.append(
            {
                "seed": float(seed),
                "base_avg_edge": base_edge,
                "candidate_avg_edge": cand_edge,
                "delta_avg_edge": delta,
            }
        )

    summary = summarize_deltas(deltas, n_bootstrap=n_bootstrap, rng_seed=rng_seed)
    return {
        "per_seed": records,
        "summary": asdict(summary),
    }


def _distribute_iterations(total: int, workers: int) -> list[int]:
    if total <= 0:
        return [0] * workers
    base = total // workers
    rem = total % workers
    return [base + (1 if i < rem else 0) for i in range(workers)]


def _load_local_optimizer_module(repo_root: Path) -> Any:
    tool_path = repo_root / "Cursor" / "tools" / "optimize_theo1_local_parallel.py"
    spec = importlib.util.spec_from_file_location("cursor_optimize_theo1_local_parallel", tool_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load optimizer module from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    # Register module before exec so decorators relying on sys.modules work.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_stage_a_worker(worker_payload: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(worker_payload["repo_root"])
    worker_id = int(worker_payload["worker_id"])
    worker_dir = Path(worker_payload["worker_dir"])
    worker_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = worker_dir / "stdout.log"
    stderr_path = worker_dir / "stderr.log"
    module = _load_local_optimizer_module(repo_root)

    args = argparse.Namespace(**worker_payload["args"])
    with stdout_path.open("w", encoding="utf-8") as out_f, stderr_path.open("w", encoding="utf-8") as err_f:
        with contextlib.redirect_stdout(out_f), contextlib.redirect_stderr(err_f):
            code = int(module.worker_main(args))
    return {"worker_id": worker_id, "exit_code": code, "stdout": str(stdout_path), "stderr": str(stderr_path)}


def _run_stage_a_worker_entry(worker_payload: dict[str, Any], status_path: str) -> None:
    result: dict[str, Any]
    try:
        result = _run_stage_a_worker(worker_payload)
        result["error"] = None
    except Exception as exc:
        worker_dir = Path(worker_payload["worker_dir"])
        stderr_path = worker_dir / "stderr.log"
        with stderr_path.open("a", encoding="utf-8") as err_f:
            err_f.write(f"\n[worker wrapper] fatal: {exc}\n")
        result = {
            "worker_id": int(worker_payload["worker_id"]),
            "exit_code": 1,
            "stdout": str(worker_dir / "stdout.log"),
            "stderr": str(stderr_path),
            "error": str(exc),
        }
    Path(status_path).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def run_stage_a_native(
    repo_root: Path,
    stage_a_dir: Path,
    args: argparse.Namespace,
    a_train_seeds: list[int],
    a_holdout_seeds: list[int],
    log_path: Path,
) -> dict[str, Any]:
    workers_requested = max(1, int(args.a_workers))
    max_safe = max(1, int(args.a_max_safe_workers))
    workers = min(workers_requested, max_safe)
    worker_iterations = _distribute_iterations(0, workers)
    workers_root = stage_a_dir / "workers"
    workers_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "base_strategy": str(stage_a_dir.parent / "base.sol"),
        "out_dir": str(stage_a_dir),
        "workers_requested": workers_requested,
        "workers": workers,
        "sim_workers": int(args.a_sim_workers),
        "hours_cap": float(args.a_hours),
        "iterations_total": 0,
        "iterations_per_worker": worker_iterations,
        "quick_sims": int(args.a_quick_sims),
        "refine_sims": int(args.a_refine_sims),
        "refine_every": int(args.a_refine_every),
        "eval_seeds": a_train_seeds,
        "holdout_seeds": a_holdout_seeds,
        "step_pct": float(args.a_step_pct),
        "max_changes": int(args.a_max_changes),
        "max_drift": float(args.a_max_drift),
        "restart_prob": float(args.a_restart_prob),
        "global_parent_prob": float(args.a_global_parent_prob),
        "global_top_k": int(args.a_global_top_k),
        "share_sync_every": int(args.a_share_sync_every),
        "min_delta_edge": float(args.a_min_delta_edge),
        "holdout_max_drop_edge": float(args.a_holdout_max_drop_edge),
        "holdout_max_drop_retail": float(args.a_holdout_max_drop_retail),
        "holdout_max_drop_arb": float(args.a_holdout_max_drop_arb),
        "mutable_constants": [x.strip() for x in args.a_mutable_constants.split(",") if x.strip()],
        "algorithm": "native_python_multiprocessing_worker_main",
        "processes": [],
    }

    payloads: list[dict[str, Any]] = []
    for wid in range(workers):
        worker_dir = workers_root / f"worker_{wid}"
        worker_seed = int(args.a_seed) + wid * 100_003
        worker_args = {
            "mode": "worker",
            "base_strategy": str(stage_a_dir.parent / "base.sol"),
            "hours": float(args.a_hours),
            "iterations": 0,
            "worker_iterations": int(worker_iterations[wid]),
            "workers": workers,
            "max_safe_workers": int(args.a_max_safe_workers),
            "spawn_stagger_seconds": float(args.a_spawn_stagger_seconds),
            "sim_workers": int(args.a_sim_workers),
            "total_workers_hint": workers,
            "quick_sims": int(args.a_quick_sims),
            "refine_sims": int(args.a_refine_sims),
            "refine_every": int(args.a_refine_every),
            "seed": worker_seed,
            "eval_seed": a_train_seeds[0] if a_train_seeds else int(args.a_seed),
            "eval_seeds": ",".join(str(s) for s in a_train_seeds),
            "holdout_seeds": ",".join(str(s) for s in a_holdout_seeds),
            "worker_id": wid,
            "mutable_constants": args.a_mutable_constants,
            "step_pct": float(args.a_step_pct),
            "bps_step_scale": 0.15,
            "max_changes": int(args.a_max_changes),
            "max_drift": float(args.a_max_drift),
            "restart_prob": float(args.a_restart_prob),
            "global_parent_prob": float(args.a_global_parent_prob),
            "global_top_k": int(args.a_global_top_k),
            "share_sync_every": int(args.a_share_sync_every),
            "min_delta_edge": float(args.a_min_delta_edge),
            "holdout_max_drop_edge": float(args.a_holdout_max_drop_edge),
            "holdout_max_drop_retail": float(args.a_holdout_max_drop_retail),
            "holdout_max_drop_arb": float(args.a_holdout_max_drop_arb),
            "out_dir": str(stage_a_dir),
        }
        payload = {
            "repo_root": str(repo_root),
            "worker_id": wid,
            "worker_dir": str(worker_dir),
            "args": worker_args,
        }
        payloads.append(payload)
        manifest["processes"].append(
            {
                "worker_id": wid,
                "seed": worker_seed,
                "stdout": str(worker_dir / "stdout.log"),
                "stderr": str(worker_dir / "stderr.log"),
            }
        )

    manifest_path = stage_a_dir / "manifest.json"
    write_json(manifest_path, manifest)
    append_log(log_path, f"stage_a_native_launch workers={workers} sim_workers={args.a_sim_workers}")

    ctx = multiprocessing.get_context("spawn")
    pipeline_pid = os.getpid()
    pids_path = stage_a_dir.parent / "pids.json"
    pids_payload: dict[str, Any] = {
        "pipeline_pid": pipeline_pid,
        "stage_a_dir": str(stage_a_dir),
        "workers": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
    }
    processes: list[dict[str, Any]] = []
    for idx, payload in enumerate(payloads):
        worker_id = int(payload["worker_id"])
        worker_dir = Path(payload["worker_dir"])
        status_path = worker_dir / "exit_status.json"
        proc = ctx.Process(
            target=_run_stage_a_worker_entry,
            args=(payload, str(status_path)),
            name=f"stagea_worker_{worker_id}",
        )
        proc.start()
        processes.append(
            {
                "worker_id": worker_id,
                "process": proc,
                "status_path": status_path,
            }
        )
        pids_payload["workers"].append(
            {
                "worker_id": worker_id,
                "pid": proc.pid,
                "status_path": str(status_path),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "ended_at": None,
                "exit_code": None,
            }
        )
        write_json(pids_path, pids_payload)
        append_log(log_path, f"stage_a_worker_spawned worker_id={worker_id} pid={proc.pid}")
        if idx < len(payloads) - 1 and float(args.a_spawn_stagger_seconds) > 0:
            time.sleep(float(args.a_spawn_stagger_seconds))

    done = 0
    failures: list[str] = []
    for rec in processes:
        worker_id = int(rec["worker_id"])
        proc = rec["process"]
        status_path: Path = rec["status_path"]
        proc.join()
        done += 1
        exit_code = int(proc.exitcode or 0)
        status: dict[str, Any] = {}
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                status = {}
        if status:
            exit_code = int(status.get("exit_code", exit_code))
        append_log(log_path, f"stage_a_worker_complete worker_id={worker_id} exit_code={exit_code} done={done}/{workers}")
        for row in pids_payload["workers"]:
            if int(row["worker_id"]) == worker_id:
                row["ended_at"] = datetime.now(timezone.utc).isoformat()
                row["exit_code"] = exit_code
                break
        write_json(pids_path, pids_payload)
        if exit_code != 0:
            failures.append(f"worker {worker_id} exit={exit_code}")

    pids_payload["ended_at"] = datetime.now(timezone.utc).isoformat()
    write_json(pids_path, pids_payload)
    if failures:
        raise RuntimeError("Stage A failures: " + "; ".join(failures))
    append_log(log_path, "stage_a_native_complete")
    return manifest


def collect_stage_a_best_native(stage_a_dir: Path) -> tuple[Path, dict[str, Any]]:
    bests: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((stage_a_dir / "workers").glob("worker_*/best.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bests.append((path, data))
        except Exception:
            continue
    if not bests:
        raise RuntimeError(f"No worker best files found in {stage_a_dir}")

    def score(entry: tuple[Path, dict[str, Any]]) -> float:
        data = entry[1]
        if "refine_score" in data:
            return float(data["refine_score"])
        return float(data.get("refine_metrics", {}).get("avg_edge", float("-inf")))

    best_path, best_data = max(bests, key=score)
    worker_sol = best_path.with_name("best.sol")
    merged_json = stage_a_dir / "best_overall.json"
    merged_sol = stage_a_dir / "best_overall.sol"
    merged_json.write_text(json.dumps(best_data, indent=2, sort_keys=True), encoding="utf-8")
    if worker_sol.exists():
        merged_sol.write_text(worker_sol.read_text(encoding="utf-8"), encoding="utf-8")
    return merged_sol, best_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run automated Stage A/B/C Theo1 pipeline.")
    parser.add_argument(
        "--base-strategy",
        default="Strat/theo1_local_islandga_10h_expanded_q30r100_w7i5_seed42000_20260217T2250358541760Z/worker_0.best.sol",
    )
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--seed-offset-scale", type=int, default=1_000_000)
    parser.add_argument("--bootstrap", type=int, default=4000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260218)
    parser.add_argument("--gate-p10-floor", type=float, default=-10.0)

    # Stage A config
    parser.add_argument("--a-hours", type=float, default=2.0)
    parser.add_argument("--a-workers", type=int, default=20)
    parser.add_argument("--a-max-safe-workers", type=int, default=24)
    parser.add_argument("--a-sim-workers", type=int, default=1)
    parser.add_argument("--a-spawn-stagger-seconds", type=float, default=0.5)
    parser.add_argument("--a-seed", type=int, default=42000)
    parser.add_argument("--a-train-seeds", default="42000-42004")
    parser.add_argument("--a-holdout-seeds", default="43000-43005")
    parser.add_argument("--a-quick-sims", type=int, default=6)
    parser.add_argument("--a-refine-sims", type=int, default=12)
    parser.add_argument("--a-refine-every", type=int, default=4)
    parser.add_argument("--a-step-pct", type=float, default=0.03)
    parser.add_argument("--a-max-changes", type=int, default=3)
    parser.add_argument("--a-max-drift", type=float, default=2.0)
    parser.add_argument("--a-restart-prob", type=float, default=0.20)
    parser.add_argument("--a-global-parent-prob", type=float, default=0.40)
    parser.add_argument("--a-global-top-k", type=int, default=64)
    parser.add_argument("--a-share-sync-every", type=int, default=4)
    parser.add_argument("--a-min-delta-edge", type=float, default=0.005)
    parser.add_argument("--a-holdout-max-drop-edge", type=float, default=0.10)
    parser.add_argument("--a-holdout-max-drop-retail", type=float, default=200.0)
    parser.add_argument("--a-holdout-max-drop-arb", type=float, default=100.0)
    parser.add_argument("--a-mutable-constants", default=DEFAULT_MUTABLE)
    parser.add_argument("--a-trigger-mean-delta", type=float, default=5.0)
    parser.add_argument("--a-trigger-p10-floor", type=float, default=-10.0)
    parser.add_argument("--a-trigger-sims", type=int, default=16)

    # Stage B
    parser.add_argument("--b-seeds", default="44000-44019")
    parser.add_argument("--b-sims", type=int, default=16)
    parser.add_argument("--b-workers", type=int, default=1)

    # Stage C
    parser.add_argument("--c-seeds", default="45000-45047")
    parser.add_argument("--c-sims", type=int, default=20)
    parser.add_argument("--c-workers", type=int, default=1)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else Path("Cursor") / "runs" / f"{utc_stamp()}_theo1_staged_pipeline"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "pipeline.log"

    base_path = Path(args.base_strategy)
    if not base_path.exists():
        raise FileNotFoundError(f"Base strategy not found: {base_path}")
    base_source = base_path.read_text(encoding="utf-8")

    base_copy_path = out_dir / "base.sol"
    base_copy_path.write_text(base_source, encoding="utf-8")

    stage_a_dir = out_dir / "stage_a_search"
    stage_a_dir.mkdir(parents=True, exist_ok=True)

    append_log(log_path, f"pipeline_start out_dir={out_dir}")
    append_log(log_path, f"base_strategy={base_path}")

    a_train_seeds = parse_seed_spec(args.a_train_seeds)
    a_holdout_seeds = parse_seed_spec(args.a_holdout_seeds)
    b_seeds = parse_seed_spec(args.b_seeds)
    c_seeds = parse_seed_spec(args.c_seeds)

    # Stage A launch + wait + collect (native python; no shell subprocesses)
    append_log(log_path, "stage_a_launch_start")
    manifest = run_stage_a_native(
        repo_root=repo_root,
        stage_a_dir=stage_a_dir,
        args=args,
        a_train_seeds=a_train_seeds,
        a_holdout_seeds=a_holdout_seeds,
        log_path=log_path,
    )
    append_log(log_path, f"stage_a_manifest workers={manifest.get('workers')}")
    append_log(log_path, "stage_a_collect_start")
    candidate_sol, _best_meta = collect_stage_a_best_native(stage_a_dir)
    if not candidate_sol.exists():
        raise RuntimeError("Stage A native collect did not produce best_overall.sol")
    candidate_source = candidate_sol.read_text(encoding="utf-8")

    # Stage A trigger check on deltas (search-stage promotion buffer)
    append_log(log_path, "stage_a_trigger_eval_start")
    stage_a_eval = evaluate_paired_deltas(
        base_source,
        candidate_source,
        a_train_seeds,
        n_simulations=args.a_trigger_sims,
        n_workers=args.a_sim_workers,
        seed_offset_scale=args.seed_offset_scale,
        n_bootstrap=args.bootstrap,
        rng_seed=args.bootstrap_seed + 11,
    )
    stage_a_summary = stage_a_eval["summary"]
    stage_a_pass = (
        stage_a_summary["mean_delta"] >= args.a_trigger_mean_delta
        or stage_a_summary["p10_delta"] >= args.a_trigger_p10_floor
    )
    stage_a_report = {
        "stage": "A",
        "trigger_rule": {
            "mean_delta_threshold": args.a_trigger_mean_delta,
            "p10_delta_floor": args.a_trigger_p10_floor,
            "pass_condition": "mean_delta >= threshold OR p10_delta >= floor",
        },
        "pass": stage_a_pass,
        "evaluation": stage_a_eval,
    }
    (out_dir / "stage_a_report.json").write_text(json.dumps(stage_a_report, indent=2, sort_keys=True), encoding="utf-8")
    append_log(
        log_path,
        "stage_a_trigger_result "
        f"pass={stage_a_pass} mean_delta={stage_a_summary['mean_delta']:.4f} "
        f"p10_delta={stage_a_summary['p10_delta']:.4f}",
    )

    if not stage_a_pass:
        result = {
            "status": "stopped_stage_a_trigger_failed",
            "stage_a_report": stage_a_report,
            "stage_a_out_dir": str(stage_a_dir),
            "candidate_path": str(candidate_sol),
        }
        (out_dir / "pipeline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        append_log(log_path, "pipeline_stop stage_a_failed")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    # Stage B gate
    append_log(log_path, "stage_b_eval_start")
    stage_b_eval = evaluate_paired_deltas(
        base_source,
        candidate_source,
        b_seeds,
        n_simulations=args.b_sims,
        n_workers=args.b_workers,
        seed_offset_scale=args.seed_offset_scale,
        n_bootstrap=args.bootstrap,
        rng_seed=args.bootstrap_seed + 29,
    )
    bsum = stage_b_eval["summary"]
    stage_b_pass = bsum["lcb95_mean_delta"] > 0.0 and bsum["p10_delta"] >= args.gate_p10_floor
    stage_b_report = {
        "stage": "B",
        "gate_rule": {
            "lcb95_mean_delta_gt": 0.0,
            "p10_delta_floor": args.gate_p10_floor,
            "pass_condition": "lcb95_mean_delta > 0 AND p10_delta >= floor",
        },
        "pass": stage_b_pass,
        "evaluation": stage_b_eval,
    }
    (out_dir / "stage_b_report.json").write_text(json.dumps(stage_b_report, indent=2, sort_keys=True), encoding="utf-8")
    append_log(
        log_path,
        "stage_b_result "
        f"pass={stage_b_pass} lcb95={bsum['lcb95_mean_delta']:.4f} p10={bsum['p10_delta']:.4f}",
    )
    if not stage_b_pass:
        result = {
            "status": "stopped_stage_b_gate_failed",
            "stage_a_report": stage_a_report,
            "stage_b_report": stage_b_report,
            "stage_a_out_dir": str(stage_a_dir),
            "candidate_path": str(candidate_sol),
        }
        (out_dir / "pipeline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        append_log(log_path, "pipeline_stop stage_b_failed")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    # Stage C final validation
    append_log(log_path, "stage_c_eval_start")
    stage_c_eval = evaluate_paired_deltas(
        base_source,
        candidate_source,
        c_seeds,
        n_simulations=args.c_sims,
        n_workers=args.c_workers,
        seed_offset_scale=args.seed_offset_scale,
        n_bootstrap=args.bootstrap,
        rng_seed=args.bootstrap_seed + 47,
    )
    csum = stage_c_eval["summary"]
    stage_c_pass = csum["lcb95_mean_delta"] > 0.0 and csum["p10_delta"] >= args.gate_p10_floor
    stage_c_report = {
        "stage": "C",
        "final_rule": {
            "lcb95_mean_delta_gt": 0.0,
            "p10_delta_floor": args.gate_p10_floor,
            "pass_condition": "lcb95_mean_delta > 0 AND p10_delta >= floor",
        },
        "pass": stage_c_pass,
        "evaluation": stage_c_eval,
    }
    (out_dir / "stage_c_report.json").write_text(json.dumps(stage_c_report, indent=2, sort_keys=True), encoding="utf-8")
    append_log(
        log_path,
        "stage_c_result "
        f"pass={stage_c_pass} lcb95={csum['lcb95_mean_delta']:.4f} p10={csum['p10_delta']:.4f}",
    )

    promoted_path = out_dir / "promoted_best.sol"
    if stage_c_pass:
        shutil.copyfile(candidate_sol, promoted_path)
        status = "passed_all_stages"
        append_log(log_path, f"pipeline_complete promoted={promoted_path}")
    else:
        status = "stopped_stage_c_final_failed"
        append_log(log_path, "pipeline_stop stage_c_failed")

    result = {
        "status": status,
        "stage_a_report": stage_a_report,
        "stage_b_report": stage_b_report,
        "stage_c_report": stage_c_report,
        "stage_a_out_dir": str(stage_a_dir),
        "candidate_path": str(candidate_sol),
        "promoted_path": str(promoted_path) if stage_c_pass else None,
    }
    (out_dir / "pipeline_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
