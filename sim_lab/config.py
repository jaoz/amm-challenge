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

    opening_fee_bps: float = 29.0
    center_fee_bps: float = 34.0
    min_fee_bps: float = 6.0
    max_fee_bps: float = 50.0
    max_step_change_bps: float = 3.4155384371138955
    max_asym_bps: float = 15.480528307961563
    normalizer_fee_bps: float = 30.0
    normalizer_state_alpha: float = 0.45
    normalizer_pretrade_arb_alpha: float = 0.75

    # Hidden-price filter controls.
    eps_weak: float = 0.0811636435345428
    collapse_reopen_eps: float = 0.003
    w_arb_anchor: float = 0.6591188386064732
    c_sigma: float = 0.7773710527801535
    mid_smoothing: float = 0.35664414417957846
    mid_smoothing_same_step: float = 0.008
    max_anchor_move_bps: float = 14.0
    max_mid_move_bps: float = 7.0

    # State EWMAs.
    alpha_lambda: float = 0.0827012877121022
    alpha_lambda_same_step: float = 0.22
    alpha_arb: float = 0.10327292893841228
    alpha_arb_same_step: float = 0.24
    alpha_vol: float = 0.04297702892525788
    alpha_share: float = 0.08360390766015166
    alpha_order: float = 0.27962079901709624
    alpha_kappa: float = 0.1806910705772641
    lambda_same_step_bonus: float = 0.9

    # Priors.
    lambda_prior: float = 1.1876540458366056
    order_size_prior: float = 16.03866545969269
    kappa_prior: float = 22.32772567305108

    # Probable-arb classifier.
    arb_anchor_gate: float = 0.02282692238538065
    arb_move_floor: float = 0.0012039740333856057
    retail_size_sigma_assumed: float = 1.2
    retail_mean_prior_y: float = 20.0
    retail_mean_blend: float = 0.45
    size_arb_abs_y: float = 1.0
    size_arb_rel_ratio: float = 0.06
    size_arb_relax_anchor_mult: float = 1.6
    size_arb_relax_move_mult: float = 0.35
    size_tail_prob_gate: float = 0.30
    size_tail_relax_anchor_mult: float = 2.0
    size_tail_relax_move_mult: float = 0.40

    # One-step value proxy weights.
    value_penalty_arb: float = 24.775705835210523
    value_penalty_stale: float = 94.72224524856807
    value_penalty_inv_abs: float = 62.08474726382997
    value_penalty_inv_signed: float = 13.735908763754765
    value_penalty_lambda_gap: float = 0.0
    value_penalty_spread: float = 78.5181616860509
    value_penalty_jump: float = 51.54111642171323
    edge_arb_scale: float = 24.0
    edge_inventory_scale: float = 14.0

    # Large-imbalance no-arb protection.
    arb_shield_trigger_bps: float = 180.0
    arb_shield_buffer_bps: float = 0.0

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
