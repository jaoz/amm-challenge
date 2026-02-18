#!/usr/bin/env python3
"""CMA-ES optimizer for Strat/theo1.sol.

Modes:
- launch: start N worker processes
- worker: run one worker CMA-ES loop
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
    parser = argparse.ArgumentParser(description="CMA-ES optimizer for Strat/theo1.sol")
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
        help="Initial per-parameter std as pct of base value.",
    )
    parser.add_argument(
        "--bps-step-scale",
        type=float,
        default=0.15,
        help="Additional multiplier for initial std of constants defined as `* BPS`.",
    )
    parser.add_argument(
        "--max-drift",
        type=float,
        default=2.0,
        help="Max multiplicative drift from base value (applied as [base/max_drift, base*max_drift]).",
    )
    parser.add_argument(
        "--min-delta-edge",
        type=float,
        default=0.0,
        help="Minimum refined avg_edge increase required for promotion.",
    )
    parser.add_argument("--cma-popsize", type=int, default=0, help="Population size per generation (0 = auto).")
    parser.add_argument("--cma-mu", type=int, default=0, help="Elite count per generation (0 = popsize//2).")
    parser.add_argument("--cma-sigma", type=float, default=0.7, help="Global CMA sigma multiplier.")
    parser.add_argument("--cma-sigma-min", type=float, default=0.05, help="Lower clamp for sigma.")
    parser.add_argument("--cma-sigma-max", type=float, default=2.5, help="Upper clamp for sigma.")
    parser.add_argument("--out-dir", default="")
    return parser.parse_args()


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
    for key, spec in specs.items():
        v = int(out[key])
        v = max(spec.lower_bound, min(spec.upper_bound, v))
        out[key] = v

    if "SHIELD_TRIGGER" in out and "SHIELD_BUFFER" in out:
        trigger = out["SHIELD_TRIGGER"]
        buffer = out["SHIELD_BUFFER"]
        if buffer > trigger:
            out["SHIELD_BUFFER"] = trigger

    if "TAIL_SLOPE_PROTECT" in out and "TAIL_SLOPE_ATTRACT" in out:
        if out["TAIL_SLOPE_PROTECT"] > out["TAIL_SLOPE_ATTRACT"]:
            out["TAIL_SLOPE_PROTECT"] = out["TAIL_SLOPE_ATTRACT"]

    if "PHAT_ALPHA" in out and "PHAT_ALPHA_RETAIL" in out:
        if out["PHAT_ALPHA_RETAIL"] > out["PHAT_ALPHA"]:
            out["PHAT_ALPHA_RETAIL"] = out["PHAT_ALPHA"]

    return out


def _drift_bounds(spec: ConstantSpec, base_v: int, max_drift: float) -> tuple[int, int]:
    lo = max(spec.lower_bound, int(base_v / max_drift))
    hi = min(spec.upper_bound, int(base_v * max_drift))
    if hi < lo:
        hi = lo
    return lo, hi


def _vector_to_params(
    vec: list[float],
    *,
    mutable_constants: list[str],
    base_params: dict[str, int],
    specs: dict[str, ConstantSpec],
    max_drift: float,
) -> dict[str, int]:
    out = dict(base_params)
    for i, key in enumerate(mutable_constants):
        spec = specs[key]
        base_v = int(base_params[key])
        lo, hi = _drift_bounds(spec, base_v, max_drift)
        v = int(round(vec[i]))
        out[key] = max(lo, min(hi, v))
    return enforce_constraints(out, specs)


def _params_to_vector(params: dict[str, int], mutable_constants: list[str]) -> list[float]:
    return [float(params[k]) for k in mutable_constants]


def evaluate(
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
        config=build_base_config(seed=eval_seed),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
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


def write_json_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def _cma_weights(mu: int) -> tuple[list[float], float]:
    raw = [math.log(mu + 0.5) - math.log(i + 1) for i in range(mu)]
    s = sum(raw)
    w = [v / s for v in raw]
    mueff = 1.0 / sum(v * v for v in w)
    return w, mueff


def worker_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = args.worker_id
    rng = random.Random(args.seed + wid * 1_000_003)

    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    specs = parse_constant_specs(base_source)
    if not specs:
        raise ValueError("No parseable constants found in base strategy")
    mutable_constants = parse_mutable_constants(args.mutable_constants, specs)
    if not mutable_constants:
        raise ValueError("No mutable constants selected")

    base_params = {k: spec.base_value for k, spec in specs.items()}
    base_params = enforce_constraints(base_params, specs)

    progress_path = out_dir / f"worker_{wid}.progress.jsonl"
    best_json_path = out_dir / f"worker_{wid}.best.json"
    best_sol_path = out_dir / f"worker_{wid}.best.sol"
    summary_path = out_dir / f"worker_{wid}.summary.json"

    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    n_workers = resolve_n_workers()
    shared_eval_seed = args.eval_seed if args.eval_seed >= 0 else args.seed

    iter_budget = args.worker_iterations if args.worker_iterations > 0 else args.iterations
    deadline = time.time() + args.hours * 3600.0

    dim = len(mutable_constants)
    popsize = args.cma_popsize if args.cma_popsize > 0 else max(4, 4 + int(3 * math.log(max(2, dim))))
    mu = args.cma_mu if args.cma_mu > 0 else max(2, popsize // 2)
    mu = min(mu, popsize)
    weights, mueff = _cma_weights(mu)

    cc = (4.0 + mueff / dim) / (dim + 4.0 + 2.0 * mueff / dim)
    cs = (mueff + 2.0) / (dim + mueff + 5.0)
    c1 = 2.0 / (((dim + 1.3) ** 2) + mueff)
    cmu = min(1.0 - c1, 2.0 * (mueff - 2.0 + 1.0 / mueff) / (((dim + 2.0) ** 2) + mueff))
    damps = 1.0 + 2.0 * max(0.0, math.sqrt((mueff - 1.0) / (dim + 1.0)) - 1.0) + cs
    chi_n = math.sqrt(dim) * (1.0 - 1.0 / (4.0 * dim) + 1.0 / (21.0 * dim * dim))

    m = _params_to_vector(base_params, mutable_constants)
    c_diag = []
    for key in mutable_constants:
        b = float(max(1, abs(base_params[key])))
        std0 = max(1.0, b * args.step_pct)
        if specs[key].kind == "bps_value":
            std0 = max(1.0, std0 * max(0.01, args.bps_step_scale))
        c_diag.append(std0 * std0)
    p_sigma = [0.0] * dim
    p_c = [0.0] * dim
    sigma = float(args.cma_sigma)

    quick: list[dict[str, Any]] = []
    refined: list[dict[str, Any]] = []
    best_refined: dict[str, Any] | None = None
    attempts = 0
    quick_done = 0
    generation = 0

    print(
        f"[worker {wid}] start hours={args.hours} quick={args.quick_sims} refine={args.refine_sims} "
        f"objective=avg_edge mutable={dim} pop={popsize} mu={mu} sigma={sigma:.3f}",
        flush=True,
    )

    base_metrics = evaluate(base_source, args.refine_sims, compiler, baseline, n_workers, shared_eval_seed)
    if base_metrics is not None:
        best_refined = {
            "attempt": 0,
            "iteration": 0,
            "name": "BASE_STRATEGY",
            "params": None,
            "refine_metrics": base_metrics,
            "refine_score": base_metrics["avg_edge"],
            "timestamp": time.time(),
            "base_strategy": str(base_path),
        }
        best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
        best_sol_path.write_text(base_source, encoding="utf-8")
        write_json_line(progress_path, {"event": "base_refine_seed", **best_refined})
        print(f"[worker {wid}] base seed edge={base_metrics['avg_edge']:.2f}", flush=True)
    else:
        print(f"[worker {wid}] warning: base strategy compile/eval failed; continuing without base incumbent", flush=True)

    while time.time() < deadline and (iter_budget <= 0 or quick_done < iter_budget):
        generation += 1
        gen_samples: list[dict[str, Any]] = []
        m_old = list(m)
        sigma_old = sigma

        for k in range(popsize):
            if time.time() >= deadline:
                break
            if iter_budget > 0 and quick_done >= iter_budget:
                break

            attempts += 1
            z = [rng.gauss(0.0, 1.0) for _ in range(dim)]
            y = [math.sqrt(max(1e-12, c_diag[i])) * z[i] for i in range(dim)]
            x = [m_old[i] + sigma_old * y[i] for i in range(dim)]

            params = _vector_to_params(
                x,
                mutable_constants=mutable_constants,
                base_params=base_params,
                specs=specs,
                max_drift=args.max_drift,
            )
            x_eff = _params_to_vector(params, mutable_constants)
            y_eff = [(x_eff[i] - m_old[i]) / max(1e-12, sigma_old) for i in range(dim)]

            name = _build_change_name("Theo1CMA", params, base_params, mutable_constants)
            source = apply_params_to_source(base_source, specs, params, name)
            q_metrics = evaluate(source, args.quick_sims, compiler, baseline, n_workers, shared_eval_seed)
            if q_metrics is None:
                write_json_line(
                    progress_path,
                    {
                        "event": "quick_compile_or_eval_fail",
                        "attempt": attempts,
                        "iteration": quick_done + 1,
                        "generation": generation,
                        "name": name,
                        "params": params,
                    },
                )
                continue

            quick_done += 1
            qrec = {
                "attempt": attempts,
                "iteration": quick_done,
                "generation": generation,
                "name": name,
                "params": params,
                "quick_metrics": q_metrics,
                "quick_score": q_metrics["avg_edge"],
                "timestamp": time.time(),
            }
            quick.append(qrec)
            gen_samples.append({"qrec": qrec, "y": y_eff})
            write_json_line(progress_path, {"event": "quick", **qrec})

            best_quick = max(quick, key=lambda x0: x0["quick_score"])
            print(
                f"[worker {wid}] g={generation} it={quick_done}{'' if iter_budget <= 0 else '/' + str(iter_budget)} "
                f"quick edge={q_metrics['avg_edge']:.2f} p10={q_metrics['p10_edge']:.2f} fee={q_metrics['avg_fee_bps']:.2f} "
                f"best={best_quick['name']}:{best_quick['quick_score']:.2f}",
                flush=True,
            )

            if quick_done % max(1, args.refine_every) != 0:
                continue

            refined_names = {r["name"] for r in refined}
            pool = [r for r in sorted(quick, key=lambda x0: x0["quick_score"], reverse=True) if r["name"] not in refined_names]
            if not pool:
                continue

            pick = pool[0]
            r_source = apply_params_to_source(base_source, specs, pick["params"], pick["name"] + "_refined")
            r_metrics = evaluate(r_source, args.refine_sims, compiler, baseline, n_workers, shared_eval_seed)
            if r_metrics is None:
                write_json_line(
                    progress_path,
                    {
                        "event": "refine_compile_or_eval_fail",
                        "attempt": attempts,
                        "iteration": quick_done,
                        "generation": generation,
                        "name": pick["name"],
                        "params": pick["params"],
                    },
                )
                continue

            r_score = r_metrics["avg_edge"]
            rrec = {
                "attempt": attempts,
                "iteration": quick_done,
                "generation": generation,
                "name": pick["name"],
                "params": pick["params"],
                "refine_metrics": r_metrics,
                "refine_score": r_score,
                "timestamp": time.time(),
            }
            refined.append(rrec)
            write_json_line(progress_path, {"event": "refine", **rrec})
            print(
                f"[worker {wid}] g={generation} it={quick_done}{'' if iter_budget <= 0 else '/' + str(iter_budget)} "
                f"REFINE edge={r_metrics['avg_edge']:.2f} p10={r_metrics['p10_edge']:.2f} fee={r_metrics['avg_fee_bps']:.2f}",
                flush=True,
            )

            incumbent_edge = best_refined["refine_metrics"]["avg_edge"] if best_refined else float("-inf")
            delta = r_metrics["avg_edge"] - incumbent_edge
            if best_refined is None or delta > args.min_delta_edge:
                best_refined = rrec
                best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
                best_name = _build_change_name("Theo1CMABest", pick["params"], base_params, mutable_constants)
                best_sol_path.write_text(
                    apply_params_to_source(base_source, specs, pick["params"], best_name),
                    encoding="utf-8",
                )
                print(
                    f"[worker {wid}] NEW BEST delta={delta:.4f} edge={r_metrics['avg_edge']:.2f}",
                    flush=True,
                )
            else:
                write_json_line(
                    progress_path,
                    {
                        "event": "refine_not_better",
                        "attempt": attempts,
                        "iteration": quick_done,
                        "generation": generation,
                        "name": pick["name"],
                        "candidate_avg_edge": r_metrics["avg_edge"],
                        "incumbent_avg_edge": incumbent_edge,
                        "delta_avg_edge": delta,
                        "min_delta_edge": args.min_delta_edge,
                    },
                )
                print(
                    f"[worker {wid}] REFINE rejected delta={delta:.4f} required>{args.min_delta_edge:.4f}",
                    flush=True,
                )

        if not gen_samples:
            continue

        gen_sorted = sorted(gen_samples, key=lambda x0: x0["qrec"]["quick_score"], reverse=True)
        mu_used = min(mu, len(gen_sorted))
        sel = gen_sorted[:mu_used]
        w = weights[:mu_used]
        s_w = sum(w)
        w = [wi / s_w for wi in w]
        mueff_used = 1.0 / sum(wi * wi for wi in w)

        y_w = [0.0] * dim
        for i in range(mu_used):
            yi = sel[i]["y"]
            wi = w[i]
            for j in range(dim):
                y_w[j] += wi * yi[j]

        inv_sqrt_c = [1.0 / math.sqrt(max(1e-12, c_diag[j])) for j in range(dim)]
        for j in range(dim):
            p_sigma[j] = (1.0 - cs) * p_sigma[j] + math.sqrt(cs * (2.0 - cs) * mueff_used) * (y_w[j] * inv_sqrt_c[j])

        norm_ps = math.sqrt(sum(v * v for v in p_sigma))
        left = norm_ps / math.sqrt(1.0 - (1.0 - cs) ** (2.0 * generation))
        hsig = 1.0 if (left / chi_n) < (1.4 + 2.0 / (dim + 1.0)) else 0.0

        for j in range(dim):
            p_c[j] = (1.0 - cc) * p_c[j] + hsig * math.sqrt(cc * (2.0 - cc) * mueff_used) * y_w[j]

        rank_mu = [0.0] * dim
        for i in range(mu_used):
            yi = sel[i]["y"]
            wi = w[i]
            for j in range(dim):
                rank_mu[j] += wi * yi[j] * yi[j]

        for j in range(dim):
            old_c = c_diag[j]
            c_diag[j] = (1.0 - c1 - cmu) * old_c
            c_diag[j] += c1 * (p_c[j] * p_c[j] + (1.0 - hsig) * cc * (2.0 - cc) * old_c)
            c_diag[j] += cmu * rank_mu[j]
            if c_diag[j] < 1e-12:
                c_diag[j] = 1e-12

        sigma *= math.exp((cs / damps) * ((norm_ps / chi_n) - 1.0))
        sigma = min(args.cma_sigma_max, max(args.cma_sigma_min, sigma))

        m = [m_old[j] + sigma_old * y_w[j] for j in range(dim)]
        m_params = _vector_to_params(
            m,
            mutable_constants=mutable_constants,
            base_params=base_params,
            specs=specs,
            max_drift=args.max_drift,
        )
        m = _params_to_vector(m_params, mutable_constants)

        write_json_line(
            progress_path,
            {
                "event": "cma_generation",
                "generation": generation,
                "iteration": quick_done,
                "sigma": sigma,
                "elite_quick_best": sel[0]["qrec"]["quick_score"],
                "elite_quick_mean": sum(s0["qrec"]["quick_score"] for s0 in sel) / mu_used,
                "n_eval": len(gen_samples),
            },
        )
        print(
            f"[worker {wid}] g={generation} CMA sigma={sigma:.4f} elite_best={sel[0]['qrec']['quick_score']:.2f}",
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
        "bps_step_scale": args.bps_step_scale,
        "max_drift": args.max_drift,
        "min_delta_edge": args.min_delta_edge,
        "hours_cap": args.hours,
        "eval_seed": shared_eval_seed,
        "cma_popsize": popsize,
        "cma_mu": mu,
        "cma_sigma_final": sigma,
        "cma_generations": generation,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_json_line(progress_path, {"event": "complete", **summary})
    print(f"[worker {wid}] complete", flush=True)
    return 0


def launch_main(args: argparse.Namespace) -> int:
    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    specs = parse_constant_specs(base_source)
    mutable_constants = parse_mutable_constants(args.mutable_constants, specs)

    out_dir = Path(args.out_dir) if args.out_dir else Path("Strat") / (
        "theo1_cmaes_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    shared_eval_seed = args.eval_seed if args.eval_seed >= 0 else args.seed

    worker_iterations = _distribute_iterations(args.iterations, args.workers)
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
            "--mutable-constants",
            args.mutable_constants,
            "--step-pct",
            str(args.step_pct),
            "--bps-step-scale",
            str(args.bps_step_scale),
            "--max-drift",
            str(args.max_drift),
            "--min-delta-edge",
            str(args.min_delta_edge),
            "--cma-popsize",
            str(args.cma_popsize),
            "--cma-mu",
            str(args.cma_mu),
            "--cma-sigma",
            str(args.cma_sigma),
            "--cma-sigma-min",
            str(args.cma_sigma_min),
            "--cma-sigma-max",
            str(args.cma_sigma_max),
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
        "objective": "avg_edge_only",
        "seed": args.seed,
        "eval_seed": shared_eval_seed,
        "mutable_constants": mutable_constants,
        "n_parseable_constants": len(specs),
        "step_pct": args.step_pct,
        "bps_step_scale": args.bps_step_scale,
        "max_drift": args.max_drift,
        "min_delta_edge": args.min_delta_edge,
        "cma_popsize": args.cma_popsize,
        "cma_mu": args.cma_mu,
        "cma_sigma": args.cma_sigma,
        "cma_sigma_min": args.cma_sigma_min,
        "cma_sigma_max": args.cma_sigma_max,
        "algorithm": "diagonal_cma_es_on_mutable_constant_values",
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
        return float(entry[1].get("refine_metrics", {}).get("avg_edge", float("-inf")))

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
