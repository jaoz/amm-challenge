"""Deterministic adaptive Python strategy for simulator-side prototyping."""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any

from amm_competition.core.interfaces import AMMStrategy
from amm_competition.core.trade import FeeQuote, TradeInfo
from sim_lab.config import StrategyParams

EPS = 1e-12


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_step(current: float, target: float, max_delta: float) -> float:
    if target > current + max_delta:
        return current + max_delta
    if target < current - max_delta:
        return current - max_delta
    return target


def _ewma(old: float, observed: float, alpha: float) -> float:
    a = _clamp(alpha, 0.0, 1.0)
    if a <= 0.0:
        return old
    return ((1.0 - a) * old) + (a * observed)


def _abs_log_ratio(a: float, b: float) -> float:
    return abs(math.log(max(a, EPS) / max(b, EPS)))


class DeterministicAdaptiveStrategy(AMMStrategy):
    """Single-controller deterministic policy for Python-side optimization."""

    def __init__(self, params: StrategyParams | None = None):
        self.params = params if params is not None else StrategyParams()
        self._reset_state()
        self._initialized = False

    def _reset_state(self) -> None:
        self.bid_fee = 0.0
        self.ask_fee = 0.0
        self.prev_bid_for_sensitivity = 0.0
        self.prev_ask_for_sensitivity = 0.0

        self.p_low = 100.0
        self.p_high = 100.0
        self.p_step = 100.0
        self.prev_p_step = 100.0
        self.prev_spot = 100.0
        self.anchored_this_step = False

        self.lambda_hat = self.params.lambda_prior
        self.arb_hat = 0.5
        self.vol_hat = 0.001
        self.stale_hat = 0.0
        self.order_size_hat = self.params.order_size_prior
        self.inventory_signed = 0.0

        self.s_buy_base = 0.5
        self.s_sell_base = 0.5
        self.kappa_buy = self.params.kappa_prior
        self.kappa_sell = self.params.kappa_prior

        self.last_timestamp = -1
        self.last_gap = 1
        self.step_trades = 0
        self.last_probable_arb = False
        self.last_rel_move = 0.0

    def _to_fee_quote(self, bid_fee: float, ask_fee: float) -> FeeQuote:
        min_fee = self.params.min_fee_bps / 10_000.0
        max_fee = self.params.max_fee_bps / 10_000.0
        bid = _clamp(bid_fee, min_fee, max_fee)
        ask = _clamp(ask_fee, min_fee, max_fee)
        return FeeQuote(
            bid_fee=Decimal(str(bid)),
            ask_fee=Decimal(str(ask)),
        )

    def after_initialize(self, initial_x: Decimal, initial_y: Decimal) -> FeeQuote:
        self._reset_state()
        x = float(initial_x)
        y = float(initial_y)
        p0 = (y / x) if x > EPS else 100.0

        self.p_step = p0
        self.prev_p_step = p0
        self.prev_spot = p0
        self.p_low = p0 * (1.0 - self.params.collapse_reopen_eps)
        self.p_high = p0 * (1.0 + self.params.collapse_reopen_eps)

        opening_fee = self.params.center_fee_bps / 10_000.0
        self.bid_fee = opening_fee
        self.ask_fee = opening_fee
        self.prev_bid_for_sensitivity = opening_fee
        self.prev_ask_for_sensitivity = opening_fee

        self._initialized = True
        return self._to_fee_quote(self.bid_fee, self.ask_fee)

    def _on_new_timestamp(self, timestamp: int) -> None:
        if self.last_timestamp >= 0:
            gap = max(1, timestamp - self.last_timestamp)
            self.last_gap = gap
            sigma_hat = max(self.vol_hat, 0.00001)
            slack = math.exp(self.params.c_sigma * sigma_hat * math.sqrt(gap))
            self.p_low /= max(slack, 1.0)
            self.p_high *= max(slack, 1.0)
            self.prev_p_step = self.p_step
        else:
            self.last_gap = 1

        self.anchored_this_step = False
        self.step_trades = 0

    def _update_hidden_price_filter(self, *, side: str, spot: float, probable_arb: bool) -> None:
        bid_gamma = max(1.0 - self.bid_fee, EPS)
        ask_gamma = max(1.0 - self.ask_fee, EPS)

        l_tight = spot * bid_gamma
        u_tight = spot / ask_gamma
        if probable_arb:
            self.p_low = max(self.p_low, l_tight)
            self.p_high = min(self.p_high, u_tight)
        else:
            weak = self.params.eps_weak
            self.p_low = max(self.p_low, l_tight * (1.0 - weak))
            self.p_high = min(self.p_high, u_tight * (1.0 + weak))

        if self.p_low > self.p_high:
            mid = math.sqrt(max(self.p_low, EPS) * max(self.p_high, EPS))
            eps = self.params.collapse_reopen_eps
            self.p_low = mid * (1.0 - eps)
            self.p_high = mid * (1.0 + eps)

        mid = math.sqrt(max(self.p_low, EPS) * max(self.p_high, EPS))
        if probable_arb and not self.anchored_this_step:
            anchor = l_tight if side == "buy" else u_tight
            w = _clamp(self.params.w_arb_anchor, 0.0, 1.0)
            self.p_step = math.exp(
                ((1.0 - w) * math.log(max(mid, EPS))) + (w * math.log(max(anchor, EPS)))
            )
            self.anchored_this_step = True
        else:
            smooth = _clamp(self.params.mid_smoothing, 0.0, 1.0)
            self.p_step = math.exp(
                ((1.0 - smooth) * math.log(max(self.p_step, EPS)))
                + (smooth * math.log(max(mid, EPS)))
            )

    def _classify_probable_arb(self, *, side: str, spot: float) -> bool:
        anchor = spot * (1.0 - self.bid_fee) if side == "buy" else spot / max(1.0 - self.ask_fee, EPS)
        rel_anchor = _abs_log_ratio(anchor, self.p_step)
        rel_move = _abs_log_ratio(spot, self.prev_spot)
        direction_ok = (side == "buy" and spot <= self.prev_spot) or (side == "sell" and spot >= self.prev_spot)

        self.last_rel_move = rel_move
        return (
            self.step_trades == 1
            and direction_ok
            and rel_anchor <= self.params.arb_anchor_gate
            and rel_move >= self.params.arb_move_floor
        )

    def _update_route_sensitivity(self, *, side: str, amount_y: float) -> None:
        obs_share = _clamp(amount_y / max(self.order_size_hat, EPS), 0.0, 1.0)

        if side == "sell":
            prev_base = self.s_buy_base
            self.s_buy_base = _ewma(self.s_buy_base, obs_share, self.params.alpha_share)
            d_fee = self.ask_fee - self.prev_ask_for_sensitivity
            if 0.00001 < abs(d_fee) <= 0.0025:
                slope = (obs_share - prev_base) / max(-d_fee, EPS)
                slope = _clamp(slope, 0.1, 120.0)
                self.kappa_buy = _ewma(self.kappa_buy, slope, self.params.alpha_kappa)
        else:
            prev_base = self.s_sell_base
            self.s_sell_base = _ewma(self.s_sell_base, obs_share, self.params.alpha_share)
            d_fee = self.bid_fee - self.prev_bid_for_sensitivity
            if 0.00001 < abs(d_fee) <= 0.0025:
                slope = (obs_share - prev_base) / max(-d_fee, EPS)
                slope = _clamp(slope, 0.1, 120.0)
                self.kappa_sell = _ewma(self.kappa_sell, slope, self.params.alpha_kappa)

        self.prev_bid_for_sensitivity = self.bid_fee
        self.prev_ask_for_sensitivity = self.ask_fee

    def _score_candidate(self, bid_bps: float, ask_bps: float) -> float:
        bid = bid_bps / 10_000.0
        ask = ask_bps / 10_000.0
        norm_fee = self.params.normalizer_fee_bps / 10_000.0

        s_buy_hat = _clamp(self.s_buy_base + self.kappa_buy * (norm_fee - ask), 0.0, 1.0)
        s_sell_hat = _clamp(self.s_sell_base + self.kappa_sell * (norm_fee - bid), 0.0, 1.0)

        expected_retail = self.lambda_hat * self.order_size_hat * ((s_buy_hat * ask) + (s_sell_hat * bid))
        spread = 0.5 * (bid + ask)
        expected_arb = self.arb_hat * self.order_size_hat * max(0.0, self.stale_hat - spread)

        current_bid_bps = self.bid_fee * 10_000.0
        current_ask_bps = self.ask_fee * 10_000.0
        fee_jump = (abs(bid_bps - current_bid_bps) + abs(ask_bps - current_ask_bps)) / 10_000.0
        spread_to_norm = abs(spread - norm_fee)
        inv_abs = abs(self.inventory_signed)

        # Penalize asymmetry that pushes inventory away from balance.
        asym_sign = ask_bps - bid_bps
        signed_push = self.inventory_signed * asym_sign
        inv_signed_penalty = max(0.0, signed_push)

        vhat = (
            self.params.value_penalty_arb * self.arb_hat
            + self.params.value_penalty_stale * self.stale_hat
            + self.params.value_penalty_inv_abs * inv_abs
            + self.params.value_penalty_inv_signed * inv_signed_penalty
            + self.params.value_penalty_spread * spread_to_norm
            + self.params.value_penalty_jump * fee_jump
        )
        return expected_retail - expected_arb - vhat

    def _choose_next_quote(self) -> tuple[float, float]:
        min_bps = self.params.min_fee_bps
        max_bps = self.params.max_fee_bps
        max_delta = self.params.max_step_change_bps
        max_asym = self.params.max_asym_bps

        center = self.params.center_fee_bps
        center += 10.0 * (self.arb_hat - 0.5)
        center += 8.0 * max(0.0, self.params.lambda_prior - self.lambda_hat)
        center -= 12.0 * max(0.0, self.lambda_hat - self.params.lambda_prior)
        center -= 5_000.0 * self.stale_hat
        center = _clamp(center, min_bps, max_bps)

        # If inventory is long X (positive), tilt toward selling X: lower ask / higher bid.
        inv_tilt = _clamp(self.inventory_signed * max_asym, -max_asym, max_asym)

        current_bid_bps = self.bid_fee * 10_000.0
        current_ask_bps = self.ask_fee * 10_000.0
        best_bid = current_bid_bps
        best_ask = current_ask_bps
        best_score = -math.inf

        for base_shift in self.params.action_grid_bps:
            base = _clamp(center + base_shift, min_bps, max_bps)
            for asym_shift in self.params.asym_grid_bps:
                asym = _clamp(asym_shift + inv_tilt, -max_asym, max_asym)
                target_bid = _clamp(base + asym, min_bps, max_bps)
                target_ask = _clamp(base - asym, min_bps, max_bps)

                bid_bps = _clamp_step(current_bid_bps, target_bid, max_delta)
                ask_bps = _clamp_step(current_ask_bps, target_ask, max_delta)
                score = self._score_candidate(bid_bps, ask_bps)

                if score > best_score + 1e-15:
                    best_score = score
                    best_bid = bid_bps
                    best_ask = ask_bps

        return best_bid, best_ask

    def after_swap(self, trade: TradeInfo) -> FeeQuote:
        if not self._initialized:
            raise RuntimeError("Strategy not initialized. Call after_initialize first.")

        ts = int(trade.timestamp)
        if ts != self.last_timestamp:
            self._on_new_timestamp(ts)
        self.step_trades += 1

        reserve_x = float(trade.reserve_x)
        reserve_y = float(trade.reserve_y)
        spot = reserve_y / max(reserve_x, EPS)
        side = str(trade.side)
        amount_y = float(trade.amount_y)

        probable_arb = self._classify_probable_arb(side=side, spot=spot)
        self.last_probable_arb = probable_arb
        self._update_hidden_price_filter(side=side, spot=spot, probable_arb=probable_arb)

        stale = _abs_log_ratio(spot, self.p_step)
        self.stale_hat = _ewma(self.stale_hat, stale, self.params.alpha_vol)
        self.vol_hat = _ewma(self.vol_hat, stale, self.params.alpha_vol)
        self.arb_hat = _ewma(self.arb_hat, 1.0 if probable_arb else 0.0, self.params.alpha_arb)

        obs_lambda = 1.0 / max(float(self.last_gap), 1.0)
        self.lambda_hat = _ewma(self.lambda_hat, obs_lambda, self.params.alpha_lambda)
        self.order_size_hat = _ewma(self.order_size_hat, amount_y, self.params.alpha_order)
        self._update_route_sensitivity(side=side, amount_y=amount_y)

        value_x = reserve_x * self.p_step
        self.inventory_signed = (value_x - reserve_y) / max(value_x + reserve_y, EPS)

        next_bid_bps, next_ask_bps = self._choose_next_quote()
        self.bid_fee = next_bid_bps / 10_000.0
        self.ask_fee = next_ask_bps / 10_000.0

        self.prev_spot = spot
        self.last_timestamp = ts
        return self._to_fee_quote(self.bid_fee, self.ask_fee)

    def snapshot(self) -> dict[str, Any]:
        """Return current internal state for tracing/debug plots."""
        return {
            "p_low": self.p_low,
            "p_high": self.p_high,
            "p_step": self.p_step,
            "lambda_hat": self.lambda_hat,
            "arb_hat": self.arb_hat,
            "vol_hat": self.vol_hat,
            "stale_hat": self.stale_hat,
            "inventory_signed": self.inventory_signed,
            "s_buy_base": self.s_buy_base,
            "s_sell_base": self.s_sell_base,
            "kappa_buy": self.kappa_buy,
            "kappa_sell": self.kappa_sell,
            "bid_fee_bps": self.bid_fee * 10_000.0,
            "ask_fee_bps": self.ask_fee * 10_000.0,
            "last_gap": self.last_gap,
            "step_trades": self.step_trades,
            "last_probable_arb": self.last_probable_arb,
            "last_rel_move": self.last_rel_move,
        }

    def get_name(self) -> str:
        return "DeterministicAdaptiveStrategy"
