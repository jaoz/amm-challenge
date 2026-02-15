"""Configuration models for deterministic strategy simulation and search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class WorldRanges:
    """Randomized world parameter ranges per simulation seed."""

    sigma_min: float = 0.000882
    sigma_max: float = 0.001008
    retail_arrival_min: float = 0.6
    retail_arrival_max: float = 1.0
    retail_mean_size_min: float = 19.0
    retail_mean_size_max: float = 21.0


@dataclass(frozen=True)
class WorldConfig:
    """Global simulation configuration shared by all runs."""

    n_steps: int = 10_000
    initial_price: float = 100.0
    initial_x: float = 100.0
    initial_y: float = 10_000.0
    gbm_mu: float = 0.0
    gbm_dt: float = 1.0
    retail_size_sigma: float = 1.2
    retail_buy_prob: float = 0.5
    ranges: WorldRanges = field(default_factory=WorldRanges)


@dataclass(frozen=True)
class SampledWorld:
    """World parameters sampled from a single deterministic seed."""

    sigma: float
    retail_arrival_rate: float
    retail_mean_size: float


def sample_world_from_seed(seed: int, ranges: WorldRanges) -> SampledWorld:
    """Sample one world state from a deterministic RNG seed."""
    rng = np.random.default_rng(seed=seed)
    return SampledWorld(
        sigma=float(rng.uniform(ranges.sigma_min, ranges.sigma_max)),
        retail_arrival_rate=float(
            rng.uniform(ranges.retail_arrival_min, ranges.retail_arrival_max)
        ),
        retail_mean_size=float(
            rng.uniform(ranges.retail_mean_size_min, ranges.retail_mean_size_max)
        ),
    )


@dataclass(frozen=True)
class SeedSets:
    """Disjoint seed splits for train/validation/test stages."""

    train: tuple[int, ...]
    val: tuple[int, ...]
    test: tuple[int, ...]


def build_seed_sets(
    *,
    seed: int,
    n_train: int,
    n_val: int,
    n_test: int,
    spacing: int = 100_000,
) -> SeedSets:
    """Build disjoint deterministic seed sets."""
    train_start = seed
    val_start = seed + spacing
    test_start = seed + (2 * spacing)
    return SeedSets(
        train=tuple(range(train_start, train_start + n_train)),
        val=tuple(range(val_start, val_start + n_val)),
        test=tuple(range(test_start, test_start + n_test)),
    )


@dataclass(frozen=True)
class StrategyParams:
    """Tunable deterministic strategy parameters."""

    center_fee_bps: float = 37.0
    min_fee_bps: float = 8.0
    max_fee_bps: float = 100.0
    max_step_change_bps: float = 3.0
    max_asym_bps: float = 12.0
    normalizer_fee_bps: float = 30.0

    # Hidden-price filter controls.
    eps_weak: float = 0.06
    collapse_reopen_eps: float = 0.003
    w_arb_anchor: float = 0.68
    c_sigma: float = 2.0
    mid_smoothing: float = 0.08

    # State EWMAs.
    alpha_lambda: float = 0.08
    alpha_arb: float = 0.10
    alpha_vol: float = 0.06
    alpha_share: float = 0.08
    alpha_order: float = 0.06
    alpha_kappa: float = 0.05

    # Priors.
    lambda_prior: float = 0.8
    order_size_prior: float = 20.0
    kappa_prior: float = 7.0

    # Probable-arb classifier.
    arb_anchor_gate: float = 0.006
    arb_move_floor: float = 0.0002

    # One-step value proxy weights.
    value_penalty_arb: float = 8.0
    value_penalty_stale: float = 40.0
    value_penalty_inv_abs: float = 24.0
    value_penalty_inv_signed: float = 10.0
    value_penalty_spread: float = 30.0
    value_penalty_jump: float = 18.0

    # Discrete action set around center.
    action_grid_bps: tuple[float, ...] = (-4.0, -2.0, 0.0, 2.0, 4.0)
    asym_grid_bps: tuple[float, ...] = (-6.0, -3.0, 0.0, 3.0, 6.0)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action_grid_bps"] = list(self.action_grid_bps)
        payload["asym_grid_bps"] = list(self.asym_grid_bps)
        return payload


@dataclass(frozen=True)
class OptimizationConfig:
    """Config for deterministic train/validation/test optimization."""

    iterations: int = 18
    keep_top: int = 5
    n_train: int = 24
    n_val: int = 60
    n_test: int = 120

    train_steps: int = 1_500
    val_steps: int = 3_000
    test_steps: int = 5_000

    seed: int = 20260215
    z_alpha: float = 1.64
    min_effect: float = 1.0
    output_dir: str = "sim_lab/out"
    world: WorldConfig = field(default_factory=WorldConfig)
