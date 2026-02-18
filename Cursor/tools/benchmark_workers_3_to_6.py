#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_case(repo_root: Path, workers: int) -> dict:
    out_dir = repo_root / "Cursor" / "runs" / f"{ts()}_bench_w{workers}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "workers").mkdir(parents=True, exist_ok=True)
    base_src = repo_root / "Cursor" / "strategies" / "current" / "theo1_current.sol"
    (out_dir / "base.sol").write_text(base_src.read_text(encoding="utf-8"), encoding="utf-8")

    cmd = [
        "py",
        "-3.10",
        str(repo_root / "Cursor" / "tools" / "optimize_theo1_local_parallel.py"),
        "--mode",
        "launch",
        "--base-strategy",
        str(out_dir / "base.sol"),
        "--out-dir",
        str(out_dir),
        "--hours",
        "0.03",
        "--workers",
        str(workers),
        "--max-safe-workers",
        "6",
        "--spawn-stagger-seconds",
        "4",
        "--sim-workers",
        "1",
        "--seed",
        "20260218",
        "--eval-seeds",
        "42000,42001",
        "--holdout-seeds",
        "43000",
        "--quick-sims",
        "4",
        "--refine-sims",
        "8",
        "--refine-every",
        "2",
        "--step-pct",
        "0.02",
        "--max-changes",
        "2",
        "--max-drift",
        "2.0",
        "--restart-prob",
        "0.1",
        "--global-parent-prob",
        "0.3",
        "--global-top-k",
        "16",
        "--share-sync-every",
        "2",
        "--min-delta-edge",
        "0.001",
        "--holdout-max-drop-edge",
        "6.0",
        "--holdout-max-drop-retail",
        "1500",
        "--holdout-max-drop-arb",
        "800",
        "--mutable-constants",
        "SIGMA_DECAY,STALE_DIR_COEF,PHAT_ALPHA_RETAIL,ELAPSED_CAP",
    ]

    subprocess.run(cmd, cwd=repo_root, check=False)
    deadline = time.time() + 150
    first_progress_at = None
    while time.time() < deadline:
        progress_files = list((out_dir / "workers").glob("worker_*/progress.jsonl"))
        if any(p.exists() and p.stat().st_size > 0 for p in progress_files):
            first_progress_at = time.time()
            break
        time.sleep(2)

    return {
        "workers": workers,
        "out_dir": str(out_dir),
        "first_progress_observed": first_progress_at is not None,
        "waited_seconds": None if first_progress_at is None else round(first_progress_at - (deadline - 150), 2),
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    os.chdir(repo_root)
    results = [run_case(repo_root, w) for w in (3, 4, 5, 6)]
    report = {"timestamp": ts(), "results": results}
    report_path = repo_root / "Cursor" / "logs" / f"benchmark_workers_3_6_{ts()}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote: {report_path}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

