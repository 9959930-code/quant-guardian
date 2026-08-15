from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import numpy as np
import pandas as pd

import equity_v2_isa_tiger_qld_research as research
from quant_guardian import fetch_text


_original_fetch_yahoo_price = research.fetch_yahoo_price
_original_calibrate_tiger_model = research.calibrate_tiger_model


def _fetch_naver_tiger_history(ticker: str) -> pd.DataFrame:
    symbol = ticker.split(".", 1)[0]
    url = (
        "https://fchart.stock.naver.com/sise.nhn"
        f"?symbol={symbol}&timeframe=day&count=3000&requestType=0"
    )
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 equity-v2-research/1.0",
            "Referer": "https://finance.naver.com/",
        },
    )
    with urlopen(request, timeout=90) as response:
        payload = response.read()
    text: str | None = None
    for encoding in ("utf-8", "euc-kr", "cp949"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError(f"Could not decode Naver price history for {ticker}")
    root = ElementTree.fromstring(text)
    rows: list[dict[str, object]] = []
    for item in root.iter("item"):
        values = str(item.attrib.get("data", "")).split("|")
        if len(values) < 6:
            continue
        date, open_value, high, low, close, volume = values[:6]
        rows.append(
            {
                "Date": pd.to_datetime(date, format="%Y%m%d", errors="coerce"),
                "Open": pd.to_numeric(open_value, errors="coerce"),
                "High": pd.to_numeric(high, errors="coerce"),
                "Low": pd.to_numeric(low, errors="coerce"),
                "Close": pd.to_numeric(close, errors="coerce"),
                "Volume": pd.to_numeric(volume, errors="coerce"),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"Naver returned empty prices for {ticker}")
    frame = (
        frame.dropna(subset=["Date", "Close"])
        .loc[lambda value: value["Close"] > 0]
        .drop_duplicates(subset=["Date"], keep="last")
        .sort_values("Date")
    )
    return frame.set_index("Date")


def _fetch_yahoo_price_preserving_krx_suffix(ticker: str) -> pd.DataFrame:
    if ticker == research.ACTUAL_TIGER_TICKER:
        return _fetch_naver_tiger_history(ticker)

    return _original_fetch_yahoo_price(ticker)


def _calibrate_official_double_krw_model(*args, **kwargs) -> pd.DataFrame:
    """Select the documented KRW-converted 2x benchmark with nonnegative drag.

    Cross-model rows remain in the diagnostic CSV, but the production choice is
    constrained to the issuer-documented double-KRW structure. Residual drag is
    also constrained to zero or above because a short favorable tracking period
    must not become a permanent positive alpha assumption in the 20-year splice.
    """
    table = _original_calibrate_tiger_model(*args, **kwargs)
    official = table.loc[
        (table["model"] == "double_krw")
        & (table["residual_drag"] >= -1e-12)
    ].sort_values(["score", "daily_rmse", "residual_drag"])
    diagnostics = table.loc[
        ~(
            (table["model"] == "double_krw")
            & (table["residual_drag"] >= -1e-12)
        )
    ].sort_values(["score", "daily_rmse", "residual_drag"])
    if official.empty:
        raise RuntimeError("official nonnegative-drag TIGER calibration is missing")
    return pd.concat([official, diagnostics], ignore_index=True)


research.fetch_yahoo_price = _fetch_yahoo_price_preserving_krx_suffix
research.calibrate_tiger_model = _calibrate_official_double_krw_model


if __name__ == "__main__":
    raise SystemExit(research.main())
