from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlparse

import numpy as np
import pandas as pd

from btc_guardian import (
    BtcDataError,
    DEFAULT_CONFIG,
    DEFAULT_ONCHAIN_METRICS,
    ROOT,
    build_phase1_report,
    cache_key,
    closed_yahoo_daily_frame,
    fetch_json,
    iso_utc,
    load_config,
    read_price_cache,
    resolve_paths,
    utc_now,
    write_price_cache,
)
from quant_guardian import annualized_metrics, atr, rsi


STRATEGY_VERSION = "btc-research-baseline-0.2"
COINMETRICS_TIMESERIES = (
    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
)
RESEARCH_METRICS = ("BlkCnt",) + DEFAULT_ONCHAIN_METRICS


@dataclass(frozen=True)
class ResearchParameters:
    deep_drawdown_365: float = -0.45
    trend_discount_ratio: float = 0.90
    rsi_capitulation_max: float = 35.0
    return_30d_max: float = -0.10
    required_entry_domains: int = 3
    entry_persistence_days: int = 3
    recovery_sma50_band: float = 0.01
    recovery_rsi_min: float = 45.0
    recovery_persistence_days: int = 3
    trend_sma200_band: float = 0.01
    structural_trend_persistence_days: int = 3
    overheat_percentile_min: float = 0.90
    overheat_price_ratio: float = 1.80
    overheat_return_180d: float = 1.20
    overheat_weekly_rsi: float = 75.0
    required_overheat_domains: int = 3
    overheat_persistence_days: int = 3
    structural_bear_band: float = 0.97
    structural_bear_persistence_days: int = 3
    increase_cooldown_days: int = 3
    core_weight: float = 0.25


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    sell_policy: str
    use_halving: bool
    parameters: ResearchParameters
    fee_bps: float
    slippage_bps: float
    additional_delay_days: int = 0
    bear_market_max_weight: float | None = None
    selection_eligible: bool = True
    research_origin: str = "predeclared-design"


@dataclass
class SimulationResult:
    daily: pd.DataFrame
    trades: pd.DataFrame


JsonFetcher = Callable[[str], Any]


def fetch_coinmetrics_history(
    metrics: Sequence[str],
    start_date: date,
    end_date: date,
    *,
    fetcher: JsonFetcher = fetch_json,
    max_pages: int = 10,
) -> pd.DataFrame:
    query = {
        "assets": "btc",
        "metrics": ",".join(dict.fromkeys(metrics)),
        "frequency": "1d",
        "start_time": start_date.isoformat(),
        "end_time": end_date.isoformat(),
        "page_size": 10000,
    }
    url = COINMETRICS_TIMESERIES + "?" + urlencode(query)
    rows: list[Mapping[str, Any]] = []
    visited: set[str] = set()

    for _ in range(max_pages):
        if url in visited:
            raise BtcDataError("Coin Metrics pagination loop detected")
        visited.add(url)
        payload = fetcher(url)
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("data"), list
        ):
            raise BtcDataError("Coin Metrics timeseries response is invalid")
        rows.extend(item for item in payload["data"] if isinstance(item, Mapping))
        next_url = payload.get("next_page_url")
        if not next_url:
            break
        parsed = urlparse(str(next_url))
        if parsed.scheme != "https" or parsed.netloc != "community-api.coinmetrics.io":
            raise BtcDataError("Coin Metrics returned an untrusted next-page URL")
        url = str(next_url)
    else:
        raise BtcDataError(f"Coin Metrics history exceeded {max_pages} pages")

    return parse_coinmetrics_history(rows, metrics)


def parse_coinmetrics_history(
    rows: Sequence[Mapping[str, Any]],
    metrics: Sequence[str],
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        if "time" not in row:
            continue
        record: dict[str, Any] = {
            "Date": pd.to_datetime(row["time"], utc=True).tz_localize(None)
        }
        for metric in metrics:
            record[metric] = pd.to_numeric(row.get(metric), errors="coerce")
        records.append(record)
    if not records:
        raise BtcDataError("Coin Metrics returned no usable daily rows")
    frame = (
        pd.DataFrame(records).drop_duplicates("Date", keep="last").sort_values("Date")
    )
    frame = frame.set_index("Date")
    if "BlkCnt" not in frame or frame["BlkCnt"].dropna().empty:
        raise BtcDataError("Coin Metrics history is missing BlkCnt")
    return frame


def load_coinmetrics_history(
    *,
    refresh: bool,
    config: Mapping[str, Any],
    now_utc: datetime,
) -> tuple[pd.DataFrame, bool, str | None]:
    paths = resolve_paths(dict(config))
    cache_file = paths.cache / "coinmetrics_timeseries_btc.csv"
    research_cfg = config.get("btc", {}).get("research_runtime", {})
    start = date.fromisoformat(
        str(research_cfg.get("coinmetrics_start_date", "2009-01-03"))
    )
    try:
        if refresh:
            frame = fetch_coinmetrics_history(RESEARCH_METRICS, start, now_utc.date())
            write_price_cache(frame, cache_file)
            return frame, False, None
        return read_metric_cache(cache_file, RESEARCH_METRICS), False, None
    except Exception as exc:
        if refresh and cache_file.exists():
            return read_metric_cache(cache_file, RESEARCH_METRICS), True, str(exc)
        raise BtcDataError(f"Coin Metrics history unavailable: {exc}") from exc


def read_metric_cache(path: Path, metrics: Sequence[str]) -> pd.DataFrame:
    if not path.exists():
        raise BtcDataError(f"Metric cache does not exist: {path}")
    frame = pd.read_csv(path)
    if frame.empty or "Date" not in frame:
        raise BtcDataError(f"Metric cache is invalid: {path}")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).drop_duplicates("Date").sort_values("Date")
    for metric in metrics:
        if metric not in frame:
            frame[metric] = np.nan
        frame[metric] = pd.to_numeric(frame[metric], errors="coerce")
    return frame.set_index("Date")


def expanding_percentile(series: pd.Series, min_periods: int = 365) -> pd.Series:
    history: list[float] = []
    output = pd.Series(np.nan, index=series.index, dtype=float)
    for index, value in series.items():
        if pd.notna(value):
            numeric = float(value)
            if len(history) >= min_periods:
                output.loc[index] = bisect.bisect_right(history, numeric) / len(history)
            bisect.insort(history, numeric)
    return output


def _phase_labels(progress: pd.Series) -> pd.Series:
    conditions = [
        progress < 0.08,
        progress < 0.32,
        progress < 0.50,
        progress < 0.75,
        progress < 1.00,
    ]
    labels = [
        "HALVING_TRANSITION",
        "POST_HALVING_EXPANSION",
        "LATE_EXPANSION_DISTRIBUTION",
        "CONTRACTION_RECOVERY",
        "PRE_HALVING_ACCUMULATION",
    ]
    values = np.select(conditions, labels, default="UNKNOWN")
    return pd.Series(values, index=progress.index, dtype="object")


def build_feature_frame(
    upbit: pd.DataFrame,
    usd: pd.DataFrame,
    fx: pd.DataFrame,
    onchain: pd.DataFrame,
    *,
    onchain_lag_days: int = 2,
    percentile_min_periods: int = 365,
) -> pd.DataFrame:
    upbit = upbit.sort_index().copy()
    usd = usd.sort_index().copy()
    fx = fx.sort_index().copy()
    onchain = onchain.sort_index().copy()
    index = pd.DatetimeIndex(upbit.index).tz_localize(None)
    frame = pd.DataFrame(index=index)

    for source, target in (
        ("Open", "open"),
        ("High", "high"),
        ("Low", "low"),
        ("Close", "close"),
        ("Volume", "volume"),
        ("QuoteVolume", "quote_volume"),
    ):
        frame[target] = pd.to_numeric(upbit.get(source), errors="coerce").reindex(index)

    frame["btc_usd_close"] = (
        pd.to_numeric(usd["Close"], errors="coerce").reindex(index).ffill()
    )
    # The FX daily label can finish after the BTC UTC candle, so use the prior labeled day.
    frame["usdkrw_close"] = (
        pd.to_numeric(fx["Close"], errors="coerce").shift(1).reindex(index).ffill()
    )
    frame["synthetic_krw"] = frame["btc_usd_close"] * frame["usdkrw_close"]
    frame["kimchi_premium"] = frame["close"] / frame["synthetic_krw"] - 1

    close = frame["close"]
    for window in (20, 50, 100, 200):
        frame[f"sma{window}"] = close.rolling(window, min_periods=window).mean()
        frame[f"ema{window}"] = close.ewm(
            span=window, adjust=False, min_periods=window
        ).mean()
    frame["close_sma50_ratio"] = close / frame["sma50"]
    frame["close_sma200_ratio"] = close / frame["sma200"]
    frame["sma200_slope_60d"] = frame["sma200"] / frame["sma200"].shift(60) - 1

    weekly_close = close.resample("W-SUN").last()
    weekly_wma20 = weekly_close.rolling(20, min_periods=20).mean()
    weekly_wma40 = weekly_close.rolling(40, min_periods=40).mean()
    weekly_rsi14 = rsi(weekly_close, 14)
    frame["wma20"] = weekly_wma20.reindex(index).ffill()
    frame["wma40"] = weekly_wma40.reindex(index).ffill()
    frame["weekly_rsi14"] = weekly_rsi14.reindex(index).ffill()

    for window in (30, 90, 180, 365):
        frame[f"return_{window}d"] = close.pct_change(window, fill_method=None)
    frame["drawdown_365"] = close / close.rolling(365, min_periods=365).max() - 1
    frame["drawdown_ath"] = close / close.cummax() - 1
    frame["rsi14"] = rsi(close, 14)
    frame["atr14"] = atr(frame["high"], frame["low"], close, 14)
    frame["natr14"] = frame["atr14"] / close
    frame["volatility_30d"] = close.pct_change(fill_method=None).rolling(30).std(
        ddof=0
    ) * math.sqrt(365)
    frame["volatility_90d"] = close.pct_change(fill_method=None).rolling(90).std(
        ddof=0
    ) * math.sqrt(365)

    ema_fast = close.ewm(span=12, adjust=False, min_periods=26).mean()
    ema_slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    frame["ppo"] = (ema_fast / ema_slow - 1) * 100
    frame["ppo_signal"] = frame["ppo"].ewm(span=9, adjust=False, min_periods=9).mean()
    frame["ppo_hist"] = frame["ppo"] - frame["ppo_signal"]
    volume_mean = frame["volume"].rolling(30, min_periods=30).mean()
    volume_std = frame["volume"].rolling(30, min_periods=30).std(ddof=0)
    frame["volume_z30"] = (frame["volume"] - volume_mean) / volume_std.replace(
        0, np.nan
    )
    bollinger_std = close.rolling(20, min_periods=20).std(ddof=0)
    frame["bollinger_width"] = 4 * bollinger_std / frame["sma20"]
    frame["bollinger_position"] = (close - (frame["sma20"] - 2 * bollinger_std)) / (
        4 * bollinger_std
    ).replace(0, np.nan)

    block_count = pd.to_numeric(onchain["BlkCnt"], errors="coerce").fillna(0)
    historical_height = block_count.cumsum() - 1
    frame["block_height"] = historical_height.reindex(index).ffill()
    frame["halving_epoch"] = np.floor(frame["block_height"] / 210_000)
    frame["cycle_progress"] = (frame["block_height"] % 210_000) / 210_000
    frame["cycle_sin"] = np.sin(2 * math.pi * frame["cycle_progress"])
    frame["cycle_cos"] = np.cos(2 * math.pi * frame["cycle_progress"])
    frame["phase_label"] = _phase_labels(frame["cycle_progress"])
    frame.loc[frame["block_height"].isna(), "phase_label"] = "UNKNOWN"

    available_onchain_history = onchain.shift(onchain_lag_days)
    available_onchain = available_onchain_history.reindex(index).ffill()
    for metric in DEFAULT_ONCHAIN_METRICS:
        frame[metric] = pd.to_numeric(available_onchain.get(metric), errors="coerce")
    frame["realized_cap_usd"] = frame["CapMrktCurUSD"] / frame["CapMVRVCur"].replace(
        0, np.nan
    )
    frame["realized_price_usd"] = frame["realized_cap_usd"] / frame["SplyCur"].replace(
        0, np.nan
    )
    frame["nupl_derived"] = 1 - 1 / frame["CapMVRVCur"].replace(0, np.nan)
    frame["miner_revenue_usd"] = (
        frame["IssTotUSD"] + frame["FeeTotNtv"] * frame["PriceUSD"]
    )
    frame["price_realized_ratio"] = frame["btc_usd_close"] / frame["realized_price_usd"]
    historical_miner_revenue = pd.to_numeric(
        available_onchain_history["IssTotUSD"], errors="coerce"
    ) + pd.to_numeric(
        available_onchain_history["FeeTotNtv"], errors="coerce"
    ) * pd.to_numeric(available_onchain_history["PriceUSD"], errors="coerce")
    historical_mvrv_percentile = expanding_percentile(
        pd.to_numeric(available_onchain_history["CapMVRVCur"], errors="coerce"),
        min_periods=percentile_min_periods,
    )
    historical_miner_percentile = expanding_percentile(
        historical_miner_revenue,
        min_periods=percentile_min_periods,
    )
    frame["mvrv_percentile"] = historical_mvrv_percentile.reindex(index).ffill()
    frame["miner_revenue_percentile"] = historical_miner_percentile.reindex(
        index
    ).ffill()
    frame["kimchi_premium_percentile"] = expanding_percentile(
        frame["kimchi_premium"], min_periods=percentile_min_periods
    )
    frame["onchain_available"] = frame["CapMVRVCur"].notna()

    core_columns = [
        "open",
        "high",
        "low",
        "close",
        "sma50",
        "sma200",
        "rsi14",
        "ppo_hist",
        "block_height",
    ]
    frame["feature_ready"] = frame[core_columns].notna().all(axis=1)
    return frame


def build_evidence_frame(
    features: pd.DataFrame,
    parameters: ResearchParameters,
    *,
    use_halving: bool,
) -> pd.DataFrame:
    evidence = pd.DataFrame(index=features.index)
    evidence["cycle_accumulation"] = features["phase_label"].isin(
        ["CONTRACTION_RECOVERY", "PRE_HALVING_ACCUMULATION"]
    )
    evidence["cycle_distribution"] = features["phase_label"].eq(
        "LATE_EXPANSION_DISTRIBUTION"
    )
    evidence["drawdown_value"] = (
        features["drawdown_365"] <= parameters.deep_drawdown_365
    )
    evidence["trend_discount"] = (
        features["close_sma200_ratio"] <= parameters.trend_discount_ratio
    )
    evidence["momentum_capitulation"] = (
        features["rsi14"] <= parameters.rsi_capitulation_max
    ) & (features["return_30d"] <= parameters.return_30d_max)
    evidence["onchain_value"] = (
        (features["mvrv_percentile"] <= 0.30)
        | (features["price_realized_ratio"] <= 1.15)
    ).fillna(False)
    noncycle_entry = [
        "drawdown_value",
        "trend_discount",
        "momentum_capitulation",
        "onchain_value",
    ]
    evidence["entry_noncycle_count"] = evidence[noncycle_entry].sum(axis=1)
    evidence["entry_domain_count"] = evidence["entry_noncycle_count"] + (
        evidence["cycle_accumulation"].astype(int) if use_halving else 0
    )
    entry_prior_adjustment = (
        evidence["cycle_accumulation"].astype(int)
        if use_halving
        else pd.Series(0, index=evidence.index, dtype=int)
    )
    evidence["entry_required_noncycle"] = (
        parameters.required_entry_domains - entry_prior_adjustment
    ).clip(lower=2)
    evidence["entry_raw"] = (
        evidence["entry_noncycle_count"] >= evidence["entry_required_noncycle"]
    )
    evidence["entry_streak"] = _consecutive_true(evidence["entry_raw"])
    evidence["entry_persistent"] = (
        evidence["entry_streak"] >= parameters.entry_persistence_days
    )
    evidence["recent_capitulation"] = (
        evidence["entry_raw"].rolling(90, min_periods=1).max().astype(bool)
    )
    evidence["seed_four_of_four"] = (
        (features["drawdown_365"] <= -0.50)
        & (features["close_sma200_ratio"] <= 0.80)
        & (features["rsi14"] <= 30)
        & (features["return_30d"] <= -0.15)
    )

    evidence["overheat_price"] = (
        features["close_sma200_ratio"] >= parameters.overheat_price_ratio
    )
    evidence["overheat_momentum"] = (
        features["return_180d"] >= parameters.overheat_return_180d
    ) | (features["weekly_rsi14"] >= parameters.overheat_weekly_rsi)
    evidence["overheat_onchain"] = (
        features["mvrv_percentile"] >= parameters.overheat_percentile_min
    ).fillna(False)
    evidence["overheat_krw"] = (
        (features["kimchi_premium"] > 0)
        & (features["kimchi_premium_percentile"] >= parameters.overheat_percentile_min)
    ).fillna(False)
    overheat_columns = [
        "overheat_price",
        "overheat_momentum",
        "overheat_onchain",
        "overheat_krw",
    ]
    evidence["overheat_domain_count"] = evidence[overheat_columns].sum(axis=1)
    overheat_prior_adjustment = (
        evidence["cycle_distribution"].astype(int)
        if use_halving
        else pd.Series(0, index=evidence.index, dtype=int)
    )
    evidence["overheat_required_domains"] = (
        parameters.required_overheat_domains - overheat_prior_adjustment
    ).clip(lower=2)
    evidence["overheat_raw"] = (
        evidence["overheat_domain_count"] >= evidence["overheat_required_domains"]
    )
    evidence["overheat_streak"] = _consecutive_true(evidence["overheat_raw"])
    evidence["overheat_persistent"] = (
        evidence["overheat_streak"] >= parameters.overheat_persistence_days
    )

    evidence["recovery"] = (
        (features["close"] >= features["sma50"] * (1 + parameters.recovery_sma50_band))
        & (features["rsi14"] >= parameters.recovery_rsi_min)
        & (features["ppo_hist"] > 0)
    )
    evidence["recovery_streak"] = _consecutive_true(evidence["recovery"])
    evidence["recovery_persistent"] = (
        evidence["recovery_streak"] >= parameters.recovery_persistence_days
    )
    evidence["structural_trend"] = (
        (features["close"] >= features["sma200"] * (1 + parameters.trend_sma200_band))
        & (features["sma200_slope_60d"] >= 0)
        & (features["close"] >= features["wma40"])
    )
    evidence["structural_trend_streak"] = _consecutive_true(
        evidence["structural_trend"]
    )
    evidence["structural_trend_persistent"] = (
        evidence["structural_trend_streak"]
        >= parameters.structural_trend_persistence_days
    )
    evidence["full_trend"] = (
        evidence["structural_trend_persistent"]
        & (features["ppo_hist"] >= 0)
        & ~evidence["overheat_persistent"]
    )
    evidence["trend_weakness"] = (features["close"] < features["sma50"] * 0.98) & (
        features["ppo_hist"] < 0
    )
    evidence["bear_regime"] = (features["close"] < features["sma200"]) & (
        features["sma200_slope_60d"] < 0
    )
    evidence["structural_bear_raw"] = (
        (features["close"] < features["sma200"] * parameters.structural_bear_band)
        & (features["sma200_slope_60d"] < 0)
        & (features["return_90d"] < 0)
        & ~evidence["entry_raw"]
    )
    evidence["structural_bear_streak"] = _consecutive_true(
        evidence["structural_bear_raw"]
    )
    evidence["structural_bear_persistent"] = (
        evidence["structural_bear_streak"]
        >= parameters.structural_bear_persistence_days
    )
    evidence["data_ready"] = features["feature_ready"].fillna(False)
    return evidence


def _consecutive_true(series: pd.Series) -> pd.Series:
    values: list[int] = []
    streak = 0
    for value in series.fillna(False).astype(bool):
        streak = streak + 1 if value else 0
        values.append(streak)
    return pd.Series(values, index=series.index, dtype=int)


def _step_weight(current: float, desired: float) -> float:
    if math.isclose(current, desired, abs_tol=1e-9):
        return current
    upward = [0.0, 0.20, 0.40, 0.60, 0.80, 1.00]
    downward = [1.00, 0.75, 0.50, 0.25, 0.00]
    if desired > current:
        candidates = [value for value in upward if current < value <= desired + 1e-9]
        return min(candidates) if candidates else desired
    candidates = [value for value in downward if desired - 1e-9 <= value < current]
    return max(candidates) if candidates else desired


def _state_for_weight(weight: float, previous_weight: float) -> str:
    if math.isclose(weight, 0.0):
        return "EXIT" if previous_weight > 0 else "WAIT"
    labels = {
        0.20: "ACCUMULATE_1",
        0.25: "CORE_ONLY",
        0.40: "ACCUMULATE_2",
        0.50: "REDUCE_2",
        0.60: "CONFIRM_BUY",
        0.75: "REDUCE_1",
        0.80: "TREND_HOLD",
        1.00: "FULL_HOLD",
    }
    return labels.get(round(weight, 2), "WATCH")


def generate_state_targets(
    features: pd.DataFrame,
    parameters: ResearchParameters,
    *,
    sell_policy: str = "core_tactical",
    use_halving: bool = True,
    bear_market_max_weight: float | None = None,
) -> pd.DataFrame:
    if sell_policy not in {"core_tactical", "tiered", "all_out"}:
        raise ValueError(f"Unsupported sell policy: {sell_policy}")
    if bear_market_max_weight is not None and not 0 <= bear_market_max_weight <= 1:
        raise ValueError("Bear-market weight cap must be between zero and one")
    evidence = build_evidence_frame(features, parameters, use_halving=use_halving)
    rows: list[dict[str, Any]] = []
    current = 0.0
    last_change_position = -10_000

    for position, (index, row) in enumerate(evidence.iterrows()):
        previous = current
        desired = current
        reasons: list[str] = []
        if not bool(row["data_ready"]):
            desired = current
            reasons.append("필수 가격·반감기 특성 준비 전")
        elif bool(row["structural_bear_persistent"]):
            streak = int(row["structural_bear_streak"])
            if sell_policy == "all_out":
                desired = 0.0
            elif sell_policy == "tiered":
                desired = 0.0 if streak >= 10 else parameters.core_weight
            else:
                desired = parameters.core_weight
            reasons.append("장기선·기울기·중기 모멘텀 구조적 약세")
        elif bool(row["overheat_persistent"]) and bool(row["trend_weakness"]):
            desired = min(current, 0.50)
            reasons.append("과열 다중확인 후 중기 추세 약화")
        elif bool(row["overheat_persistent"]):
            desired = min(current, 0.75)
            reasons.append("반감기 중후반 과열영역 다중확인")
        elif bool(row["full_trend"]):
            desired = 1.00
            reasons.append("장기추세·주봉·모멘텀 동시 확인")
        elif bool(row["structural_trend_persistent"]):
            desired = 0.80
            reasons.append("SMA200·40주선 구조적 추세 확인")
        elif bool(row["recovery_persistent"]) and bool(row["recent_capitulation"]):
            desired = max(current, 0.60)
            reasons.append("최근 투매 이후 SMA50·RSI·PPO 회복")
        elif int(row["entry_streak"]) >= max(5, parameters.entry_persistence_days):
            desired = max(current, 0.40)
            reasons.append("가치·투매 독립영역 신호 지속")
        elif bool(row["entry_persistent"]):
            desired = max(current, 0.20)
            reasons.append("반감기 포함 가치·투매 독립영역 확인")
        elif current > 0 and bool(row["trend_weakness"]):
            desired = parameters.core_weight if sell_policy != "all_out" else 0.0
            reasons.append("중기 추세 약화로 전술비중 축소")
        else:
            reasons.append("기존 상태 유지")

        if (
            bear_market_max_weight is not None
            and bool(row["bear_regime"])
            and desired > bear_market_max_weight
        ):
            desired = bear_market_max_weight
            reasons.append("약세장 재진입 비중 상한")

        if (
            desired > current
            and position - last_change_position < parameters.increase_cooldown_days
        ):
            desired = current
            reasons.append("추가매수 냉각기간")
        current = _step_weight(current, desired)
        if not math.isclose(current, previous, abs_tol=1e-9):
            last_change_position = position
        rows.append(
            {
                "Date": index,
                "state": _state_for_weight(current, previous),
                "target_weight": current,
                "previous_weight": previous,
                "reason": "; ".join(reasons),
                "entry_domains": int(row["entry_domain_count"]),
                "overheat_domains": int(row["overheat_domain_count"]),
                "entry_raw": bool(row["entry_raw"]),
                "overheat_raw": bool(row["overheat_raw"]),
                "structural_bear": bool(row["structural_bear_persistent"]),
                "bear_regime": bool(row["bear_regime"]),
            }
        )
    return pd.DataFrame(rows).set_index("Date")


def simulate_strategy(
    features: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    fee_bps: float,
    slippage_bps: float,
    additional_delay_days: int = 0,
    initial_capital: float = 1.0,
) -> SimulationResult:
    if fee_bps < 0 or slippage_bps < 0 or additional_delay_days < 0:
        raise ValueError("Costs and delays must be non-negative")
    shift_days = 1 + additional_delay_days
    execution_target = signals["target_weight"].shift(shift_days)
    execution_state = signals["state"].shift(shift_days)
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    cash = float(initial_capital)
    units = 0.0
    last_target = 0.0
    previous_equity = float(initial_capital)
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for position, index in enumerate(features.index):
        open_price = float(features.at[index, "open"])
        close_price = float(features.at[index, "close"])
        desired = execution_target.loc[index]
        turnover = 0.0
        fee_cost = 0.0
        slippage_cost = 0.0
        traded_units = 0.0
        trade_side = ""
        open_nav = cash + units * open_price

        if pd.notna(desired) and not math.isclose(
            float(desired), last_target, abs_tol=1e-9
        ):
            desired = float(np.clip(desired, 0.0, 1.0))
            desired_units = open_nav * desired / open_price
            unit_change = desired_units - units
            if unit_change > 1e-15:
                execution_price = open_price * (1 + slippage_rate)
                max_units = cash / (execution_price * (1 + fee_rate))
                bought = min(unit_change, max_units)
                notional = bought * execution_price
                fee_cost = notional * fee_rate
                slippage_cost = bought * open_price * slippage_rate
                cash -= notional + fee_cost
                units += bought
                traded_units = bought
                trade_side = "BUY"
            elif unit_change < -1e-15:
                execution_price = open_price * (1 - slippage_rate)
                sold = min(-unit_change, units)
                notional = sold * execution_price
                fee_cost = notional * fee_rate
                slippage_cost = sold * open_price * slippage_rate
                cash += notional - fee_cost
                units -= sold
                traded_units = -sold
                trade_side = "SELL"
            raw_notional = abs(traded_units) * open_price
            turnover = raw_notional / open_nav if open_nav > 0 else 0.0
            if trade_side:
                signal_position = position - shift_days
                signal_date = (
                    features.index[signal_position].date().isoformat()
                    if signal_position >= 0
                    else None
                )
                trade_rows.append(
                    {
                        "date": index,
                        "signal_date": signal_date,
                        "side": trade_side,
                        "state": execution_state.loc[index],
                        "target_weight": desired,
                        "units": abs(traded_units),
                        "open_price": open_price,
                        "fee_cost": fee_cost,
                        "slippage_cost": slippage_cost,
                        "turnover": turnover,
                    }
                )
            last_target = desired

        cash = max(cash, 0.0)
        units = max(units, 0.0)
        equity = cash + units * close_price
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "equity": equity,
                "daily_return": daily_return,
                "cash": cash,
                "btc_units": units,
                "actual_weight": actual_weight,
                "executed_target": last_target,
                "turnover": turnover,
                "fee_cost": fee_cost,
                "slippage_cost": slippage_cost,
            }
        )
        previous_equity = equity

    daily = pd.DataFrame(daily_rows).set_index("Date")
    trades = pd.DataFrame(trade_rows)
    return SimulationResult(daily=daily, trades=trades)


def make_buy_hold_signals(features: pd.DataFrame) -> pd.DataFrame:
    ready = features["feature_ready"].fillna(False)
    target = pd.Series(0.0, index=features.index)
    if ready.any():
        target.loc[ready.idxmax() :] = 1.0
    state = pd.Series("WAIT", index=features.index)
    state.loc[target > 0] = "BUY_HOLD"
    return pd.DataFrame({"target_weight": target, "state": state}, index=features.index)


def make_ma_trend_signals(
    features: pd.DataFrame,
    ma_column: str,
    *,
    band: float = 0.01,
    persistence_days: int = 3,
) -> pd.DataFrame:
    if ma_column not in features:
        raise ValueError(f"Missing moving-average column: {ma_column}")
    if band < 0 or persistence_days < 1:
        raise ValueError("Trend benchmark band and persistence must be positive")

    invested = False
    above_streak = 0
    below_streak = 0
    targets: list[float] = []
    states: list[str] = []
    for _, row in features.iterrows():
        ready = bool(row.get("feature_ready", False))
        close = float(row["close"])
        moving_average = float(row[ma_column])
        if not ready or not math.isfinite(moving_average):
            targets.append(1.0 if invested else 0.0)
            states.append("HOLD" if invested else "WAIT")
            continue

        above = close >= moving_average * (1 + band)
        below = close <= moving_average * (1 - band)
        above_streak = above_streak + 1 if above else 0
        below_streak = below_streak + 1 if below else 0
        if not invested and above_streak >= persistence_days:
            invested = True
        elif invested and below_streak >= persistence_days:
            invested = False
        targets.append(1.0 if invested else 0.0)
        states.append("TREND_IN" if invested else "TREND_OUT")
    return pd.DataFrame(
        {"target_weight": targets, "state": states},
        index=features.index,
    )


def simulate_monthly_dca(
    features: pd.DataFrame,
    *,
    fee_bps: float,
    slippage_bps: float,
    contribution: float = 1.0,
) -> tuple[SimulationResult, float]:
    if contribution <= 0 or fee_bps < 0 or slippage_bps < 0:
        raise ValueError("DCA contribution and costs must be non-negative")
    ready_dates = features.index[features["feature_ready"].fillna(False)]
    if ready_dates.empty:
        raise ValueError("DCA benchmark requires at least one research-ready row")
    contribution_dates = set(
        pd.Series(ready_dates, index=ready_dates)
        .groupby(ready_dates.to_period("M"))
        .first()
        .tolist()
    )
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    cash = 0.0
    units = 0.0
    previous_equity = 0.0
    total_contributed = 0.0
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for index, row in features.iterrows():
        open_price = float(row["open"])
        close_price = float(row["close"])
        external_flow = contribution if index in contribution_dates else 0.0
        cash += external_flow
        total_contributed += external_flow
        fee_cost = 0.0
        slippage_cost = 0.0
        turnover = 0.0

        if external_flow > 0:
            open_nav = cash + units * open_price
            execution_price = open_price * (1 + slippage_rate)
            bought = cash / (execution_price * (1 + fee_rate))
            notional = bought * execution_price
            fee_cost = notional * fee_rate
            slippage_cost = bought * open_price * slippage_rate
            cash -= notional + fee_cost
            units += bought
            turnover = bought * open_price / open_nav if open_nav > 0 else 0.0
            trade_rows.append(
                {
                    "date": index,
                    "signal_date": "monthly_schedule",
                    "side": "BUY",
                    "state": "DCA",
                    "target_weight": 1.0,
                    "units": bought,
                    "open_price": open_price,
                    "fee_cost": fee_cost,
                    "slippage_cost": slippage_cost,
                    "turnover": turnover,
                }
            )

        cash = max(cash, 0.0)
        equity = cash + units * close_price
        if previous_equity > 0:
            daily_return = (equity - external_flow) / previous_equity - 1
        else:
            daily_return = 0.0
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "equity": equity,
                "daily_return": daily_return,
                "cash": cash,
                "btc_units": units,
                "actual_weight": actual_weight,
                "executed_target": 1.0 if units > 0 else 0.0,
                "turnover": turnover,
                "fee_cost": fee_cost,
                "slippage_cost": slippage_cost,
                "external_flow": external_flow,
            }
        )
        previous_equity = equity

    return (
        SimulationResult(
            daily=pd.DataFrame(daily_rows).set_index("Date"),
            trades=pd.DataFrame(trade_rows),
        ),
        total_contributed,
    )


def evaluate_benchmarks(
    features: pd.DataFrame,
    *,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    signals = {
        "cash": pd.DataFrame(
            {"target_weight": 0.0, "state": "CASH"}, index=features.index
        ),
        "buy_hold": make_buy_hold_signals(features),
        "sma200_trend": make_ma_trend_signals(features, "sma200"),
        "wma40_trend": make_ma_trend_signals(features, "wma40"),
    }
    outputs = {
        name: simulate_strategy(
            features,
            signal,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        for name, signal in signals.items()
    }
    dca, dca_contributed = simulate_monthly_dca(
        features,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    outputs["monthly_dca"] = dca

    rows: list[dict[str, Any]] = []
    for name, simulation in outputs.items():
        metrics = performance_metrics(simulation)
        contributed = dca_contributed if name == "monthly_dca" else 1.0
        rows.append(
            {
                "benchmark_id": name,
                "capital_contributed": contributed,
                "terminal_wealth": metrics["terminal_wealth"],
                "wealth_to_contribution": metrics["terminal_wealth"] / contributed,
                **{
                    key: value
                    for key, value in metrics.items()
                    if key != "terminal_wealth"
                },
            }
        )
    return pd.DataFrame(rows), outputs


def performance_metrics(simulation: SimulationResult) -> dict[str, float | int | bool]:
    base = annualized_metrics(simulation.daily["daily_return"], periods_per_year=365)
    return {
        **base,
        "terminal_wealth": float(simulation.daily["equity"].iloc[-1]),
        "exposure": float(simulation.daily["actual_weight"].mean()),
        "turnover": float(simulation.daily["turnover"].sum()),
        "trades": int(len(simulation.trades)),
        "fee_cost": float(simulation.daily["fee_cost"].sum()),
        "slippage_cost": float(simulation.daily["slippage_cost"].sum()),
        "mdd_gate_pass": bool(base["mdd"] >= -0.50),
    }


def _period_metrics(returns: pd.Series) -> dict[str, float]:
    return annualized_metrics(returns, periods_per_year=365)


def trim_to_research_window(features: pd.DataFrame) -> pd.DataFrame:
    ready = features["feature_ready"].fillna(False)
    if not ready.any():
        raise BtcDataError("Feature frame has no research-ready rows")
    return features.loc[ready.idxmax() :].copy()


def _simulation_slice_metrics(
    simulation: SimulationResult,
    mask: pd.Series | np.ndarray,
) -> dict[str, float | int | bool]:
    daily = simulation.daily.loc[mask]
    metrics = _period_metrics(daily["daily_return"])
    if daily.empty:
        return {
            **metrics,
            "exposure": np.nan,
            "turnover": np.nan,
            "trades": 0,
            "mdd_gate_pass": False,
        }
    if simulation.trades.empty or "date" not in simulation.trades:
        trade_count = 0
    else:
        trade_dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        trade_count = int(
            (
                (trade_dates >= daily.index.min()) & (trade_dates <= daily.index.max())
            ).sum()
        )
    return {
        **metrics,
        "exposure": float(daily["actual_weight"].mean()),
        "turnover": float(daily["turnover"].sum()),
        "trades": trade_count,
        "mdd_gate_pass": bool(metrics["mdd"] >= -0.50),
    }


def _select_training_candidate(
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
    specs: Sequence[CandidateSpec],
    train_mask: pd.Series | np.ndarray,
) -> tuple[str, dict[str, float | int | bool] | None]:
    rows: list[tuple[str, dict[str, float | int | bool]]] = []
    for spec in specs:
        if not spec.selection_eligible:
            continue
        metrics = _simulation_slice_metrics(outputs[spec.candidate_id][1], train_mask)
        if not bool(metrics["mdd_gate_pass"]) or not math.isfinite(
            float(metrics["calmar"])
        ):
            continue
        rows.append((spec.candidate_id, metrics))
    if not rows:
        return "cash", None
    rows.sort(
        key=lambda item: (
            float(item[1]["calmar"]),
            float(item[1]["cagr"]),
            -float(item[1]["turnover"]),
        ),
        reverse=True,
    )
    return rows[0]


def anchored_walk_forward_metrics(
    features: pd.DataFrame,
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
    specs: Sequence[CandidateSpec],
    buy_hold: SimulationResult,
    *,
    minimum_train_days: int = 730,
) -> pd.DataFrame:
    ready_dates = features.index[features["feature_ready"].fillna(False)]
    if ready_dates.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for year in sorted(set(features.index.year)):
        start = pd.Timestamp(year=year, month=1, day=1)
        end = pd.Timestamp(year=year, month=12, day=31)
        train_mask = pd.Series(features.index < start, index=features.index)
        test_mask = pd.Series(
            (features.index >= start) & (features.index <= end),
            index=features.index,
        )
        if int(train_mask.sum()) < minimum_train_days or int(test_mask.sum()) < 90:
            continue
        selected_id, train_metrics = _select_training_candidate(
            outputs, specs, train_mask
        )
        if selected_id == "cash":
            test_returns = pd.Series(0.0, index=features.index[test_mask])
            strategy_metrics = _period_metrics(test_returns)
            test_exposure = 0.0
            test_trades = 0
        else:
            selected_simulation = outputs[selected_id][1]
            strategy_metrics = _simulation_slice_metrics(selected_simulation, test_mask)
            test_exposure = float(strategy_metrics["exposure"])
            test_trades = int(strategy_metrics["trades"])
        hold_metrics = _simulation_slice_metrics(buy_hold, test_mask)
        rows.append(
            {
                "test_year": year,
                "train_start": ready_dates.min().date().isoformat(),
                "train_end": (start - timedelta(days=1)).date().isoformat(),
                "train_days": int(train_mask.sum()),
                "test_days": int(test_mask.sum()),
                "mode": "anchored train-only candidate selection",
                "selected_candidate": selected_id,
                "train_cagr": np.nan
                if train_metrics is None
                else train_metrics["cagr"],
                "train_mdd": np.nan if train_metrics is None else train_metrics["mdd"],
                "train_calmar": np.nan
                if train_metrics is None
                else train_metrics["calmar"],
                "strategy_total_return": strategy_metrics["total_return"],
                "strategy_cagr": strategy_metrics["cagr"],
                "strategy_mdd": strategy_metrics["mdd"],
                "strategy_calmar": strategy_metrics["calmar"],
                "strategy_exposure": test_exposure,
                "strategy_trades": test_trades,
                "buy_hold_total_return": hold_metrics["total_return"],
                "buy_hold_mdd": hold_metrics["mdd"],
                "excess_total_return": (
                    strategy_metrics["total_return"] - hold_metrics["total_return"]
                ),
            }
        )
    return pd.DataFrame(rows)


def candidate_year_metrics(
    features: pd.DataFrame,
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate_id, (_, simulation) in outputs.items():
        for year in sorted(set(features.index.year)):
            mask = pd.Series(features.index.year == year, index=features.index)
            if int(mask.sum()) < 90:
                continue
            metrics = _simulation_slice_metrics(simulation, mask)
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "year": year,
                    "days": int(mask.sum()),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def candidate_cycle_metrics(
    features: pd.DataFrame,
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
    buy_hold: SimulationResult,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    epochs = pd.to_numeric(features["halving_epoch"], errors="coerce")
    for candidate_id, (_, simulation) in outputs.items():
        for epoch in sorted(epochs.dropna().unique()):
            mask = epochs.eq(epoch) & features["feature_ready"].fillna(False)
            if int(mask.sum()) < 90:
                continue
            strategy_metrics = _simulation_slice_metrics(simulation, mask)
            hold_metrics = _simulation_slice_metrics(buy_hold, mask)
            dates = features.index[mask]
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "halving_epoch": int(epoch),
                    "start": dates.min().date().isoformat(),
                    "end": dates.max().date().isoformat(),
                    "days": int(mask.sum()),
                    "strategy_total_return": strategy_metrics["total_return"],
                    "strategy_cagr": strategy_metrics["cagr"],
                    "strategy_mdd": strategy_metrics["mdd"],
                    "strategy_calmar": strategy_metrics["calmar"],
                    "strategy_exposure": strategy_metrics["exposure"],
                    "strategy_trades": strategy_metrics["trades"],
                    "buy_hold_total_return": hold_metrics["total_return"],
                    "buy_hold_mdd": hold_metrics["mdd"],
                    "excess_total_return": (
                        strategy_metrics["total_return"] - hold_metrics["total_return"]
                    ),
                }
            )
    return pd.DataFrame(rows)


def leave_one_cycle_out_metrics(
    features: pd.DataFrame,
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
    specs: Sequence[CandidateSpec],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    epochs = pd.to_numeric(features["halving_epoch"], errors="coerce")
    valid_epochs = [
        epoch
        for epoch in sorted(epochs.dropna().unique())
        if int(epochs.eq(epoch).sum()) >= 90
    ]
    for epoch in valid_epochs:
        test_mask = epochs.eq(epoch)
        train_mask = epochs.notna() & ~test_mask
        selected_id, train_metrics = _select_training_candidate(
            outputs, specs, train_mask
        )
        if selected_id == "cash":
            test_metrics = _period_metrics(
                pd.Series(0.0, index=features.index[test_mask])
            )
        else:
            test_metrics = _simulation_slice_metrics(outputs[selected_id][1], test_mask)
        rows.append(
            {
                "held_out_epoch": int(epoch),
                "selected_candidate": selected_id,
                "train_days": int(train_mask.sum()),
                "test_days": int(test_mask.sum()),
                "train_cagr": np.nan
                if train_metrics is None
                else train_metrics["cagr"],
                "train_mdd": np.nan if train_metrics is None else train_metrics["mdd"],
                "train_calmar": np.nan
                if train_metrics is None
                else train_metrics["calmar"],
                "test_total_return": test_metrics["total_return"],
                "test_cagr": test_metrics["cagr"],
                "test_mdd": test_metrics["mdd"],
                "test_calmar": test_metrics["calmar"],
                "note": "cross-cycle robustness; not chronological deployment evidence",
            }
        )
    return pd.DataFrame(rows)


def robustness_sensitivity(
    features: pd.DataFrame,
    specs: Sequence[CandidateSpec],
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_id = {spec.candidate_id: spec for spec in specs}
    scenarios = [
        ("zero_cost", 0.0, 0.0, 0),
        ("base_cost", None, None, 0),
        ("double_cost", None, None, 0),
        ("delay_plus_1", None, None, 1),
        ("delay_plus_2", None, None, 2),
    ]
    for candidate_id in ("baseline_core_tactical", "bear_reentry_core_cap"):
        spec = by_id[candidate_id]
        signals = outputs[candidate_id][0]
        for scenario, fee_override, slippage_override, delay in scenarios:
            if scenario == "base_cost":
                fee = spec.fee_bps
                slippage = spec.slippage_bps
            elif scenario == "double_cost":
                fee = spec.fee_bps * 2
                slippage = spec.slippage_bps * 2
            else:
                fee = spec.fee_bps if fee_override is None else fee_override
                slippage = (
                    spec.slippage_bps
                    if slippage_override is None
                    else slippage_override
                )
            simulation = simulate_strategy(
                features,
                signals,
                fee_bps=fee,
                slippage_bps=slippage,
                additional_delay_days=delay,
            )
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "scenario": scenario,
                    "fee_bps": fee,
                    "slippage_bps": slippage,
                    "additional_delay_days": delay,
                    **performance_metrics(simulation),
                }
            )
    return pd.DataFrame(rows)


def bear_cap_parameter_sensitivity(
    features: pd.DataFrame,
    *,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    parameters = ResearchParameters()
    for cap in (0.20, 0.25, 0.30):
        signals = generate_state_targets(
            features,
            parameters,
            sell_policy="core_tactical",
            use_halving=True,
            bear_market_max_weight=cap,
        )
        simulation = simulate_strategy(
            features,
            signals,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        rows.append(
            {
                "bear_market_max_weight": cap,
                **performance_metrics(simulation),
            }
        )
    return pd.DataFrame(rows)


def candidate_specs(config: Mapping[str, Any]) -> list[CandidateSpec]:
    runtime = config.get("btc", {}).get("research_runtime", {})
    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    base = ResearchParameters()
    return [
        CandidateSpec(
            "baseline_core_tactical", "core_tactical", True, base, fee_bps, slippage_bps
        ),
        CandidateSpec("sell_tiered", "tiered", True, base, fee_bps, slippage_bps),
        CandidateSpec("sell_all_out", "all_out", True, base, fee_bps, slippage_bps),
        CandidateSpec(
            "halving_ablation",
            "core_tactical",
            False,
            base,
            fee_bps,
            slippage_bps,
            selection_eligible=False,
        ),
        CandidateSpec(
            "deep_drawdown_minus_50",
            "core_tactical",
            True,
            replace(base, deep_drawdown_365=-0.50),
            fee_bps,
            slippage_bps,
        ),
        CandidateSpec(
            "entry_domains_4",
            "core_tactical",
            True,
            replace(base, required_entry_domains=4),
            fee_bps,
            slippage_bps,
        ),
        CandidateSpec(
            "overheat_percentile_95",
            "core_tactical",
            True,
            replace(base, overheat_percentile_min=0.95),
            fee_bps,
            slippage_bps,
        ),
        CandidateSpec(
            "cost_stress_50bps",
            "core_tactical",
            True,
            base,
            fee_bps,
            50.0,
            selection_eligible=False,
        ),
        CandidateSpec(
            "bear_reentry_core_cap",
            "core_tactical",
            True,
            base,
            fee_bps,
            slippage_bps,
            bear_market_max_weight=base.core_weight,
            selection_eligible=False,
            research_origin="post-2021-2022-drawdown-diagnostic",
        ),
    ]


def evaluate_candidates(
    features: pd.DataFrame,
    specs: Sequence[CandidateSpec],
) -> tuple[pd.DataFrame, dict[str, tuple[pd.DataFrame, SimulationResult]]]:
    rows: list[dict[str, Any]] = []
    outputs: dict[str, tuple[pd.DataFrame, SimulationResult]] = {}
    for spec in specs:
        signals = generate_state_targets(
            features,
            spec.parameters,
            sell_policy=spec.sell_policy,
            use_halving=spec.use_halving,
            bear_market_max_weight=spec.bear_market_max_weight,
        )
        simulation = simulate_strategy(
            features,
            signals,
            fee_bps=spec.fee_bps,
            slippage_bps=spec.slippage_bps,
            additional_delay_days=spec.additional_delay_days,
        )
        metrics = performance_metrics(simulation)
        rows.append(
            {
                "candidate_id": spec.candidate_id,
                "sell_policy": spec.sell_policy,
                "use_halving": spec.use_halving,
                "fee_bps": spec.fee_bps,
                "slippage_bps": spec.slippage_bps,
                "deep_drawdown_365": spec.parameters.deep_drawdown_365,
                "required_entry_domains": spec.parameters.required_entry_domains,
                "overheat_percentile_min": spec.parameters.overheat_percentile_min,
                "bear_market_max_weight": spec.bear_market_max_weight,
                "selection_eligible": spec.selection_eligible,
                "research_origin": spec.research_origin,
                **metrics,
            }
        )
        outputs[spec.candidate_id] = (signals, simulation)
    return pd.DataFrame(rows), outputs


def _frame_hash(frame: pd.DataFrame) -> str:
    normalized = frame.to_csv(index=True, float_format="%.12g", na_rep="NA").encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(normalized).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_number(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):.{digits}f}"


def build_research_report(
    candidates: pd.DataFrame,
    benchmarks: pd.DataFrame,
    walk_forward: pd.DataFrame,
    cycles: pd.DataFrame,
    robustness: pd.DataFrame,
    parameter_sensitivity: pd.DataFrame,
    manifest: Mapping[str, Any],
) -> str:
    baseline = candidates.loc[
        candidates["candidate_id"] == "baseline_core_tactical"
    ].iloc[0]
    challenger = candidates.loc[
        candidates["candidate_id"] == "bear_reentry_core_cap"
    ].iloc[0]
    passed = candidates.loc[
        candidates["mdd_gate_pass"].astype(bool), "candidate_id"
    ].tolist()
    lines = [
        "# Quant Guardian BTC 연구 보고서",
        "",
        f"생성시각: {manifest['generated_at_utc']}",
        f"전략버전: {manifest['strategy_version']}",
        "",
        "> 연구·모의운영 전용입니다. 자동주문과 실전 승인 신호가 아닙니다.",
        "",
        "## 현재 결론",
        f"- 고정 기준안: CAGR {_fmt_pct(baseline['cagr'])}, MDD {_fmt_pct(baseline['mdd'])}로 -50% 경계를 통과하지 못했습니다.",
        f"- 약세장 재진입 상한 후보: CAGR {_fmt_pct(challenger['cagr'])}, MDD {_fmt_pct(challenger['mdd'])}입니다.",
        f"- 전체기간 MDD 경계 통과 후보: {', '.join(passed) if passed else '없음'}",
        "- 약세장 상한 후보는 2021~2022 낙폭을 본 뒤 만든 사후 진단 규칙이라 자동선택과 실전승인에서 제외했습니다.",
        "- 결론: 연구 코드는 유지하되, 전략 승인은 보류하고 새로운 확정 일봉으로 전향 검증해야 합니다.",
        "",
        "## 고정 기준안 상세",
        f"- CAGR: {_fmt_pct(baseline['cagr'])}",
        f"- MDD: {_fmt_pct(baseline['mdd'])}",
        f"- Calmar: {float(baseline['calmar']):.3f}",
        f"- 거래 횟수: {int(baseline['trades'])}",
        f"- 평균 BTC 노출: {_fmt_pct(baseline['exposure'])}",
        f"- MDD -50% 연구 경계 통과: {'예' if baseline['mdd_gate_pass'] else '아니오'}",
        "",
        "## 필수 벤치마크",
        "",
        "| 기준 | CAGR/TWR | MDD | Calmar | 거래 | 자본 대비 말기자산 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in benchmarks.iterrows():
        lines.append(
            f"| {row['benchmark_id']} | {_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_number(row['calmar'])} | {int(row['trades'])} | "
            f"{float(row['wealth_to_contribution']):.3f}x |"
        )
    lines.extend(
        [
            "",
            "월 적립식은 외부 납입을 제거한 TWR과 총 납입액 대비 말기자산을 표시하므로, 일시납 말기자산과 직접 비교하지 않습니다.",
            "",
            "## 후보 비교",
            "",
            "| 후보 | CAGR | MDD | Calmar | 거래 | MDD 경계 | 선택대상 |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for _, row in candidates.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_number(row['calmar'])} | {int(row['trades'])} | "
            f"{'통과' if row['mdd_gate_pass'] else '실패'} | "
            f"{'예' if row['selection_eligible'] else '아니오'} |"
        )
    lines.extend(
        [
            "",
            "후보는 전체 조합이 아니라 한 번에 한 가정만 바꾼 연구 목록입니다. 최고 CAGR 후보를 자동 채택하지 않습니다.",
            "",
            "## Anchored Walk-forward",
            "",
            "각 연도 선택에는 그 연도 이전 데이터만 사용했습니다. 사후 진단 후보는 선택대상에서 제외했습니다.",
            "",
            "| 시험연도 | 훈련종료 | 선택후보 | 시험수익 | 시험MDD | BTC 수익 |",
            "|---:|---|---|---:|---:|---:|",
        ]
    )
    for _, row in walk_forward.iterrows():
        lines.append(
            f"| {int(row['test_year'])} | {row['train_end']} | {row['selected_candidate']} | "
            f"{_fmt_pct(row['strategy_total_return'])} | {_fmt_pct(row['strategy_mdd'])} | "
            f"{_fmt_pct(row['buy_hold_total_return'])} |"
        )
    lines.extend(
        [
            "",
            "## 반감기 사이클 비교",
            "",
            "| 후보 | Epoch | 기간 | CAGR | MDD | BTC 수익 대비 |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    cycle_view = cycles.loc[
        cycles["candidate_id"].isin(["baseline_core_tactical", "bear_reentry_core_cap"])
    ]
    for _, row in cycle_view.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {int(row['halving_epoch'])} | {row['start']}~{row['end']} | "
            f"{_fmt_pct(row['strategy_cagr'])} | {_fmt_pct(row['strategy_mdd'])} | "
            f"{_fmt_pct(row['excess_total_return'])} |"
        )
    lines.extend(
        [
            "",
            "## 비용·체결지연 강건성",
            "",
            "| 후보 | 조건 | CAGR | MDD | 거래 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for _, row in robustness.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {row['scenario']} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 약세장 상한 인접값",
            "",
            "| 약세장 최대비중 | CAGR | MDD | Calmar | 거래 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in parameter_sensitivity.iterrows():
        lines.append(
            f"| {_fmt_pct(row['bear_market_max_weight'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_number(row['calmar'])} | {int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 검증 범위와 한계",
            f"- Walk-forward 시험 구간: {len(walk_forward)}개",
            f"- 후보별 반감기 epoch 행: {len(cycles)}개",
            f"- 온체인 공개 지연: {manifest['onchain_lag_days']}일",
            f"- 체결: 신호 다음 일봉 시가 + {manifest['additional_delay_days']}일 추가지연",
            f"- 비용: 수수료 {manifest['fee_bps']}bps + 기본 슬리피지 {manifest['slippage_bps']}bps",
            "- Upbit KRW 실행기간만 사용했으며 2012년 이후 USD 확장 보고서는 아직 없습니다.",
            "- 사후 진단 후보는 과거 데이터에 대한 순수 아웃오브샘플 증거가 될 수 없습니다.",
            "",
            "## 아직 승인하지 않는 이유",
            "- 고정 기준안은 MDD 경계를 실패했습니다.",
            "- 경계를 통과한 후보는 사후 진단에서 만들어져 전향 데이터 검증이 필요합니다.",
            "- 독립 USD 현물 공급자와 2012년 이후 extended-cycle 검증이 남아 있습니다.",
            "- 30개 확정 일봉 Shadow와 사용자 최종 승인이 남아 있다.",
        ]
    )
    return "\n".join(lines)


def run_research(
    *,
    refresh: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow" or bool(
        btc_cfg.get("auto_order", False)
    ):
        raise BtcDataError(
            "BTC research requires shadow mode with automatic orders disabled"
        )
    runtime = btc_cfg.get("research_runtime", {})
    strategy_version = str(runtime.get("strategy_version", STRATEGY_VERSION))
    paths = resolve_paths(config)

    phase1 = build_phase1_report(refresh=refresh, config_path=config_path, now_utc=now)
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 critical data gate is blocked")
    onchain, onchain_fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh,
        config=config,
        now_utc=now,
    )

    market = str(btc_cfg.get("execution_market", "KRW-BTC"))
    data_cfg = btc_cfg.get("data", {})
    upbit = read_price_cache(
        paths.cache / f"upbit_{market.replace('-', '_')}_daily.csv"
    )
    yahoo_cache = ROOT / config.get("settings", {}).get("cache_dir", "data/cache")
    usd = read_price_cache(
        yahoo_cache / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))
    )
    fx = read_price_cache(
        yahoo_cache / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))
    )
    usd = closed_yahoo_daily_frame(usd, now)
    fx = closed_yahoo_daily_frame(fx, now)

    onchain_lag_days = int(runtime.get("onchain_lag_days", 2))
    percentile_min_periods = int(runtime.get("percentile_min_periods", 365))
    features = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    if int(features["feature_ready"].sum()) < 730:
        raise BtcDataError("Feature frame has fewer than 730 research-ready daily rows")
    live_tip = int(phase1["halving"]["tip_height"])
    historical_tip = int(features["block_height"].dropna().iloc[-1])
    if abs(live_tip - historical_tip) > 300:
        raise BtcDataError(
            f"Historical block-height reconstruction differs from live tip by {abs(live_tip - historical_tip)}"
        )
    features = trim_to_research_window(features)

    specs = candidate_specs(config)
    candidates, outputs = evaluate_candidates(features, specs)
    baseline_signals, baseline_simulation = outputs["baseline_core_tactical"]
    challenger_signals, challenger_simulation = outputs["bear_reentry_core_cap"]
    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    benchmarks, benchmark_outputs = evaluate_benchmarks(
        features,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    buy_hold = benchmark_outputs["buy_hold"]
    walk_forward = anchored_walk_forward_metrics(
        features,
        outputs,
        specs,
        buy_hold,
        minimum_train_days=int(runtime.get("minimum_train_days", 730)),
    )
    years = candidate_year_metrics(features, outputs)
    cycles = candidate_cycle_metrics(features, outputs, buy_hold)
    leave_one_cycle = leave_one_cycle_out_metrics(features, outputs, specs)
    robustness = robustness_sensitivity(features, specs, outputs)
    parameter_sensitivity = bear_cap_parameter_sensitivity(
        features,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )

    feature_path = paths.output / "btc_feature_frame.csv"
    candidate_path = paths.output / "btc_strategy_candidates.csv"
    benchmark_path = paths.output / "btc_benchmarks.csv"
    signal_path = paths.output / "btc_signal_history.csv"
    challenger_signal_path = paths.output / "btc_challenger_signal_history.csv"
    equity_path = paths.output / "btc_equity_curves.csv"
    trade_path = paths.output / "btc_trade_ledger.csv"
    walk_path = paths.output / "btc_walk_forward_metrics.csv"
    year_path = paths.output / "btc_candidate_year_metrics.csv"
    cycle_path = paths.output / "btc_cycle_metrics.csv"
    leave_cycle_path = paths.output / "btc_leave_one_cycle_out.csv"
    robustness_path = paths.output / "btc_robustness_sensitivity.csv"
    parameter_path = paths.output / "btc_parameter_sensitivity.csv"
    manifest_path = paths.output / "btc_experiment_manifest.json"
    report_path = paths.output / "btc_research_report.md"

    features.to_csv(feature_path, encoding="utf-8-sig")
    candidates.to_csv(candidate_path, index=False, encoding="utf-8-sig")
    benchmarks.to_csv(benchmark_path, index=False, encoding="utf-8-sig")
    baseline_signals.to_csv(signal_path, encoding="utf-8-sig")
    challenger_signals.to_csv(challenger_signal_path, encoding="utf-8-sig")
    equity = pd.DataFrame(
        {
            "baseline_equity": baseline_simulation.daily["equity"],
            "challenger_equity": challenger_simulation.daily["equity"],
            "buy_hold_equity": buy_hold.daily["equity"],
            "baseline_drawdown": (
                baseline_simulation.daily["equity"]
                / baseline_simulation.daily["equity"].cummax()
                - 1
            ),
            "challenger_drawdown": (
                challenger_simulation.daily["equity"]
                / challenger_simulation.daily["equity"].cummax()
                - 1
            ),
            "buy_hold_drawdown": buy_hold.daily["equity"]
            / buy_hold.daily["equity"].cummax()
            - 1,
        }
    )
    equity.to_csv(equity_path, encoding="utf-8-sig")
    trade_frames: list[pd.DataFrame] = []
    for candidate_id, (_, simulation) in outputs.items():
        if simulation.trades.empty:
            continue
        candidate_trades = simulation.trades.copy()
        candidate_trades.insert(0, "candidate_id", candidate_id)
        trade_frames.append(candidate_trades)
    pd.concat(trade_frames, ignore_index=True).to_csv(
        trade_path, index=False, encoding="utf-8-sig"
    )
    walk_forward.to_csv(walk_path, index=False, encoding="utf-8-sig")
    years.to_csv(year_path, index=False, encoding="utf-8-sig")
    cycles.to_csv(cycle_path, index=False, encoding="utf-8-sig")
    leave_one_cycle.to_csv(leave_cycle_path, index=False, encoding="utf-8-sig")
    robustness.to_csv(robustness_path, index=False, encoding="utf-8-sig")
    parameter_sensitivity.to_csv(parameter_path, index=False, encoding="utf-8-sig")

    manifest = {
        "schema_version": "btc-research-0.1",
        "strategy_version": strategy_version,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "approved_strategy": None,
        "candidate_selection": "none-for-deployment",
        "walk_forward_selection_rule": "train MDD >= -50%, then Calmar/CAGR/turnover",
        "full_cartesian_product": False,
        "candidate_count": len(specs),
        "candidate_ids": [spec.candidate_id for spec in specs],
        "candidate_metadata": [
            {
                "candidate_id": spec.candidate_id,
                "selection_eligible": spec.selection_eligible,
                "research_origin": spec.research_origin,
            }
            for spec in specs
        ],
        "data_start": features.index.min().date().isoformat(),
        "data_end": features.index.max().date().isoformat(),
        "feature_ready_rows": int(features["feature_ready"].sum()),
        "feature_frame_hash": _frame_hash(features),
        "onchain_lag_days": onchain_lag_days,
        "onchain_cache_fallback": onchain_fallback,
        "onchain_cache_error": onchain_error,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "additional_delay_days": 0,
        "execution_rule": "closed UTC signal, next Upbit daily open",
        "lookahead_guards": [
            "current UTC Yahoo candle excluded",
            "FX label delayed one day",
            f"on-chain values delayed {onchain_lag_days} days",
            "expanding percentile excludes current observation",
            "signal executes on next daily open",
        ],
        "limitations": [
            "the bear re-entry challenger was created after inspecting the 2021-2022 drawdown",
            "retrospective walk-forward cannot make a post-diagnostic rule pristine out-of-sample",
            "Upbit execution period only",
            "extended USD history and independent spot-provider consensus remain future work",
            "no live or advisory approval",
        ],
    }
    _write_json(manifest_path, manifest)
    report_text = build_research_report(
        candidates,
        benchmarks,
        walk_forward,
        cycles,
        robustness,
        parameter_sensitivity,
        manifest,
    )
    report_path.write_text(report_text, encoding="utf-8-sig")

    baseline_metrics = performance_metrics(baseline_simulation)
    challenger_metrics = performance_metrics(challenger_simulation)
    buy_hold_metrics = performance_metrics(buy_hold)
    return {
        "manifest": manifest,
        "baseline_metrics": baseline_metrics,
        "challenger_metrics": challenger_metrics,
        "buy_hold_metrics": buy_hold_metrics,
        "current_state": baseline_signals.iloc[-1].to_dict(),
        "outputs": {
            "features": str(feature_path),
            "candidates": str(candidate_path),
            "benchmarks": str(benchmark_path),
            "signals": str(signal_path),
            "challenger_signals": str(challenger_signal_path),
            "equity": str(equity_path),
            "trades": str(trade_path),
            "walk_forward": str(walk_path),
            "years": str(year_path),
            "cycles": str(cycle_path),
            "leave_one_cycle_out": str(leave_cycle_path),
            "robustness": str(robustness_path),
            "parameter_sensitivity": str(parameter_path),
            "manifest": str(manifest_path),
            "report": str(report_path),
        },
    }


def print_summary(result: Mapping[str, Any]) -> None:
    strategy = result["baseline_metrics"]
    challenger = result["challenger_metrics"]
    buy_hold = result["buy_hold_metrics"]
    current = result["current_state"]
    print("Quant Guardian BTC Phase 2 연구 결과")
    print(
        f"고정 기준안 CAGR/MDD: {_fmt_pct(strategy['cagr'])} / {_fmt_pct(strategy['mdd'])} (경계 실패)"
    )
    print(
        "사후 진단 후보 CAGR/MDD: "
        f"{_fmt_pct(challenger['cagr'])} / {_fmt_pct(challenger['mdd'])} "
        "(실전 선택 제외)"
    )
    print(
        f"BTC 보유 CAGR/MDD: {_fmt_pct(buy_hold['cagr'])} / {_fmt_pct(buy_hold['mdd'])}"
    )
    print(
        f"기준안의 마지막 참고상태: {current['state']} / "
        f"연구비중 {float(current['target_weight']) * 100:.0f}% (매매지시 아님)"
    )
    print(f"비교 후보: {result['manifest']['candidate_count']}개, 자동 채택: 없음")
    print("자동주문과 실전 승인 없음. 결과는 연구·모의운영 전용입니다.")
    print(f"보고서: {result['outputs']['report']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant Guardian BTC research engine")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser(
        "run", help="Build the BTC candidate and robustness report"
    )
    run.add_argument("--refresh", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_research(refresh=args.refresh, config_path=Path(args.config))
            print_summary(result)
            return 0
    except (BtcDataError, ValueError, OSError) as exc:
        print(f"BTC 연구 실행 실패: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
