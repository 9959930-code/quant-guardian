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
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
USER_AGENT = "quant-guardian-v2/0.2 research-tool"


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
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", yahoo_symbol(ticker))
    return f"{source}_{safe}.csv"


def fetch_text(url: str, retries: int = 2, pause: float = 0.5) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=20) as resp:
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
    vol = returns.std(ddof=0) * math.sqrt(periods_per_year)
    sharpe = (returns.mean() * periods_per_year) / vol if vol > 0 else np.nan
    downside = returns[returns < 0].std(ddof=0) * math.sqrt(periods_per_year)
    sortino = (returns.mean() * periods_per_year) / downside if downside > 0 else np.nan
    mdd = max_drawdown(returns)
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    win_rate = float((returns > 0).mean())
    return {
        "cagr": float(cagr),
        "mdd": float(mdd),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "win_rate": win_rate,
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
    row = {}
    score = 0

    if market in close:
        spy = close[market].dropna()
        row["market_above_200d"] = bool(spy.iloc[-1] > spy.rolling(200).mean().iloc[-1])
        row["market_6m_return"] = float(spy.iloc[-1] / spy.iloc[-126] - 1) if len(spy) > 126 else np.nan
        score += int(row["market_above_200d"])
        score += int(row["market_6m_return"] > 0)
    else:
        row["market_above_200d"] = None
        row["market_6m_return"] = np.nan

    if growth in close:
        qqq = close[growth].dropna()
        row["growth_above_200d"] = bool(qqq.iloc[-1] > qqq.rolling(200).mean().iloc[-1])
        score += int(row["growth_above_200d"])
    else:
        row["growth_above_200d"] = None

    if vix in close:
        vix_last = float(close[vix].dropna().iloc[-1])
        row["vix"] = vix_last
        row["vix_ok"] = bool(vix_last < float(mr["risk_on_vix"]))
        score += int(row["vix_ok"])
    else:
        row["vix"] = np.nan
        row["vix_ok"] = None

    if score >= 3:
        regime = "공격"
        profile = "aggressive"
    elif score == 2:
        regime = "중립"
        profile = "neutral"
    else:
        regime = "방어"
        profile = "defensive"

    row.update(
        {
            "as_of": close.index[-1].date().isoformat(),
            "score": score,
            "regime": regime,
            "profile": profile,
            "data_errors": close.attrs.get("errors", []),
        }
    )
    return row


def score_stock(close: pd.Series, market_regime_name: str = "중립") -> dict:
    close = close.dropna()
    if len(close) < 253:
        return {"error": "not enough data"}
    last = float(close.iloc[-1])
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

    momentum_score = 35 * (
        0.55 * clamp((mom_12_1 + 0.10) / 0.60, 0, 1)
        + 0.30 * clamp((ret_6m + 0.05) / 0.40, 0, 1)
        + 0.15 * clamp((ret_3m + 0.03) / 0.25, 0, 1)
    )
    trend_score = 25 * (
        0.40 * int(last > sma50)
        + 0.40 * int(last > sma200)
        + 0.20 * int(sma50 > sma200)
    )
    risk_score = 20 * (
        0.55 * clamp((0.60 - vol63) / 0.50, 0, 1)
        + 0.45 * clamp((dd252 + 0.45) / 0.45, 0, 1)
    )
    if 45 <= rsi14 <= 65:
        timing_score = 10
    elif 35 <= rsi14 < 45 or 65 < rsi14 <= 72:
        timing_score = 7
    elif rsi14 > 72:
        timing_score = 3
    else:
        timing_score = 4
    regime_score = {"공격": 10, "중립": 6, "방어": 2}.get(market_regime_name, 6)
    total = clamp(momentum_score + trend_score + risk_score + timing_score + regime_score, 0, 100)

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
        reasons.append("12-1 모멘텀 양호")
    if last > sma200 and sma50 > sma200:
        reasons.append("중기 추세 우상향")
    if rsi14 > 72:
        reasons.append("RSI 과열")
    if dd252 < -0.30:
        reasons.append("낙폭 큼")
    if vol63 > 0.55:
        reasons.append("변동성 높음")
    if not reasons:
        reasons.append("특별한 우위 약함")

    return {
        "last": last,
        "ret_3m": ret_3m,
        "ret_6m": ret_6m,
        "ret_12m": ret_12m,
        "mom_12_1": mom_12_1,
        "sma50": sma50,
        "sma200": sma200,
        "vol63": vol63,
        "drawdown_252d": dd252,
        "rsi14": rsi14,
        "momentum_score": momentum_score,
        "trend_score": trend_score,
        "risk_score": risk_score,
        "timing_score": timing_score,
        "regime_score": regime_score,
        "quant_score": total,
        "status": status,
        "reason": ", ".join(reasons),
    }


def stock_scores(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    regime = market_regime(cfg, paths, refresh=refresh)
    tickers = [x.upper() for x in cfg["universe"]["tickers"]]
    close = price_panel(tickers, paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    start = pd.Timestamp(cfg["stock_strategy"]["start_date"])
    close = close.loc[close.index >= start]
    rows = []
    for ticker in tickers:
        if ticker not in close:
            continue
        row = {"ticker": ticker}
        row.update(score_stock(close[ticker], regime["regime"]))
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df.get("error").isna() if "error" in df else [True] * len(df)]
        df = df.sort_values(["quant_score", "mom_12_1"], ascending=False)
    errors = pd.DataFrame({"error": close.attrs.get("errors", [])})
    errors.to_csv(paths.output / "data_errors.csv", index=False, encoding="utf-8-sig")
    df.to_csv(paths.output / "stock_scores.csv", index=False, encoding="utf-8-sig")
    return df


def portfolio_plan(cfg: dict, paths: Paths, refresh: bool = False) -> pd.DataFrame:
    regime = market_regime(cfg, paths, refresh=False)
    signal = latest_etf_signal(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=refresh)
    profile = cfg["portfolio_profiles"][regime["profile"]]
    st = cfg["stock_strategy"]
    max_positions = int(st["max_positions"])
    max_single = float(st["max_single_stock_weight"])
    stock_budget = float(profile["stock_weight"])
    etf_weight = float(profile["etf_weight"])
    cash_weight = float(profile["cash_weight"])

    rows = []
    etf_symbol = signal["current_signal"]
    if regime["regime"] == "방어":
        etf_symbol = cfg["etf_strategy"]["safety_asset"].upper()
    rows.append(
        {
            "asset": etf_symbol,
            "type": "ETF 코어",
            "weight": etf_weight,
            "reason": f"{regime['regime']} 모드 ETF 코어",
        }
    )

    candidates = scores[(scores["status"] == "매수후보") & (scores["quant_score"] >= float(st["min_score"]))].head(max_positions)
    if stock_budget > 0 and not candidates.empty:
        raw = candidates["quant_score"] / candidates["quant_score"].sum()
        weights = (raw * stock_budget).clip(upper=max_single)
        used = float(weights.sum())
        for (_, row), weight in zip(candidates.iterrows(), weights):
            rows.append(
                {
                    "asset": row["ticker"],
                    "type": "개별주 위성",
                    "weight": float(weight),
                    "reason": row["reason"],
                }
            )
        cash_weight += stock_budget - used
    else:
        cash_weight += stock_budget

    if cash_weight > 0:
        rows.append({"asset": "CASH/SHY", "type": "현금/대기", "weight": cash_weight, "reason": "미사용 위험 예산"})

    plan = pd.DataFrame(rows)
    plan.to_csv(paths.output / "portfolio_plan.csv", index=False, encoding="utf-8-sig")
    return plan


def stock_rotation_backtest(cfg: dict, paths: Paths, refresh: bool = False) -> dict:
    tickers = [x.upper() for x in cfg["universe"]["tickers"]]
    bench = ["SPY", "QQQ"]
    close = price_panel([*tickers, *bench], paths, refresh=refresh, source=cfg["settings"].get("data_source", "yahoo"))
    start = pd.Timestamp(cfg["stock_strategy"]["rotation_start_date"])
    monthly = close.loc[close.index >= start].resample("ME").last().ffill()
    returns = monthly.pct_change()
    top_n = int(cfg["stock_strategy"]["rotation_top_n"])
    rows = []
    strat_returns = []
    dates = []

    for i in range(13, len(monthly) - 1):
        signal_date = monthly.index[i]
        hold_date = monthly.index[i + 1]
        price_now = monthly.iloc[i]
        mom = monthly.iloc[i - 1] / monthly.iloc[i - 12] - 1
        trend = price_now / monthly.iloc[: i + 1].rolling(10).mean().iloc[i] - 1
        rank_score = mom + trend.clip(lower=-0.2, upper=0.2)
        tradable = [ticker for ticker in tickers if ticker in rank_score.index]
        available = rank_score[tradable].replace([np.inf, -np.inf], np.nan).dropna()
        available = available[available > 0]
        selected = available.sort_values(ascending=False).head(top_n).index.tolist()
        if selected:
            ret = float(returns.loc[hold_date, selected].mean())
        else:
            ret = 0.0
        rows.append({"date": hold_date, "selected": ",".join(selected), "strategy_return": ret})
        dates.append(hold_date)
        strat_returns.append(ret)

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
    etf = etf_backtest(cfg, paths, refresh=refresh)
    signal = latest_etf_signal(cfg, paths, refresh=False)
    regime = market_regime(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=refresh)
    plan = portfolio_plan(cfg, paths, refresh=False)
    rotation = stock_rotation_backtest(cfg, paths, refresh=refresh)
    etf_metrics = etf["metrics"]["STRATEGY"]
    rotation_metrics = rotation["metrics"].get("STOCK_ROTATION", {})

    lines = [
        "# 퀀트 가디언 v2 리포트",
        "",
        f"생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "이 도구는 투자 후보를 찾고 리스크를 점검하는 연구용 사이트입니다. 자동 주문은 없습니다.",
        "",
        "## 시장 모드",
        f"- 현재 모드: {regime['regime']} ({regime['score']}점)",
        f"- 기준일: {regime['as_of']}",
        f"- SPY 6개월 수익률: {pct(regime.get('market_6m_return'))}",
        f"- VIX: {num(regime.get('vix'))}",
        "",
        "## ETF 코어",
        f"- 현재 신호: {signal['current_signal']}",
        f"- 12개월 모멘텀: {pct(signal['latest_momentum'])}",
        f"- ETF 전략 CAGR: {pct(etf_metrics['cagr'])}",
        f"- ETF 전략 MDD: {pct(etf_metrics['mdd'])}",
        f"- ETF 전략 Sharpe: {num(etf_metrics['sharpe'])}",
        "",
        "## 개별주 로테이션 백테스트",
        f"- CAGR: {pct(rotation_metrics.get('cagr'))}",
        f"- MDD: {pct(rotation_metrics.get('mdd'))}",
        f"- Sharpe: {num(rotation_metrics.get('sharpe'))}",
        "",
        "## 상위 퀀트 후보",
    ]
    for _, row in scores.head(10).iterrows():
        lines.append(
            f"- {row['ticker']}: {num(row['quant_score'])}점, {row['status']}, "
            f"12-1 모멘텀 {pct(row['mom_12_1'])}, RSI {num(row['rsi14'])}, {row['reason']}"
        )
    lines.extend(["", "## 포트폴리오 제안"])
    for _, row in plan.iterrows():
        lines.append(f"- {row['asset']}: {pct(row['weight'])} / {row['type']} / {row['reason']}")
    lines.extend(
        [
            "",
            "## 생성 파일",
            "- stock_scores.csv",
            "- portfolio_plan.csv",
            "- stock_rotation_metrics.csv",
            "- stock_rotation_signals.csv",
            "- etf_metrics.csv",
            "- dashboard.html",
        ]
    )
    text = "\n".join(lines)
    (paths.output / "report.md").write_text(text, encoding="utf-8-sig")
    html = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>퀀트 가디언 v2 리포트</title>"
        "<style>body{font-family:Arial,'Malgun Gothic',sans-serif;background:#f4f6f9;color:#172033;"
        "max-width:960px;margin:0 auto;padding:32px 20px;line-height:1.6}"
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
    signal = latest_etf_signal(cfg, paths, refresh=False)
    print(f"시장 모드: {regime['regime']} ({regime['score']}점)")
    print(f"{signal['as_of']} 기준 ETF 신호: {signal['current_signal']}")
    print(f"최근 12개월 모멘텀: {pct(signal['latest_momentum'])}")
    print("이 도구는 자동 주문을 하지 않습니다.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    df = stock_scores(cfg, paths, refresh=args.refresh)
    cols = ["ticker", "quant_score", "status", "mom_12_1", "rsi14", "reason"]
    print(df[cols].head(args.limit).to_string(index=False))
    print(f"저장 위치: {paths.output / 'stock_scores.csv'}")
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    plan = portfolio_plan(cfg, paths, refresh=args.refresh)
    print(plan.to_string(index=False))
    print(f"저장 위치: {paths.output / 'portfolio_plan.csv'}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    paths = resolve_paths(cfg)
    etf = etf_backtest(cfg, paths, refresh=args.refresh)
    rotation = stock_rotation_backtest(cfg, paths, refresh=False)
    print("ETF 전략")
    for key, value in etf["metrics"]["STRATEGY"].items():
        print(f"- {key}: {pct(value) if key in {'cagr', 'mdd', 'win_rate', 'total_return'} else num(value)}")
    print("\n개별주 로테이션")
    for key, value in rotation["metrics"]["STOCK_ROTATION"].items():
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
            Quant Guardian v2
            - 시장 모드
            - ETF 코어 신호
            - 미국 대형주 퀀트 스캐너
            - 포트폴리오 제안
            - 백테스트

            자동 주문은 없습니다.
            """
        ),
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "signal": cmd_signal,
        "scan": cmd_scan,
        "portfolio": cmd_portfolio,
        "backtest": cmd_backtest,
        "report": cmd_report,
    }
    for name, handler in commands.items():
        sp = sub.add_parser(name)
        sp.set_defaults(func=handler)
        sp.add_argument("--refresh", action="store_true", help="무료 가격 데이터를 새로 받기")
        if name == "scan":
            sp.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
