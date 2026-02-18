#!/usr/bin/env python3
"""Benchmark Stage-A throughput vs worker count and sim_workers.

Goal: maximize estimated simulations per minute.
Runs native worker_main processes directly (no shell launch mode).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import multiprocessing
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_int_csv(raw: str) -> list[int]:
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        out.append(int(token))
    return out


def parse_seed_spec(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            lo = min(int(a.strip()), int(b.strip()))
            hi = max(int(a.strip()), int(b.strip()))
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


def load_optimizer_module(repo_root: Path) -> Any:
    tool_path = repo_root / "Cursor" / "tools" / "optimize_theo1_local_parallel.py"
    spec = importlib.util.spec_from_file_location("cursor_optimize_theo1_local_parallel_bench", tool_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load optimizer module from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_worker_entry(payload: dict[str, Any], status_path: str) -> None:
    repo_root = Path(payload["repo_root"])
    worker_id = int(payload["worker_id"])
    worker_dir = Path(payload["worker_dir"])
    worker_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = worker_dir / "stdout.log"
    stderr_path = worker_dir / "stderr.log"
    module = load_optimizer_module(repo_root)
    args = argparse.Namespace(**payload["args"])

    result: dict[str, Any]
    try:
        with stdout_path.open("w", encoding="utf-8") as out_f, stderr_path.open("w", encoding="utf-8") as err_f:
            with contextlib.redirect_stdout(out_f), contextlib.redirect_stderr(err_f):
                exit_code = int(module.worker_main(args))
        result = {
            "worker_id": worker_id,
            "exit_code": exit_code,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "error": None,
        }
    except Exception as exc:
        with stderr_path.open("a", encoding="utf-8") as err_f:
            err_f.write(f"\n[benchmark wrapper] fatal: {exc}\n")
        result = {
            "worker_id": worker_id,
            "exit_code": 1,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "error": str(exc),
        }
    Path(status_path).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def estimate_sim_matches(
    *,
    quick_evals: int,
    refine_evals: int,
    quick_sims: int,
    refine_sims: int,
    train_seed_count: int,
    holdout_seed_count: int,
) -> int:
    quick_total = quick_evals * quick_sims * train_seed_count
    refine_total = refine_evals * refine_sims * (train_seed_count + holdout_seed_count)
    return int(quick_total + refine_total)


def run_case(
    repo_root: Path,
    *,
    out_dir: Path,
    base_strategy: str,
    workers: int,
    sim_workers: int,
    hours: float,
    quick_sims: int,
    refine_sims: int,
    refine_every: int,
    eval_seeds: list[int],
    holdout_seeds: list[int],
    seed_base: int,
    mutable_constants: str,
    step_pct: float,
    max_changes: int,
    max_drift: float,
    restart_prob: float,
    global_parent_prob: float,
    global_top_k: int,
    share_sync_every: int,
    min_delta_edge: float,
    holdout_max_drop_edge: float,
    holdout_max_drop_retail: float,
    holdout_max_drop_arb: float,
    spawn_stagger_seconds: float,
) -> dict[str, Any]:
    stage_dir = out_dir / f"w{workers}_sw{sim_workers}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    workers_root = stage_dir / "workers"
    workers_root.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "bench.log"

    t0 = time.time()
    ctx = multiprocessing.get_context("spawn")
    procs: list[dict[str, Any]] = []

    for wid in range(workers):
        worker_dir = workers_root / f"worker_{wid}"
        status_path = worker_dir / "exit_status.json"
        payload = {
            "repo_root": str(repo_root),
            "worker_id": wid,
            "worker_dir": str(worker_dir),
            "args": {
                "mode": "worker",
                "base_strategy": base_strategy,
                "hours": hours,
                "iterations": 0,
                "worker_iterations": 0,
                "workers": workers,
                "max_safe_workers": workers,
                "spawn_stagger_seconds": spawn_stagger_seconds,
                "sim_workers": sim_workers,
                "total_workers_hint": workers,
                "quick_sims": quick_sims,
                "refine_sims": refine_sims,
                "refine_every": refine_every,
                "seed": seed_base + wid * 100_003,
                "eval_seed": eval_seeds[0] if eval_seeds else seed_base,
                "eval_seeds": ",".join(str(x) for x in eval_seeds),
                "holdout_seeds": ",".join(str(x) for x in holdout_seeds),
                "worker_id": wid,
                "mutable_constants": mutable_constants,
                "step_pct": step_pct,
                "bps_step_scale": 0.15,
                "max_changes": max_changes,
                "max_drift": max_drift,
                "restart_prob": restart_prob,
                "global_parent_prob": global_parent_prob,
                "global_top_k": global_top_k,
                "share_sync_every": share_sync_every,
                "min_delta_edge": min_delta_edge,
                "holdout_max_drop_edge": holdout_max_drop_edge,
                "holdout_max_drop_retail": holdout_max_drop_retail,
                "holdout_max_drop_arb": holdout_max_drop_arb,
                "out_dir": str(stage_dir),
            },
        }
        proc = ctx.Process(target=run_worker_entry, args=(payload, str(status_path)), name=f"bench_w{workers}_sw{sim_workers}_{wid}")
        proc.start()
        procs.append({"worker_id": wid, "pid": proc.pid, "process": proc, "status_path": status_path})
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} spawned worker={wid} pid={proc.pid}\n")
        if wid < workers - 1 and spawn_stagger_seconds > 0:
            time.sleep(spawn_stagger_seconds)

    failures: list[str] = []
    total_quick = 0
    total_refine = 0
    for rec in procs:
        wid = int(rec["worker_id"])
        proc = rec["process"]
        proc.join()
        status_path: Path = rec["status_path"]
        status = {"exit_code": int(proc.exitcode or 0), "error": None}
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        exit_code = int(status.get("exit_code", int(proc.exitcode or 0)))
        if exit_code != 0:
            failures.append(f"worker {wid} exit={exit_code} err={status.get('error')}")
        summary_path = workers_root / f"worker_{wid}" / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                total_quick += int(summary.get("quick_evals", 0))
                total_refine += int(summary.get("refine_evals", 0))
            except Exception:
                pass

    elapsed_s = max(1.0, time.time() - t0)
    estimated_matches = estimate_sim_matches(
        quick_evals=total_quick,
        refine_evals=total_refine,
        quick_sims=quick_sims,
        refine_sims=refine_sims,
        train_seed_count=len(eval_seeds),
        holdout_seed_count=len(holdout_seeds),
    )
    sims_per_min = estimated_matches / (elapsed_s / 60.0)

    return {
        "workers": workers,
        "sim_workers": sim_workers,
        "elapsed_seconds": round(elapsed_s, 2),
        "quick_evals_total": total_quick,
        "refine_evals_total": total_refine,
        "estimated_sim_matches": estimated_matches,
        "estimated_sims_per_min": round(sims_per_min, 2),
        "pids": [{"worker_id": int(p["worker_id"]), "pid": int(p["pid"])} for p in procs],
        "failed": len(failures) > 0,
        "failures": failures,
        "run_dir": str(stage_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Theo1 Stage-A worker scaling.")
    parser.add_argument(
        "--base-strategy",
        default="Strat/theo1_local_islandga_10h_expanded_q30r100_w7i5_seed42000_20260217T2250358541760Z/worker_0.best.sol",
    )
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--workers-grid", default="4,8,12,16,20,24")
    parser.add_argument("--sim-workers-grid", default="1,2")
    parser.add_argument("--hours", type=float, default=0.012)
    parser.add_argument("--quick-sims", type=int, default=6)
    parser.add_argument("--refine-sims", type=int, default=12)
    parser.add_argument("--refine-every", type=int, default=4)
    parser.add_argument("--eval-seeds", default="42000-42004")
    parser.add_argument("--holdout-seeds", default="43000-43001")
    parser.add_argument("--seed", type=int, default=42000)
    parser.add_argument(
        "--mutable-constants",
        default=(
            "BASE_FEE,MIN_GATE,GATE_SIGMA_MULT,RET_CAP,PHAT_ALPHA_RETAIL,PHAT_ALPHA,"
            "SIGMA_COEF,LAMBDA_COEF,FLOW_SIZE_COEF,TOX_COEF,TOX_QUAD_COEF,TOX_CUBIC_COEF,"
            "SHIELD_TRIGGER,SHIELD_BUFFER,DIR_TOX_COEF,SIGMA_TOX_COEF"
        ),
    )
    parser.add_argument("--step-pct", type=float, default=0.03)
    parser.add_argument("--max-changes", type=int, default=3)
    parser.add_argument("--max-drift", type=float, default=2.0)
    parser.add_argument("--restart-prob", type=float, default=0.20)
    parser.add_argument("--global-parent-prob", type=float, default=0.40)
    parser.add_argument("--global-top-k", type=int, default=64)
    parser.add_argument("--share-sync-every", type=int, default=4)
    parser.add_argument("--min-delta-edge", type=float, default=0.005)
    parser.add_argument("--holdout-max-drop-edge", type=float, default=0.10)
    parser.add_argument("--holdout-max-drop-retail", type=float, default=200.0)
    parser.add_argument("--holdout-max-drop-arb", type=float, default=100.0)
    parser.add_argument("--spawn-stagger-seconds", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "Cursor" / "runs" / f"{utc_stamp()}_worker_scaling_bench")
    out_dir.mkdir(parents=True, exist_ok=True)

    workers_grid = parse_int_csv(args.workers_grid)
    sim_workers_grid = parse_int_csv(args.sim_workers_grid)
    eval_seeds = parse_seed_spec(args.eval_seeds)
    holdout_seeds = parse_seed_spec(args.holdout_seeds)
    base_strategy = str(Path(args.base_strategy))

    results: list[dict[str, Any]] = []
    for workers in workers_grid:
        for sim_workers in sim_workers_grid:
            case = run_case(
                repo_root=repo_root,
                out_dir=out_dir,
                base_strategy=base_strategy,
                workers=workers,
                sim_workers=sim_workers,
                hours=args.hours,
                quick_sims=args.quick_sims,
                refine_sims=args.refine_sims,
                refine_every=args.refine_every,
                eval_seeds=eval_seeds,
                holdout_seeds=holdout_seeds,
                seed_base=args.seed,
                mutable_constants=args.mutable_constants,
                step_pct=args.step_pct,
                max_changes=args.max_changes,
                max_drift=args.max_drift,
                restart_prob=args.restart_prob,
                global_parent_prob=args.global_parent_prob,
                global_top_k=args.global_top_k,
                share_sync_every=args.share_sync_every,
                min_delta_edge=args.min_delta_edge,
                holdout_max_drop_edge=args.holdout_max_drop_edge,
                holdout_max_drop_retail=args.holdout_max_drop_retail,
                holdout_max_drop_arb=args.holdout_max_drop_arb,
                spawn_stagger_seconds=args.spawn_stagger_seconds,
            )
            results.append(case)
            print(
                f"[bench] workers={workers} sim_workers={sim_workers} "
                f"sims_per_min={case['estimated_sims_per_min']} failed={case['failed']}"
            )

    ranked = sorted(results, key=lambda x: float(x["estimated_sims_per_min"]), reverse=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "results": results,
        "ranked": ranked,
        "best": ranked[0] if ranked else None,
    }
    report_path = out_dir / "benchmark_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Report: {report_path}")
    if ranked:
        top = ranked[0]
        print(
            f"Best config workers={top['workers']} sim_workers={top['sim_workers']} "
            f"sims_per_min={top['estimated_sims_per_min']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
