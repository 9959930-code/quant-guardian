from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from quant_guardian import read_price


TRADING_DAYS = 252
RISK_ASSETS = ("SPY", "QQQ", "XLK", "SOXX", "SMH", "QLD", "TQQQ")
DEFENSIVE_ASSETS = ("CASH", "GLD", "IEF", "TLT")
LEVERAGED_ASSETS = frozenset({"QLD", "TQQQ"})
ALL_DOWNLOAD_TICKERS = (
    "SPY",
    "QQQ",
    "XLK",
    "SOXX",
    "SMH",
    "QLD",
    "TQQQ",
    "GLD",
    "IEF",
    "TLT",
    "^IRX",
    "KRW=X",
)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    track: str
    params: Mapping[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "track": self.track,
            "params_json": json.dumps(
                dict(self.params), ensure_ascii=False, sort_keys=True
            ),
        }


@dataclass
class SimulationResult:
    daily: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]


class FeatureStore:
    def __init__(self, close: pd.DataFrame):
        self.close = close
        self._sma: dict[tuple[str, int], pd.Series] = {}
        self._ret: dict[tuple[str, int], pd.Series] = {}
        self._vol: dict[tuple[str, int], pd.Series] = {}
        self._dd: dict[tuple[str, int], pd.Series] = {}
        self._high: dict[tuple[str, int], pd.Series] = {}

    def sma(self, ticker: str, window: int) -> pd.Series:
        key = (ticker, int(window))
        if key not in self._sma:
            self._sma[key] = self.close[ticker].rolling(window).mean()
        return self._sma[key]

    def ret(self, ticker: str, days: int) -> pd.Series:
        key = (ticker, int(days))
        if key not in self._ret:
            self._ret[key] = self.close[ticker].pct_change(days)
        return self._ret[key]

    def vol(self, ticker: str, days: int) -> pd.Series:
        key = (ticker, int(days))
        if key not in self._vol:
            self._vol[key] = (
                self.close[ticker]
                .pct_change()
                .rolling(days)
                .std(ddof=0)
                * math.sqrt(TRADING_DAYS)
            )
        return self._vol[key]

    def rolling_high(self, ticker: str, days: int) -> pd.Series:
        key = (ticker, int(days))
        if key not in self._high:
            self._high[key] = self.close[ticker].rolling(days).max()
        return self._high[key]

    def drawdown(self, ticker: str, days: int) -> pd.Series:
        key = (ticker, int(days))
        if key not in self._dd:
            high = self.rolling_high(ticker, days)
            self._dd[key] = self.close[ticker] / high - 1
        return self._dd[key]


def _coerce_ohlc(frame: pd.DataFrame, master: pd.DatetimeIndex) -> pd.DataFrame:
    out = frame.copy().sort_index()
    out.index = pd.DatetimeIndex(out.index).tz_localize(None)
    out = out.reindex(master)
    for column in ("Open", "Close"):
        if column not in out:
            out[column] = np.nan
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return out[["Open", "Close"]]


def load_market_data(
    *,
    cfg: dict,
    paths: Any,
    refresh: bool,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    source = cfg.get("settings", {}).get("data_source", "yahoo")
    raw: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for ticker in ALL_DOWNLOAD_TICKERS:
        try:
            raw[ticker] = read_price(
                ticker, paths, refresh=refresh, source=source
            )
        except Exception as exc:  # pragma: no cover - live provider path
            errors.append(f"{ticker}: {exc}")

    if "SPY" not in raw:
        raise RuntimeError("SPY data is required for the master trading calendar")
    master = pd.DatetimeIndex(raw["SPY"].index).tz_localize(None)
    frames: dict[str, pd.DataFrame] = {}
    for ticker in ALL_DOWNLOAD_TICKERS:
        if ticker in raw:
            frames[ticker] = _coerce_ohlc(raw[ticker], master)

    if "^IRX" in frames:
        yield_pct = frames["^IRX"]["Close"].ffill()
        cash_daily = (yield_pct.shift(1) / 100 / TRADING_DAYS).clip(
            lower=-0.01, upper=0.01
        )
    else:
        cash_daily = pd.Series(0.0, index=master)
    cash_close = (1 + cash_daily.fillna(0)).cumprod() * 100.0
    cash_open = cash_close.shift(1).fillna(cash_close.iloc[0])
    frames["CASH"] = pd.DataFrame(
        {"Open": cash_open, "Close": cash_close}, index=master
    )

    metadata = {
        "errors": errors,
        "master_start": master.min().date().isoformat(),
        "master_end": master.max().date().isoformat(),
        "ticker_ranges": {
            ticker: {
                "start": (
                    frame["Close"].dropna().index.min().date().isoformat()
                    if not frame["Close"].dropna().empty
                    else None
                ),
                "end": (
                    frame["Close"].dropna().index.max().date().isoformat()
                    if not frame["Close"].dropna().empty
                    else None
                ),
            }
            for ticker, frame in frames.items()
        },
    }
    return frames, metadata


def close_panel(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(
        {
            ticker: frame["Close"]
            for ticker, frame in frames.items()
            if ticker != "^IRX"
        },
        axis=1,
    ).sort_index()


def schedule_dates(index: pd.DatetimeIndex, frequency: str) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(index)
    if frequency == "daily":
        return index
    series = pd.Series(index=index, data=index)
    if frequency == "weekly":
        grouped = series.groupby(index.to_period("W-FRI"))
    elif frequency == "monthly":
        grouped = series.groupby(index.to_period("M"))
    elif frequency == "quarterly":
        grouped = series.groupby(index.to_period("Q"))
    elif frequency == "annual":
        grouped = series.groupby(index.to_period("Y"))
    else:
        raise ValueError(f"Unsupported frequency: {frequency}")
    values = [pd.Timestamp(group.index.max()) for _, group in grouped]
    return pd.DatetimeIndex(values)


def candidate_required_assets(candidate: Candidate) -> set[str]:
    params = dict(candidate.params)
    required: set[str] = {"CASH"}
    for key in (
        "asset",
        "signal_asset",
        "leverage_asset",
        "moderate_asset",
    ):
        value = params.get(key)
        if value:
            required.add(str(value))
    for key in ("assets", "universe", "mix"):
        value = params.get(key)
        if isinstance(value, Mapping):
            required.update(str(item) for item in value)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            required.update(str(item) for item in value)
    if params.get("defense") == "best":
        required.update({"GLD", "IEF", "TLT"})
    else:
        defense = params.get("defense")
        if defense and defense != "CASH":
            required.add(str(defense))
    return required


def candidate_track(candidate: Candidate) -> str:
    assets = candidate_required_assets(candidate)
    if "TQQQ" in assets:
        return "tqqq_actual"
    if "QLD" in assets:
        return "qld_20y"
    if "SMH" in assets:
        return "smh_common"
    return "core_20y"


def first_valid_date(
    frames: Mapping[str, pd.DataFrame],
    assets: Iterable[str],
    minimum: str | pd.Timestamp,
) -> pd.Timestamp:
    starts = [pd.Timestamp(minimum)]
    for asset in assets:
        if asset not in frames:
            raise KeyError(f"Missing asset: {asset}")
        valid = frames[asset][["Open", "Close"]].dropna()
        if valid.empty:
            raise ValueError(f"No valid prices for {asset}")
        starts.append(pd.Timestamp(valid.index.min()))
    return max(starts)


def choose_defense(
    date: pd.Timestamp,
    store: FeatureStore,
    mode: str,
    *,
    lookback: int = 126,
) -> dict[str, float]:
    if mode != "best":
        return {mode: 1.0}
    candidates = ("GLD", "IEF", "TLT")
    scores: list[tuple[float, str]] = []
    for ticker in candidates:
        value = store.ret(ticker, lookback).get(date, np.nan)
        trend = store.close[ticker].get(date, np.nan)
        sma = store.sma(ticker, 200).get(date, np.nan)
        if pd.notna(value) and pd.notna(trend) and pd.notna(sma):
            if value > 0 and trend > sma:
                scores.append((float(value), ticker))
    if not scores:
        return {"CASH": 1.0}
    scores.sort(reverse=True)
    return {scores[0][1]: 1.0}


def _normalize_weights(weights: Mapping[str, float]) -> dict[str, float]:
    cleaned = {
        str(ticker): max(0.0, float(weight))
        for ticker, weight in weights.items()
        if float(weight) > 1e-12
    }
    total = sum(cleaned.values())
    if total < 1 - 1e-10:
        cleaned["CASH"] = cleaned.get("CASH", 0.0) + (1 - total)
        total = 1.0
    if total <= 0:
        return {"CASH": 1.0}
    return {ticker: weight / total for ticker, weight in cleaned.items()}


def _append_if_changed(
    rows: list[dict[str, Any]],
    date: pd.Timestamp,
    weights: Mapping[str, float],
) -> None:
    normalized = _normalize_weights(weights)
    if rows:
        previous = rows[-1]["weights"]
        keys = set(previous) | set(normalized)
        if all(
            abs(previous.get(key, 0.0) - normalized.get(key, 0.0)) < 1e-10
            for key in keys
        ):
            return
    rows.append({"date": pd.Timestamp(date), "weights": normalized})


def targets_to_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    assets = sorted(
        {
            ticker
            for row in rows
            for ticker in dict(row["weights"]).keys()
        }
    )
    records = []
    for row in rows:
        record = {"Date": pd.Timestamp(row["date"])}
        record.update({asset: float(row["weights"].get(asset, 0.0)) for asset in assets})
        records.append(record)
    frame = pd.DataFrame(records).set_index("Date").sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def _asset_spec(asset: str, signal_mode: str) -> tuple[str, str]:
    if asset in LEVERAGED_ASSETS and signal_mode == "underlying":
        return asset, "QQQ"
    return asset, asset


def generate_targets(
    candidate: Candidate,
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    family = candidate.family
    p = dict(candidate.params)
    index = store.close.loc[start:end].index
    rows: list[dict[str, Any]] = []
    if len(index) < 3:
        return pd.DataFrame()

    if family == "buy_hold":
        _append_if_changed(rows, index[0], {str(p["asset"]): 1.0})
        return targets_to_frame(rows)

    if family == "fixed_mix":
        mix = {str(k): float(v) for k, v in dict(p["mix"]).items()}
        frequency = str(p.get("frequency", "annual"))
        if frequency == "none":
            dates = pd.DatetimeIndex([index[0]])
        else:
            dates = schedule_dates(index, frequency)
        for date in dates:
            _append_if_changed(rows, date, mix)
        return targets_to_frame(rows)

    if family in {"trend_hold", "ma_momentum"}:
        asset, signal_asset = _asset_spec(
            str(p["asset"]), str(p.get("signal_mode", "self"))
        )
        dates = schedule_dates(index, str(p["frequency"]))
        sma = store.sma(signal_asset, int(p["sma"]))
        slope_days = int(p.get("slope_days", 0))
        momentum_days = int(p.get("momentum_days", 0))
        for date in dates:
            close_value = store.close[signal_asset].get(date, np.nan)
            sma_value = sma.get(date, np.nan)
            if pd.isna(close_value) or pd.isna(sma_value):
                continue
            risk_on = bool(close_value > sma_value)
            if slope_days:
                prior_sma = sma.shift(slope_days).get(date, np.nan)
                risk_on = risk_on and pd.notna(prior_sma) and sma_value > prior_sma
            if family == "ma_momentum":
                momentum = store.ret(signal_asset, momentum_days).get(date, np.nan)
                risk_on = risk_on and pd.notna(momentum) and momentum > 0
            weights = (
                {asset: 1.0}
                if risk_on
                else choose_defense(date, store, str(p.get("defense", "CASH")))
            )
            _append_if_changed(rows, date, weights)
        return targets_to_frame(rows)

    if family == "rotation":
        universe = [str(item) for item in p["universe"]]
        dates = schedule_dates(index, str(p["frequency"]))
        lookbacks = [int(item) for item in p["lookbacks"]]
        top_n = int(p["top_n"])
        filter_mode = str(p["filter"])
        score_mode = str(p["score_mode"])
        leverage_cap = float(p.get("leverage_cap", 1.0))
        for date in dates:
            scored: list[tuple[float, str]] = []
            for ticker in universe:
                values = [store.ret(ticker, days).get(date, np.nan) for days in lookbacks]
                if any(pd.isna(value) for value in values):
                    continue
                raw = float(np.mean(values))
                if score_mode == "risk_adjusted":
                    vol = store.vol(ticker, 126).get(date, np.nan)
                    if pd.isna(vol) or vol <= 0:
                        continue
                    score = raw / float(vol)
                else:
                    score = raw
                allowed = True
                if filter_mode in {"momentum", "both"}:
                    allowed = allowed and values[-1] > 0
                if filter_mode in {"sma", "both"}:
                    close_value = store.close[ticker].get(date, np.nan)
                    sma = store.sma(ticker, 200).get(date, np.nan)
                    allowed = allowed and pd.notna(sma) and close_value > sma
                if allowed:
                    scored.append((score, ticker))
            scored.sort(reverse=True)
            selected = [ticker for _, ticker in scored[:top_n]]
            if not selected:
                weights = choose_defense(
                    date, store, str(p.get("defense", "CASH"))
                )
            else:
                base_weight = 1.0 / len(selected)
                weights: dict[str, float] = {}
                spill = 0.0
                for ticker in selected:
                    weight = base_weight
                    if ticker in LEVERAGED_ASSETS and weight > leverage_cap:
                        spill += weight - leverage_cap
                        weight = leverage_cap
                    weights[ticker] = weights.get(ticker, 0.0) + weight
                if spill > 0:
                    weights["QQQ"] = weights.get("QQQ", 0.0) + spill
            _append_if_changed(rows, date, weights)
        return targets_to_frame(rows)

    if family in {"drawdown_recovery", "pullback_hold", "breakout_hold"}:
        asset, signal_asset = _asset_spec(
            str(p["asset"]), str(p.get("signal_mode", "self"))
        )
        dates = schedule_dates(index, str(p["frequency"]))
        entry_parts = int(p.get("entry_parts", 1))
        exit_parts = int(p.get("exit_parts", entry_parts))
        recovery_sma = store.sma(signal_asset, int(p.get("recovery_sma", 50)))
        exit_sma_window = int(p.get("exit_sma", 200))
        exit_sma = store.sma(signal_asset, exit_sma_window)
        state = "out"
        armed = False
        stage = 0
        peak = -np.inf
        for date in dates:
            close_value = store.close[signal_asset].get(date, np.nan)
            if pd.isna(close_value):
                continue

            if state == "out":
                if family == "drawdown_recovery":
                    dd = store.drawdown(signal_asset, 252).get(date, np.nan)
                    if pd.notna(dd) and dd <= float(p["drawdown"]):
                        armed = True
                    entry_ok = armed and close_value > recovery_sma.get(date, np.nan)
                elif family == "pullback_hold":
                    dd = store.drawdown(signal_asset, 252).get(date, np.nan)
                    long_sma = store.sma(signal_asset, 200)
                    long_ok = (
                        close_value > long_sma.get(date, np.nan)
                        and long_sma.get(date, np.nan)
                        > long_sma.shift(int(p.get("slope_days", 20))).get(
                            date, np.nan
                        )
                    )
                    if pd.notna(dd) and dd <= float(p["drawdown"]) and long_ok:
                        armed = True
                    entry_ok = (
                        armed
                        and long_ok
                        and close_value > recovery_sma.get(date, np.nan)
                    )
                else:
                    breakout_days = int(p["breakout_days"])
                    prior_high = store.rolling_high(
                        signal_asset, breakout_days
                    ).shift(1).get(date, np.nan)
                    long_ok = True
                    if bool(p.get("require_long_trend", True)):
                        long_ok = close_value > store.sma(
                            signal_asset, 200
                        ).get(date, np.nan)
                    entry_ok = (
                        pd.notna(prior_high)
                        and close_value >= prior_high
                        and long_ok
                    )
                if entry_ok:
                    state = "entering"
                    stage = 1
                    peak = close_value
                    _append_if_changed(
                        rows,
                        date,
                        {
                            asset: stage / entry_parts,
                            "CASH": 1 - stage / entry_parts,
                        },
                    )
                    if entry_parts == 1:
                        state = "holding"
                    armed = False
                continue

            if state == "entering":
                stage += 1
                fraction = min(1.0, stage / entry_parts)
                _append_if_changed(
                    rows, date, {asset: fraction, "CASH": 1 - fraction}
                )
                peak = max(peak, close_value)
                if stage >= entry_parts:
                    state = "holding"
                continue

            if state == "holding":
                peak = max(peak, close_value)
                exit_rule = str(p["exit_rule"])
                if exit_rule == "sma":
                    exit_ok = close_value < exit_sma.get(date, np.nan)
                else:
                    exit_ok = close_value / peak - 1 <= float(p["trailing_stop"])
                if exit_ok:
                    state = "exiting"
                    stage = 1
                    fraction = max(0.0, 1 - stage / exit_parts)
                    _append_if_changed(
                        rows,
                        date,
                        {asset: fraction, "CASH": 1 - fraction},
                    )
                    if exit_parts == 1:
                        state = "out"
                        peak = -np.inf
                continue

            if state == "exiting":
                stage += 1
                fraction = max(0.0, 1 - stage / exit_parts)
                _append_if_changed(
                    rows, date, {asset: fraction, "CASH": 1 - fraction}
                )
                if stage >= exit_parts:
                    state = "out"
                    peak = -np.inf
        return targets_to_frame(rows)

    if family == "leveraged_regime":
        dates = schedule_dates(index, str(p["frequency"]))
        qqq = store.close["QQQ"]
        sma = store.sma("QQQ", int(p["sma"]))
        momentum = store.ret("QQQ", int(p["momentum_days"]))
        vol = store.vol("QQQ", int(p.get("vol_days", 63)))
        mode = str(p["mode"])
        leverage_asset = str(p.get("leverage_asset", "QLD"))
        threshold = float(p["strong_threshold"])
        max_vol = float(p["max_vol"])
        for date in dates:
            values = (
                qqq.get(date, np.nan),
                sma.get(date, np.nan),
                momentum.get(date, np.nan),
                vol.get(date, np.nan),
            )
            if any(pd.isna(value) for value in values):
                continue
            close_value, sma_value, mom_value, vol_value = map(float, values)
            if close_value <= sma_value or mom_value <= 0:
                weights = choose_defense(
                    date, store, str(p.get("defense", "CASH"))
                )
            elif mode == "single":
                selected = (
                    leverage_asset
                    if mom_value >= threshold and vol_value <= max_vol
                    else "QQQ"
                )
                weights = {selected: 1.0}
            else:
                if mom_value >= threshold and vol_value <= max_vol:
                    selected = "TQQQ"
                elif mom_value > 0:
                    selected = str(p.get("moderate_asset", "QLD"))
                else:
                    selected = "QQQ"
                weights = {selected: 1.0}
            _append_if_changed(rows, date, weights)
        return targets_to_frame(rows)

    raise ValueError(f"Unsupported candidate family: {family}")


def effective_weight_panel(
    targets: pd.DataFrame,
    *,
    index: pd.DatetimeIndex,
    assets: Sequence[str],
    shift_days: int = 1,
) -> pd.DataFrame:
    panel = pd.DataFrame(index=index, columns=list(assets), dtype=float)
    if targets.empty:
        panel.loc[:, :] = 0.0
        panel["CASH"] = 1.0
        return panel
    aligned = targets.reindex(index)
    aligned = aligned.reindex(columns=list(assets), fill_value=0.0)
    panel = aligned.ffill().shift(shift_days).fillna(0.0)
    row_sum = panel.sum(axis=1)
    panel["CASH"] = panel.get("CASH", 0.0) + (1 - row_sum).clip(lower=0)
    row_sum = panel.sum(axis=1).replace(0, np.nan)
    panel = panel.div(row_sum, axis=0).fillna(0.0)
    panel.loc[panel.sum(axis=1) == 0, "CASH"] = 1.0
    return panel


def _metrics_from_equity(
    equity: pd.Series,
    *,
    trades: int,
    turnover: float,
    exposure: float,
    leveraged_exposure: float,
) -> dict[str, Any]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {
            "total_return": np.nan,
            "cagr": np.nan,
            "mdd": np.nan,
            "sharpe": np.nan,
            "sortino": np.nan,
            "calmar": np.nan,
            "recovery_days": np.nan,
            "trades": trades,
            "trades_per_year": np.nan,
            "turnover": turnover,
            "average_exposure": exposure,
            "leveraged_exposure": leveraged_exposure,
        }
    daily = equity.pct_change().dropna()
    elapsed_days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = elapsed_days / 365.25
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    dd = equity / equity.cummax() - 1
    mdd = float(dd.min())
    vol = float(daily.std(ddof=0) * math.sqrt(TRADING_DAYS))
    sharpe = float(daily.mean() * TRADING_DAYS / vol) if vol > 0 else np.nan
    downside = float(daily[daily < 0].std(ddof=0) * math.sqrt(TRADING_DAYS))
    sortino = (
        float(daily.mean() * TRADING_DAYS / downside)
        if downside > 0
        else np.nan
    )
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan

    peak = equity.cummax()
    underwater = equity < peak
    longest = 0
    start = None
    for date, flag in underwater.items():
        if flag and start is None:
            start = date
        elif not flag and start is not None:
            longest = max(longest, (date - start).days)
            start = None
    if start is not None:
        longest = max(longest, (equity.index[-1] - start).days)

    return {
        "total_return": total,
        "cagr": cagr,
        "mdd": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "recovery_days": int(longest),
        "trades": int(trades),
        "trades_per_year": trades / years if years > 0 else np.nan,
        "turnover": float(turnover),
        "average_exposure": float(exposure),
        "leveraged_exposure": float(leveraged_exposure),
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
    }


def simulate_close_to_close(
    *,
    targets: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
) -> SimulationResult:
    required = sorted(set(targets.columns) | {"CASH"})
    index = frames["SPY"].loc[start:end].index
    close = pd.concat(
        {asset: frames[asset]["Close"].reindex(index) for asset in required},
        axis=1,
    )
    valid = close.dropna().index
    if len(valid) < 3:
        return SimulationResult(
            pd.DataFrame(), pd.DataFrame(), _metrics_from_equity(
                pd.Series(dtype=float),
                trades=0,
                turnover=0,
                exposure=0,
                leveraged_exposure=0,
            )
        )
    index = valid
    close = close.loc[index]
    weights = effective_weight_panel(
        targets, index=index, assets=required, shift_days=1
    )
    returns = close.pct_change().fillna(0.0)
    prior_weights = weights.shift(1).fillna(0.0)
    asset_turnover = (weights - prior_weights).abs()
    traded = asset_turnover.drop(columns=["CASH"], errors="ignore").sum(axis=1)
    costs = traded * (cost_bps / 10_000)
    portfolio_return = (weights * returns).sum(axis=1) - costs
    equity = (1 + portfolio_return).cumprod()
    daily = pd.DataFrame(
        {
            "equity": equity,
            "daily_return": portfolio_return,
            "cash_weight": weights["CASH"],
            "risk_weight": 1 - weights["CASH"],
            "leveraged_weight": weights.reindex(
                columns=sorted(LEVERAGED_ASSETS), fill_value=0.0
            ).sum(axis=1),
        }
    )
    trade_mask = traded > 1e-8
    trades = pd.DataFrame(
        {
            "date": index[trade_mask],
            "turnover": traded.loc[trade_mask].values,
        }
    )
    metrics = _metrics_from_equity(
        equity,
        trades=int(trade_mask.sum()),
        turnover=float(traded.sum()),
        exposure=float((1 - weights["CASH"]).mean()),
        leveraged_exposure=float(
            weights.reindex(
                columns=sorted(LEVERAGED_ASSETS), fill_value=0.0
            ).sum(axis=1).mean()
        ),
    )
    return SimulationResult(daily, trades, metrics)


def _next_execution_date(
    index: pd.DatetimeIndex,
    signal_date: pd.Timestamp,
    delay_days: int,
) -> pd.Timestamp | None:
    position = int(index.searchsorted(signal_date, side="right")) + delay_days
    if position >= len(index):
        return None
    return pd.Timestamp(index[position])


def simulate_next_open(
    *,
    targets: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
    delay_days: int = 0,
) -> SimulationResult:
    required = sorted(set(targets.columns) | {"CASH"})
    master = frames["SPY"].loc[start:end].index
    valid_mask = pd.Series(True, index=master)
    for asset in required:
        valid_mask &= (
            frames[asset]["Open"].reindex(master).notna()
            & frames[asset]["Close"].reindex(master).notna()
        )
    index = master[valid_mask.to_numpy()]
    if len(index) < 3:
        return SimulationResult(
            pd.DataFrame(), pd.DataFrame(), _metrics_from_equity(
                pd.Series(dtype=float),
                trades=0,
                turnover=0,
                exposure=0,
                leveraged_exposure=0,
            )
        )

    executions: dict[pd.Timestamp, dict[str, float]] = {}
    for signal_date, row in targets.iterrows():
        execution = _next_execution_date(index, pd.Timestamp(signal_date), delay_days)
        if execution is None:
            continue
        weights = {
            asset: float(value)
            for asset, value in row.items()
            if pd.notna(value) and float(value) > 1e-12
        }
        executions[execution] = _normalize_weights(weights)

    units = {asset: 0.0 for asset in required}
    cash_base_price = float(frames["CASH"].loc[index[0], "Open"])
    units["CASH"] = 1.0 / cash_base_price
    previous_equity = 1.0
    cost_rate = (cost_bps + slippage_bps) / 10_000
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    current_target = {"CASH": 1.0}

    for date in index:
        opens = {
            asset: float(frames[asset].loc[date, "Open"]) for asset in required
        }
        closes = {
            asset: float(frames[asset].loc[date, "Close"]) for asset in required
        }
        open_values = {asset: units.get(asset, 0.0) * opens[asset] for asset in required}
        gross_open = sum(open_values.values())

        if date in executions:
            desired_weights = _normalize_weights(executions[date])
            desired = {
                asset: gross_open * desired_weights.get(asset, 0.0)
                for asset in required
            }
            traded_notional = sum(
                abs(desired[asset] - open_values.get(asset, 0.0))
                for asset in required
                if asset != "CASH"
            )
            cost = traded_notional * cost_rate
            net_open = max(0.0, gross_open - cost)
            desired = {
                asset: net_open * desired_weights.get(asset, 0.0)
                for asset in required
            }
            traded_notional = sum(
                abs(desired[asset] - open_values.get(asset, 0.0))
                for asset in required
                if asset != "CASH"
            )
            cost = traded_notional * cost_rate
            net_open = max(0.0, gross_open - cost)
            for asset in required:
                units[asset] = (
                    net_open * desired_weights.get(asset, 0.0) / opens[asset]
                )
            if traded_notional > 1e-8:
                trade_rows.append(
                    {
                        "date": date,
                        "turnover": traded_notional / gross_open
                        if gross_open > 0
                        else 0.0,
                        "cost": cost,
                        "target": json.dumps(
                            desired_weights, sort_keys=True
                        ),
                    }
                )
            current_target = desired_weights

        close_values = {
            asset: units.get(asset, 0.0) * closes[asset] for asset in required
        }
        equity = sum(close_values.values())
        actual_weights = {
            asset: value / equity if equity > 0 else 0.0
            for asset, value in close_values.items()
        }
        daily_rows.append(
            {
                "Date": date,
                "equity": equity,
                "daily_return": equity / previous_equity - 1
                if previous_equity > 0
                else 0.0,
                "cash_weight": actual_weights.get("CASH", 0.0),
                "risk_weight": 1 - actual_weights.get("CASH", 0.0),
                "leveraged_weight": sum(
                    actual_weights.get(asset, 0.0)
                    for asset in LEVERAGED_ASSETS
                ),
                "target_json": json.dumps(current_target, sort_keys=True),
            }
        )
        previous_equity = equity

    daily = pd.DataFrame(daily_rows).set_index("Date")
    trades = pd.DataFrame(trade_rows)
    metrics = _metrics_from_equity(
        daily["equity"],
        trades=len(trades),
        turnover=float(trades["turnover"].sum()) if not trades.empty else 0.0,
        exposure=float(daily["risk_weight"].mean()),
        leveraged_exposure=float(daily["leveraged_weight"].mean()),
    )
    return SimulationResult(daily, trades, metrics)


def period_metrics(
    daily: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> dict[str, Any]:
    if daily.empty:
        return _metrics_from_equity(
            pd.Series(dtype=float),
            trades=0,
            turnover=0,
            exposure=0,
            leveraged_exposure=0,
        )
    subset = daily.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if subset.empty:
        return _metrics_from_equity(
            pd.Series(dtype=float),
            trades=0,
            turnover=0,
            exposure=0,
            leveraged_exposure=0,
        )
    return _metrics_from_equity(
        subset["equity"],
        trades=0,
        turnover=0,
        exposure=float(subset["risk_weight"].mean()),
        leveraged_exposure=float(subset["leveraged_weight"].mean()),
    )


def simulation_period_metrics(
    simulation: SimulationResult,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> dict[str, Any]:
    if simulation.daily.empty:
        return period_metrics(simulation.daily, start, end)
    subset = simulation.daily.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if subset.empty:
        return period_metrics(subset, start, end)
    if simulation.trades.empty or "date" not in simulation.trades:
        trade_subset = pd.DataFrame()
    else:
        dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        mask = (
            (dates >= subset.index.min())
            & (dates <= subset.index.max())
        )
        trade_subset = simulation.trades.loc[mask]
    return _metrics_from_equity(
        subset["equity"],
        trades=len(trade_subset),
        turnover=(
            float(trade_subset["turnover"].sum())
            if not trade_subset.empty and "turnover" in trade_subset
            else 0.0
        ),
        exposure=float(subset["risk_weight"].mean()),
        leveraged_exposure=float(subset["leveraged_weight"].mean()),
    )


def annual_returns(daily: pd.DataFrame) -> dict[int, float]:
    if daily.empty:
        return {}
    year_end = daily["equity"].resample("YE").last()
    returns = year_end.pct_change()
    first_year = daily.index[0].year
    first_value = daily.loc[daily.index.year == first_year, "equity"]
    if not first_value.empty:
        returns.loc[year_end.index[0]] = (
            year_end.iloc[0] / first_value.iloc[0] - 1
        )
    return {int(index.year): float(value) for index, value in returns.dropna().items()}


def candidate_id(family: str, **params: Any) -> str:
    def encode(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.3g}".replace("-", "m").replace(".", "p")
        if isinstance(value, (list, tuple)):
            return "-".join(encode(item) for item in value)
        if isinstance(value, Mapping):
            return "-".join(
                f"{key}{encode(item)}" for key, item in sorted(value.items())
            )
        return str(value).replace("-", "m").replace(".", "p").replace(" ", "")

    suffix = "_".join(f"{key}{encode(value)}" for key, value in sorted(params.items()))
    return f"{family}_{suffix}"


def build_candidate_grid() -> list[Candidate]:
    candidates: list[Candidate] = []

    def add(family: str, params: dict[str, Any]) -> None:
        cid = candidate_id(family, **params)
        provisional = Candidate(cid, family, "pending", params)
        candidates.append(
            Candidate(cid, family, candidate_track(provisional), params)
        )

    for asset in ("SPY", "QQQ", "XLK", "SOXX", "SMH", "QLD", "TQQQ"):
        add("buy_hold", {"asset": asset})

    mixes = [
        {"SPY": 0.5, "QQQ": 0.5},
        {"QQQ": 0.5, "XLK": 0.5},
        {"QQQ": 0.5, "SOXX": 0.5},
        {"XLK": 0.5, "SOXX": 0.5},
        {"SPY": 0.34, "QQQ": 0.33, "SOXX": 0.33},
        {"QQQ": 0.5, "QLD": 0.5},
        {"QQQ": 0.5, "TQQQ": 0.5},
        {"QLD": 0.5, "TQQQ": 0.5},
    ]
    for mix in mixes:
        for frequency in ("none", "annual", "quarterly"):
            add("fixed_mix", {"mix": mix, "frequency": frequency})

    asset_specs = [
        ("SPY", "self"),
        ("QQQ", "self"),
        ("XLK", "self"),
        ("SOXX", "self"),
        ("QLD", "underlying"),
        ("TQQQ", "underlying"),
    ]

    for asset, signal_mode in asset_specs:
        for sma in (100, 150, 180, 200, 220, 250):
            for slope_days in (0, 20, 60):
                for frequency in ("weekly", "monthly"):
                    for defense in ("CASH", "best"):
                        add(
                            "trend_hold",
                            {
                                "asset": asset,
                                "signal_mode": signal_mode,
                                "sma": sma,
                                "slope_days": slope_days,
                                "frequency": frequency,
                                "defense": defense,
                            },
                        )

    for asset, signal_mode in asset_specs:
        for sma in (100, 150, 200, 220, 250):
            for momentum_days in (63, 126, 189, 252):
                for frequency in ("weekly", "monthly"):
                    for defense in ("CASH", "best"):
                        add(
                            "ma_momentum",
                            {
                                "asset": asset,
                                "signal_mode": signal_mode,
                                "sma": sma,
                                "momentum_days": momentum_days,
                                "frequency": frequency,
                                "defense": defense,
                            },
                        )

    universes = [
        ("SPY", "QQQ"),
        ("SPY", "QQQ", "XLK", "SOXX"),
        ("QQQ", "XLK", "SOXX"),
        ("SPY", "QQQ", "XLK", "SOXX", "QLD"),
        ("QQQ", "XLK", "SOXX", "QLD", "TQQQ"),
    ]
    lookback_sets = [
        (63,),
        (126,),
        (252,),
        (63, 126),
        (126, 252),
        (63, 126, 252),
    ]
    for universe in universes:
        leveraged = any(asset in LEVERAGED_ASSETS for asset in universe)
        caps = (0.25, 0.5, 1.0) if leveraged else (1.0,)
        for lookbacks in lookback_sets:
            for top_n in (1, 2):
                for filter_mode in ("none", "momentum", "sma", "both"):
                    for score_mode in ("raw", "risk_adjusted"):
                        for frequency in ("monthly", "quarterly"):
                            for defense in ("CASH", "best"):
                                for leverage_cap in caps:
                                    add(
                                        "rotation",
                                        {
                                            "universe": universe,
                                            "lookbacks": lookbacks,
                                            "top_n": top_n,
                                            "filter": filter_mode,
                                            "score_mode": score_mode,
                                            "frequency": frequency,
                                            "defense": defense,
                                            "leverage_cap": leverage_cap,
                                        },
                                    )

    exit_variants = [
        {"exit_rule": "sma", "exit_sma": 50},
        {"exit_rule": "sma", "exit_sma": 100},
        {"exit_rule": "sma", "exit_sma": 150},
        {"exit_rule": "sma", "exit_sma": 200},
        {"exit_rule": "trailing", "trailing_stop": -0.20},
        {"exit_rule": "trailing", "trailing_stop": -0.30},
    ]
    for asset, signal_mode in asset_specs:
        for drawdown in (-0.15, -0.20, -0.25, -0.30, -0.40):
            for recovery_sma in (20, 50, 100):
                for exit_variant in exit_variants:
                    for frequency in ("weekly", "monthly"):
                        for parts in (1, 3):
                            add(
                                "drawdown_recovery",
                                {
                                    "asset": asset,
                                    "signal_mode": signal_mode,
                                    "drawdown": drawdown,
                                    "recovery_sma": recovery_sma,
                                    "frequency": frequency,
                                    "entry_parts": parts,
                                    "exit_parts": parts,
                                    **exit_variant,
                                },
                            )

    pullback_exit = [
        {"exit_rule": "sma", "exit_sma": 100},
        {"exit_rule": "sma", "exit_sma": 200},
        {"exit_rule": "trailing", "trailing_stop": -0.15},
        {"exit_rule": "trailing", "trailing_stop": -0.25},
    ]
    for asset, signal_mode in asset_specs:
        for drawdown in (-0.05, -0.10, -0.15, -0.20):
            for recovery_sma in (20, 50):
                for exit_variant in pullback_exit:
                    for frequency in ("weekly", "monthly"):
                        for parts in (1, 3):
                            add(
                                "pullback_hold",
                                {
                                    "asset": asset,
                                    "signal_mode": signal_mode,
                                    "drawdown": drawdown,
                                    "recovery_sma": recovery_sma,
                                    "slope_days": 20,
                                    "frequency": frequency,
                                    "entry_parts": parts,
                                    "exit_parts": parts,
                                    **exit_variant,
                                },
                            )

    breakout_exit = pullback_exit
    for asset, signal_mode in asset_specs:
        for breakout_days in (20, 55, 126, 252):
            for exit_variant in breakout_exit:
                for frequency in ("weekly", "monthly"):
                    for parts in (1, 3):
                        add(
                            "breakout_hold",
                            {
                                "asset": asset,
                                "signal_mode": signal_mode,
                                "breakout_days": breakout_days,
                                "require_long_trend": True,
                                "recovery_sma": 50,
                                "frequency": frequency,
                                "entry_parts": parts,
                                "exit_parts": parts,
                                **exit_variant,
                            },
                        )

    for mode, leverage_asset in (
        ("single", "QLD"),
        ("single", "TQQQ"),
        ("ladder", "TQQQ"),
    ):
        for sma in (100, 150, 200, 250):
            for momentum_days in (63, 126, 252):
                for threshold in (0.0, 0.05, 0.10, 0.20):
                    for max_vol in (0.25, 0.35, 0.45, 0.60):
                        for frequency in ("weekly", "monthly"):
                            for defense in ("CASH", "best"):
                                add(
                                    "leveraged_regime",
                                    {
                                        "mode": mode,
                                        "leverage_asset": leverage_asset,
                                        "moderate_asset": "QLD",
                                        "sma": sma,
                                        "momentum_days": momentum_days,
                                        "strong_threshold": threshold,
                                        "max_vol": max_vol,
                                        "vol_days": 63,
                                        "frequency": frequency,
                                        "defense": defense,
                                    },
                                )

    unique = {candidate.candidate_id: candidate for candidate in candidates}
    if len(unique) != len(candidates):
        raise RuntimeError("Duplicate candidate IDs were generated")
    return candidates
