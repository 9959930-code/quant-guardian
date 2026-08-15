from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from quant_guardian import (
    DEFAULT_CONFIG,
    fetch_text,
    fetch_yahoo_price,
    load_config,
    read_price,
    resolve_paths,
)


STRATEGY_VERSION = "equity-v2-isa-tiger-qld-0.1"
FIXED_START = pd.Timestamp("2006-08-01")
FIXED_END = pd.Timestamp("2026-08-12")
DOTCOM_START = pd.Timestamp("1999-03-10")
DOTCOM_END = pd.Timestamp("2003-12-31")
ACTUAL_TIGER_TICKER = "418660.KS"
TIGER_LISTING_REFERENCE = pd.Timestamp("2022-02-22")
TRADING_DAYS = 252
INITIAL_CAPITAL_KRW = 10_000_000.0
MONTHLY_CONTRIBUTION_KRW = 500_000.0
ISA_TOTAL_CONTRIBUTION_LIMIT_KRW = 100_000_000.0
DIRECT_ANNUAL_DEDUCTION_KRW = 2_500_000.0
DIRECT_TAX_RATE = 0.22
ISA_GENERAL_EXEMPTION_KRW = 2_000_000.0
ISA_LOW_INCOME_EXEMPTION_KRW = 4_000_000.0
ISA_SEPARATE_TAX_RATE = 0.099
DIRECT_ONE_WAY_COST = 0.0020
TIGER_ONE_WAY_COST = 0.0010
FRED_KRW_USD = "DEXKOUS"
FRED_KR_3M = "IR3TIB01KRM156N"
SELL_Z = 1.25
BUY_Z = 0.75
REDUCED_WEIGHT = 0.25

DEV_FOLDS = {
    "gfc_qe": (pd.Timestamp("2006-08-01"), pd.Timestamp("2011-12-30")),
    "post_gfc": (pd.Timestamp("2012-01-03"), pd.Timestamp("2018-12-31")),
    "covid_inflation": (pd.Timestamp("2019-01-02"), pd.Timestamp("2022-12-30")),
}
HOLDOUT = (pd.Timestamp("2023-01-03"), FIXED_END)

CURRENT_DIRECT_KRW = {
    "TQQQ": 9_000_000.0,
    "QLD": 7_000_000.0,
    "SOXL": 1_000_000.0,
    "CASH": 3_000_000.0,
}
CURRENT_ISA_KRW = {
    "NASDAQ_1X": 8_658_000.0,
    "HBM_1X": 1_813_500.0,
    "CASH": 1_228_500.0,
}


@dataclass(frozen=True)
class ModelChoice:
    model: str
    us_lag: int
    fx_lag: int
    residual_drag: float
    correlation: float
    rmse: float
    terminal_ratio: float
    score: float


@dataclass
class Simulation:
    metrics: dict[str, Any]
    daily: pd.DataFrame
    trades: pd.DataFrame
    contributions: pd.DataFrame


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
        return {str(key): _json_ready(item) for key, item in value.items()}
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


def _clean_price(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy().sort_index()
    output.index = pd.DatetimeIndex(output.index).tz_localize(None)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in output:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    if "Close" not in output:
        raise ValueError("price frame has no Close column")
    return output.dropna(subset=["Close"])


def _clean_series(series: pd.Series) -> pd.Series:
    output = pd.to_numeric(series, errors="coerce").dropna().sort_index()
    output.index = pd.DatetimeIndex(output.index).tz_localize(None)
    return output[~output.index.duplicated(keep="last")]


def _align(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    return _clean_series(series).reindex(index).ffill().bfill()


def _first_trading_days(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    series = pd.Series(index=index, data=index)
    return {
        pd.Timestamp(group.index.min())
        for _, group in series.groupby(index.to_period("M"))
    }


def _weekly_decision_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    series = pd.Series(index=index, data=index)
    values = [
        pd.Timestamp(group.index.max())
        for _, group in series.groupby(index.to_period("W-FRI"))
    ]
    return pd.DatetimeIndex(values)


def xirr(cashflows: Iterable[tuple[pd.Timestamp, float]]) -> float:
    flows = [(pd.Timestamp(date), float(value)) for date, value in cashflows]
    flows = [(date, value) for date, value in flows if abs(value) > 1e-12]
    if not flows or not any(value < 0 for _, value in flows) or not any(
        value > 0 for _, value in flows
    ):
        return float("nan")
    base = min(date for date, _ in flows)

    def npv(rate: float) -> float:
        if rate <= -0.999999999:
            return float("inf")
        return sum(
            value / ((1.0 + rate) ** ((date - base).days / 365.25))
            for date, value in flows
        )

    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    while _finite(f_high) and f_low * f_high > 0 and high < 1_000_000:
        high *= 2.0
        f_high = npv(high)
    if not _finite(f_low) or not _finite(f_high) or f_low * f_high > 0:
        return float("nan")
    for _ in range(240):
        middle = (low + high) / 2.0
        f_middle = npv(middle)
        if abs(f_middle) < 1e-7:
            return middle
        if f_low * f_middle <= 0:
            high, f_high = middle, f_middle
        else:
            low, f_low = middle, f_middle
    return (low + high) / 2.0


def fetch_fred_series(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = fetch_text(url, retries=3, pause=1.0)
    frame = pd.read_csv(io.StringIO(text))
    date_column = "DATE" if "DATE" in frame.columns else frame.columns[0]
    value_column = series_id if series_id in frame.columns else frame.columns[-1]
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    frame = frame.dropna(subset=[date_column, value_column])
    return _clean_series(frame.set_index(date_column)[value_column])


def _save_series(series: pd.Series, path: Path, name: str) -> None:
    pd.DataFrame({"Date": series.index, name: series.to_numpy()}).to_csv(
        path, index=False, encoding="utf-8"
    )


def snapshot_file(
    *,
    ticker: str,
    frame: pd.DataFrame | pd.Series,
    output_path: Path,
) -> dict[str, Any]:
    if isinstance(frame, pd.Series):
        values = _clean_series(frame)
        _save_series(values, output_path, ticker)
        rows = len(values)
        start = values.index.min()
        end = values.index.max()
    else:
        values = _clean_price(frame)
        values.reset_index(names="Date").to_csv(
            output_path, index=False, encoding="utf-8"
        )
        rows = len(values)
        start = values.index.min()
        end = values.index.max()
    return {
        "ticker": ticker,
        "path": output_path.name,
        "rows": int(rows),
        "start": pd.Timestamp(start).date().isoformat(),
        "end": pd.Timestamp(end).date().isoformat(),
        "sha256": _sha256(output_path),
    }


def _leveraged_returns(
    qqq_return: pd.Series,
    fx_return: pd.Series,
    short_rate_pct: pd.Series,
    *,
    model: str,
    residual_drag: float,
    us_lag: int,
    fx_lag: int,
) -> pd.Series:
    q = qqq_return.shift(int(us_lag)).fillna(0.0)
    f = fx_return.shift(int(fx_lag)).fillna(0.0)
    financing = short_rate_pct.shift(1).ffill().fillna(0.0) / 100.0 / TRADING_DAYS
    if model == "double_krw":
        raw = 2.0 * ((1.0 + q) * (1.0 + f) - 1.0)
    elif model == "double_additive":
        raw = 2.0 * q + 2.0 * f
    elif model == "qld_fx1":
        raw = (1.0 + 2.0 * q - financing) * (1.0 + f) - 1.0
        financing = pd.Series(0.0, index=raw.index)
    else:
        raise ValueError(f"unsupported model: {model}")
    result = raw - financing - float(residual_drag) / TRADING_DAYS
    return result.clip(lower=-0.995)


def calibrate_qld_drag(
    qqq_close: pd.Series,
    qld_close: pd.Series,
    short_rate_pct: pd.Series,
) -> tuple[pd.DataFrame, float]:
    index = qqq_close.index.intersection(qld_close.index)
    qqq = _align(qqq_close, index)
    qld = _align(qld_close, index)
    rate = _align(short_rate_pct, index)
    q_return = qqq.pct_change().fillna(0.0)
    actual_return = qld.pct_change().fillna(0.0)
    actual_norm = (1.0 + actual_return).cumprod()
    rows: list[dict[str, Any]] = []
    for residual in np.arange(-0.04, 0.1201, 0.0005):
        model_return = (
            2.0 * q_return - rate.shift(1).ffill().fillna(0.0) / 100.0 / TRADING_DAYS
            - residual / TRADING_DAYS
        ).clip(lower=-0.995)
        model_norm = (1.0 + model_return).cumprod()
        terminal_ratio = float(model_norm.iloc[-1] / actual_norm.iloc[-1])
        rmse = float(np.sqrt(np.mean((model_return - actual_return) ** 2)))
        correlation = float(model_return.corr(actual_return))
        log_error = abs(math.log(max(1e-12, terminal_ratio)))
        score = log_error + 2.0 * rmse - 0.02 * correlation
        rows.append(
            {
                "residual_drag": float(residual),
                "terminal_ratio": terminal_ratio,
                "daily_rmse": rmse,
                "daily_correlation": correlation,
                "score": score,
            }
        )
    table = pd.DataFrame(rows).sort_values("score").reset_index(drop=True)
    return table, float(table.iloc[0]["residual_drag"])


def calibrate_tiger_model(
    qqq_close: pd.Series,
    fx_close: pd.Series,
    short_rate_pct: pd.Series,
    actual_tiger_close: pd.Series,
) -> pd.DataFrame:
    actual = _clean_series(actual_tiger_close)
    index = actual.index
    qqq = _align(qqq_close, index)
    fx = _align(fx_close, index)
    rate = _align(short_rate_pct, index)
    q_return = qqq.pct_change().fillna(0.0)
    fx_return = fx.pct_change().fillna(0.0)
    actual_return = actual.pct_change().fillna(0.0)
    actual_norm = (1.0 + actual_return).cumprod()
    rows: list[dict[str, Any]] = []
    for model in ("double_krw", "double_additive", "qld_fx1"):
        for us_lag in (0, 1, 2):
            for fx_lag in (0, 1):
                for residual in np.arange(-0.05, 0.1501, 0.001):
                    model_return = _leveraged_returns(
                        q_return,
                        fx_return,
                        rate,
                        model=model,
                        residual_drag=float(residual),
                        us_lag=us_lag,
                        fx_lag=fx_lag,
                    )
                    model_norm = (1.0 + model_return).cumprod()
                    terminal_ratio = float(model_norm.iloc[-1] / actual_norm.iloc[-1])
                    rmse = float(np.sqrt(np.mean((model_return - actual_return) ** 2)))
                    correlation = float(model_return.corr(actual_return))
                    log_error = abs(math.log(max(1e-12, terminal_ratio)))
                    score = 12.0 * rmse + 0.10 * log_error - 0.10 * correlation
                    rows.append(
                        {
                            "model": model,
                            "us_lag": us_lag,
                            "fx_lag": fx_lag,
                            "residual_drag": float(residual),
                            "terminal_ratio": terminal_ratio,
                            "daily_rmse": rmse,
                            "daily_correlation": correlation,
                            "score": score,
                        }
                    )
    return pd.DataFrame(rows).sort_values("score").reset_index(drop=True)


def build_price_from_returns(returns: pd.Series, start_value: float = 100.0) -> pd.Series:
    result = (1.0 + returns.fillna(0.0)).cumprod() * float(start_value)
    result.name = "Close"
    return result


def build_tiger_synthetic(
    *,
    qqq_close: pd.Series,
    fx_close: pd.Series,
    short_rate_pct: pd.Series,
    choice: ModelChoice,
) -> pd.Series:
    index = qqq_close.index
    qqq = _align(qqq_close, index)
    fx = _align(fx_close, index)
    rate = _align(short_rate_pct, index)
    returns = _leveraged_returns(
        qqq.pct_change().fillna(0.0),
        fx.pct_change().fillna(0.0),
        rate,
        model=choice.model,
        residual_drag=choice.residual_drag,
        us_lag=choice.us_lag,
        fx_lag=choice.fx_lag,
    )
    return build_price_from_returns(returns)


def splice_actual(
    synthetic: pd.Series,
    actual: pd.Series,
    index: pd.DatetimeIndex,
) -> pd.Series:
    synthetic = _align(synthetic, index)
    actual_aligned = _clean_series(actual).reindex(index).ffill()
    available = actual_aligned.dropna()
    if available.empty:
        return synthetic
    first = pd.Timestamp(available.index.min())
    scale = float(available.loc[first] / synthetic.loc[first])
    combined = synthetic * scale
    combined.loc[first:] = actual_aligned.loc[first:]
    return combined.ffill().bfill()


def build_cash_price(
    annual_rate_pct: pd.Series,
    index: pd.DatetimeIndex,
    start_value: float = 100.0,
) -> pd.Series:
    rate = _align(annual_rate_pct, index).clip(lower=-1.0, upper=30.0) / 100.0
    daily = rate.shift(1).fillna(0.0) / TRADING_DAYS
    return (1.0 + daily).cumprod() * float(start_value)


def fx_zscore(fx_close: pd.Series, window: int = 252) -> pd.Series:
    values = np.log(_clean_series(fx_close))
    mean = values.rolling(window, min_periods=window).mean()
    std = values.rolling(window, min_periods=window).std(ddof=0)
    return (values - mean) / std.replace(0.0, np.nan)


def build_fx_target_schedule(
    fx_close: pd.Series,
    index: pd.DatetimeIndex,
    *,
    sell_z: float = SELL_Z,
    buy_z: float = BUY_Z,
    reduced_weight: float = REDUCED_WEIGHT,
    execution_delay_days: int = 1,
) -> tuple[dict[pd.Timestamp, float], pd.DataFrame]:
    fx = _align(fx_close, index)
    z = fx_zscore(fx).reindex(index)
    decisions = _weekly_decision_dates(index)
    target = 1.0
    schedule: dict[pd.Timestamp, float] = {pd.Timestamp(index[0]): 1.0}
    rows: list[dict[str, Any]] = []
    for decision_date in decisions:
        value = z.get(decision_date, np.nan)
        if pd.isna(value):
            continue
        new_target = target
        action = "HOLD"
        if target > reduced_weight + 1e-12 and float(value) >= float(sell_z):
            new_target = float(reduced_weight)
            action = "REDUCE"
        elif target < 1.0 - 1e-12 and float(value) <= float(buy_z):
            new_target = 1.0
            action = "RESTORE"
        if new_target != target:
            position = int(index.searchsorted(decision_date, side="right"))
            position += max(0, int(execution_delay_days) - 1)
            if position < len(index):
                execution_date = pd.Timestamp(index[position])
                schedule[execution_date] = new_target
                rows.append(
                    {
                        "decision_date": pd.Timestamp(decision_date),
                        "execution_date": execution_date,
                        "fx_z": float(value),
                        "action": action,
                        "target_before": target,
                        "target_after": new_target,
                    }
                )
                target = new_target
    return schedule, pd.DataFrame(rows)


def simulate_dca(
    *,
    risk_price: pd.Series,
    cash_price: pd.Series,
    target_schedule: Mapping[pd.Timestamp, float] | None,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital_krw: float = INITIAL_CAPITAL_KRW,
    monthly_contribution_krw: float = MONTHLY_CONTRIBUTION_KRW,
    total_contribution_cap_krw: float | None = None,
    one_way_cost: float,
    tax_mode: str,
    isa_exemption_krw: float = ISA_GENERAL_EXEMPTION_KRW,
) -> Simulation:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    index = risk_price.loc[start:end].dropna().index.intersection(
        cash_price.loc[start:end].dropna().index
    )
    if len(index) < 20:
        raise ValueError("simulation period is too short")
    risk = _align(risk_price, index)
    cash = _align(cash_price, index)
    target_schedule = {
        pd.Timestamp(date): float(weight)
        for date, weight in (target_schedule or {index[0]: 1.0}).items()
        if pd.Timestamp(date) in index
    }
    if pd.Timestamp(index[0]) not in target_schedule:
        target_schedule[pd.Timestamp(index[0])] = 1.0
    monthly_dates = _first_trading_days(index)
    first_date = pd.Timestamp(index[0])
    first_month = first_date.to_period("M")

    risk_units = 0.0
    cash_units = 0.0
    target = float(target_schedule[first_date])
    nav_units = 0.0
    total_contributions = 0.0
    cashflows: list[tuple[pd.Timestamp, float]] = []
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    contribution_rows: list[dict[str, Any]] = []

    def values(date: pd.Timestamp) -> tuple[float, float, float]:
        risk_value = risk_units * float(risk.loc[date])
        cash_value = cash_units * float(cash.loc[date])
        return risk_value, cash_value, risk_value + cash_value

    def buy_risk(date: pd.Timestamp, amount: float, reason: str) -> None:
        nonlocal risk_units, cash_units
        if amount <= 1e-8:
            return
        available_cash = cash_units * float(cash.loc[date])
        amount = min(float(amount), available_cash)
        if amount <= 1e-8:
            return
        cash_units -= amount / float(cash.loc[date])
        units = amount / (1.0 + one_way_cost) / float(risk.loc[date])
        risk_units += units
        trade_rows.append(
            {
                "date": date,
                "side": "BUY",
                "amount_krw": amount,
                "target_weight": target,
                "reason": reason,
            }
        )

    def sell_risk(date: pd.Timestamp, amount: float, reason: str) -> None:
        nonlocal risk_units, cash_units
        risk_value = risk_units * float(risk.loc[date])
        amount = min(float(amount), risk_value)
        if amount <= 1e-8:
            return
        units = amount / float(risk.loc[date])
        risk_units -= units
        proceeds = amount * (1.0 - one_way_cost)
        cash_units += proceeds / float(cash.loc[date])
        trade_rows.append(
            {
                "date": date,
                "side": "SELL",
                "amount_krw": amount,
                "target_weight": target,
                "reason": reason,
            }
        )

    def rebalance(date: pd.Timestamp, new_target: float, reason: str) -> None:
        nonlocal target
        risk_value, _, total = values(date)
        if total <= 0:
            target = float(new_target)
            return
        desired = total * float(new_target)
        if risk_value > desired + 1e-8:
            target = float(new_target)
            sell_risk(date, risk_value - desired, reason)
        elif risk_value < desired - 1e-8:
            target = float(new_target)
            buy_risk(date, desired - risk_value, reason)
        target = float(new_target)

    def contribute(date: pd.Timestamp, amount: float, reason: str) -> None:
        nonlocal risk_units, cash_units, nav_units, total_contributions
        if amount <= 0:
            return
        if total_contribution_cap_krw is not None:
            remaining = float(total_contribution_cap_krw) - total_contributions
            amount = min(float(amount), max(0.0, remaining))
        if amount <= 1e-8:
            return
        _, _, before = values(date)
        if nav_units <= 0:
            nav_units = amount
        else:
            unit_price = before / nav_units if before > 0 else 1.0
            nav_units += amount / unit_price
        risk_amount = amount * target
        cash_amount = amount - risk_amount
        if cash_amount > 0:
            cash_units += cash_amount / float(cash.loc[date])
        if risk_amount > 0:
            units = risk_amount / (1.0 + one_way_cost) / float(risk.loc[date])
            risk_units += units
            trade_rows.append(
                {
                    "date": date,
                    "side": "BUY",
                    "amount_krw": risk_amount,
                    "target_weight": target,
                    "reason": reason,
                }
            )
        total_contributions += amount
        cashflows.append((date, -amount))
        contribution_rows.append(
            {
                "date": date,
                "amount_krw": amount,
                "target_weight": target,
                "total_contributions_krw": total_contributions,
            }
        )

    contribute(first_date, float(initial_capital_krw), "initial")

    for date in index:
        date = pd.Timestamp(date)
        if date != first_date and date in target_schedule:
            rebalance(date, float(target_schedule[date]), "signal_rebalance")
        if date in monthly_dates and date.to_period("M") != first_month:
            contribute(date, float(monthly_contribution_krw), "monthly")
        risk_value, cash_value, total = values(date)
        nav = total / nav_units if nav_units > 0 else 0.0
        daily_rows.append(
            {
                "Date": date,
                "risk_value_krw": risk_value,
                "cash_value_krw": cash_value,
                "value_krw": total,
                "target_weight": target,
                "actual_risk_weight": risk_value / total if total > 0 else 0.0,
                "nav": nav,
                "contributions_krw": total_contributions,
                "profit_vs_contributions": total - total_contributions,
            }
        )

    daily = pd.DataFrame(daily_rows).set_index("Date")
    final_date = pd.Timestamp(index[-1])
    risk_value, cash_value, _ = values(final_date)
    pre_tax_liquidation = risk_value * (1.0 - one_way_cost) + cash_value
    profit = pre_tax_liquidation - total_contributions
    if tax_mode == "direct":
        tax = max(0.0, profit - DIRECT_ANNUAL_DEDUCTION_KRW) * DIRECT_TAX_RATE
    elif tax_mode == "isa":
        tax = max(0.0, profit - float(isa_exemption_krw)) * ISA_SEPARATE_TAX_RATE
    elif tax_mode == "none":
        tax = 0.0
    else:
        raise ValueError(f"unsupported tax mode: {tax_mode}")
    after_tax = pre_tax_liquidation - tax
    cashflows.append((final_date, after_tax))
    nav = daily["nav"]
    elapsed_years = max(1e-9, (final_date - first_date).days / 365.25)
    trade_frame = pd.DataFrame(trade_rows)
    metrics = {
        "start": first_date.date().isoformat(),
        "end": final_date.date().isoformat(),
        "total_contributions_krw": total_contributions,
        "contribution_count": len(contribution_rows),
        "pre_tax_liquidation_value_krw": pre_tax_liquidation,
        "after_tax_liquidation_value_krw": after_tax,
        "tax_krw": tax,
        "after_tax_xirr": xirr(cashflows),
        "twr_cagr": float((nav.iloc[-1] / nav.iloc[0]) ** (1.0 / elapsed_years) - 1.0),
        "mdd": float((nav / nav.cummax() - 1.0).min()),
        "minimum_vs_contributions": float(
            (daily["value_krw"] / daily["contributions_krw"] - 1.0).min()
        ),
        "trade_count": int(len(trade_frame)),
        "signal_sell_count": int(
            ((trade_frame.get("side") == "SELL") & (trade_frame.get("reason") == "signal_rebalance")).sum()
        )
        if not trade_frame.empty
        else 0,
        "final_risk_weight": float(daily["actual_risk_weight"].iloc[-1]),
    }
    return Simulation(
        metrics=metrics,
        daily=daily,
        trades=trade_frame,
        contributions=pd.DataFrame(contribution_rows),
    )


def evaluate_three_strategies(
    *,
    qld_krw_price: pd.Series,
    tiger_price: pd.Series,
    kr_cash_price: pd.Series,
    fx_close: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    legal_cap: bool,
    execution_delay_days: int = 1,
    sell_z: float = SELL_Z,
    buy_z: float = BUY_Z,
    isa_exemption_krw: float = ISA_GENERAL_EXEMPTION_KRW,
) -> dict[str, Any]:
    index = tiger_price.loc[start:end].dropna().index
    schedule, signal_log = build_fx_target_schedule(
        fx_close,
        index,
        sell_z=sell_z,
        buy_z=buy_z,
        execution_delay_days=execution_delay_days,
    )
    cap = ISA_TOTAL_CONTRIBUTION_LIMIT_KRW if legal_cap else None
    qld = simulate_dca(
        risk_price=qld_krw_price,
        cash_price=kr_cash_price,
        target_schedule={index[0]: 1.0},
        start=start,
        end=end,
        total_contribution_cap_krw=cap,
        one_way_cost=DIRECT_ONE_WAY_COST,
        tax_mode="direct",
    )
    tiger_hold = simulate_dca(
        risk_price=tiger_price,
        cash_price=kr_cash_price,
        target_schedule={index[0]: 1.0},
        start=start,
        end=end,
        total_contribution_cap_krw=cap,
        one_way_cost=TIGER_ONE_WAY_COST,
        tax_mode="isa",
        isa_exemption_krw=isa_exemption_krw,
    )
    tiger_timing = simulate_dca(
        risk_price=tiger_price,
        cash_price=kr_cash_price,
        target_schedule=schedule,
        start=start,
        end=end,
        total_contribution_cap_krw=cap,
        one_way_cost=TIGER_ONE_WAY_COST,
        tax_mode="isa",
        isa_exemption_krw=isa_exemption_krw,
    )
    return {
        "qld": qld,
        "tiger_hold": tiger_hold,
        "tiger_timing": tiger_timing,
        "signal_log": signal_log,
    }


def rolling_window_rows(
    *,
    qld_krw_price: pd.Series,
    tiger_price: pd.Series,
    cash_price: pd.Series,
    fx_close: pd.Series,
    years: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for year in range(FIXED_START.year, FIXED_END.year - years + 1):
        requested_start = pd.Timestamp(year=year, month=8, day=1)
        requested_end = requested_start + pd.DateOffset(years=years)
        if requested_end > FIXED_END:
            continue
        actual_start = tiger_price.loc[requested_start:].dropna().index.min()
        actual_end = tiger_price.loc[:requested_end].dropna().index.max()
        if pd.isna(actual_start) or pd.isna(actual_end) or actual_end <= actual_start:
            continue
        result = evaluate_three_strategies(
            qld_krw_price=qld_krw_price,
            tiger_price=tiger_price,
            kr_cash_price=cash_price,
            fx_close=fx_close,
            start=pd.Timestamp(actual_start),
            end=pd.Timestamp(actual_end),
            legal_cap=False,
        )
        rows.append(
            {
                "window_years": years,
                "start": pd.Timestamp(actual_start).date().isoformat(),
                "end": pd.Timestamp(actual_end).date().isoformat(),
                "qld_after_tax_xirr": result["qld"].metrics["after_tax_xirr"],
                "qld_mdd": result["qld"].metrics["mdd"],
                "tiger_hold_after_tax_xirr": result["tiger_hold"].metrics["after_tax_xirr"],
                "tiger_hold_mdd": result["tiger_hold"].metrics["mdd"],
                "tiger_timing_after_tax_xirr": result["tiger_timing"].metrics["after_tax_xirr"],
                "tiger_timing_mdd": result["tiger_timing"].metrics["mdd"],
                "timing_minus_hold_xirr": result["tiger_timing"].metrics["after_tax_xirr"]
                - result["tiger_hold"].metrics["after_tax_xirr"],
                "timing_minus_hold_mdd": result["tiger_timing"].metrics["mdd"]
                - result["tiger_hold"].metrics["mdd"],
            }
        )
    return rows


def scenario_fx(
    actual_fx: pd.Series,
    index: pd.DatetimeIndex,
    scenario: str,
) -> pd.Series:
    actual = _align(actual_fx, index)
    start = float(actual.iloc[0])
    years = pd.Series(
        [(pd.Timestamp(date) - pd.Timestamp(index[0])).days / 365.25 for date in index],
        index=index,
        dtype=float,
    )
    if scenario == "actual":
        return actual
    if scenario == "flat":
        return pd.Series(start, index=index, dtype=float)
    if scenario == "krw_appreciates_2pct_pa":
        return start * np.exp(np.log(0.98) * years)
    if scenario == "krw_appreciates_5pct_first5y":
        return start * np.exp(np.log(0.95) * years.clip(upper=5.0))
    if scenario == "krw_strengthens_20pct_year5":
        return pd.Series(
            np.where(years < 5.0, start, start * 0.80), index=index, dtype=float
        )
    raise ValueError(f"unsupported FX scenario: {scenario}")


def leverage_row(
    name: str,
    direct: Mapping[str, float],
    isa: Mapping[str, float],
) -> dict[str, Any]:
    leverage = {
        "TQQQ": 3.0,
        "QLD": 2.0,
        "SOXL": 3.0,
        "CASH": 0.0,
        "NASDAQ_1X": 1.0,
        "HBM_1X": 1.0,
        "TIGER_2X": 2.0,
    }
    fx_beta = {
        "TQQQ": 1.0,
        "QLD": 1.0,
        "SOXL": 1.0,
        "CASH": 0.0,
        "NASDAQ_1X": 1.0,
        "HBM_1X": 1.0,
        "TIGER_2X": 2.0,
    }
    nasdaq_beta = {
        "TQQQ": 3.0,
        "QLD": 2.0,
        "SOXL": 0.0,
        "CASH": 0.0,
        "NASDAQ_1X": 1.0,
        "HBM_1X": 0.0,
        "TIGER_2X": 2.0,
    }
    semiconductor_beta = {
        "TQQQ": 0.0,
        "QLD": 0.0,
        "SOXL": 3.0,
        "CASH": 0.0,
        "NASDAQ_1X": 0.0,
        "HBM_1X": 1.0,
        "TIGER_2X": 0.0,
    }
    holdings = {**direct}
    for key, value in isa.items():
        holdings[key] = holdings.get(key, 0.0) + float(value)
    total = float(sum(holdings.values()))
    return {
        "scenario": name,
        "total_assets_krw": total,
        "nominal_gross_exposure_krw": sum(
            float(value) * leverage.get(key, 0.0) for key, value in holdings.items()
        ),
        "nominal_gross_leverage": sum(
            float(value) * leverage.get(key, 0.0) for key, value in holdings.items()
        )
        / total,
        "nasdaq_equivalent_leverage": sum(
            float(value) * nasdaq_beta.get(key, 0.0) for key, value in holdings.items()
        )
        / total,
        "semiconductor_equivalent_leverage": sum(
            float(value) * semiconductor_beta.get(key, 0.0)
            for key, value in holdings.items()
        )
        / total,
        "usdkrw_beta": sum(
            float(value) * fx_beta.get(key, 0.0) for key, value in holdings.items()
        )
        / total,
    }


def build_leverage_scenarios() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    rows.append(leverage_row("current", CURRENT_DIRECT_KRW, CURRENT_ISA_KRW))

    direct = dict(CURRENT_DIRECT_KRW)
    direct["QLD"] += 1_500_000.0
    rows.append(leverage_row("add_1_5m_new_money_to_qld", direct, CURRENT_ISA_KRW))

    isa = dict(CURRENT_ISA_KRW)
    isa["TIGER_2X"] = 1_500_000.0
    rows.append(leverage_row("add_1_5m_new_money_to_tiger", CURRENT_DIRECT_KRW, isa))

    isa_replace = dict(CURRENT_ISA_KRW)
    isa_replace["NASDAQ_1X"] -= 1_500_000.0
    isa_replace["TIGER_2X"] = 1_500_000.0
    rows.append(
        leverage_row(
            "replace_1_5m_isa_nasdaq1x_with_tiger",
            CURRENT_DIRECT_KRW,
            isa_replace,
        )
    )

    direct_12m = dict(CURRENT_DIRECT_KRW)
    direct_12m["QLD"] += 6_000_000.0
    rows.append(leverage_row("12m_new_money_to_qld", direct_12m, CURRENT_ISA_KRW))

    isa_12m = dict(CURRENT_ISA_KRW)
    isa_12m["TIGER_2X"] = 6_000_000.0
    rows.append(leverage_row("12m_new_money_to_tiger", CURRENT_DIRECT_KRW, isa_12m))
    return pd.DataFrame(rows)


def current_fx_signal() -> dict[str, Any]:
    live = _clean_price(fetch_yahoo_price("KRW=X"))["Close"]
    today = pd.Timestamp(datetime.now(UTC).date())
    live = live.loc[live.index < today]
    z = fx_zscore(live)
    latest_date = pd.Timestamp(z.dropna().index.max())
    value = float(z.loc[latest_date])
    if value >= SELL_Z:
        state = "REDUCE_TO_25_PERCENT"
    elif value <= BUY_Z:
        state = "FULL_100_PERCENT"
    else:
        state = "HYSTERESIS_HOLD_PREVIOUS_STATE"
    return {
        "date": latest_date.date().isoformat(),
        "usdkrw": float(live.loc[latest_date]),
        "z_52w": value,
        "signal": state,
        "sell_threshold": SELL_Z,
        "restore_threshold": BUY_Z,
    }


def build_report(
    *,
    manifest: Mapping[str, Any],
    summary: pd.DataFrame,
    regime: pd.DataFrame,
    rolling_summary: pd.DataFrame,
    delay: pd.DataFrame,
    threshold: pd.DataFrame,
    stress: pd.DataFrame,
    leverage: pd.DataFrame,
    calibration_top: pd.DataFrame,
) -> str:
    lines = [
        "# QLD 직투 vs TIGER 미국나스닥100레버리지 ISA 통합 연구",
        "",
        f"- 고정기간: {FIXED_START.date().isoformat()}~{FIXED_END.date().isoformat()}",
        "- 초기 1,000만원 + 월 50만원",
        "- 직투 QLD와 ISA TIGER의 비용·세금·납입한도를 구분",
        "- TIGER 상장 이전은 실제 TIGER 구간으로 보정한 합성 시계열",
        "- 연구 전용: BTC·기존 Equity v1·Telegram·자동주문 변경 없음",
        "",
        "## 1. 핵심 비교",
        "",
        "| 시나리오 | 전략 | 총 납입 | 세후 XIRR | TWR CAGR | MDD | 납입원금 대비 최저 | 세금 | 세후 최종액 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"| {row['scenario']} | {row['strategy']} | {_fmt_krw(row['total_contributions_krw'])} | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['twr_cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['tax_krw'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 2. TIGER 합성모형 보정",
            "",
            "| 모형 | US lag | FX lag | 잔여 드래그 | 일간 상관 | RMSE | 종단비율 | 점수 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in calibration_top.head(8).iterrows():
        lines.append(
            f"| {row['model']} | {int(row['us_lag'])} | {int(row['fx_lag'])} | "
            f"{row['residual_drag']:.3%} | {row['daily_correlation']:.4f} | "
            f"{row['daily_rmse']:.6f} | {row['terminal_ratio']:.4f} | {row['score']:.6f} |"
        )

    lines.extend(
        [
            "",
            "## 3. 개발구간·홀드아웃",
            "",
            "| 구간 | 전략 | 세후 XIRR | MDD | 세후 최종액 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for _, row in regime.iterrows():
        lines.append(
            f"| {row['period']} | {row['strategy']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 4. 고정 환율규칙의 롤링 검증",
            "",
            "| 기간 | 창 개수 | 타이밍 XIRR 우위 비율 | XIRR 차이 중앙값 | 최악 XIRR 차이 | MDD 개선 비율 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in rolling_summary.iterrows():
        lines.append(
            f"| {int(row['window_years'])}년 | {int(row['window_count'])} | "
            f"{_fmt_pct(row['timing_xirr_win_rate'])} | {_fmt_pct(row['median_xirr_delta'])} | "
            f"{_fmt_pct(row['worst_xirr_delta'])} | {_fmt_pct(row['mdd_improvement_rate'])} |"
        )

    lines.extend(
        [
            "",
            "## 5. 체결지연 민감도",
            "",
            "| 지연 거래일 | 세후 XIRR | MDD | 세후 최종액 | 비중변경 횟수 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in delay.iterrows():
        lines.append(
            f"| {int(row['execution_delay_days'])} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} | "
            f"{int(row['signal_change_count'])} |"
        )

    lines.extend(
        [
            "",
            "## 6. 임계값 민감도",
            "",
            "| 축소 z | 복원 z | 세후 XIRR | MDD | 세후 최종액 | 비중변경 횟수 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in threshold.iterrows():
        lines.append(
            f"| {row['sell_z']:.2f} | {row['buy_z']:.2f} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} | "
            f"{int(row['signal_change_count'])} |"
        )

    lines.extend(
        [
            "",
            "## 7. 스트레스",
            "",
            "| 시나리오 | 전략 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in stress.iterrows():
        lines.append(
            f"| {row['scenario']} | {row['strategy']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 8. 현재 계좌 유효 레버리지",
            "",
            "| 시나리오 | 총자산 | 명목 총레버리지 | 나스닥 환산 | 반도체 환산 | USDKRW 베타 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in leverage.iterrows():
        lines.append(
            f"| {row['scenario']} | {_fmt_krw(row['total_assets_krw'])} | "
            f"{row['nominal_gross_leverage']:.3f}x | {row['nasdaq_equivalent_leverage']:.3f}x | "
            f"{row['semiconductor_equivalent_leverage']:.3f}x | {row['usdkrw_beta']:.3f}x |"
        )

    signal = manifest["current_fx_signal"]
    lines.extend(
        [
            "",
            "## 9. 최신 환율 신호",
            "",
            f"- 기준일: {signal['date']}",
            f"- USD/KRW: {signal['usdkrw']:.2f}",
            f"- 52주 로그환율 z점수: {signal['z_52w']:.3f}",
            f"- 신호: `{signal['signal']}`",
            "",
            "## 10. 주의",
            "",
            "- ISA 세금은 이 TIGER·현금 슬리브만 존재한다고 가정한 근사다. 실제 ISA의 다른 ETF 손익과 통산하면 달라진다.",
            "- QLD 조정주가는 배당 재투자 효과를 포함하지만 미국 원천징수세를 별도로 차감하지 않아 직투 성과가 소폭 높게 보일 수 있다.",
            "- TIGER 상장 이전과 원화강세 스트레스는 합성값이다.",
            "- 환율 타이밍 규칙은 미래 성과를 보장하지 않으며, 특히 지연 민감도가 크면 실전 채택을 보수적으로 해석해야 한다.",
            "- 현재 보유액은 최근 사용자 제공값의 근사이며 실제 수량·취득원가로 다시 계산해야 한다.",
            "- 연구 전용이며 실전 주문·Telegram·사이트에는 반영하지 않았다.",
            "",
            "## 11. 재현정보",
            "",
            f"- 전략 버전: `{manifest['strategy_version']}`",
            f"- 입력 파일 수: {len(manifest['input_snapshot']['files'])}",
            f"- 생성시각 UTC: `{manifest['generated_at_utc']}`",
        ]
    )
    return "\n".join(lines)


def run(*, refresh_external: bool, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    output = paths.output
    output.mkdir(parents=True, exist_ok=True)
    snapshot_dir = output / "equity_v2_isa_tiger_qld_input_snapshot"
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    qqq_frame = _clean_price(read_price("QQQ", paths, refresh=False))
    qld_frame = _clean_price(read_price("QLD", paths, refresh=False))
    irx_frame = _clean_price(read_price("^IRX", paths, refresh=False))
    fx_frame = _clean_price(read_price("KRW=X", paths, refresh=False))

    tiger_cache = paths.cache / "yahoo_418660_KS.csv"
    if refresh_external or not tiger_cache.exists():
        tiger_frame = _clean_price(fetch_yahoo_price(ACTUAL_TIGER_TICKER))
        tiger_frame.reset_index(names="Date").to_csv(
            tiger_cache, index=False, encoding="utf-8"
        )
    else:
        tiger_frame = _clean_price(pd.read_csv(tiger_cache, parse_dates=["Date"]).set_index("Date"))

    fred_fx_cache = paths.cache / f"fred_{FRED_KRW_USD}.csv"
    if refresh_external or not fred_fx_cache.exists():
        fred_fx = fetch_fred_series(FRED_KRW_USD)
        _save_series(fred_fx, fred_fx_cache, FRED_KRW_USD)
    else:
        fred_fx = _clean_series(
            pd.read_csv(fred_fx_cache, parse_dates=["Date"]).set_index("Date").iloc[:, 0]
        )

    kr_rate_cache = paths.cache / f"fred_{FRED_KR_3M}.csv"
    if refresh_external or not kr_rate_cache.exists():
        kr_rate = fetch_fred_series(FRED_KR_3M)
        _save_series(kr_rate, kr_rate_cache, FRED_KR_3M)
    else:
        kr_rate = _clean_series(
            pd.read_csv(kr_rate_cache, parse_dates=["Date"]).set_index("Date").iloc[:, 0]
        )

    snapshot_files = [
        snapshot_file(ticker="QQQ", frame=qqq_frame.loc[:FIXED_END], output_path=snapshot_dir / "QQQ.csv"),
        snapshot_file(ticker="QLD", frame=qld_frame.loc[:FIXED_END], output_path=snapshot_dir / "QLD.csv"),
        snapshot_file(ticker="^IRX", frame=irx_frame.loc[:FIXED_END], output_path=snapshot_dir / "_IRX.csv"),
        snapshot_file(ticker="KRW=X", frame=fx_frame.loc[:FIXED_END], output_path=snapshot_dir / "KRW=X.csv"),
        snapshot_file(ticker=ACTUAL_TIGER_TICKER, frame=tiger_frame.loc[:FIXED_END], output_path=snapshot_dir / "TIGER_418660.csv"),
        snapshot_file(ticker=FRED_KRW_USD, frame=fred_fx.loc[:FIXED_END], output_path=snapshot_dir / f"{FRED_KRW_USD}.csv"),
        snapshot_file(ticker=FRED_KR_3M, frame=kr_rate.loc[:FIXED_END], output_path=snapshot_dir / f"{FRED_KR_3M}.csv"),
    ]

    qqq_close_all = qqq_frame["Close"].loc[:FIXED_END]
    qld_close_all = qld_frame["Close"].loc[:FIXED_END]
    irx_all = irx_frame["Close"].loc[:FIXED_END]
    fx_yahoo_all = fx_frame["Close"].loc[:FIXED_END]
    tiger_actual_all = tiger_frame["Close"].loc[:FIXED_END]

    modern_index = qqq_close_all.loc[FIXED_START:FIXED_END].index
    qqq_modern = _align(qqq_close_all, modern_index)
    qld_modern = _align(qld_close_all, modern_index)
    irx_modern = _align(irx_all, modern_index)
    fx_modern = _align(fx_yahoo_all, modern_index)
    kr_rate_modern = _align(kr_rate, modern_index)

    qld_calibration, qld_drag = calibrate_qld_drag(
        qqq_modern, qld_modern, irx_modern
    )
    tiger_calibration = calibrate_tiger_model(
        qqq_close_all,
        fx_yahoo_all,
        irx_all,
        tiger_actual_all,
    )
    best = tiger_calibration.iloc[0]
    choice = ModelChoice(
        model=str(best["model"]),
        us_lag=int(best["us_lag"]),
        fx_lag=int(best["fx_lag"]),
        residual_drag=float(best["residual_drag"]),
        correlation=float(best["daily_correlation"]),
        rmse=float(best["daily_rmse"]),
        terminal_ratio=float(best["terminal_ratio"]),
        score=float(best["score"]),
    )

    tiger_synthetic = build_tiger_synthetic(
        qqq_close=qqq_modern,
        fx_close=fx_modern,
        short_rate_pct=irx_modern,
        choice=choice,
    )
    tiger_price = splice_actual(
        tiger_synthetic,
        tiger_actual_all,
        modern_index,
    )
    qld_krw_price = qld_modern * fx_modern
    kr_cash_price = build_cash_price(kr_rate_modern, modern_index)

    summary_rows: list[dict[str, Any]] = []
    full_runs: dict[str, Simulation] = {}
    full_signal_logs: dict[str, pd.DataFrame] = {}
    for scenario, legal_cap in (("20y_unlimited_theoretical", False), ("isa_100m_cap_15y_then_hold", True)):
        results = evaluate_three_strategies(
            qld_krw_price=qld_krw_price,
            tiger_price=tiger_price,
            kr_cash_price=kr_cash_price,
            fx_close=fx_modern,
            start=FIXED_START,
            end=FIXED_END,
            legal_cap=legal_cap,
        )
        for key, label in (
            ("qld", "QLD direct buy-and-hold"),
            ("tiger_hold", "TIGER ISA buy-and-hold"),
            ("tiger_timing", "TIGER ISA FX z timing"),
        ):
            simulation = results[key]
            summary_rows.append(
                {
                    "scenario": scenario,
                    "strategy": label,
                    **simulation.metrics,
                }
            )
            full_runs[f"{scenario}_{key}"] = simulation
        full_signal_logs[scenario] = results["signal_log"]

    low_income = evaluate_three_strategies(
        qld_krw_price=qld_krw_price,
        tiger_price=tiger_price,
        kr_cash_price=kr_cash_price,
        fx_close=fx_modern,
        start=FIXED_START,
        end=FIXED_END,
        legal_cap=True,
        isa_exemption_krw=ISA_LOW_INCOME_EXEMPTION_KRW,
    )
    for key, label in (
        ("tiger_hold", "TIGER ISA buy-and-hold (low-income exemption)"),
        ("tiger_timing", "TIGER ISA FX z timing (low-income exemption)"),
    ):
        summary_rows.append(
            {
                "scenario": "isa_100m_cap_low_income",
                "strategy": label,
                **low_income[key].metrics,
            }
        )

    summary = pd.DataFrame(summary_rows)

    regime_rows: list[dict[str, Any]] = []
    for period, (start, end) in {**DEV_FOLDS, "holdout_2023_2026": HOLDOUT}.items():
        results = evaluate_three_strategies(
            qld_krw_price=qld_krw_price,
            tiger_price=tiger_price,
            kr_cash_price=kr_cash_price,
            fx_close=fx_modern,
            start=start,
            end=end,
            legal_cap=False,
        )
        for key, label in (
            ("qld", "QLD direct"),
            ("tiger_hold", "TIGER ISA hold"),
            ("tiger_timing", "TIGER ISA timing"),
        ):
            regime_rows.append(
                {"period": period, "strategy": label, **results[key].metrics}
            )
    regime = pd.DataFrame(regime_rows)

    rolling_rows = rolling_window_rows(
        qld_krw_price=qld_krw_price,
        tiger_price=tiger_price,
        cash_price=kr_cash_price,
        fx_close=fx_modern,
        years=5,
    ) + rolling_window_rows(
        qld_krw_price=qld_krw_price,
        tiger_price=tiger_price,
        cash_price=kr_cash_price,
        fx_close=fx_modern,
        years=10,
    )
    rolling = pd.DataFrame(rolling_rows)
    rolling_summary_rows: list[dict[str, Any]] = []
    for years, group in rolling.groupby("window_years"):
        rolling_summary_rows.append(
            {
                "window_years": int(years),
                "window_count": len(group),
                "timing_xirr_win_rate": float((group["timing_minus_hold_xirr"] > 0).mean()),
                "median_xirr_delta": float(group["timing_minus_hold_xirr"].median()),
                "worst_xirr_delta": float(group["timing_minus_hold_xirr"].min()),
                "mdd_improvement_rate": float((group["timing_minus_hold_mdd"] > 0).mean()),
                "median_mdd_delta": float(group["timing_minus_hold_mdd"].median()),
            }
        )
    rolling_summary = pd.DataFrame(rolling_summary_rows)

    delay_rows: list[dict[str, Any]] = []
    for delay_days in (1, 5, 10, 20):
        result = evaluate_three_strategies(
            qld_krw_price=qld_krw_price,
            tiger_price=tiger_price,
            kr_cash_price=kr_cash_price,
            fx_close=fx_modern,
            start=FIXED_START,
            end=FIXED_END,
            legal_cap=True,
            execution_delay_days=delay_days,
        )
        delay_rows.append(
            {
                "execution_delay_days": delay_days,
                "signal_change_count": len(result["signal_log"]),
                **result["tiger_timing"].metrics,
            }
        )
    delay = pd.DataFrame(delay_rows)

    threshold_rows: list[dict[str, Any]] = []
    for sell_z, buy_z in ((1.00, 0.50), (1.25, 0.75), (1.50, 1.00), (1.75, 1.25)):
        result = evaluate_three_strategies(
            qld_krw_price=qld_krw_price,
            tiger_price=tiger_price,
            kr_cash_price=kr_cash_price,
            fx_close=fx_modern,
            start=FIXED_START,
            end=FIXED_END,
            legal_cap=True,
            sell_z=sell_z,
            buy_z=buy_z,
        )
        threshold_rows.append(
            {
                "sell_z": sell_z,
                "buy_z": buy_z,
                "signal_change_count": len(result["signal_log"]),
                **result["tiger_timing"].metrics,
            }
        )
    threshold = pd.DataFrame(threshold_rows)

    stress_rows: list[dict[str, Any]] = []
    dot_index = qqq_close_all.loc[DOTCOM_START:DOTCOM_END].index
    qqq_dot = _align(qqq_close_all, dot_index)
    irx_dot = _align(irx_all, dot_index)
    fred_fx_dot = _align(fred_fx, dot_index)
    kr_rate_dot = _align(kr_rate, dot_index)
    qld_dot_return = (
        2.0 * qqq_dot.pct_change().fillna(0.0)
        - irx_dot.shift(1).ffill().fillna(0.0) / 100.0 / TRADING_DAYS
        - qld_drag / TRADING_DAYS
    ).clip(lower=-0.995)
    qld_dot_usd = build_price_from_returns(qld_dot_return)
    qld_dot_krw = qld_dot_usd * fred_fx_dot
    tiger_dot = build_tiger_synthetic(
        qqq_close=qqq_dot,
        fx_close=fred_fx_dot,
        short_rate_pct=irx_dot,
        choice=choice,
    )
    cash_dot = build_cash_price(kr_rate_dot, dot_index)
    dot_result = evaluate_three_strategies(
        qld_krw_price=qld_dot_krw,
        tiger_price=tiger_dot,
        kr_cash_price=cash_dot,
        fx_close=fred_fx_dot,
        start=DOTCOM_START,
        end=DOTCOM_END,
        legal_cap=False,
    )
    for key, label in (
        ("qld", "QLD direct synthetic"),
        ("tiger_hold", "TIGER ISA hold synthetic"),
        ("tiger_timing", "TIGER ISA timing synthetic"),
    ):
        stress_rows.append(
            {"scenario": "dotcom_1999_2003", "strategy": label, **dot_result[key].metrics}
        )

    for fx_scenario in (
        "actual",
        "flat",
        "krw_appreciates_2pct_pa",
        "krw_appreciates_5pct_first5y",
        "krw_strengthens_20pct_year5",
    ):
        scenario_fx_series = scenario_fx(fx_modern, modern_index, fx_scenario)
        qld_scenario = qld_modern * scenario_fx_series
        tiger_scenario = build_tiger_synthetic(
            qqq_close=qqq_modern,
            fx_close=scenario_fx_series,
            short_rate_pct=irx_modern,
            choice=choice,
        )
        result = evaluate_three_strategies(
            qld_krw_price=qld_scenario,
            tiger_price=tiger_scenario,
            kr_cash_price=kr_cash_price,
            fx_close=scenario_fx_series,
            start=FIXED_START,
            end=FIXED_END,
            legal_cap=True,
        )
        for key, label in (
            ("qld", "QLD direct"),
            ("tiger_hold", "TIGER ISA hold"),
            ("tiger_timing", "TIGER ISA timing"),
        ):
            stress_rows.append(
                {"scenario": fx_scenario, "strategy": label, **result[key].metrics}
            )
    stress = pd.DataFrame(stress_rows)

    leverage = build_leverage_scenarios()
    live_signal = current_fx_signal()

    legal_signal = full_signal_logs["isa_100m_cap_15y_then_hold"]
    legal_hold = full_runs["isa_100m_cap_15y_then_hold_tiger_hold"]
    legal_timing = full_runs["isa_100m_cap_15y_then_hold_tiger_timing"]
    legal_qld = full_runs["isa_100m_cap_15y_then_hold_qld"]

    input_manifest = {
        "schema_version": "equity-v2-isa-tiger-qld-input-snapshot-1",
        "fixed_end": FIXED_END.date().isoformat(),
        "created_at_utc": datetime.now(UTC).isoformat(),
        "files": snapshot_files,
    }
    _write_json(snapshot_dir / "manifest.json", input_manifest)

    output_paths = {
        "summary": output / "equity_v2_isa_tiger_qld_summary.csv",
        "regime": output / "equity_v2_isa_tiger_qld_regime.csv",
        "rolling": output / "equity_v2_isa_tiger_qld_rolling.csv",
        "rolling_summary": output / "equity_v2_isa_tiger_qld_rolling_summary.csv",
        "delay": output / "equity_v2_isa_tiger_qld_delay.csv",
        "threshold": output / "equity_v2_isa_tiger_qld_threshold.csv",
        "stress": output / "equity_v2_isa_tiger_qld_stress.csv",
        "leverage": output / "equity_v2_isa_tiger_qld_leverage.csv",
        "tiger_calibration": output / "equity_v2_isa_tiger_qld_tiger_calibration.csv",
        "qld_calibration": output / "equity_v2_isa_tiger_qld_qld_calibration.csv",
        "signal_log": output / "equity_v2_isa_tiger_qld_signal_log.csv",
        "qld_daily": output / "equity_v2_isa_tiger_qld_qld_daily.csv",
        "tiger_hold_daily": output / "equity_v2_isa_tiger_qld_tiger_hold_daily.csv",
        "tiger_timing_daily": output / "equity_v2_isa_tiger_qld_tiger_timing_daily.csv",
        "report": output / "equity_v2_isa_tiger_qld_report.md",
        "manifest": output / "equity_v2_isa_tiger_qld_manifest.json",
    }
    summary.to_csv(output_paths["summary"], index=False, encoding="utf-8")
    regime.to_csv(output_paths["regime"], index=False, encoding="utf-8")
    rolling.to_csv(output_paths["rolling"], index=False, encoding="utf-8")
    rolling_summary.to_csv(output_paths["rolling_summary"], index=False, encoding="utf-8")
    delay.to_csv(output_paths["delay"], index=False, encoding="utf-8")
    threshold.to_csv(output_paths["threshold"], index=False, encoding="utf-8")
    stress.to_csv(output_paths["stress"], index=False, encoding="utf-8")
    leverage.to_csv(output_paths["leverage"], index=False, encoding="utf-8")
    tiger_calibration.to_csv(output_paths["tiger_calibration"], index=False, encoding="utf-8")
    qld_calibration.to_csv(output_paths["qld_calibration"], index=False, encoding="utf-8")
    legal_signal.to_csv(output_paths["signal_log"], index=False, encoding="utf-8")
    legal_qld.daily.to_csv(output_paths["qld_daily"], encoding="utf-8")
    legal_hold.daily.to_csv(output_paths["tiger_hold_daily"], encoding="utf-8")
    legal_timing.daily.to_csv(output_paths["tiger_timing_daily"], encoding="utf-8")

    manifest = {
        "schema_version": "equity-v2-isa-tiger-qld-result-1",
        "strategy_version": STRATEGY_VERSION,
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "fixed_start": FIXED_START.date().isoformat(),
        "fixed_end": FIXED_END.date().isoformat(),
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "isa_total_contribution_limit_krw": ISA_TOTAL_CONTRIBUTION_LIMIT_KRW,
        "direct_tax_rate": DIRECT_TAX_RATE,
        "isa_tax_rate": ISA_SEPARATE_TAX_RATE,
        "isa_general_exemption_krw": ISA_GENERAL_EXEMPTION_KRW,
        "isa_low_income_exemption_krw": ISA_LOW_INCOME_EXEMPTION_KRW,
        "fx_timing_rule": {
            "sell_z": SELL_Z,
            "restore_z": BUY_Z,
            "reduced_weight": REDUCED_WEIGHT,
            "decision_frequency": "weekly",
            "execution": "next_common_trading_day",
        },
        "qld_residual_drag": qld_drag,
        "tiger_model_choice": _json_ready(choice.__dict__),
        "current_fx_signal": live_signal,
        "input_snapshot": input_manifest,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "outputs": {key: str(path) for key, path in output_paths.items()},
    }
    report = build_report(
        manifest=manifest,
        summary=summary,
        regime=regime,
        rolling_summary=rolling_summary,
        delay=delay,
        threshold=threshold,
        stress=stress,
        leverage=leverage,
        calibration_top=tiger_calibration,
    )
    output_paths["report"].write_text(report, encoding="utf-8")
    _write_json(output_paths["manifest"], manifest)
    return {"manifest": manifest, "outputs": output_paths}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="QLD direct vs TIGER Nasdaq-100 leveraged ISA research"
    )
    parser.add_argument("--refresh-external", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run(refresh_external=args.refresh_external, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
