"""Deterministic train/validation/test optimization for Python strategy params."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
from statistics import fmean, stdev
from typing import Any
import uuid

from sim_lab.config import OptimizationConfig, StrategyParams, build_seed_sets
from sim_lab.simulator import DeterministicSimulator


@dataclass(frozen=True)
class StageEvaluation:
    stage: str
    seed_set_id: str
    seeds: tuple[int, ...]
    n_steps: int
    mean_edge: float
    p10_edge: float
    p90_edge: float
    max_edge: float
    avg_fee_bps: float
    arb_volume_y: float
    retail_volume_y: float
    arb_trade_count: float
    retail_trade_count: float
    edges: tuple[float, ...]


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    params: StrategyParams
    train_eval: StageEvaluation
    val_eval: StageEvaluation | None = None
    val_delta_mean: float | None = None
    val_delta_se: float | None = None


@dataclass(frozen=True)
class OptimizationSummary:
    run_id: str
    config: OptimizationConfig
    winner: CandidateResult
    test_eval: StageEvaluation
    pass_flag: str
    log_path: str
    summary_path: str


def _quantile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[idx]


def _stage_record(
    *,
    run_id: str,
    stage: str,
    seed_set_id: str,
    params: StrategyParams,
    n_steps: int,
    seeds: tuple[int, ...],
    metrics: StageEvaluation,
    start_ts: str,
    end_ts: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "stage": stage,
        "strategy_path": "sim_lab/strategy.py",
        "strategy_name": "DeterministicAdaptiveStrategy",
        "params_json": json.dumps(params.to_json(), sort_keys=True, separators=(",", ":")),
        "seed_set_id": seed_set_id,
        "n_simulations": len(seeds),
        "n_steps": n_steps,
        "mean_edge": metrics.mean_edge,
        "p10_edge": metrics.p10_edge,
        "p90_edge": metrics.p90_edge,
        "max_edge": metrics.max_edge,
        "avg_fee_bps": metrics.avg_fee_bps,
        "arb_volume_y": metrics.arb_volume_y,
        "retail_volume_y": metrics.retail_volume_y,
        "arb_trade_count": metrics.arb_trade_count,
        "retail_trade_count": metrics.retail_trade_count,
    }


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _evaluate_stage(
    *,
    params: StrategyParams,
    world_config,
    stage: str,
    seed_set_id: str,
    seeds: tuple[int, ...],
    n_steps: int,
) -> StageEvaluation:
    edges: list[float] = []
    fees: list[float] = []
    arb_vols: list[float] = []
    retail_vols: list[float] = []
    arb_counts: list[float] = []
    retail_counts: list[float] = []

    simulator = DeterministicSimulator(world=world_config, strategy_params=params)
    for seed in seeds:
        run = simulator.run(
            seed=seed,
            n_steps=n_steps,
            capture_steps=False,
            capture_events=False,
            run_id=f"{stage}-{seed}",
        )
        edges.append(run.total_edge_submission)
        fees.append(run.avg_fee_bps_submission)
        arb_vols.append(run.arb_volume_y_submission)
        retail_vols.append(run.retail_volume_y_submission)
        arb_counts.append(float(run.arb_trade_count_submission))
        retail_counts.append(float(run.retail_trade_count_submission))

    sorted_edges = sorted(edges)
    return StageEvaluation(
        stage=stage,
        seed_set_id=seed_set_id,
        seeds=seeds,
        n_steps=n_steps,
        mean_edge=fmean(edges) if edges else 0.0,
        p10_edge=_quantile(sorted_edges, 0.10),
        p90_edge=_quantile(sorted_edges, 0.90),
        max_edge=sorted_edges[-1] if sorted_edges else 0.0,
        avg_fee_bps=fmean(fees) if fees else 0.0,
        arb_volume_y=fmean(arb_vols) if arb_vols else 0.0,
        retail_volume_y=fmean(retail_vols) if retail_vols else 0.0,
        arb_trade_count=fmean(arb_counts) if arb_counts else 0.0,
        retail_trade_count=fmean(retail_counts) if retail_counts else 0.0,
        edges=tuple(edges),
    )


def _paired_delta(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float]:
    if len(a) != len(b):
        raise ValueError("paired_delta requires equal-length vectors.")
    if not a:
        return 0.0, 0.0
    deltas = [x - y for x, y in zip(a, b)]
    mean_delta = fmean(deltas)
    if len(deltas) < 2:
        return mean_delta, 0.0
    se_delta = stdev(deltas) / math.sqrt(len(deltas))
    return mean_delta, se_delta


PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "center_fee_bps": (20.0, 80.0),
    "min_fee_bps": (4.0, 30.0),
    "max_fee_bps": (40.0, 130.0),
    "max_step_change_bps": (1.0, 8.0),
    "max_asym_bps": (2.0, 24.0),
    "eps_weak": (0.01, 0.25),
    "w_arb_anchor": (0.20, 0.90),
    "c_sigma": (0.5, 5.0),
    "mid_smoothing": (0.01, 0.40),
    "alpha_lambda": (0.02, 0.30),
    "alpha_arb": (0.02, 0.30),
    "alpha_vol": (0.02, 0.30),
    "alpha_share": (0.02, 0.30),
    "alpha_order": (0.02, 0.30),
    "alpha_kappa": (0.01, 0.20),
    "lambda_prior": (0.4, 1.2),
    "order_size_prior": (8.0, 40.0),
    "kappa_prior": (1.0, 30.0),
    "arb_anchor_gate": (0.001, 0.03),
    "arb_move_floor": (0.00001, 0.005),
    "value_penalty_arb": (0.0, 40.0),
    "value_penalty_stale": (0.0, 120.0),
    "value_penalty_inv_abs": (0.0, 80.0),
    "value_penalty_inv_signed": (0.0, 50.0),
    "value_penalty_spread": (0.0, 80.0),
    "value_penalty_jump": (0.0, 80.0),
}


def _build_params(overrides: dict[str, Any]) -> StrategyParams:
    params = StrategyParams().to_json()
    params.update(overrides)
    params["action_grid_bps"] = tuple(params["action_grid_bps"])
    params["asym_grid_bps"] = tuple(params["asym_grid_bps"])
    return StrategyParams(**params)


def _random_params(rng: random.Random) -> StrategyParams:
    overrides: dict[str, Any] = {}
    for key, (lo, hi) in PARAM_BOUNDS.items():
        overrides[key] = rng.uniform(lo, hi)
    candidate = _build_params(overrides)
    return _normalize_params(candidate)


def _mutate_params(rng: random.Random, base: StrategyParams) -> StrategyParams:
    data = base.to_json()
    mutate_keys = list(PARAM_BOUNDS.keys())
    n_mut = rng.randint(4, 10)
    for _ in range(n_mut):
        key = rng.choice(mutate_keys)
        lo, hi = PARAM_BOUNDS[key]
        current = float(data[key])
        span = hi - lo
        delta = rng.uniform(-0.18 * span, 0.18 * span)
        if rng.random() < 0.25:
            next_value = rng.uniform(lo, hi)
        else:
            next_value = current + delta
        data[key] = max(lo, min(hi, next_value))
    return _normalize_params(_build_params(data))


def _normalize_params(p: StrategyParams) -> StrategyParams:
    data = p.to_json()
    data["max_fee_bps"] = max(data["max_fee_bps"], data["min_fee_bps"] + 4.0)
    data["center_fee_bps"] = max(
        data["min_fee_bps"] + 1.0,
        min(data["max_fee_bps"] - 1.0, data["center_fee_bps"]),
    )
    data["action_grid_bps"] = tuple(data["action_grid_bps"])
    data["asym_grid_bps"] = tuple(data["asym_grid_bps"])
    return StrategyParams(**data)


def run_deterministic_optimization(config: OptimizationConfig) -> OptimizationSummary:
    """Run deterministic parameter optimization with seed-set separation."""
    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    log_path = out_dir / "optimization_runs.jsonl"
    summary_path = out_dir / "optimization_summary.json"

    seeds = build_seed_sets(
        seed=config.seed,
        n_train=config.n_train,
        n_val=config.n_val,
        n_test=config.n_test,
    )
    rng = random.Random(config.seed)

    candidates: list[CandidateResult] = []
    for idx in range(config.iterations):
        if idx == 0:
            params = StrategyParams()
        elif idx < 4:
            params = _random_params(rng)
        else:
            parent_pool = sorted(
                candidates,
                key=lambda c: c.train_eval.mean_edge,
                reverse=True,
            )[: max(2, min(config.keep_top, len(candidates)))]
            parent = parent_pool[rng.randrange(len(parent_pool))].params
            params = _mutate_params(rng, parent)

        train_start = datetime.now(timezone.utc).isoformat()
        train_eval = _evaluate_stage(
            params=params,
            world_config=config.world,
            stage="train",
            seed_set_id="S_train",
            seeds=seeds.train,
            n_steps=config.train_steps,
        )
        train_end = datetime.now(timezone.utc).isoformat()
        _append_jsonl(
            log_path,
            _stage_record(
                run_id=run_id,
                stage="train",
                seed_set_id="S_train",
                params=params,
                n_steps=config.train_steps,
                seeds=seeds.train,
                metrics=train_eval,
                start_ts=train_start,
                end_ts=train_end,
            ),
        )
        candidates.append(
            CandidateResult(
                candidate_id=f"cand_{idx:03d}",
                params=params,
                train_eval=train_eval,
            )
        )

    top_candidates = sorted(
        candidates,
        key=lambda c: c.train_eval.mean_edge,
        reverse=True,
    )[: max(1, config.keep_top)]

    val_evaluated: list[CandidateResult] = []
    incumbent: CandidateResult | None = None
    for base_candidate in top_candidates:
        val_start = datetime.now(timezone.utc).isoformat()
        val_eval = _evaluate_stage(
            params=base_candidate.params,
            world_config=config.world,
            stage="validation",
            seed_set_id="S_val",
            seeds=seeds.val,
            n_steps=config.val_steps,
        )
        val_end = datetime.now(timezone.utc).isoformat()
        _append_jsonl(
            log_path,
            _stage_record(
                run_id=run_id,
                stage="validation",
                seed_set_id="S_val",
                params=base_candidate.params,
                n_steps=config.val_steps,
                seeds=seeds.val,
                metrics=val_eval,
                start_ts=val_start,
                end_ts=val_end,
            ),
        )

        with_val = CandidateResult(
            candidate_id=base_candidate.candidate_id,
            params=base_candidate.params,
            train_eval=base_candidate.train_eval,
            val_eval=val_eval,
        )
        if incumbent is None:
            incumbent = with_val
            val_evaluated.append(with_val)
            continue

        mean_delta, se_delta = _paired_delta(val_eval.edges, incumbent.val_eval.edges)
        with_val = CandidateResult(
            candidate_id=with_val.candidate_id,
            params=with_val.params,
            train_eval=with_val.train_eval,
            val_eval=with_val.val_eval,
            val_delta_mean=mean_delta,
            val_delta_se=se_delta,
        )
        val_evaluated.append(with_val)

        if (mean_delta > (config.z_alpha * se_delta)) and (mean_delta > config.min_effect):
            incumbent = with_val

    if incumbent is None or incumbent.val_eval is None:
        raise RuntimeError("No incumbent after validation stage.")

    test_start = datetime.now(timezone.utc).isoformat()
    test_eval = _evaluate_stage(
        params=incumbent.params,
        world_config=config.world,
        stage="test",
        seed_set_id="S_test",
        seeds=seeds.test,
        n_steps=config.test_steps,
    )
    test_end = datetime.now(timezone.utc).isoformat()
    _append_jsonl(
        log_path,
        _stage_record(
            run_id=run_id,
            stage="test",
            seed_set_id="S_test",
            params=incumbent.params,
            n_steps=config.test_steps,
            seeds=seeds.test,
            metrics=test_eval,
            start_ts=test_start,
            end_ts=test_end,
        ),
    )

    if test_eval.mean_edge > 530.0:
        pass_flag = "STRETCH PASS"
    elif test_eval.mean_edge > 528.0:
        pass_flag = "PASS"
    else:
        pass_flag = "FAIL"

    summary_payload = {
        "run_id": run_id,
        "pass_flag": pass_flag,
        "winner_candidate_id": incumbent.candidate_id,
        "winner_params": incumbent.params.to_json(),
        "train_eval": incumbent.train_eval.__dict__,
        "val_eval": incumbent.val_eval.__dict__ if incumbent.val_eval else None,
        "test_eval": test_eval.__dict__,
        "config": {
            "iterations": config.iterations,
            "keep_top": config.keep_top,
            "n_train": config.n_train,
            "n_val": config.n_val,
            "n_test": config.n_test,
            "train_steps": config.train_steps,
            "val_steps": config.val_steps,
            "test_steps": config.test_steps,
            "seed": config.seed,
            "z_alpha": config.z_alpha,
            "min_effect": config.min_effect,
            "output_dir": config.output_dir,
        },
        "shortlist": [
            {
                "candidate_id": c.candidate_id,
                "train_mean_edge": c.train_eval.mean_edge,
                "val_mean_edge": c.val_eval.mean_edge if c.val_eval else None,
                "val_delta_mean": c.val_delta_mean,
                "val_delta_se": c.val_delta_se,
            }
            for c in val_evaluated
        ],
        "log_path": str(log_path),
    }
    summary_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8")

    return OptimizationSummary(
        run_id=run_id,
        config=config,
        winner=incumbent,
        test_eval=test_eval,
        pass_flag=pass_flag,
        log_path=str(log_path),
        summary_path=str(summary_path),
    )
