from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import textwrap
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
USER_AGENT = "quant-guardian-index/1.0 research-tool"
KST = ZoneInfo("Asia/Seoul")

ACTION_LABELS = {
    "buy": "신규매수 검토",
    "accumulate": "분할매수",
    "hold": "보유·관찰",
    "trim": "비중축소",
    "exit": "매도·대기",
}


@dataclass(frozen=True)
class Paths:
    root: Path
    cache: Path
    output: Path


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_paths(cfg: dict) -> Paths:
    cache = ROOT / cfg["settings"].get("cache_dir", "data/cache")
    output = ROOT / cfg["settings"].get("output_dir", "output")
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    return Paths(root=ROOT, cache=cache, output=output)


def yahoo_symbol(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def cache_key(source: str, ticker: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_=-]+", "_", yahoo_symbol(ticker))
    return f"{source}_{safe}.csv"


def fetch_text(url: str, retries: int = 2, pause: float = 0.5) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=25) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(pause)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def fetch_yahoo_price(ticker: str) -> pd.DataFrame:
    symbol = yahoo_symbol(ticker)
    period2 = int((datetime.now(UTC) + timedelta(days=2)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        f"?period1=0&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    payload = json.loads(fetch_text(url))
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo error for {ticker}: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"No Yahoo data for {ticker}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    raw_close = quote_data.get("close") or []
    adjusted = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or raw_close
    if not timestamps or not raw_close:
        raise RuntimeError(f"Yahoo response missing prices for {ticker}")

    raw = pd.to_numeric(pd.Series(raw_close), errors="coerce")
    adj = pd.to_numeric(pd.Series(adjusted), errors="coerce")
    factor = (adj / raw.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)

    def adjusted_column(name: str) -> pd.Series:
        values = quote_data.get(name) or [np.nan] * len(timestamps)
        return pd.to_numeric(pd.Series(values), errors="coerce") * factor

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(timestamps, unit="s").tz_localize("UTC").tz_convert(None).date,
            "Open": adjusted_column("open"),
            "High": adjusted_column("high"),
            "Low": adjusted_column("low"),
            "Close": adj,
            "Volume": quote_data.get("volume"),
        }
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"]).sort_values("Date")
    if frame.empty:
        raise RuntimeError(f"Yahoo returned empty prices for {ticker}")
    return frame.set_index("Date")


def read_price(ticker: str, paths: Paths, refresh: bool = False, source: str = "yahoo") -> pd.DataFrame:
    source = source.lower().strip()
    cache_file = paths.cache / cache_key(source, ticker)
    if refresh or not cache_file.exists():
        if not refresh and not cache_file.exists():
            raise RuntimeError(f"No cached data for {ticker}; run with --refresh first")
        if source != "yahoo":
            raise RuntimeError(f"Unsupported data source: {source}")
        fetch_yahoo_price(ticker).to_csv(cache_file, encoding="utf-8")
    frame = pd.read_csv(cache_file)
    if frame.empty or "Date" not in frame or "Close" not in frame:
        raise RuntimeError(f"Cached data is invalid for {ticker}: {cache_file}")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date", "Close"]).sort_values("Date").set_index("Date")
    for column in ["Open", "High", "Low", "Close", "Volume"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def price_panel(tickers: Iterable[str], paths: Paths, refresh: bool = False, source: str = "yahoo") -> pd.DataFrame:
    series: dict[str, pd.Series] = {}
    errors: list[str] = []
    for ticker in sorted(set(tickers)):
        try:
            frame = read_price(ticker, paths, refresh=refresh, source=source)
            series[ticker.upper()] = frame["Close"].rename(ticker.upper())
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")
    if not series:
        raise RuntimeError("No price data loaded. " + " | ".join(errors[:5]))
    panel = pd.concat(series.values(), axis=1).sort_index().ffill()
    panel.attrs["errors"] = errors
    return panel


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(value: float | int | np.floating | None) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value) * 100:,.2f}%"


def num(value: float | int | np.floating | None) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):,.3f}"


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous = close.shift(1)
    return pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / window, min_periods=window, adjust=False).mean()


def directional_index(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)
    range_avg = atr(high, low, close, window)
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / range_avg.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, min_periods=window, adjust=False).mean() / range_avg.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_value = dx.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    return adx_value, plus_di, minus_di


def max_drawdown(returns: pd.Series) -> float:
    curve = (1 + returns.fillna(0)).cumprod()
    return float((curve / curve.cummax() - 1).min())


def annualized_metrics(returns: pd.Series, periods_per_year: int = 12) -> dict:
    returns = returns.dropna()
    if returns.empty:
        return {
            "cagr": np.nan,
            "mdd": np.nan,
            "sharpe": np.nan,
            "sortino": np.nan,
            "calmar": np.nan,
            "win_rate": np.nan,
            "total_return": np.nan,
        }
    years = len(returns) / periods_per_year
    total = float((1 + returns).prod() - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    volatility = returns.std(ddof=0) * math.sqrt(periods_per_year)
    sharpe = (returns.mean() * periods_per_year) / volatility if volatility > 0 else np.nan
    downside = returns[returns < 0].std(ddof=0) * math.sqrt(periods_per_year)
    sortino = (returns.mean() * periods_per_year) / downside if downside > 0 else np.nan
    drawdown = max_drawdown(returns)
    calmar = cagr / abs(drawdown) if drawdown < 0 else np.nan
    return {
        "cagr": float(cagr),
        "mdd": float(drawdown),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "win_rate": float((returns > 0).mean()),
        "total_return": total,
    }


def _last(series: pd.Series) -> float:
    value = series.iloc[-1]
    return float(value) if pd.notna(value) else np.nan


def _period_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return np.nan
    return float(close.iloc[-1] / close.iloc[-days - 1] - 1)


def technical_snapshot(
    ticker: str,
    price: pd.DataFrame,
    benchmark_close: pd.Series | None = None,
    vix_value: float | None = None,
) -> dict:
    frame = price.copy().sort_index()
    close = frame["Close"].dropna()
    if len(close) < 260:
        return {"ticker": ticker.upper(), "error": "not enough data"}
    frame = frame.reindex(close.index)
    high = frame["High"].fillna(close) if "High" in frame else close
    low = frame["Low"].fillna(close) if "Low" in frame else close
    volume = frame["Volume"].fillna(0) if "Volume" in frame else pd.Series(0.0, index=close.index)

    last = float(close.iloc[-1])
    sma20_series = close.rolling(20).mean()
    sma50_series = close.rolling(50).mean()
    sma100_series = close.rolling(100).mean()
    sma200_series = close.rolling(200).mean()
    sma20, sma50, sma100, sma200 = map(_last, [sma20_series, sma50_series, sma100_series, sma200_series])
    ema12_series = close.ewm(span=12, adjust=False).mean()
    ema26_series = close.ewm(span=26, adjust=False).mean()
    macd_series = ema12_series - ema26_series
    macd_signal_series = macd_series.ewm(span=9, adjust=False).mean()
    macd_value = _last(macd_series)
    macd_signal = _last(macd_signal_series)
    macd_hist = macd_value - macd_signal
    rsi14 = _last(rsi(close, 14))

    rolling_high14 = high.rolling(14).max()
    rolling_low14 = low.rolling(14).min()
    stochastic_k_series = 100 * (close - rolling_low14) / (rolling_high14 - rolling_low14).replace(0, np.nan)
    stochastic_d_series = stochastic_k_series.rolling(3).mean()
    stochastic_k = _last(stochastic_k_series)
    stochastic_d = _last(stochastic_d_series)

    bb_middle = sma20_series
    bb_std = close.rolling(20).std(ddof=0)
    bb_upper = bb_middle + 2 * bb_std
    bb_lower = bb_middle - 2 * bb_std
    bb_percent = float((last - _last(bb_lower)) / max(_last(bb_upper) - _last(bb_lower), 1e-12))

    atr14 = _last(atr(high, low, close, 14))
    atr_pct = atr14 / last
    adx_series, plus_di_series, minus_di_series = directional_index(high, low, close, 14)
    adx14 = _last(adx_series)
    plus_di = _last(plus_di_series)
    minus_di = _last(minus_di_series)

    tenkan_series = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun_series = (high.rolling(26).max() + low.rolling(26).min()) / 2
    span_a_series = ((tenkan_series + kijun_series) / 2).shift(26)
    span_b_series = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    tenkan, kijun = _last(tenkan_series), _last(kijun_series)
    span_a, span_b = _last(span_a_series), _last(span_b_series)
    cloud_top, cloud_bottom = max(span_a, span_b), min(span_a, span_b)

    donchian20 = _last(high.shift(1).rolling(20).max())
    donchian55 = _last(high.shift(1).rolling(55).max())
    volume20 = _last(volume.rolling(20).mean())
    volume60 = _last(volume.rolling(60).mean())
    volume_ratio = volume20 / volume60 if volume60 > 0 else np.nan
    obv = (np.sign(close.diff()).fillna(0) * volume).cumsum()
    obv_rising = bool(_last(obv) > _last(obv.rolling(20).mean())) if volume60 > 0 else False

    ret_1m = _period_return(close, 21)
    ret_3m = _period_return(close, 63)
    ret_6m = _period_return(close, 126)
    ret_12m = _period_return(close, 252)
    mom_12_1 = float(close.iloc[-22] / close.iloc[-253] - 1)
    relative_6m = 0.0
    if benchmark_close is not None:
        benchmark = benchmark_close.dropna().loc[: close.index[-1]]
        benchmark_6m = _period_return(benchmark, 126)
        if pd.notna(benchmark_6m):
            relative_6m = ret_6m - benchmark_6m
    daily_returns = close.pct_change().dropna()
    volatility63 = float(daily_returns.tail(63).std(ddof=0) * math.sqrt(252))
    drawdown252 = float((close.tail(252) / close.tail(252).cummax() - 1).min())
    distance_sma20_atr = (last - sma20) / atr14 if atr14 > 0 else np.nan

    trend_score = 0.0
    trend_score += 10 if last > sma200 else 0
    trend_score += 6 if sma50 > sma200 else 0
    trend_score += 4 if last > sma50 else 0
    trend_score += 4 if sma20 > sma50 else 0
    trend_score += 4 if _last(ema12_series) > _last(ema26_series) else 0
    trend_score += 4 if macd_value > macd_signal else 0
    trend_score += 5 if last > cloud_top else 2 if last >= cloud_bottom else 0
    trend_score += 2 if tenkan > kijun else 0
    trend_score += 1 if adx14 >= 20 and plus_di > minus_di else 0

    momentum_score = (
        10 * clamp((mom_12_1 + 0.05) / 0.50, 0, 1)
        + 6 * clamp((ret_6m + 0.03) / 0.35, 0, 1)
        + 4 * clamp((ret_3m + 0.02) / 0.22, 0, 1)
        + 3 * clamp((relative_6m + 0.05) / 0.20, 0, 1)
        + 2 * int(ret_1m > 0)
    )

    if 45 <= rsi14 <= 65:
        rsi_points = 6
    elif 35 <= rsi14 < 45 or 65 < rsi14 <= 72:
        rsi_points = 4
    elif rsi14 < 35:
        rsi_points = 3
    else:
        rsi_points = 1
    if 0.30 <= bb_percent <= 0.85:
        bb_points = 4
    elif 0.10 <= bb_percent < 0.30:
        bb_points = 3
    elif 0.85 < bb_percent <= 1.00:
        bb_points = 2
    else:
        bb_points = 1
    if -0.50 <= distance_sma20_atr <= 1.50:
        distance_points = 4
    elif -1.50 <= distance_sma20_atr < -0.50:
        distance_points = 3
    elif 1.50 < distance_sma20_atr <= 2.50:
        distance_points = 2
    else:
        distance_points = 1
    if 30 <= stochastic_k <= 80:
        stochastic_points = 3
    elif stochastic_k < 30:
        stochastic_points = 2
    else:
        stochastic_points = 1
    breakout = bool(last >= donchian20 and (pd.isna(volume_ratio) or volume_ratio >= 1.0))
    volume_points = 3 if breakout else 2 if obv_rising else 1
    timing_score = float(rsi_points + bb_points + distance_points + stochastic_points + volume_points)

    risk_score = (
        6 * clamp((0.55 - volatility63) / 0.45, 0, 1)
        + 4 * clamp((drawdown252 + 0.35) / 0.35, 0, 1)
        + 3 * clamp((0.055 - atr_pct) / 0.045, 0, 1)
    )
    if vix_value is None or pd.isna(vix_value):
        risk_score += 1
    elif vix_value < 20:
        risk_score += 2
    elif vix_value < 30:
        risk_score += 1

    score = clamp(trend_score + momentum_score + timing_score + risk_score, 0, 100)
    strong_trend = bool(last > sma200 and sma50 > sma200 and last > cloud_top)
    weak_trend = bool(last < sma200 and last < cloud_bottom)
    overheat_count = sum(
        [rsi14 > 72, bb_percent > 1.05, distance_sma20_atr > 2.5, stochastic_k > 90]
    )
    overheated = bool(overheat_count >= 2 or rsi14 > 78 or distance_sma20_atr > 3.5)
    pullback_zone = bool(
        strong_trend
        and (abs(last / sma20 - 1) <= 0.02 or (45 <= rsi14 <= 60 and distance_sma20_atr <= 1.0))
    )

    if weak_trend and score < 40:
        action = "매도·대기"
        tone = "bad"
        timing = "신규매수를 멈추고, 주봉 종가가 200일선과 구름대 위로 회복할 때 다시 확인합니다."
    elif score < 52 or last < sma200:
        action = "비중축소"
        tone = "bad"
        timing = "신규매수는 중단합니다. 다음 주 종가도 200일선 아래면 목표 비중까지 나눠 줄입니다."
    elif strong_trend and overheated:
        action = "보유·추격매수 대기"
        tone = "warn"
        timing = "보유분은 유지하되 RSI 65 이하 또는 가격이 20일선 ±2% 안으로 올 때까지 추가매수를 기다립니다."
    elif strong_trend and score >= 75 and (pullback_zone or breakout):
        action = "신규매수"
        tone = "good"
        timing = "목표 금액을 한 번에 사지 말고 이번 주 2회로 나눕니다. 종가가 50일선 아래면 두 번째 매수는 보류합니다."
    elif strong_trend and score >= 65:
        action = "분할매수"
        tone = "good"
        timing = "목표 금액의 절반만 먼저 검토하고, 다음 종가가 20일선 위를 유지하면 나머지를 검토합니다."
    else:
        action = "보유·관찰"
        tone = "warn"
        timing = "기존 보유분은 유지합니다. 20일선 회복과 MACD 개선이 함께 확인될 때 추가매수를 검토합니다."

    positives: list[str] = []
    cautions: list[str] = []
    (positives if last > sma200 else cautions).append("200일선 위" if last > sma200 else "200일선 아래")
    (positives if last > cloud_top else cautions).append("일목 구름대 위" if last > cloud_top else "일목 구름대 안/아래")
    (positives if macd_value > macd_signal else cautions).append("MACD 상승" if macd_value > macd_signal else "MACD 약세")
    (positives if 45 <= rsi14 <= 65 else cautions).append(
        f"RSI {rsi14:.1f} 진입권" if 45 <= rsi14 <= 65 else f"RSI {rsi14:.1f} 추격 주의"
    )
    if adx14 >= 20 and plus_di > minus_di:
        positives.append("ADX 상승 추세 확인")
    elif adx14 >= 20 and plus_di <= minus_di:
        cautions.append("ADX 하락 방향 우세")
    else:
        cautions.append(f"ADX {adx14:.1f} 추세 강도 약함")
    if overheated:
        cautions.append("단기 과열로 추격매수 대기")

    return {
        "ticker": ticker.upper(),
        "as_of": close.index[-1].date().isoformat(),
        "last": last,
        "score": score,
        "action": action,
        "tone": tone,
        "timing": timing,
        "positives": positives,
        "cautions": cautions,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "timing_score": timing_score,
        "risk_score": risk_score,
        "sma20": sma20,
        "sma50": sma50,
        "sma100": sma100,
        "sma200": sma200,
        "above_200d": bool(last > sma200),
        "ema12": _last(ema12_series),
        "ema26": _last(ema26_series),
        "macd": macd_value,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "rsi14": rsi14,
        "stochastic_k": stochastic_k,
        "stochastic_d": stochastic_d,
        "bb_percent": bb_percent,
        "atr14": atr14,
        "atr_pct": atr_pct,
        "adx14": adx14,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "tenkan": tenkan,
        "kijun": kijun,
        "cloud_top": cloud_top,
        "cloud_bottom": cloud_bottom,
        "ichimoku_state": "구름대 위" if last > cloud_top else "구름대 아래" if last < cloud_bottom else "구름대 안",
        "donchian20": donchian20,
        "donchian55": donchian55,
        "breakout20": breakout,
        "volume_ratio": volume_ratio,
        "obv_rising": obv_rising,
        "ret_1m": ret_1m,
        "ret_3m": ret_3m,
        "ret_6m": ret_6m,
        "ret_12m": ret_12m,
        "mom_12_1": mom_12_1,
        "relative_6m": relative_6m,
        "vol63": volatility63,
        "drawdown_252d": drawdown252,
        "distance_sma20_atr": distance_sma20_atr,
        "strong_trend": strong_trend,
        "overheated": overheated,
    }


def execution_ticker(cfg: dict, signal_ticker: str) -> str:
    mapping = cfg.get("qg_core_execution_map", {})
    return str(mapping.get(signal_ticker.upper(), signal_ticker.upper()))


def _all_analysis_assets(cfg: dict) -> list[str]:
    core = cfg["qg_core"]
    market = cfg["market_timing"]
    return sorted(
        set(
            [
                *[str(item).upper() for item in core["equity_etfs"]],
                *[str(item).upper() for item in cfg.get("qg_core_execution_map", {}).values()],
                str(core.get("live_safety_asset", "SGOV")).upper(),
                str(core.get("backtest_safety_asset", "SHY")).upper(),
                str(core.get("gold_asset", "GLD")).upper(),
                str(core.get("bond_asset", "TLT")).upper(),
                str(market.get("vix_symbol", "^VIX")).upper(),
            ]
        )
    )


def _load_analysis_frames(cfg: dict, paths: Paths, refresh: bool) -> tuple[dict[str, pd.DataFrame], list[str]]:
    source = cfg["settings"].get("data_source", "yahoo")
    frames: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for ticker in _all_analysis_assets(cfg):
        try:
            frames[ticker] = read_price(ticker, paths, refresh=refresh, source=source)
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")
    return frames, errors


def qg_core_etf_scores(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    core = cfg["qg_core"]
    market = cfg["market_timing"]
    frames, errors = _load_analysis_frames(cfg, paths, refresh)
    benchmark_ticker = str(market.get("market_asset", "SPY")).upper()
    benchmark = frames.get(benchmark_ticker, pd.DataFrame()).get("Close")
    vix_ticker = str(market.get("vix_symbol", "^VIX")).upper()
    vix_frame = frames.get(vix_ticker)
    vix_value = float(vix_frame["Close"].dropna().iloc[-1]) if vix_frame is not None else None
    broad = {str(item).upper() for item in core.get("broad_etfs", [])}
    rows: list[dict] = []
    for ticker in [str(item).upper() for item in core["equity_etfs"]]:
        frame = frames.get(ticker)
        if frame is None:
            continue
        row = technical_snapshot(ticker, frame, benchmark_close=benchmark, vix_value=vix_value)
        if "error" not in row:
            row["execution_ticker"] = execution_ticker(cfg, ticker)
            row["role"] = "핵심 지수" if ticker in broad else "섹터 보조"
            rows.append(row)
    scores = pd.DataFrame(rows)
    if not scores.empty:
        scores = scores.sort_values(["score", "mom_12_1"], ascending=False).reset_index(drop=True)
    scores.to_csv(paths.output / "qg_core_etf_scores.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame({"error": errors}).to_csv(paths.output / "data_errors.csv", index=False, encoding="utf-8-sig")
    return scores


def _profile_from_market(spy: dict, qqq: dict, vix_value: float | None, cfg: dict) -> tuple[str, float]:
    market_cfg = cfg["market_timing"]
    score = 0.55 * float(spy.get("score", 0)) + 0.45 * float(qqq.get("score", 0))
    both_above = bool(spy.get("above_200d")) and bool(qqq.get("above_200d"))
    neither_above = not bool(spy.get("above_200d")) and not bool(qqq.get("above_200d"))
    if vix_value is not None and not pd.isna(vix_value) and vix_value >= float(market_cfg.get("high_vix", 30)):
        score -= 8
    if neither_above:
        score = min(score, 34)
    elif not both_above:
        score = min(score, 59)
    score = clamp(score, 0, 100)
    if score >= 75 and both_above:
        profile = "buy"
    elif score >= 62 and (bool(spy.get("above_200d")) or bool(qqq.get("above_200d"))):
        profile = "accumulate"
    elif score >= 50:
        profile = "hold"
    elif score >= 35:
        profile = "trim"
    else:
        profile = "exit"
    return profile, score


def market_regime(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    market = cfg["market_timing"]
    source = cfg["settings"].get("data_source", "yahoo")
    market_ticker = str(market.get("market_asset", "SPY")).upper()
    growth_ticker = str(market.get("growth_asset", "QQQ")).upper()
    vix_ticker = str(market.get("vix_symbol", "^VIX")).upper()
    spy_frame = read_price(market_ticker, paths, refresh=refresh, source=source)
    qqq_frame = read_price(growth_ticker, paths, refresh=refresh, source=source)
    vix_frame = read_price(vix_ticker, paths, refresh=refresh, source=source)
    vix_value = float(vix_frame["Close"].dropna().iloc[-1])
    spy = technical_snapshot(market_ticker, spy_frame, spy_frame["Close"], vix_value)
    qqq = technical_snapshot(growth_ticker, qqq_frame, spy_frame["Close"], vix_value)
    profile, score = _profile_from_market(spy, qqq, vix_value, cfg)
    allocation = qg_core_allocations(cfg, profile)
    reasons = [
        f"SPY {spy['score']:.1f}점·{spy['action']}",
        f"QQQ {qqq['score']:.1f}점·{qqq['action']}",
        f"VIX {vix_value:.1f}",
    ]
    return {
        "as_of": max(spy["as_of"], qqq["as_of"]),
        "score": score,
        "max_score": 100,
        "profile": profile,
        "action": ACTION_LABELS[profile],
        "regime": ACTION_LABELS[profile],
        "target_equity_weight": float(allocation["equity"]),
        "reason": ", ".join(reasons),
        "market_above_200d": bool(spy["above_200d"]),
        "growth_above_200d": bool(qqq["above_200d"]),
        "market_6m_return": float(spy["ret_6m"]),
        "growth_6m_return": float(qqq["ret_6m"]),
        "market_cloud": spy["ichimoku_state"],
        "growth_cloud": qqq["ichimoku_state"],
        "market_rsi": float(spy["rsi14"]),
        "growth_rsi": float(qqq["rsi14"]),
        "vix": vix_value,
        "vix_ok": bool(vix_value < float(market.get("high_vix", 30))),
    }


def qg_core_allocations(cfg: dict, profile: str) -> dict:
    defaults = {
        "buy": {"equity": 1.00, "safety": 0.00, "gold": 0.00, "bond": 0.00},
        "accumulate": {"equity": 0.90, "safety": 0.05, "gold": 0.025, "bond": 0.025},
        "hold": {"equity": 0.75, "safety": 0.15, "gold": 0.05, "bond": 0.05},
        "trim": {"equity": 0.50, "safety": 0.40, "gold": 0.05, "bond": 0.05},
        "exit": {"equity": 0.00, "safety": 0.80, "gold": 0.10, "bond": 0.10},
    }
    configured = cfg.get("qg_core_allocations", {}).get(profile, {})
    return {**defaults[profile], **configured}


def _equity_weight_map(scores: pd.DataFrame, equity_budget: float, cfg: dict) -> dict[str, float]:
    if equity_budget <= 0 or scores.empty:
        return {}
    core = cfg["qg_core"]
    minimum = float(core.get("minimum_equity_score", 52))
    permitted = {"신규매수", "분할매수", "보유·관찰", "보유·추격매수 대기"}
    eligible = scores[(scores["score"] >= minimum) & (scores["action"].isin(permitted))]
    broad_names = {str(item).upper() for item in core.get("broad_etfs", [])}
    satellite_names = {str(item).upper() for item in core.get("satellite_etfs", [])}
    eligible = eligible[
        eligible["ticker"].isin(broad_names)
        | (
            eligible["ticker"].isin(satellite_names)
            & (eligible["score"] >= float(core.get("minimum_satellite_score", 62)))
        )
    ]
    if eligible.empty:
        return {}
    chosen = [str(eligible.iloc[0]["ticker"])]
    max_positions = max(1, int(core.get("max_equity_positions", 2)))
    if max_positions >= 2 and len(eligible) >= 2:
        if chosen[0] in satellite_names:
            second_pool = eligible[(eligible["ticker"].isin(broad_names)) & (eligible["ticker"] != chosen[0])]
        else:
            second_pool = eligible[eligible["ticker"] != chosen[0]]
        if not second_pool.empty:
            chosen.append(str(second_pool.iloc[0]["ticker"]))
    if len(chosen) == 1:
        only_weight = equity_budget
        if chosen[0] in satellite_names:
            only_weight = min(only_weight, float(core.get("max_satellite_weight", 0.50)))
        return {chosen[0]: only_weight}
    top_share = clamp(float(core.get("top_position_share", 0.60)), 0.50, 0.70)
    top_weight = equity_budget * top_share
    if chosen[0] in satellite_names:
        top_weight = min(top_weight, float(core.get("max_satellite_weight", 0.50)))
    return {chosen[0]: top_weight, chosen[1]: equity_budget - top_weight}


def _defensive_snapshot(ticker: str, cfg: dict, paths: Paths, benchmark: pd.Series, vix_value: float) -> dict:
    frame = read_price(ticker, paths, refresh=False, source=cfg["settings"].get("data_source", "yahoo"))
    return technical_snapshot(ticker, frame, benchmark, vix_value)


def qg_core_portfolio_plan(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    scores = qg_core_etf_scores(cfg, paths, refresh=refresh)
    decision = market_regime(cfg, paths, refresh=False)
    allocation = qg_core_allocations(cfg, decision["profile"])
    core = cfg["qg_core"]
    equity_weights = _equity_weight_map(scores, float(allocation["equity"]), cfg)
    safety_weight = float(allocation["safety"]) + max(0.0, float(allocation["equity"]) - sum(equity_weights.values()))
    rows: list[dict] = []
    by_ticker = scores.set_index("ticker") if not scores.empty else pd.DataFrame()
    for signal_ticker, weight in equity_weights.items():
        row = by_ticker.loc[signal_ticker]
        rows.append(
            {
                "asset": execution_ticker(cfg, signal_ticker),
                "signal_asset": signal_ticker,
                "type": str(row["role"]),
                "weight": float(weight),
                "action": str(row["action"]),
                "last": float(row["last"]),
                "reason": f"종합 {row['score']:.1f}점 / 추세 {row['trend_score']:.1f} / 모멘텀 {row['momentum_score']:.1f}",
                "timing": str(row["timing"]),
            }
        )

    market_ticker = str(cfg["market_timing"].get("market_asset", "SPY")).upper()
    benchmark = read_price(market_ticker, paths, refresh=False)["Close"]
    vix_value = float(decision["vix"])
    for key, asset_type in [("gold", "금 분산"), ("bond", "장기채 분산")]:
        requested = float(allocation[key])
        if requested <= 0:
            continue
        ticker = str(core.get("gold_asset" if key == "gold" else "bond_asset", "GLD" if key == "gold" else "TLT")).upper()
        snapshot = _defensive_snapshot(ticker, cfg, paths, benchmark, vix_value)
        if snapshot.get("above_200d") and float(snapshot.get("score", 0)) >= 45:
            rows.append(
                {
                    "asset": ticker,
                    "signal_asset": ticker,
                    "type": asset_type,
                    "weight": requested,
                    "action": snapshot["action"],
                    "last": snapshot["last"],
                    "reason": f"분산 비중 / 종합 {snapshot['score']:.1f}점 / 200일선 위",
                    "timing": snapshot["timing"],
                }
            )
        else:
            safety_weight += requested

    safety_asset = str(core.get("live_safety_asset", "SGOV")).upper()
    if safety_weight > 0:
        rows.append(
            {
                "asset": safety_asset,
                "signal_asset": safety_asset,
                "type": "초단기 국채·대기자금",
                "weight": safety_weight,
                "action": "대기자금 보유",
                "last": np.nan,
                "reason": f"주식형 목표 {float(allocation['equity']) * 100:.0f}%와 분산 조건을 적용한 잔여 비중",
                "timing": "다음 매수 신호가 확인될 때까지 대기자금으로 둡니다.",
            }
        )
    plan = pd.DataFrame(rows)
    if not plan.empty:
        total = float(plan["weight"].sum())
        if total > 0:
            plan["weight"] = plan["weight"] / total
    plan.to_csv(paths.output / "qg_core_plan.csv", index=False, encoding="utf-8-sig")
    return plan


def portfolio_plan(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    return qg_core_portfolio_plan(cfg, paths, refresh=refresh)


def _historical_snapshot(
    ticker: str,
    frames: dict[str, pd.DataFrame],
    signal_date: pd.Timestamp,
    benchmark_ticker: str,
    vix_value: float,
) -> dict:
    return technical_snapshot(
        ticker,
        frames[ticker].loc[:signal_date],
        frames[benchmark_ticker]["Close"].loc[:signal_date],
        vix_value,
    )


def qg_core_backtest(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    core = cfg["qg_core"]
    market = cfg["market_timing"]
    frames, errors = _load_analysis_frames(cfg, paths, refresh)
    equity_tickers = [str(item).upper() for item in core["equity_etfs"]]
    safety = str(core.get("backtest_safety_asset", "SHY")).upper()
    gold = str(core.get("gold_asset", "GLD")).upper()
    bond = str(core.get("bond_asset", "TLT")).upper()
    benchmark_ticker = str(market.get("market_asset", "SPY")).upper()
    growth_ticker = str(market.get("growth_asset", "QQQ")).upper()
    vix_ticker = str(market.get("vix_symbol", "^VIX")).upper()
    required = [*equity_tickers, safety, gold, bond, benchmark_ticker, growth_ticker, vix_ticker]
    missing = [ticker for ticker in required if ticker not in frames]
    if missing:
        raise RuntimeError("Backtest data missing: " + ", ".join(sorted(set(missing))))

    close = pd.concat({ticker: frames[ticker]["Close"] for ticker in sorted(set(required))}, axis=1).sort_index().ffill()
    monthly = close.resample("ME").last()
    if not monthly.empty and monthly.index[-1] > close.index[-1]:
        monthly = monthly.iloc[:-1]
    monthly_returns = monthly.pct_change()
    start = pd.Timestamp(core.get("backtest_start_date", "2016-01-01"))
    start_index = next((index for index, date in enumerate(monthly.index) if date >= start), 0)
    transaction_cost = float(core.get("transaction_cost_bps", 10)) / 10_000
    previous_weights: dict[str, float] = {}
    rows: list[dict] = []

    for index in range(start_index, len(monthly.index) - 1):
        signal_date = monthly.index[index]
        hold_date = monthly.index[index + 1]
        vix_history = frames[vix_ticker]["Close"].loc[:signal_date].dropna()
        if vix_history.empty:
            continue
        vix_value = float(vix_history.iloc[-1])
        snapshots: list[dict] = []
        try:
            for ticker in equity_tickers:
                snapshots.append(_historical_snapshot(ticker, frames, signal_date, benchmark_ticker, vix_value))
        except (IndexError, KeyError):
            continue
        if any("error" in row for row in snapshots):
            continue
        scores = pd.DataFrame(snapshots).sort_values(["score", "mom_12_1"], ascending=False)
        spy = next(row for row in snapshots if row["ticker"] == benchmark_ticker)
        qqq = next(row for row in snapshots if row["ticker"] == growth_ticker)
        profile, market_score = _profile_from_market(spy, qqq, vix_value, cfg)
        allocation = qg_core_allocations(cfg, profile)
        weights = _equity_weight_map(scores, float(allocation["equity"]), cfg)
        safety_weight = float(allocation["safety"]) + max(0.0, float(allocation["equity"]) - sum(weights.values()))

        for ticker, key in [(gold, "gold"), (bond, "bond")]:
            requested = float(allocation[key])
            if requested <= 0:
                continue
            snapshot = _historical_snapshot(ticker, frames, signal_date, benchmark_ticker, vix_value)
            if "error" not in snapshot and snapshot.get("above_200d") and float(snapshot.get("score", 0)) >= 45:
                weights[ticker] = weights.get(ticker, 0.0) + requested
            else:
                safety_weight += requested
        weights[safety] = weights.get(safety, 0.0) + safety_weight
        total_weight = sum(weights.values())
        if total_weight <= 0:
            continue
        weights = {ticker: weight / total_weight for ticker, weight in weights.items()}

        turnover = 0.5 * sum(
            abs(weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0))
            for ticker in set(weights) | set(previous_weights)
        )
        strategy_return = -turnover * transaction_cost
        for ticker, weight in weights.items():
            value = monthly_returns.loc[hold_date, ticker] if ticker in monthly_returns else np.nan
            strategy_return += weight * (0.0 if pd.isna(value) else float(value))
        rows.append(
            {
                "date": hold_date,
                "signal_date": signal_date,
                "decision": ACTION_LABELS[profile],
                "market_score": market_score,
                "selected_equity": ",".join(ticker for ticker in weights if ticker in equity_tickers),
                "turnover": turnover,
                "strategy_return": strategy_return,
            }
        )
        previous_weights = weights

    if not rows:
        raise RuntimeError("Backtest could not produce any valid monthly signals")
    signals = pd.DataFrame(rows).set_index("date")
    signals["equity"] = (1 + signals["strategy_return"]).cumprod()
    comparison = pd.DataFrame(index=signals.index)
    comparison["QG_CORE"] = signals["strategy_return"]
    for ticker in [benchmark_ticker, growth_ticker]:
        comparison[ticker] = monthly_returns[ticker].reindex(signals.index)
    metrics = {column: annualized_metrics(comparison[column], 12) for column in comparison.columns}
    signals.to_csv(paths.output / "qg_core_signals.csv", encoding="utf-8-sig")
    comparison.to_csv(paths.output / "qg_core_returns.csv", encoding="utf-8-sig")
    pd.DataFrame(metrics).T.to_csv(paths.output / "qg_core_metrics.csv", encoding="utf-8-sig")
    return {"signals": signals, "returns": comparison, "metrics": metrics, "data_errors": errors}


def full_report(
    cfg: dict,
    paths: Paths,
    refresh: bool = False,
    prepared: dict | None = None,
) -> str:
    if prepared is None:
        scores = qg_core_etf_scores(cfg, paths, refresh=refresh)
        decision = market_regime(cfg, paths, refresh=False)
        plan = qg_core_portfolio_plan(cfg, paths, refresh=False)
        backtest = qg_core_backtest(cfg, paths, refresh=False)
    else:
        scores = prepared["scores"]
        decision = prepared["decision"]
        plan = prepared["plan"]
        backtest = prepared["backtest"]
    qg_metrics = backtest["metrics"].get("QG_CORE", {})
    spy_metrics = backtest["metrics"].get("SPY", {})
    qqq_metrics = backtest["metrics"].get("QQQ", {})
    lines = [
        "# Quant Guardian 지수 타이밍 리포트",
        "",
        f"생성 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}",
        "",
        "미국장 마감 종가를 사용하는 투자 의사결정 보조 자료입니다. 자동주문과 실시간 장중 신호는 제공하지 않습니다.",
        "",
        "## 오늘의 판단",
        f"- 행동: {decision['action']}",
        f"- 주식형 ETF 목표 비중: {decision['target_equity_weight'] * 100:.0f}%",
        f"- 기준일: {decision['as_of']}",
        f"- 근거: {decision['reason']}",
        "",
        "## 지수 ETF 매수 타이밍",
    ]
    for _, row in scores.iterrows():
        lines.append(
            f"- {row['ticker']} → {row['execution_ticker']}: {row['action']}, {row['score']:.1f}점, "
            f"RSI {row['rsi14']:.1f}, {row['ichimoku_state']}, 200일선 {'위' if row['above_200d'] else '아래'}"
        )
        lines.append(f"  - 실행 기준: {row['timing']}")
    lines.extend(["", "## 목표 포트폴리오"])
    for _, row in plan.iterrows():
        lines.append(
            f"- {row['asset']}: {row['weight'] * 100:.1f}% / {row['action']} / {row['reason']}"
        )
    lines.extend(
        [
            "",
            "## 백테스트 참고",
            f"- QG Index Timing CAGR: {pct(qg_metrics.get('cagr'))}",
            f"- QG Index Timing MDD: {pct(qg_metrics.get('mdd'))}",
            f"- QG Index Timing Sharpe: {num(qg_metrics.get('sharpe'))}",
            f"- SPY CAGR/MDD: {pct(spy_metrics.get('cagr'))} / {pct(spy_metrics.get('mdd'))}",
            f"- QQQ CAGR/MDD: {pct(qqq_metrics.get('cagr'))} / {pct(qqq_metrics.get('mdd'))}",
            "- 월말 신호를 다음 달 수익률에 적용하고 설정된 거래비용을 차감했습니다.",
            "- 백테스트는 가상 성과이며 미래 수익을 보장하지 않습니다.",
            "",
            "## 계산 지표",
            "- 추세: 20/50/100/200일선, EMA 12/26, MACD, 일목균형표, ADX",
            "- 모멘텀: 1/3/6/12개월 수익률, 12-1개월 모멘텀, SPY 대비 6개월 상대강도",
            "- 진입 타이밍: RSI, 스토캐스틱, 볼린저밴드, ATR 이격, 20일 돌파, 거래량, OBV",
            "- 위험: 63일 변동성, 1년 최대낙폭, ATR 비율, VIX",
        ]
    )
    text = "\n".join(lines)
    (paths.output / "report.md").write_text(text, encoding="utf-8-sig")
    report_html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Quant Guardian 지수 타이밍 리포트</title>"
        "<style>body{font-family:Arial,'Malgun Gothic',sans-serif;background:#f4f6f9;color:#172033;max-width:960px;margin:0 auto;padding:32px 20px;line-height:1.65}"
        "pre{white-space:pre-wrap;background:#fff;border:1px solid #dfe5ef;border-radius:8px;padding:18px}</style>"
        "</head><body><pre>"
        + html.escape(text)
        + "</pre></body></html>"
    )
    (paths.output / "report.html").write_text(report_html, encoding="utf-8-sig")
    return text


def cmd_signal(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    scores = qg_core_etf_scores(cfg, paths, refresh=args.refresh)
    decision = market_regime(cfg, paths, refresh=False)
    top = scores.iloc[0] if not scores.empty else None
    print(f"오늘의 판단: {decision['action']}")
    print(f"주식형 ETF 목표 비중: {decision['target_equity_weight'] * 100:.0f}%")
    if top is not None:
        print(f"우선 확인: {top['execution_ticker']} (신호 지수 {top['ticker']})")
        print(f"종합점수: {top['score']:.1f}, 행동: {top['action']}")
        print(f"시점: {top['timing']}")
    print("자동주문 없음. 미국장 마감 종가 기준입니다.")
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    plan = qg_core_portfolio_plan(cfg, paths, refresh=args.refresh)
    print(plan.to_string(index=False))
    print(f"저장 위치: {paths.output / 'qg_core_plan.csv'}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    result = qg_core_backtest(cfg, paths, refresh=args.refresh)
    for name, metrics in result["metrics"].items():
        print(f"\n{name}")
        for key, value in metrics.items():
            print(f"- {key}: {pct(value) if key in {'cagr', 'mdd', 'win_rate', 'total_return'} else num(value)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    print(full_report(cfg, paths, refresh=args.refresh))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant_guardian",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Quant Guardian 지수 타이밍
            - 지수 ETF의 추세, 모멘텀, 진입 타이밍, 위험을 종합합니다.
            - 결과는 신규매수, 분할매수, 보유, 비중축소, 매도·대기로 표시합니다.
            - 자동주문은 하지 않습니다.
            """
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    signal = subparsers.add_parser("signal", help="Show current index timing signal")
    signal.add_argument("--refresh", action="store_true")
    signal.set_defaults(func=cmd_signal)
    portfolio = subparsers.add_parser("portfolio", help="Show target portfolio")
    portfolio.add_argument("--refresh", action="store_true")
    portfolio.set_defaults(func=cmd_portfolio)
    backtest = subparsers.add_parser("backtest", help="Run index timing backtest")
    backtest.add_argument("--refresh", action="store_true")
    backtest.set_defaults(func=cmd_backtest)
    report = subparsers.add_parser("report", help="Create report files")
    report.add_argument("--refresh", action="store_true")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
