from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd


TRADING_DAYS = 252
INITIAL_CAPITAL_KRW = 80_000_000.0
MONTHLY_CONTRIBUTION_KRW = 500_000.0
COMMISSION_BPS = 5.0
SLIPPAGE_BPS = 5.0
FX_SPREAD_BPS = 10.0
TAX_RATE = 0.22
ANNUAL_DEDUCTION_KRW = 2_500_000.0


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    mode: str
    drawdown: float | None = None
    recovery_ma: int | None = None
    conversion_fraction: float = 0.0
    conversion_stages: int = 1
    stage_frequency: str = "monthly"
    switch_months: int = 0
    direct_ladder: tuple[tuple[float, float], ...] = ()


@dataclass
class Position:
    units: float = 0.0
    cost_basis_krw: float = 0.0

    @property
    def avg_cost_krw(self) -> float:
        return self.cost_basis_krw / self.units if self.units > 0 else 0.0


@dataclass
class PendingConversion:
    execute_dates: list[pd.Timestamp]
    units_per_stage: float
    remaining_stages: int
    trigger_date: pd.Timestamp


@dataclass
class SimulationResult:
    metrics: dict[str, Any]
    daily: pd.DataFrame
    trades: pd.DataFrame
    taxes: pd.DataFrame


def xirr(cashflows: list[tuple[pd.Timestamp, float]]) -> float | None:
    if not cashflows:
        return None
    cashflows = sorted(cashflows, key=lambda item: item[0])
    if not any(value < 0 for _, value in cashflows) or not any(
        value > 0 for _, value in cashflows
    ):
        return None
    origin = cashflows[0][0]
    times = np.array([(d - origin).days / 365.25 for d, _ in cashflows], dtype=float)
    values = np.array([v for _, v in cashflows], dtype=float)

    def npv(rate: float) -> float:
        if rate <= -0.999999:
            return np.inf
        return float(np.sum(values / np.power(1.0 + rate, times)))

    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    for _ in range(40):
        if math.isfinite(f_low) and math.isfinite(f_high) and f_low * f_high <= 0:
            break
        high *= 2
        f_high = npv(high)
    else:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        value = npv(mid)
        if abs(value) < 1e-7:
            return mid
        if f_low * value <= 0:
            high = mid
            f_high = value
        else:
            low = mid
            f_low = value
    return (low + high) / 2


def first_trading_days(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    values = pd.Series(index=index, data=index)
    return {
        pd.Timestamp(group.index.min())
        for _, group in values.groupby(index.to_period("M"))
    }


def _next_weekly_dates(
    index: pd.DatetimeIndex, trigger_date: pd.Timestamp, count: int
) -> list[pd.Timestamp]:
    dates: list[pd.Timestamp] = []
    cursor = pd.Timestamp(trigger_date)
    for _ in range(count):
        target = cursor + pd.Timedelta(days=1 if not dates else 7)
        pos = int(index.searchsorted(target, side="left"))
        if pos >= len(index):
            break
        cursor = pd.Timestamp(index[pos])
        dates.append(cursor)
    return dates


def _next_monthly_dates(
    index: pd.DatetimeIndex, trigger_date: pd.Timestamp, count: int
) -> list[pd.Timestamp]:
    dates: list[pd.Timestamp] = []
    cursor = pd.Timestamp(trigger_date)
    for step in range(1, count + 1):
        period = cursor.to_period("M") + step
        month_idx = index[index.to_period("M") == period]
        if len(month_idx):
            dates.append(pd.Timestamp(month_idx[0]))
    return dates


def schedule_dates(
    index: pd.DatetimeIndex,
    trigger_date: pd.Timestamp,
    count: int,
    frequency: str,
) -> list[pd.Timestamp]:
    if frequency == "weekly":
        return _next_weekly_dates(index, trigger_date, count)
    if frequency == "monthly":
        return _next_monthly_dates(index, trigger_date, count)
    raise ValueError(f"unsupported stage frequency: {frequency}")


def prepare_market(
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    synthetic_tqqq: pd.DataFrame | None = None,
) -> pd.DataFrame:
    master = frames["QQQ"].loc[start:end].index
    names = ["QQQ", "KRW=X"]
    if synthetic_tqqq is None:
        names.append("TQQQ")
    valid = pd.Series(True, index=master)
    for name in names:
        valid &= frames[name][["Open", "Close"]].reindex(master).notna().all(axis=1)
    if synthetic_tqqq is not None:
        valid &= synthetic_tqqq[["Open", "Close"]].reindex(master).notna().all(axis=1)
    index = master[valid.to_numpy()]
    data = pd.DataFrame(index=index)
    data["qqq_open"] = frames["QQQ"]["Open"].reindex(index).astype(float)
    data["qqq_close"] = frames["QQQ"]["Close"].reindex(index).astype(float)
    tqqq = frames["TQQQ"] if synthetic_tqqq is None else synthetic_tqqq
    data["tqqq_open"] = tqqq["Open"].reindex(index).astype(float)
    data["tqqq_close"] = tqqq["Close"].reindex(index).astype(float)
    data["fx_open"] = frames["KRW=X"]["Open"].reindex(index).ffill().astype(float)
    data["fx_close"] = frames["KRW=X"]["Close"].reindex(index).ffill().astype(float)
    qqq_history = frames["QQQ"]["Close"].loc[:end].astype(float)
    high_history = qqq_history.rolling(252, min_periods=126).max()
    data["qqq_high_252"] = high_history.reindex(index)
    data["qqq_drawdown"] = data["qqq_close"] / data["qqq_high_252"] - 1
    for window in (20, 50, 100):
        data[f"qqq_sma_{window}"] = qqq_history.rolling(window).mean().reindex(index)
    return data.dropna(subset=["qqq_high_252"])


def synthetic_tqqq_from_qqq(
    qqq: pd.DataFrame,
    *,
    annual_drag: float = 0.025,
) -> pd.DataFrame:
    frame = qqq[["Open", "Close"]].dropna().copy()
    daily_drag = annual_drag / TRADING_DAYS
    opens: list[float] = []
    closes: list[float] = []
    prior_qqq_close: float | None = None
    prior_close = 100.0
    for _, row in frame.iterrows():
        q_open = float(row["Open"])
        q_close = float(row["Close"])
        if prior_qqq_close is None:
            s_open = 100.0
            s_close = 100.0
        else:
            overnight = q_open / prior_qqq_close - 1
            full_day = q_close / prior_qqq_close - 1
            s_open = prior_close * max(0.001, 1 + 3 * overnight)
            s_close = prior_close * max(0.001, 1 + 3 * full_day - daily_drag)
        opens.append(s_open)
        closes.append(s_close)
        prior_qqq_close = q_close
        prior_close = s_close
    return pd.DataFrame({"Open": opens, "Close": closes}, index=frame.index)


class DcaSimulator:
    def __init__(
        self,
        market: pd.DataFrame,
        spec: StrategySpec,
        *,
        initial_capital_krw: float = INITIAL_CAPITAL_KRW,
        monthly_contribution_krw: float = MONTHLY_CONTRIBUTION_KRW,
        tax_deduction_krw: float = ANNUAL_DEDUCTION_KRW,
        commission_bps: float = COMMISSION_BPS,
        slippage_bps: float = SLIPPAGE_BPS,
        fx_spread_bps: float = FX_SPREAD_BPS,
        liquidate_at_end: bool = True,
    ) -> None:
        self.market = market.copy()
        self.spec = spec
        self.initial = float(initial_capital_krw)
        self.monthly = float(monthly_contribution_krw)
        self.tax_deduction = float(tax_deduction_krw)
        self.trade_rate = (commission_bps + slippage_bps) / 10_000
        self.fx_spread = fx_spread_bps / 10_000
        self.liquidate_at_end = liquidate_at_end
        self.positions = {"TQQQ": Position(), "QQQ": Position()}
        self.usd_cash = 0.0
        self.realized_by_year: dict[int, float] = {}
        self.tax_paid_krw = 0.0
        self.pending_tax_krw: dict[int, float] = {}
        self.pending_conversion: PendingConversion | None = None
        self.switch_remaining = 0
        self.armed = False
        self.triggered_this_cycle = False
        self.ladder_completed: set[float] = set()
        self.trades: list[dict[str, Any]] = []
        self.tax_rows: list[dict[str, Any]] = []
        self.external_cashflows: list[tuple[pd.Timestamp, float]] = []
        self.total_contributions = 0.0
        self.nav_units = 0.0
        self.daily_rows: list[dict[str, Any]] = []

    def _value_krw(self, row: pd.Series, *, close: bool = True) -> float:
        suffix = "close" if close else "open"
        fx = float(row[f"fx_{suffix}"])
        total_usd = self.usd_cash
        total_usd += self.positions["TQQQ"].units * float(row[f"tqqq_{suffix}"])
        total_usd += self.positions["QQQ"].units * float(row[f"qqq_{suffix}"])
        return total_usd * fx

    def _buy_with_krw(
        self,
        asset: str,
        amount_krw: float,
        row: pd.Series,
        date_: pd.Timestamp,
        reason: str,
        *,
        external: bool,
    ) -> None:
        if amount_krw <= 0:
            return
        fx = float(row["fx_open"])
        price = float(row[f"{asset.lower()}_open"])
        usd = amount_krw / (fx * (1 + self.fx_spread))
        usd_net = usd / (1 + self.trade_rate)
        units = usd_net / price
        if units <= 0:
            return
        position = self.positions[asset]
        position.units += units
        position.cost_basis_krw += amount_krw
        if external:
            self.total_contributions += amount_krw
            self.external_cashflows.append((date_, -amount_krw))
        self.trades.append(
            {
                "date": date_,
                "side": "BUY",
                "asset": asset,
                "units": units,
                "price_usd": price,
                "amount_krw": amount_krw,
                "reason": reason,
            }
        )

    def _sell_units(
        self,
        asset: str,
        units: float,
        row: pd.Series,
        date_: pd.Timestamp,
        reason: str,
    ) -> float:
        position = self.positions[asset]
        units = min(float(units), position.units)
        if units <= 0:
            return 0.0
        fx = float(row["fx_open"])
        price = float(row[f"{asset.lower()}_open"])
        gross_usd = units * price
        net_usd = gross_usd * (1 - self.trade_rate)
        proceeds_krw = net_usd * fx * (1 - self.fx_spread)
        basis = position.avg_cost_krw * units
        gain = proceeds_krw - basis
        position.units -= units
        position.cost_basis_krw -= basis
        if position.units <= 1e-12:
            position.units = 0.0
            position.cost_basis_krw = 0.0
        self.usd_cash += net_usd
        self.realized_by_year[date_.year] = self.realized_by_year.get(date_.year, 0.0) + gain
        self.trades.append(
            {
                "date": date_,
                "side": "SELL",
                "asset": asset,
                "units": units,
                "price_usd": price,
                "amount_krw": proceeds_krw,
                "realized_gain_krw": gain,
                "reason": reason,
            }
        )
        return proceeds_krw

    def _buy_from_usd_cash(
        self,
        asset: str,
        usd_amount: float,
        row: pd.Series,
        date_: pd.Timestamp,
        reason: str,
    ) -> None:
        usd_amount = min(float(usd_amount), self.usd_cash)
        if usd_amount <= 0:
            return
        price = float(row[f"{asset.lower()}_open"])
        fx = float(row["fx_open"])
        usd_net = usd_amount / (1 + self.trade_rate)
        units = usd_net / price
        self.usd_cash -= usd_amount
        position = self.positions[asset]
        position.units += units
        position.cost_basis_krw += usd_amount * fx
        self.trades.append(
            {
                "date": date_,
                "side": "BUY",
                "asset": asset,
                "units": units,
                "price_usd": price,
                "amount_krw": usd_amount * fx,
                "reason": reason,
            }
        )

    def _convert_qqq_to_tqqq(
        self, units: float, row: pd.Series, date_: pd.Timestamp, reason: str
    ) -> None:
        if units <= 0 or self.positions["QQQ"].units <= 0:
            return
        before_cash = self.usd_cash
        self._sell_units("QQQ", units, row, date_, reason)
        raised = self.usd_cash - before_cash
        self._buy_from_usd_cash("TQQQ", raised, row, date_, reason)

    def _fund_tax(self, amount_krw: float, row: pd.Series, date_: pd.Timestamp) -> None:
        if amount_krw <= 0:
            return
        fx = float(row["fx_open"])
        required_usd = amount_krw / fx
        cash_used = min(required_usd, self.usd_cash)
        self.usd_cash -= cash_used
        remaining = required_usd - cash_used
        for asset in ("QQQ", "TQQQ"):
            if remaining <= 1e-8:
                break
            price = float(row[f"{asset.lower()}_open"])
            units = min(self.positions[asset].units, remaining / (price * (1 - self.trade_rate)))
            before_cash = self.usd_cash
            self._sell_units(asset, units, row, date_, "tax_payment")
            raised = self.usd_cash - before_cash
            used = min(remaining, raised)
            self.usd_cash -= used
            remaining -= used
        paid = amount_krw - max(0.0, remaining * fx)
        self.tax_paid_krw += paid
        self.tax_rows.append({"date": date_, "tax_paid_krw": paid, "reason": "annual_tax"})

    def _schedule_tax(self, year: int) -> None:
        gain = self.realized_by_year.get(year, 0.0)
        taxable = max(0.0, gain - self.tax_deduction)
        self.pending_tax_krw[year + 1] = taxable * TAX_RATE

    def _process_due_tax(self, row: pd.Series, date_: pd.Timestamp) -> None:
        if date_.month != 5:
            return
        year = date_.year
        if year in self.pending_tax_krw:
            self._fund_tax(self.pending_tax_krw.pop(year), row, date_)

    def _schedule_conversion(self, row: pd.Series, date_: pd.Timestamp) -> None:
        fraction = self.spec.conversion_fraction
        if fraction <= 0 or self.positions["QQQ"].units <= 0:
            return
        stages = max(1, self.spec.conversion_stages)
        units = self.positions["QQQ"].units * fraction
        dates = schedule_dates(self.market.index, date_, stages, self.spec.stage_frequency)
        if not dates:
            return
        self.pending_conversion = PendingConversion(
            execute_dates=dates,
            units_per_stage=units / len(dates),
            remaining_stages=len(dates),
            trigger_date=date_,
        )

    def _process_conversion(self, row: pd.Series, date_: pd.Timestamp) -> None:
        pending = self.pending_conversion
        if pending is None or date_ not in pending.execute_dates:
            return
        self._convert_qqq_to_tqqq(pending.units_per_stage, row, date_, "drawdown_conversion")
        pending.remaining_stages -= 1
        if pending.remaining_stages <= 0:
            self.pending_conversion = None

    def _monthly_destination(self) -> str:
        if self.spec.mode == "always_tqqq":
            return "TQQQ"
        if self.spec.mode in {"always_qqq", "qqq_convert", "ladder"}:
            if self.switch_remaining > 0:
                self.switch_remaining -= 1
                return "TQQQ"
            return "QQQ"
        if self.spec.mode == "split_50_50":
            return "BOTH"
        raise ValueError(f"unsupported mode: {self.spec.mode}")

    def _process_contribution(self, row: pd.Series, date_: pd.Timestamp) -> None:
        destination = self._monthly_destination()
        if destination == "BOTH":
            self._buy_with_krw("TQQQ", self.monthly / 2, row, date_, "monthly_contribution", external=True)
            self._buy_with_krw("QQQ", self.monthly / 2, row, date_, "monthly_contribution", external=True)
        else:
            self._buy_with_krw(destination, self.monthly, row, date_, "monthly_contribution", external=True)

    def _trigger(self, row: pd.Series, prior: pd.Series | None, date_: pd.Timestamp) -> None:
        dd = float(row["qqq_drawdown"])
        if dd >= -0.02:
            self.armed = False
            self.triggered_this_cycle = False
            self.ladder_completed.clear()
        if self.spec.mode == "ladder":
            reached = [
                (threshold, cumulative)
                for threshold, cumulative in self.spec.direct_ladder
                if dd <= threshold
            ]
            if reached:
                new_cumulative = max(cumulative for _, cumulative in reached)
                completed_cumulative = max(
                    [
                        cumulative
                        for threshold, cumulative in self.spec.direct_ladder
                        if threshold in self.ladder_completed
                    ]
                    or [0.0]
                )
                incremental = max(0.0, new_cumulative - completed_cumulative)
                if incremental > 0 and self.positions["QQQ"].units > 0:
                    stages = max(1, self.spec.conversion_stages)
                    units = self.positions["QQQ"].units * incremental
                    dates = schedule_dates(self.market.index, date_, stages, self.spec.stage_frequency)
                    if dates:
                        self.pending_conversion = PendingConversion(
                            execute_dates=dates,
                            units_per_stage=units / len(dates),
                            remaining_stages=len(dates),
                            trigger_date=date_,
                        )
                if incremental > 0:
                    self.switch_remaining = max(self.switch_remaining, self.spec.switch_months)
                for threshold, _ in reached:
                    self.ladder_completed.add(threshold)
            return
        if self.spec.mode != "qqq_convert" or self.spec.drawdown is None:
            return
        if dd <= self.spec.drawdown:
            self.armed = True
        if self.triggered_this_cycle or not self.armed:
            return
        if self.spec.recovery_ma is None:
            triggered = dd <= self.spec.drawdown
        else:
            ma_col = f"qqq_sma_{self.spec.recovery_ma}"
            current_ma = row.get(ma_col, np.nan)
            if prior is None:
                triggered = False
            else:
                prior_ma = prior.get(ma_col, np.nan)
                triggered = (
                    pd.notna(current_ma)
                    and pd.notna(prior_ma)
                    and float(row["qqq_close"]) > float(current_ma)
                    and float(prior["qqq_close"]) <= float(prior_ma)
                )
        if triggered:
            self._schedule_conversion(row, date_)
            self.switch_remaining = max(self.switch_remaining, self.spec.switch_months)
            self.triggered_this_cycle = True
            self.armed = False

    def run(self) -> SimulationResult:
        if self.market.empty:
            raise ValueError("empty market")
        monthly_dates = first_trading_days(self.market.index)
        start_date = pd.Timestamp(self.market.index[0])
        first_row = self.market.iloc[0]
        self.nav_units = self.initial
        self._buy_with_krw("TQQQ", self.initial, first_row, start_date, "initial_capital", external=True)
        prior: pd.Series | None = None
        initial_month = start_date.to_period("M")
        for date_, row in self.market.iterrows():
            date_ = pd.Timestamp(date_)
            self._process_due_tax(row, date_)
            self._process_conversion(row, date_)
            if date_ in monthly_dates and date_.to_period("M") != initial_month:
                before = self._value_krw(row, close=False)
                unit_price = before / self.nav_units if self.nav_units > 0 else 1.0
                self.nav_units += self.monthly / unit_price
                self._process_contribution(row, date_)
            value = self._value_krw(row, close=True)
            nav = value / self.nav_units if self.nav_units > 0 else 0.0
            self.daily_rows.append(
                {
                    "Date": date_,
                    "value_krw": value,
                    "nav": nav,
                    "contributions_krw": self.total_contributions,
                    "profit_vs_contributions": value - self.total_contributions,
                    "tqqq_units": self.positions["TQQQ"].units,
                    "qqq_units": self.positions["QQQ"].units,
                }
            )
            self._trigger(row, prior, date_)
            if prior is not None and date_.year != prior.name.year:
                self._schedule_tax(prior.name.year)
            prior = row.copy()
            prior.name = date_
        if prior is not None:
            self._schedule_tax(prior.name.year)

        daily = pd.DataFrame(self.daily_rows).set_index("Date")
        final_date = pd.Timestamp(self.market.index[-1])
        final_row = self.market.iloc[-1]
        pre_tax_value = self._value_krw(final_row, close=True)
        terminal_tax = 0.0
        if self.liquidate_at_end:
            gains: dict[int, float] = {}
            for asset, position in self.positions.items():
                if position.units <= 0:
                    continue
                fx = float(final_row["fx_close"])
                price = float(final_row[f"{asset.lower()}_close"])
                proceeds = position.units * price * (1 - self.trade_rate) * fx * (1 - self.fx_spread)
                gains[final_date.year] = gains.get(final_date.year, 0.0) + proceeds - position.cost_basis_krw
            final_gain = self.realized_by_year.get(final_date.year, 0.0) + gains.get(final_date.year, 0.0)
            terminal_tax = max(0.0, final_gain - self.tax_deduction) * TAX_RATE
        current_due_year = final_date.year + 1
        cumulative_scheduled_tax = sum(
            amount for due_year, amount in self.pending_tax_krw.items() if due_year != current_due_year
        )
        fx_close = float(final_row["fx_close"])
        liquidation_value = self.usd_cash * fx_close * (1 - self.fx_spread)
        for asset, position in self.positions.items():
            if position.units <= 0:
                continue
            price = float(final_row[f"{asset.lower()}_close"])
            liquidation_value += position.units * price * (1 - self.trade_rate) * fx_close * (1 - self.fx_spread)
        after_tax_liquidation = liquidation_value - terminal_tax - cumulative_scheduled_tax

        self.external_cashflows.append((final_date, pre_tax_value))
        pretax_xirr = xirr(self.external_cashflows)
        after_tax_cashflows = list(self.external_cashflows[:-1]) + [(final_date, after_tax_liquidation)]
        after_tax_xirr = xirr(after_tax_cashflows)
        nav = daily["nav"]
        mdd = float((nav / nav.cummax() - 1).min())
        min_principal_ratio = float((daily["value_krw"] / daily["contributions_krw"] - 1).min())
        elapsed = max(1, (final_date - start_date).days) / 365.25
        twr_cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / elapsed) - 1)
        metrics = {
            "strategy_id": self.spec.strategy_id,
            "mode": self.spec.mode,
            "start": start_date.date().isoformat(),
            "end": final_date.date().isoformat(),
            "initial_capital_krw": self.initial,
            "monthly_contribution_krw": self.monthly,
            "total_contributions_krw": self.total_contributions,
            "pre_tax_value_krw": pre_tax_value,
            "after_tax_liquidation_value_krw": after_tax_liquidation,
            "pre_tax_profit_krw": pre_tax_value - self.total_contributions,
            "after_tax_profit_krw": after_tax_liquidation - self.total_contributions,
            "pre_tax_xirr": pretax_xirr,
            "after_tax_xirr": after_tax_xirr,
            "twr_cagr": twr_cagr,
            "mdd": mdd,
            "minimum_vs_contributions": min_principal_ratio,
            "tax_paid_krw": self.tax_paid_krw,
            "pending_tax_krw": cumulative_scheduled_tax,
            "terminal_tax_krw": terminal_tax,
            "trade_count": len(self.trades),
            "conversion_trade_count": sum(1 for trade in self.trades if "conversion" in trade["reason"]),
            "final_tqqq_weight": (
                self.positions["TQQQ"].units * float(final_row["tqqq_close"]) * float(final_row["fx_close"]) / pre_tax_value
                if pre_tax_value > 0
                else 0.0
            ),
            "final_qqq_weight": (
                self.positions["QQQ"].units * float(final_row["qqq_close"]) * float(final_row["fx_close"]) / pre_tax_value
                if pre_tax_value > 0
                else 0.0
            ),
        }
        return SimulationResult(
            metrics=metrics,
            daily=daily,
            trades=pd.DataFrame(self.trades),
            taxes=pd.DataFrame(self.tax_rows),
        )


def build_strategy_grid() -> list[StrategySpec]:
    specs: list[StrategySpec] = [
        StrategySpec("always_tqqq", "always_tqqq"),
        StrategySpec("always_qqq_contrib", "always_qqq"),
        StrategySpec("split_50_50", "split_50_50"),
    ]
    for drawdown in (-0.15, -0.20, -0.25, -0.30, -0.35, -0.40, -0.45):
        for recovery_ma in (None, 20, 50, 100):
            for fraction in (0.0, 0.25, 0.50, 0.75, 1.00):
                for stages in (1, 3):
                    for frequency in ("weekly", "monthly"):
                        for switch_months in (3, 6, 12):
                            strategy_id = (
                                f"convert_dd{int(abs(drawdown)*100)}_"
                                f"ma{recovery_ma or 0}_f{int(fraction*100)}_"
                                f"s{stages}_{frequency}_nm{switch_months}"
                            )
                            specs.append(
                                StrategySpec(
                                    strategy_id,
                                    "qqq_convert",
                                    drawdown=drawdown,
                                    recovery_ma=recovery_ma,
                                    conversion_fraction=fraction,
                                    conversion_stages=stages,
                                    stage_frequency=frequency,
                                    switch_months=switch_months,
                                )
                            )
    ladders = [
        ((-0.20, 0.25), (-0.30, 0.50), (-0.40, 1.00)),
        ((-0.25, 0.33), (-0.35, 0.66), (-0.45, 1.00)),
        ((-0.20, 0.50), (-0.35, 1.00)),
    ]
    for number, ladder in enumerate(ladders, start=1):
        for stages in (1, 3):
            for frequency in ("weekly", "monthly"):
                for switch_months in (3, 6, 12):
                    specs.append(
                        StrategySpec(
                            f"ladder{number}_s{stages}_{frequency}_nm{switch_months}",
                            "ladder",
                            conversion_stages=stages,
                            stage_frequency=frequency,
                            switch_months=switch_months,
                            direct_ladder=ladder,
                        )
                    )
    return specs
