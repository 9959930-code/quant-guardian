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
    Candidate,
    FeatureStore,
    SimulationResult,
    annual_returns,
    build_candidate_grid,
    candidate_id,
    candidate_required_assets,
    candidate_track,
    close_panel,
    first_valid_date,
    generate_targets,
    load_market_data,
    simulation_period_metrics,
    simulate_close_to_close,
    simulate_next_open,
)
from quant_guardian import (
    DEFAULT_CONFIG,
    load_config,
    qg_core_backtest,
    resolve_paths,
)


STRATEGY_VERSION = "equity-v2-leverage-research-0.1"
DISCOVERY_END = pd.Timestamp("2018-12-31")
VALIDATION_START = pd.Timestamp("2019-01-01")
VALIDATION_END = pd.Timestamp("2022-12-31")
HOLDOUT_START = pd.Timestamp("2023-01-01")
TRACK_STARTS = {
    "core_20y": pd.Timestamp("2005-01-03"),
    "qld_20y": pd.Timestamp("2006-07-01"),
    "tqqq_actual": pd.Timestamp("2010-03-01"),
    "smh_common": pd.Timestamp("2012-01-03"),
}
MDD_TIERS = (-0.20, -0.30, -0.40, -0.50, -0.60)
DEFAULT_COST_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 5.0


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_pct(value: Any) -> str:
    if not _finite(value):
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    if not _finite(value):
        return "n/a"
    return f"{float(value):.{digits}f}"


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


def _candidate_start(
    candidate: Candidate,
    frames: Mapping[str, pd.DataFrame],
) -> pd.Timestamp:
    minimum = TRACK_STARTS[candidate.track]
    return first_valid_date(
        frames, candidate_required_assets(candidate), minimum
    )


def _targets_for(
    candidate: Candidate,
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    end: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.DataFrame]:
    start = _candidate_start(candidate, frames)
    targets = generate_targets(
        candidate,
        frames=frames,
        store=store,
        start=start,
        end=end,
    )
    return start, targets


def broad_search(
    *,
    candidates: list[Candidate],
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    end: pd.Timestamp,
    cost_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for number, candidate in enumerate(candidates, start=1):
        record = candidate.to_record()
        try:
            start, targets = _targets_for(
                candidate, frames=frames, store=store, end=end
            )
            if start >= DISCOVERY_END or targets.empty:
                raise ValueError("no discovery-period target schedule")
            simulation = simulate_close_to_close(
                targets=targets,
                frames=frames,
                start=start,
                end=min(DISCOVERY_END, end),
                cost_bps=cost_bps,
            )
            metrics = simulation.metrics
            if not _finite(metrics.get("cagr")):
                raise ValueError("invalid broad-search metrics")
            rows.append(
                {
                    **record,
                    "status": "ok",
                    "error": None,
                    **{
                        f"discovery_{key}": value
                        for key, value in metrics.items()
                    },
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **record,
                    "status": "error",
                    "error": str(exc),
                }
            )
        if number % 500 == 0:
            print(f"broad search: {number}/{len(candidates)}")
    return pd.DataFrame(rows)


def shortlist_candidates(
    broad: pd.DataFrame,
    candidate_map: Mapping[str, Candidate],
    *,
    maximum: int = 500,
) -> list[Candidate]:
    valid = broad.loc[
        (broad["status"] == "ok")
        & pd.to_numeric(broad["discovery_cagr"], errors="coerce").notna()
    ].copy()
    selected: set[str] = set()

    selected.update(
        valid.loc[valid["family"].isin(["buy_hold", "fixed_mix"]), "candidate_id"]
    )

    for track, track_rows in valid.groupby("track"):
        for threshold in (None, *MDD_TIERS):
            eligible = track_rows
            if threshold is not None:
                eligible = eligible.loc[eligible["discovery_mdd"] >= threshold]
            selected.update(
                eligible.nlargest(15, "discovery_cagr")["candidate_id"]
            )
            selected.update(
                eligible.nlargest(7, "discovery_calmar")["candidate_id"]
            )

    for (track, family), group in valid.groupby(["track", "family"]):
        selected.update(group.nlargest(4, "discovery_cagr")["candidate_id"])
        selected.update(group.nlargest(2, "discovery_calmar")["candidate_id"])

    ranked = valid.loc[valid["candidate_id"].isin(selected)].copy()
    ranked["selection_hint"] = (
        ranked["discovery_cagr"].fillna(-10)
        + 0.15 * ranked["discovery_calmar"].fillna(-10).clip(-10, 10)
    )
    if len(ranked) > maximum:
        baseline_ids = set(
            ranked.loc[
                ranked["family"].isin(["buy_hold", "fixed_mix"]),
                "candidate_id",
            ]
        )
        keep = set(
            ranked.nlargest(maximum - len(baseline_ids), "selection_hint")[
                "candidate_id"
            ]
        ) | baseline_ids
        ranked = ranked.loc[ranked["candidate_id"].isin(keep)]
    return [candidate_map[cid] for cid in ranked["candidate_id"]]


def _period_columns(
    simulation: SimulationResult,
    *,
    latest: pd.Timestamp,
) -> dict[str, Any]:
    periods = {
        "discovery": ("1900-01-01", DISCOVERY_END),
        "validation": (VALIDATION_START, VALIDATION_END),
        "holdout": (HOLDOUT_START, latest),
        "gfc": ("2007-10-09", "2009-03-09"),
        "covid": ("2020-02-19", "2020-03-23"),
        "year2022": ("2022-01-03", "2022-12-30"),
    }
    values: dict[str, Any] = {}
    for prefix, (start, end) in periods.items():
        metrics = simulation_period_metrics(simulation, start, end)
        for key, value in metrics.items():
            values[f"{prefix}_{key}"] = value
    return values


def _krw_metrics(
    simulation: SimulationResult,
    frames: Mapping[str, pd.DataFrame],
) -> dict[str, Any]:
    if simulation.daily.empty or "KRW=X" not in frames:
        return {}
    fx = frames["KRW=X"]["Close"].reindex(simulation.daily.index).ffill()
    valid = fx.dropna().index
    if len(valid) < 2:
        return {}
    equity = simulation.daily.loc[valid, "equity"]
    normalized_fx = fx.loc[valid] / float(fx.loc[valid].iloc[0])
    krw_equity = equity * normalized_fx
    daily = simulation.daily.loc[valid].copy()
    daily["equity"] = krw_equity
    wrapped = SimulationResult(
        daily=daily,
        trades=simulation.trades,
        metrics={},
    )
    full = simulation_period_metrics(
        wrapped, valid.min(), valid.max()
    )
    holdout = simulation_period_metrics(
        wrapped, HOLDOUT_START, valid.max()
    )
    return {
        **{f"krw_{key}": value for key, value in full.items()},
        **{f"krw_holdout_{key}": value for key, value in holdout.items()},
    }


def exact_evaluate(
    *,
    candidates: Iterable[Candidate],
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
        record = candidate.to_record()
        try:
            start, targets = _targets_for(
                candidate, frames=frames, store=store, end=latest
            )
            if targets.empty:
                raise ValueError("empty target schedule")
            simulation = simulate_next_open(
                targets=targets,
                frames=frames,
                start=start,
                end=latest,
                cost_bps=cost_bps,
                slippage_bps=slippage_bps,
                delay_days=delay_days,
            )
            if not _finite(simulation.metrics.get("cagr")):
                raise ValueError("invalid exact metrics")
            simulations[candidate.candidate_id] = simulation
            row = {
                **record,
                "status": "ok",
                "error": None,
                **{f"full_{key}": value for key, value in simulation.metrics.items()},
                **_period_columns(simulation, latest=latest),
                **_krw_metrics(simulation, frames),
            }
            rows.append(row)
        except Exception as exc:
            rows.append(
                {
                    **record,
                    "status": "error",
                    "error": str(exc),
                }
            )
        if number % 50 == 0:
            print(f"exact evaluation: {number}/{len(candidate_list)}")
    return pd.DataFrame(rows), simulations


def add_selection_scores(exact: pd.DataFrame) -> pd.DataFrame:
    out = exact.copy()
    valid = (
        out["status"].eq("ok")
        & pd.to_numeric(out["discovery_cagr"], errors="coerce").notna()
        & pd.to_numeric(out["validation_cagr"], errors="coerce").notna()
    )
    out["selection_score"] = np.nan
    out["robust_cagr"] = np.nan
    out.loc[valid, "robust_cagr"] = np.minimum(
        out.loc[valid, "discovery_cagr"],
        out.loc[valid, "validation_cagr"],
    )
    turnover_penalty = np.log1p(
        out.loc[valid, "full_trades_per_year"].clip(lower=0)
    )
    out.loc[valid, "selection_score"] = (
        0.40 * out.loc[valid, "discovery_cagr"]
        + 0.45 * out.loc[valid, "validation_cagr"]
        + 0.15 * out.loc[valid, "robust_cagr"]
        - 0.0025 * turnover_penalty
    )
    return out


def _eligible(
    frame: pd.DataFrame,
    *,
    mdd_limit: float | None,
    leveraged: bool | None = None,
    max_trades: float | None = None,
    families: set[str] | None = None,
) -> pd.DataFrame:
    result = frame.loc[
        frame["status"].eq("ok")
        & frame["selection_score"].notna()
        & (frame["discovery_cagr"] > 0)
        & (frame["validation_cagr"] > 0)
    ].copy()
    if mdd_limit is not None:
        result = result.loc[
            (result["discovery_mdd"] >= mdd_limit)
            & (result["validation_mdd"] >= mdd_limit)
            & (result["full_mdd"] >= mdd_limit)
        ]
    if leveraged is True:
        result = result.loc[result["full_leveraged_exposure"] > 0.01]
    elif leveraged is False:
        result = result.loc[result["full_leveraged_exposure"] <= 0.01]
    if max_trades is not None:
        result = result.loc[result["full_trades_per_year"] <= max_trades]
    if families is not None:
        result = result.loc[result["family"].isin(families)]
    return result


def select_research_winners(exact: pd.DataFrame) -> pd.DataFrame:
    categories: list[tuple[str, dict[str, Any]]] = [
        ("absolute_return", {"mdd_limit": None}),
        ("mdd_20", {"mdd_limit": -0.20}),
        ("mdd_30", {"mdd_limit": -0.30}),
        ("mdd_40", {"mdd_limit": -0.40}),
        ("mdd_50", {"mdd_limit": -0.50}),
        ("mdd_60", {"mdd_limit": -0.60}),
        (
            "leveraged_mdd_50",
            {"mdd_limit": -0.50, "leveraged": True},
        ),
        (
            "nonleveraged_mdd_40",
            {"mdd_limit": -0.40, "leveraged": False},
        ),
        (
            "low_turnover_mdd_50",
            {"mdd_limit": -0.50, "max_trades": 2.0},
        ),
        (
            "buy_then_hold_mdd_60",
            {
                "mdd_limit": -0.60,
                "max_trades": 4.0,
                "families": {
                    "trend_hold",
                    "ma_momentum",
                    "drawdown_recovery",
                    "pullback_hold",
                    "breakout_hold",
                },
            },
        ),
    ]
    rows: list[dict[str, Any]] = []
    for category, kwargs in categories:
        eligible = _eligible(exact, **kwargs)
        if eligible.empty:
            continue
        best = eligible.sort_values(
            ["selection_score", "validation_cagr", "discovery_cagr"],
            ascending=False,
        ).iloc[0]
        rows.append({"category": category, **best.to_dict()})
    return pd.DataFrame(rows)


def _recursive_replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, tuple):
        return tuple(_recursive_replace(item, old, new) for item in value)
    if isinstance(value, list):
        return [_recursive_replace(item, old, new) for item in value]
    if isinstance(value, dict):
        return {
            _recursive_replace(key, old, new): _recursive_replace(item, old, new)
            for key, item in value.items()
        }
    return value


def _neighbor_values(key: str, value: Any) -> list[Any]:
    if key in {"sma", "recovery_sma", "exit_sma", "breakout_days"}:
        numeric = int(value)
        return sorted(
            {
                max(20, numeric - 50),
                max(20, numeric - 20),
                numeric,
                numeric + 20,
                numeric + 50,
            }
        )
    if key in {"momentum_days", "vol_days", "slope_days"}:
        numeric = int(value)
        return sorted({max(0, numeric - 42), numeric, numeric + 42})
    if key in {"drawdown", "trailing_stop"}:
        numeric = float(value)
        return sorted(
            {
                round(numeric - 0.05, 3),
                round(numeric, 3),
                round(min(-0.05, numeric + 0.05), 3),
            }
        )
    if key == "strong_threshold":
        numeric = float(value)
        return sorted(
            {
                max(0.0, round(numeric - 0.05, 3)),
                round(numeric, 3),
                round(numeric + 0.05, 3),
            }
        )
    if key == "max_vol":
        numeric = float(value)
        return sorted(
            {
                max(0.15, round(numeric - 0.10, 3)),
                round(numeric, 3),
                round(numeric + 0.10, 3),
            }
        )
    if key in {"entry_parts", "exit_parts"}:
        return [1, 2, 3]
    if key == "frequency":
        return ["weekly", "monthly"]
    if key == "defense":
        return ["CASH", "best"]
    return [value]


def generate_local_neighbors(candidate: Candidate) -> list[Candidate]:
    base = dict(candidate.params)
    neighbors: dict[str, Candidate] = {}
    tunable = [
        key
        for key in (
            "sma",
            "recovery_sma",
            "exit_sma",
            "breakout_days",
            "momentum_days",
            "vol_days",
            "slope_days",
            "drawdown",
            "trailing_stop",
            "strong_threshold",
            "max_vol",
            "entry_parts",
            "exit_parts",
            "frequency",
            "defense",
        )
        if key in base
    ]
    for key in tunable:
        for value in _neighbor_values(key, base[key]):
            if value == base[key]:
                continue
            params = dict(base)
            params[key] = value
            cid = candidate_id(candidate.family, **params)
            provisional = Candidate(cid, candidate.family, "pending", params)
            neighbors[cid] = Candidate(
                cid, candidate.family, candidate_track(provisional), params
            )
    return list(neighbors.values())


def refine_candidates(
    *,
    winners: pd.DataFrame,
    candidate_map: Mapping[str, Candidate],
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, Candidate]]:
    seed_ids = list(dict.fromkeys(winners["candidate_id"].tolist()))
    pool: dict[str, Candidate] = {
        cid: candidate_map[cid] for cid in seed_ids if cid in candidate_map
    }
    for cid in list(pool):
        for neighbor in generate_local_neighbors(pool[cid]):
            pool[neighbor.candidate_id] = neighbor
    refined, _ = exact_evaluate(
        candidates=pool.values(),
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    refined = add_selection_scores(refined)
    return refined, pool


def select_final_winners(refined: pd.DataFrame) -> pd.DataFrame:
    return select_research_winners(refined)


def robustness_checks(
    *,
    winners: pd.DataFrame,
    candidate_map: Mapping[str, Candidate],
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, winner in winners.iterrows():
        cid = str(winner["candidate_id"])
        if cid in seen or cid not in candidate_map:
            continue
        seen.add(cid)
        candidate = candidate_map[cid]
        scenarios: list[tuple[str, Candidate, float, float, int, pd.Timestamp | None]] = [
            ("base", candidate, cost_bps, slippage_bps, 0, None),
            ("cost_2x", candidate, cost_bps * 2, slippage_bps * 2, 0, None),
            ("delay_1d", candidate, cost_bps, slippage_bps, 1, None),
            ("delay_3d", candidate, cost_bps, slippage_bps, 3, None),
        ]
        required = candidate_required_assets(candidate)
        if "SOXX" in required:
            params = _recursive_replace(dict(candidate.params), "SOXX", "SMH")
            alt_cid = candidate_id(candidate.family, **params)
            provisional = Candidate(alt_cid, candidate.family, "pending", params)
            alt = Candidate(
                alt_cid, candidate.family, candidate_track(provisional), params
            )
            scenarios.append(
                ("soxx_to_smh", alt, cost_bps, slippage_bps, 0, pd.Timestamp("2012-01-03"))
            )
        if "QLD" in required and "TQQQ" not in required:
            params = _recursive_replace(dict(candidate.params), "QLD", "TQQQ")
            alt_cid = candidate_id(candidate.family, **params)
            provisional = Candidate(alt_cid, candidate.family, "pending", params)
            alt = Candidate(
                alt_cid, candidate.family, candidate_track(provisional), params
            )
            scenarios.append(
                ("qld_to_tqqq", alt, cost_bps, slippage_bps, 0, pd.Timestamp("2010-03-01"))
            )

        for scenario, spec, fee, slip, delay, forced_start in scenarios:
            try:
                start, targets = _targets_for(
                    spec, frames=frames, store=store, end=latest
                )
                if forced_start is not None:
                    start = max(start, forced_start)
                simulation = simulate_next_open(
                    targets=targets,
                    frames=frames,
                    start=start,
                    end=latest,
                    cost_bps=fee,
                    slippage_bps=slip,
                    delay_days=delay,
                )
                rows.append(
                    {
                        "source_candidate_id": cid,
                        "scenario": scenario,
                        "candidate_id": spec.candidate_id,
                        **{f"full_{key}": value for key, value in simulation.metrics.items()},
                        **_period_columns(simulation, latest=latest),
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "source_candidate_id": cid,
                        "scenario": scenario,
                        "candidate_id": spec.candidate_id,
                        "error": str(exc),
                    }
                )
    return pd.DataFrame(rows)


def selected_annual_returns(
    *,
    winners: pd.DataFrame,
    candidate_map: Mapping[str, Candidate],
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    latest: pd.Timestamp,
    cost_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, winner in winners.iterrows():
        cid = str(winner["candidate_id"])
        if cid not in candidate_map:
            continue
        candidate = candidate_map[cid]
        start, targets = _targets_for(
            candidate, frames=frames, store=store, end=latest
        )
        simulation = simulate_next_open(
            targets=targets,
            frames=frames,
            start=start,
            end=latest,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        )
        for year, value in annual_returns(simulation.daily).items():
            rows.append(
                {
                    "category": winner["category"],
                    "candidate_id": cid,
                    "year": year,
                    "return": value,
                }
            )
    return pd.DataFrame(rows)


def current_strategy_baseline(cfg: dict, paths: Any) -> dict[str, Any]:
    try:
        result = qg_core_backtest(cfg, paths, refresh=True)
        return {
            "metrics": result.get("metrics", {}),
            "error": None,
        }
    except Exception as exc:
        return {"metrics": {}, "error": str(exc)}


def _params_summary(params_json: str) -> str:
    try:
        params = json.loads(params_json)
    except Exception:
        return params_json
    important = [
        "asset",
        "signal_mode",
        "universe",
        "mix",
        "sma",
        "momentum_days",
        "lookbacks",
        "drawdown",
        "recovery_sma",
        "breakout_days",
        "exit_rule",
        "exit_sma",
        "trailing_stop",
        "mode",
        "leverage_asset",
        "strong_threshold",
        "max_vol",
        "frequency",
        "defense",
        "entry_parts",
        "exit_parts",
    ]
    return ", ".join(
        f"{key}={params[key]}" for key in important if key in params
    )


def build_report(
    *,
    manifest: Mapping[str, Any],
    broad: pd.DataFrame,
    exact: pd.DataFrame,
    initial_winners: pd.DataFrame,
    refined: pd.DataFrame,
    final_winners: pd.DataFrame,
    robustness: pd.DataFrame,
    baseline: Mapping[str, Any],
) -> str:
    lines = [
        "# Quant Guardian Equity v2 레버리지 포함 1차 연구 결과",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 데이터 종료일: {manifest['data_end']}",
        f"- 1차 후보 수: {manifest['broad_candidate_count']:,}",
        f"- 정확 체결 재검증 후보: {manifest['shortlist_count']:,}",
        f"- 지역 재탐색 후보: {manifest['refinement_count']:,}",
        "- 상태: 연구 전용 / 주식 Telegram·사이트·실전 미반영",
        "- BTC 파트: 변경하지 않음",
        "",
        "## 연구 구조",
        "",
        "1. 실제 ETF 조정 OHLC로 광범위 후보를 종가-다음날 방식으로 1차 탐색",
        "2. 상위 후보를 다음 거래일 시가·비용·슬리피지로 재검증",
        "3. 2005~2018 발견, 2019~2022 검증으로 선택하고 2023~최신은 홀드아웃 보고",
        "4. 상위 후보 주변 파라미터를 다시 탐색한 뒤 위험수준별 최종 후보 선정",
        "",
        "## 최종 후보",
        "",
        "| 구분 | 전략군 | 파라미터 | 전체 CAGR | 전체 MDD | 검증 CAGR | 검증 MDD | 홀드아웃 CAGR | 홀드아웃 MDD | 거래/년 | 레버리지 노출 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in final_winners.iterrows():
        lines.append(
            f"| {row['category']} | {row['family']} | "
            f"{_params_summary(str(row['params_json']))} | "
            f"{_fmt_pct(row['full_cagr'])} | {_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_pct(row['validation_cagr'])} | {_fmt_pct(row['validation_mdd'])} | "
            f"{_fmt_pct(row['holdout_cagr'])} | {_fmt_pct(row['holdout_mdd'])} | "
            f"{_fmt_num(row['full_trades_per_year'])} | "
            f"{_fmt_pct(row['full_leveraged_exposure'])} |"
        )

    lines.extend(
        [
            "",
            "## 1차 선택 후보",
            "",
            "| 구분 | 후보 | 발견 CAGR | 검증 CAGR | 홀드아웃 CAGR | 전체 MDD |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in initial_winners.iterrows():
        lines.append(
            f"| {row['category']} | `{row['candidate_id']}` | "
            f"{_fmt_pct(row['discovery_cagr'])} | "
            f"{_fmt_pct(row['validation_cagr'])} | "
            f"{_fmt_pct(row['holdout_cagr'])} | "
            f"{_fmt_pct(row['full_mdd'])} |"
        )

    qg = baseline.get("metrics", {}).get("QG_CORE", {})
    spy = baseline.get("metrics", {}).get("SPY", {})
    qqq = baseline.get("metrics", {}).get("QQQ", {})
    lines.extend(
        [
            "",
            "## 현재 Equity v1 기준선",
            "",
            f"- QG v1 CAGR/MDD: {_fmt_pct(qg.get('cagr'))} / {_fmt_pct(qg.get('mdd'))}",
            f"- 같은 월간 구간 SPY CAGR/MDD: {_fmt_pct(spy.get('cagr'))} / {_fmt_pct(spy.get('mdd'))}",
            f"- 같은 월간 구간 QQQ CAGR/MDD: {_fmt_pct(qqq.get('cagr'))} / {_fmt_pct(qqq.get('mdd'))}",
            f"- 기준선 오류: {baseline.get('error') or '없음'}",
            "",
            "## 강건성 점검",
            "",
            "| 원후보 | 시나리오 | 전체 CAGR | 전체 MDD | 검증 CAGR | 홀드아웃 CAGR |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in robustness.iterrows():
        if row.get("error"):
            lines.append(
                f"| {row['source_candidate_id']} | {row['scenario']} | 오류 | 오류 | 오류 | 오류 |"
            )
        else:
            lines.append(
                f"| {row['source_candidate_id']} | {row['scenario']} | "
                f"{_fmt_pct(row.get('full_cagr'))} | {_fmt_pct(row.get('full_mdd'))} | "
                f"{_fmt_pct(row.get('validation_cagr'))} | "
                f"{_fmt_pct(row.get('holdout_cagr'))} |"
            )

    lines.extend(
        [
            "",
            "## 해석 원칙",
            "",
            "- 절대수익 1위와 MDD 제한 후보를 분리한다. 레버리지 전략의 높은 CAGR은 더 큰 경로위험을 포함한다.",
            "- 홀드아웃 성과는 최종 후보 선택에 사용하지 않고 사후 진단으로만 표시했다.",
            "- QLD는 2006년 이후, TQQQ는 2010년 이후 실제 ETF 데이터만 사용한다.",
            "- SOXX는 20년 반도체 주 연구 자산이고, 상위 SOXX 후보는 SMH로 교체한 공통기간 민감도를 별도 확인한다.",
            "- GLD·IEF·TLT는 고정 편입하지 않고 현금보다 나은 방어 선택지인지 후보군 안에서 경쟁시켰다.",
            "- '저점 매수·고점 매도'는 사후 최저·최고가가 아니라 낙폭 후 회복, 눌림, 돌파, 장기추세 이탈 규칙으로 근사한다.",
            "- 과거 후보를 많이 탐색했으므로 선택 편향이 남는다. 최종 배포 전 추가 홀드아웃·Shadow 검증이 필요하다.",
            "",
            "## 다음 단계",
            "",
            "이번 결과에서 위험수준별 상위 2~3개만 고정한 뒤, 더 촘촘한 파라미터·KRW 수익률·세금·실제 보유상태를 포함한 2차 연구를 진행한다.",
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
    frames, data_metadata = load_market_data(
        cfg=cfg, paths=paths, refresh=refresh
    )
    close = close_panel(frames)
    store = FeatureStore(close)
    latest = pd.Timestamp(frames["SPY"]["Close"].dropna().index.max())

    candidates = build_candidate_grid()
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    print(f"generated {len(candidates):,} candidates")

    broad = broad_search(
        candidates=candidates,
        frames=frames,
        store=store,
        end=latest,
        cost_bps=cost_bps + slippage_bps,
    )
    shortlist = shortlist_candidates(broad, candidate_map, maximum=500)
    print(f"shortlist: {len(shortlist):,}")

    exact, _ = exact_evaluate(
        candidates=shortlist,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    exact = add_selection_scores(exact)
    initial_winners = select_research_winners(exact)

    refined, refined_map = refine_candidates(
        winners=initial_winners,
        candidate_map=candidate_map,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    combined = pd.concat([exact, refined], ignore_index=True)
    combined = combined.sort_values(
        ["candidate_id", "selection_score"],
        ascending=[True, False],
    ).drop_duplicates("candidate_id", keep="first")
    final_winners = select_final_winners(combined)

    all_candidate_map = {**candidate_map, **refined_map}
    robustness = robustness_checks(
        winners=final_winners,
        candidate_map=all_candidate_map,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    annual = selected_annual_returns(
        winners=final_winners,
        candidate_map=all_candidate_map,
        frames=frames,
        store=store,
        latest=latest,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    baseline = current_strategy_baseline(cfg, paths)

    output_paths = {
        "broad": paths.output / "equity_v2_broad.csv",
        "exact": paths.output / "equity_v2_exact.csv",
        "initial_winners": paths.output / "equity_v2_initial_winners.csv",
        "refinement": paths.output / "equity_v2_refinement.csv",
        "final_winners": paths.output / "equity_v2_final_winners.csv",
        "robustness": paths.output / "equity_v2_robustness.csv",
        "annual": paths.output / "equity_v2_annual_returns.csv",
        "manifest": paths.output / "equity_v2_manifest.json",
        "report": paths.output / "equity_v2_report.md",
    }
    broad.to_csv(output_paths["broad"], index=False, encoding="utf-8-sig")
    exact.to_csv(output_paths["exact"], index=False, encoding="utf-8-sig")
    initial_winners.to_csv(
        output_paths["initial_winners"], index=False, encoding="utf-8-sig"
    )
    refined.to_csv(
        output_paths["refinement"], index=False, encoding="utf-8-sig"
    )
    final_winners.to_csv(
        output_paths["final_winners"], index=False, encoding="utf-8-sig"
    )
    robustness.to_csv(
        output_paths["robustness"], index=False, encoding="utf-8-sig"
    )
    annual.to_csv(output_paths["annual"], index=False, encoding="utf-8-sig")

    manifest = {
        "schema_version": "equity-v2-leverage-research-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now.isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "data_start": data_metadata["master_start"],
        "data_end": latest.date().isoformat(),
        "track_starts": {
            key: value.date().isoformat()
            for key, value in TRACK_STARTS.items()
        },
        "discovery_end": DISCOVERY_END.date().isoformat(),
        "validation": [
            VALIDATION_START.date().isoformat(),
            VALIDATION_END.date().isoformat(),
        ],
        "holdout_start": HOLDOUT_START.date().isoformat(),
        "broad_candidate_count": len(candidates),
        "broad_success_count": int((broad["status"] == "ok").sum()),
        "shortlist_count": len(shortlist),
        "refinement_count": len(refined),
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
        "data_metadata": data_metadata,
        "final_candidates": [
            {
                "category": row["category"],
                "candidate_id": row["candidate_id"],
                "family": row["family"],
                "params": json.loads(row["params_json"]),
            }
            for _, row in final_winners.iterrows()
        ],
        "limitations": [
            "large candidate search creates selection bias",
            "TQQQ actual history begins in 2010",
            "cash return is approximated from prior 13-week T-bill yield",
            "taxes and personal FX conversion costs are excluded",
            "holdout is short and current 2026 period is incomplete",
            "no live or Telegram approval",
        ],
    }
    _write_json(output_paths["manifest"], manifest)
    report = build_report(
        manifest=manifest,
        broad=broad,
        exact=exact,
        initial_winners=initial_winners,
        refined=refined,
        final_winners=final_winners,
        robustness=robustness,
        baseline=baseline,
    )
    output_paths["report"].write_text(report, encoding="utf-8-sig")

    return {
        "manifest": manifest,
        "final_winners": final_winners.to_dict(orient="records"),
        "outputs": {key: str(value) for key, value in output_paths.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quant Guardian Equity v2 leveraged strategy research"
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
