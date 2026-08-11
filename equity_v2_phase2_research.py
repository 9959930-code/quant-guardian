from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from equity_v2_engine import (
    FeatureStore,
    SimulationResult,
    close_panel,
    load_market_data,
    schedule_dates,
    simulate_close_to_close,
    simulate_next_open,
    simulation_period_metrics,
)
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


STRATEGY_VERSION = "equity-v2-phase2-0.1"
ACTUAL_START = pd.Timestamp("2010-03-01")
SYNTHETIC_START = pd.Timestamp("2005-01-03")
LATEST_DEV_END = pd.Timestamp("2022-12-31")
HOLDOUT_START = pd.Timestamp("2023-01-01")
DEFAULT_COST_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 5.0
BLEND_REBALANCE_COST_BPS = 10.0
INITIAL_CAPITAL_KRW = 10_000_000.0

DEV_FOLDS = {
    "dev_2010_2014": (pd.Timestamp("2010-03-01"), pd.Timestamp("2014-12-31")),
    "dev_2015_2018": (pd.Timestamp("2015-01-01"), pd.Timestamp("2018-12-31")),
    "dev_2019_2022": (pd.Timestamp("2019-01-01"), pd.Timestamp("2022-12-31")),
}


@dataclass(frozen=True)
class Phase2Candidate:
    candidate_id: str
    kind: str
    params: Mapping[str, Any]

    def record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "params_json": json.dumps(
                dict(self.params), ensure_ascii=False, sort_keys=True
            ),
        }


@dataclass
class BlendSimulation:
    daily: pd.DataFrame
    rebalance_events: pd.DataFrame
    metrics: dict[str, Any]


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_pct(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value) * 100:.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    return "n/a" if not _finite(value) else f"{float(value):.{digits}f}"


def _fmt_krw(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value):,.0f}원"


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
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
        json.dumps(
            _json_ready(dict(payload)),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def _candidate_id(kind: str, params: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(params), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]
    return f"{kind}_{digest}"


def build_phase2_grid() -> list[Phase2Candidate]:
    rows: list[Phase2Candidate] = []

    for breakout_days in (10, 20, 30, 55, 63, 90):
        for long_ma in (150, 180, 200, 220):
            for exit_ma in (180, 200, 220):
                for frequency in ("weekly", "monthly"):
                    for parts in (1, 2, 3):
                        for tqqq_weight in (0.50, 0.75, 1.00):
                            for remainder in ("QQQ", "CASH"):
                                for entry_confirm in (1, 2):
                                    for exit_confirm in (1, 2):
                                        params = {
                                            "asset": "TQQQ",
                                            "breakout_days": breakout_days,
                                            "long_ma": long_ma,
                                            "exit_ma": exit_ma,
                                            "frequency": frequency,
                                            "parts": parts,
                                            "tqqq_weight": tqqq_weight,
                                            "remainder": remainder,
                                            "entry_confirm": entry_confirm,
                                            "exit_confirm": exit_confirm,
                                        }
                                        rows.append(
                                            Phase2Candidate(
                                                _candidate_id("balanced", params),
                                                "balanced",
                                                params,
                                            )
                                        )

    for pullback in (0.03, 0.05, 0.075, 0.10, 0.15):
        for recovery_ma in (10, 20, 50):
            for long_ma in (180, 200, 220):
                for trailing_stop in (0.15, 0.20, 0.25, 0.30):
                    for frequency in ("weekly", "monthly"):
                        for parts in (1, 2, 3):
                            for tqqq_weight in (0.50, 0.75, 1.00):
                                for remainder in ("QQQ", "CASH"):
                                    for slope_days in (0, 20):
                                        for confirmation in (1, 2):
                                            params = {
                                                "asset": "TQQQ",
                                                "pullback": pullback,
                                                "recovery_ma": recovery_ma,
                                                "long_ma": long_ma,
                                                "trailing_stop": trailing_stop,
                                                "frequency": frequency,
                                                "parts": parts,
                                                "tqqq_weight": tqqq_weight,
                                                "remainder": remainder,
                                                "slope_days": slope_days,
                                                "confirmation": confirmation,
                                            }
                                            rows.append(
                                                Phase2Candidate(
                                                    _candidate_id(
                                                        "aggressive", params
                                                    ),
                                                    "aggressive",
                                                    params,
                                                )
                                            )

    unique = {candidate.candidate_id: candidate for candidate in rows}
    if len(unique) != len(rows):
        raise RuntimeError("Duplicate phase-two candidate IDs")
    return rows


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


def _weights_for_fraction(params: Mapping[str, Any], fraction: float) -> dict[str, float]:
    fraction = min(1.0, max(0.0, float(fraction)))
    asset = str(params.get("asset", "TQQQ"))
    leveraged = float(params["tqqq_weight"])
    remainder = str(params["remainder"])
    weights: dict[str, float] = {asset: leveraged * fraction}
    if remainder == "QQQ":
        weights["QQQ"] = (1.0 - leveraged) * fraction
        weights["CASH"] = 1.0 - fraction
    else:
        weights["CASH"] = 1.0 - leveraged * fraction
    return _normalize(weights)


def _append_target(
    rows: list[dict[str, Any]], date: pd.Timestamp, weights: Mapping[str, float]
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


def generate_phase2_targets(
    candidate: Phase2Candidate,
    *,
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    p = dict(candidate.params)
    index = store.close.loc[start:end].index
    dates = schedule_dates(index, str(p["frequency"]))
    rows: list[dict[str, Any]] = []
    qqq = store.close["QQQ"]
    parts = int(p["parts"])
    state = "out"
    stage = 0
    peak = -np.inf
    armed = False
    entry_streak = 0
    exit_streak = 0

    if candidate.kind == "balanced":
        prior_high = store.rolling_high("QQQ", int(p["breakout_days"])).shift(1)
        long_ma = store.sma("QQQ", int(p["long_ma"]))
        exit_ma = store.sma("QQQ", int(p["exit_ma"]))
        for date in dates:
            close_value = qqq.get(date, np.nan)
            values = (
                close_value,
                prior_high.get(date, np.nan),
                long_ma.get(date, np.nan),
                exit_ma.get(date, np.nan),
            )
            if any(pd.isna(value) for value in values):
                continue
            close_value, high_value, long_value, exit_value = map(float, values)
            entry_ok = close_value >= high_value and close_value > long_value
            exit_ok = close_value < exit_value

            if state == "out":
                entry_streak = entry_streak + 1 if entry_ok else 0
                if entry_streak >= int(p["entry_confirm"]):
                    state = "entering"
                    stage = 1
                    _append_target(
                        rows, date, _weights_for_fraction(p, stage / parts)
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
                    continue
                stage += 1
                _append_target(rows, date, _weights_for_fraction(p, stage / parts))
                if stage >= parts:
                    state = "holding"
                continue

            if state == "holding":
                exit_streak = exit_streak + 1 if exit_ok else 0
                if exit_streak >= int(p["exit_confirm"]):
                    state = "exiting"
                    stage = 1
                    _append_target(
                        rows, date, _weights_for_fraction(p, 1 - stage / parts)
                    )
                    if parts == 1:
                        state = "out"
                    exit_streak = 0
                continue

            if state == "exiting":
                stage += 1
                _append_target(
                    rows, date, _weights_for_fraction(p, 1 - stage / parts)
                )
                if stage >= parts:
                    state = "out"
        return _targets_frame(rows)

    if candidate.kind == "aggressive":
        long_ma = store.sma("QQQ", int(p["long_ma"]))
        recovery_ma = store.sma("QQQ", int(p["recovery_ma"]))
        rolling_high = store.rolling_high("QQQ", 252)
        slope_days = int(p["slope_days"])
        for date in dates:
            close_value = qqq.get(date, np.nan)
            long_value = long_ma.get(date, np.nan)
            recovery_value = recovery_ma.get(date, np.nan)
            high_value = rolling_high.get(date, np.nan)
            if any(
                pd.isna(value)
                for value in (close_value, long_value, recovery_value, high_value)
            ):
                continue
            close_value = float(close_value)
            long_value = float(long_value)
            long_ok = close_value > long_value
            if slope_days:
                prior_long = long_ma.shift(slope_days).get(date, np.nan)
                long_ok = long_ok and pd.notna(prior_long) and long_value > float(prior_long)
            drawdown = close_value / float(high_value) - 1

            if state == "out":
                if long_ok and drawdown <= -float(p["pullback"]):
                    armed = True
                entry_ok = armed and long_ok and close_value > float(recovery_value)
                entry_streak = entry_streak + 1 if entry_ok else 0
                if entry_streak >= int(p["confirmation"]):
                    state = "entering"
                    stage = 1
                    peak = close_value
                    _append_target(
                        rows, date, _weights_for_fraction(p, stage / parts)
                    )
                    if parts == 1:
                        state = "holding"
                    armed = False
                    entry_streak = 0
                continue

            if state == "entering":
                peak = max(peak, close_value)
                stage += 1
                _append_target(rows, date, _weights_for_fraction(p, stage / parts))
                if stage >= parts:
                    state = "holding"
                continue

            if state == "holding":
                peak = max(peak, close_value)
                exit_ok = close_value / peak - 1 <= -float(p["trailing_stop"])
                exit_streak = exit_streak + 1 if exit_ok else 0
                if exit_streak >= int(p["confirmation"]):
                    state = "exiting"
                    stage = 1
                    _append_target(
                        rows, date, _weights_for_fraction(p, 1 - stage / parts)
                    )
                    if parts == 1:
                        state = "out"
                        peak = -np.inf
                    exit_streak = 0
                continue

            if state == "exiting":
                stage += 1
                _append_target(
                    rows, date, _weights_for_fraction(p, 1 - stage / parts)
                )
                if stage >= parts:
                    state = "out"
                    peak = -np.inf
        return _targets_frame(rows)

    raise ValueError(f"Unsupported phase-two kind: {candidate.kind}")


def _metric_row(
    simulation: SimulationResult, latest: pd.Timestamp
) -> dict[str, Any]:
    row: dict[str, Any] = {
        **{f"full_{key}": value for key, value in simulation.metrics.items()}
    }
    for prefix, (start, end) in DEV_FOLDS.items():
        metrics = simulation_period_metrics(simulation, start, end)
        row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
    dev = simulation_period_metrics(simulation, ACTUAL_START, LATEST_DEV_END)
    holdout = simulation_period_metrics(simulation, HOLDOUT_START, latest)
    row.update({f"dev_all_{key}": value for key, value in dev.items()})
    row.update({f"holdout_{key}": value for key, value in holdout.items()})
    return row


def _selection_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    cagrs = [row.get(f"{prefix}_cagr") for prefix in DEV_FOLDS]
    mdds = [row.get(f"{prefix}_mdd") for prefix in DEV_FOLDS]
    calmars = [row.get(f"{prefix}_calmar") for prefix in DEV_FOLDS]
    if not all(_finite(value) for value in cagrs + mdds):
        return {
            "dev_mean_cagr": np.nan,
            "dev_min_cagr": np.nan,
            "dev_worst_mdd": np.nan,
            "dev_mean_calmar": np.nan,
            "selection_score": np.nan,
        }
    mean_cagr = float(np.mean(cagrs))
    min_cagr = float(np.min(cagrs))
    worst_mdd = float(np.min(mdds))
    valid_calmars = [float(value) for value in calmars if _finite(value)]
    mean_calmar = float(np.mean(valid_calmars)) if valid_calmars else 0.0
    trades = float(row.get("dev_all_trades_per_year", 0) or 0)
    score = (
        0.50 * mean_cagr
        + 0.35 * min_cagr
        + 0.15 * min(3.0, mean_calmar) * 0.10
        - 0.0015 * math.log1p(max(0.0, trades))
    )
    return {
        "dev_mean_cagr": mean_cagr,
        "dev_min_cagr": min_cagr,
        "dev_worst_mdd": worst_mdd,
        "dev_mean_calmar": mean_calmar,
        "selection_score": score,
    }


def broad_search(
    candidates: Iterable[Phase2Candidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    candidate_list = list(candidates)
    for number, candidate in enumerate(candidate_list, start=1):
        record = candidate.record()
        try:
            targets = generate_phase2_targets(
                candidate, store=store, start=ACTUAL_START, end=latest
            )
            if targets.empty:
                raise ValueError("no target schedule")
            simulation = simulate_close_to_close(
                targets=targets,
                frames=frames,
                start=ACTUAL_START,
                end=latest,
                cost_bps=cost_bps,
            )
            values = _metric_row(simulation, latest)
            values.update(_selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 1000 == 0:
            print(f"phase2 broad: {number}/{len(candidate_list)}")
    return pd.DataFrame(rows)


def shortlist(
    broad: pd.DataFrame,
    candidate_map: Mapping[str, Phase2Candidate],
    maximum: int = 400,
) -> list[Phase2Candidate]:
    valid = broad.loc[
        broad["status"].eq("ok") & broad["selection_score"].notna()
    ].copy()
    selected: set[str] = set()
    for kind, group in valid.groupby("kind"):
        selected.update(group.nlargest(80, "selection_score")["candidate_id"])
        selected.update(group.nlargest(30, "dev_mean_cagr")["candidate_id"])
        for mdd_limit in (-0.40, -0.50, -0.60, -0.75):
            eligible = group.loc[group["dev_worst_mdd"] >= mdd_limit]
            selected.update(eligible.nlargest(40, "selection_score")["candidate_id"])
            selected.update(eligible.nlargest(15, "dev_mean_cagr")["candidate_id"])
    ranked = valid.loc[valid["candidate_id"].isin(selected)].sort_values(
        "selection_score", ascending=False
    )
    if len(ranked) > maximum:
        ranked = ranked.head(maximum)
    return [candidate_map[cid] for cid in ranked["candidate_id"]]


def exact_evaluate(
    candidates: Iterable[Phase2Candidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
    delay_days: int = 0,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    rows: list[dict[str, Any]] = []
    simulations: dict[str, SimulationResult] = {}
    candidate_list = list(candidates)
    for number, candidate in enumerate(candidate_list, start=1):
        record = candidate.record()
        try:
            targets = generate_phase2_targets(
                candidate, store=store, start=ACTUAL_START, end=latest
            )
            simulation = simulate_next_open(
                targets=targets,
                frames=frames,
                start=ACTUAL_START,
                end=latest,
                cost_bps=cost_bps,
                slippage_bps=slippage_bps,
                delay_days=delay_days,
            )
            if not _finite(simulation.metrics.get("cagr")):
                raise ValueError("invalid exact metrics")
            simulations[candidate.candidate_id] = simulation
            values = _metric_row(simulation, latest)
            values.update(_selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 50 == 0:
            print(f"phase2 exact: {number}/{len(candidate_list)}")
    return pd.DataFrame(rows), simulations


def select_strategy_rows(exact: pd.DataFrame) -> pd.DataFrame:
    valid = exact.loc[
        exact["status"].eq("ok")
        & exact["selection_score"].notna()
        & (exact["dev_min_cagr"] > 0)
    ].copy()
    categories = [
        ("aggressive_max", "aggressive", -0.75),
        ("aggressive_mdd60", "aggressive", -0.60),
        ("balanced_mdd50", "balanced", -0.50),
        ("balanced_mdd40", "balanced", -0.40),
    ]
    rows: list[dict[str, Any]] = []
    for category, kind, mdd_limit in categories:
        eligible = valid.loc[
            (valid["kind"] == kind) & (valid["dev_worst_mdd"] >= mdd_limit)
        ]
        if eligible.empty:
            continue
        best = eligible.sort_values(
            ["selection_score", "dev_mean_cagr", "dev_min_cagr"],
            ascending=False,
        ).iloc[0]
        rows.append({"category": category, **best.to_dict()})
    return pd.DataFrame(rows)


def _daily_metrics(equity: pd.Series, exposure: pd.Series) -> dict[str, Any]:
    equity = equity.dropna()
    exposure = exposure.reindex(equity.index).fillna(0.0)
    if len(equity) < 2:
        return {}
    daily = equity.pct_change().dropna()
    days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = days / 365.25
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((1 + total) ** (1 / years) - 1)
    drawdown = equity / equity.cummax() - 1
    mdd = float(drawdown.min())
    vol = float(daily.std(ddof=0) * math.sqrt(252))
    sharpe = float(daily.mean() * 252 / vol) if vol > 0 else np.nan
    downside = float(daily[daily < 0].std(ddof=0) * math.sqrt(252))
    sortino = float(daily.mean() * 252 / downside) if downside > 0 else np.nan
    return {
        "total_return": total,
        "capital_multiple": 1 + total,
        "cagr": cagr,
        "mdd": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "average_tqqq_exposure": float(exposure.mean()),
        "maximum_tqqq_exposure": float(exposure.max()),
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
    }


def blend_sleeves(
    aggressive: SimulationResult,
    balanced: SimulationResult,
    *,
    aggressive_weight: float,
    policy: str,
    threshold: float | None,
    rebalance_cost_bps: float,
) -> BlendSimulation:
    index = aggressive.daily.index.intersection(balanced.daily.index)
    index = index[index >= ACTUAL_START]
    a_equity = aggressive.daily.loc[index, "equity"]
    b_equity = balanced.daily.loc[index, "equity"]
    a_returns = a_equity.pct_change().fillna(0.0)
    b_returns = b_equity.pct_change().fillna(0.0)
    a_exposure = aggressive.daily.loc[index, "leveraged_weight"].fillna(0.0)
    b_exposure = balanced.daily.loc[index, "leveraged_weight"].fillna(0.0)
    a_value = float(aggressive_weight)
    b_value = float(1 - aggressive_weight)
    prior_year = index[0].year
    prior_month = index[0].to_period("M")
    cost_rate = rebalance_cost_bps / 10_000
    daily_rows: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for date in index:
        do_rebalance = False
        reason = None
        total_before = a_value + b_value
        current_weight = a_value / total_before if total_before > 0 else aggressive_weight
        if policy == "annual" and date.year != prior_year:
            do_rebalance = True
            reason = "annual"
        elif policy == "threshold" and date.to_period("M") != prior_month:
            if threshold is not None and abs(current_weight - aggressive_weight) >= threshold:
                do_rebalance = True
                reason = f"threshold_{threshold:.2f}"
        if do_rebalance and total_before > 0:
            target_a = total_before * aggressive_weight
            transferred = abs(target_a - a_value)
            cost = transferred * cost_rate
            net = max(0.0, total_before - cost)
            a_value = net * aggressive_weight
            b_value = net * (1 - aggressive_weight)
            events.append(
                {
                    "date": date,
                    "reason": reason,
                    "transferred": transferred,
                    "cost": cost,
                }
            )
        a_value *= 1 + float(a_returns.loc[date])
        b_value *= 1 + float(b_returns.loc[date])
        total = a_value + b_value
        exposure = (
            a_value * float(a_exposure.loc[date])
            + b_value * float(b_exposure.loc[date])
        ) / total if total > 0 else 0.0
        daily_rows.append(
            {
                "Date": date,
                "equity": total,
                "daily_return": 0.0,
                "risk_weight": exposure,
                "leveraged_weight": exposure,
                "aggressive_sleeve_weight": a_value / total if total > 0 else 0.0,
            }
        )
        prior_year = date.year
        prior_month = date.to_period("M")

    daily = pd.DataFrame(daily_rows).set_index("Date")
    daily["daily_return"] = daily["equity"].pct_change().fillna(0.0)
    metrics = _daily_metrics(daily["equity"], daily["leveraged_weight"])
    return BlendSimulation(daily, pd.DataFrame(events), metrics)


def _blend_metric_row(simulation: BlendSimulation, latest: pd.Timestamp) -> dict[str, Any]:
    wrapped = SimulationResult(
        daily=simulation.daily,
        trades=simulation.rebalance_events,
        metrics=simulation.metrics,
    )
    values = _metric_row(wrapped, latest)
    values.update(_selection_fields(values))
    values["blend_rebalance_events"] = len(simulation.rebalance_events)
    return values


def blend_grid(
    aggressive: SimulationResult,
    balanced: SimulationResult,
    *,
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, BlendSimulation]]:
    rows: list[dict[str, Any]] = []
    simulations: dict[str, BlendSimulation] = {}
    for weight in np.round(np.arange(0.0, 1.0001, 0.10), 2):
        policies = [("none", None), ("annual", None)]
        policies.extend(("threshold", value) for value in (0.10, 0.15, 0.20))
        for policy, threshold in policies:
            if weight in {0.0, 1.0} and policy != "none":
                continue
            label = (
                f"a{int(weight*100):02d}_b{int((1-weight)*100):02d}_"
                f"{policy}{'' if threshold is None else int(threshold*100)}"
            )
            simulation = blend_sleeves(
                aggressive,
                balanced,
                aggressive_weight=float(weight),
                policy=policy,
                threshold=threshold,
                rebalance_cost_bps=BLEND_REBALANCE_COST_BPS,
            )
            simulations[label] = simulation
            values = _blend_metric_row(simulation, latest)
            rows.append(
                {
                    "label": label,
                    "aggressive_weight": float(weight),
                    "balanced_weight": float(1 - weight),
                    "policy": policy,
                    "threshold": threshold,
                    **values,
                }
            )
    return pd.DataFrame(rows), simulations


def select_blends(blends: pd.DataFrame) -> pd.DataFrame:
    valid = blends.loc[
        blends["selection_score"].notna() & (blends["dev_min_cagr"] > 0)
    ].copy()
    categories = [
        ("blend_absolute", None),
        ("blend_mdd50", -0.50),
        ("blend_mdd45", -0.45),
        ("blend_mdd40", -0.40),
    ]
    rows: list[dict[str, Any]] = []
    for category, limit in categories:
        eligible = valid
        if limit is not None:
            eligible = eligible.loc[eligible["dev_worst_mdd"] >= limit]
        if eligible.empty:
            continue
        best = eligible.sort_values(
            ["selection_score", "dev_mean_cagr", "dev_min_cagr"],
            ascending=False,
        ).iloc[0]
        rows.append({"category": category, **best.to_dict()})
    calmar = valid.sort_values(
        ["dev_mean_calmar", "selection_score"], ascending=False
    ).iloc[0]
    rows.append({"category": "blend_calmar", **calmar.to_dict()})
    return pd.DataFrame(rows).drop_duplicates("category")


def _buy_hold_target(
    frames: Mapping[str, pd.DataFrame], asset: str, start: pd.Timestamp
) -> tuple[pd.Timestamp, pd.DataFrame]:
    valid = frames[asset][["Open", "Close"]].dropna().index
    first = pd.Timestamp(valid[valid >= start][0])
    master = frames["SPY"].index
    prior = master[master < first]
    signal = pd.Timestamp(prior[-1]) if len(prior) else first
    return signal, pd.DataFrame({asset: [1.0], "CASH": [0.0]}, index=[signal])


def buy_hold_comparison(
    frames: Mapping[str, pd.DataFrame],
    *,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
    first_entry_dates: Mapping[str, pd.Timestamp],
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    rows: list[dict[str, Any]] = []
    simulations: dict[str, SimulationResult] = {}
    scenarios = [
        ("TQQQ_from_2010", "TQQQ", ACTUAL_START),
        ("QLD_from_2010", "QLD", ACTUAL_START),
        ("QQQ_from_2010", "QQQ", ACTUAL_START),
        ("XLK_from_2010", "XLK", ACTUAL_START),
        ("SOXX_from_2010", "SOXX", ACTUAL_START),
        ("QLD_full_actual", "QLD", pd.Timestamp("2006-07-01")),
    ]
    scenarios.extend(
        (f"TQQQ_from_{name}_first_entry", "TQQQ", date)
        for name, date in first_entry_dates.items()
    )
    for label, asset, start in scenarios:
        signal, targets = _buy_hold_target(frames, asset, start)
        simulation = simulate_next_open(
            targets=targets,
            frames=frames,
            start=signal,
            end=latest,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        )
        simulations[label] = simulation
        metrics = simulation.metrics
        rows.append(
            {
                "label": label,
                "asset": asset,
                **metrics,
                "ten_million_final_krw": INITIAL_CAPITAL_KRW
                * float(metrics.get("capital_multiple", 1 + metrics.get("total_return", 0))),
            }
        )
    return pd.DataFrame(rows), simulations


def synthetic_leverage_frame(
    qqq: pd.DataFrame, *, annual_drag: float
) -> pd.DataFrame:
    frame = qqq[["Open", "Close"]].dropna().copy()
    daily_drag = annual_drag / 252
    synthetic_open: list[float] = []
    synthetic_close: list[float] = []
    prior_qqq_close = None
    prior_synthetic_close = 100.0
    for _, row in frame.iterrows():
        q_open = float(row["Open"])
        q_close = float(row["Close"])
        if prior_qqq_close is None:
            s_open = 100.0
            s_close = 100.0
        else:
            overnight = q_open / prior_qqq_close - 1
            full_day = q_close / prior_qqq_close - 1
            s_open = prior_synthetic_close * max(0.001, 1 + 3 * overnight)
            s_close = prior_synthetic_close * max(
                0.001, 1 + 3 * full_day - daily_drag
            )
        synthetic_open.append(s_open)
        synthetic_close.append(s_close)
        prior_qqq_close = q_close
        prior_synthetic_close = s_close
    return pd.DataFrame(
        {"Open": synthetic_open, "Close": synthetic_close}, index=frame.index
    )


def synthetic_stress(
    selected: Mapping[str, Phase2Candidate],
    *,
    base_frames: Mapping[str, pd.DataFrame],
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for drag in (0.01, 0.025, 0.04):
        synthetic_asset = f"TQQQ_SYNTH_{str(drag).replace('.', 'p')}"
        frames = dict(base_frames)
        frames[synthetic_asset] = synthetic_leverage_frame(
            base_frames["QQQ"], annual_drag=drag
        ).reindex(base_frames["SPY"].index)
        store = FeatureStore(close_panel(frames))
        simulations: dict[str, SimulationResult] = {}
        for name, candidate in selected.items():
            params = dict(candidate.params)
            params["asset"] = synthetic_asset
            spec = replace(
                candidate,
                candidate_id=f"{candidate.candidate_id}_{synthetic_asset}",
                params=params,
            )
            targets = generate_phase2_targets(
                spec, store=store, start=SYNTHETIC_START, end=latest
            )
            simulation = simulate_next_open(
                targets=targets,
                frames=frames,
                start=SYNTHETIC_START,
                end=latest,
                cost_bps=cost_bps,
                slippage_bps=slippage_bps,
            )
            simulations[name] = simulation
            crisis = simulation_period_metrics(
                simulation, "2007-10-01", "2009-12-31"
            )
            rows.append(
                {
                    "annual_drag": drag,
                    "strategy": name,
                    **{f"full_{key}": value for key, value in simulation.metrics.items()},
                    **{f"gfc_{key}": value for key, value in crisis.items()},
                }
            )

        signal, targets = _buy_hold_target(
            frames, synthetic_asset, SYNTHETIC_START
        )
        buy_hold = simulate_next_open(
            targets=targets,
            frames=frames,
            start=signal,
            end=latest,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        )
        crisis = simulation_period_metrics(
            buy_hold, "2007-10-01", "2009-12-31"
        )
        rows.append(
            {
                "annual_drag": drag,
                "strategy": "synthetic_buy_hold",
                **{f"full_{key}": value for key, value in buy_hold.metrics.items()},
                **{f"gfc_{key}": value for key, value in crisis.items()},
            }
        )
    return pd.DataFrame(rows)


def robustness(
    selected: Mapping[str, Phase2Candidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, candidate in selected.items():
        for delay in (0, 1, 2, 3, 5):
            for cost_multiplier in (1.0, 2.0):
                targets = generate_phase2_targets(
                    candidate, store=store, start=ACTUAL_START, end=latest
                )
                simulation = simulate_next_open(
                    targets=targets,
                    frames=frames,
                    start=ACTUAL_START,
                    end=latest,
                    cost_bps=cost_bps * cost_multiplier,
                    slippage_bps=slippage_bps * cost_multiplier,
                    delay_days=delay,
                )
                values = _metric_row(simulation, latest)
                values.update(_selection_fields(values))
                rows.append(
                    {
                        "strategy": name,
                        "delay_days": delay,
                        "cost_multiplier": cost_multiplier,
                        **values,
                    }
                )
    return pd.DataFrame(rows)


def _params_text(params_json: str) -> str:
    params = json.loads(params_json)
    order = [
        "breakout_days",
        "pullback",
        "recovery_ma",
        "long_ma",
        "exit_ma",
        "trailing_stop",
        "frequency",
        "parts",
        "tqqq_weight",
        "remainder",
        "entry_confirm",
        "exit_confirm",
        "confirmation",
        "slope_days",
    ]
    return ", ".join(f"{key}={params[key]}" for key in order if key in params)


def build_report(
    *,
    manifest: Mapping[str, Any],
    selected_strategies: pd.DataFrame,
    selected_blends: pd.DataFrame,
    buy_hold: pd.DataFrame,
    robustness_table: pd.DataFrame,
    synthetic: pd.DataFrame,
) -> str:
    lines = [
        "# Quant Guardian Equity v2 2차 연구 결과",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 실제 ETF 데이터: {manifest['actual_start']}~{manifest['data_end']}",
        f"- 광범위 후보: {manifest['candidate_count']:,}",
        f"- 다음 시가 정밀검증: {manifest['exact_count']:,}",
        "- 선택구간: 2010~2022의 세 개발 폴드만 사용",
        "- 2023~최신 홀드아웃: 선택에 사용하지 않고 보고만 함",
        "- BTC·현재 주식 Telegram·사이트 변경 없음",
        "",
        "## 최종 단일전략 후보",
        "",
        "| 구분 | 전략 | 파라미터 | 전체 CAGR | 전체 MDD | 개발 평균 CAGR | 개발 최악 MDD | 홀드아웃 CAGR | 홀드아웃 MDD | 거래/년 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in selected_strategies.iterrows():
        lines.append(
            f"| {row['category']} | {row['kind']} | {_params_text(str(row['params_json']))} | "
            f"{_fmt_pct(row['full_cagr'])} | {_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['dev_mean_cagr'])} | {_fmt_pct(row['dev_worst_mdd'])} | "
            f"{_fmt_pct(row['holdout_cagr'])} | {_fmt_pct(row['holdout_mdd'])} | "
            f"{_fmt_num(row['full_trades_per_year'])} |"
        )

    lines.extend(
        [
            "",
            "## 공격형·균형형 혼합 후보",
            "",
            "| 구분 | 공격형 | 균형형 | 리밸런싱 | 전체 CAGR | 전체 MDD | 개발 평균 CAGR | 개발 최악 MDD | 홀드아웃 CAGR | 홀드아웃 MDD |",
            "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in selected_blends.iterrows():
        policy = str(row["policy"])
        if policy == "threshold":
            policy += f" {float(row['threshold']) * 100:.0f}%p"
        lines.append(
            f"| {row['category']} | {row['aggressive_weight'] * 100:.0f}% | "
            f"{row['balanced_weight'] * 100:.0f}% | {policy} | "
            f"{_fmt_pct(row['full_cagr'])} | {_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['dev_mean_cagr'])} | {_fmt_pct(row['dev_worst_mdd'])} | "
            f"{_fmt_pct(row['holdout_cagr'])} | {_fmt_pct(row['holdout_mdd'])} |"
        )

    lines.extend(
        [
            "",
            "## 처음부터 매수 후 계속 보유한 기준",
            "",
            "| 기준 | CAGR | MDD | 자산배수 | 1,000만원 최종액 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in buy_hold.iterrows():
        lines.append(
            f"| {row['label']} | {_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_num(row['capital_multiple'])}배 | {_fmt_krw(row['ten_million_final_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 비용·체결지연",
            "",
            "| 전략 | 지연 | 비용배수 | 전체 CAGR | 전체 MDD | 개발 최악 MDD | 홀드아웃 CAGR |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in robustness_table.iterrows():
        lines.append(
            f"| {row['strategy']} | {int(row['delay_days'])}일 | {row['cost_multiplier']:.0f}배 | "
            f"{_fmt_pct(row['full_cagr'])} | {_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['dev_worst_mdd'])} | {_fmt_pct(row['holdout_cagr'])} |"
        )

    lines.extend(
        [
            "",
            "## 2008 합성 TQQQ 스트레스",
            "",
            "| 연간 드래그 | 전략 | 전체 CAGR | 전체 MDD | 2007~2009 CAGR | 2007~2009 MDD |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in synthetic.iterrows():
        lines.append(
            f"| {row['annual_drag'] * 100:.1f}% | {row['strategy']} | "
            f"{_fmt_pct(row['full_cagr'])} | {_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['gfc_cagr'])} | {_fmt_pct(row['gfc_mdd'])} |"
        )

    lines.extend(
        [
            "",
            "## 해석",
            "",
            "- 매수 후 보유 기준은 같은 시작일에 TQQQ를 사서 한 번도 팔지 않은 결과다.",
            "- 전략의 장점은 단순보유보다 CAGR을 반드시 높이는 것이 아니라, 장기 약세에서 현금화해 MDD를 줄이는 데 있다.",
            "- 공격형·균형형 모두 같은 TQQQ를 사용하므로 혼합은 자산분산이 아니라 진입·청산 타이밍 분산이다.",
            "- 2008 합성 TQQQ는 QQQ 일일수익률 3배와 고정 드래그를 적용한 스트레스 근사이며 실제 TQQQ 체결기록이 아니다.",
            "- 최종 Telegram 적용 전 실제 보유상태, 세금, 환전비용, 월말 신호 도착시각을 포함한 Shadow 검증이 필요하다.",
        ]
    )
    return "\n".join(lines)


def run_research(
    *,
    refresh: bool,
    config_path: Path,
    cost_bps: float,
    slippage_bps: float,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(UTC)
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, metadata = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    store = FeatureStore(close_panel(frames))
    latest = pd.Timestamp(frames["SPY"]["Close"].dropna().index.max())

    candidates = build_phase2_grid()
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    print(f"phase2 candidates: {len(candidates):,}")
    broad = broad_search(
        candidates,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps + slippage_bps,
    )
    exact_candidates = shortlist(broad, candidate_map, maximum=400)
    exact, exact_simulations = exact_evaluate(
        exact_candidates,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    selected_strategies = select_strategy_rows(exact)
    aggressive_row = selected_strategies.loc[
        selected_strategies["category"] == "aggressive_max"
    ].iloc[0]
    balanced_row = selected_strategies.loc[
        selected_strategies["category"] == "balanced_mdd50"
    ].iloc[0]
    aggressive_id = str(aggressive_row["candidate_id"])
    balanced_id = str(balanced_row["candidate_id"])
    selected_map = {
        "aggressive": candidate_map[aggressive_id],
        "balanced": candidate_map[balanced_id],
    }
    selected_simulations = {
        "aggressive": exact_simulations[aggressive_id],
        "balanced": exact_simulations[balanced_id],
    }

    blends, blend_simulations = blend_grid(
        selected_simulations["aggressive"],
        selected_simulations["balanced"],
        latest=latest,
    )
    selected_blends = select_blends(blends)

    first_entries: dict[str, pd.Timestamp] = {}
    for name, simulation in selected_simulations.items():
        if not simulation.trades.empty:
            first_entries[name] = pd.Timestamp(simulation.trades.iloc[0]["date"])
    buy_hold, _ = buy_hold_comparison(
        frames,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
        first_entry_dates=first_entries,
    )
    robustness_table = robustness(
        selected_map,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    synthetic = synthetic_stress(
        selected_map,
        base_frames=frames,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )

    outputs = {
        "broad": paths.output / "equity_v2_phase2_broad.csv",
        "exact": paths.output / "equity_v2_phase2_exact.csv",
        "selected_strategies": paths.output / "equity_v2_phase2_selected_strategies.csv",
        "blends": paths.output / "equity_v2_phase2_blends.csv",
        "selected_blends": paths.output / "equity_v2_phase2_selected_blends.csv",
        "buy_hold": paths.output / "equity_v2_phase2_buy_hold.csv",
        "robustness": paths.output / "equity_v2_phase2_robustness.csv",
        "synthetic": paths.output / "equity_v2_phase2_synthetic_stress.csv",
        "report": paths.output / "equity_v2_phase2_report.md",
        "manifest": paths.output / "equity_v2_phase2_manifest.json",
    }
    broad.to_csv(outputs["broad"], index=False, encoding="utf-8-sig")
    exact.to_csv(outputs["exact"], index=False, encoding="utf-8-sig")
    selected_strategies.to_csv(
        outputs["selected_strategies"], index=False, encoding="utf-8-sig"
    )
    blends.to_csv(outputs["blends"], index=False, encoding="utf-8-sig")
    selected_blends.to_csv(
        outputs["selected_blends"], index=False, encoding="utf-8-sig"
    )
    buy_hold.to_csv(outputs["buy_hold"], index=False, encoding="utf-8-sig")
    robustness_table.to_csv(
        outputs["robustness"], index=False, encoding="utf-8-sig"
    )
    synthetic.to_csv(outputs["synthetic"], index=False, encoding="utf-8-sig")

    manifest = {
        "schema_version": "equity-v2-phase2-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now.isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "actual_start": ACTUAL_START.date().isoformat(),
        "data_end": latest.date().isoformat(),
        "candidate_count": len(candidates),
        "broad_success_count": int((broad["status"] == "ok").sum()),
        "exact_count": len(exact_candidates),
        "development_folds": {
            key: [start.date().isoformat(), end.date().isoformat()]
            for key, (start, end) in DEV_FOLDS.items()
        },
        "holdout_start": HOLDOUT_START.date().isoformat(),
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
        "blend_rebalance_cost_bps": BLEND_REBALANCE_COST_BPS,
        "selected_aggressive": {
            "candidate_id": aggressive_id,
            "params": dict(selected_map["aggressive"].params),
        },
        "selected_balanced": {
            "candidate_id": balanced_id,
            "params": dict(selected_map["balanced"].params),
        },
        "selected_blends": selected_blends[
            [
                "category",
                "label",
                "aggressive_weight",
                "balanced_weight",
                "policy",
                "threshold",
            ]
        ].to_dict(orient="records"),
        "data_metadata": metadata,
        "limitations": [
            "large candidate search creates selection bias",
            "actual TQQQ history begins in 2010",
            "synthetic 2008 stress is an approximation",
            "taxes and personal FX conversion costs are excluded",
            "2026 is incomplete",
            "no live or Telegram approval",
        ],
    }
    _write_json(outputs["manifest"], manifest)
    report = build_report(
        manifest=manifest,
        selected_strategies=selected_strategies,
        selected_blends=selected_blends,
        buy_hold=buy_hold,
        robustness_table=robustness_table,
        synthetic=synthetic,
    )
    outputs["report"].write_text(report, encoding="utf-8-sig")
    return {
        "manifest": manifest,
        "outputs": {key: str(path) for key, path in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quant Guardian Equity v2 phase-two research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument(
        "--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS
    )
    args = parser.parse_args()
    result = run_research(
        refresh=args.refresh,
        config_path=args.config,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
    )
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
