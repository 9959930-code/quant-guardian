from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from equity_v2_dca_engine import xirr
from equity_v2_engine import FeatureStore, schedule_dates


TRADING_DAYS = 252
INITIAL_CAPITAL_KRW = 80_000_000.0
MONTHLY_CONTRIBUTION_KRW = 500_000.0
COMMISSION_BPS = 5.0
SLIPPAGE_BPS = 5.0
FX_SPREAD_BPS = 10.0
TAX_RATE = 0.22
ANNUAL_DEDUCTION_KRW = 2_500_000.0
RISK_ASSETS = ("QQQ", "QLD", "TQQQ")
ALL_ASSETS = (*RISK_ASSETS, "CASH")


@dataclass(frozen=True)
class ModernCandidate:
    candidate_id: str
    family: str
    params: Mapping[str, Any]

    def record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "params_json": json.dumps(
                dict(self.params), ensure_ascii=False, sort_keys=True
            ),
        }


@dataclass
class TaxPosition:
    units: float = 0.0
    basis_krw: float = 0.0

    @property
    def average_basis_krw(self) -> float:
        return self.basis_krw / self.units if self.units > 0 else 0.0


@dataclass
class TaxableSimulation:
    metrics: dict[str, Any]
    daily: pd.DataFrame
    trades: pd.DataFrame
    taxes: pd.DataFrame


def _encode(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}".replace("-", "m").replace(".", "p")
    if isinstance(value, (tuple, list)):
        return "-".join(_encode(item) for item in value)
    if isinstance(value, Mapping):
        return "-".join(
            f"{key}{_encode(item)}" for key, item in sorted(value.items())
        )
    return str(value).replace("-", "m").replace(".", "p").replace(" ", "")


def candidate_id(family: str, params: Mapping[str, Any]) -> str:
    readable = "_".join(
        f"{key}{_encode(value)}" for key, value in sorted(params.items())
    )
    if len(readable) <= 150:
        return f"{family}_{readable}"
    digest = hashlib.sha1(
        json.dumps(dict(params), sort_keys=True).encode("utf-8")
    ).hexdigest()[:14]
    return f"{family}_{digest}"


def _normalize(weights: Mapping[str, float]) -> dict[str, float]:
    cleaned = {
        str(asset): max(0.0, float(weight))
        for asset, weight in weights.items()
        if float(weight) > 1e-12
    }
    total = sum(cleaned.values())
    if total < 1.0 - 1e-10:
        cleaned["CASH"] = cleaned.get("CASH", 0.0) + 1.0 - total
        total = 1.0
    if total <= 0:
        return {"CASH": 1.0}
    return {asset: weight / total for asset, weight in cleaned.items()}


def allocation_patterns() -> dict[str, dict[str, float]]:
    return {
        "q100": {"QQQ": 1.0},
        "l100": {"QLD": 1.0},
        "t100": {"TQQQ": 1.0},
        "t25_q75": {"TQQQ": 0.25, "QQQ": 0.75},
        "t50_q50": {"TQQQ": 0.50, "QQQ": 0.50},
        "t75_q25": {"TQQQ": 0.75, "QQQ": 0.25},
        "t25_l75": {"TQQQ": 0.25, "QLD": 0.75},
        "t50_l50": {"TQQQ": 0.50, "QLD": 0.50},
        "t75_l25": {"TQQQ": 0.75, "QLD": 0.25},
        "l50_q50": {"QLD": 0.50, "QQQ": 0.50},
        "t50_cash50": {"TQQQ": 0.50, "CASH": 0.50},
        "l75_cash25": {"QLD": 0.75, "CASH": 0.25},
    }


def build_candidate_grid() -> list[ModernCandidate]:
    rows: list[ModernCandidate] = []
    patterns = allocation_patterns()

    def add(family: str, params: dict[str, Any]) -> None:
        rows.append(ModernCandidate(candidate_id(family, params), family, params))

    for asset in ("QQQ", "QLD", "TQQQ"):
        add("buy_hold", {"asset": asset})

    fixed_names = (
        "q100",
        "l100",
        "t100",
        "t25_q75",
        "t50_q50",
        "t75_q25",
        "t25_l75",
        "t50_l50",
        "t75_l25",
        "l50_q50",
        "t50_cash50",
        "l75_cash25",
    )
    for allocation in fixed_names:
        for frequency in ("none", "annual", "quarterly"):
            add(
                "fixed_mix",
                {"allocation": allocation, "frequency": frequency},
            )

    risk_on_names = (
        "q100",
        "l100",
        "t100",
        "t25_q75",
        "t50_q50",
        "t75_q25",
        "t25_l75",
        "t50_l50",
        "t75_l25",
        "l50_q50",
    )
    for entry_ma in (150, 180, 200, 220, 250):
        for exit_ma in (180, 200, 220):
            for frequency in ("weekly", "monthly"):
                for confirm in (1, 2):
                    for slope_days in (0, 20):
                        for allocation in risk_on_names:
                            for risk_off in ("CASH", "QQQ"):
                                add(
                                    "trend",
                                    {
                                        "entry_ma": entry_ma,
                                        "exit_ma": exit_ma,
                                        "frequency": frequency,
                                        "confirm": confirm,
                                        "slope_days": slope_days,
                                        "allocation": allocation,
                                        "risk_off": risk_off,
                                    },
                                )

    strong_names = (
        "t100",
        "t50_q50",
        "t75_q25",
        "t50_l50",
        "t75_l25",
        "l100",
    )
    for long_ma in (150, 180, 200, 220):
        for breakout_days in (20, 55, 90):
            for momentum_days in (63, 126):
                for frequency in ("weekly", "monthly"):
                    for strong_allocation in strong_names:
                        for moderate_asset in ("QQQ", "QLD"):
                            for max_vol in (0.0, 0.35, 0.45):
                                add(
                                    "tiered",
                                    {
                                        "long_ma": long_ma,
                                        "breakout_days": breakout_days,
                                        "momentum_days": momentum_days,
                                        "frequency": frequency,
                                        "strong_allocation": strong_allocation,
                                        "moderate_asset": moderate_asset,
                                        "max_vol": max_vol,
                                    },
                                )

    breakout_names = (
        "t100",
        "t50_q50",
        "t75_q25",
        "t50_l50",
        "t75_l25",
        "l100",
        "l50_q50",
    )
    for breakout_days in (20, 55, 90, 126):
        for long_ma in (150, 180, 200, 220):
            for exit_ma in (180, 200, 220):
                for frequency in ("weekly", "monthly"):
                    for parts in (1, 2, 3):
                        for allocation in breakout_names:
                            for confirm in (1, 2):
                                add(
                                    "breakout",
                                    {
                                        "breakout_days": breakout_days,
                                        "long_ma": long_ma,
                                        "exit_ma": exit_ma,
                                        "frequency": frequency,
                                        "parts": parts,
                                        "allocation": allocation,
                                        "confirm": confirm,
                                    },
                                )

    recovery_names = (
        "t100",
        "t50_q50",
        "t75_q25",
        "t50_l50",
        "t75_l25",
        "l100",
    )
    exit_variants = (
        {"exit_rule": "ma", "exit_ma": 200},
        {"exit_rule": "trailing", "trailing_stop": 0.20},
        {"exit_rule": "trailing", "trailing_stop": 0.30},
    )
    for drawdown in (0.15, 0.20, 0.25, 0.30, 0.35):
        for recovery_ma in (20, 50, 100):
            for long_ma in (150, 180, 200, 220):
                for exit_variant in exit_variants:
                    for frequency in ("weekly", "monthly"):
                        for parts in (1, 3):
                            for allocation in recovery_names:
                                add(
                                    "drawdown_recovery",
                                    {
                                        "drawdown": drawdown,
                                        "recovery_ma": recovery_ma,
                                        "long_ma": long_ma,
                                        "frequency": frequency,
                                        "parts": parts,
                                        "allocation": allocation,
                                        **exit_variant,
                                    },
                                )

    unique = {row.candidate_id: row for row in rows}
    if len(unique) != len(rows):
        raise RuntimeError("duplicate modern candidate IDs")
    return rows


def _append_target(
    rows: list[dict[str, Any]],
    date: pd.Timestamp,
    weights: Mapping[str, float],
) -> None:
    normalized = _normalize(weights)
    if rows:
        previous = rows[-1]["weights"]
        keys = set(previous) | set(normalized)
        if all(
            abs(previous.get(key, 0.0) - normalized.get(key, 0.0)) < 1e-10
            for key in keys
        ):
            return
    rows.append({"date": pd.Timestamp(date), "weights": normalized})


def _targets_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    assets = sorted(
        {asset for row in rows for asset in dict(row["weights"]).keys()}
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {"Date": pd.Timestamp(row["date"])}
        record.update(
            {
                asset: float(row["weights"].get(asset, 0.0))
                for asset in assets
            }
        )
        records.append(record)
    return (
        pd.DataFrame(records)
        .set_index("Date")
        .sort_index()
        .loc[lambda frame: ~frame.index.duplicated(keep="last")]
    )


def _partial_allocation(
    final_weights: Mapping[str, float], fraction: float
) -> dict[str, float]:
    fraction = min(1.0, max(0.0, float(fraction)))
    weights = {
        asset: weight * fraction
        for asset, weight in final_weights.items()
        if asset != "CASH"
    }
    weights["CASH"] = 1.0 - sum(weights.values())
    return _normalize(weights)


def generate_targets(
    candidate: ModernCandidate,
    *,
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    p = dict(candidate.params)
    index = store.close.loc[start:end].index
    if len(index) < 3:
        return pd.DataFrame()
    patterns = allocation_patterns()
    rows: list[dict[str, Any]] = []

    if candidate.family == "buy_hold":
        _append_target(rows, index[0], {str(p["asset"]): 1.0})
        return _targets_frame(rows)

    if candidate.family == "fixed_mix":
        weights = patterns[str(p["allocation"])]
        frequency = str(p["frequency"])
        dates = (
            pd.DatetimeIndex([index[0]])
            if frequency == "none"
            else schedule_dates(index, frequency)
        )
        for date in dates:
            _append_target(rows, date, weights)
        return _targets_frame(rows)

    dates = schedule_dates(index, str(p["frequency"]))
    qqq = store.close["QQQ"]

    if candidate.family == "trend":
        entry_ma = store.sma("QQQ", int(p["entry_ma"]))
        exit_ma = store.sma("QQQ", int(p["exit_ma"]))
        slope_days = int(p["slope_days"])
        state = "out"
        entry_streak = 0
        exit_streak = 0
        risk_on = patterns[str(p["allocation"])]
        risk_off = {str(p["risk_off"]): 1.0}
        _append_target(rows, index[0], risk_off)
        for date in dates:
            close = qqq.get(date, np.nan)
            entry_value = entry_ma.get(date, np.nan)
            exit_value = exit_ma.get(date, np.nan)
            if any(pd.isna(value) for value in (close, entry_value, exit_value)):
                continue
            entry_ok = float(close) > float(entry_value)
            if slope_days:
                prior = entry_ma.shift(slope_days).get(date, np.nan)
                entry_ok = entry_ok and pd.notna(prior) and float(entry_value) > float(prior)
            exit_ok = float(close) < float(exit_value)
            if state == "out":
                entry_streak = entry_streak + 1 if entry_ok else 0
                if entry_streak >= int(p["confirm"]):
                    state = "in"
                    entry_streak = 0
                    _append_target(rows, date, risk_on)
            else:
                exit_streak = exit_streak + 1 if exit_ok else 0
                if exit_streak >= int(p["confirm"]):
                    state = "out"
                    exit_streak = 0
                    _append_target(rows, date, risk_off)
        return _targets_frame(rows)

    if candidate.family == "tiered":
        long_ma = store.sma("QQQ", int(p["long_ma"]))
        prior_high = store.rolling_high(
            "QQQ", int(p["breakout_days"])
        ).shift(1)
        momentum = store.ret("QQQ", int(p["momentum_days"]))
        volatility = store.vol("QQQ", 63)
        strong = patterns[str(p["strong_allocation"])]
        moderate = {str(p["moderate_asset"]): 1.0}
        _append_target(rows, index[0], {"CASH": 1.0})
        for date in dates:
            values = (
                qqq.get(date, np.nan),
                long_ma.get(date, np.nan),
                prior_high.get(date, np.nan),
                momentum.get(date, np.nan),
                volatility.get(date, np.nan),
            )
            if any(pd.isna(value) for value in values):
                continue
            close, ma_value, high, mom, vol = map(float, values)
            if close <= ma_value:
                weights = {"CASH": 1.0}
            else:
                strong_ok = close >= high and mom > 0
                max_vol = float(p["max_vol"])
                if max_vol > 0:
                    strong_ok = strong_ok and vol <= max_vol
                weights = strong if strong_ok else moderate
            _append_target(rows, date, weights)
        return _targets_frame(rows)

    if candidate.family == "breakout":
        long_ma = store.sma("QQQ", int(p["long_ma"]))
        exit_ma = store.sma("QQQ", int(p["exit_ma"]))
        prior_high = store.rolling_high(
            "QQQ", int(p["breakout_days"])
        ).shift(1)
        final_weights = patterns[str(p["allocation"])]
        parts = int(p["parts"])
        state = "out"
        stage = 0
        entry_streak = 0
        exit_streak = 0
        _append_target(rows, index[0], {"CASH": 1.0})
        for date in dates:
            values = (
                qqq.get(date, np.nan),
                long_ma.get(date, np.nan),
                exit_ma.get(date, np.nan),
                prior_high.get(date, np.nan),
            )
            if any(pd.isna(value) for value in values):
                continue
            close, long_value, exit_value, high = map(float, values)
            entry_ok = close >= high and close > long_value
            exit_ok = close < exit_value
            if state == "out":
                entry_streak = entry_streak + 1 if entry_ok else 0
                if entry_streak >= int(p["confirm"]):
                    state = "entering"
                    stage = 1
                    _append_target(
                        rows,
                        date,
                        _partial_allocation(final_weights, stage / parts),
                    )
                    if parts == 1:
                        state = "holding"
                    entry_streak = 0
                continue
            if state == "entering":
                if exit_ok:
                    state = "out"
                    stage = 0
                    _append_target(rows, date, {"CASH": 1.0})
                else:
                    stage += 1
                    _append_target(
                        rows,
                        date,
                        _partial_allocation(final_weights, stage / parts),
                    )
                    if stage >= parts:
                        state = "holding"
                continue
            if state == "holding":
                exit_streak = exit_streak + 1 if exit_ok else 0
                if exit_streak >= int(p["confirm"]):
                    state = "exiting"
                    stage = 1
                    _append_target(
                        rows,
                        date,
                        _partial_allocation(final_weights, 1 - stage / parts),
                    )
                    if parts == 1:
                        state = "out"
                    exit_streak = 0
                continue
            if state == "exiting":
                stage += 1
                _append_target(
                    rows,
                    date,
                    _partial_allocation(final_weights, 1 - stage / parts),
                )
                if stage >= parts:
                    state = "out"
        return _targets_frame(rows)

    if candidate.family == "drawdown_recovery":
        rolling_high = store.rolling_high("QQQ", 252)
        recovery_ma = store.sma("QQQ", int(p["recovery_ma"]))
        long_ma = store.sma("QQQ", int(p["long_ma"]))
        final_weights = patterns[str(p["allocation"])]
        parts = int(p["parts"])
        state = "out"
        armed = False
        stage = 0
        peak = -np.inf
        _append_target(rows, index[0], {"CASH": 1.0})
        for date in dates:
            values = (
                qqq.get(date, np.nan),
                rolling_high.get(date, np.nan),
                recovery_ma.get(date, np.nan),
                long_ma.get(date, np.nan),
            )
            if any(pd.isna(value) for value in values):
                continue
            close, high, recovery_value, long_value = map(float, values)
            drawdown = close / high - 1.0
            if state == "out":
                if drawdown <= -float(p["drawdown"]):
                    armed = True
                if armed and close > recovery_value and close > long_value:
                    state = "entering"
                    stage = 1
                    peak = close
                    _append_target(
                        rows,
                        date,
                        _partial_allocation(final_weights, stage / parts),
                    )
                    if parts == 1:
                        state = "holding"
                    armed = False
                continue
            if state == "entering":
                peak = max(peak, close)
                stage += 1
                _append_target(
                    rows,
                    date,
                    _partial_allocation(final_weights, stage / parts),
                )
                if stage >= parts:
                    state = "holding"
                continue
            if state == "holding":
                peak = max(peak, close)
                if str(p["exit_rule"]) == "ma":
                    exit_value = store.sma("QQQ", int(p["exit_ma"])).get(
                        date, np.nan
                    )
                    exit_ok = pd.notna(exit_value) and close < float(exit_value)
                else:
                    exit_ok = close / peak - 1.0 <= -float(p["trailing_stop"])
                if exit_ok:
                    state = "exiting"
                    stage = 1
                    _append_target(
                        rows,
                        date,
                        _partial_allocation(final_weights, 1 - stage / parts),
                    )
                    if parts == 1:
                        state = "out"
                        peak = -np.inf
                continue
            if state == "exiting":
                stage += 1
                _append_target(
                    rows,
                    date,
                    _partial_allocation(final_weights, 1 - stage / parts),
                )
                if stage >= parts:
                    state = "out"
                    peak = -np.inf
        return _targets_frame(rows)

    raise ValueError(f"unsupported family: {candidate.family}")


def first_trading_days(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    values = pd.Series(index=index, data=index)
    return {
        pd.Timestamp(group.index.min())
        for _, group in values.groupby(index.to_period("M"))
    }


class GenericTaxableSimulator:
    def __init__(
        self,
        *,
        frames: Mapping[str, pd.DataFrame],
        targets: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
        initial_capital_krw: float = INITIAL_CAPITAL_KRW,
        monthly_contribution_krw: float = MONTHLY_CONTRIBUTION_KRW,
        commission_bps: float = COMMISSION_BPS,
        slippage_bps: float = SLIPPAGE_BPS,
        fx_spread_bps: float = FX_SPREAD_BPS,
        tax_rate: float = TAX_RATE,
        annual_deduction_krw: float = ANNUAL_DEDUCTION_KRW,
    ) -> None:
        self.frames = frames
        self.targets = targets.copy()
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        self.initial = float(initial_capital_krw)
        self.monthly = float(monthly_contribution_krw)
        self.trade_rate = (commission_bps + slippage_bps) / 10_000
        self.fx_spread = fx_spread_bps / 10_000
        self.tax_rate = float(tax_rate)
        self.deduction = float(annual_deduction_krw)
        self.positions = {asset: TaxPosition() for asset in ALL_ASSETS}
        self.realized_by_year: dict[int, float] = {}
        self.current_target = {"CASH": 1.0}
        self.trades: list[dict[str, Any]] = []
        self.taxes: list[dict[str, Any]] = []
        self.cashflows: list[tuple[pd.Timestamp, float]] = []
        self.total_contributions = 0.0
        self.nav_units = 0.0

    def _build_index(self) -> pd.DatetimeIndex:
        master = self.frames["QQQ"].loc[self.start : self.end].index
        valid = pd.Series(True, index=master)
        for asset in ALL_ASSETS:
            valid &= self.frames[asset][["Open", "Close"]].reindex(master).notna().all(
                axis=1
            )
        valid &= self.frames["KRW=X"][["Open", "Close"]].reindex(master).notna().all(
            axis=1
        )
        return master[valid.to_numpy()]

    def _price(self, asset: str, date: pd.Timestamp, field: str) -> float:
        return float(self.frames[asset].loc[date, field])

    def _fx(self, date: pd.Timestamp, field: str) -> float:
        return float(self.frames["KRW=X"].loc[date, field])

    def _asset_value_usd(self, asset: str, date: pd.Timestamp, field: str) -> float:
        return self.positions[asset].units * self._price(asset, date, field)

    def _value_usd(self, date: pd.Timestamp, field: str) -> float:
        return sum(self._asset_value_usd(asset, date, field) for asset in ALL_ASSETS)

    def _value_krw(self, date: pd.Timestamp, field: str) -> float:
        return self._value_usd(date, field) * self._fx(date, field)

    def _sell(
        self,
        asset: str,
        units: float,
        date: pd.Timestamp,
        reason: str,
    ) -> float:
        position = self.positions[asset]
        units = min(float(units), position.units)
        if units <= 1e-12:
            return 0.0
        price = self._price(asset, date, "Open")
        proceeds_usd = units * price * (1 - self.trade_rate)
        if asset != "CASH":
            fx = self._fx(date, "Open")
            proceeds_krw = proceeds_usd * fx
            basis = position.average_basis_krw * units
            gain = proceeds_krw - basis
            position.basis_krw -= basis
            self.realized_by_year[date.year] = (
                self.realized_by_year.get(date.year, 0.0) + gain
            )
        else:
            gain = 0.0
        position.units -= units
        if position.units <= 1e-10:
            position.units = 0.0
            if asset != "CASH":
                position.basis_krw = 0.0
        self.trades.append(
            {
                "date": date,
                "side": "SELL",
                "asset": asset,
                "units": units,
                "price_usd": price,
                "reason": reason,
                "realized_gain_krw": gain,
            }
        )
        return proceeds_usd

    def _buy(
        self,
        asset: str,
        usd_amount: float,
        date: pd.Timestamp,
        reason: str,
        *,
        external_basis_krw: float | None = None,
    ) -> float:
        if usd_amount <= 1e-10:
            return 0.0
        price = self._price(asset, date, "Open")
        net_usd = usd_amount / (1 + self.trade_rate)
        units = net_usd / price
        position = self.positions[asset]
        position.units += units
        if asset != "CASH":
            basis_krw = (
                float(external_basis_krw)
                if external_basis_krw is not None
                else usd_amount * self._fx(date, "Open")
            )
            position.basis_krw += basis_krw
        self.trades.append(
            {
                "date": date,
                "side": "BUY",
                "asset": asset,
                "units": units,
                "price_usd": price,
                "reason": reason,
            }
        )
        return units

    def _rebalance(
        self, date: pd.Timestamp, target: Mapping[str, float], reason: str
    ) -> None:
        target = _normalize(target)
        total = self._value_usd(date, "Open")
        if total <= 0:
            self.current_target = target
            return
        desired = {asset: total * target.get(asset, 0.0) for asset in ALL_ASSETS}
        free_usd = 0.0
        for asset in ALL_ASSETS:
            current = self._asset_value_usd(asset, date, "Open")
            excess = current - desired[asset]
            if excess > 1e-8:
                units = excess / self._price(asset, date, "Open")
                free_usd += self._sell(asset, units, date, reason)
        for asset in ALL_ASSETS:
            current = self._asset_value_usd(asset, date, "Open")
            shortfall = desired[asset] - current
            if shortfall > 1e-8 and free_usd > 0:
                spend = min(shortfall, free_usd)
                self._buy(asset, spend, date, reason)
                free_usd -= spend
        if free_usd > 1e-8:
            self._buy("CASH", free_usd, date, reason)
        self.current_target = target

    def _contribute(self, date: pd.Timestamp, amount_krw: float) -> None:
        if amount_krw <= 0:
            return
        fx = self._fx(date, "Open")
        usd_total = amount_krw / (fx * (1 + self.fx_spread))
        target = _normalize(self.current_target)
        for asset in ALL_ASSETS:
            allocated_krw = amount_krw * target.get(asset, 0.0)
            allocated_usd = usd_total * target.get(asset, 0.0)
            self._buy(
                asset,
                allocated_usd,
                date,
                "contribution",
                external_basis_krw=allocated_krw,
            )
        self.total_contributions += amount_krw
        self.cashflows.append((date, -amount_krw))

    def _pay_tax(self, date: pd.Timestamp, prior_year: int) -> None:
        gain = self.realized_by_year.get(prior_year, 0.0)
        tax = max(0.0, gain - self.deduction) * self.tax_rate
        if tax <= 0:
            return
        required_usd = tax / self._fx(date, "Open")
        raised = 0.0
        for asset in ("CASH", "QQQ", "QLD", "TQQQ"):
            if raised >= required_usd - 1e-8:
                break
            price = self._price(asset, date, "Open")
            available = self.positions[asset].units * price * (1 - self.trade_rate)
            need = required_usd - raised
            units = min(
                self.positions[asset].units,
                need / (price * (1 - self.trade_rate)),
            )
            raised += self._sell(asset, units, date, "tax_payment")
        paid = min(required_usd, raised) * self._fx(date, "Open")
        excess = raised - required_usd
        if excess > 1e-8:
            self._buy("CASH", excess, date, "tax_change")
        self.taxes.append({"date": date, "tax_krw": paid, "tax_year": prior_year})

    def run(self) -> TaxableSimulation:
        index = self._build_index()
        if len(index) < 250:
            raise ValueError("taxable simulation period is too short")
        execution_map: dict[pd.Timestamp, dict[str, float]] = {}
        for signal_date, row in self.targets.sort_index().iterrows():
            position = int(index.searchsorted(pd.Timestamp(signal_date), side="right"))
            if position >= len(index):
                continue
            weights = {
                asset: float(value)
                for asset, value in row.items()
                if pd.notna(value) and float(value) > 1e-12
            }
            execution_map[pd.Timestamp(index[position])] = _normalize(weights)
        monthly_dates = first_trading_days(index)
        first_date = pd.Timestamp(index[0])
        initial_month = first_date.to_period("M")
        daily_rows: list[dict[str, Any]] = []
        previous_year = first_date.year

        if first_date in execution_map:
            self.current_target = execution_map[first_date]
        self._contribute(first_date, self.initial)
        self.nav_units = self.initial

        for date in index:
            date = pd.Timestamp(date)
            if date.year != previous_year:
                self._pay_tax(date, previous_year)
                previous_year = date.year
            if date in execution_map:
                self._rebalance(date, execution_map[date], "strategy_rebalance")
            if date in monthly_dates and date.to_period("M") != initial_month:
                before = self._value_krw(date, "Open")
                unit_price = before / self.nav_units if self.nav_units > 0 else 1.0
                self.nav_units += self.monthly / unit_price
                self._contribute(date, self.monthly)
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
        elapsed_years = max(1, (final_date - first_date).days) / 365.25
        metrics = {
            "start": first_date.date().isoformat(),
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
        }
        return TaxableSimulation(
            metrics=metrics,
            daily=daily,
            trades=pd.DataFrame(self.trades),
            taxes=pd.DataFrame(self.taxes),
        )
