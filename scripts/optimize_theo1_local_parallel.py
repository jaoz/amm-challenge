#!/usr/bin/env python3
"""Local optimizer for Strat/theo1.sol.

Goals:
- Optimize for robust cross-seed performance (mean edge with variance/fee penalties).
- Start from the exact base strategy source as incumbent.
- Use small local parameter changes to avoid large jumps.
- Allow gradual drift from base through many small steps.

Modes:
- launch: start N worker processes
- worker: run one worker search loop
- collect: merge best worker output
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
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
class ConstantSpec:
    name: str
    kind: str
    base_value: int
    lower_bound: int
    upper_bound: int


CONST_LINE_RE = re.compile(r"uint256\s+constant\s+([A-Z0-9_]+)\s*=\s*([^;]+);")
WAD_INT = 10**18
BPS_INT = 10**14


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local optimizer for Strat/theo1.sol")
    parser.add_argument("--mode", choices=["launch", "worker", "collect"], default="launch")
    parser.add_argument("--base-strategy", default="Strat/theo1.sol")
    parser.add_argument("--hours", type=float, default=1.0, help="Hard runtime cap in hours.")
    parser.add_argument("--iterations", type=int, default=0, help="Total quick eval budget (0 = time-only).")
    parser.add_argument("--worker-iterations", type=int, default=0, help="Per-worker quick eval budget.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--quick-sims", type=int, default=50)
    parser.add_argument("--refine-sims", type=int, default=100)
    parser.add_argument("--refine-every", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260215)
    parser.add_argument(
        "--eval-seed",
        type=int,
        default=-1,
        help="Shared evaluation seed for base/quick/refine scoring (default: same as launch seed).",
    )
    parser.add_argument(
        "--eval-seeds",
        default="",
        help="Comma-separated train seeds for robust scoring. Overrides --eval-seed when set.",
    )
    parser.add_argument(
        "--holdout-seeds",
        default="",
        help="Comma-separated holdout seeds used as promotion guardrails (not optimized directly).",
    )
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument(
        "--mutable-constants",
        default="",
        help="Comma-separated constant names to mutate. Empty = all parseable constants.",
    )
    parser.add_argument(
        "--step-pct",
        type=float,
        default=0.05,
        help="Max per-mutation relative step size around current value (e.g., 0.05 = 5%%).",
    )
    parser.add_argument(
        "--bps-step-scale",
        type=float,
        default=0.15,
        help="Additional multiplier for step size of constants defined as `* BPS`.",
    )
    parser.add_argument(
        "--max-changes",
        type=int,
        default=2,
        help="Max constants mutated per candidate.",
    )
    parser.add_argument(
        "--max-drift",
        type=float,
        default=2.0,
        help="Max multiplicative drift from base value (applied as [base/max_drift, base*max_drift]).",
    )
    parser.add_argument(
        "--restart-prob",
        type=float,
        default=0.20,
        help="Probability of mutating from base params instead of a top quick parent.",
    )
    parser.add_argument(
        "--global-parent-prob",
        type=float,
        default=0.30,
        help="Probability of picking parent from cross-worker shared elite pool.",
    )
    parser.add_argument(
        "--global-top-k",
        type=int,
        default=64,
        help="Max shared elite candidates kept/loaded across workers.",
    )
    parser.add_argument(
        "--share-sync-every",
        type=int,
        default=8,
        help="Quick-iteration interval for publishing/loading shared elites.",
    )
    parser.add_argument(
        "--min-delta-edge",
        type=float,
        default=0.0,
        help="Minimum refined objective-score increase required for promotion.",
    )
    parser.add_argument(
        "--holdout-max-drop-edge",
        type=float,
        default=0.10,
        help="Max allowed holdout avg_edge drop versus incumbent when promoting.",
    )
    parser.add_argument(
        "--holdout-max-drop-retail",
        type=float,
        default=200.0,
        help="Max allowed holdout retail_volume_y drop versus incumbent when promoting.",
    )
    parser.add_argument(
        "--holdout-max-drop-arb",
        type=float,
        default=100.0,
        help="Max allowed holdout arb_volume_y drop versus incumbent when promoting.",
    )
    parser.add_argument("--out-dir", default="")
    return parser.parse_args()


def parse_seed_csv(raw: str) -> list[int]:
    seeds: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value not in seen:
            seen.add(value)
            seeds.append(value)
    return seeds


def resolve_seed_sets(args: argparse.Namespace) -> tuple[list[int], list[int]]:
    if args.eval_seeds.strip():
        train_seeds = parse_seed_csv(args.eval_seeds)
    else:
        shared_eval_seed = args.eval_seed if args.eval_seed >= 0 else args.seed
        train_seeds = [shared_eval_seed]
    holdout_seeds = parse_seed_csv(args.holdout_seeds) if args.holdout_seeds.strip() else []
    return train_seeds, holdout_seeds


def _distribute_iterations(total: int, workers: int) -> list[int]:
    if total <= 0:
        return [0] * workers
    base = total // workers
    rem = total % workers
    return [base + (1 if i < rem else 0) for i in range(workers)]


def _expr_to_kind_and_value(expr: str) -> tuple[str, int] | None:
    s = expr.strip()
    m = re.fullmatch(r"(\d+)", s)
    if m:
        return "int", int(m.group(1))
    m = re.fullmatch(r"(\d+)\s*\*\s*BPS", s)
    if m:
        return "bps_value", int(m.group(1)) * BPS_INT
    m = re.fullmatch(r"(\d+)\s*\*\s*WAD", s)
    if m:
        # Optimize in value-space, not multiplier-space.
        return "wad_value", int(m.group(1)) * WAD_INT
    m = re.fullmatch(r"WAD\s*/\s*(\d+)", s)
    if m:
        # Optimize in value-space, not denominator-space.
        d = int(m.group(1))
        if d <= 0:
            return None
        return "wad_value", WAD_INT // d
    m = re.fullmatch(r"(\d+)e(\d+)", s)
    if m:
        return f"sci:{m.group(2)}", int(m.group(1))
    return None


def _render_expr(kind: str, value: int) -> str:
    if kind == "int":
        return str(value)
    if kind == "bps_value":
        return str(value)
    if kind == "wad_mult":
        return f"{value} * WAD"
    if kind == "wad_value":
        return str(value)
    if kind.startswith("sci:"):
        exp = kind.split(":", 1)[1]
        return f"{value}e{exp}"
    raise ValueError(f"Unknown kind {kind}")


def _default_bounds(name: str, kind: str, base_value: int) -> tuple[int, int]:
    if kind == "wad_div":
        lo = max(2, int(base_value * 0.4))
        hi = max(lo + 1, int(base_value * 2.5))
    else:
        lo = max(1, int(base_value * 0.4))
        hi = max(lo + 1, int(base_value * 2.5))

    if name in {"DIR_IMPACT_MULT", "STEP_COUNT_CAP", "ELAPSED_CAP"}:
        lo = max(1, int(base_value * 0.5))
        hi = max(lo + 1, int(base_value * 2.0))

    # Keep classic EWMA decay/alpha constants in sane fixed-point range.
    if ("DECAY" in name or "ALPHA" in name) and kind in {"int", "sci:16"}:
        hi = min(hi, 10**18)

    return lo, hi


def parse_constant_specs(source: str) -> dict[str, ConstantSpec]:
    specs: dict[str, ConstantSpec] = {}
    for name, expr in CONST_LINE_RE.findall(source):
        parsed = _expr_to_kind_and_value(expr.strip())
        if parsed is None:
            continue
        kind, base_value = parsed
        lo, hi = _default_bounds(name, kind, base_value)
        specs[name] = ConstantSpec(
            name=name,
            kind=kind,
            base_value=base_value,
            lower_bound=lo,
            upper_bound=hi,
        )
    return specs


def parse_mutable_constants(raw: str, specs: dict[str, ConstantSpec]) -> list[str]:
    if not raw.strip():
        return sorted(specs.keys())
    names: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        if name not in specs:
            raise ValueError(f"Unknown or unsupported constant '{name}'")
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _replace_constant_expr(source: str, name: str, rendered_expr: str) -> str:
    pattern = re.compile(rf"(uint256\s+constant\s+{name}\s*=\s*)([^;]+)(;)")
    updated, count = pattern.subn(lambda m: f"{m.group(1)}{rendered_expr}{m.group(3)}", source, count=1)
    if count != 1:
        raise ValueError(f"Failed to replace constant '{name}'")
    return updated


def apply_params_to_source(base_source: str, specs: dict[str, ConstantSpec], params: dict[str, int], name: str) -> str:
    src = base_source
    for key, value in params.items():
        spec = specs[key]
        src = _replace_constant_expr(src, key, _render_expr(spec.kind, value))

    # Keep worker variants identifiable.
    src = re.sub(r'return\s+"[^"]+";', f'return "{name}";', src, count=1)
    return src


def _build_change_name(
    prefix: str,
    params: dict[str, int],
    base_params: dict[str, int],
    mutable_constants: list[str],
    *,
    max_terms: int = 3,
) -> str:
    changes: list[tuple[float, str, float]] = []
    for key in mutable_constants:
        cur = int(params[key])
        base_v = int(base_params[key])
        if cur == base_v:
            continue
        if base_v == 0:
            pct = 0.0 if cur == 0 else 999.0
        else:
            pct = ((cur - base_v) / base_v) * 100.0
        changes.append((abs(pct), key, pct))

    if not changes:
        core = "SEED"
    else:
        changes.sort(key=lambda x: (-x[0], x[1]))
        parts: list[str] = []
        for _, key, pct in changes[:max_terms]:
            direction = "UP" if pct > 0 else "DN"
            mag = max(1, int(round(abs(pct))))
            parts.append(f"{key}_{direction}{mag}")
        core = f"D{len(changes)}_" + "_".join(parts)

    digest_src = ";".join(f"{k}:{int(params[k])}" for k in sorted(mutable_constants))
    sig = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}_{core}_{sig}"


def enforce_constraints(candidate: dict[str, int], specs: dict[str, ConstantSpec]) -> dict[str, int]:
    out = dict(candidate)
    # Hard bounds.
    for key, spec in specs.items():
        v = int(out[key])
        v = max(spec.lower_bound, min(spec.upper_bound, v))
        out[key] = v

    # Domain relationship: SHIELD_BUFFER <= SHIELD_TRIGGER in value-space.
    if "SHIELD_TRIGGER" in out and "SHIELD_BUFFER" in out:
        trigger = out["SHIELD_TRIGGER"]
        buffer = out["SHIELD_BUFFER"]
        if buffer > trigger:
            out["SHIELD_BUFFER"] = trigger

    # Keep slope ordering sensible.
    if "TAIL_SLOPE_PROTECT" in out and "TAIL_SLOPE_ATTRACT" in out:
        if out["TAIL_SLOPE_PROTECT"] > out["TAIL_SLOPE_ATTRACT"]:
            out["TAIL_SLOPE_PROTECT"] = out["TAIL_SLOPE_ATTRACT"]

    # Usually retail alpha should not exceed main alpha.
    if "PHAT_ALPHA" in out and "PHAT_ALPHA_RETAIL" in out:
        if out["PHAT_ALPHA_RETAIL"] > out["PHAT_ALPHA"]:
            out["PHAT_ALPHA_RETAIL"] = out["PHAT_ALPHA"]

    return out


def mutate_local(
    rng: random.Random,
    parent: dict[str, int],
    base: dict[str, int],
    specs: dict[str, ConstantSpec],
    mutable_constants: list[str],
    *,
    max_changes: int,
    step_pct: float,
    bps_step_scale: float,
    max_drift: float,
) -> dict[str, int]:
    child = dict(parent)
    if not mutable_constants:
        return child

    n_mut = rng.randint(1, max(1, min(max_changes, len(mutable_constants))))
    chosen = rng.sample(mutable_constants, n_mut)
    for key in chosen:
        spec = specs[key]
        cur = int(child[key])
        base_v = int(base[key])

        step = max(1, int(max(1, cur) * step_pct))
        if spec.kind == "bps_value":
            step = max(1, int(step * max(0.01, bps_step_scale)))
        delta = rng.randint(-step, step)
        if delta == 0:
            delta = 1 if rng.random() < 0.5 else -1
        nxt = cur + delta

        drift_lo = max(spec.lower_bound, int(base_v / max_drift))
        drift_hi = min(spec.upper_bound, int(base_v * max_drift))
        if drift_hi < drift_lo:
            drift_hi = drift_lo
        nxt = max(drift_lo, min(drift_hi, nxt))
        child[key] = nxt

    return enforce_constraints(child, specs)


def _evaluate_for_seed(
    source: str,
    n_simulations: int,
    compiler: SolidityCompiler,
    baseline: EVMStrategyAdapter,
    n_workers: int,
    eval_seed: int,
) -> dict[str, float] | None:
    compilation = compiler.compile(source)
    if not compilation.success:
        return None

    user = EVMStrategyAdapter(bytecode=compilation.bytecode, abi=compilation.abi)
    runner = MatchRunner(
        n_simulations=n_simulations,
        # MatchRunner sets per-simulation seeds internally from seed_offset+i.
        # Keep config.seed unset to avoid implying it participates in diversity.
        config=build_base_config(seed=None),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
        # MatchRunner derives per-sim seeds from seed_offset+i.
        # Use eval_seed as the offset basis so different eval seeds
        # truly evaluate different simulation paths.
        seed_offset=eval_seed * 1_000_000,
    )
    try:
        result = runner.run_match(user, baseline, store_results=True)
    except Exception:
        return None

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


def evaluate(
    source: str,
    n_simulations: int,
    compiler: SolidityCompiler,
    baseline: EVMStrategyAdapter,
    n_workers: int,
    eval_seeds: list[int],
) -> dict[str, Any] | None:
    if not eval_seeds:
        return None

    per_seed: list[dict[str, Any]] = []
    for seed in eval_seeds:
        metrics = _evaluate_for_seed(source, n_simulations, compiler, baseline, n_workers, seed)
        if metrics is None:
            return None
        rec = dict(metrics)
        rec["seed"] = seed
        per_seed.append(rec)

    agg: dict[str, Any] = {
        "avg_edge": mean(float(x["avg_edge"]) for x in per_seed),
        "min_edge": mean(float(x["min_edge"]) for x in per_seed),
        "p05_edge": mean(float(x["p05_edge"]) for x in per_seed),
        "p10_edge": mean(float(x["p10_edge"]) for x in per_seed),
        "median_edge": mean(float(x["median_edge"]) for x in per_seed),
        "max_edge": mean(float(x["max_edge"]) for x in per_seed),
        "avg_fee_bps": mean(float(x["avg_fee_bps"]) for x in per_seed),
        "retail_volume_y": mean(float(x["retail_volume_y"]) for x in per_seed),
        "arb_volume_y": mean(float(x["arb_volume_y"]) for x in per_seed),
        "objective_score": mean(float(x["avg_edge"]) for x in per_seed),
        "n_eval_seeds": len(per_seed),
        "eval_seeds": list(eval_seeds),
        "per_seed": per_seed,
    }
    return agg


def write_json_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def _build_shared_elites(
    quick: list[dict[str, Any]],
    refined: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}

    for r in sorted(refined, key=lambda x: float(x["refine_score"]), reverse=True)[: max(1, top_k)]:
        rec = {
            "name": str(r["name"]),
            "params": r["params"],
            "score": float(r["refine_score"]),
            "quality": "refine",
            "timestamp": float(r.get("timestamp", time.time())),
        }
        by_name[rec["name"]] = rec

    quick_cap = max(8, top_k // 4)
    for q in sorted(quick, key=lambda x: float(x["quick_score"]), reverse=True)[:quick_cap]:
        name = str(q["name"])
        if name in by_name:
            continue
        rec = {
            "name": name,
            "params": q["params"],
            "score": float(q["quick_score"]),
            "quality": "quick",
            "timestamp": float(q.get("timestamp", time.time())),
        }
        by_name[name] = rec

    return sorted(by_name.values(), key=lambda x: float(x["score"]), reverse=True)[: max(1, top_k)]


def write_worker_shared_snapshot(
    shared_dir: Path,
    *,
    worker_id: int,
    quick: list[dict[str, Any]],
    refined: list[dict[str, Any]],
    top_k: int,
) -> None:
    shared_dir.mkdir(parents=True, exist_ok=True)
    path = shared_dir / f"worker_{worker_id}.json"
    tmp = shared_dir / f"worker_{worker_id}.tmp"
    payload = {
        "worker_id": worker_id,
        "updated_at": time.time(),
        "elites": _build_shared_elites(quick, refined, top_k=top_k),
    }
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_global_elites(shared_dir: Path, *, self_worker_id: int, top_k: int) -> list[dict[str, Any]]:
    if not shared_dir.exists():
        return []
    by_name: dict[str, dict[str, Any]] = {}
    for path in shared_dir.glob("worker_*.json"):
        m = re.fullmatch(r"worker_(\d+)\.json", path.name)
        if m and int(m.group(1)) == self_worker_id:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rec in data.get("elites", []):
            try:
                name = str(rec["name"])
                score = float(rec["score"])
                params = rec["params"]
            except Exception:
                continue
            old = by_name.get(name)
            if old is None or score > float(old["score"]):
                by_name[name] = {
                    "name": name,
                    "params": params,
                    "score": score,
                    "quality": str(rec.get("quality", "quick")),
                    "timestamp": float(rec.get("timestamp", time.time())),
                }
    return sorted(by_name.values(), key=lambda x: float(x["score"]), reverse=True)[: max(1, top_k)]


def worker_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = args.worker_id
    rng = random.Random(args.seed + wid * 1_000_003)

    worker_dir = out_dir / "workers" / f"worker_{wid}"
    worker_dir.mkdir(parents=True, exist_ok=True)

    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    specs = parse_constant_specs(base_source)
    if not specs:
        raise ValueError("No parseable constants found in base strategy")
    mutable_constants = parse_mutable_constants(args.mutable_constants, specs)

    base_params = {k: spec.base_value for k, spec in specs.items()}
    base_params = enforce_constraints(base_params, specs)

    progress_path = worker_dir / "progress.jsonl"
    best_json_path = worker_dir / "best.json"
    best_sol_path = worker_dir / "best.sol"
    summary_path = worker_dir / "summary.json"
    shared_dir = out_dir / "shared_pool"

    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    n_workers = resolve_n_workers()
    train_eval_seeds, holdout_eval_seeds = resolve_seed_sets(args)

    iter_budget = args.worker_iterations if args.worker_iterations > 0 else args.iterations
    deadline = time.time() + args.hours * 3600.0

    quick: list[dict[str, Any]] = []
    refined: list[dict[str, Any]] = []
    best_refined: dict[str, Any] | None = None
    attempts = 0
    quick_done = 0
    global_elites: list[dict[str, Any]] = []

    print(
        f"[worker {wid}] start hours={args.hours} quick={args.quick_sims} refine={args.refine_sims} "
        f"objective=mean_edge_over_seeds train={train_eval_seeds} holdout={holdout_eval_seeds} "
        f"mutable={len(mutable_constants)} step_pct={args.step_pct:.4f} max_changes={args.max_changes} "
        f"global_parent_prob={args.global_parent_prob:.2f}",
        flush=True,
    )

    # Seed incumbent from exact base strategy source.
    base_metrics = evaluate(base_source, args.refine_sims, compiler, baseline, n_workers, train_eval_seeds)
    base_holdout_metrics = (
        evaluate(base_source, args.refine_sims, compiler, baseline, n_workers, holdout_eval_seeds)
        if holdout_eval_seeds
        else None
    )
    if base_metrics is not None:
        best_refined = {
            "attempt": 0,
            "iteration": 0,
            "name": "BASE_STRATEGY",
            "params": None,
            "refine_metrics": base_metrics,
            "holdout_metrics": base_holdout_metrics,
            "refine_score": base_metrics["objective_score"],
            "timestamp": time.time(),
            "base_strategy": str(base_path),
        }
        best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
        best_sol_path.write_text(base_source, encoding="utf-8")
        write_json_line(progress_path, {"event": "base_refine_seed", **best_refined})
        refined.append(
            {
                "attempt": 0,
                "iteration": 0,
                "name": "BASE_STRATEGY",
                "params": base_params,
                "refine_metrics": base_metrics,
                "holdout_metrics": base_holdout_metrics,
                "refine_score": base_metrics["objective_score"],
                "timestamp": time.time(),
            }
        )
        write_worker_shared_snapshot(
            shared_dir,
            worker_id=wid,
            quick=quick,
            refined=refined,
            top_k=args.global_top_k,
        )
        holdout_txt = (
            f" holdout_edge={base_holdout_metrics['avg_edge']:.2f}"
            if base_holdout_metrics is not None
            else ""
        )
        print(f"[worker {wid}] base train_edge={base_metrics['avg_edge']:.2f}{holdout_txt}", flush=True)
    else:
        print(f"[worker {wid}] warning: base strategy compile/eval failed; continuing without base incumbent", flush=True)

    while time.time() < deadline and (iter_budget <= 0 or quick_done < iter_budget):
        attempts += 1
        if quick_done % max(1, args.share_sync_every) == 0:
            global_elites = load_global_elites(shared_dir, self_worker_id=wid, top_k=args.global_top_k)

        if global_elites and rng.random() < args.global_parent_prob:
            parent = global_elites[rng.randrange(len(global_elites))]["params"]
        elif not quick or rng.random() < args.restart_prob:
            parent = base_params
        else:
            top_pool = sorted(quick, key=lambda x: x["quick_score"], reverse=True)[: max(8, min(48, len(quick)))]
            parent = top_pool[rng.randrange(len(top_pool))]["params"]

        params = mutate_local(
            rng,
            parent,
            base_params,
            specs,
            mutable_constants,
            max_changes=args.max_changes,
            step_pct=args.step_pct,
            bps_step_scale=args.bps_step_scale,
            max_drift=args.max_drift,
        )
        name = _build_change_name("Theo1Local", params, base_params, mutable_constants)
        source = apply_params_to_source(base_source, specs, params, name)
        q_metrics = evaluate(source, args.quick_sims, compiler, baseline, n_workers, train_eval_seeds)
        if q_metrics is None:
            write_json_line(
                progress_path,
                {
                    "event": "quick_compile_or_eval_fail",
                    "attempt": attempts,
                    "iteration": quick_done + 1,
                    "name": name,
                    "params": params,
                },
            )
            continue

        quick_done += 1
        quick_score = q_metrics["objective_score"]
        qrec = {
            "attempt": attempts,
            "iteration": quick_done,
            "name": name,
            "params": params,
            "quick_metrics": q_metrics,
            "quick_score": quick_score,
            "timestamp": time.time(),
        }
        quick.append(qrec)
        write_json_line(progress_path, {"event": "quick", **qrec})
        if quick_done % max(1, args.share_sync_every) == 0:
            write_worker_shared_snapshot(
                shared_dir,
                worker_id=wid,
                quick=quick,
                refined=refined,
                top_k=args.global_top_k,
            )

        best_quick = max(quick, key=lambda x: x["quick_score"])
        print(
            f"[worker {wid}] it={quick_done}{'' if iter_budget <= 0 else '/' + str(iter_budget)} "
            f"quick score={quick_score:.2f} edge={q_metrics['avg_edge']:.2f} p10={q_metrics['p10_edge']:.2f} fee={q_metrics['avg_fee_bps']:.2f} "
            f"best={best_quick['name']}:{best_quick['quick_score']:.2f}",
            flush=True,
        )

        if quick_done % max(1, args.refine_every) != 0:
            continue

        refined_names = {r["name"] for r in refined}
        pool = [r for r in sorted(quick, key=lambda x: x["quick_score"], reverse=True) if r["name"] not in refined_names]
        if not pool:
            continue

        pick = pool[0]
        r_source = apply_params_to_source(base_source, specs, pick["params"], pick["name"] + "_refined")
        r_metrics = evaluate(r_source, args.refine_sims, compiler, baseline, n_workers, train_eval_seeds)
        if r_metrics is None:
            write_json_line(
                progress_path,
                {
                    "event": "refine_compile_or_eval_fail",
                    "attempt": attempts,
                    "iteration": quick_done,
                    "name": pick["name"],
                    "params": pick["params"],
                },
            )
            continue

        r_holdout_metrics = (
            evaluate(r_source, args.refine_sims, compiler, baseline, n_workers, holdout_eval_seeds)
            if holdout_eval_seeds
            else None
        )
        if holdout_eval_seeds and r_holdout_metrics is None:
            write_json_line(
                progress_path,
                {
                    "event": "refine_holdout_eval_fail",
                    "attempt": attempts,
                    "iteration": quick_done,
                    "name": pick["name"],
                    "params": pick["params"],
                },
            )
            continue

        r_score = r_metrics["objective_score"]
        rrec = {
            "attempt": attempts,
            "iteration": quick_done,
            "name": pick["name"],
            "params": pick["params"],
            "refine_metrics": r_metrics,
            "holdout_metrics": r_holdout_metrics,
            "refine_score": r_score,
            "timestamp": time.time(),
        }
        refined.append(rrec)
        write_json_line(progress_path, {"event": "refine", **rrec})
        write_worker_shared_snapshot(
            shared_dir,
            worker_id=wid,
            quick=quick,
            refined=refined,
            top_k=args.global_top_k,
        )
        holdout_txt = f" holdout_edge={r_holdout_metrics['avg_edge']:.2f}" if r_holdout_metrics is not None else ""
        print(
            f"[worker {wid}] it={quick_done}{'' if iter_budget <= 0 else '/' + str(iter_budget)} "
            f"REFINE score={r_score:.2f} train_edge={r_metrics['avg_edge']:.2f}{holdout_txt} "
            f"p10={r_metrics['p10_edge']:.2f} fee={r_metrics['avg_fee_bps']:.2f}",
            flush=True,
        )

        incumbent_score = best_refined["refine_score"] if best_refined else float("-inf")
        delta = r_score - incumbent_score
        holdout_ok = True
        holdout_reasons: list[str] = []
        if holdout_eval_seeds and best_refined is not None and r_holdout_metrics is not None:
            incumbent_holdout = best_refined.get("holdout_metrics")
            if incumbent_holdout is not None:
                if r_holdout_metrics["avg_edge"] < incumbent_holdout["avg_edge"] - args.holdout_max_drop_edge:
                    holdout_ok = False
                    holdout_reasons.append("holdout_avg_edge_drop")
                if r_holdout_metrics["retail_volume_y"] < incumbent_holdout["retail_volume_y"] - args.holdout_max_drop_retail:
                    holdout_ok = False
                    holdout_reasons.append("holdout_retail_drop")
                if r_holdout_metrics["arb_volume_y"] < incumbent_holdout["arb_volume_y"] - args.holdout_max_drop_arb:
                    holdout_ok = False
                    holdout_reasons.append("holdout_arb_drop")

        if (best_refined is None or delta > args.min_delta_edge) and holdout_ok:
            best_refined = rrec
            best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
            best_name = _build_change_name("Theo1LocalBest", pick["params"], base_params, mutable_constants)
            best_sol_path.write_text(
                apply_params_to_source(base_source, specs, pick["params"], best_name),
                encoding="utf-8",
            )
            print(
                f"[worker {wid}] NEW BEST delta={delta:.4f} score={r_score:.2f} edge={r_metrics['avg_edge']:.2f}",
                flush=True,
            )
        else:
            write_json_line(
                progress_path,
                {
                    "event": "refine_not_better",
                    "attempt": attempts,
                    "iteration": quick_done,
                    "name": pick["name"],
                    "candidate_score": r_score,
                    "incumbent_score": incumbent_score,
                    "delta_score": delta,
                    "min_delta_edge": args.min_delta_edge,
                    "holdout_ok": holdout_ok,
                    "holdout_reasons": holdout_reasons,
                },
            )
            print(
                f"[worker {wid}] REFINE rejected delta={delta:.4f} required>{args.min_delta_edge:.4f}"
                f"{'' if holdout_ok else ' holdout=' + ','.join(holdout_reasons)}",
                flush=True,
            )

    summary = {
        "worker_id": wid,
        "attempts": attempts,
        "iterations_budget": iter_budget,
        "iterations_executed": quick_done,
        "quick_evals": len(quick),
        "refine_evals": len(refined),
        "best_refined": best_refined,
        "base_strategy": str(base_path),
        "mutable_constants": mutable_constants,
        "step_pct": args.step_pct,
        "max_changes": args.max_changes,
        "max_drift": args.max_drift,
        "bps_step_scale": args.bps_step_scale,
        "restart_prob": args.restart_prob,
        "global_parent_prob": args.global_parent_prob,
        "global_top_k": args.global_top_k,
        "share_sync_every": args.share_sync_every,
        "min_delta_edge": args.min_delta_edge,
        "hours_cap": args.hours,
        "eval_seeds": train_eval_seeds,
        "holdout_seeds": holdout_eval_seeds,
        "holdout_max_drop_edge": args.holdout_max_drop_edge,
        "holdout_max_drop_retail": args.holdout_max_drop_retail,
        "holdout_max_drop_arb": args.holdout_max_drop_arb,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_worker_shared_snapshot(
        shared_dir,
        worker_id=wid,
        quick=quick,
        refined=refined,
        top_k=args.global_top_k,
    )
    write_json_line(progress_path, {"event": "complete", **summary})
    print(f"[worker {wid}] complete", flush=True)
    return 0


def launch_main(args: argparse.Namespace) -> int:
    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    specs = parse_constant_specs(base_source)
    mutable_constants = parse_mutable_constants(args.mutable_constants, specs)

    out_dir = Path(args.out_dir) if args.out_dir else Path("Strat") / (
        "theo1_local_search_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    train_eval_seeds, holdout_eval_seeds = resolve_seed_sets(args)
    shared_eval_seed = train_eval_seeds[0]

    worker_iterations = _distribute_iterations(args.iterations, args.workers)
    script = Path(__file__).resolve()
    procs: list[dict[str, Any]] = []
    for wid in range(args.workers):
        worker_dir = out_dir / "workers" / f"worker_{wid}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = worker_dir / "stdout.log"
        stderr_path = worker_dir / "stderr.log"
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
            "--iterations",
            str(args.iterations),
            "--worker-iterations",
            str(worker_iterations[wid]),
            "--quick-sims",
            str(args.quick_sims),
            "--refine-sims",
            str(args.refine_sims),
            "--refine-every",
            str(args.refine_every),
            "--seed",
            str(args.seed + wid * 100_003),
            "--eval-seed",
            str(shared_eval_seed),
            "--eval-seeds",
            ",".join(str(x) for x in train_eval_seeds),
            "--holdout-seeds",
            ",".join(str(x) for x in holdout_eval_seeds),
            "--mutable-constants",
            args.mutable_constants,
            "--step-pct",
            str(args.step_pct),
            "--max-changes",
            str(args.max_changes),
            "--max-drift",
            str(args.max_drift),
            "--bps-step-scale",
            str(args.bps_step_scale),
            "--restart-prob",
            str(args.restart_prob),
            "--global-parent-prob",
            str(args.global_parent_prob),
            "--global-top-k",
            str(args.global_top_k),
            "--share-sync-every",
            str(args.share_sync_every),
            "--min-delta-edge",
            str(args.min_delta_edge),
            "--holdout-max-drop-edge",
            str(args.holdout_max_drop_edge),
            "--holdout-max-drop-retail",
            str(args.holdout_max_drop_retail),
            "--holdout-max-drop-arb",
            str(args.holdout_max_drop_arb),
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
                "iterations": worker_iterations[wid],
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "command": cmd,
            }
        )

    manifest = {
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "base_strategy": str(base_path),
        "out_dir": str(out_dir),
        "workers": args.workers,
        "hours_cap": args.hours,
        "iterations_total": args.iterations,
        "iterations_per_worker": worker_iterations,
        "quick_sims": args.quick_sims,
        "refine_sims": args.refine_sims,
        "refine_every": args.refine_every,
        "objective": "mean_edge_over_eval_seeds",
        "seed": args.seed,
        "eval_seed": shared_eval_seed,
        "eval_seeds": train_eval_seeds,
        "holdout_seeds": holdout_eval_seeds,
        "mutable_constants": mutable_constants,
        "n_parseable_constants": len(specs),
        "step_pct": args.step_pct,
        "max_changes": args.max_changes,
        "max_drift": args.max_drift,
        "bps_step_scale": args.bps_step_scale,
        "restart_prob": args.restart_prob,
        "global_parent_prob": args.global_parent_prob,
        "global_top_k": args.global_top_k,
        "share_sync_every": args.share_sync_every,
        "min_delta_edge": args.min_delta_edge,
        "holdout_max_drop_edge": args.holdout_max_drop_edge,
        "holdout_max_drop_retail": args.holdout_max_drop_retail,
        "holdout_max_drop_arb": args.holdout_max_drop_arb,
        "algorithm": "local mutation around base/current with bounded drift",
        "processes": procs,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Manifest: {manifest_path}")
    return 0


def collect_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    bests: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(out_dir.glob("**/best.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bests.append((path, data))
        except Exception:
            continue
    if not bests:
        print(f"No worker best files found in {out_dir}")
        return 1

    def score(entry: tuple[Path, dict[str, Any]]) -> float:
        data = entry[1]
        if "refine_score" in data:
            return float(data["refine_score"])
        return float(data.get("refine_metrics", {}).get("avg_edge", float("-inf")))

    best_path, best_data = max(bests, key=score)
    worker_sol = best_path.with_name("best.sol")
    merged_json = out_dir / "best_overall.json"
    merged_sol = out_dir / "best_overall.sol"
    merged_json.write_text(json.dumps(best_data, indent=2, sort_keys=True), encoding="utf-8")
    if worker_sol.exists():
        merged_sol.write_text(worker_sol.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"Best worker: {best_path.parent.name}")
    if "refine_score" in best_data:
        print(f"Score: {best_data.get('refine_score')}")
    print(f"Edge: {best_data.get('refine_metrics', {}).get('avg_edge')}")
    if best_data.get("holdout_metrics"):
        print(f"Holdout edge: {best_data.get('holdout_metrics', {}).get('avg_edge')}")
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
