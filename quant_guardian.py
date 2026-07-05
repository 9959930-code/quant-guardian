from __future__ import annotations

import argparse
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
USER_AGENT = "quant-guardian-v3/0.3 research-tool"
KST = ZoneInfo("Asia/Seoul")

SECTOR_MAP = {
    "AAPL": "정보기술", "MSFT": "정보기술", "NVDA": "정보기술", "AMZN": "경기소비재",
    "GOOGL": "커뮤니케이션", "META": "커뮤니케이션", "AVGO": "정보기술", "TSLA": "경기소비재",
    "COST": "필수소비재", "NFLX": "커뮤니케이션", "AMD": "정보기술", "ADBE": "정보기술",
    "CRM": "정보기술", "ORCL": "정보기술", "NOW": "정보기술", "PANW": "정보기술",
    "CRWD": "정보기술", "SNPS": "정보기술", "CDNS": "정보기술", "AMAT": "정보기술",
    "LRCX": "정보기술", "KLAC": "정보기술", "MU": "정보기술", "QCOM": "정보기술",
    "INTC": "정보기술", "TXN": "정보기술", "ADI": "정보기술", "JPM": "금융",
    "V": "금융", "MA": "금융", "UNH": "헬스케어", "LLY": "헬스케어",
    "ABBV": "헬스케어", "MRK": "헬스케어", "JNJ": "헬스케어", "HD": "경기소비재",
    "LOW": "경기소비재", "WMT": "필수소비재", "TGT": "필수소비재", "TJX": "경기소비재",
    "NKE": "경기소비재", "SBUX": "경기소비재", "MCD": "경기소비재", "KO": "필수소비재",
    "PEP": "필수소비재", "XOM": "에너지", "CVX": "에너지", "CAT": "산업재",
    "DE": "산업재", "GE": "산업재", "LIN": "소재", "SHW": "소재",
    "NEE": "유틸리티", "CEG": "유틸리티", "PLTR": "정보기술", "UBER": "산업재",
    "SHOP": "정보기술", "ISRG": "헬스케어", "BKNG": "경기소비재", "MAR": "경기소비재",
}


@dataclass(frozen=True)
class Paths:
    root: Path
    cache: Path
    output: Path


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def resolve_paths(cfg: dict) -> Paths:
    root = ROOT
    cache = root / cfg["settings"].get("cache_dir", "data/cache")
    output = root / cfg["settings"].get("output_dir", "output")
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    return Paths(root=root, cache=cache, output=output)


def yahoo_symbol(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def cache_key(source: str, ticker: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_^=-]+", "_", yahoo_symbol(ticker))
    return f"{source}_{safe}.csv"


def fetch_text(url: str, retries: int = 2, pause: float = 0.5) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(pause)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def fetch_yahoo_price(ticker: str) -> pd.DataFrame:
    symbol = yahoo_symbol(ticker)
    url_symbol = quote(symbol, safe="")
    period2 = int((datetime.now(UTC) + timedelta(days=2)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{url_symbol}"
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
    price_quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    close = adj if adj else price_quote.get("close")
    if not timestamps or close is None:
        raise RuntimeError(f"Yahoo response missing prices for {ticker}")
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(timestamps, unit="s").tz_localize("UTC").tz_convert(None).date,
            "Open": price_quote.get("open"),
            "High": price_quote.get("high"),
            "Low": price_quote.get("low"),
            "Close": close,
            "Volume": price_quote.get("volume"),
        }
    )
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"]).sort_values("Date")
    if df.empty:
        raise RuntimeError(f"Yahoo returned empty prices for {ticker}")
    return df.set_index("Date")


def read_price(ticker: str, paths: Paths, refresh: bool = False, source: str = "yahoo") -> pd.DataFrame:
    source = source.lower().strip()
    cache_file = paths.cache / cache_key(source, ticker)
    if refresh or not cache_file.exists():
        if not refresh and not cache_file.exists():
            raise RuntimeError(f"No cached data for {ticker}; run refresh first")
        if source != "yahoo":
            raise RuntimeError(f"Unsupported data source: {source}")
        df = fetch_yahoo_price(ticker)
        df.to_csv(cache_file, encoding="utf-8")
    df = pd.read_csv(cache_file)
    if df.empty or "Date" not in df or "Close" not in df:
        raise RuntimeError(f"Cached data is invalid for {ticker}: {cache_file}")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date").set_index("Date")
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def price_panel(
    tickers: Iterable[str],
    paths: Paths,
    refresh: bool = False,
    source: str = "yahoo",
) -> pd.DataFrame:
    series = {}
    errors = []
    for ticker in sorted(set(tickers)):
        try:
            df = read_price(ticker, paths, refresh=refresh, source=source)
            series[ticker.upper()] = df["Close"].rename(ticker.upper())
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


def max_drawdown(returns: pd.Series) -> float:
    curve = (1 + returns.fillna(0)).cumprod()
    peak = curve.cummax()
    dd = curve / peak - 1
    return float(dd.min())


def annualized_metrics(returns: pd.Series, periods_per_year: int = 12) -> dict:
    returns = returns.dropna()
    if returns.empty:
        return {
            "cagr": np.nan, "mdd": np.nan, "sharpe": np.nan, "sortino": np.nan,
            "calmar": np.nan, "win_rate": np.nan, "total_return": np.nan,
        }
    years = len(returns) / periods_per_year
    total = float((1 + returns).prod() - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = returns.std(ddof=0) * math.sqrt(periods_per_year)
    sharpe = (returns.mean() * periods_per_year) / vol if vol > 0 else np.nan
    downside = returns[returns < 0].std(ddof=0) * math.sqrt(periods_per_year)
    sortino = (returns.mean() * periods_per_year) / downside if downside > 0 else np.nan
    mdd = max_drawdown(returns)
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    return {
        "cagr": float(cagr),
        "mdd": float(mdd),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "win_rate": float((returns > 0).mean()),
        "total_return": total,
    }


def monthly_etf_momentum(
    close: pd.DataFrame,
    risk_assets: list[str],
    safety_asset: str,
    lookback_months: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    risk_assets = [x.upper() for x in risk_assets]
    safety_asset = safety_asset.upper()
    all_assets = [*risk_assets, safety_asset]
    prices = close[all_assets].dropna(how="all").resample("ME").last().ffill()
    returns = prices.pct_change()
    momentum = prices[risk_assets].pct_change(lookback_months)
    rows = []
    for dt, row in momentum.iterrows():
        valid = row.dropna()
        if valid.empty:
            signal = None
            signal_momentum = np.nan
        else:
            signal = str(valid.idxmax())
            signal_momentum = float(valid.max())
            if signal_momentum <= 0:
                signal = safety_asset
        rows.append({"date": dt, "signal": signal, "momentum": signal_momentum})
    signals = pd.DataFrame(rows).set_index("date")
    signals["held_asset"] = signals["signal"].shift(1)
    signals["strategy_return"] = [
        returns.loc[dt, held] if isinstance(held, str) and held in returns.columns else np.nan
        for dt, held in signals["held_asset"].items()
    ]
    signals = signals.dropna(subset=["strategy_return"])
    signals["equity"] = (1 + signals["strategy_return"]).cumprod()
    bench = returns[all_assets].copy()
    bench["STRATEGY"] = signals["strategy_return"]
    return signals, bench


def etf_backtest(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    st = cfg["etf_strategy"]
    risk_assets = [x.upper() for x in st["risk_assets"]]
    safety_asset = st["safety_asset"].upper()
    tickers = [*risk_assets, safety_asset]
    close = price_panel(tickers, paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    close = close.loc[close.index >= pd.Timestamp(st["start_date"])]
    signals, bench = monthly_etf_momentum(close, risk_assets, safety_asset, int(st["lookback_months"]))
    metrics = {"STRATEGY": annualized_metrics(signals["strategy_return"], 12)}
    for ticker in tickers:
        metrics[ticker] = annualized_metrics(bench[ticker].loc[signals.index], 12)
    signals.to_csv(paths.output / "etf_signals.csv", encoding="utf-8-sig")
    pd.DataFrame(metrics).T.to_csv(paths.output / "etf_metrics.csv", encoding="utf-8-sig")
    return {
        "signals": signals,
        "metrics": metrics,
        "latest_price_date": close.index[-1].date().isoformat(),
        "data_errors": close.attrs.get("errors", []),
    }


def latest_etf_signal(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    result = etf_backtest(cfg, paths, refresh=refresh)
    signals = result["signals"]
    signal = str(signals["signal"].dropna().iloc[-1])
    momentum = float(signals["momentum"].dropna().iloc[-1])
    return {
        "as_of": result["latest_price_date"],
        "current_signal": signal,
        "latest_momentum": momentum,
        "metrics": result["metrics"]["STRATEGY"],
    }


def market_regime(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    mr = cfg["market_regime"]
    tickers = [mr["market_asset"], mr["growth_asset"], mr["safety_asset"], mr["vix_symbol"]]
    close = price_panel(tickers, paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    market = mr["market_asset"].upper()
    growth = mr["growth_asset"].upper()
    vix = mr["vix_symbol"].upper()
    row: dict[str, object] = {}
    score = 0
    if market in close:
        spy = close[market].dropna()
        row["market_above_200d"] = bool(spy.iloc[-1] > spy.rolling(200).mean().iloc[-1])
        row["market_6m_return"] = float(spy.iloc[-1] / spy.iloc[-126] - 1) if len(spy) > 126 else np.nan
        score += int(bool(row["market_above_200d"]))
        score += int(float(row["market_6m_return"]) > 0)
    else:
        row["market_above_200d"] = None
        row["market_6m_return"] = np.nan
    if growth in close:
        qqq = close[growth].dropna()
        row["growth_above_200d"] = bool(qqq.iloc[-1] > qqq.rolling(200).mean().iloc[-1])
        row["growth_6m_return"] = float(qqq.iloc[-1] / qqq.iloc[-126] - 1) if len(qqq) > 126 else np.nan
        score += int(bool(row["growth_above_200d"]))
        score += int(float(row["growth_6m_return"]) > 0)
    else:
        row["growth_above_200d"] = None
        row["growth_6m_return"] = np.nan
    if vix in close:
        vix_last = float(close[vix].dropna().iloc[-1])
        row["vix"] = vix_last
        row["vix_ok"] = bool(vix_last < float(mr["risk_on_vix"]))
        score += int(bool(row["vix_ok"]))
    else:
        row["vix"] = np.nan
        row["vix_ok"] = None
    if score >= 4:
        regime = "공격"
        profile = "aggressive"
    elif score >= 2:
        regime = "중립"
        profile = "neutral"
    else:
        regime = "방어"
        profile = "defensive"
    row.update(
        {
            "as_of": close.index[-1].date().isoformat(),
            "score": score,
            "max_score": 5,
            "regime": regime,
            "profile": profile,
            "data_errors": close.attrs.get("errors", []),
        }
    )
    return row


def score_stock(close: pd.Series, volume: pd.Series | None = None, market_regime_name: str = "중립") -> dict:
    close = close.dropna()
    if len(close) < 253:
        return {"error": "not enough data"}
    last = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1)
    ret_6m = float(close.iloc[-1] / close.iloc[-126] - 1)
    ret_12m = float(close.iloc[-1] / close.iloc[-252] - 1)
    mom_12_1 = float(close.iloc[-22] / close.iloc[-252] - 1)
    daily_ret = close.pct_change().dropna()
    vol63 = float(daily_ret.tail(63).std(ddof=0) * math.sqrt(252))
    dd252 = float((close.tail(252) / close.tail(252).cummax() - 1).min())
    rsi14 = float(rsi(close).iloc[-1])
    avg_volume_60d = np.nan
    dollar_volume_m = np.nan
    if volume is not None and not volume.dropna().empty:
        volume = volume.reindex(close.index).dropna()
        if not volume.empty:
            avg_volume_60d = float(volume.tail(60).mean())
            dollar_volume_m = avg_volume_60d * last / 1_000_000
    momentum_score = 35 * (
        0.50 * clamp((mom_12_1 + 0.05) / 0.65, 0, 1)
        + 0.35 * clamp((ret_6m + 0.03) / 0.45, 0, 1)
        + 0.15 * clamp((ret_3m + 0.02) / 0.28, 0, 1)
    )
    trend_score = 20 * (
        0.35 * int(last > sma20) + 0.35 * int(last > sma50) + 0.20 * int(last > sma200) + 0.10 * int(sma50 > sma200)
    )
    risk_score = 15 * (0.55 * clamp((0.65 - vol63) / 0.55, 0, 1) + 0.45 * clamp((dd252 + 0.45) / 0.45, 0, 1))
    if 45 <= rsi14 <= 65:
        timing_score = 10
    elif 35 <= rsi14 < 45 or 65 < rsi14 <= 72:
        timing_score = 7
    elif rsi14 > 72:
        timing_score = 3
    else:
        timing_score = 4
    liquidity_score = 5 * clamp((math.log10(max(dollar_volume_m, 0.01)) - 1) / 3, 0, 1)
    quality_proxy_score = 5 * (0.50 * int(ret_12m > 0) + 0.25 * clamp((0.50 - vol63) / 0.40, 0, 1) + 0.25 * int(last > sma200))
    regime_score = {"공격": 10, "중립": 6, "방어": 2}.get(market_regime_name, 6)
    total = clamp(momentum_score + trend_score + risk_score + timing_score + liquidity_score + quality_proxy_score + regime_score, 0, 100)
    if total >= 75 and rsi14 <= 72 and last > sma200:
        status = "매수후보"
    elif total >= 70 and rsi14 > 72:
        status = "과열주의"
    elif total >= 60:
        status = "관찰"
    else:
        status = "제외"
    reasons = []
    if mom_12_1 > 0.15:
        reasons.append("12-1개월 모멘텀 양호")
    if last > sma200 and sma50 > sma200:
        reasons.append("중기 추세 상승")
    if 45 <= rsi14 <= 65:
        reasons.append("RSI 진입권")
    if rsi14 > 72:
        reasons.append("RSI 과열")
    if dd252 < -0.30:
        reasons.append("1년 낙폭 큼")
    if vol63 > 0.55:
        reasons.append("변동성 높음")
    if pd.notna(dollar_volume_m) and dollar_volume_m > 500:
        reasons.append("거래대금 충분")
    if not reasons:
        reasons.append("점수는 중립권")
    return {
        "last": last, "ret_3m": ret_3m, "ret_6m": ret_6m, "ret_12m": ret_12m, "mom_12_1": mom_12_1,
        "sma20": sma20, "sma50": sma50, "sma200": sma200, "vol63": vol63, "drawdown_252d": dd252,
        "rsi14": rsi14, "avg_volume_60d": avg_volume_60d, "dollar_volume_m": dollar_volume_m,
        "momentum_score": momentum_score, "trend_score": trend_score, "risk_score": risk_score,
        "timing_score": timing_score, "liquidity_score": liquidity_score, "quality_proxy_score": quality_proxy_score,
        "regime_score": regime_score, "quant_score": total, "status": status, "reason": ", ".join(reasons),
    }


def stock_scores(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    regime = market_regime(cfg, paths, refresh=refresh)
    tickers = [x.upper() for x in cfg["universe"]["tickers"]]
    source = cfg["settings"].get("data_source", "yahoo")
    start = pd.Timestamp(cfg["stock_strategy"]["start_date"])
    rows = []
    errors = []
    for ticker in tickers:
        try:
            price = read_price(ticker, paths, refresh=refresh, source=source)
            price = price.loc[price.index >= start]
            row = {"ticker": ticker}
            row.update(score_stock(price["Close"], price.get("Volume"), regime["regime"]))
            rows.append(row)
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df.get("error").isna() if "error" in df else [True] * len(df)]
        df["sector"] = df["ticker"].map(SECTOR_MAP).fillna("기타")
        df = df.sort_values(["quant_score", "mom_12_1"], ascending=False)
    pd.DataFrame({"error": errors}).to_csv(paths.output / "data_errors.csv", index=False, encoding="utf-8-sig")
    df.to_csv(paths.output / "stock_scores.csv", index=False, encoding="utf-8-sig")
    return df


def etf_score_from_close(ticker: str, close: pd.Series) -> dict:
    close = close.dropna()
    if len(close) < 253:
        return {"ticker": ticker, "error": "not enough data"}
    last = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    mom_12_1 = float(close.iloc[-22] / close.iloc[-252] - 1)
    ret_6m = float(close.iloc[-1] / close.iloc[-126] - 1)
    ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1)
    rsi14 = float(rsi(close).iloc[-1])
    score = (
        40 * clamp((mom_12_1 + 0.05) / 0.65, 0, 1)
        + 30 * clamp((ret_6m + 0.03) / 0.45, 0, 1)
        + 15 * clamp((ret_3m + 0.02) / 0.28, 0, 1)
        + 10 * int(last > sma200)
    )
    if rsi14 > 72:
        score -= 5
    elif 45 <= rsi14 <= 65:
        score += 5
    return {
        "ticker": ticker, "last": last, "mom_12_1": mom_12_1, "ret_6m": ret_6m, "ret_3m": ret_3m,
        "sma50": sma50, "sma200": sma200, "above_200d": bool(last > sma200), "rsi14": rsi14,
        "score": clamp(score, 0, 100),
    }


def qg_core_etf_scores(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    qc = cfg["qg_core"]
    tickers = [x.upper() for x in qc["offensive_etfs"]]
    close = price_panel(tickers, paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    rows = [etf_score_from_close(ticker, close[ticker]) for ticker in tickers if ticker in close]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df.get("error").isna() if "error" in df else [True] * len(df)]
        df = df.sort_values(["score", "mom_12_1"], ascending=False)
    df.to_csv(paths.output / "qg_core_etf_scores.csv", index=False, encoding="utf-8-sig")
    return df


def execution_ticker(cfg: dict, signal_ticker: str) -> str:
    mapping = cfg.get("qg_core_execution_map", {})
    return str(mapping.get(signal_ticker.upper(), signal_ticker.upper()))


def qg_core_allocations(cfg: dict, profile: str) -> dict:
    defaults = {
        "aggressive": {"etf": 0.60, "stock": 0.25, "safety": 0.15, "gold": 0.00, "bond": 0.00},
        "neutral": {"etf": 0.35, "stock": 0.10, "safety": 0.35, "gold": 0.10, "bond": 0.10},
        "defensive": {"etf": 0.00, "stock": 0.00, "safety": 0.80, "gold": 0.10, "bond": 0.10},
    }
    configured = cfg.get("qg_core_allocations", {}).get(profile, {})
    return {**defaults[profile], **configured}


def qg_core_portfolio_plan(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    regime = market_regime(cfg, paths, refresh=refresh)
    alloc = qg_core_allocations(cfg, regime["profile"])
    qc = cfg["qg_core"]
    etfs = qg_core_etf_scores(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=False)
    safety_asset = qc.get("live_safety_asset", "SGOV").upper()
    gold_asset = qc.get("gold_asset", "GLD").upper()
    bond_asset = qc.get("bond_asset", "TLT").upper()
    rows: list[dict] = []
    etf_budget = float(alloc["etf"])
    if etf_budget > 0 and not etfs.empty:
        positive = etfs[(etfs["mom_12_1"] > 0) & (etfs["above_200d"])]
        top = positive.head(2) if not positive.empty else etfs.head(1)
        weights = [etf_budget * 0.70, etf_budget * 0.30] if len(top) >= 2 else [etf_budget]
        for (_, row), weight in zip(top.iterrows(), weights):
            signal = str(row["ticker"])
            rows.append(
                {
                    "asset": execution_ticker(cfg, signal),
                    "signal_asset": signal,
                    "type": "성장 ETF 코어",
                    "weight": float(weight),
                    "reason": f"{signal} 점수 {row['score']:.1f}, 12-1M {row['mom_12_1'] * 100:.1f}%",
                }
            )
    else:
        alloc["safety"] = float(alloc["safety"]) + etf_budget
    stock_budget = float(alloc["stock"])
    if stock_budget > 0 and not scores.empty:
        max_positions = int(qc.get("stock_max_positions", cfg["stock_strategy"].get("max_positions", 5)))
        max_single = float(qc.get("max_single_stock_weight", cfg["stock_strategy"].get("max_single_stock_weight", 0.06)))
        sector_cap = float(qc.get("sector_cap", 0.40))
        per_stock = min(max_single, stock_budget / max_positions)
        selected = []
        sector_weights: dict[str, float] = {}
        candidates = scores[(scores["status"] == "매수후보") & (scores["quant_score"] >= float(qc.get("stock_min_score", 75)))]
        for _, row in candidates.iterrows():
            sector = str(row.get("sector", "기타"))
            if sector_weights.get(sector, 0.0) + per_stock > sector_cap:
                continue
            selected.append(row)
            sector_weights[sector] = sector_weights.get(sector, 0.0) + per_stock
            if len(selected) >= max_positions:
                break
        used = per_stock * len(selected)
        for row in selected:
            rows.append(
                {
                    "asset": row["ticker"],
                    "signal_asset": row["ticker"],
                    "type": "대형주 위성",
                    "weight": float(per_stock),
                    "reason": f"{row.get('sector', '기타')} / 점수 {row['quant_score']:.1f} / {row['reason']}",
                }
            )
        alloc["safety"] = float(alloc["safety"]) + max(0.0, stock_budget - used)
    else:
        alloc["safety"] = float(alloc["safety"]) + stock_budget
    for asset, asset_type, weight in [
        (safety_asset, "초단기 국채/대기자금", float(alloc["safety"])),
        (gold_asset, "금 대체자산", float(alloc["gold"])),
        (bond_asset, "장기채 방어자산", float(alloc["bond"])),
    ]:
        if weight > 0:
            rows.append(
                {
                    "asset": asset,
                    "signal_asset": asset,
                    "type": asset_type,
                    "weight": weight,
                    "reason": f"{regime['regime']} 국면의 방어/대기 비중",
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


def _monthly_regime(monthly: pd.DataFrame, idx: int, cfg: dict) -> str:
    mr = cfg["market_regime"]
    market = mr["market_asset"].upper()
    growth = "QQQ"
    vix = mr["vix_symbol"].upper()
    score = 0
    if market in monthly and idx >= 10:
        score += int(monthly[market].iloc[idx] > monthly[market].iloc[idx - 9 : idx + 1].mean())
    if growth in monthly and idx >= 10:
        score += int(monthly[growth].iloc[idx] > monthly[growth].iloc[idx - 9 : idx + 1].mean())
    if market in monthly and idx >= 6:
        score += int(monthly[market].iloc[idx] / monthly[market].iloc[idx - 6] - 1 > 0)
    if growth in monthly and idx >= 6:
        score += int(monthly[growth].iloc[idx] / monthly[growth].iloc[idx - 6] - 1 > 0)
    if vix in monthly:
        score += int(float(monthly[vix].iloc[idx]) < float(mr["risk_on_vix"]))
    if score >= 4:
        return "공격"
    if score >= 2:
        return "중립"
    return "방어"


def _monthly_rank_score(monthly: pd.DataFrame, ticker: str, idx: int) -> float:
    if ticker not in monthly or idx < 12:
        return np.nan
    now = monthly[ticker].iloc[idx]
    if pd.isna(now):
        return np.nan
    mom_12_1 = monthly[ticker].iloc[idx - 1] / monthly[ticker].iloc[idx - 12] - 1
    ret_6m = monthly[ticker].iloc[idx] / monthly[ticker].iloc[idx - 6] - 1
    ret_3m = monthly[ticker].iloc[idx] / monthly[ticker].iloc[idx - 3] - 1
    trend = 1.0 if monthly[ticker].iloc[idx] > monthly[ticker].iloc[idx - 9 : idx + 1].mean() else 0.0
    return float(0.45 * mom_12_1 + 0.35 * ret_6m + 0.20 * ret_3m + 0.10 * trend)


def qg_core_backtest(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    qc = cfg["qg_core"]
    offensive = [x.upper() for x in qc["backtest_offensive_etfs"]]
    safety = qc.get("backtest_safety_asset", "SHY").upper()
    defensive = [safety, qc.get("gold_asset", "GLD").upper(), qc.get("bond_asset", "TLT").upper()]
    stocks = [x.upper() for x in cfg["universe"]["tickers"]]
    bench = ["SPY", "QQQ", cfg["market_regime"]["vix_symbol"].upper()]
    tickers = sorted(set([*offensive, *defensive, *stocks, *bench]))
    close = price_panel(tickers, paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    start = pd.Timestamp(qc.get("backtest_start_date", cfg["stock_strategy"].get("rotation_start_date", "2016-01-01")))
    monthly = close.loc[close.index >= start].resample("ME").last().ffill()
    returns = monthly.pct_change()
    rows = []
    for i in range(13, len(monthly) - 1):
        signal_date = monthly.index[i]
        hold_date = monthly.index[i + 1]
        regime = _monthly_regime(monthly, i, cfg)
        profile = {"공격": "aggressive", "중립": "neutral", "방어": "defensive"}[regime]
        alloc = qg_core_allocations(cfg, profile)
        weights: dict[str, float] = {}
        etf_scores = {ticker: _monthly_rank_score(monthly, ticker, i) for ticker in offensive}
        etf_scores = {k: v for k, v in etf_scores.items() if pd.notna(v) and v > 0}
        top_etfs = [k for k, _ in sorted(etf_scores.items(), key=lambda item: item[1], reverse=True)[:2]]
        etf_budget = float(alloc["etf"])
        if etf_budget > 0 and top_etfs:
            if len(top_etfs) >= 2:
                weights[top_etfs[0]] = weights.get(top_etfs[0], 0.0) + etf_budget * 0.70
                weights[top_etfs[1]] = weights.get(top_etfs[1], 0.0) + etf_budget * 0.30
            else:
                weights[top_etfs[0]] = weights.get(top_etfs[0], 0.0) + etf_budget
        else:
            weights[safety] = weights.get(safety, 0.0) + etf_budget
        stock_budget = float(alloc["stock"])
        selected_stocks: list[str] = []
        if stock_budget > 0:
            stock_scores_now = {ticker: _monthly_rank_score(monthly, ticker, i) for ticker in stocks}
            stock_scores_now = {k: v for k, v in stock_scores_now.items() if pd.notna(v) and v > 0}
            selected_stocks = [
                k for k, _ in sorted(stock_scores_now.items(), key=lambda item: item[1], reverse=True)[: int(qc["stock_max_positions"])]
            ]
            if selected_stocks:
                each = stock_budget / len(selected_stocks)
                for ticker in selected_stocks:
                    weights[ticker] = weights.get(ticker, 0.0) + each
            else:
                weights[safety] = weights.get(safety, 0.0) + stock_budget
        weights[safety] = weights.get(safety, 0.0) + float(alloc["safety"])
        weights[qc.get("gold_asset", "GLD").upper()] = weights.get(qc.get("gold_asset", "GLD").upper(), 0.0) + float(alloc["gold"])
        weights[qc.get("bond_asset", "TLT").upper()] = weights.get(qc.get("bond_asset", "TLT").upper(), 0.0) + float(alloc["bond"])
        total_weight = sum(weights.values())
        if total_weight > 0:
            weights = {k: v / total_weight for k, v in weights.items()}
        strat_return = 0.0
        for ticker, weight in weights.items():
            value = returns.loc[hold_date, ticker] if ticker in returns else np.nan
            strat_return += weight * (0.0 if pd.isna(value) else float(value))
        rows.append(
            {
                "date": hold_date,
                "signal_date": signal_date,
                "regime": regime,
                "selected_etfs": ",".join(top_etfs),
                "selected_stocks": ",".join(selected_stocks),
                "strategy_return": strat_return,
            }
        )
    signals = pd.DataFrame(rows).set_index("date")
    signals["equity"] = (1 + signals["strategy_return"]).cumprod()
    comparison = pd.DataFrame(index=signals.index)
    comparison["QG_CORE"] = signals["strategy_return"]
    for ticker in ["SPY", "QQQ"]:
        if ticker in returns:
            comparison[ticker] = returns[ticker].loc[signals.index]
    metrics = {col: annualized_metrics(comparison[col], 12) for col in comparison.columns}
    signals.to_csv(paths.output / "qg_core_signals.csv", encoding="utf-8-sig")
    comparison.to_csv(paths.output / "qg_core_returns.csv", encoding="utf-8-sig")
    pd.DataFrame(metrics).T.to_csv(paths.output / "qg_core_metrics.csv", encoding="utf-8-sig")
    return {"signals": signals, "returns": comparison, "metrics": metrics, "data_errors": close.attrs.get("errors", [])}


def stock_rotation_backtest(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    tickers = [x.upper() for x in cfg["universe"]["tickers"]]
    bench = ["SPY", "QQQ"]
    close = price_panel([*tickers, *bench], paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    start = pd.Timestamp(cfg["stock_strategy"]["rotation_start_date"])
    monthly = close.loc[close.index >= start].resample("ME").last().ffill()
    returns = monthly.pct_change()
    rows = []
    for i in range(13, len(monthly) - 1):
        hold_date = monthly.index[i + 1]
        rank_score = {ticker: _monthly_rank_score(monthly, ticker, i) for ticker in tickers}
        available = {k: v for k, v in rank_score.items() if pd.notna(v) and v > 0}
        selected = [k for k, _ in sorted(available.items(), key=lambda item: item[1], reverse=True)[: int(cfg["stock_strategy"]["rotation_top_n"])]]
        ret = float(returns.loc[hold_date, selected].mean()) if selected else 0.0
        rows.append({"date": hold_date, "selected": ",".join(selected), "strategy_return": ret})
    bt = pd.DataFrame(rows).set_index("date")
    comparison = pd.DataFrame(index=bt.index)
    comparison["STOCK_ROTATION"] = bt["strategy_return"]
    for ticker in bench:
        if ticker in returns:
            comparison[ticker] = returns[ticker].loc[bt.index]
    metrics = {col: annualized_metrics(comparison[col], 12) for col in comparison.columns}
    bt.to_csv(paths.output / "stock_rotation_signals.csv", encoding="utf-8-sig")
    pd.DataFrame(metrics).T.to_csv(paths.output / "stock_rotation_metrics.csv", encoding="utf-8-sig")
    return {"signals": bt, "returns": comparison, "metrics": metrics, "data_errors": close.attrs.get("errors", [])}


def full_report(cfg: dict, paths: Paths, refresh: bool = False) -> str:
    regime = market_regime(cfg, paths, refresh=refresh)
    etf_scores = qg_core_etf_scores(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=False)
    plan = qg_core_portfolio_plan(cfg, paths, refresh=False)
    qg = qg_core_backtest(cfg, paths, refresh=False)
    qg_metrics = qg["metrics"].get("QG_CORE", {})
    spy_metrics = qg["metrics"].get("SPY", {})
    qqq_metrics = qg["metrics"].get("QQQ", {})
    lines = [
        "# Quant Guardian QG-Core 리포트",
        "",
        f"생성 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}",
        "",
        "투자 추천이 아니라 무료 데이터 기반 퀀트 점검 리포트입니다. 자동주문은 하지 않습니다.",
        "",
        "## 시장국면",
        f"- 현재 모드: {regime['regime']} ({regime['score']}/{regime['max_score']})",
        f"- 기준일: {regime['as_of']}",
        f"- SPY 6개월 수익률: {pct(regime.get('market_6m_return'))}",
        f"- QQQ 6개월 수익률: {pct(regime.get('growth_6m_return'))}",
        f"- VIX: {num(regime.get('vix'))}",
        "",
        "## QG-Core 백테스트",
        f"- QG-Core CAGR: {pct(qg_metrics.get('cagr'))}",
        f"- QG-Core MDD: {pct(qg_metrics.get('mdd'))}",
        f"- QG-Core Sharpe: {num(qg_metrics.get('sharpe'))}",
        f"- SPY CAGR: {pct(spy_metrics.get('cagr'))}",
        f"- QQQ CAGR: {pct(qqq_metrics.get('cagr'))}",
        "",
        "## 성장 ETF 점수",
    ]
    for _, row in etf_scores.head(6).iterrows():
        lines.append(
            f"- {row['ticker']}: 점수 {num(row['score'])}, 12-1M {pct(row['mom_12_1'])}, 6M {pct(row['ret_6m'])}, RSI {num(row['rsi14'])}"
        )
    lines.extend(["", "## 상위 대형주 후보"])
    for _, row in scores.head(10).iterrows():
        lines.append(
            f"- {row['ticker']} ({row.get('sector', '기타')}): {num(row['quant_score'])}점, {row['status']}, "
            f"12-1M {pct(row['mom_12_1'])}, RSI {num(row['rsi14'])}, {row['reason']}"
        )
    lines.extend(["", "## QG-Core 제안 비중"])
    for _, row in plan.iterrows():
        lines.append(f"- {row['asset']}: {pct(row['weight'])} / {row['type']} / {row['reason']}")
    lines.extend(
        [
            "",
            "## 생성 파일",
            "- qg_core_plan.csv",
            "- qg_core_etf_scores.csv",
            "- qg_core_metrics.csv",
            "- qg_core_signals.csv",
            "- stock_scores.csv",
            "- dashboard.html",
        ]
    )
    text = "\n".join(lines)
    (paths.output / "report.md").write_text(text, encoding="utf-8-sig")
    html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Quant Guardian QG-Core 리포트</title>"
        "<style>body{font-family:Arial,'Malgun Gothic',sans-serif;background:#f4f6f9;color:#172033;max-width:960px;margin:0 auto;padding:32px 20px;line-height:1.6}"
        "pre{white-space:pre-wrap;background:#fff;border:1px solid #dfe5ef;border-radius:8px;padding:18px}</style>"
        "</head><body><pre>"
        + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre></body></html>"
    )
    (paths.output / "report.html").write_text(html, encoding="utf-8-sig")
    return text


def cmd_signal(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    regime = market_regime(cfg, paths, refresh=args.refresh)
    etfs = qg_core_etf_scores(cfg, paths, refresh=False)
    top = etfs.iloc[0] if not etfs.empty else None
    print(f"시장 모드: {regime['regime']} ({regime['score']}/{regime['max_score']})")
    if top is not None:
        print(f"QG-Core ETF 1순위: {execution_ticker(cfg, str(top['ticker']))} (신호 기준 {top['ticker']})")
        print(f"점수: {num(top['score'])}, 12-1개월 모멘텀: {pct(top['mom_12_1'])}")
    print("자동주문 없음. 실제 매매 전 직접 확인이 필요합니다.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    df = stock_scores(cfg, paths, refresh=args.refresh)
    cols = ["ticker", "sector", "quant_score", "status", "mom_12_1", "ret_6m", "rsi14", "reason"]
    print(df[cols].head(args.limit).to_string(index=False))
    print(f"저장 위치: {paths.output / 'stock_scores.csv'}")
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
    qg = qg_core_backtest(cfg, paths, refresh=args.refresh)
    for name, metrics in qg["metrics"].items():
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
            Quant Guardian QG-Core
            - 시장국면: 공격 / 중립 / 방어
            - 성장 ETF 코어: QQQM, SPYG, XLK/VGT, SMH/SOXX
            - 대형주 위성: 상위 5~8개 후보
            - 방어자산: SGOV, GLD, TLT

            자동주문은 없습니다.
            """
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("signal", help="Show current QG-Core signal")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_signal)
    p = sub.add_parser("scan", help="Show stock scanner")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("portfolio", help="Show QG-Core portfolio plan")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_portfolio)
    p = sub.add_parser("backtest", help="Run QG-Core backtest")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_backtest)
    p = sub.add_parser("report", help="Create markdown/html report")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
