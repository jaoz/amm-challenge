"""Deterministic adaptive Python strategy for simulator-side prototyping."""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any

from amm_competition.core.interfaces import AMMStrategy
from amm_competition.core.trade import FeeQuote, TradeInfo
from sim_lab.config import StrategyParams

EPS = 1e-12
MIN_ROUTE_AMOUNT = 1e-4


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


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _log_blend(current: float, target: float, alpha: float) -> float:
    c = max(current, EPS)
    t = max(target, EPS)
    a = _clamp(alpha, 0.0, 1.0)
    return math.exp(((1.0 - a) * math.log(c)) + (a * math.log(t)))


def _bounded_log_update(current: float, target: float, alpha: float, max_move_bps: float) -> float:
    c = max(current, EPS)
    t = max(target, EPS)
    a = _clamp(alpha, 0.0, 1.0)
    blended = _log_blend(c, t, a)

    max_log_move = max(0.0, max_move_bps) / 10_000.0
    if max_log_move <= 0.0:
        return blended

    move = math.log(max(blended, EPS) / c)
    move = _clamp(move, -max_log_move, max_log_move)
    return c * math.exp(move)


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
        self.buy_prob_hat = 0.5
        self.last_expected_retail_edge = 0.0
        self.last_expected_capture_share = 0.5
        self.last_inferred_total_order_y = self.params.order_size_prior
        self.last_inferred_capture_share = 0.5

        self.s_buy_base = 0.5
        self.s_sell_base = 0.5
        self.r_buy_hat = 1.0
        self.r_sell_hat = 1.0
        self.kappa_buy = self.params.kappa_prior
        self.kappa_sell = self.params.kappa_prior

        self.last_timestamp = -1
        self.last_gap = 1
        self.step_trades = 0
        self.last_probable_arb = False
        self.last_rel_move = 0.0
        self.same_step_retail_signal = False
        self.last_p_step_move_bps = 0.0
        self.last_small_trade_arb_signal = False
        self.last_size_tail_prob = 1.0
        self.last_size_tail_signal = False

        self.our_x = 100.0
        self.our_y = 10_000.0
        self.norm_x_hat = 100.0
        self.norm_y_hat = 10_000.0
        self.norm_k = self.norm_x_hat * self.norm_y_hat
        self.last_norm_projection_spot = self.norm_y_hat / self.norm_x_hat

    def _to_fee_quote(self, bid_fee: float, ask_fee: float) -> FeeQuote:
        # Allow explicit 0% fee when one-sided shield mode chooses it for rebalancing.
        min_fee = 0.0
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

        opening_fee_bps = _clamp(
            self.params.opening_fee_bps,
            self.params.min_fee_bps,
            self.params.max_fee_bps,
        )
        opening_fee = opening_fee_bps / 10_000.0
        self.bid_fee = opening_fee
        self.ask_fee = opening_fee
        self.prev_bid_for_sensitivity = opening_fee
        self.prev_ask_for_sensitivity = opening_fee
        self.our_x = x
        self.our_y = y
        self.norm_x_hat = x
        self.norm_y_hat = y
        self.norm_k = max(x * y, EPS)
        self.last_norm_projection_spot = p0

        self._initialized = True
        return self._to_fee_quote(self.bid_fee, self.ask_fee)

    def _set_normalizer_spot(self, spot: float) -> None:
        s = max(spot, EPS)
        k = max(self.norm_k, EPS)
        x = math.sqrt(k / s)
        y = k / max(x, EPS)
        self.norm_x_hat = x
        self.norm_y_hat = y

    def _blend_normalizer_spot(self, target_spot: float, alpha: float) -> None:
        curr_spot = self.norm_y_hat / max(self.norm_x_hat, EPS)
        next_spot = _log_blend(curr_spot, target_spot, alpha)
        self._set_normalizer_spot(next_spot)

    def _project_normalizer_arb_toward_internal(self, *, confidence: float) -> None:
        fee = self.params.normalizer_fee_bps / 10_000.0
        gamma = max(1.0 - fee, EPS)
        spot = self.norm_y_hat / max(self.norm_x_hat, EPS)
        p_ref = max(self.p_step, EPS)
        lower = spot * gamma
        upper = spot / gamma

        target_spot = spot
        if p_ref > upper:
            target_spot = p_ref * gamma
        elif p_ref < lower:
            target_spot = p_ref / gamma
        else:
            self.last_norm_projection_spot = spot
            return

        alpha = _clamp(
            self.params.normalizer_pretrade_arb_alpha * _clamp(confidence, 0.0, 1.0),
            0.0,
            1.0,
        )
        self._blend_normalizer_spot(target_spot, alpha)
        self.last_norm_projection_spot = target_spot

    def _amm_x_out_for_y(
        self,
        *,
        reserve_x: float,
        reserve_y: float,
        ask_fee: float,
        amount_y_in: float,
    ) -> float:
        if amount_y_in <= MIN_ROUTE_AMOUNT:
            return 0.0
        gamma = max(1.0 - ask_fee, EPS)
        net_y = amount_y_in * gamma
        new_y = reserve_y + net_y
        k = reserve_x * reserve_y
        new_x = k / max(new_y, EPS)
        return max(0.0, reserve_x - new_x)

    def _amm_y_out_for_x(
        self,
        *,
        reserve_x: float,
        reserve_y: float,
        bid_fee: float,
        amount_x_in: float,
    ) -> float:
        if amount_x_in <= MIN_ROUTE_AMOUNT:
            return 0.0
        gamma = max(1.0 - bid_fee, EPS)
        net_x = amount_x_in * gamma
        new_x = reserve_x + net_x
        k = reserve_x * reserve_y
        new_y = k / max(new_x, EPS)
        return max(0.0, reserve_y - new_y)

    def _split_buy_two_amms(
        self,
        *,
        total_y: float,
        submission_x: float,
        submission_y: float,
        submission_ask_fee: float,
        normalizer_x: float,
        normalizer_y: float,
        normalizer_ask_fee: float,
    ) -> tuple[float, float]:
        if total_y <= MIN_ROUTE_AMOUNT:
            return 0.0, 0.0
        gamma_sub = max(1.0 - submission_ask_fee, EPS)
        gamma_norm = max(1.0 - normalizer_ask_fee, EPS)
        a_sub = math.sqrt(max(submission_x * gamma_sub * submission_y, EPS))
        a_norm = math.sqrt(max(normalizer_x * gamma_norm * normalizer_y, EPS))
        if a_norm <= EPS:
            return total_y, 0.0
        r = a_sub / a_norm
        denom = gamma_sub + (r * gamma_norm)
        if denom <= EPS:
            y_sub = 0.5 * total_y
        else:
            y_sub = (r * (normalizer_y + (gamma_norm * total_y)) - submission_y) / denom
        y_sub = _clamp(y_sub, 0.0, total_y)
        y_norm = max(0.0, total_y - y_sub)
        if y_sub <= MIN_ROUTE_AMOUNT:
            return 0.0, total_y
        if y_norm <= MIN_ROUTE_AMOUNT:
            return total_y, 0.0
        return y_sub, y_norm

    def _split_sell_two_amms(
        self,
        *,
        total_x: float,
        submission_x: float,
        submission_y: float,
        submission_bid_fee: float,
        normalizer_x: float,
        normalizer_y: float,
        normalizer_bid_fee: float,
    ) -> tuple[float, float]:
        if total_x <= MIN_ROUTE_AMOUNT:
            return 0.0, 0.0
        gamma_sub = max(1.0 - submission_bid_fee, EPS)
        gamma_norm = max(1.0 - normalizer_bid_fee, EPS)
        b_sub = math.sqrt(max(submission_y * gamma_sub * submission_x, EPS))
        b_norm = math.sqrt(max(normalizer_y * gamma_norm * normalizer_x, EPS))
        if b_norm <= EPS:
            return total_x, 0.0
        r = b_sub / b_norm
        denom = gamma_sub + (r * gamma_norm)
        if denom <= EPS:
            x_sub = 0.5 * total_x
        else:
            x_sub = (r * (normalizer_x + (gamma_norm * total_x)) - submission_x) / denom
        x_sub = _clamp(x_sub, 0.0, total_x)
        x_norm = max(0.0, total_x - x_sub)
        if x_sub <= MIN_ROUTE_AMOUNT:
            return 0.0, total_x
        if x_norm <= MIN_ROUTE_AMOUNT:
            return total_x, 0.0
        return x_sub, x_norm

    def _infer_buy_total_y_from_submission_fill(
        self,
        *,
        y_submission: float,
        submission_x: float,
        submission_y: float,
        submission_ask_fee: float,
        normalizer_x: float,
        normalizer_y: float,
        normalizer_ask_fee: float,
    ) -> tuple[float, float]:
        y_sub = max(y_submission, 0.0)
        if y_sub <= MIN_ROUTE_AMOUNT:
            return 0.0, 0.0

        gamma_sub = max(1.0 - submission_ask_fee, EPS)
        gamma_norm = max(1.0 - normalizer_ask_fee, EPS)
        a_sub = math.sqrt(max(submission_x * gamma_sub * submission_y, EPS))
        a_norm = math.sqrt(max(normalizer_x * gamma_norm * normalizer_y, EPS))
        if a_norm <= EPS:
            return y_sub, 0.0
        r = a_sub / a_norm
        denom = gamma_sub + (r * gamma_norm)
        if denom <= EPS:
            return y_sub, 0.0

        # For two AMMs, unclamped optimal split is linear in total size:
        # y_sub_unclamped = a + b * totalY.
        a = ((r * normalizer_y) - submission_y) / denom
        b = (r * gamma_norm) / denom
        if b <= EPS:
            return y_sub, 0.0

        total_int = (y_sub - a) / b
        if total_int > (y_sub + MIN_ROUTE_AMOUNT):
            y_unclamped = a + (b * total_int)
            if MIN_ROUTE_AMOUNT < y_unclamped < (total_int - MIN_ROUTE_AMOUNT):
                y_norm = max(0.0, total_int - y_sub)
                return max(total_int, y_sub), y_norm

        # Non-interior case with y_sub > 0 means clamped to full flow on submission.
        return y_sub, 0.0

    def _infer_sell_total_x_from_submission_fill(
        self,
        *,
        x_submission: float,
        submission_x: float,
        submission_y: float,
        submission_bid_fee: float,
        normalizer_x: float,
        normalizer_y: float,
        normalizer_bid_fee: float,
    ) -> tuple[float, float]:
        x_sub = max(x_submission, 0.0)
        if x_sub <= MIN_ROUTE_AMOUNT:
            return 0.0, 0.0

        gamma_sub = max(1.0 - submission_bid_fee, EPS)
        gamma_norm = max(1.0 - normalizer_bid_fee, EPS)
        b_sub = math.sqrt(max(submission_y * gamma_sub * submission_x, EPS))
        b_norm = math.sqrt(max(normalizer_y * gamma_norm * normalizer_x, EPS))
        if b_norm <= EPS:
            return x_sub, 0.0
        r = b_sub / b_norm
        denom = gamma_sub + (r * gamma_norm)
        if denom <= EPS:
            return x_sub, 0.0

        # For two AMMs, unclamped optimal split is linear in total size:
        # x_sub_unclamped = a + b * totalX.
        a = ((r * normalizer_x) - submission_x) / denom
        b = (r * gamma_norm) / denom
        if b <= EPS:
            return x_sub, 0.0

        total_int = (x_sub - a) / b
        if total_int > (x_sub + MIN_ROUTE_AMOUNT):
            x_unclamped = a + (b * total_int)
            if MIN_ROUTE_AMOUNT < x_unclamped < (total_int - MIN_ROUTE_AMOUNT):
                x_norm = max(0.0, total_int - x_sub)
                return max(total_int, x_sub), x_norm

        # Non-interior case with x_sub > 0 means clamped to full flow on submission.
        return x_sub, 0.0

    def _apply_normalizer_buy_with_y(self, amount_y: float, alpha: float) -> None:
        y_in = max(amount_y, 0.0)
        if y_in <= MIN_ROUTE_AMOUNT:
            return
        fee = self.params.normalizer_fee_bps / 10_000.0
        gamma = max(1.0 - fee, EPS)
        k = max(self.norm_k, EPS)
        new_y = self.norm_y_hat + (y_in * gamma)
        new_x = k / max(new_y, EPS)
        target_spot = new_y / max(new_x, EPS)
        self._blend_normalizer_spot(target_spot, alpha)

    def _apply_normalizer_buy_x(self, amount_x: float, alpha: float) -> None:
        x_in = max(amount_x, 0.0)
        if x_in <= MIN_ROUTE_AMOUNT:
            return
        fee = self.params.normalizer_fee_bps / 10_000.0
        gamma = max(1.0 - fee, EPS)
        k = max(self.norm_k, EPS)
        new_x = self.norm_x_hat + (x_in * gamma)
        new_y = k / max(new_x, EPS)
        target_spot = new_y / max(new_x, EPS)
        self._blend_normalizer_spot(target_spot, alpha)

    def _estimate_and_apply_normalizer_retail_fill(
        self,
        *,
        side: str,
        amount_x_submission: float,
        amount_y_submission: float,
        submission_x_pre: float,
        submission_y_pre: float,
    ) -> tuple[float, float]:
        norm_fee = self.params.normalizer_fee_bps / 10_000.0
        alpha = _clamp(self.params.normalizer_state_alpha, 0.0, 1.0)
        if side == "sell":
            total_y, norm_y = self._infer_buy_total_y_from_submission_fill(
                y_submission=amount_y_submission,
                submission_x=submission_x_pre,
                submission_y=submission_y_pre,
                submission_ask_fee=self.ask_fee,
                normalizer_x=self.norm_x_hat,
                normalizer_y=self.norm_y_hat,
                normalizer_ask_fee=norm_fee,
            )
            self._apply_normalizer_buy_with_y(norm_y, alpha)
            share = amount_y_submission / max(total_y, EPS)
            return max(total_y, amount_y_submission), _clamp(share, 0.0, 1.0)

        total_x, norm_x = self._infer_sell_total_x_from_submission_fill(
            x_submission=amount_x_submission,
            submission_x=submission_x_pre,
            submission_y=submission_y_pre,
            submission_bid_fee=self.bid_fee,
            normalizer_x=self.norm_x_hat,
            normalizer_y=self.norm_y_hat,
            normalizer_bid_fee=norm_fee,
        )
        self._apply_normalizer_buy_x(norm_x, alpha)
        total_y_equiv = max(total_x * max(self.p_step, EPS), amount_y_submission)
        share = amount_x_submission / max(total_x, EPS)
        return total_y_equiv, _clamp(share, 0.0, 1.0)

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
        self._project_normalizer_arb_toward_internal(confidence=max(0.25, self.arb_hat))

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
            target = math.exp(
                ((1.0 - w) * math.log(max(mid, EPS))) + (w * math.log(max(anchor, EPS)))
            )
            prev = self.p_step
            self.p_step = _bounded_log_update(
                current=self.p_step,
                target=target,
                alpha=1.0,
                max_move_bps=self.params.max_anchor_move_bps,
            )
            self.last_p_step_move_bps = _abs_log_ratio(self.p_step, prev) * 10_000.0
            self.anchored_this_step = True
        else:
            smooth = _clamp(self.params.mid_smoothing, 0.0, 1.0)
            if self.step_trades >= 2:
                smooth = min(smooth, _clamp(self.params.mid_smoothing_same_step, 0.0, 1.0))
            prev = self.p_step
            self.p_step = _bounded_log_update(
                current=self.p_step,
                target=mid,
                alpha=smooth,
                max_move_bps=self.params.max_mid_move_bps,
            )
            self.last_p_step_move_bps = _abs_log_ratio(self.p_step, prev) * 10_000.0

    def _retail_lognormal_tail_prob(self, amount_y: float) -> float:
        """Two-sided tail probability under retail size prior in Y terms."""
        y = max(amount_y, EPS)
        sigma = max(self.params.retail_size_sigma_assumed, 0.2)
        mean_hat = (
            (1.0 - self.params.retail_mean_blend) * self.params.retail_mean_prior_y
            + self.params.retail_mean_blend * max(self.order_size_hat, 0.5)
        )
        mean_hat = max(mean_hat, 0.5)
        mu = math.log(mean_hat) - 0.5 * sigma * sigma
        z = (math.log(y) - mu) / sigma
        cdf = _normal_cdf(z)
        tail_prob = 2.0 * min(cdf, 1.0 - cdf)
        return _clamp(tail_prob, 0.0, 1.0)

    def _classify_probable_arb(self, *, side: str, spot: float, amount_y: float) -> bool:
        if self.step_trades >= 2:
            # Timestamp-ordering leak: second+ callback within step is retail by construction.
            self.last_rel_move = _abs_log_ratio(spot, self.prev_spot)
            self.same_step_retail_signal = True
            self.last_small_trade_arb_signal = False
            self.last_size_tail_prob = 1.0
            self.last_size_tail_signal = False
            return False

        anchor = spot * (1.0 - self.bid_fee) if side == "buy" else spot / max(1.0 - self.ask_fee, EPS)
        rel_anchor = _abs_log_ratio(anchor, self.p_step)
        rel_move = _abs_log_ratio(spot, self.prev_spot)
        direction_ok = (side == "buy" and spot <= self.prev_spot) or (side == "sell" and spot >= self.prev_spot)
        size_ratio = amount_y / max(self.order_size_hat, EPS)
        small_abs = amount_y <= self.params.size_arb_abs_y
        small_rel = size_ratio <= self.params.size_arb_rel_ratio
        small_trade_signal = small_abs or small_rel
        tail_prob = self._retail_lognormal_tail_prob(amount_y)
        tail_signal = tail_prob <= self.params.size_tail_prob_gate
        tail_strength = 0.0
        if tail_signal and self.params.size_tail_prob_gate > EPS:
            tail_strength = _clamp(
                (self.params.size_tail_prob_gate - tail_prob) / self.params.size_tail_prob_gate,
                0.0,
                1.0,
            )

        base_arb = (
            direction_ok
            and rel_anchor <= self.params.arb_anchor_gate
            and rel_move >= self.params.arb_move_floor
        )

        relax_anchor_mult = 1.0
        relax_move_mult = 1.0
        if small_trade_signal:
            relax_anchor_mult = max(relax_anchor_mult, self.params.size_arb_relax_anchor_mult)
            relax_move_mult = min(relax_move_mult, self.params.size_arb_relax_move_mult)
        if tail_signal:
            tail_anchor_mult = 1.0 + tail_strength * (self.params.size_tail_relax_anchor_mult - 1.0)
            tail_move_mult = 1.0 - tail_strength * (1.0 - self.params.size_tail_relax_move_mult)
            relax_anchor_mult = max(relax_anchor_mult, tail_anchor_mult)
            relax_move_mult = min(relax_move_mult, tail_move_mult)

        size_signal = small_trade_signal or tail_signal
        size_relaxed_arb = (
            direction_ok
            and size_signal
            and rel_anchor <= (self.params.arb_anchor_gate * relax_anchor_mult)
            and rel_move >= (self.params.arb_move_floor * relax_move_mult)
        )

        self.same_step_retail_signal = False
        self.last_rel_move = rel_move
        self.last_small_trade_arb_signal = small_trade_signal
        self.last_size_tail_prob = tail_prob
        self.last_size_tail_signal = tail_signal
        return self.step_trades == 1 and (base_arb or size_relaxed_arb)

    def _update_route_sensitivity(self, *, side: str, captured_share: float) -> None:
        obs_share = _clamp(captured_share, 0.0, 1.0)

        if side == "sell":
            prev_base = self.s_buy_base
            self.s_buy_base = _ewma(self.s_buy_base, obs_share, self.params.alpha_share)
            self.r_buy_hat = _ewma(
                self.r_buy_hat,
                _clamp(self.s_buy_base / max(1.0 - self.s_buy_base, EPS), 0.1, 10.0),
                self.params.alpha_share,
            )
            d_fee = self.ask_fee - self.prev_ask_for_sensitivity
            if 0.00001 < abs(d_fee) <= 0.0025:
                slope = (obs_share - prev_base) / max(-d_fee, EPS)
                slope = _clamp(slope, 0.1, 120.0)
                self.kappa_buy = _ewma(self.kappa_buy, slope, self.params.alpha_kappa)
        else:
            prev_base = self.s_sell_base
            self.s_sell_base = _ewma(self.s_sell_base, obs_share, self.params.alpha_share)
            self.r_sell_hat = _ewma(
                self.r_sell_hat,
                _clamp(self.s_sell_base / max(1.0 - self.s_sell_base, EPS), 0.1, 10.0),
                self.params.alpha_share,
            )
            d_fee = self.bid_fee - self.prev_bid_for_sensitivity
            if 0.00001 < abs(d_fee) <= 0.0025:
                slope = (obs_share - prev_base) / max(-d_fee, EPS)
                slope = _clamp(slope, 0.1, 120.0)
                self.kappa_sell = _ewma(self.kappa_sell, slope, self.params.alpha_kappa)

        self.prev_bid_for_sensitivity = self.bid_fee
        self.prev_ask_for_sensitivity = self.ask_fee

    def _expected_next_retail_edge(self, *, bid_fee: float, ask_fee: float) -> tuple[float, float]:
        p_ref = max(self.p_step, EPS)
        order_y = max(self.order_size_hat, 0.2)
        norm_fee = self.params.normalizer_fee_bps / 10_000.0
        p_buy = _clamp(self.buy_prob_hat, 0.08, 0.92)
        p_sell = 1.0 - p_buy

        y_sub, _ = self._split_buy_two_amms(
            total_y=order_y,
            submission_x=self.our_x,
            submission_y=self.our_y,
            submission_ask_fee=ask_fee,
            normalizer_x=self.norm_x_hat,
            normalizer_y=self.norm_y_hat,
            normalizer_ask_fee=norm_fee,
        )
        buy_share = _clamp(y_sub / max(order_y, EPS), 0.0, 1.0)
        buy_edge = 0.0
        if y_sub > MIN_ROUTE_AMOUNT:
            x_out = self._amm_x_out_for_y(
                reserve_x=self.our_x,
                reserve_y=self.our_y,
                ask_fee=ask_fee,
                amount_y_in=y_sub,
            )
            buy_edge = y_sub - (x_out * p_ref)

        total_x = order_y / max(p_ref, EPS)
        x_sub, _ = self._split_sell_two_amms(
            total_x=total_x,
            submission_x=self.our_x,
            submission_y=self.our_y,
            submission_bid_fee=bid_fee,
            normalizer_x=self.norm_x_hat,
            normalizer_y=self.norm_y_hat,
            normalizer_bid_fee=norm_fee,
        )
        sell_share = _clamp(x_sub / max(total_x, EPS), 0.0, 1.0)
        sell_edge = 0.0
        if x_sub > MIN_ROUTE_AMOUNT:
            y_out = self._amm_y_out_for_x(
                reserve_x=self.our_x,
                reserve_y=self.our_y,
                bid_fee=bid_fee,
                amount_x_in=x_sub,
            )
            sell_edge = (x_sub * p_ref) - y_out

        per_order_edge = (p_buy * buy_edge) + (p_sell * sell_edge)
        expected_orders = _clamp(self.lambda_hat, 0.0, 2.5)
        expected_edge = expected_orders * per_order_edge
        expected_share = (p_buy * buy_share) + (p_sell * sell_share)
        return expected_edge, expected_share

    def _score_candidate(self, bid_bps: float, ask_bps: float) -> float:
        bid = bid_bps / 10_000.0
        ask = ask_bps / 10_000.0
        norm_fee = self.params.normalizer_fee_bps / 10_000.0

        expected_retail, expected_share = self._expected_next_retail_edge(
            bid_fee=bid,
            ask_fee=ask,
        )
        spread = 0.5 * (bid + ask)
        expected_arb = (
            self.params.edge_arb_scale
            * self.arb_hat
            * self.order_size_hat
            * max(0.0, self.stale_hat - spread)
        )

        current_bid_bps = self.bid_fee * 10_000.0
        current_ask_bps = self.ask_fee * 10_000.0
        fee_jump = (abs(bid_bps - current_bid_bps) + abs(ask_bps - current_ask_bps)) / 10_000.0
        spread_to_norm = abs(spread - norm_fee)
        inv_abs = abs(self.inventory_signed)
        lambda_gap = abs(self.lambda_hat - self.params.lambda_prior)

        # Penalize asymmetry that pushes inventory away from balance.
        asym_sign = ask_bps - bid_bps
        signed_push = self.inventory_signed * (asym_sign / 10_000.0)
        inv_signed_penalty = max(0.0, signed_push)
        inventory_penalty = self.params.edge_inventory_scale * inv_abs * self.order_size_hat

        vhat = (
            self.params.value_penalty_arb * self.arb_hat
            + self.params.value_penalty_stale * self.stale_hat
            + self.params.value_penalty_inv_abs * inv_abs
            + self.params.value_penalty_inv_signed * inv_signed_penalty
            + self.params.value_penalty_lambda_gap * lambda_gap
            + self.params.value_penalty_spread * spread_to_norm
            + self.params.value_penalty_jump * fee_jump
        )
        # Tiny capture-share tie-break to avoid overfitting to stale penalties.
        return expected_retail - expected_arb - inventory_penalty - vhat + (0.01 * expected_share)

    def _required_no_arb_fees_bps(self) -> tuple[float, float]:
        """Compute side-specific minimum fees implied by current spot vs internal fair value."""
        spot = self.our_y / max(self.our_x, EPS)
        p_ref = max(self.p_step, EPS)
        cap = 0.10  # 10% fee cap per challenge requirements.
        # No-arb interval: p_ref in [spot*(1-bid), spot/(1-ask)].
        bid_required = max(0.0, 1.0 - (p_ref / max(spot, EPS)))
        ask_required = max(0.0, 1.0 - (spot / p_ref))
        bid_required = min(bid_required, cap)
        ask_required = min(ask_required, cap)
        return bid_required * 10_000.0, ask_required * 10_000.0

    def _arb_shield_floors_bps(self) -> tuple[float, float, bool, bool]:
        min_bps = self.params.min_fee_bps
        max_bps = self.params.max_fee_bps
        if self.last_probable_arb:
            return min_bps, min_bps, False, False

        req_bid_bps, req_ask_bps = self._required_no_arb_fees_bps()
        trigger = max(self.params.arb_shield_trigger_bps, 0.0)
        buffer_bps = max(self.params.arb_shield_buffer_bps, 0.0)

        bid_active = req_bid_bps >= trigger
        ask_active = req_ask_bps >= trigger
        bid_floor = _clamp(req_bid_bps + buffer_bps, min_bps, max_bps) if bid_active else min_bps
        ask_floor = _clamp(req_ask_bps + buffer_bps, min_bps, max_bps) if ask_active else min_bps
        return bid_floor, ask_floor, bid_active, ask_active

    def _can_zero_opposite_side(
        self,
        *,
        bid_shield_active: bool,
        ask_shield_active: bool,
    ) -> tuple[bool, bool]:
        """Allow 0 on opposite side only if that flow direction compensates inventory imbalance."""
        inv_eps = 1e-4
        inv = self.inventory_signed
        # ask_shield_active means ask side is protected; opposite bid-side flow compensates only if inventory is short X.
        zero_bid = ask_shield_active and (not bid_shield_active) and (inv < -inv_eps)
        # bid_shield_active means bid side is protected; opposite ask-side flow compensates only if inventory is long X.
        zero_ask = bid_shield_active and (not ask_shield_active) and (inv > inv_eps)
        return zero_bid, zero_ask

    def _build_candidate_levels(self, current_bps: float) -> tuple[float, ...]:
        min_bps = self.params.min_fee_bps
        max_bps = self.params.max_fee_bps
        max_delta = self.params.max_step_change_bps
        levels: set[float] = {current_bps}
        for delta in self.params.action_grid_bps:
            target = _clamp(current_bps + delta, min_bps, max_bps)
            levels.add(_clamp_step(current_bps, target, max_delta))
        levels.add(_clamp_step(current_bps, min_bps, max_delta))
        levels.add(_clamp_step(current_bps, max_bps, max_delta))
        return tuple(sorted(levels))

    def _choose_next_quote(self) -> tuple[float, float]:
        max_asym = self.params.max_asym_bps
        current_bid_bps = self.bid_fee * 10_000.0
        current_ask_bps = self.ask_fee * 10_000.0
        bid_levels = set(self._build_candidate_levels(current_bid_bps))
        ask_levels = set(self._build_candidate_levels(current_ask_bps))
        bid_floor, ask_floor, bid_shield_active, ask_shield_active = self._arb_shield_floors_bps()
        zero_bid_ok, zero_ask_ok = self._can_zero_opposite_side(
            bid_shield_active=bid_shield_active,
            ask_shield_active=ask_shield_active,
        )

        if bid_shield_active:
            # Override step-size limit on exposed side for fast no-arb recovery.
            bid_levels.add(bid_floor)
            bid_levels = {x for x in bid_levels if x >= bid_floor - 1e-12}
        if ask_shield_active:
            # Override step-size limit on exposed side for fast no-arb recovery.
            ask_levels.add(ask_floor)
            ask_levels = {x for x in ask_levels if x >= ask_floor - 1e-12}
        if zero_bid_ok:
            bid_levels.add(0.0)
        if zero_ask_ok:
            ask_levels.add(0.0)

        bid_candidates = tuple(sorted(bid_levels))
        ask_candidates = tuple(sorted(ask_levels))

        best_bid = current_bid_bps
        best_ask = current_ask_bps
        best_score = -math.inf

        for bid_bps in bid_candidates:
            for ask_bps in ask_candidates:
                if bid_shield_active and bid_bps + 1e-12 < bid_floor:
                    continue
                if ask_shield_active and ask_bps + 1e-12 < ask_floor:
                    continue

                # Under large imbalance, protecting against arb has priority over inventory-tilt limits.
                if not (bid_shield_active or ask_shield_active) and abs(ask_bps - bid_bps) > (2.0 * max_asym):
                    continue
                score = self._score_candidate(bid_bps, ask_bps)
                if score > best_score + 1e-15:
                    best_score = score
                    best_bid = bid_bps
                    best_ask = ask_bps
                elif abs(score - best_score) <= 1e-15:
                    # Keep tie-breaking deterministic and centered near normalizer fee.
                    norm_bps = self.params.normalizer_fee_bps
                    cur_dist = abs(best_bid - norm_bps) + abs(best_ask - norm_bps)
                    nxt_dist = abs(bid_bps - norm_bps) + abs(ask_bps - norm_bps)
                    if nxt_dist < cur_dist:
                        best_bid = bid_bps
                        best_ask = ask_bps

        if best_score == -math.inf:
            best_bid = max(current_bid_bps, bid_floor) if bid_shield_active else current_bid_bps
            best_ask = max(current_ask_bps, ask_floor) if ask_shield_active else current_ask_bps

        return best_bid, best_ask

    def after_swap(self, trade: TradeInfo) -> FeeQuote:
        if not self._initialized:
            raise RuntimeError("Strategy not initialized. Call after_initialize first.")

        ts = int(trade.timestamp)
        if ts != self.last_timestamp:
            self._on_new_timestamp(ts)
        self.step_trades += 1

        submission_x_pre = self.our_x
        submission_y_pre = self.our_y

        reserve_x = float(trade.reserve_x)
        reserve_y = float(trade.reserve_y)
        spot = reserve_y / max(reserve_x, EPS)
        side = str(trade.side)
        amount_x = float(trade.amount_x)
        amount_y = float(trade.amount_y)

        probable_arb = self._classify_probable_arb(side=side, spot=spot, amount_y=amount_y)
        self.last_probable_arb = probable_arb
        self._update_hidden_price_filter(side=side, spot=spot, probable_arb=probable_arb)

        stale = _abs_log_ratio(spot, self.p_step)
        stale_obs = stale if not self.same_step_retail_signal else (0.5 * stale)
        self.stale_hat = _ewma(self.stale_hat, stale_obs, self.params.alpha_vol)
        self.vol_hat = _ewma(self.vol_hat, stale_obs, self.params.alpha_vol)

        arb_alpha = self.params.alpha_arb
        if self.same_step_retail_signal:
            arb_alpha = max(arb_alpha, self.params.alpha_arb_same_step)
        self.arb_hat = _ewma(self.arb_hat, 1.0 if probable_arb else 0.0, arb_alpha)

        obs_lambda = 1.0 / max(float(self.last_gap), 1.0)
        lambda_alpha = self.params.alpha_lambda
        if self.same_step_retail_signal:
            obs_lambda += self.params.lambda_same_step_bonus * float(self.step_trades - 1)
            lambda_alpha = max(lambda_alpha, self.params.alpha_lambda_same_step)
        self.lambda_hat = _ewma(self.lambda_hat, obs_lambda, lambda_alpha)

        inferred_total_order_y = amount_y
        inferred_capture_share = 1.0
        if not probable_arb:
            inferred_total_order_y, inferred_capture_share = self._estimate_and_apply_normalizer_retail_fill(
                side=side,
                amount_x_submission=amount_x,
                amount_y_submission=amount_y,
                submission_x_pre=submission_x_pre,
                submission_y_pre=submission_y_pre,
            )
            self.buy_prob_hat = _ewma(
                self.buy_prob_hat,
                1.0 if side == "sell" else 0.0,
                self.params.alpha_share,
            )
            self._update_route_sensitivity(side=side, captured_share=inferred_capture_share)
        elif self.step_trades == 1:
            # If our first callback in step is arb, a normalizer arb may execute later in this step.
            self._project_normalizer_arb_toward_internal(confidence=1.0)

        self.order_size_hat = _ewma(
            self.order_size_hat,
            max(inferred_total_order_y, MIN_ROUTE_AMOUNT),
            self.params.alpha_order,
        )
        self.last_inferred_total_order_y = inferred_total_order_y
        self.last_inferred_capture_share = inferred_capture_share

        self.our_x = reserve_x
        self.our_y = reserve_y

        value_x = reserve_x * self.p_step
        self.inventory_signed = (value_x - reserve_y) / max(value_x + reserve_y, EPS)

        next_bid_bps, next_ask_bps = self._choose_next_quote()
        expected_edge, expected_share = self._expected_next_retail_edge(
            bid_fee=next_bid_bps / 10_000.0,
            ask_fee=next_ask_bps / 10_000.0,
        )
        self.last_expected_retail_edge = expected_edge
        self.last_expected_capture_share = expected_share
        self.bid_fee = next_bid_bps / 10_000.0
        self.ask_fee = next_ask_bps / 10_000.0

        self.prev_spot = spot
        self.last_timestamp = ts
        return self._to_fee_quote(self.bid_fee, self.ask_fee)

    def snapshot(self) -> dict[str, Any]:
        """Return current internal state for tracing/debug plots."""
        req_bid_bps, req_ask_bps = self._required_no_arb_fees_bps()
        return {
            "p_low": self.p_low,
            "p_high": self.p_high,
            "p_step": self.p_step,
            "lambda_hat": self.lambda_hat,
            "arb_hat": self.arb_hat,
            "vol_hat": self.vol_hat,
            "stale_hat": self.stale_hat,
            "inventory_signed": self.inventory_signed,
            "buy_prob_hat": self.buy_prob_hat,
            "s_buy_base": self.s_buy_base,
            "s_sell_base": self.s_sell_base,
            "r_buy_hat": self.r_buy_hat,
            "r_sell_hat": self.r_sell_hat,
            "kappa_buy": self.kappa_buy,
            "kappa_sell": self.kappa_sell,
            "our_x": self.our_x,
            "our_y": self.our_y,
            "norm_x_hat": self.norm_x_hat,
            "norm_y_hat": self.norm_y_hat,
            "norm_spot_hat": self.norm_y_hat / max(self.norm_x_hat, EPS),
            "last_norm_projection_spot": self.last_norm_projection_spot,
            "bid_fee_bps": self.bid_fee * 10_000.0,
            "ask_fee_bps": self.ask_fee * 10_000.0,
            "last_gap": self.last_gap,
            "step_trades": self.step_trades,
            "last_probable_arb": self.last_probable_arb,
            "last_rel_move": self.last_rel_move,
            "same_step_retail_signal": self.same_step_retail_signal,
            "last_p_step_move_bps": self.last_p_step_move_bps,
            "last_small_trade_arb_signal": self.last_small_trade_arb_signal,
            "last_size_tail_prob": self.last_size_tail_prob,
            "last_size_tail_signal": self.last_size_tail_signal,
            "last_expected_retail_edge": self.last_expected_retail_edge,
            "last_expected_capture_share": self.last_expected_capture_share,
            "last_inferred_total_order_y": self.last_inferred_total_order_y,
            "last_inferred_capture_share": self.last_inferred_capture_share,
            "required_no_arb_bid_bps": req_bid_bps,
            "required_no_arb_ask_bps": req_ask_bps,
        }

    def get_name(self) -> str:
        return "DeterministicAdaptiveStrategy"
