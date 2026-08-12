from __future__ import annotations

import argparse
import json
import math
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
    simulation_period_metrics,
    simulate_close_to_close,
    simulate_next_open,
)
from equity_v2_modern_engine import (
    ANNUAL_DEDUCTION_KRW,
    INITIAL_CAPITAL_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    GenericTaxableSimulator,
    ModernCandidate,
    TaxableSimulation,
    build_candidate_grid,
    generate_targets,
)
from equity_v2_ndx_1985_research import _clean_ohlc
from quant_guardian import DEFAULT_CONFIG, load_config, read_price, resolve_paths


STRATEGY_VERSION = "equity-v2-modern-2006-0.1"
MODERN_START = pd.Timestamp("2006-07-03")
ACTUAL_TQQQ_START = pd.Timestamp("2010-02-09")
DOTCOM_START = pd.Timestamp("1999-03-10")
DOTCOM_END = pd.Timestamp("2003-12-31")
NDX_START = pd.Timestamp("1985-10-01")
HOLDOUT_START = pd.Timestamp("2023-01-03")
DEV_FOLDS = {
    "gfc_qe": (pd.Timestamp("2006-07-03"), pd.Timestamp("2011-12-30")),
    "post_gfc": (pd.Timestamp("2012-01-03"), pd.Timestamp("2018-12-31")),
    "covid_inflation": (pd.Timestamp("2019-01-02"), pd.Timestamp("2022-12-30")),
}
CALIBRATION_GRID = np.arange(-0.08, 0.1201, 0.001)
TRADING_DAYS = 252
COST_BPS = 5.0
SLIPPAGE_BPS = 5.0


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_pct(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value):,.0f}원"


def _fmt_num(value: Any, digits: int = 2) -> str:
    return "n/a" if not _finite(value) else f"{float(value):.{digits}f}"


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
        json.dumps(_json_ready(dict(payload)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def dynamic_synthetic(
    underlying: pd.DataFrame,
    short_yield: pd.Series,
    *,
    leverage: float,
    residual_drag: float,
) -> pd.DataFrame:
    frame = _clean_ohlc(underlying)
    rates = (
        pd.to_numeric(short_yield, errors="coerce")
        .reindex(frame.index)
        .ffill()
        .bfill()
        .clip(lower=-1.0, upper=30.0)
        / 100.0
    )
    opens: list[float] = []
    closes: list[float] = []
    previous_underlying_close: float | None = None
    previous_synthetic_close = 100.0
    for date, row in frame.iterrows():
        underlying_open = float(row["Open"])
        underlying_close = float(row["Close"])
        if previous_underlying_close is None:
            synthetic_open = 100.0
            synthetic_close = 100.0
        else:
            overnight = underlying_open / previous_underlying_close - 1.0
            full_day = underlying_close / previous_underlying_close - 1.0
            annual_financing = (leverage - 1.0) * float(rates.loc[date])
            daily_drag = (annual_financing + residual_drag) / TRADING_DAYS
            synthetic_open = previous_synthetic_close * max(
                0.0001, 1.0 + leverage * overnight
            )
            synthetic_close = previous_synthetic_close * max(
                0.0001, 1.0 + leverage * full_day - daily_drag
            )
        opens.append(synthetic_open)
        closes.append(synthetic_close)
        previous_underlying_close = underlying_close
        previous_synthetic_close = synthetic_close
    return pd.DataFrame({"Open": opens, "Close": closes}, index=frame.index)


def calibrate_residual(
    underlying: pd.DataFrame,
    short_yield: pd.Series,
    actual: pd.DataFrame,
    *,
    leverage: float,
) -> tuple[pd.DataFrame, float]:
    actual_clean = _clean_ohlc(actual)
    first_actual = pd.Timestamp(actual_clean.index.min())
    underlying_clean = _clean_ohlc(underlying).loc[first_actual:]
    index = underlying_clean.index.intersection(actual_clean.index)
    underlying_clean = underlying_clean.loc[index]
    actual_clean = actual_clean.loc[index]
    actual_norm = actual_clean["Close"] / float(actual_clean["Close"].iloc[0])
    actual_returns = actual_clean["Close"].pct_change().dropna()
    rows: list[dict[str, Any]] = []
    best_residual = 0.0
    best_error = float("inf")
    for residual in CALIBRATION_GRID:
        synthetic = dynamic_synthetic(
            underlying_clean,
            short_yield,
            leverage=leverage,
            residual_drag=float(residual),
        )
        synthetic_norm = synthetic["Close"] / float(synthetic["Close"].iloc[0])
        common = actual_norm.index.intersection(synthetic_norm.index)
        terminal_ratio = float(
            synthetic_norm.loc[common].iloc[-1]
            / actual_norm.loc[common].iloc[-1]
        )
        log_error = abs(math.log(max(1e-12, terminal_ratio)))
        synthetic_returns = synthetic["Close"].pct_change().dropna()
        return_index = actual_returns.index.intersection(synthetic_returns.index)
        correlation = float(
            actual_returns.loc[return_index].corr(
                synthetic_returns.loc[return_index]
            )
        )
        rmse = float(
            np.sqrt(
                np.mean(
                    (
                        actual_returns.loc[return_index].to_numpy()
                        - synthetic_returns.loc[return_index].to_numpy()
                    )
                    ** 2
                )
            )
        )
        rows.append(
            {
                "leverage": leverage,
                "residual_drag": float(residual),
                "terminal_ratio": terminal_ratio,
                "log_terminal_error": log_error,
                "daily_correlation": correlation,
                "daily_rmse": rmse,
            }
        )
        if log_error < best_error:
            best_error = log_error
            best_residual = float(residual)
    return pd.DataFrame(rows), best_residual


def splice_synthetic_actual(
    synthetic: pd.DataFrame, actual: pd.DataFrame
) -> pd.DataFrame:
    synthetic = _clean_ohlc(synthetic)
    actual = _clean_ohlc(actual)
    first = pd.Timestamp(actual.index.min())
    common = synthetic.index.intersection(actual.index)
    if first not in common:
        first = pd.Timestamp(common.min())
    scale = float(actual.loc[first, "Close"] / synthetic.loc[first, "Close"])
    scaled = synthetic * scale
    combined = scaled.copy()
    combined.loc[first:, ["Open", "Close"]] = actual.loc[
        first:, ["Open", "Close"]
    ]
    return combined.sort_index()


def cash_frame(index: pd.DatetimeIndex, short_yield: pd.Series) -> pd.DataFrame:
    rates = (
        pd.to_numeric(short_yield, errors="coerce")
        .reindex(index)
        .ffill()
        .bfill()
        .clip(lower=-1.0, upper=30.0)
        / 100.0
    )
    daily = rates.shift(1).fillna(0.0) / TRADING_DAYS
    close = (1.0 + daily).cumprod() * 100.0
    open_price = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({"Open": open_price, "Close": close}, index=index)


def aligned_frame(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    output = _clean_ohlc(frame).reindex(index)
    return output[["Open", "Close"]]


def build_primary_frames(
    *,
    cfg: dict,
    paths: Any,
    refresh: bool,
) -> tuple[
    dict[str, pd.DataFrame],
    pd.DataFrame,
    dict[str, float],
    dict[str, Any],
]:
    loaded, metadata = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    qqq_raw = read_price("QQQ", paths, refresh=refresh)
    qld_raw = read_price("QLD", paths, refresh=refresh)
    tqqq_raw = read_price("TQQQ", paths, refresh=refresh)
    irx_raw = read_price("^IRX", paths, refresh=refresh)
    calibration_2x, residual_2x = calibrate_residual(
        qqq_raw,
        irx_raw["Close"],
        qld_raw,
        leverage=2.0,
    )
    calibration_3x, residual_3x = calibrate_residual(
        qqq_raw,
        irx_raw["Close"],
        tqqq_raw,
        leverage=3.0,
    )
    synthetic_3x = dynamic_synthetic(
        qqq_raw,
        irx_raw["Close"],
        leverage=3.0,
        residual_drag=residual_3x,
    )
    spliced_tqqq = splice_synthetic_actual(synthetic_3x, tqqq_raw)
    master = loaded["QQQ"].loc[MODERN_START:].index
    frames = {
        "QQQ": aligned_frame(qqq_raw, master),
        "QLD": aligned_frame(qld_raw, master),
        "TQQQ": aligned_frame(spliced_tqqq, master),
        "CASH": cash_frame(master, irx_raw["Close"]),
        "KRW=X": loaded["KRW=X"].reindex(master).ffill(),
    }
    calibration = pd.concat([calibration_2x, calibration_3x], ignore_index=True)
    residuals = {"QLD_2x": residual_2x, "TQQQ_3x": residual_3x}
    return frames, calibration, residuals, metadata


def constant_fx(frame: pd.DataFrame, value: float = 1000.0) -> pd.DataFrame:
    output = frame.copy()
    output.loc[:, "Open"] = value
    output.loc[:, "Close"] = value
    return output


def build_stress_frames(
    *,
    underlying: pd.DataFrame,
    short_yield: pd.Series,
    residual_2x: float,
    residual_3x: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    underlying = _clean_ohlc(underlying)
    synthetic_2x = dynamic_synthetic(
        underlying,
        short_yield,
        leverage=2.0,
        residual_drag=residual_2x,
    )
    synthetic_3x = dynamic_synthetic(
        underlying,
        short_yield,
        leverage=3.0,
        residual_drag=residual_3x,
    )
    index = underlying.loc[start:end].index
    fx = constant_fx(underlying.loc[index], 1000.0)
    return {
        "QQQ": aligned_frame(underlying, index),
        "QLD": aligned_frame(synthetic_2x, index),
        "TQQQ": aligned_frame(synthetic_3x, index),
        "CASH": cash_frame(index, short_yield),
        "KRW=X": fx,
    }


def valid_start(frames: Mapping[str, pd.DataFrame], requested: pd.Timestamp) -> pd.Timestamp:
    starts = [pd.Timestamp(requested)]
    for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X"):
        valid = frames[asset][["Open", "Close"]].dropna()
        if valid.empty:
            raise ValueError(f"no usable prices for {asset}")
        starts.append(pd.Timestamp(valid.index.min()))
    return max(starts)


def metric_columns(
    simulation: SimulationResult, latest: pd.Timestamp
) -> dict[str, Any]:
    values = {
        f"full_{key}": value for key, value in simulation.metrics.items()
    }
    for prefix, (start, end) in DEV_FOLDS.items():
        metrics = simulation_period_metrics(simulation, start, end)
        values.update({f"{prefix}_{key}": value for key, value in metrics.items()})
    holdout = simulation_period_metrics(simulation, HOLDOUT_START, latest)
    values.update({f"holdout_{key}": value for key, value in holdout.items()})
    return values


def selection_fields(row: Mapping[str, Any]) -> dict[str, Any]:
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
    trades = float(row.get("full_trades_per_year", 0) or 0)
    score = (
        0.55 * mean_cagr
        + 0.30 * min_cagr
        + 0.015 * min(5.0, mean_calmar)
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
    candidates: Iterable[ModernCandidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    latest: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    candidates = list(candidates)
    for number, candidate in enumerate(candidates, start=1):
        record = candidate.record()
        try:
            targets = generate_targets(
                candidate,
                store=store,
                start=start,
                end=latest,
            )
            if targets.empty:
                raise ValueError("empty target schedule")
            simulation = simulate_close_to_close(
                targets=targets,
                frames=frames,
                start=start,
                end=latest,
                cost_bps=COST_BPS + SLIPPAGE_BPS,
            )
            values = metric_columns(simulation, latest)
            values.update(selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 1000 == 0:
            print(f"modern broad: {number}/{len(candidates)}", flush=True)
    return pd.DataFrame(rows)


def shortlist(
    broad: pd.DataFrame,
    candidate_map: Mapping[str, ModernCandidate],
    maximum: int = 320,
) -> list[ModernCandidate]:
    valid = broad.loc[
        broad["status"].eq("ok") & broad["selection_score"].notna()
    ].copy()
    selected: set[str] = set(
        valid.loc[
            valid["family"].isin(["buy_hold", "fixed_mix"]),
            "candidate_id",
        ]
    )
    selected.update(valid.nlargest(70, "selection_score")["candidate_id"])
    selected.update(valid.nlargest(30, "dev_mean_cagr")["candidate_id"])
    for limit in (-0.90, -0.70, -0.60, -0.50, -0.45, -0.40):
        eligible = valid.loc[valid["dev_worst_mdd"] >= limit]
        selected.update(eligible.nlargest(35, "selection_score")["candidate_id"])
        selected.update(eligible.nlargest(12, "dev_mean_cagr")["candidate_id"])
    for _, group in valid.groupby("family"):
        selected.update(group.nlargest(8, "selection_score")["candidate_id"])
    ranked = valid.loc[valid["candidate_id"].isin(selected)].sort_values(
        "selection_score", ascending=False
    )
    if len(ranked) > maximum:
        baselines = set(
            ranked.loc[
                ranked["family"].isin(["buy_hold", "fixed_mix"]),
                "candidate_id",
            ]
        )
        keep = set(
            ranked.head(maximum - len(baselines))["candidate_id"]
        ) | baselines
        ranked = ranked.loc[ranked["candidate_id"].isin(keep)]
    return [candidate_map[cid] for cid in ranked["candidate_id"]]


def exact_evaluate(
    candidates: Iterable[ModernCandidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    rows: list[dict[str, Any]] = []
    simulations: dict[str, SimulationResult] = {}
    candidates = list(candidates)
    for number, candidate in enumerate(candidates, start=1):
        record = candidate.record()
        try:
            targets = generate_targets(
                candidate,
                store=store,
                start=start,
                end=latest,
            )
            simulation = simulate_next_open(
                targets=targets,
                frames=frames,
                start=start,
                end=latest,
                cost_bps=COST_BPS,
                slippage_bps=SLIPPAGE_BPS,
            )
            if not _finite(simulation.metrics.get("cagr")):
                raise ValueError("invalid exact simulation")
            simulations[candidate.candidate_id] = simulation
            values = metric_columns(simulation, latest)
            values.update(selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 50 == 0:
            print(f"modern exact: {number}/{len(candidates)}", flush=True)
    return pd.DataFrame(rows), simulations


def taxable_shortlist(
    exact: pd.DataFrame,
    candidate_map: Mapping[str, ModernCandidate],
    maximum: int = 60,
) -> list[ModernCandidate]:
    valid = exact.loc[
        exact["status"].eq("ok") & exact["selection_score"].notna()
    ].copy()
    selected: set[str] = set(
        valid.loc[
            valid["family"].isin(["buy_hold", "fixed_mix"]),
            "candidate_id",
        ]
    )
    selected.update(valid.nlargest(20, "selection_score")["candidate_id"])
    for limit in (-0.90, -0.70, -0.60, -0.55, -0.50, -0.45):
        eligible = valid.loc[valid["dev_worst_mdd"] >= limit]
        selected.update(eligible.nlargest(12, "selection_score")["candidate_id"])
    ranked = valid.loc[valid["candidate_id"].isin(selected)].sort_values(
        "selection_score", ascending=False
    )
    if len(ranked) > maximum:
        baseline_ids = set(
            ranked.loc[
                ranked["family"].isin(["buy_hold", "fixed_mix"]),
                "candidate_id",
            ]
        )
        keep = set(ranked.head(maximum - len(baseline_ids))["candidate_id"]) | baseline_ids
        ranked = ranked.loc[ranked["candidate_id"].isin(keep)]
    return [candidate_map[cid] for cid in ranked["candidate_id"]]


def taxable_fold_metrics(
    candidate: ModernCandidate,
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> TaxableSimulation:
    targets = generate_targets(candidate, store=store, start=start, end=end)
    return GenericTaxableSimulator(
        frames=frames,
        targets=targets,
        start=start,
        end=end,
    ).run()


def taxable_evaluate(
    candidates: Iterable[ModernCandidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, TaxableSimulation]]:
    rows: list[dict[str, Any]] = []
    full_simulations: dict[str, TaxableSimulation] = {}
    candidates = list(candidates)
    for number, candidate in enumerate(candidates, start=1):
        record = candidate.record()
        try:
            full = taxable_fold_metrics(
                candidate,
                frames=frames,
                store=store,
                start=start,
                end=latest,
            )
            full_simulations[candidate.candidate_id] = full
            values = {
                f"full_{key}": value for key, value in full.metrics.items()
            }
            fold_xirrs: list[float] = []
            fold_mdds: list[float] = []
            for prefix, (fold_start, fold_end) in DEV_FOLDS.items():
                simulation = taxable_fold_metrics(
                    candidate,
                    frames=frames,
                    store=store,
                    start=fold_start,
                    end=fold_end,
                )
                values.update(
                    {
                        f"{prefix}_{key}": value
                        for key, value in simulation.metrics.items()
                    }
                )
                if _finite(simulation.metrics.get("after_tax_xirr")):
                    fold_xirrs.append(float(simulation.metrics["after_tax_xirr"]))
                if _finite(simulation.metrics.get("mdd")):
                    fold_mdds.append(float(simulation.metrics["mdd"]))
            holdout = taxable_fold_metrics(
                candidate,
                frames=frames,
                store=store,
                start=HOLDOUT_START,
                end=latest,
            )
            values.update(
                {
                    f"holdout_{key}": value
                    for key, value in holdout.metrics.items()
                }
            )
            if len(fold_xirrs) != len(DEV_FOLDS):
                raise ValueError("missing taxable development XIRR")
            dev_mean = float(np.mean(fold_xirrs))
            dev_min = float(np.min(fold_xirrs))
            dev_worst_mdd = float(np.min(fold_mdds))
            signal_trades = float(values.get("full_trade_count", 0))
            score = (
                0.60 * dev_mean
                + 0.40 * dev_min
                - 0.0003 * math.log1p(max(0.0, signal_trades))
            )
            rows.append(
                {
                    **record,
                    "status": "ok",
                    "error": None,
                    **values,
                    "dev_mean_after_tax_xirr": dev_mean,
                    "dev_min_after_tax_xirr": dev_min,
                    "dev_worst_mdd": dev_worst_mdd,
                    "taxable_selection_score": score,
                }
            )
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 10 == 0:
            print(f"modern taxable: {number}/{len(candidates)}", flush=True)
    return pd.DataFrame(rows), full_simulations


def select_finalists(taxable: pd.DataFrame) -> pd.DataFrame:
    valid = taxable.loc[
        taxable["status"].eq("ok")
        & taxable["taxable_selection_score"].notna()
        & (taxable["dev_min_after_tax_xirr"] > 0)
    ].copy()
    categories = [
        ("absolute_return", None),
        ("aggressive_balance", -0.70),
        ("balanced", -0.55),
        ("survival", -0.45),
    ]
    rows: list[dict[str, Any]] = []
    for category, limit in categories:
        eligible = valid
        if limit is not None:
            eligible = eligible.loc[eligible["dev_worst_mdd"] >= limit]
        if eligible.empty:
            continue
        best = eligible.sort_values(
            [
                "taxable_selection_score",
                "dev_mean_after_tax_xirr",
                "dev_min_after_tax_xirr",
            ],
            ascending=False,
        ).iloc[0]
        rows.append({"category": category, **best.to_dict()})
    for baseline_id in (
        "buy_hold_assetQQQ",
        "buy_hold_assetQLD",
        "buy_hold_assetTQQQ",
    ):
        row = valid.loc[valid["candidate_id"] == baseline_id]
        if not row.empty:
            rows.append({"category": baseline_id, **row.iloc[0].to_dict()})
    return pd.DataFrame(rows).drop_duplicates("category")


def evaluate_reference_period(
    candidate: ModernCandidate,
    *,
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    store = FeatureStore(close_panel(frames))
    actual_start = valid_start(frames, start)
    actual_end = min(pd.Timestamp(end), pd.Timestamp(frames["QQQ"]["Close"].dropna().index.max()))
    simulation = taxable_fold_metrics(
        candidate,
        frames=frames,
        store=store,
        start=actual_start,
        end=actual_end,
    )
    return simulation.metrics


def stress_tables(
    finalists: pd.DataFrame,
    candidate_map: Mapping[str, ModernCandidate],
    *,
    qqq_raw: pd.DataFrame,
    ndx_raw: pd.DataFrame,
    short_yield: pd.Series,
    residuals: Mapping[str, float],
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected_ids = list(dict.fromkeys(finalists["candidate_id"].tolist()))
    selected = {
        candidate_id: candidate_map[candidate_id]
        for candidate_id in selected_ids
        if candidate_id in candidate_map
    }
    dotcom_frames = build_stress_frames(
        underlying=qqq_raw,
        short_yield=short_yield,
        residual_2x=float(residuals["QLD_2x"]),
        residual_3x=float(residuals["TQQQ_3x"]),
        start=DOTCOM_START,
        end=DOTCOM_END,
    )
    ndx_frames = build_stress_frames(
        underlying=ndx_raw,
        short_yield=short_yield,
        residual_2x=float(residuals["QLD_2x"]),
        residual_3x=float(residuals["TQQQ_3x"]),
        start=NDX_START,
        end=latest,
    )
    dotcom_rows: list[dict[str, Any]] = []
    ndx_rows: list[dict[str, Any]] = []
    for candidate_id, candidate in selected.items():
        try:
            metrics = evaluate_reference_period(
                candidate,
                frames=dotcom_frames,
                start=DOTCOM_START,
                end=DOTCOM_END,
            )
            dotcom_rows.append(
                {"candidate_id": candidate_id, **metrics}
            )
        except Exception as exc:
            dotcom_rows.append({"candidate_id": candidate_id, "error": str(exc)})
        try:
            metrics = evaluate_reference_period(
                candidate,
                frames=ndx_frames,
                start=NDX_START,
                end=latest,
            )
            ndx_rows.append({"candidate_id": candidate_id, **metrics})
        except Exception as exc:
            ndx_rows.append({"candidate_id": candidate_id, "error": str(exc)})
    return pd.DataFrame(dotcom_rows), pd.DataFrame(ndx_rows)


def actual_tqqq_validation(
    finalists: pd.DataFrame,
    candidate_map: Mapping[str, ModernCandidate],
    *,
    primary_frames: Mapping[str, pd.DataFrame],
    actual_tqqq: pd.DataFrame,
    latest: pd.Timestamp,
) -> pd.DataFrame:
    index = primary_frames["QQQ"].loc[ACTUAL_TQQQ_START:latest].index
    frames = dict(primary_frames)
    frames["TQQQ"] = aligned_frame(actual_tqqq, index)
    frames["QQQ"] = primary_frames["QQQ"].reindex(index)
    frames["QLD"] = primary_frames["QLD"].reindex(index)
    frames["CASH"] = primary_frames["CASH"].reindex(index)
    frames["KRW=X"] = primary_frames["KRW=X"].reindex(index).ffill()
    store = FeatureStore(close_panel(frames))
    rows: list[dict[str, Any]] = []
    for candidate_id in dict.fromkeys(finalists["candidate_id"].tolist()):
        if candidate_id not in candidate_map:
            continue
        candidate = candidate_map[candidate_id]
        try:
            simulation = taxable_fold_metrics(
                candidate,
                frames=frames,
                store=store,
                start=valid_start(frames, ACTUAL_TQQQ_START),
                end=latest,
            )
            rows.append({"candidate_id": candidate_id, **simulation.metrics})
        except Exception as exc:
            rows.append({"candidate_id": candidate_id, "error": str(exc)})
    return pd.DataFrame(rows)


def params_summary(params_json: str) -> str:
    try:
        params = json.loads(params_json)
    except Exception:
        return params_json
    ordered = (
        "asset",
        "allocation",
        "frequency",
        "entry_ma",
        "exit_ma",
        "long_ma",
        "breakout_days",
        "momentum_days",
        "strong_allocation",
        "moderate_asset",
        "max_vol",
        "drawdown",
        "recovery_ma",
        "exit_rule",
        "trailing_stop",
        "parts",
        "confirm",
        "risk_off",
    )
    return ", ".join(
        f"{key}={params[key]}" for key in ordered if key in params
    )


def build_report(
    *,
    manifest: Mapping[str, Any],
    finalists: pd.DataFrame,
    actual_validation: pd.DataFrame,
    dotcom: pd.DataFrame,
    extreme: pd.DataFrame,
) -> str:
    lines = [
        "# Quant Guardian Equity v2 현대시장 기준 재연구",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 주 최적화 기간: {manifest['modern_start']}~{manifest['data_end']}",
        "- 개발구간: 2006~2011, 2012~2018, 2019~2022",
        "- 2023~최신: 홀드아웃 보고만 하고 후보 선택에는 사용하지 않음",
        "- 실제 TQQQ 검증: 2010~최신",
        "- 닷컴 스트레스: 1999~2003",
        "- 1985 구간: 최종 참고용 극단 스트레스",
        f"- 초기자금 {_fmt_krw(manifest['initial_capital_krw'])}, 월 {_fmt_krw(manifest['monthly_contribution_krw'])}",
        "- 과세계좌, 수수료·슬리피지·환전비용·양도세 근사",
        "- BTC 및 현재 주식 Telegram 변경 없음",
        "",
        "## 최종 후보",
        "",
        "| 구분 | 전략군 | 규칙 | 개발 평균 세후 XIRR | 개발 최저 세후 XIRR | 개발 최악 MDD | 전체 세후 XIRR | 전체 MDD | 홀드아웃 세후 XIRR | 세후 최종액 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in finalists.iterrows():
        lines.append(
            f"| {row['category']} | {row['family']} | {params_summary(str(row['params_json']))} | "
            f"{_fmt_pct(row['dev_mean_after_tax_xirr'])} | "
            f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
            f"{_fmt_pct(row['dev_worst_mdd'])} | "
            f"{_fmt_pct(row['full_after_tax_xirr'])} | "
            f"{_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['holdout_after_tax_xirr'])} | "
            f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 실제 TQQQ 2010년 이후 교차검증",
            "",
            "| 후보 | 세후 XIRR | TWR CAGR | MDD | 세후 최종액 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in actual_validation.iterrows():
        if row.get("error"):
            lines.append(f"| {row['candidate_id']} | 오류 | 오류 | 오류 | 오류 |")
        else:
            lines.append(
                f"| {row['candidate_id']} | {_fmt_pct(row.get('after_tax_xirr'))} | "
                f"{_fmt_pct(row.get('twr_cagr'))} | {_fmt_pct(row.get('mdd'))} | "
                f"{_fmt_krw(row.get('after_tax_liquidation_value_krw'))} |"
            )

    lines.extend(
        [
            "",
            "## 닷컴버블 1999~2003 스트레스",
            "",
            "| 후보 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in dotcom.iterrows():
        if row.get("error"):
            lines.append(f"| {row['candidate_id']} | 오류 | 오류 | 오류 | 오류 |")
        else:
            lines.append(
                f"| {row['candidate_id']} | {_fmt_pct(row.get('after_tax_xirr'))} | "
                f"{_fmt_pct(row.get('mdd'))} | "
                f"{_fmt_pct(row.get('minimum_vs_contributions'))} | "
                f"{_fmt_krw(row.get('after_tax_liquidation_value_krw'))} |"
            )

    lines.extend(
        [
            "",
            "## 1985~최신 극단 참고",
            "",
            "| 후보 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in extreme.iterrows():
        if row.get("error"):
            lines.append(f"| {row['candidate_id']} | 오류 | 오류 | 오류 | 오류 |")
        else:
            lines.append(
                f"| {row['candidate_id']} | {_fmt_pct(row.get('after_tax_xirr'))} | "
                f"{_fmt_pct(row.get('mdd'))} | "
                f"{_fmt_pct(row.get('minimum_vs_contributions'))} | "
                f"{_fmt_krw(row.get('after_tax_liquidation_value_krw'))} |"
            )

    lines.extend(
        [
            "",
            "## 선정 원칙",
            "",
            "- 수익률 최적화는 2006년 이후 현대시장에 우선 가중했다.",
            "- 2023년 이후 홀드아웃은 선택에 사용하지 않았다.",
            "- 닷컴·1985 결과는 전략을 자동 탈락시키는 기준이 아니라 극단위험 참고자료로 사용했다.",
            "- 모든 후보는 동일한 초기 8,000만원, 월 50만원, 과세계좌 조건에서 세후 XIRR과 MDD를 함께 계산했다.",
            "- 상장 전 TQQQ 구간은 QQQ/NDX 일수익률, 단기금리 금융비용, 실제 ETF 보정으로 만든 합성값이다.",
            "- 과거 후보를 많이 탐색했으므로 선택 편향은 남아 있으며 최종 적용 전 Shadow 검증이 필요하다.",
        ]
    )
    return "\n".join(lines)


def run_research(
    *,
    refresh: bool,
    config_path: Path,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(UTC)
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, calibration, residuals, metadata = build_primary_frames(
        cfg=cfg,
        paths=paths,
        refresh=refresh,
    )
    start = valid_start(frames, MODERN_START)
    latest = min(
        pd.Timestamp(frames[asset]["Close"].dropna().index.max())
        for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
    )
    store = FeatureStore(close_panel(frames))
    candidates = build_candidate_grid()
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    print(f"modern candidates: {len(candidates):,}", flush=True)
    broad = broad_search(
        candidates,
        frames=frames,
        store=store,
        start=start,
        latest=latest,
    )
    exact_candidates = shortlist(broad, candidate_map, maximum=320)
    exact, _ = exact_evaluate(
        exact_candidates,
        frames=frames,
        store=store,
        start=start,
        latest=latest,
    )
    tax_candidates = taxable_shortlist(exact, candidate_map, maximum=60)
    taxable, _ = taxable_evaluate(
        tax_candidates,
        frames=frames,
        store=store,
        start=start,
        latest=latest,
    )
    finalists = select_finalists(taxable)

    qqq_raw = read_price("QQQ", paths, refresh=refresh)
    ndx_raw = read_price("^NDX", paths, refresh=refresh)
    short_yield = read_price("^IRX", paths, refresh=refresh)["Close"]
    actual_tqqq = read_price("TQQQ", paths, refresh=refresh)
    actual_validation = actual_tqqq_validation(
        finalists,
        candidate_map,
        primary_frames=frames,
        actual_tqqq=actual_tqqq,
        latest=latest,
    )
    dotcom, extreme = stress_tables(
        finalists,
        candidate_map,
        qqq_raw=qqq_raw,
        ndx_raw=ndx_raw,
        short_yield=short_yield,
        residuals=residuals,
        latest=latest,
    )

    outputs = {
        "broad": paths.output / "equity_v2_modern_broad.csv",
        "exact": paths.output / "equity_v2_modern_exact.csv",
        "taxable": paths.output / "equity_v2_modern_taxable.csv",
        "finalists": paths.output / "equity_v2_modern_finalists.csv",
        "calibration": paths.output / "equity_v2_modern_calibration.csv",
        "actual_validation": paths.output / "equity_v2_modern_actual_tqqq.csv",
        "dotcom": paths.output / "equity_v2_modern_dotcom.csv",
        "extreme": paths.output / "equity_v2_modern_1985_reference.csv",
        "manifest": paths.output / "equity_v2_modern_manifest.json",
        "report": paths.output / "equity_v2_modern_report.md",
    }
    broad.to_csv(outputs["broad"], index=False, encoding="utf-8-sig")
    exact.to_csv(outputs["exact"], index=False, encoding="utf-8-sig")
    taxable.to_csv(outputs["taxable"], index=False, encoding="utf-8-sig")
    finalists.to_csv(outputs["finalists"], index=False, encoding="utf-8-sig")
    calibration.to_csv(outputs["calibration"], index=False, encoding="utf-8-sig")
    actual_validation.to_csv(
        outputs["actual_validation"], index=False, encoding="utf-8-sig"
    )
    dotcom.to_csv(outputs["dotcom"], index=False, encoding="utf-8-sig")
    extreme.to_csv(outputs["extreme"], index=False, encoding="utf-8-sig")

    manifest = {
        "schema_version": "equity-v2-modern-2006-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now.isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "modern_start": start.date().isoformat(),
        "data_end": latest.date().isoformat(),
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "candidate_count": len(candidates),
        "broad_success_count": int((broad["status"] == "ok").sum()),
        "exact_count": len(exact_candidates),
        "taxable_count": len(tax_candidates),
        "development_folds": {
            name: [fold_start.date().isoformat(), fold_end.date().isoformat()]
            for name, (fold_start, fold_end) in DEV_FOLDS.items()
        },
        "holdout_start": HOLDOUT_START.date().isoformat(),
        "residual_drags": residuals,
        "data_metadata": metadata,
        "finalists": [
            {
                "category": row["category"],
                "candidate_id": row["candidate_id"],
                "family": row["family"],
                "params": json.loads(row["params_json"]),
            }
            for _, row in finalists.iterrows()
        ],
        "limitations": [
            "pre-2010 TQQQ is synthetic",
            "dotcom and 1985 are stress references, not selection periods",
            "tax accounting is an average-cost approximation",
            "large candidate search creates selection bias",
            "2026 is incomplete",
            "no live approval",
        ],
    }
    _write_json(outputs["manifest"], manifest)
    outputs["report"].write_text(
        build_report(
            manifest=manifest,
            finalists=finalists,
            actual_validation=actual_validation,
            dotcom=dotcom,
            extreme=extreme,
        ),
        encoding="utf-8-sig",
    )
    return {
        "manifest": manifest,
        "outputs": {key: str(path) for key, path in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Modern-regime Equity v2 optimization with common taxable DCA conditions"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run_research(refresh=args.refresh, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
