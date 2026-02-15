"""Deterministic pure-Python simulator and trace exporters."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any

from amm_competition.core.amm import AMM
from amm_competition.core.interfaces import AMMStrategy
from amm_competition.core.trade import FeeQuote, TradeInfo
from amm_competition.market.arbitrageur import Arbitrageur
from amm_competition.market.price_process import GBMPriceProcess
from amm_competition.market.retail import RetailTrader
from amm_competition.market.router import OrderRouter
from sim_lab.config import StrategyParams, WorldConfig, sample_world_from_seed
from sim_lab.strategy import DeterministicAdaptiveStrategy

EPS = 1e-12


class FixedFeeStrategy(AMMStrategy):
    """Static-fee strategy used for the normalizer AMM."""

    def __init__(self, *, fee_bps: float = 30.0, name: str = "Normalizer30bps"):
        self._fee = fee_bps / 10_000.0
        self._name = name

    def after_initialize(self, initial_x: Decimal, initial_y: Decimal) -> FeeQuote:
        _ = initial_x, initial_y
        return FeeQuote(bid_fee=Decimal(str(self._fee)), ask_fee=Decimal(str(self._fee)))

    def after_swap(self, trade: TradeInfo) -> FeeQuote:
        _ = trade
        return FeeQuote(bid_fee=Decimal(str(self._fee)), ask_fee=Decimal(str(self._fee)))

    def get_name(self) -> str:
        return self._name


@dataclass(frozen=True)
class StepRecord:
    step: int
    fair_price: float
    submission_spot: float
    normalizer_spot: float
    internal_price_estimate: float
    p_low: float
    p_high: float
    bid_fee_bps: float
    ask_fee_bps: float
    lambda_hat: float
    arb_hat: float
    stale_hat: float
    inventory_signed: float


@dataclass(frozen=True)
class EventRecord:
    step: int
    event_index: int
    event_type: str
    amm: str
    trade_side: str
    amount_x: float
    amount_y: float
    fair_price: float
    edge: float
    submission_spot: float
    internal_price_estimate: float
    bid_fee_bps: float
    ask_fee_bps: float
    probable_arb: bool


@dataclass(frozen=True)
class SimulationRunResult:
    run_id: str
    seed: int
    n_steps: int
    sampled_sigma: float
    sampled_retail_arrival_rate: float
    sampled_retail_mean_size: float

    total_edge_submission: float
    total_edge_normalizer: float
    avg_fee_bps_submission: float
    avg_bid_fee_bps_submission: float
    avg_ask_fee_bps_submission: float

    arb_volume_y_submission: float
    retail_volume_y_submission: float
    arb_trade_count_submission: int
    retail_trade_count_submission: int

    arb_volume_y_normalizer: float
    retail_volume_y_normalizer: float
    arb_trade_count_normalizer: int
    retail_trade_count_normalizer: int

    step_records: list[StepRecord]
    event_records: list[EventRecord]
    started_at: str
    finished_at: str

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seed": self.seed,
            "n_steps": self.n_steps,
            "sampled_sigma": self.sampled_sigma,
            "sampled_retail_arrival_rate": self.sampled_retail_arrival_rate,
            "sampled_retail_mean_size": self.sampled_retail_mean_size,
            "total_edge_submission": self.total_edge_submission,
            "total_edge_normalizer": self.total_edge_normalizer,
            "avg_fee_bps_submission": self.avg_fee_bps_submission,
            "arb_volume_y_submission": self.arb_volume_y_submission,
            "retail_volume_y_submission": self.retail_volume_y_submission,
            "arb_trade_count_submission": self.arb_trade_count_submission,
            "retail_trade_count_submission": self.retail_trade_count_submission,
        }


def _trade_edge(trade: TradeInfo, fair_price: float) -> float:
    amount_x = float(trade.amount_x)
    amount_y = float(trade.amount_y)
    # Match canonical Rust engine sign convention:
    # - AMM buys X  (trade.side == "buy"):  edge = x*p - y
    # - AMM sells X (trade.side == "sell"): edge = y - x*p
    if trade.side == "buy":
        return (amount_x * fair_price) - amount_y
    return amount_y - (amount_x * fair_price)


class DeterministicSimulator:
    """Pure-Python simulator for strategy prototyping before Solidity translation."""

    def __init__(
        self,
        *,
        world: WorldConfig | None = None,
        strategy_params: StrategyParams | None = None,
    ):
        self.world = world if world is not None else WorldConfig()
        self.strategy_params = strategy_params if strategy_params is not None else StrategyParams()

    def run(
        self,
        *,
        seed: int,
        n_steps: int | None = None,
        capture_steps: bool = True,
        capture_events: bool = True,
        run_id: str | None = None,
    ) -> SimulationRunResult:
        steps = self.world.n_steps if n_steps is None else int(n_steps)
        sampled = sample_world_from_seed(seed, self.world.ranges)

        started_at = datetime.now(timezone.utc).isoformat()
        price_process = GBMPriceProcess(
            initial_price=self.world.initial_price,
            mu=self.world.gbm_mu,
            sigma=sampled.sigma,
            dt=self.world.gbm_dt,
            seed=seed + 11,
        )
        retail = RetailTrader(
            arrival_rate=sampled.retail_arrival_rate,
            mean_size=sampled.retail_mean_size,
            size_sigma=self.world.retail_size_sigma,
            buy_prob=self.world.retail_buy_prob,
            seed=seed + 29,
        )

        submission_strategy = DeterministicAdaptiveStrategy(self.strategy_params)
        normalizer_strategy = FixedFeeStrategy(fee_bps=self.strategy_params.normalizer_fee_bps)
        submission = AMM(
            strategy=submission_strategy,
            reserve_x=Decimal(str(self.world.initial_x)),
            reserve_y=Decimal(str(self.world.initial_y)),
            name="submission",
        )
        normalizer = AMM(
            strategy=normalizer_strategy,
            reserve_x=Decimal(str(self.world.initial_x)),
            reserve_y=Decimal(str(self.world.initial_y)),
            name="normalizer",
        )
        submission.initialize()
        normalizer.initialize()

        router = OrderRouter()
        arbitrageur = Arbitrageur()

        edge_submission = 0.0
        edge_normalizer = 0.0
        arb_volume_submission = 0.0
        retail_volume_submission = 0.0
        arb_count_submission = 0
        retail_count_submission = 0
        arb_volume_normalizer = 0.0
        retail_volume_normalizer = 0.0
        arb_count_normalizer = 0
        retail_count_normalizer = 0

        step_records: list[StepRecord] = []
        event_records: list[EventRecord] = []
        avg_bid_fee_samples: list[float] = []
        avg_ask_fee_samples: list[float] = []

        for step in range(steps):
            fair_price = float(price_process.current_price if step == 0 else price_process.step())
            event_index = 0

            for amm_name, amm in (("submission", submission), ("normalizer", normalizer)):
                opportunity = arbitrageur.find_arb_opportunity(amm, Decimal(str(fair_price)))
                if opportunity is None:
                    continue

                if opportunity.side == "sell":
                    trade = amm.execute_sell_x(opportunity.amount_x, step)
                else:
                    trade = amm.execute_buy_x(opportunity.amount_x, step)

                if trade is None:
                    continue

                edge = _trade_edge(trade, fair_price)
                amount_y = abs(float(trade.amount_y))
                if amm_name == "submission":
                    edge_submission += edge
                    arb_volume_submission += amount_y
                    arb_count_submission += 1
                else:
                    edge_normalizer += edge
                    arb_volume_normalizer += amount_y
                    arb_count_normalizer += 1

                if capture_events:
                    snap = submission_strategy.snapshot()
                    event_records.append(
                        EventRecord(
                            step=step,
                            event_index=event_index,
                            event_type="arb",
                            amm=amm_name,
                            trade_side=str(trade.side),
                            amount_x=float(trade.amount_x),
                            amount_y=float(trade.amount_y),
                            fair_price=fair_price,
                            edge=edge if amm_name == "submission" else 0.0,
                            submission_spot=float(submission.spot_price),
                            internal_price_estimate=float(snap["p_step"]),
                            bid_fee_bps=float(snap["bid_fee_bps"]),
                            ask_fee_bps=float(snap["ask_fee_bps"]),
                            probable_arb=bool(snap["last_probable_arb"]),
                        )
                    )
                event_index += 1

            orders = retail.generate_orders()
            routed = router.route_orders(
                orders=orders,
                amms=[submission, normalizer],
                fair_price=Decimal(str(fair_price)),
                timestamp=step,
            )
            for routed_trade in routed:
                amm_name = routed_trade.amm.name
                trade = routed_trade.trade_info
                edge = _trade_edge(trade, fair_price)
                amount_y = abs(float(trade.amount_y))

                if amm_name == "submission":
                    edge_submission += edge
                    retail_volume_submission += amount_y
                    retail_count_submission += 1
                else:
                    edge_normalizer += edge
                    retail_volume_normalizer += amount_y
                    retail_count_normalizer += 1

                if capture_events:
                    snap = submission_strategy.snapshot()
                    event_records.append(
                        EventRecord(
                            step=step,
                            event_index=event_index,
                            event_type="retail",
                            amm=amm_name,
                            trade_side=str(trade.side),
                            amount_x=float(trade.amount_x),
                            amount_y=float(trade.amount_y),
                            fair_price=fair_price,
                            edge=edge if amm_name == "submission" else 0.0,
                            submission_spot=float(submission.spot_price),
                            internal_price_estimate=float(snap["p_step"]),
                            bid_fee_bps=float(snap["bid_fee_bps"]),
                            ask_fee_bps=float(snap["ask_fee_bps"]),
                            probable_arb=bool(snap["last_probable_arb"]),
                        )
                    )
                event_index += 1

            snap = submission_strategy.snapshot()
            avg_bid_fee_samples.append(float(snap["bid_fee_bps"]))
            avg_ask_fee_samples.append(float(snap["ask_fee_bps"]))

            if capture_steps:
                step_records.append(
                    StepRecord(
                        step=step,
                        fair_price=fair_price,
                        submission_spot=float(submission.spot_price),
                        normalizer_spot=float(normalizer.spot_price),
                        internal_price_estimate=float(snap["p_step"]),
                        p_low=float(snap["p_low"]),
                        p_high=float(snap["p_high"]),
                        bid_fee_bps=float(snap["bid_fee_bps"]),
                        ask_fee_bps=float(snap["ask_fee_bps"]),
                        lambda_hat=float(snap["lambda_hat"]),
                        arb_hat=float(snap["arb_hat"]),
                        stale_hat=float(snap["stale_hat"]),
                        inventory_signed=float(snap["inventory_signed"]),
                    )
                )

        finished_at = datetime.now(timezone.utc).isoformat()
        avg_bid_fee = fmean(avg_bid_fee_samples) if avg_bid_fee_samples else 0.0
        avg_ask_fee = fmean(avg_ask_fee_samples) if avg_ask_fee_samples else 0.0

        return SimulationRunResult(
            run_id=run_id if run_id else f"sim-{seed}-{steps}",
            seed=seed,
            n_steps=steps,
            sampled_sigma=sampled.sigma,
            sampled_retail_arrival_rate=sampled.retail_arrival_rate,
            sampled_retail_mean_size=sampled.retail_mean_size,
            total_edge_submission=edge_submission,
            total_edge_normalizer=edge_normalizer,
            avg_fee_bps_submission=0.5 * (avg_bid_fee + avg_ask_fee),
            avg_bid_fee_bps_submission=avg_bid_fee,
            avg_ask_fee_bps_submission=avg_ask_fee,
            arb_volume_y_submission=arb_volume_submission,
            retail_volume_y_submission=retail_volume_submission,
            arb_trade_count_submission=arb_count_submission,
            retail_trade_count_submission=retail_count_submission,
            arb_volume_y_normalizer=arb_volume_normalizer,
            retail_volume_y_normalizer=retail_volume_normalizer,
            arb_trade_count_normalizer=arb_count_normalizer,
            retail_trade_count_normalizer=retail_count_normalizer,
            step_records=step_records,
            event_records=event_records,
            started_at=started_at,
            finished_at=finished_at,
        )


def write_step_trace_csv(result: SimulationRunResult, path: str | Path) -> Path:
    """Write step-level trace data to CSV."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [f.name for f in StepRecord.__dataclass_fields__.values()]
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result.step_records:
            writer.writerow(asdict(row))
    return out


def write_event_trace_csv(result: SimulationRunResult, path: str | Path) -> Path:
    """Write event-level trade trace data to CSV."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [f.name for f in EventRecord.__dataclass_fields__.values()]
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result.event_records:
            writer.writerow(asdict(row))
    return out


def write_price_plot_png(result: SimulationRunResult, path: str | Path) -> Path | None:
    """Plot external fair price vs internal estimate and submission spot."""
    if not result.step_records:
        return None

    try:
        import matplotlib.pyplot as plt
    except Exception:
        return None

    steps = [row.step for row in result.step_records]
    fair = [row.fair_price for row in result.step_records]
    p_hat = [row.internal_price_estimate for row in result.step_records]
    spot = [row.submission_spot for row in result.step_records]

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(1, 1, figsize=(13, 6))
    ax.plot(steps, fair, label="external fair price", linewidth=1.3)
    ax.plot(steps, p_hat, label="internal estimate p_step", linewidth=1.2)
    ax.plot(steps, spot, label="submission spot", linewidth=1.0, alpha=0.9)
    ax.set_xlabel("step")
    ax.set_ylabel("price (Y per X)")
    ax.set_title(f"Price Evolution | seed={result.seed}")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out
