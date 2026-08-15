from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import numpy as np
import pandas as pd

import equity_v2_isa_tiger_qld_research as research
from quant_guardian import fetch_text


_original_fetch_yahoo_price = research.fetch_yahoo_price
_original_calibrate_tiger_model = research.calibrate_tiger_model


def _fetch_yahoo_price_preserving_krx_suffix(ticker: str) -> pd.DataFrame:
    if ticker != research.ACTUAL_TIGER_TICKER:
        return _original_fetch_yahoo_price(ticker)

    symbol = ticker.strip().upper()
    period2 = int((datetime.now(UTC) + timedelta(days=2)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        f"?period1=0&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    payload = json.loads(fetch_text(url, retries=3, pause=1.0))
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
    adjusted = (result.get("indicators", {}).get("adjclose") or [{}])[0].get(
        "adjclose"
    ) or raw_close
    if not timestamps or not raw_close:
        raise RuntimeError(f"Yahoo response missing prices for {ticker}")

    raw = pd.to_numeric(pd.Series(raw_close), errors="coerce")
    adj = pd.to_numeric(pd.Series(adjusted), errors="coerce")
    factor = (
        (adj / raw.replace(0, np.nan))
        .replace([np.inf, -np.inf], np.nan)
        .fillna(1.0)
    )

    def adjusted_column(name: str) -> pd.Series:
        values = quote_data.get(name) or [np.nan] * len(timestamps)
        return pd.to_numeric(pd.Series(values), errors="coerce") * factor

    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(timestamps, unit="s")
            .tz_localize("UTC")
            .tz_convert(None)
            .date,
            "Open": adjusted_column("open"),
            "High": adjusted_column("high"),
            "Low": adjusted_column("low"),
            "Close": adj,
            "Volume": quote_data.get("volume"),
        }
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = (
        frame.dropna(subset=["Date", "Close"])
        .drop_duplicates(subset=["Date"])
        .sort_values("Date")
    )
    if frame.empty:
        raise RuntimeError(f"Yahoo returned empty prices for {ticker}")
    return frame.set_index("Date")


def _calibrate_official_double_krw_model(*args, **kwargs) -> pd.DataFrame:
    """Keep cross-model diagnostics, but select only the official benchmark structure.

    TIGER 418660 follows the KRW-converted Nasdaq-100 leveraged index, so an
    empirical fit must not replace that documented structure with a different
    FX beta merely because a short actual-history RMSE is marginally lower.
    """
    table = _original_calibrate_tiger_model(*args, **kwargs)
    official = table.loc[table["model"] == "double_krw"].sort_values(
        ["score", "daily_rmse", "residual_drag"]
    )
    diagnostics = table.loc[table["model"] != "double_krw"].sort_values(
        ["score", "daily_rmse", "residual_drag"]
    )
    if official.empty:
        raise RuntimeError("official double_krw TIGER calibration is missing")
    return pd.concat([official, diagnostics], ignore_index=True)


research.fetch_yahoo_price = _fetch_yahoo_price_preserving_krx_suffix
research.calibrate_tiger_model = _calibrate_official_double_krw_model


if __name__ == "__main__":
    raise SystemExit(research.main())
