#!/usr/bin/env python3
"""Parallel optimizer for strategy_powell_direct_v2_no_share.sol.

Modes:
- launch: start N workers in parallel
- worker: run evolutionary search in one process
- collect: aggregate best worker output
"""

from __future__ import annotations

import argparse
import gc
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
from statistics import mean, stdev
from typing import Any

from amm_competition.competition.config import BASELINE_VARIANCE, build_base_config, resolve_n_workers
from amm_competition.competition.match import MatchRunner
from amm_competition.evm.adapter import EVMStrategyAdapter
from amm_competition.evm.baseline import load_vanilla_strategy
from amm_competition.evm.compiler import SolidityCompiler


@dataclass
class Params:
    open_bps: int
    prior_bps: int
    target_max_bps: int
    max_jump_bps: int
    max_asym_bps: int
    max_skew_bps: int
    shield_trigger_bps: int
    shield_buffer_bps: int
    a_lambda: int
    a_lambda_same: int
    a_arb: int
    a_arb_same: int
    a_vol: int
    a_weak: int
    a_mid: int
    a_mid_same: int
    eps_weak_bps: int
    collapse_buf_bps: int
    anchor_w: int
    skew_fair_w: int
    skew_inv_w: int
    skew_w_sum: int
    mid_lambda_base: int
    mid_lambda_div: int
    mid_arb_base: int
    mid_arb_div: int
    mid_vol_base_bps: int
    mid_vol_div: int
    mid_step_penalty_bps: int
    mid_arb_bonus_bps: int
    mid_floor_bps: int
    fair_t1_bps: int
    fair_t2_bps: int
    fair_t3_bps: int
    fair_s1_bps: int
    fair_s2_bps: int
    fair_s3_bps: int
    inv_t1_bps: int
    inv_t2_bps: int
    inv_t3_bps: int
    inv_t4_bps: int
    inv_s1_bps: int
    inv_s2_bps: int
    inv_s3_bps: int
    inv_s4_bps: int
    arb_anchor_gate_bps: int
    arb_move_floor_bps: int
    size_relax_gate_mult: int
    size_relax_move_mult: int
    size_small_abs_y_tenths: int
    size_small_rel_pct: int


BOUNDS: dict[str, tuple[int, int]] = {
    "open_bps": (1, 120),
    "prior_bps": (4, 180),
    "target_max_bps": (80, 900),
    "max_jump_bps": (1, 120),
    "max_asym_bps": (2, 220),
    "max_skew_bps": (1, 220),
    "shield_trigger_bps": (1, 120),
    "shield_buffer_bps": (1, 30),
    "a_lambda": (1, 60),
    "a_lambda_same": (1, 90),
    "a_arb": (1, 60),
    "a_arb_same": (1, 90),
    "a_vol": (1, 40),
    "a_weak": (1, 60),
    "a_mid": (0, 40),
    "a_mid_same": (0, 30),
    "eps_weak_bps": (1, 400),
    "collapse_buf_bps": (1, 160),
    "anchor_w": (5, 95),
    "skew_fair_w": (1, 24),
    "skew_inv_w": (1, 24),
    "skew_w_sum": (2, 48),
    "mid_lambda_base": (40, 180),
    "mid_lambda_div": (1, 40),
    "mid_arb_base": (20, 120),
    "mid_arb_div": (1, 40),
    "mid_vol_base_bps": (1, 30),
    "mid_vol_div": (1, 20),
    "mid_step_penalty_bps": (0, 20),
    "mid_arb_bonus_bps": (0, 20),
    "mid_floor_bps": (0, 120),
    "fair_t1_bps": (1, 40),
    "fair_t2_bps": (2, 80),
    "fair_t3_bps": (4, 140),
    "fair_s1_bps": (0, 14),
    "fair_s2_bps": (0, 20),
    "fair_s3_bps": (0, 30),
    "inv_t1_bps": (20, 500),
    "inv_t2_bps": (40, 900),
    "inv_t3_bps": (60, 1400),
    "inv_t4_bps": (80, 2000),
    "inv_s1_bps": (0, 12),
    "inv_s2_bps": (0, 18),
    "inv_s3_bps": (0, 24),
    "inv_s4_bps": (0, 32),
    "arb_anchor_gate_bps": (20, 900),
    "arb_move_floor_bps": (1, 80),
    "size_relax_gate_mult": (100, 260),
    "size_relax_move_mult": (20, 150),
    "size_small_abs_y_tenths": (1, 120),
    "size_small_rel_pct": (1, 60),
}


CONSTANT_RENDERERS: dict[str, tuple[str, str]] = {
    "OPEN_BPS": ("open_bps", "{}"),
    "PRIOR_BPS": ("prior_bps", "{}"),
    "TARGET_MAX_BPS": ("target_max_bps", "{}"),
    "MAX_JUMP_BPS": ("max_jump_bps", "{}"),
    "MAX_ASYM_BPS": ("max_asym_bps", "{}"),
    "MAX_SKEW_BPS": ("max_skew_bps", "{}"),
    "SHIELD_TRIGGER_BPS": ("shield_trigger_bps", "{}"),
    "SHIELD_BUFFER_BPS": ("shield_buffer_bps", "{}"),
    "A_LAMBDA": ("a_lambda", "{}e16"),
    "A_LAMBDA_SAME": ("a_lambda_same", "{}e16"),
    "A_ARB": ("a_arb", "{}e16"),
    "A_ARB_SAME": ("a_arb_same", "{}e16"),
    "A_VOL": ("a_vol", "{}e16"),
    "A_WEAK": ("a_weak", "{}e16"),
    "A_MID": ("a_mid", "{}e16"),
    "A_MID_SAME": ("a_mid_same", "{}e16"),
    "EPS_WEAK_BPS": ("eps_weak_bps", "{}"),
    "COLLAPSE_BUF_BPS": ("collapse_buf_bps", "{}"),
    "ANCHOR_W": ("anchor_w", "{}e16"),
    "SKEW_FAIR_W": ("skew_fair_w", "{}"),
    "SKEW_INV_W": ("skew_inv_w", "{}"),
    "SKEW_W_SUM": ("skew_w_sum", "{}"),
    "MID_LAMBDA_BASE": ("mid_lambda_base", "{}e16"),
    "MID_LAMBDA_DIV": ("mid_lambda_div", "{}e16"),
    "MID_ARB_BASE": ("mid_arb_base", "{}e16"),
    "MID_ARB_DIV": ("mid_arb_div", "{}e16"),
    "MID_VOL_BASE_BPS": ("mid_vol_base_bps", "{}"),
    "MID_VOL_DIV": ("mid_vol_div", "{}"),
    "MID_STEP_PENALTY_BPS": ("mid_step_penalty_bps", "{}"),
    "MID_ARB_BONUS_BPS": ("mid_arb_bonus_bps", "{}"),
    "MID_FLOOR_BPS": ("mid_floor_bps", "{}"),
    "FAIR_T1_BPS": ("fair_t1_bps", "{}"),
    "FAIR_T2_BPS": ("fair_t2_bps", "{}"),
    "FAIR_T3_BPS": ("fair_t3_bps", "{}"),
    "FAIR_S1_BPS": ("fair_s1_bps", "{}"),
    "FAIR_S2_BPS": ("fair_s2_bps", "{}"),
    "FAIR_S3_BPS": ("fair_s3_bps", "{}"),
    "INV_T1_BPS": ("inv_t1_bps", "{}"),
    "INV_T2_BPS": ("inv_t2_bps", "{}"),
    "INV_T3_BPS": ("inv_t3_bps", "{}"),
    "INV_T4_BPS": ("inv_t4_bps", "{}"),
    "INV_S1_BPS": ("inv_s1_bps", "{}"),
    "INV_S2_BPS": ("inv_s2_bps", "{}"),
    "INV_S3_BPS": ("inv_s3_bps", "{}"),
    "INV_S4_BPS": ("inv_s4_bps", "{}"),
    "ARB_ANCHOR_GATE_BPS": ("arb_anchor_gate_bps", "{}"),
    "ARB_MOVE_FLOOR_BPS": ("arb_move_floor_bps", "{}"),
    "SIZE_RELAX_GATE_MULT": ("size_relax_gate_mult", "{}"),
    "SIZE_RELAX_MOVE_MULT": ("size_relax_move_mult", "{}"),
    "SIZE_SMALL_ABS_Y": ("size_small_abs_y_tenths", "{}e17"),
    "SIZE_SMALL_REL_WAD": ("size_small_rel_pct", "{}e16"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parallel optimizer for PowellDirect_NoShare_v2 strategy.")
    parser.add_argument("--mode", choices=["launch", "worker", "collect"], default="launch")
    parser.add_argument("--base-strategy", default="Strat/strategy_powell_direct_v2_no_share.sol")
    parser.add_argument("--hours", type=float, default=8.0, help="Hard runtime cap (hours).")
    parser.add_argument("--iterations", type=int, default=100, help="Total quick-eval iterations.")
    parser.add_argument("--worker-iterations", type=int, default=0, help="Quick-eval iterations per worker.")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--target-fee-bps", type=float, default=24.0)
    parser.add_argument(
        "--seed-progress-dir",
        default="",
        help="Optional prior run directory containing worker_*.progress.jsonl; best avg_edge params seed the search.",
    )
    parser.add_argument("--quick-sims", type=int, default=36)
    parser.add_argument("--refine-sims", type=int, default=160)
    parser.add_argument("--refine-every", type=int, default=5)
    parser.add_argument("--sig-z", type=float, default=1.96, help="Z-threshold for significance-gated promotion.")
    parser.add_argument(
        "--min-delta-edge",
        type=float,
        default=0.0,
        help="Minimum absolute avg_edge improvement required in addition to significance.",
    )
    parser.add_argument("--seed", type=int, default=20260215)
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument(
        "--mutable-params",
        default="",
        help="Comma-separated Params field names to mutate; when set, all other params stay fixed to the seed/base.",
    )
    parser.add_argument("--out-dir", default="")
    return parser.parse_args()


def _extract_constant_expr(source: str, name: str) -> str:
    m = re.search(rf"uint256\s+private\s+constant\s+{name}\s*=\s*([^;]+);", source)
    if not m:
        raise ValueError(f"Missing constant in base strategy: {name}")
    return m.group(1).split("//", 1)[0].strip()


def _parse_plain_int(expr: str, name: str) -> int:
    if not re.fullmatch(r"\d+", expr):
        raise ValueError(f"Expected integer literal for {name}, got: {expr}")
    return int(expr)


def _parse_e_notation(expr: str, power: int, name: str) -> int:
    m = re.fullmatch(rf"(\d+)e{power}", expr)
    if not m:
        raise ValueError(f"Expected e{power} literal for {name}, got: {expr}")
    return int(m.group(1))


def parse_base_params(base_source: str) -> Params:
    return Params(
        open_bps=_parse_plain_int(_extract_constant_expr(base_source, "OPEN_BPS"), "OPEN_BPS"),
        prior_bps=_parse_plain_int(_extract_constant_expr(base_source, "PRIOR_BPS"), "PRIOR_BPS"),
        target_max_bps=_parse_plain_int(_extract_constant_expr(base_source, "TARGET_MAX_BPS"), "TARGET_MAX_BPS"),
        max_jump_bps=_parse_plain_int(_extract_constant_expr(base_source, "MAX_JUMP_BPS"), "MAX_JUMP_BPS"),
        max_asym_bps=_parse_plain_int(_extract_constant_expr(base_source, "MAX_ASYM_BPS"), "MAX_ASYM_BPS"),
        max_skew_bps=_parse_plain_int(_extract_constant_expr(base_source, "MAX_SKEW_BPS"), "MAX_SKEW_BPS"),
        shield_trigger_bps=_parse_plain_int(
            _extract_constant_expr(base_source, "SHIELD_TRIGGER_BPS"),
            "SHIELD_TRIGGER_BPS",
        ),
        shield_buffer_bps=_parse_plain_int(_extract_constant_expr(base_source, "SHIELD_BUFFER_BPS"), "SHIELD_BUFFER_BPS"),
        a_lambda=_parse_e_notation(_extract_constant_expr(base_source, "A_LAMBDA"), 16, "A_LAMBDA"),
        a_lambda_same=_parse_e_notation(_extract_constant_expr(base_source, "A_LAMBDA_SAME"), 16, "A_LAMBDA_SAME"),
        a_arb=_parse_e_notation(_extract_constant_expr(base_source, "A_ARB"), 16, "A_ARB"),
        a_arb_same=_parse_e_notation(_extract_constant_expr(base_source, "A_ARB_SAME"), 16, "A_ARB_SAME"),
        a_vol=_parse_e_notation(_extract_constant_expr(base_source, "A_VOL"), 16, "A_VOL"),
        a_weak=_parse_e_notation(_extract_constant_expr(base_source, "A_WEAK"), 16, "A_WEAK"),
        a_mid=_parse_e_notation(_extract_constant_expr(base_source, "A_MID"), 16, "A_MID"),
        a_mid_same=_parse_e_notation(_extract_constant_expr(base_source, "A_MID_SAME"), 16, "A_MID_SAME"),
        eps_weak_bps=_parse_plain_int(_extract_constant_expr(base_source, "EPS_WEAK_BPS"), "EPS_WEAK_BPS"),
        collapse_buf_bps=_parse_plain_int(_extract_constant_expr(base_source, "COLLAPSE_BUF_BPS"), "COLLAPSE_BUF_BPS"),
        anchor_w=_parse_e_notation(_extract_constant_expr(base_source, "ANCHOR_W"), 16, "ANCHOR_W"),
        skew_fair_w=_parse_plain_int(_extract_constant_expr(base_source, "SKEW_FAIR_W"), "SKEW_FAIR_W"),
        skew_inv_w=_parse_plain_int(_extract_constant_expr(base_source, "SKEW_INV_W"), "SKEW_INV_W"),
        skew_w_sum=_parse_plain_int(_extract_constant_expr(base_source, "SKEW_W_SUM"), "SKEW_W_SUM"),
        mid_lambda_base=_parse_e_notation(_extract_constant_expr(base_source, "MID_LAMBDA_BASE"), 16, "MID_LAMBDA_BASE"),
        mid_lambda_div=_parse_e_notation(_extract_constant_expr(base_source, "MID_LAMBDA_DIV"), 16, "MID_LAMBDA_DIV"),
        mid_arb_base=_parse_e_notation(_extract_constant_expr(base_source, "MID_ARB_BASE"), 16, "MID_ARB_BASE"),
        mid_arb_div=_parse_e_notation(_extract_constant_expr(base_source, "MID_ARB_DIV"), 16, "MID_ARB_DIV"),
        mid_vol_base_bps=_parse_plain_int(_extract_constant_expr(base_source, "MID_VOL_BASE_BPS"), "MID_VOL_BASE_BPS"),
        mid_vol_div=_parse_plain_int(_extract_constant_expr(base_source, "MID_VOL_DIV"), "MID_VOL_DIV"),
        mid_step_penalty_bps=_parse_plain_int(
            _extract_constant_expr(base_source, "MID_STEP_PENALTY_BPS"),
            "MID_STEP_PENALTY_BPS",
        ),
        mid_arb_bonus_bps=_parse_plain_int(
            _extract_constant_expr(base_source, "MID_ARB_BONUS_BPS"),
            "MID_ARB_BONUS_BPS",
        ),
        mid_floor_bps=_parse_plain_int(_extract_constant_expr(base_source, "MID_FLOOR_BPS"), "MID_FLOOR_BPS"),
        fair_t1_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_T1_BPS"), "FAIR_T1_BPS"),
        fair_t2_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_T2_BPS"), "FAIR_T2_BPS"),
        fair_t3_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_T3_BPS"), "FAIR_T3_BPS"),
        fair_s1_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_S1_BPS"), "FAIR_S1_BPS"),
        fair_s2_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_S2_BPS"), "FAIR_S2_BPS"),
        fair_s3_bps=_parse_plain_int(_extract_constant_expr(base_source, "FAIR_S3_BPS"), "FAIR_S3_BPS"),
        inv_t1_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_T1_BPS"), "INV_T1_BPS"),
        inv_t2_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_T2_BPS"), "INV_T2_BPS"),
        inv_t3_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_T3_BPS"), "INV_T3_BPS"),
        inv_t4_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_T4_BPS"), "INV_T4_BPS"),
        inv_s1_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_S1_BPS"), "INV_S1_BPS"),
        inv_s2_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_S2_BPS"), "INV_S2_BPS"),
        inv_s3_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_S3_BPS"), "INV_S3_BPS"),
        inv_s4_bps=_parse_plain_int(_extract_constant_expr(base_source, "INV_S4_BPS"), "INV_S4_BPS"),
        arb_anchor_gate_bps=_parse_plain_int(
            _extract_constant_expr(base_source, "ARB_ANCHOR_GATE_BPS"),
            "ARB_ANCHOR_GATE_BPS",
        ),
        arb_move_floor_bps=_parse_plain_int(
            _extract_constant_expr(base_source, "ARB_MOVE_FLOOR_BPS"),
            "ARB_MOVE_FLOOR_BPS",
        ),
        size_relax_gate_mult=_parse_plain_int(
            _extract_constant_expr(base_source, "SIZE_RELAX_GATE_MULT"),
            "SIZE_RELAX_GATE_MULT",
        ),
        size_relax_move_mult=_parse_plain_int(
            _extract_constant_expr(base_source, "SIZE_RELAX_MOVE_MULT"),
            "SIZE_RELAX_MOVE_MULT",
        ),
        size_small_abs_y_tenths=_parse_e_notation(
            _extract_constant_expr(base_source, "SIZE_SMALL_ABS_Y"),
            17,
            "SIZE_SMALL_ABS_Y",
        ),
        size_small_rel_pct=_parse_e_notation(
            _extract_constant_expr(base_source, "SIZE_SMALL_REL_WAD"),
            16,
            "SIZE_SMALL_REL_WAD",
        ),
    )


def valid_params(p: Params) -> bool:
    if p.prior_bps >= p.target_max_bps - 4:
        return False
    if p.mid_floor_bps >= p.target_max_bps - 1:
        return False
    if p.max_skew_bps > p.max_asym_bps:
        return False
    if p.shield_buffer_bps > p.shield_trigger_bps:
        return False
    if p.skew_w_sum < 1:
        return False
    if p.a_lambda_same < p.a_lambda:
        return False
    if p.a_arb_same < p.a_arb:
        return False
    if p.a_mid_same > p.a_mid:
        return False
    if p.mid_lambda_div < 1 or p.mid_arb_div < 1 or p.mid_vol_div < 1:
        return False
    if not (p.fair_t1_bps < p.fair_t2_bps < p.fair_t3_bps):
        return False
    if not (p.fair_s1_bps <= p.fair_s2_bps <= p.fair_s3_bps):
        return False
    if not (p.inv_t1_bps < p.inv_t2_bps < p.inv_t3_bps < p.inv_t4_bps):
        return False
    if not (p.inv_s1_bps <= p.inv_s2_bps <= p.inv_s3_bps <= p.inv_s4_bps):
        return False
    if p.size_relax_gate_mult < 100:
        return False
    if p.size_relax_move_mult < 1:
        return False
    if p.open_bps > p.target_max_bps:
        return False
    return True


def clamp_params(p: Params) -> Params:
    data = asdict(p)
    for k, (lo, hi) in BOUNDS.items():
        data[k] = max(lo, min(hi, int(data[k])))
    c = Params(**data)
    if c.max_skew_bps > c.max_asym_bps:
        c.max_skew_bps = c.max_asym_bps
    if c.shield_buffer_bps > c.shield_trigger_bps:
        c.shield_buffer_bps = c.shield_trigger_bps
    if c.a_lambda_same < c.a_lambda:
        c.a_lambda_same = c.a_lambda
    if c.a_arb_same < c.a_arb:
        c.a_arb_same = c.a_arb
    if c.a_mid_same > c.a_mid:
        c.a_mid_same = c.a_mid
    if c.mid_lambda_div < 1:
        c.mid_lambda_div = 1
    if c.mid_arb_div < 1:
        c.mid_arb_div = 1
    if c.mid_vol_div < 1:
        c.mid_vol_div = 1
    if c.fair_t2_bps <= c.fair_t1_bps:
        c.fair_t2_bps = c.fair_t1_bps + 1
    if c.fair_t3_bps <= c.fair_t2_bps:
        c.fair_t3_bps = c.fair_t2_bps + 1
    if c.fair_s2_bps < c.fair_s1_bps:
        c.fair_s2_bps = c.fair_s1_bps
    if c.fair_s3_bps < c.fair_s2_bps:
        c.fair_s3_bps = c.fair_s2_bps
    if c.inv_t2_bps <= c.inv_t1_bps:
        c.inv_t2_bps = c.inv_t1_bps + 1
    if c.inv_t3_bps <= c.inv_t2_bps:
        c.inv_t3_bps = c.inv_t2_bps + 1
    if c.inv_t4_bps <= c.inv_t3_bps:
        c.inv_t4_bps = c.inv_t3_bps + 1
    if c.inv_s2_bps < c.inv_s1_bps:
        c.inv_s2_bps = c.inv_s1_bps
    if c.inv_s3_bps < c.inv_s2_bps:
        c.inv_s3_bps = c.inv_s2_bps
    if c.inv_s4_bps < c.inv_s3_bps:
        c.inv_s4_bps = c.inv_s3_bps
    if c.size_relax_gate_mult < 100:
        c.size_relax_gate_mult = 100
    if c.skew_w_sum < 1:
        c.skew_w_sum = 1
    if c.prior_bps >= c.target_max_bps - 4:
        c.prior_bps = max(BOUNDS["prior_bps"][0], c.target_max_bps - 8)
    if c.mid_floor_bps >= c.target_max_bps:
        c.mid_floor_bps = max(0, c.target_max_bps - 1)
    if c.open_bps > c.target_max_bps:
        c.open_bps = c.target_max_bps
    return c


def parse_mutable_params(raw: str) -> list[str]:
    if not raw.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        key = token.strip()
        if not key:
            continue
        if key not in BOUNDS:
            raise ValueError(f"Unknown mutable param '{key}'. Expected one of: {', '.join(sorted(BOUNDS))}")
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def random_params(
    rng: random.Random,
    *,
    fixed_base: Params | None = None,
    mutable_keys: list[str] | None = None,
) -> Params:
    if fixed_base is None:
        params: dict[str, int] = {}
        for k, (lo, hi) in BOUNDS.items():
            params[k] = rng.randint(lo, hi)
        return clamp_params(Params(**params))

    params = asdict(fixed_base)
    keys = list(mutable_keys or BOUNDS.keys())
    for k in keys:
        lo, hi = BOUNDS[k]
        params[k] = rng.randint(lo, hi)
    return clamp_params(Params(**params))


def mutate_params(rng: random.Random, parent: Params, *, mutable_keys: list[str] | None = None) -> Params:
    child = Params(**asdict(parent))
    keys = list(mutable_keys or BOUNDS.keys())
    if not keys:
        return child
    if mutable_keys:
        n_mut = rng.randint(1, min(3, len(keys)))
    else:
        n_mut = rng.randint(4, min(12, len(keys)))
    for _ in range(n_mut):
        k = rng.choice(keys)
        lo, hi = BOUNDS[k]
        span = hi - lo
        step = max(1, int(span * rng.choice([0.05, 0.10, 0.16, 0.22])))
        if rng.random() < 0.68:
            value = getattr(child, k) + rng.randint(-step, step)
        else:
            value = rng.randint(lo, hi)
        setattr(child, k, max(lo, min(hi, value)))
    return clamp_params(child)


def _replace_constant(source: str, name: str, rendered_value: str) -> str:
    pattern = re.compile(rf"(uint256\s+private\s+constant\s+{name}\s*=\s*)([^;]+)(;)")
    updated, count = pattern.subn(
        lambda m: f"{m.group(1)}{rendered_value}{m.group(3)}",
        source,
        count=1,
    )
    if count != 1:
        raise ValueError(f"Failed to replace constant {name}")
    return updated


def strategy_source(base_source: str, params: Params, name: str) -> str:
    src = base_source
    data = asdict(params)
    for const_name, (field, fmt) in CONSTANT_RENDERERS.items():
        src = _replace_constant(src, const_name, fmt.format(data[field]))

    name_old = 'return "PowellDirect_NoShare_v2";'
    name_new = f'return "{name}";'
    if name_old in src:
        src = src.replace(name_old, name_new, 1)
    else:
        src = re.sub(r'return\s+"[^"]+";', name_new, src, count=1)
    return src


def compute_objective(metrics: dict[str, float], target_fee_bps: float) -> float:
    # Requested objective: optimize only for average edge.
    _ = target_fee_bps
    return metrics["avg_edge"]


def is_significant_improvement(
    candidate_metrics: dict[str, float],
    incumbent_metrics: dict[str, float],
    *,
    z_threshold: float,
    min_delta_edge: float,
) -> tuple[bool, float, float, float]:
    diff = float(candidate_metrics["avg_edge"] - incumbent_metrics["avg_edge"])
    se_new = float(candidate_metrics.get("edge_se", 0.0))
    se_old = float(incumbent_metrics.get("edge_se", 0.0))
    se_diff = math.sqrt(se_new * se_new + se_old * se_old)
    required = max(min_delta_edge, z_threshold * se_diff)
    return diff > required, diff, required, se_diff


def evaluate(
    source: str,
    n_simulations: int,
    compiler: SolidityCompiler,
    baseline: EVMStrategyAdapter,
    n_workers: int,
) -> tuple[dict[str, float] | None, str | None]:
    comp = compiler.compile(source)
    if not comp.success:
        errors = comp.errors or ["compile_failed"]
        err = errors[0].replace("\r", " ").replace("\n", " ")
        return None, err[:500]

    user = EVMStrategyAdapter(bytecode=comp.bytecode, abi=comp.abi)
    runner = MatchRunner(
        n_simulations=n_simulations,
        config=build_base_config(seed=None),
        n_workers=n_workers,
        variance=BASELINE_VARIANCE,
    )
    try:
        result = runner.run_match(user, baseline, store_results=True)
    except Exception as exc:
        return None, f"run_match_error: {exc}"
    sims = len(result.simulation_results)
    if sims == 0:
        return None, "zero_simulations"

    edges_raw = [float(sim.edges["submission"]) for sim in result.simulation_results]
    edges = sorted(edges_raw)
    p05_idx = max(0, math.floor(0.05 * sims) - 1)
    p10_idx = max(0, math.floor(0.10 * sims) - 1)
    avg_fee_bps = (
        mean((sim.average_fees["submission"][0] + sim.average_fees["submission"][1]) * 0.5 for sim in result.simulation_results)
        * 10000
    )
    retail = mean(sim.retail_volume_y["submission"] for sim in result.simulation_results)
    arb = mean(sim.arb_volume_y["submission"] for sim in result.simulation_results)

    edge_std = stdev(edges_raw) if sims > 1 else 0.0
    edge_se = edge_std / math.sqrt(sims) if sims > 0 else 0.0

    metrics = {
        "avg_edge": float(result.total_edge_a / Decimal(sims)),
        "n_sims": float(sims),
        "edge_std": edge_std,
        "edge_se": edge_se,
        "min_edge": edges[0],
        "p05_edge": edges[p05_idx],
        "p10_edge": edges[p10_idx],
        "median_edge": edges[sims // 2],
        "max_edge": edges[-1],
        "avg_fee_bps": avg_fee_bps,
        "retail_volume_y": retail,
        "arb_volume_y": arb,
    }
    del result
    gc.collect()
    return metrics, None


def write_json_line(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def find_best_params_from_progress(progress_dir: str) -> dict[str, Any] | None:
    if not progress_dir:
        return None
    root = Path(progress_dir)
    if not root.exists():
        return None

    best: dict[str, Any] | None = None
    for path in sorted(root.glob("worker_*.progress.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("event") not in {"quick", "refine"}:
                        continue
                    metrics = event.get("quick_metrics") or event.get("refine_metrics") or {}
                    avg_edge = metrics.get("avg_edge")
                    params = event.get("params")
                    if avg_edge is None or not isinstance(params, dict):
                        continue
                    candidate = {
                        "path": str(path),
                        "name": event.get("name"),
                        "event": event.get("event"),
                        "avg_edge": float(avg_edge),
                        "params": params,
                    }
                    if best is None or candidate["avg_edge"] > best["avg_edge"]:
                        best = candidate
        except Exception:
            continue
    return best


def _seed_candidates(base: Params) -> list[Params]:
    base_seed = clamp_params(base)
    lower_fee = clamp_params(
        Params(
            **{
                **asdict(base_seed),
                "open_bps": max(BOUNDS["open_bps"][0], base_seed.open_bps - 6),
                "prior_bps": max(BOUNDS["prior_bps"][0], base_seed.prior_bps - 8),
                "shield_trigger_bps": max(BOUNDS["shield_trigger_bps"][0], base_seed.shield_trigger_bps - 2),
                "shield_buffer_bps": max(BOUNDS["shield_buffer_bps"][0], base_seed.shield_buffer_bps - 1),
                "max_jump_bps": min(BOUNDS["max_jump_bps"][1], base_seed.max_jump_bps + 3),
                "max_asym_bps": min(BOUNDS["max_asym_bps"][1], base_seed.max_asym_bps + 6),
                "max_skew_bps": min(BOUNDS["max_skew_bps"][1], base_seed.max_skew_bps + 5),
            }
        )
    )
    wider_capture = clamp_params(
        Params(
            **{
                **asdict(base_seed),
                "open_bps": max(BOUNDS["open_bps"][0], base_seed.open_bps - 10),
                "prior_bps": max(BOUNDS["prior_bps"][0], base_seed.prior_bps - 10),
                "target_max_bps": min(BOUNDS["target_max_bps"][1], base_seed.target_max_bps + 40),
                "max_jump_bps": min(BOUNDS["max_jump_bps"][1], base_seed.max_jump_bps + 8),
                "eps_weak_bps": max(BOUNDS["eps_weak_bps"][0], base_seed.eps_weak_bps - 15),
                "arb_anchor_gate_bps": min(BOUNDS["arb_anchor_gate_bps"][1], base_seed.arb_anchor_gate_bps + 70),
                "arb_move_floor_bps": max(BOUNDS["arb_move_floor_bps"][0], base_seed.arb_move_floor_bps - 2),
            }
        )
    )
    return [base_seed, lower_fee, wider_capture]


def worker_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wid = args.worker_id
    iterations_budget = args.worker_iterations if args.worker_iterations > 0 else args.iterations
    rng = random.Random(args.seed + wid * 1_000_003)

    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    base_params = parse_base_params(base_source)
    mutable_keys = parse_mutable_params(args.mutable_params)
    seed_base = base_params
    seeded_from: dict[str, Any] | None = None
    if args.seed_progress_dir:
        seeded_from = find_best_params_from_progress(args.seed_progress_dir)
        if seeded_from and isinstance(seeded_from.get("params"), dict):
            merged = asdict(base_params)
            for k, v in seeded_from["params"].items():
                if k in merged:
                    merged[k] = int(v)
            seed_base = clamp_params(Params(**merged))
    seeds = [seed_base] if mutable_keys else _seed_candidates(seed_base)

    progress_path = out_dir / f"worker_{wid}.progress.jsonl"
    best_json_path = out_dir / f"worker_{wid}.best.json"
    best_sol_path = out_dir / f"worker_{wid}.best.sol"
    summary_path = out_dir / f"worker_{wid}.summary.json"

    compiler = SolidityCompiler()
    baseline = load_vanilla_strategy()
    n_workers = resolve_n_workers()

    quick: list[dict[str, Any]] = []
    refined: list[dict[str, Any]] = []
    best_refined: dict[str, Any] | None = None
    deadline = time.time() + args.hours * 3600.0
    quick_done = 0
    attempts = 0

    print(
        f"[worker {wid}] start iter_budget={iterations_budget} hours={args.hours} "
        f"quick={args.quick_sims} refine={args.refine_sims} target_fee={args.target_fee_bps} "
        f"mutable={mutable_keys or 'ALL'}",
        flush=True,
    )
    if seeded_from is not None:
        print(
            f"[worker {wid}] seeded_from={seeded_from.get('name')} event={seeded_from.get('event')} "
            f"avg_edge={seeded_from.get('avg_edge')}",
            flush=True,
        )

    while quick_done < iterations_budget and time.time() < deadline:
        attempts += 1
        seed_index = quick_done + 1
        if seed_index <= len(seeds):
            params = seeds[(seed_index - 1 + wid) % len(seeds)]
        else:
            if not quick or rng.random() < 0.60:
                params = random_params(rng, fixed_base=seed_base if mutable_keys else None, mutable_keys=mutable_keys)
            else:
                top_pool = sorted(quick, key=lambda x: x["quick_score"], reverse=True)[: max(8, min(48, len(quick)))]
                parent = Params(**top_pool[rng.randrange(len(top_pool))]["params"])
                params = mutate_params(rng, parent, mutable_keys=mutable_keys)

        if not valid_params(params):
            continue

        name = f"PowellDirectNoShare_w{wid}_{attempts:06d}"
        source = strategy_source(base_source, params, name)
        metrics, fail_reason = evaluate(source, args.quick_sims, compiler, baseline, n_workers)
        if metrics is None:
            write_json_line(
                progress_path,
                {
                    "event": "quick_compile_or_eval_fail",
                    "attempt": attempts,
                    "iteration": quick_done + 1,
                    "name": name,
                    "params": asdict(params),
                    "reason": fail_reason,
                },
            )
            continue

        quick_done += 1
        quick_score = compute_objective(metrics, args.target_fee_bps)
        qrec = {
            "attempt": attempts,
            "iteration": quick_done,
            "name": name,
            "params": asdict(params),
            "quick_metrics": metrics,
            "quick_score": quick_score,
            "timestamp": time.time(),
        }
        quick.append(qrec)
        write_json_line(progress_path, {"event": "quick", **qrec})

        best_quick = max(quick, key=lambda x: x["quick_score"])
        print(
            f"[worker {wid}] it={quick_done}/{iterations_budget} (attempt={attempts}) quick score={quick_score:.2f} "
            f"edge={metrics['avg_edge']:.2f} p10={metrics['p10_edge']:.2f} fee={metrics['avg_fee_bps']:.2f} "
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
        p_obj = Params(**pick["params"])
        r_source = strategy_source(base_source, p_obj, pick["name"] + "_refined")
        r_metrics, r_fail_reason = evaluate(r_source, args.refine_sims, compiler, baseline, n_workers)
        if r_metrics is None:
            write_json_line(
                progress_path,
                {
                    "event": "refine_compile_or_eval_fail",
                    "attempt": attempts,
                    "iteration": quick_done,
                    "name": pick["name"],
                    "params": pick["params"],
                    "reason": r_fail_reason,
                },
            )
            continue

        r_score = compute_objective(r_metrics, args.target_fee_bps)
        rrec = {
            "attempt": attempts,
            "iteration": quick_done,
            "name": pick["name"],
            "params": pick["params"],
            "refine_metrics": r_metrics,
            "refine_score": r_score,
            "timestamp": time.time(),
        }
        refined.append(rrec)
        write_json_line(progress_path, {"event": "refine", **rrec})
        print(
            f"[worker {wid}] it={quick_done}/{iterations_budget} REFINE score={r_score:.2f} "
            f"edge={r_metrics['avg_edge']:.2f} p10={r_metrics['p10_edge']:.2f} fee={r_metrics['avg_fee_bps']:.2f}",
            flush=True,
        )

        if best_refined is None:
            best_refined = rrec
            best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
            best_sol_path.write_text(strategy_source(base_source, p_obj, f"PowellDirectNoShare_best_worker_{wid}"), encoding="utf-8")
            print(
                f"[worker {wid}] NEW BEST (seed) refine score={r_score:.2f} edge={r_metrics['avg_edge']:.2f} "
                f"fee={r_metrics['avg_fee_bps']:.2f}",
                flush=True,
            )
        else:
            incumbent_metrics = best_refined.get("refine_metrics", {})
            is_sig, delta, required, se_diff = is_significant_improvement(
                r_metrics,
                incumbent_metrics,
                z_threshold=args.sig_z,
                min_delta_edge=args.min_delta_edge,
            )
            if is_sig:
                best_refined = rrec
                best_json_path.write_text(json.dumps(best_refined, indent=2, sort_keys=True), encoding="utf-8")
                best_sol_path.write_text(
                    strategy_source(base_source, p_obj, f"PowellDirectNoShare_best_worker_{wid}"),
                    encoding="utf-8",
                )
                print(
                    f"[worker {wid}] NEW BEST (significant) delta={delta:.4f} required>{required:.4f} "
                    f"(se_diff={se_diff:.4f}) edge={r_metrics['avg_edge']:.2f}",
                    flush=True,
                )
            else:
                write_json_line(
                    progress_path,
                    {
                        "event": "refine_not_significant",
                        "attempt": attempts,
                        "iteration": quick_done,
                        "name": pick["name"],
                        "candidate_avg_edge": r_metrics["avg_edge"],
                        "incumbent_avg_edge": incumbent_metrics.get("avg_edge"),
                        "delta_avg_edge": delta,
                        "required_delta": required,
                        "se_diff": se_diff,
                        "sig_z": args.sig_z,
                        "min_delta_edge": args.min_delta_edge,
                    },
                )
                print(
                    f"[worker {wid}] REFINE rejected (not significant) delta={delta:.4f} required>{required:.4f} "
                    f"(se_diff={se_diff:.4f})",
                    flush=True,
                )

    summary = {
        "worker_id": wid,
        "iterations_budget": iterations_budget,
        "iterations_executed": quick_done,
        "attempts": attempts,
        "quick_evals": len(quick),
        "refine_evals": len(refined),
        "best_refined": best_refined,
        "target_fee_bps": args.target_fee_bps,
        "sig_z": args.sig_z,
        "min_delta_edge": args.min_delta_edge,
        "mutable_params": mutable_keys,
        "hours_cap": args.hours,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_json_line(progress_path, {"event": "complete", **summary})
    print(f"[worker {wid}] complete", flush=True)
    return 0


def _distribute_iterations(total: int, workers: int) -> list[int]:
    base = total // workers
    rem = total % workers
    return [base + (1 if i < rem else 0) for i in range(workers)]


def launch_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else Path("Strat") / (
        "powell_direct_v2_no_share_search_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    base_path = Path(args.base_strategy)
    base_source = base_path.read_text(encoding="utf-8")
    base_params = parse_base_params(base_source)
    mutable_keys = parse_mutable_params(args.mutable_params)
    seed_from_progress = find_best_params_from_progress(args.seed_progress_dir) if args.seed_progress_dir else None
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
            "--worker-iterations",
            str(worker_iterations[wid]),
            "--hours",
            str(args.hours),
            "--base-strategy",
            str(base_path),
            "--seed-progress-dir",
            str(args.seed_progress_dir),
            "--target-fee-bps",
            str(args.target_fee_bps),
            "--quick-sims",
            str(args.quick_sims),
            "--refine-sims",
            str(args.refine_sims),
            "--refine-every",
            str(args.refine_every),
            "--sig-z",
            str(args.sig_z),
            "--min-delta-edge",
            str(args.min_delta_edge),
            "--seed",
            str(args.seed + wid * 100_003),
            "--mutable-params",
            args.mutable_params,
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
        "base_params": asdict(base_params),
        "seed_progress_dir": args.seed_progress_dir,
        "seed_best_from_progress": seed_from_progress,
        "out_dir": str(out_dir),
        "workers": args.workers,
        "hours_cap": args.hours,
        "iterations_total": args.iterations,
        "iterations_per_worker": worker_iterations,
        "target_fee_bps": args.target_fee_bps,
        "quick_sims": args.quick_sims,
        "refine_sims": args.refine_sims,
        "refine_every": args.refine_every,
        "sig_z": args.sig_z,
        "min_delta_edge": args.min_delta_edge,
        "mutable_params": mutable_keys,
        "algorithm": "asynchronous evolutionary search (wide bounds, 60% random exploration, periodic refine)",
        "objective": "avg_edge_only",
        "processes": procs,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Manifest: {manifest_path}")
    return 0


def collect_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    bests = []
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
        return float(entry[1].get("refine_score", float("-inf")))

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
    print(f"Score: {best_data.get('refine_score')}")
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
