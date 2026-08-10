from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from btc_guardian import (
    BtcDataError,
    DEFAULT_CONFIG,
    ROOT,
    build_phase1_report,
    cache_key,
    closed_yahoo_daily_frame,
    iso_utc,
    load_config,
    read_price_cache,
    resolve_paths,
    utc_now,
)
from btc_research import (
    ResearchParameters,
    SimulationResult,
    build_feature_frame,
    generate_state_targets,
    load_coinmetrics_history,
    make_buy_hold_signals,
    make_ma_trend_signals,
    performance_metrics,
    simulate_strategy,
    trim_to_research_window,
)
from quant_guardian import annualized_metrics


STRATEGY_VERSION = "btc-cycle-trend-v0.4"
DEFAULT_START_DATE = "2016-01-01"
DEFAULT_INITIAL_CAPITAL_KRW = 10_000_000.0
USER_ACCEPTED_REFERENCE_MDD = -0.5752


@dataclass(frozen=True)
class CycleTrendParameters:
    trend_band: float = 0.01
    confirmation_weeks: int = 2
    slope_lookback_weeks: int = 4
    core_weight: float = 0.25
    deep_value_weight: float = 0.20
    deep_value_drawdown: float = -0.45
    deep_value_mvrv_percentile: float = 0.20
    overheat_mvrv_percentile: float = 0.90
    overheat_weekly_rsi: float = 75.0
    overheat_price_ratio: float = 1.80
    overheat_return_180d: float = 1.20
    overheat_required_domains: int = 2
    latch_late_cycle_overheat: bool = False
    latched_late_cycle_cap: float = 0.50
    weekly_step: float = 0.25


@dataclass(frozen=True)
class CycleCandidate:
    candidate_id: str
    parameters: CycleTrendParameters
    use_halving: bool = True
    sell_policy: str = "core"
    research_origin: str = "post-phase2-predeclared-v0.4"


def build_synthetic_krw_market(
    usd: pd.DataFrame,
    fx: pd.DataFrame,
    *,
    max_fx_staleness_days: int = 7,
) -> pd.DataFrame:
    """Build a conservative pre-Upbit KRW proxy using prior-known FX closes."""
    if max_fx_staleness_days < 1:
        raise ValueError("FX staleness allowance must be positive")
    usd = usd.sort_index().copy()
    fx = fx.sort_index().copy()
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(usd.columns) or "Close" not in fx:
        raise BtcDataError("Synthetic KRW mode requires USD OHLC and FX close")

    index = pd.DatetimeIndex(usd.index).tz_localize(None)
    fx_close = pd.to_numeric(fx["Close"], errors="coerce").dropna()
    # A labeled FX close can finish after the BTC UTC candle. Shift first, then
    # carry the last known value over weekends and short market holidays.
    available_fx = fx_close.shift(1).reindex(index).ffill(
        limit=max_fx_staleness_days
    )
    frame = pd.DataFrame(index=index)
    for column in ("Open", "High", "Low", "Close"):
        frame[column] = (
            pd.to_numeric(usd[column], errors="coerce").reindex(index) * available_fx
        )
    frame["Volume"] = pd.to_numeric(
        usd.get("Volume", pd.Series(index=index, dtype=float)), errors="coerce"
    ).reindex(index)
    frame["QuoteVolume"] = frame["Volume"] * frame["Close"]
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    positive = (frame[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
    consistent = (
        (frame["High"] >= frame[["Open", "Close"]].max(axis=1))
        & (frame["Low"] <= frame[["Open", "Close"]].min(axis=1))
        & (frame["High"] >= frame["Low"])
    )
    frame = frame.loc[positive & consistent]
    if frame.empty:
        raise BtcDataError("Synthetic KRW price frame is empty after validation")
    frame.attrs["price_mode"] = "BTC-USD multiplied by prior-known USD/KRW"
    frame.attrs["max_fx_staleness_days"] = max_fx_staleness_days
    return frame


def _phase_cap(phase: str, *, use_halving: bool) -> float:
    if not use_halving:
        return 1.0
    return {
        "HALVING_TRANSITION": 0.80,
        "POST_HALVING_EXPANSION": 1.00,
        "LATE_EXPANSION_DISTRIBUTION": 0.75,
        "CONTRACTION_RECOVERY": 0.50,
        "PRE_HALVING_ACCUMULATION": 0.75,
    }.get(phase, 0.0)


def _step_toward(current: float, desired: float, step: float) -> float:
    if desired > current:
        return min(desired, current + step)
    if desired < current:
        return max(desired, current - step)
    return current


def _state_label(current: float, previous: float) -> str:
    if math.isclose(current, previous, abs_tol=1e-9):
        return "HOLD" if current > 0 else "WAIT"
    if current > previous:
        return "BUY_MORE"
    return "REDUCE" if current > 0 else "EXIT"


def generate_cycle_trend_targets(
    features: pd.DataFrame,
    parameters: CycleTrendParameters | None = None,
    *,
    use_halving: bool = True,
    sell_policy: str = "core",
) -> pd.DataFrame:
    """Create a low-turnover weekly target using the halving clock as a cap."""
    params = parameters or CycleTrendParameters()
    if sell_policy not in {"core", "all_out"}:
        raise ValueError("sell_policy must be core or all_out")
    if params.confirmation_weeks < 1 or not 0 < params.weekly_step <= 1:
        raise ValueError("Invalid weekly confirmation or step")

    frame = features.copy()
    frame["wma40_slope"] = frame["wma40"] / frame["wma40"].shift(
        params.slope_lookback_weeks * 7
    ) - 1
    current = 0.0
    trend_on = False
    late_cycle_risk_latched = False
    above_streak = 0
    below_streak = 0
    rows: list[dict[str, Any]] = []

    for index, row in frame.iterrows():
        previous = current
        reason = "주간 확정 신호 대기"
        overheat_domains = 0
        deep_value = False
        is_week_end = index.dayofweek == 6

        if is_week_end and bool(row.get("feature_ready", False)):
            close = float(row["close"])
            wma40 = float(row["wma40"])
            slope = float(row["wma40_slope"])
            above = close >= wma40 * (1 + params.trend_band) and slope > 0
            below = close <= wma40 * (1 - params.trend_band) and slope < 0
            above_streak = above_streak + 1 if above else 0
            below_streak = below_streak + 1 if below else 0
            if above_streak >= params.confirmation_weeks:
                trend_on = True
            elif below_streak >= params.confirmation_weeks:
                trend_on = False

            phase = str(row["phase_label"])
            if phase == "PRE_HALVING_ACCUMULATION":
                late_cycle_risk_latched = False
            phase_cap = _phase_cap(phase, use_halving=use_halving)
            overheat_domains = sum(
                [
                    bool(
                        pd.notna(row["mvrv_percentile"])
                        and row["mvrv_percentile"]
                        >= params.overheat_mvrv_percentile
                    ),
                    bool(row["weekly_rsi14"] >= params.overheat_weekly_rsi),
                    bool(
                        row["close_sma200_ratio"] >= params.overheat_price_ratio
                        or row["return_180d"] >= params.overheat_return_180d
                    ),
                ]
            )
            accumulation_phase = phase in {
                "CONTRACTION_RECOVERY",
                "PRE_HALVING_ACCUMULATION",
            }
            deep_value = bool(
                accumulation_phase
                and row["drawdown_365"] <= params.deep_value_drawdown
                and pd.notna(row["mvrv_percentile"])
                and row["mvrv_percentile"]
                <= params.deep_value_mvrv_percentile
            )

            if (
                params.latch_late_cycle_overheat
                and phase == "LATE_EXPANSION_DISTRIBUTION"
                and overheat_domains >= params.overheat_required_domains
            ):
                late_cycle_risk_latched = True

            if trend_on:
                desired = phase_cap
                if (
                    params.latch_late_cycle_overheat
                    and phase == "LATE_EXPANSION_DISTRIBUTION"
                    and late_cycle_risk_latched
                ):
                    desired = min(desired, params.latched_late_cycle_cap)
                    reason = "후기 사이클 과열축소 잠금 유지"
                elif overheat_domains >= params.overheat_required_domains:
                    desired = min(
                        desired,
                        0.50
                        if phase == "LATE_EXPANSION_DISTRIBUTION"
                        else 0.75,
                    )
                    reason = "상승추세 유지 중 독립 과열영역 확인"
                else:
                    reason = f"40주선 상승 확인, {phase} 최대비중 적용"
            elif deep_value:
                desired = max(current, params.deep_value_weight)
                desired = min(desired, params.core_weight)
                reason = "수축·반감기 전 구간의 낙폭·MVRV 저가영역 확인"
            else:
                desired = (
                    min(current, params.core_weight)
                    if sell_policy == "core"
                    else 0.0
                )
                reason = "40주선 하락 확인, 전술비중 축소"

            current = _step_toward(current, desired, params.weekly_step)

        rows.append(
            {
                "Date": index,
                "state": _state_label(current, previous),
                "target_weight": float(current),
                "previous_weight": float(previous),
                "trend_on": bool(trend_on),
                "phase_label": str(row.get("phase_label", "UNKNOWN")),
                "cycle_progress": float(row.get("cycle_progress", np.nan)),
                "overheat_domains": int(overheat_domains),
                "deep_value": bool(deep_value),
                "reason": reason,
            }
        )
    return pd.DataFrame(rows).set_index("Date")


def cycle_candidate_specs() -> list[CycleCandidate]:
    base = CycleTrendParameters()
    return [
        CycleCandidate("cycle_trend_core_v04", base),
        CycleCandidate("cycle_trend_all_out_v04", base, sell_policy="all_out"),
        CycleCandidate("trend_core_no_halving_v04", base, use_halving=False),
        CycleCandidate(
            "cycle_trend_core_1w_v04",
            replace(base, confirmation_weeks=1),
        ),
        CycleCandidate(
            "cycle_trend_core_3w_v04",
            replace(base, confirmation_weeks=3),
        ),
        CycleCandidate(
            "late_overheat_latch_40_v04r1",
            replace(
                base,
                latch_late_cycle_overheat=True,
                latched_late_cycle_cap=0.40,
            ),
            research_origin="post-2017-drawdown-diagnostic-v0.4r1",
        ),
        CycleCandidate(
            "late_overheat_latch_50_v04r1",
            replace(
                base,
                latch_late_cycle_overheat=True,
                latched_late_cycle_cap=0.50,
            ),
            research_origin="post-2017-drawdown-diagnostic-v0.4r1",
        ),
        CycleCandidate(
            "late_overheat_latch_60_v04r1",
            replace(
                base,
                latch_late_cycle_overheat=True,
                latched_late_cycle_cap=0.60,
            ),
            research_origin="post-2017-drawdown-diagnostic-v0.4r1",
        ),
    ]


def _simulate_candidate(
    features: pd.DataFrame,
    candidate: CycleCandidate,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, SimulationResult]:
    signals = generate_cycle_trend_targets(
        features,
        candidate.parameters,
        use_halving=candidate.use_halving,
        sell_policy=candidate.sell_policy,
    )
    simulation = simulate_strategy(
        features,
        signals,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital_krw,
    )
    return signals, simulation


def evaluate_cycle_candidates(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, tuple[pd.DataFrame, SimulationResult]]]:
    rows: list[dict[str, Any]] = []
    outputs: dict[str, tuple[pd.DataFrame, SimulationResult]] = {}
    for candidate in cycle_candidate_specs():
        signals, simulation = _simulate_candidate(
            features,
            candidate,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        metrics = performance_metrics(simulation)
        terminal = float(metrics["terminal_wealth"])
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "use_halving": candidate.use_halving,
                "sell_policy": candidate.sell_policy,
                "confirmation_weeks": candidate.parameters.confirmation_weeks,
                "research_origin": candidate.research_origin,
                "initial_capital_krw": initial_capital_krw,
                "terminal_wealth_krw": terminal,
                "profit_krw": terminal - initial_capital_krw,
                "capital_multiple": terminal / initial_capital_krw,
                "accepted_mdd_reference_pass": bool(
                    metrics["mdd"] >= USER_ACCEPTED_REFERENCE_MDD
                ),
                **metrics,
            }
        )
        outputs[candidate.candidate_id] = (signals, simulation)
    return pd.DataFrame(rows), outputs


def evaluate_extended_benchmarks(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    signal_map = {
        "phase2_baseline_extended": generate_state_targets(
            features,
            ResearchParameters(),
            sell_policy="core_tactical",
            use_halving=True,
        ),
        "buy_hold": make_buy_hold_signals(features),
        "wma40_trend": make_ma_trend_signals(
            features, "wma40", persistence_days=14
        ),
    }
    outputs: dict[str, SimulationResult] = {}
    rows: list[dict[str, Any]] = []
    for benchmark_id, signals in signal_map.items():
        simulation = simulate_strategy(
            features,
            signals,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital_krw,
        )
        outputs[benchmark_id] = simulation
        metrics = performance_metrics(simulation)
        terminal = float(metrics["terminal_wealth"])
        rows.append(
            {
                "benchmark_id": benchmark_id,
                "initial_capital_krw": initial_capital_krw,
                "terminal_wealth_krw": terminal,
                "profit_krw": terminal - initial_capital_krw,
                "capital_multiple": terminal / initial_capital_krw,
                **metrics,
            }
        )
    return pd.DataFrame(rows), outputs


def cycle_period_metrics(
    features: pd.DataFrame,
    outputs: Mapping[str, tuple[pd.DataFrame, SimulationResult]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate_id, (_, simulation) in outputs.items():
        for epoch, epoch_features in features.groupby("halving_epoch"):
            if pd.isna(epoch) or len(epoch_features) < 30:
                continue
            daily = simulation.daily.reindex(epoch_features.index).dropna()
            if daily.empty:
                continue
            metrics = annualized_metrics(daily["daily_return"], 365)
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "halving_epoch": int(epoch),
                    "start": daily.index.min().date().isoformat(),
                    "end": daily.index.max().date().isoformat(),
                    "total_return": metrics["total_return"],
                    "cagr": metrics["cagr"],
                    "mdd": metrics["mdd"],
                    "exposure": float(daily["actual_weight"].mean()),
                }
            )
    return pd.DataFrame(rows)


def robustness_diagnostics(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    specs = {
        candidate.candidate_id: candidate
        for candidate in cycle_candidate_specs()
        if candidate.candidate_id
        in {"cycle_trend_core_v04", "late_overheat_latch_40_v04r1"}
    }
    scenarios = [
        ("base", fee_bps, slippage_bps, 0),
        ("double_cost", fee_bps * 2, slippage_bps * 2, 0),
        ("delay_1d", fee_bps, slippage_bps, 1),
        ("delay_2d", fee_bps, slippage_bps, 2),
    ]
    rows: list[dict[str, Any]] = []
    for candidate_id, candidate in specs.items():
        signals = generate_cycle_trend_targets(
            features,
            candidate.parameters,
            use_halving=candidate.use_halving,
            sell_policy=candidate.sell_policy,
        )
        for scenario, scenario_fee, scenario_slippage, delay in scenarios:
            simulation = simulate_strategy(
                features,
                signals,
                fee_bps=scenario_fee,
                slippage_bps=scenario_slippage,
                additional_delay_days=delay,
                initial_capital=initial_capital_krw,
            )
            metrics = performance_metrics(simulation)
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "scenario": scenario,
                    "fee_bps": scenario_fee,
                    "slippage_bps": scenario_slippage,
                    "additional_delay_days": delay,
                    "terminal_wealth_krw": metrics["terminal_wealth"],
                    "capital_multiple": metrics["terminal_wealth"]
                    / initial_capital_krw,
                    "cagr": metrics["cagr"],
                    "mdd": metrics["mdd"],
                    "trades": metrics["trades"],
                }
            )
    return pd.DataFrame(rows)


def overlap_diagnostics(
    synthetic: pd.DataFrame,
    upbit: pd.DataFrame,
) -> dict[str, Any]:
    joined = pd.concat(
        {
            "synthetic": synthetic["Close"],
            "upbit": upbit["Close"],
        },
        axis=1,
        join="inner",
    ).dropna()
    if len(joined) < 365:
        raise BtcDataError("Synthetic/Upbit overlap has fewer than 365 rows")
    premium = joined["upbit"] / joined["synthetic"] - 1
    return_correlation = joined.pct_change(fill_method=None).dropna().corr().iloc[0, 1]
    return {
        "rows": int(len(joined)),
        "start": joined.index.min().date().isoformat(),
        "end": joined.index.max().date().isoformat(),
        "median_upbit_premium": float(premium.median()),
        "p95_absolute_premium": float(premium.abs().quantile(0.95)),
        "daily_return_correlation": float(return_correlation),
    }


def strategy_overlap_diagnostics(
    synthetic_features: pd.DataFrame,
    upbit_features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    common_index = synthetic_features.index.intersection(upbit_features.index)
    if len(common_index) < 730:
        raise BtcDataError("Strategy data-mode overlap has fewer than 730 rows")
    synthetic_common = synthetic_features.loc[common_index].copy()
    upbit_common = upbit_features.loc[common_index].copy()
    primary = cycle_candidate_specs()[0]
    results: dict[str, Any] = {
        "start": common_index.min().date().isoformat(),
        "end": common_index.max().date().isoformat(),
        "rows": int(len(common_index)),
    }
    for mode, frame in (
        ("synthetic", synthetic_common),
        ("upbit", upbit_common),
    ):
        _, simulation = _simulate_candidate(
            frame,
            primary,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        metrics = performance_metrics(simulation)
        results[mode] = {
            "terminal_wealth_krw": float(metrics["terminal_wealth"]),
            "capital_multiple": float(metrics["terminal_wealth"])
            / initial_capital_krw,
            "cagr": float(metrics["cagr"]),
            "mdd": float(metrics["mdd"]),
            "trades": int(metrics["trades"]),
        }
    return results


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):,.0f}원"


def build_cycle_report(
    candidates: pd.DataFrame,
    benchmarks: pd.DataFrame,
    cycles: pd.DataFrame,
    robustness: pd.DataFrame,
    overlap: Mapping[str, Any],
    strategy_overlap: Mapping[str, Any],
    manifest: Mapping[str, Any],
    current_state: Mapping[str, Any],
) -> str:
    primary = candidates.loc[
        candidates["candidate_id"] == "cycle_trend_core_v04"
    ].iloc[0]
    diagnostic = candidates.loc[
        candidates["candidate_id"] == "late_overheat_latch_40_v04r1"
    ].iloc[0]
    lines = [
        "# BTC 반감기·40주 추세 연구 v0.4",
        "",
        f"생성시각: {manifest['generated_at_utc']}",
        f"연구기간: {manifest['data_start']}~{manifest['data_end']}",
        "",
        "> 과거 데이터 연구 결과이며 자동주문·실전 승인 신호가 아닙니다.",
        "",
        "## 1,000만 원 일시납 결과",
        f"- v0.4 핵심유지: {_fmt_krw(primary['terminal_wealth_krw'])}",
        f"- 누적수익: {_fmt_pct(primary['total_return'])}",
        f"- CAGR / MDD: {_fmt_pct(primary['cagr'])} / {_fmt_pct(primary['mdd'])}",
        f"- 거래 횟수: {int(primary['trades'])}",
        f"- 평균 BTC 노출: {_fmt_pct(primary['exposure'])}",
        f"- 허용참고 MDD {_fmt_pct(USER_ACCEPTED_REFERENCE_MDD)} 이내: "
        f"{'예' if primary['accepted_mdd_reference_pass'] else '아니오'}",
        "",
        "## 낙폭 진단 후보",
        f"- 후기 과열축소 40% 잠금: {_fmt_krw(diagnostic['terminal_wealth_krw'])}",
        f"- CAGR / MDD: {_fmt_pct(diagnostic['cagr'])} / {_fmt_pct(diagnostic['mdd'])}",
        "- 이 규칙은 v0.4 결과의 2017 낙폭을 본 뒤 추가했으므로 실전 자동선택에서 제외합니다.",
        "",
        "## 후보 비교",
        "",
        "| 후보 | 최종자산 | CAGR | MDD | 거래 | 평균노출 | 허용참고 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in candidates.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{int(row['trades'])} | {_fmt_pct(row['exposure'])} | "
            f"{'이내' if row['accepted_mdd_reference_pass'] else '초과'} |"
        )
    lines.extend(
        [
            "",
            "## 기존 전략과 단순 기준",
            "",
            "| 기준 | 최종자산 | CAGR | MDD | 거래 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in benchmarks.iterrows():
        lines.append(
            f"| {row['benchmark_id']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 핵심후보의 반감기 epoch별 결과",
            "",
            "| Epoch | 기간 | 누적수익 | CAGR | MDD | 평균노출 |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    primary_cycles = cycles.loc[
        cycles["candidate_id"] == "cycle_trend_core_v04"
    ]
    for _, row in primary_cycles.iterrows():
        lines.append(
            f"| {int(row['halving_epoch'])} | {row['start']}~{row['end']} | "
            f"{_fmt_pct(row['total_return'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['exposure'])} |"
        )
    lines.extend(
        [
            "",
            "## 비용·체결지연 강건성",
            "",
            "| 후보 | 조건 | 최종자산 | CAGR | MDD |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for _, row in robustness.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {row['scenario']} | "
            f"{_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} |"
        )
    lines.extend(
        [
            "",
            "## 합성 원화 데이터 점검",
            f"- Upbit 중첩기간: {overlap['start']}~{overlap['end']} ({overlap['rows']}일)",
            f"- 일간 수익률 상관: {float(overlap['daily_return_correlation']):.4f}",
            f"- Upbit 프리미엄 중앙값: {_fmt_pct(overlap['median_upbit_premium'])}",
            f"- 절대 프리미엄 95분위: {_fmt_pct(overlap['p95_absolute_premium'])}",
            "",
            "### 동일 중첩기간 전략 결과",
            f"- 기간: {strategy_overlap['start']}~{strategy_overlap['end']}",
            f"- 합성 원화: {_fmt_krw(strategy_overlap['synthetic']['terminal_wealth_krw'])}, "
            f"CAGR {_fmt_pct(strategy_overlap['synthetic']['cagr'])}, "
            f"MDD {_fmt_pct(strategy_overlap['synthetic']['mdd'])}",
            f"- 실제 Upbit: {_fmt_krw(strategy_overlap['upbit']['terminal_wealth_krw'])}, "
            f"CAGR {_fmt_pct(strategy_overlap['upbit']['cagr'])}, "
            f"MDD {_fmt_pct(strategy_overlap['upbit']['mdd'])}",
            "",
            "## 현재 연구상태",
            f"- 상태: {current_state['state']}",
            f"- 목표비중: {float(current_state['target_weight']) * 100:.0f}%",
            f"- 반감기 진행률: {float(current_state['cycle_progress']) * 100:.1f}%",
            f"- 근거: {current_state['reason']}",
            "",
            "## 해석 제한",
            "- 2016~Upbit 상장 전 구간은 BTC-USD와 전일 USD/KRW로 만든 합성 원화 가격입니다.",
            "- 수수료 5bps와 슬리피지 10bps를 반영했지만 세금·예치금 이자는 제외했습니다.",
            "- 이 규칙은 Phase 2 결과를 본 뒤 설계했으므로 2016~현재 성과는 순수한 아웃오브샘플이 아닙니다.",
            "- 현재 반감기 epoch는 완결되지 않았습니다.",
            "- 진짜 전향검증 시작점은 이 규칙을 Git에 고정한 다음 확정 일봉부터입니다.",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def run_cycle_research(
    *,
    refresh: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    now_utc: datetime | None = None,
    start_date: str = DEFAULT_START_DATE,
    initial_capital_krw: float = DEFAULT_INITIAL_CAPITAL_KRW,
) -> dict[str, Any]:
    if initial_capital_krw <= 0:
        raise ValueError("Initial capital must be positive")
    now = now_utc or utc_now()
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow" or bool(
        btc_cfg.get("auto_order", False)
    ):
        raise BtcDataError("Cycle research requires shadow mode and no auto orders")

    phase1 = build_phase1_report(
        refresh=refresh, config_path=config_path, now_utc=now
    )
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 critical data gate is blocked")
    onchain, onchain_fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh,
        config=config,
        now_utc=now,
    )

    paths = resolve_paths(config)
    data_cfg = btc_cfg.get("data", {})
    runtime = btc_cfg.get("research_runtime", {})
    yahoo_cache = ROOT / config.get("settings", {}).get(
        "cache_dir", "data/cache"
    )
    usd = read_price_cache(
        yahoo_cache
        / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))
    )
    fx = read_price_cache(
        yahoo_cache / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))
    )
    usd = closed_yahoo_daily_frame(usd, now)
    fx = closed_yahoo_daily_frame(fx, now)
    synthetic = build_synthetic_krw_market(usd, fx)
    upbit = read_price_cache(
        paths.cache
        / f"upbit_{str(btc_cfg.get('execution_market', 'KRW-BTC')).replace('-', '_')}_daily.csv"
    )
    overlap = overlap_diagnostics(synthetic, upbit)

    onchain_lag_days = int(runtime.get("onchain_lag_days", 2))
    percentile_min_periods = int(runtime.get("percentile_min_periods", 730))
    features = build_feature_frame(
        synthetic,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    features = trim_to_research_window(features)
    features = features.loc[pd.Timestamp(start_date) :].copy()
    if features.empty or features.index.min() > pd.Timestamp(start_date) + pd.Timedelta(
        days=7
    ):
        raise BtcDataError("Extended feature frame cannot start near requested date")

    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    upbit_features = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    upbit_features = trim_to_research_window(upbit_features)
    strategy_overlap = strategy_overlap_diagnostics(
        features,
        upbit_features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    candidates, outputs = evaluate_cycle_candidates(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    benchmarks, benchmark_outputs = evaluate_extended_benchmarks(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    cycles = cycle_period_metrics(features, outputs)
    robustness = robustness_diagnostics(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    primary_signals, primary_simulation = outputs["cycle_trend_core_v04"]
    current_state = primary_signals.iloc[-1].to_dict()

    candidate_path = paths.output / "btc_cycle_v04_candidates.csv"
    benchmark_path = paths.output / "btc_cycle_v04_benchmarks.csv"
    signal_path = paths.output / "btc_cycle_v04_signals.csv"
    cycle_path = paths.output / "btc_cycle_v04_cycle_metrics.csv"
    robustness_path = paths.output / "btc_cycle_v04_robustness.csv"
    equity_path = paths.output / "btc_cycle_v04_equity.csv"
    manifest_path = paths.output / "btc_cycle_v04_manifest.json"
    report_path = paths.output / "btc_cycle_v04_report.md"

    candidates.to_csv(candidate_path, index=False, encoding="utf-8-sig")
    benchmarks.to_csv(benchmark_path, index=False, encoding="utf-8-sig")
    primary_signals.to_csv(signal_path, encoding="utf-8-sig")
    cycles.to_csv(cycle_path, index=False, encoding="utf-8-sig")
    robustness.to_csv(robustness_path, index=False, encoding="utf-8-sig")
    equity = pd.DataFrame(
        {
            candidate_id: simulation.daily["equity"]
            for candidate_id, (_, simulation) in outputs.items()
        }
        | {
            benchmark_id: simulation.daily["equity"]
            for benchmark_id, simulation in benchmark_outputs.items()
        }
    )
    equity.to_csv(equity_path, encoding="utf-8-sig")

    manifest = {
        "schema_version": "btc-cycle-research-0.4",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "approved_strategy": None,
        "data_mode": "synthetic KRW from BTC-USD and prior-known USD/KRW",
        "data_start": features.index.min().date().isoformat(),
        "data_end": features.index.max().date().isoformat(),
        "initial_capital_krw": initial_capital_krw,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "execution_rule": "closed UTC signal, next synthetic daily open",
        "accepted_reference_mdd": USER_ACCEPTED_REFERENCE_MDD,
        "candidate_ids": candidates["candidate_id"].tolist(),
        "benchmark_ids": benchmarks["benchmark_id"].tolist(),
        "research_origin": "designed after Phase 2 retrospective results",
        "selection_rule": "none; primary candidate fixed before this v0.4 run",
        "onchain_lag_days": onchain_lag_days,
        "onchain_cache_fallback": onchain_fallback,
        "onchain_cache_error": onchain_error,
        "overlap_diagnostics": overlap,
        "strategy_overlap_diagnostics": strategy_overlap,
        "limitations": [
            "pre-Upbit prices are synthetic, not executable venue quotes",
            "historical results are not pristine out-of-sample",
            "tax and idle-cash interest are excluded",
            "the current halving epoch is incomplete",
            "no live advisory approval",
        ],
    }
    _write_json(manifest_path, manifest)
    report = build_cycle_report(
        candidates,
        benchmarks,
        cycles,
        robustness,
        overlap,
        strategy_overlap,
        manifest,
        current_state,
    )
    report_path.write_text(report, encoding="utf-8-sig")

    primary_metrics = performance_metrics(primary_simulation)
    return {
        "manifest": manifest,
        "primary_metrics": primary_metrics,
        "current_state": current_state,
        "candidates": candidates,
        "benchmarks": benchmarks,
        "cycles": cycles,
        "robustness": robustness,
        "outputs": {
            "candidates": str(candidate_path),
            "benchmarks": str(benchmark_path),
            "signals": str(signal_path),
            "cycles": str(cycle_path),
            "robustness": str(robustness_path),
            "equity": str(equity_path),
            "manifest": str(manifest_path),
            "report": str(report_path),
        },
    }


def print_summary(result: Mapping[str, Any]) -> None:
    metrics = result["primary_metrics"]
    capital = float(result["manifest"]["initial_capital_krw"])
    terminal = float(metrics["terminal_wealth"])
    state = result["current_state"]
    print("BTC 반감기·40주 추세 v0.4 연구 결과")
    print(f"기간: {result['manifest']['data_start']}~{result['manifest']['data_end']}")
    print(f"초기 {_fmt_krw(capital)} -> {_fmt_krw(terminal)}")
    print(
        f"누적수익/CAGR/MDD: {_fmt_pct(metrics['total_return'])} / "
        f"{_fmt_pct(metrics['cagr'])} / {_fmt_pct(metrics['mdd'])}"
    )
    print(
        f"현재 연구상태: {state['state']} / 목표비중 "
        f"{float(state['target_weight']) * 100:.0f}%"
    )
    print("과거 연구·모의운영 전용이며 자동주문과 실전 승인은 없습니다.")
    print(f"보고서: {result['outputs']['report']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BTC halving-conditioned 40-week trend research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument(
        "--initial-capital-krw",
        type=float,
        default=DEFAULT_INITIAL_CAPITAL_KRW,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_cycle_research(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
