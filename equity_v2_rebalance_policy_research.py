from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

import equity_v2_modern_research as research
from equity_v2_dca_engine import xirr
from equity_v2_modern_engine import (
    ALL_ASSETS,
    ANNUAL_DEDUCTION_KRW,
    COMMISSION_BPS,
    FX_SPREAD_BPS,
    INITIAL_CAPITAL_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    SLIPPAGE_BPS,
    TAX_RATE,
    GenericTaxableSimulator,
    TaxableSimulation,
    _normalize,
    first_trading_days,
)
from quant_guardian import DEFAULT_CONFIG, cache_key, load_config, read_price, resolve_paths


STRATEGY_VERSION = "equity-v2-rebalance-policy-0.1"
FIXED_START = pd.Timestamp("2006-07-03")
FIXED_END = pd.Timestamp("2026-08-12")
ACTUAL_TQQQ_START = pd.Timestamp("2010-02-09")
HOLDOUT_START = pd.Timestamp("2023-01-03")
DOTCOM_START = pd.Timestamp("1999-03-10")
DOTCOM_END = pd.Timestamp("2003-12-31")
NDX_START = pd.Timestamp("1985-10-01")
DEV_FOLDS = {
    "gfc_qe": (pd.Timestamp("2006-07-03"), pd.Timestamp("2011-12-30")),
    "post_gfc": (pd.Timestamp("2012-01-03"), pd.Timestamp("2018-12-31")),
    "covid_inflation": (pd.Timestamp("2019-01-02"), pd.Timestamp("2022-12-30")),
}

PORTFOLIOS: dict[str, dict[str, float]] = {
    "robust_t50_cash50": {"TQQQ": 0.50, "CASH": 0.50},
    "equity_t55_q45": {"TQQQ": 0.55, "QQQ": 0.45},
    "equity_t60_q40": {"TQQQ": 0.60, "QQQ": 0.40},
}
REBALANCE_MONTHS = {"jan": 1, "jun": 6, "dec": 12}
WEIGHT_TOLERANCE = 1e-10


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    contribution_mode: str
    rebalance_mode: str
    rebalance_month: int | None = None
    band: float | None = None
    legacy_order: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "contribution_mode": self.contribution_mode,
            "rebalance_mode": self.rebalance_mode,
            "rebalance_month": self.rebalance_month,
            "band": self.band,
            "legacy_order": self.legacy_order,
        }


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_pct(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value):,.0f}원"


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_ready(dict(payload)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _first_trading_days_by_month(index: pd.DatetimeIndex) -> dict[tuple[int, int], pd.Timestamp]:
    series = pd.Series(index=index, data=index)
    return {
        (int(period.year), int(period.month)): pd.Timestamp(group.index.min())
        for period, group in series.groupby(index.to_period("M"))
    }


def _weights_at_open(
    simulator: GenericTaxableSimulator,
    date: pd.Timestamp,
) -> dict[str, float]:
    values = {
        asset: simulator._asset_value_usd(asset, date, "Open")
        for asset in ALL_ASSETS
    }
    total = float(sum(values.values()))
    if total <= 0:
        return {asset: 0.0 for asset in ALL_ASSETS}
    return {asset: value / total for asset, value in values.items()}


class PolicyTaxableSimulator(GenericTaxableSimulator):
    def __init__(
        self,
        *,
        frames: Mapping[str, pd.DataFrame],
        target_weights: Mapping[str, float],
        policy: PolicySpec,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        super().__init__(
            frames=frames,
            targets=pd.DataFrame(),
            start=start,
            end=end,
        )
        self.target_weights = _normalize(target_weights)
        self.policy = policy
        self.current_target = self.target_weights
        self.policy_events: list[dict[str, Any]] = []
        self.weight_observations: list[dict[str, Any]] = []

    def _contribute_with_allocations(
        self,
        date: pd.Timestamp,
        amount_krw: float,
        allocations: Mapping[str, float],
        reason: str,
    ) -> None:
        if amount_krw <= 0:
            return
        allocations = _normalize(allocations)
        fx = self._fx(date, "Open")
        usd_total = amount_krw / (fx * (1 + self.fx_spread))
        for asset in ALL_ASSETS:
            weight = float(allocations.get(asset, 0.0))
            if weight <= 0:
                continue
            self._buy(
                asset,
                usd_total * weight,
                date,
                reason,
                external_basis_krw=amount_krw * weight,
            )
        self.total_contributions += amount_krw
        self.cashflows.append((date, -amount_krw))

    def _deficit_allocations(
        self,
        date: pd.Timestamp,
        amount_krw: float,
    ) -> dict[str, float]:
        fx = self._fx(date, "Open")
        usd_total = amount_krw / (fx * (1 + self.fx_spread))
        current = {
            asset: self._asset_value_usd(asset, date, "Open")
            for asset in ALL_ASSETS
        }
        post_total = float(sum(current.values()) + usd_total)
        deficits = {
            asset: max(
                0.0,
                post_total * self.target_weights.get(asset, 0.0) - current[asset],
            )
            for asset in ALL_ASSETS
        }
        total_deficit = float(sum(deficits.values()))
        if total_deficit <= 1e-12:
            return self.target_weights
        return {
            asset: deficit / total_deficit
            for asset, deficit in deficits.items()
            if deficit > 1e-12
        }

    def _monthly_contribution(self, date: pd.Timestamp, amount_krw: float) -> None:
        if self.policy.contribution_mode == "fixed_target":
            allocations = self.target_weights
        elif self.policy.contribution_mode == "deficit_first":
            allocations = self._deficit_allocations(date, amount_krw)
        else:
            raise ValueError(
                f"unsupported contribution mode: {self.policy.contribution_mode}"
            )
        self._contribute_with_allocations(
            date,
            amount_krw,
            allocations,
            f"contribution_{self.policy.contribution_mode}",
        )

    def _max_abs_deviation(self, weights: Mapping[str, float]) -> float:
        return max(
            abs(float(weights.get(asset, 0.0)) - self.target_weights.get(asset, 0.0))
            for asset in ALL_ASSETS
        )

    def _record_weights(
        self,
        date: pd.Timestamp,
        stage: str,
        *,
        band_breach: bool = False,
    ) -> None:
        weights = _weights_at_open(self, date)
        self.weight_observations.append(
            {
                "date": date,
                "stage": stage,
                "max_abs_deviation": self._max_abs_deviation(weights),
                "band_breach": bool(band_breach),
                **{f"weight_{asset.lower()}": weights[asset] for asset in ALL_ASSETS},
            }
        )

    def _execute_rebalance(self, date: pd.Timestamp, reason: str) -> None:
        before_trades = len(self.trades)
        before_weights = _weights_at_open(self, date)
        self._rebalance(date, self.target_weights, reason)
        after_trades = len(self.trades)
        if after_trades > before_trades:
            self.policy_events.append(
                {
                    "date": date,
                    "event": reason,
                    "trades_added": after_trades - before_trades,
                    "pre_max_abs_deviation": self._max_abs_deviation(before_weights),
                }
            )

    def _band_breached(self, date: pd.Timestamp) -> bool:
        if self.policy.band is None:
            return False
        weights = _weights_at_open(self, date)
        return self._max_abs_deviation(weights) > float(self.policy.band) + WEIGHT_TOLERANCE

    def _finalize(
        self,
        index: pd.DatetimeIndex,
        daily_rows: list[dict[str, Any]],
    ) -> TaxableSimulation:
        daily = pd.DataFrame(daily_rows).set_index("Date")
        final_date = pd.Timestamp(index[-1])
        final_fx = self._fx(final_date, "Close")
        liquidation = 0.0
        final_gain = self.realized_by_year.get(final_date.year, 0.0)
        for asset in ALL_ASSETS:
            position = self.positions[asset]
            if position.units <= 0:
                continue
            proceeds_usd = (
                position.units
                * self._price(asset, final_date, "Close")
                * (1 - self.trade_rate)
            )
            proceeds_krw = proceeds_usd * final_fx * (1 - self.fx_spread)
            liquidation += proceeds_krw
            if asset != "CASH":
                final_gain += proceeds_krw - position.basis_krw
        final_tax = max(0.0, final_gain - self.deduction) * self.tax_rate
        unpaid_prior_tax = 0.0
        for year, gain in self.realized_by_year.items():
            if year < final_date.year and not any(
                int(row["tax_year"]) == year for row in self.taxes
            ):
                unpaid_prior_tax += max(0.0, gain - self.deduction) * self.tax_rate
        after_tax = liquidation - final_tax - unpaid_prior_tax
        self.cashflows.append((final_date, after_tax))
        pretax_flows = list(self.cashflows[:-1]) + [
            (final_date, float(daily["value_krw"].iloc[-1]))
        ]
        nav = daily["nav"]
        elapsed_years = max(1, (final_date - pd.Timestamp(index[0])).days) / 365.25
        trades = pd.DataFrame(self.trades)
        observations = pd.DataFrame(self.weight_observations)
        policy_sells = pd.DataFrame()
        all_sells = pd.DataFrame()
        total_sell_notional = 0.0
        policy_sell_notional = 0.0
        if not trades.empty:
            all_sells = trades.loc[trades["side"] == "SELL"].copy()
            if not all_sells.empty:
                all_sells["proceeds_krw"] = all_sells.apply(
                    lambda row: float(row["units"])
                    * float(row["price_usd"])
                    * (1 - self.trade_rate)
                    * self._fx(pd.Timestamp(row["date"]), "Open"),
                    axis=1,
                )
                total_sell_notional = float(all_sells["proceeds_krw"].sum())
                policy_sells = all_sells.loc[
                    ~all_sells["reason"].isin(["tax_payment"])
                ].copy()
                policy_sell_notional = float(policy_sells["proceeds_krw"].sum())
        metrics = {
            "start": pd.Timestamp(index[0]).date().isoformat(),
            "end": final_date.date().isoformat(),
            "total_contributions_krw": self.total_contributions,
            "pre_tax_value_krw": float(daily["value_krw"].iloc[-1]),
            "after_tax_liquidation_value_krw": after_tax,
            "pre_tax_xirr": xirr(pretax_flows),
            "after_tax_xirr": xirr(self.cashflows),
            "twr_cagr": float((nav.iloc[-1] / nav.iloc[0]) ** (1 / elapsed_years) - 1),
            "mdd": float((nav / nav.cummax() - 1).min()),
            "minimum_vs_contributions": float(
                (daily["value_krw"] / daily["contributions_krw"] - 1).min()
            ),
            "trade_count": len(self.trades),
            "tax_paid_krw": float(sum(row["tax_krw"] for row in self.taxes)),
            "terminal_tax_krw": final_tax + unpaid_prior_tax,
            "realized_gain_krw": float(sum(self.realized_by_year.values())),
            "total_sell_notional_krw": total_sell_notional,
            "policy_sell_notional_krw": policy_sell_notional,
            "policy_sell_count": int(len(policy_sells)),
            "policy_sell_days": int(policy_sells["date"].nunique())
            if not policy_sells.empty
            else 0,
            "rebalance_event_count": int(len(self.policy_events)),
            "max_monthly_abs_weight_deviation": float(
                observations.loc[
                    observations["stage"] == "post_contribution_pre_rebalance",
                    "max_abs_deviation",
                ].max()
            )
            if not observations.empty
            and (
                observations["stage"] == "post_contribution_pre_rebalance"
            ).any()
            else 0.0,
            "mean_monthly_abs_weight_deviation": float(
                observations.loc[
                    observations["stage"] == "post_contribution_pre_rebalance",
                    "max_abs_deviation",
                ].mean()
            )
            if not observations.empty
            and (
                observations["stage"] == "post_contribution_pre_rebalance"
            ).any()
            else 0.0,
            "band_breach_months": int(observations["band_breach"].sum())
            if not observations.empty
            else 0,
        }
        return TaxableSimulation(
            metrics=metrics,
            daily=daily,
            trades=trades,
            taxes=pd.DataFrame(self.taxes),
        )

    def run(self) -> TaxableSimulation:
        index = self._build_index()
        if len(index) < 250:
            raise ValueError("taxable simulation period is too short")
        monthly_dates = first_trading_days(index)
        month_map = _first_trading_days_by_month(index)
        first_date = pd.Timestamp(index[0])
        initial_month = first_date.to_period("M")
        daily_rows: list[dict[str, Any]] = []
        previous_year = first_date.year

        self._contribute_with_allocations(
            first_date,
            self.initial,
            self.target_weights,
            "initial_allocation",
        )
        self.nav_units = self.initial

        for date in index:
            date = pd.Timestamp(date)
            if date.year != previous_year:
                self._pay_tax(date, previous_year)
                previous_year = date.year

            is_monthly = date in monthly_dates
            is_initial_month = date.to_period("M") == initial_month
            is_annual_rebalance = (
                self.policy.rebalance_mode == "annual_exact"
                and self.policy.rebalance_month is not None
                and month_map.get((date.year, self.policy.rebalance_month)) == date
            )

            if self.policy.legacy_order and is_annual_rebalance:
                self._execute_rebalance(date, "annual_exact_legacy")

            if is_monthly and not is_initial_month:
                before = self._value_krw(date, "Open")
                unit_price = before / self.nav_units if self.nav_units > 0 else 1.0
                self.nav_units += self.monthly / unit_price
                self._monthly_contribution(date, self.monthly)

            if is_monthly:
                breach = (
                    self.policy.rebalance_mode == "monthly_band"
                    and self._band_breached(date)
                )
                self._record_weights(
                    date,
                    "post_contribution_pre_rebalance",
                    band_breach=breach,
                )
                if breach:
                    self._execute_rebalance(date, "band_rebalance")

            if (
                not self.policy.legacy_order
                and is_annual_rebalance
                and self.policy.rebalance_mode == "annual_exact"
            ):
                self._execute_rebalance(date, "annual_exact_deficit_first")

            if is_monthly:
                self._record_weights(date, "post_policy")

            value = self._value_krw(date, "Close")
            nav = value / self.nav_units if self.nav_units > 0 else 0.0
            daily_rows.append(
                {
                    "Date": date,
                    "value_krw": value,
                    "nav": nav,
                    "contributions_krw": self.total_contributions,
                    "profit_vs_contributions": value - self.total_contributions,
                }
            )
        return self._finalize(index, daily_rows)


def build_frozen_frames(
    *,
    paths: Any,
    refresh: bool,
) -> tuple[
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
    pd.DataFrame,
    dict[str, float],
]:
    tickers = ("QQQ", "QLD", "TQQQ", "^IRX", "KRW=X", "^NDX")
    raw = {
        ticker: read_price(ticker, paths, refresh=refresh).loc[:FIXED_END].copy()
        for ticker in tickers
    }
    calibration_2x, residual_2x = research.calibrate_residual(
        raw["QQQ"],
        raw["^IRX"]["Close"],
        raw["QLD"],
        leverage=2.0,
    )
    calibration_3x, residual_3x = research.calibrate_residual(
        raw["QQQ"],
        raw["^IRX"]["Close"],
        raw["TQQQ"],
        leverage=3.0,
    )
    synthetic_3x = research.dynamic_synthetic(
        raw["QQQ"],
        raw["^IRX"]["Close"],
        leverage=3.0,
        residual_drag=float(residual_3x),
    )
    spliced_tqqq = research.splice_synthetic_actual(synthetic_3x, raw["TQQQ"])
    warmup_start = pd.Timestamp("2005-01-03")
    master = research._clean_ohlc(raw["QQQ"]).loc[warmup_start:FIXED_END].index
    qqq = research.aligned_frame(raw["QQQ"], master)
    frames = {
        "QQQ": qqq,
        "QLD": research.aligned_frame(raw["QLD"], master),
        "TQQQ": research.aligned_frame(spliced_tqqq, master),
        "CASH": research.cash_frame(master, raw["^IRX"]["Close"]),
        "KRW=X": research.aligned_frame(raw["KRW=X"], master).ffill().bfill(),
    }
    calibration = pd.concat([calibration_2x, calibration_3x], ignore_index=True)
    residuals = {"QLD_2x": float(residual_2x), "TQQQ_3x": float(residual_3x)}
    return frames, raw, calibration, residuals


def build_stress_frames(
    *,
    underlying: pd.DataFrame,
    short_yield: pd.Series,
    residual_2x: float,
    residual_3x: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    underlying = research._clean_ohlc(underlying)
    synthetic_2x = research.dynamic_synthetic(
        underlying,
        short_yield,
        leverage=2.0,
        residual_drag=residual_2x,
    )
    synthetic_3x = research.dynamic_synthetic(
        underlying,
        short_yield,
        leverage=3.0,
        residual_drag=residual_3x,
    )
    warmup_start = max(
        pd.Timestamp(underlying.index.min()),
        pd.Timestamp(start) - pd.Timedelta(days=550),
    )
    index = underlying.loc[warmup_start:end].index
    fx = research.constant_fx(underlying.loc[index], 1000.0)
    return {
        "QQQ": research.aligned_frame(underlying, index),
        "QLD": research.aligned_frame(synthetic_2x, index),
        "TQQQ": research.aligned_frame(synthetic_3x, index),
        "CASH": research.cash_frame(index, short_yield),
        "KRW=X": fx,
    }


def valid_start(frames: Mapping[str, pd.DataFrame], requested: pd.Timestamp) -> pd.Timestamp:
    starts = [pd.Timestamp(requested)]
    for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X"):
        valid = frames[asset][["Open", "Close"]].dropna()
        if valid.empty:
            raise ValueError(f"no usable prices for {asset}")
        starts.append(pd.Timestamp(valid.index.min()))
    return max(starts)


def run_period(
    *,
    portfolio: Mapping[str, float],
    policy: PolicySpec,
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[dict[str, Any], PolicyTaxableSimulator, TaxableSimulation]:
    actual_start = valid_start(frames, pd.Timestamp(start))
    actual_end = min(
        pd.Timestamp(end),
        min(
            pd.Timestamp(frames[asset]["Close"].dropna().index.max())
            for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
        ),
    )
    simulator = PolicyTaxableSimulator(
        frames=frames,
        target_weights=portfolio,
        policy=policy,
        start=actual_start,
        end=actual_end,
    )
    result = simulator.run()
    return result.metrics, simulator, result


def evaluate_policy(
    *,
    portfolio_id: str,
    portfolio: Mapping[str, float],
    policy: PolicySpec,
    frames: Mapping[str, pd.DataFrame],
    latest: pd.Timestamp,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "weights_json": json.dumps(dict(portfolio), sort_keys=True),
        **policy.record(),
    }
    full, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=FIXED_START,
        end=latest,
    )
    row.update({f"full_{key}": value for key, value in full.items()})

    fold_xirrs: list[float] = []
    fold_mdds: list[float] = []
    for prefix, (fold_start, fold_end) in DEV_FOLDS.items():
        metrics, _, _ = run_period(
            portfolio=portfolio,
            policy=policy,
            frames=frames,
            start=fold_start,
            end=fold_end,
        )
        row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
        fold_xirrs.append(float(metrics["after_tax_xirr"]))
        fold_mdds.append(float(metrics["mdd"]))

    holdout, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=HOLDOUT_START,
        end=latest,
    )
    row.update({f"holdout_{key}": value for key, value in holdout.items()})

    actual, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=ACTUAL_TQQQ_START,
        end=latest,
    )
    row.update({f"actual_tqqq_{key}": value for key, value in actual.items()})

    dev_mean = float(np.mean(fold_xirrs))
    dev_min = float(np.min(fold_xirrs))
    dev_worst_mdd = float(np.min(fold_mdds))
    score = 0.50 * dev_mean + 0.40 * dev_min + 0.10 * dev_worst_mdd
    row.update(
        {
            "dev_mean_after_tax_xirr": dev_mean,
            "dev_min_after_tax_xirr": dev_min,
            "dev_worst_mdd": dev_worst_mdd,
            "selection_score": score,
        }
    )
    return row


def phase_a_policies() -> list[PolicySpec]:
    policies: list[PolicySpec] = []
    for month_name, month in REBALANCE_MONTHS.items():
        policies.append(
            PolicySpec(
                policy_id=f"legacy_exact_{month_name}",
                contribution_mode="fixed_target",
                rebalance_mode="annual_exact",
                rebalance_month=month,
                legacy_order=True,
            )
        )
        policies.append(
            PolicySpec(
                policy_id=f"deficit_exact_{month_name}",
                contribution_mode="deficit_first",
                rebalance_mode="annual_exact",
                rebalance_month=month,
                legacy_order=False,
            )
        )
    return policies


def choose_phase_a_month(frame: pd.DataFrame, portfolio_id: str) -> pd.Series:
    candidates = frame.loc[
        (frame["portfolio_id"] == portfolio_id)
        & (frame["contribution_mode"] == "deficit_first")
        & (frame["rebalance_mode"] == "annual_exact")
    ].copy()
    if candidates.empty:
        raise ValueError(f"no phase-A candidates for {portfolio_id}")
    return candidates.sort_values(
        [
            "selection_score",
            "dev_min_after_tax_xirr",
            "dev_mean_after_tax_xirr",
            "full_policy_sell_notional_krw",
        ],
        ascending=[False, False, False, True],
    ).iloc[0]


def phase_b_policies(selected_month: int) -> list[PolicySpec]:
    month_name = next(
        name for name, value in REBALANCE_MONTHS.items() if value == selected_month
    )
    return [
        PolicySpec(
            policy_id=f"deficit_exact_{month_name}",
            contribution_mode="deficit_first",
            rebalance_mode="annual_exact",
            rebalance_month=selected_month,
        ),
        PolicySpec(
            policy_id="deficit_band_05pp",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.05,
        ),
        PolicySpec(
            policy_id="deficit_band_10pp",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.10,
        ),
        PolicySpec(
            policy_id="deficit_band_15pp",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.15,
        ),
        PolicySpec(
            policy_id="deficit_contribution_only",
            contribution_mode="deficit_first",
            rebalance_mode="none",
        ),
    ]


def choose_practical_policy(frame: pd.DataFrame, portfolio_id: str) -> pd.Series:
    candidates = frame.loc[frame["portfolio_id"] == portfolio_id].copy()
    best_min = float(candidates["dev_min_after_tax_xirr"].max())
    near = candidates.loc[
        candidates["dev_min_after_tax_xirr"] >= best_min - 0.0025
    ].copy()
    best_mean = float(near["dev_mean_after_tax_xirr"].max())
    near = near.loc[near["dev_mean_after_tax_xirr"] >= best_mean - 0.0025]
    return near.sort_values(
        [
            "full_policy_sell_count",
            "full_policy_sell_notional_krw",
            "dev_worst_mdd",
            "selection_score",
        ],
        ascending=[True, True, False, False],
    ).iloc[0]


def selected_details(
    *,
    selections: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tax_rows: list[dict[str, Any]] = []
    trade_rows: list[pd.DataFrame] = []
    weight_rows: list[pd.DataFrame] = []
    for _, selected in selections.iterrows():
        portfolio_id = str(selected["portfolio_id"])
        policy = PolicySpec(
            policy_id=str(selected["policy_id"]),
            contribution_mode=str(selected["contribution_mode"]),
            rebalance_mode=str(selected["rebalance_mode"]),
            rebalance_month=(
                int(selected["rebalance_month"])
                if pd.notna(selected["rebalance_month"])
                else None
            ),
            band=float(selected["band"]) if pd.notna(selected["band"]) else None,
            legacy_order=bool(selected["legacy_order"]),
        )
        _, simulator, result = run_period(
            portfolio=PORTFOLIOS[portfolio_id],
            policy=policy,
            frames=frames,
            start=FIXED_START,
            end=latest,
        )
        for year, gain in sorted(simulator.realized_by_year.items()):
            tax_rows.append(
                {
                    "portfolio_id": portfolio_id,
                    "policy_id": policy.policy_id,
                    "year": year,
                    "realized_gain_krw": gain,
                    "estimated_tax_before_payment_timing_krw": max(
                        0.0, gain - ANNUAL_DEDUCTION_KRW
                    )
                    * TAX_RATE,
                }
            )
        if not result.trades.empty:
            trades = result.trades.copy()
            trades.insert(0, "policy_id", policy.policy_id)
            trades.insert(0, "portfolio_id", portfolio_id)
            trade_rows.append(trades)
        observations = pd.DataFrame(simulator.weight_observations)
        if not observations.empty:
            observations.insert(0, "policy_id", policy.policy_id)
            observations.insert(0, "portfolio_id", portfolio_id)
            weight_rows.append(observations)
    return (
        pd.DataFrame(tax_rows),
        pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame(),
        pd.concat(weight_rows, ignore_index=True) if weight_rows else pd.DataFrame(),
    )


def stress_selected(
    *,
    selections: pd.DataFrame,
    raw: Mapping[str, pd.DataFrame],
    residuals: Mapping[str, float],
) -> pd.DataFrame:
    scenarios = {
        "dotcom_1999_2003": (
            DOTCOM_START,
            DOTCOM_END,
        ),
        "ndx_1985_reference": (
            NDX_START,
            FIXED_END,
        ),
    }
    rows: list[dict[str, Any]] = []
    for scenario, (start, end) in scenarios.items():
        frames = build_stress_frames(
            underlying=raw["^NDX"],
            short_yield=raw["^IRX"]["Close"],
            residual_2x=float(residuals["QLD_2x"]),
            residual_3x=float(residuals["TQQQ_3x"]),
            start=start,
            end=end,
        )
        for _, selected in selections.iterrows():
            portfolio_id = str(selected["portfolio_id"])
            policy = PolicySpec(
                policy_id=str(selected["policy_id"]),
                contribution_mode=str(selected["contribution_mode"]),
                rebalance_mode=str(selected["rebalance_mode"]),
                rebalance_month=(
                    int(selected["rebalance_month"])
                    if pd.notna(selected["rebalance_month"])
                    else None
                ),
                band=float(selected["band"])
                if pd.notna(selected["band"])
                else None,
                legacy_order=bool(selected["legacy_order"]),
            )
            metrics, _, _ = run_period(
                portfolio=PORTFOLIOS[portfolio_id],
                policy=policy,
                frames=frames,
                start=start,
                end=end,
            )
            rows.append(
                {
                    "scenario": scenario,
                    "portfolio_id": portfolio_id,
                    "policy_id": policy.policy_id,
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def build_report(
    *,
    phase_a: pd.DataFrame,
    phase_b: pd.DataFrame,
    selections: pd.DataFrame,
    stress: pd.DataFrame,
    residuals: Mapping[str, float],
    snapshot_manifest: Mapping[str, Any],
) -> str:
    lines = [
        "# Equity v2 과세계좌 리밸런싱 운영정책 연구",
        "",
        f"- 고정 연구기간: {FIXED_START.date().isoformat()}~{FIXED_END.date().isoformat()}",
        "- 초기자금 8,000만원, 월 50만원",
        "- 수수료 5bp, 슬리피지 5bp, 환전스프레드 10bp",
        "- 해외주식 양도세 22%, 연 250만원 공제 근사",
        "- 후보 선택에는 2023년 이후 홀드아웃을 사용하지 않음",
        "- BTC, 기존 Equity v1 사이트·Telegram, 자동주문 변경 없음",
        f"- 합성 QLD 잔여드래그: {float(residuals['QLD_2x']):.6f}",
        f"- 합성 TQQQ 잔여드래그: {float(residuals['TQQQ_3x']):.6f}",
        "",
        "## 1. 단계 A: 연 1회 리밸런싱 월과 신규입금 방식",
        "",
        "`legacy`는 기존 엔진과 동일하게 리밸런싱 후 목표비중대로 월입금한다. "
        "`deficit`은 월입금을 부족자산에 먼저 투입한 뒤 필요한 경우에만 정확비중으로 복원한다.",
        "",
        "| 포트폴리오 | 정책 | 개발 평균 세후 XIRR | 개발 최저 세후 XIRR | 개발 최악 MDD | 전체 세후 XIRR | 전체 MDD | 정책매도 횟수 | 정책매도액 | 세후 최종액 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for portfolio_id in PORTFOLIOS:
        subset = phase_a.loc[phase_a["portfolio_id"] == portfolio_id].sort_values(
            "selection_score", ascending=False
        )
        for _, row in subset.iterrows():
            lines.append(
                f"| {portfolio_id} | {row['policy_id']} | "
                f"{_fmt_pct(row['dev_mean_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_worst_mdd'])} | "
                f"{_fmt_pct(row['full_after_tax_xirr'])} | "
                f"{_fmt_pct(row['full_mdd'])} | "
                f"{int(row['full_policy_sell_count'])} | "
                f"{_fmt_krw(row['full_policy_sell_notional_krw'])} | "
                f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
            )

    lines.extend(
        [
            "",
            "## 2. 단계 B: 정확복원·허용밴드·신규입금만 보정",
            "",
            "허용밴드는 매월 첫 거래일에 신규입금으로 먼저 보정한 뒤 확인한다. "
            "밴드를 벗어나면 목표비중까지 복원한다.",
            "",
            "| 포트폴리오 | 정책 | 개발 평균 세후 XIRR | 개발 최저 세후 XIRR | 개발 최악 MDD | 전체 세후 XIRR | 전체 MDD | 최대 월간 비중이탈 | 정책매도 횟수 | 정책매도액 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for portfolio_id in PORTFOLIOS:
        subset = phase_b.loc[phase_b["portfolio_id"] == portfolio_id].sort_values(
            "selection_score", ascending=False
        )
        for _, row in subset.iterrows():
            lines.append(
                f"| {portfolio_id} | {row['policy_id']} | "
                f"{_fmt_pct(row['dev_mean_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_worst_mdd'])} | "
                f"{_fmt_pct(row['full_after_tax_xirr'])} | "
                f"{_fmt_pct(row['full_mdd'])} | "
                f"{_fmt_pct(row['full_max_monthly_abs_weight_deviation'])} | "
                f"{int(row['full_policy_sell_count'])} | "
                f"{_fmt_krw(row['full_policy_sell_notional_krw'])} | "
                f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
            )

    lines.extend(
        [
            "",
            "## 3. 실전 우선 선택",
            "",
            "개발구간 최저 XIRR 최고치에서 0.25%p 이내, 개발 평균 XIRR 최고치에서 "
            "0.25%p 이내인 후보 중 정책매도 횟수와 매도금액이 가장 작은 안을 선택했다.",
            "",
            "| 포트폴리오 | 선택 정책 | 전체 세후 XIRR | 전체 MDD | 개발 최저 XIRR | 정책매도 횟수 | 총 세금 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in selections.iterrows():
        lines.append(
            f"| {row['portfolio_id']} | {row['policy_id']} | "
            f"{_fmt_pct(row['full_after_tax_xirr'])} | "
            f"{_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
            f"{int(row['full_policy_sell_count'])} | "
            f"{_fmt_krw(row['full_tax_paid_krw'] + row['full_terminal_tax_krw'])} | "
            f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 4. 스트레스 참고",
            "",
            "| 시나리오 | 포트폴리오 | 정책 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in stress.iterrows():
        lines.append(
            f"| {row['scenario']} | {row['portfolio_id']} | {row['policy_id']} | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 5. 재현성",
            "",
            f"- 입력 스냅샷 파일 수: {len(snapshot_manifest['files'])}",
            f"- 고정 종료일: {snapshot_manifest['fixed_end']}",
            "- 각 입력 CSV의 SHA-256은 manifest와 함께 artifact에 포함했다.",
            "- 결과 선택은 개발구간만 사용했고 홀드아웃은 보고 전용이다.",
            "- 연구 전용이며 실전 Telegram·사이트·자동주문에는 반영하지 않았다.",
        ]
    )
    return "\n".join(lines)


def write_input_snapshot(
    *,
    raw: Mapping[str, pd.DataFrame],
    output_dir: Path,
) -> dict[str, Any]:
    snapshot_dir = output_dir / "equity_v2_policy_input_snapshot"
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for ticker, frame in raw.items():
        safe = cache_key("yahoo", ticker).removeprefix("yahoo_")
        path = snapshot_dir / safe
        frame.loc[:FIXED_END].to_csv(path, encoding="utf-8", float_format="%.12g")
        files.append(
            {
                "ticker": ticker,
                "path": path.name,
                "rows": int(len(frame.loc[:FIXED_END])),
                "start": pd.Timestamp(frame.index.min()).date().isoformat(),
                "end": pd.Timestamp(frame.loc[:FIXED_END].index.max()).date().isoformat(),
                "sha256": _sha256(path),
            }
        )
    manifest = {
        "schema_version": "equity-v2-policy-input-snapshot-1",
        "fixed_end": FIXED_END.date().isoformat(),
        "created_at_utc": datetime.now(UTC).isoformat(),
        "files": files,
    }
    _write_json(snapshot_dir / "manifest.json", manifest)
    return manifest


def run(*, refresh: bool, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    paths.output.mkdir(parents=True, exist_ok=True)
    frames, raw, calibration, residuals = build_frozen_frames(
        paths=paths,
        refresh=refresh,
    )
    latest = min(
        FIXED_END,
        min(
            pd.Timestamp(frames[asset]["Close"].dropna().index.max())
            for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
        ),
    )
    snapshot_manifest = write_input_snapshot(raw=raw, output_dir=paths.output)

    phase_a_rows: list[dict[str, Any]] = []
    policies_a = phase_a_policies()
    total_a = len(PORTFOLIOS) * len(policies_a)
    counter = 0
    for portfolio_id, portfolio in PORTFOLIOS.items():
        for policy in policies_a:
            counter += 1
            print(f"phase A: {counter}/{total_a} {portfolio_id} {policy.policy_id}", flush=True)
            phase_a_rows.append(
                evaluate_policy(
                    portfolio_id=portfolio_id,
                    portfolio=portfolio,
                    policy=policy,
                    frames=frames,
                    latest=latest,
                )
            )
    phase_a = pd.DataFrame(phase_a_rows)

    chosen_months: dict[str, int] = {}
    phase_b_rows: list[dict[str, Any]] = []
    for portfolio_id, portfolio in PORTFOLIOS.items():
        chosen = choose_phase_a_month(phase_a, portfolio_id)
        chosen_months[portfolio_id] = int(chosen["rebalance_month"])
        for policy in phase_b_policies(chosen_months[portfolio_id]):
            print(f"phase B: {portfolio_id} {policy.policy_id}", flush=True)
            phase_b_rows.append(
                evaluate_policy(
                    portfolio_id=portfolio_id,
                    portfolio=portfolio,
                    policy=policy,
                    frames=frames,
                    latest=latest,
                )
            )
    phase_b = pd.DataFrame(phase_b_rows)

    selection_rows = [
        choose_practical_policy(phase_b, portfolio_id)
        for portfolio_id in PORTFOLIOS
    ]
    selections = pd.DataFrame(selection_rows).reset_index(drop=True)
    yearly_tax, selected_trades, selected_weights = selected_details(
        selections=selections,
        frames=frames,
        latest=latest,
    )
    stress = stress_selected(
        selections=selections,
        raw=raw,
        residuals=residuals,
    )
    report = build_report(
        phase_a=phase_a,
        phase_b=phase_b,
        selections=selections,
        stress=stress,
        residuals=residuals,
        snapshot_manifest=snapshot_manifest,
    )

    outputs = {
        "phase_a": paths.output / "equity_v2_rebalance_policy_phase_a.csv",
        "phase_b": paths.output / "equity_v2_rebalance_policy_phase_b.csv",
        "selected": paths.output / "equity_v2_rebalance_policy_selected.csv",
        "stress": paths.output / "equity_v2_rebalance_policy_stress.csv",
        "yearly_tax": paths.output / "equity_v2_rebalance_policy_yearly_tax.csv",
        "selected_trades": paths.output / "equity_v2_rebalance_policy_selected_trades.csv",
        "selected_weights": paths.output / "equity_v2_rebalance_policy_selected_weights.csv",
        "calibration": paths.output / "equity_v2_rebalance_policy_calibration.csv",
        "report": paths.output / "equity_v2_rebalance_policy_report.md",
        "manifest": paths.output / "equity_v2_rebalance_policy_manifest.json",
    }
    phase_a.to_csv(outputs["phase_a"], index=False, encoding="utf-8")
    phase_b.to_csv(outputs["phase_b"], index=False, encoding="utf-8")
    selections.to_csv(outputs["selected"], index=False, encoding="utf-8")
    stress.to_csv(outputs["stress"], index=False, encoding="utf-8")
    yearly_tax.to_csv(outputs["yearly_tax"], index=False, encoding="utf-8")
    selected_trades.to_csv(outputs["selected_trades"], index=False, encoding="utf-8")
    selected_weights.to_csv(outputs["selected_weights"], index=False, encoding="utf-8")
    calibration.to_csv(outputs["calibration"], index=False, encoding="utf-8")
    outputs["report"].write_text(report, encoding="utf-8")

    manifest = {
        "schema_version": "equity-v2-rebalance-policy-result-1",
        "strategy_version": STRATEGY_VERSION,
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "fixed_start": FIXED_START.date().isoformat(),
        "fixed_end": latest.date().isoformat(),
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "commission_bps": COMMISSION_BPS,
        "slippage_bps": SLIPPAGE_BPS,
        "fx_spread_bps": FX_SPREAD_BPS,
        "tax_rate": TAX_RATE,
        "annual_deduction_krw": ANNUAL_DEDUCTION_KRW,
        "phase_a_count": int(len(phase_a)),
        "phase_b_count": int(len(phase_b)),
        "selected_count": int(len(selections)),
        "selected_policies": {
            str(row["portfolio_id"]): str(row["policy_id"])
            for _, row in selections.iterrows()
        },
        "chosen_exact_rebalance_months": chosen_months,
        "residuals": residuals,
        "input_snapshot": snapshot_manifest,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "outputs": {key: str(value) for key, value in outputs.items()},
    }
    _write_json(outputs["manifest"], manifest)
    return {"manifest": manifest, "outputs": outputs}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Taxable-account annual month, contribution correction, and rebalance-band research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run(refresh=args.refresh, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
