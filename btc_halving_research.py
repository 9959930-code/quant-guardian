from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from btc_cycle_research import build_synthetic_krw_market, overlap_diagnostics
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
from btc_momentum_research import (
    MomentumVolatilityParameters,
    add_momentum_volatility_features,
)
from btc_research import (
    SimulationResult,
    build_feature_frame,
    load_coinmetrics_history,
    make_buy_hold_signals,
    performance_metrics,
    simulate_strategy,
)
from quant_guardian import annualized_metrics


STRATEGY_VERSION = "btc-halving-overlay-research-v0.6"
DEFAULT_START_DATE = "2016-01-01"
DEFAULT_INITIAL_CAPITAL_KRW = 10_000_000.0
DECISION_WEEKDAY = 6
HALVING_INTERVAL = 210_000

BALANCED_PHASE_CAPS: tuple[tuple[str, float], ...] = (
    ("HALVING_TRANSITION", 1.00),
    ("POST_HALVING_EXPANSION", 1.00),
    ("LATE_EXPANSION_DISTRIBUTION", 0.75),
    ("CONTRACTION_RECOVERY", 0.50),
    ("PRE_HALVING_ACCUMULATION", 0.75),
)
PHASE_RISK_BUDGETS: tuple[tuple[str, float], ...] = (
    ("HALVING_TRANSITION", 0.40),
    ("POST_HALVING_EXPANSION", 0.45),
    ("LATE_EXPANSION_DISTRIBUTION", 0.35),
    ("CONTRACTION_RECOVERY", 0.30),
    ("PRE_HALVING_ACCUMULATION", 0.40),
)


@dataclass(frozen=True)
class HalvingOverlayParameters:
    momentum_horizons_days: tuple[int, ...] = (30, 90, 180, 365)
    volatility_lookback_days: int = 63
    target_annual_volatility: float = 0.40
    rebalance_deadband: float = 0.10
    max_weight: float = 1.0
    phase_caps: tuple[tuple[str, float], ...] = BALANCED_PHASE_CAPS
    phase_risk_budgets: tuple[tuple[str, float], ...] = PHASE_RISK_BUDGETS
    late_cycle_min_positive_for_high_weight: int = 4
    late_cycle_confirmation_cap: float = 0.50
    accumulation_floor_weight: float = 0.25
    accumulation_drawdown_max: float = -0.45
    accumulation_mvrv_percentile_max: float = 0.30
    accumulation_realized_price_ratio_max: float = 1.15
    overheat_mvrv_percentile_min: float = 0.90
    overheat_weekly_rsi_min: float = 75.0
    overheat_price_ratio_min: float = 1.80
    overheat_return_180d_min: float = 1.20


@dataclass(frozen=True)
class HalvingCandidate:
    candidate_id: str
    overlay_mode: str
    parameters: HalvingOverlayParameters
    rebalance_policy: str
    halving_required: bool
    selection_eligible: bool
    research_origin: str
    description: str


@dataclass(frozen=True)
class CycleWindowCandidate:
    candidate_id: str
    pre_halving_start_progress: float
    post_halving_end_progress: float
    target_annual_volatility: float | None
    rebalance_deadband: float = 0.10


def _mapping(items: Sequence[tuple[str, float]]) -> dict[str, float]:
    return {str(key): float(value) for key, value in items}


def _validate_overlay_parameters(parameters: HalvingOverlayParameters) -> None:
    if not parameters.momentum_horizons_days:
        raise ValueError("At least one momentum horizon is required")
    if any(value < 2 for value in parameters.momentum_horizons_days):
        raise ValueError("Momentum horizons must be at least two days")
    if parameters.volatility_lookback_days < 2:
        raise ValueError("Volatility lookback must be at least two days")
    if not 0 < parameters.target_annual_volatility <= 1:
        raise ValueError("Target annual volatility must be in (0, 1]")
    if not 0 <= parameters.rebalance_deadband < 1:
        raise ValueError("Rebalance deadband must be in [0, 1)")
    if not 0 < parameters.max_weight <= 1:
        raise ValueError("Maximum weight must be in (0, 1]")
    phase_caps = _mapping(parameters.phase_caps)
    risk_budgets = _mapping(parameters.phase_risk_budgets)
    expected = {
        "HALVING_TRANSITION",
        "POST_HALVING_EXPANSION",
        "LATE_EXPANSION_DISTRIBUTION",
        "CONTRACTION_RECOVERY",
        "PRE_HALVING_ACCUMULATION",
    }
    if set(phase_caps) != expected or set(risk_budgets) != expected:
        raise ValueError("Every halving phase must have a cap and risk budget")
    if any(not 0 <= value <= 1 for value in phase_caps.values()):
        raise ValueError("Phase caps must be in [0, 1]")
    if any(not 0 < value <= 1 for value in risk_budgets.values()):
        raise ValueError("Phase risk budgets must be in (0, 1]")


def candidate_specs() -> list[HalvingCandidate]:
    base40 = HalvingOverlayParameters(target_annual_volatility=0.40)
    base45 = replace(base40, target_annual_volatility=0.45)
    origin = "predeclared-after-v0.5-review-2026-08-10"
    return [
        HalvingCandidate(
            "v05_vol45_target_change",
            "none",
            base45,
            "target_change",
            False,
            False,
            "frozen-v0.5-benchmark",
            "v0.5 exact-style benchmark: no halving overlay, previous target deadband",
        ),
        HalvingCandidate(
            "v05_vol45_actual_weight",
            "none",
            base45,
            "actual_weight",
            False,
            False,
            "v0.5-rebalance-policy-diagnostic",
            "v0.5 signal with actual portfolio-weight rebalancing",
        ),
        HalvingCandidate(
            "v06_phase_cap_vol45_actual",
            "phase_cap",
            base45,
            "actual_weight",
            True,
            True,
            origin,
            "v0.5 plus halving phase maximum weights",
        ),
        HalvingCandidate(
            "v06_phase_cap_vol40_actual",
            "phase_cap",
            base40,
            "actual_weight",
            True,
            True,
            origin,
            "40% risk budget plus halving phase maximum weights",
        ),
        HalvingCandidate(
            "v06_phase_risk_vol40_actual",
            "phase_risk",
            base40,
            "actual_weight",
            True,
            True,
            origin,
            "halving phase-specific risk budgets and maximum weights",
        ),
        HalvingCandidate(
            "v06_confirmation_vol40_actual",
            "confirmation",
            base40,
            "actual_weight",
            True,
            True,
            origin,
            "late-cycle high exposure requires all four momentum horizons positive",
        ),
        HalvingCandidate(
            "v06_cycle_value_vol40_actual",
            "cycle_value",
            base40,
            "actual_weight",
            True,
            True,
            origin,
            "cycle/value accumulation floor and late-cycle overheat reduction",
        ),
        HalvingCandidate(
            "v06_cycle_value_vol45_actual",
            "cycle_value",
            base45,
            "actual_weight",
            True,
            True,
            origin,
            "45% risk-budget sensitivity for cycle/value overlay",
        ),
        HalvingCandidate(
            "v06_cycle_value_vol40_target_change",
            "cycle_value",
            base40,
            "target_change",
            True,
            True,
            "rebalance-policy-sensitivity",
            "same overlay using previous target rather than actual portfolio weight",
        ),
    ]


def _safe_float(row: pd.Series, key: str) -> float:
    value = row.get(key, np.nan)
    return float(value) if pd.notna(value) else math.nan


def _phase_value(mapping_items: Sequence[tuple[str, float]], phase: str) -> float:
    mapping = _mapping(mapping_items)
    if phase not in mapping:
        raise ValueError(f"Unknown halving phase: {phase}")
    return mapping[phase]


def _overheat_domains(
    row: pd.Series, parameters: HalvingOverlayParameters
) -> tuple[int, tuple[str, ...]]:
    triggered: list[str] = []
    mvrv = _safe_float(row, "mvrv_percentile")
    weekly_rsi = _safe_float(row, "weekly_rsi14")
    price_ratio = _safe_float(row, "close_sma200_ratio")
    return_180 = _safe_float(row, "return_180d")
    if math.isfinite(mvrv) and mvrv >= parameters.overheat_mvrv_percentile_min:
        triggered.append("MVRV 상위 분위")
    if (
        math.isfinite(weekly_rsi)
        and weekly_rsi >= parameters.overheat_weekly_rsi_min
    ):
        triggered.append("주봉 RSI 과열")
    if (
        math.isfinite(price_ratio)
        and price_ratio >= parameters.overheat_price_ratio_min
    ) or (
        math.isfinite(return_180)
        and return_180 >= parameters.overheat_return_180d_min
    ):
        triggered.append("가격·180일 수익률 과열")
    return len(triggered), tuple(triggered)


def _value_floor_active(
    row: pd.Series, parameters: HalvingOverlayParameters, phase: str
) -> bool:
    if phase not in {"CONTRACTION_RECOVERY", "PRE_HALVING_ACCUMULATION"}:
        return False
    drawdown = _safe_float(row, "drawdown_365")
    mvrv = _safe_float(row, "mvrv_percentile")
    realized_ratio = _safe_float(row, "price_realized_ratio")
    value_confirmed = (
        math.isfinite(mvrv)
        and mvrv <= parameters.accumulation_mvrv_percentile_max
    ) or (
        math.isfinite(realized_ratio)
        and realized_ratio <= parameters.accumulation_realized_price_ratio_max
    )
    return (
        math.isfinite(drawdown)
        and drawdown <= parameters.accumulation_drawdown_max
        and value_confirmed
    )


def generate_halving_overlay_decisions(
    features: pd.DataFrame,
    candidate: HalvingCandidate,
    *,
    decision_weekday: int = DECISION_WEEKDAY,
) -> pd.DataFrame:
    """Return raw weekly desired weights before execution-policy deadband."""

    params = candidate.parameters
    _validate_overlay_parameters(params)
    if candidate.overlay_mode not in {
        "none",
        "phase_cap",
        "phase_risk",
        "confirmation",
        "cycle_value",
    }:
        raise ValueError(f"Unsupported overlay mode: {candidate.overlay_mode}")
    if candidate.rebalance_policy not in {"target_change", "actual_weight"}:
        raise ValueError(f"Unsupported rebalance policy: {candidate.rebalance_policy}")
    required = {
        "momentum_feature_ready",
        "realized_volatility",
        "phase_label",
        "cycle_progress",
        *(f"momentum_{horizon}d" for horizon in params.momentum_horizons_days),
    }
    missing = sorted(required.difference(features.columns))
    if missing:
        raise ValueError(f"Missing halving overlay features: {', '.join(missing)}")

    rows: list[dict[str, Any]] = []
    for index, row in features.iterrows():
        phase = str(row.get("phase_label", "UNKNOWN"))
        cycle_progress = _safe_float(row, "cycle_progress")
        halving_ready = (
            phase != "UNKNOWN"
            and math.isfinite(cycle_progress)
            and 0 <= cycle_progress < 1
        )
        is_decision_day = (
            index.dayofweek == decision_weekday
            and bool(row.get("momentum_feature_ready", False))
            and (halving_ready or not candidate.halving_required)
        )
        result: dict[str, Any] = {
            "Date": index,
            "candidate_id": candidate.candidate_id,
            "overlay_mode": candidate.overlay_mode,
            "rebalance_policy": candidate.rebalance_policy,
            "halving_required": candidate.halving_required,
            "is_decision_day": bool(is_decision_day),
            "phase_label": phase,
            "cycle_progress": cycle_progress,
            "positive_momentum_count": 0,
            "positive_horizons": "없음",
            "momentum_weight": np.nan,
            "risk_budget": np.nan,
            "realized_volatility": _safe_float(row, "realized_volatility"),
            "volatility_cap": np.nan,
            "base_weight": np.nan,
            "phase_cap": np.nan,
            "desired_weight": np.nan,
            "value_floor_active": False,
            "overheat_domains": 0,
            "overheat_reasons": "",
            "reason": (
                "반감기 컨텍스트 미확인"
                if candidate.halving_required and not halving_ready
                else "다음 주간 확정 신호 대기"
            ),
        }
        if not is_decision_day:
            rows.append(result)
            continue

        positive = [
            horizon
            for horizon in params.momentum_horizons_days
            if float(row[f"momentum_{horizon}d"]) > 0
        ]
        positive_count = len(positive)
        momentum_weight = (
            positive_count / len(params.momentum_horizons_days)
        ) * params.max_weight
        realized_volatility = float(row["realized_volatility"])
        risk_budget = params.target_annual_volatility
        if candidate.overlay_mode == "phase_risk":
            risk_budget = _phase_value(params.phase_risk_budgets, phase)
        volatility_cap = min(
            params.max_weight, risk_budget / realized_volatility
        )
        base_weight = min(momentum_weight, volatility_cap, params.max_weight)
        phase_cap = (
            params.max_weight
            if candidate.overlay_mode == "none"
            else _phase_value(params.phase_caps, phase)
        )
        desired = min(base_weight, phase_cap)
        reasons = [
            f"양수 모멘텀 {positive_count}/4",
            f"변동성 상한 {volatility_cap:.3f}",
        ]
        value_floor = False
        overheat_count, overheat_reasons = _overheat_domains(row, params)

        if candidate.overlay_mode == "confirmation":
            if (
                phase == "LATE_EXPANSION_DISTRIBUTION"
                and desired > params.late_cycle_confirmation_cap
                and positive_count
                < params.late_cycle_min_positive_for_high_weight
            ):
                desired = params.late_cycle_confirmation_cap
                reasons.append("후기 사이클 고비중 확인조건 미충족")

        if candidate.overlay_mode == "cycle_value":
            value_floor = _value_floor_active(row, params, phase)
            if value_floor:
                desired = max(desired, params.accumulation_floor_weight)
                desired = min(desired, phase_cap)
                reasons.append("반감기 매집국면·가치 저평가 25% 바닥비중")
            if phase == "LATE_EXPANSION_DISTRIBUTION":
                if overheat_count >= 2:
                    desired = min(desired, 0.50)
                    reasons.append("후기 사이클 과열영역 2개 이상")
                elif overheat_count == 1:
                    desired = min(desired, 0.75)
                    reasons.append("후기 사이클 과열영역 1개")

        desired = float(np.clip(desired, 0.0, params.max_weight))
        if math.isclose(desired, 0.0, abs_tol=1e-12):
            reasons.append("최종 목표 0%")
        elif phase_cap < params.max_weight:
            reasons.append(f"{phase} 상한 {phase_cap:.0%}")

        result.update(
            {
                "positive_momentum_count": positive_count,
                "positive_horizons": (
                    ",".join(f"{value}일" for value in positive)
                    if positive
                    else "없음"
                ),
                "momentum_weight": float(momentum_weight),
                "risk_budget": float(risk_budget),
                "realized_volatility": realized_volatility,
                "volatility_cap": float(volatility_cap),
                "base_weight": float(base_weight),
                "phase_cap": float(phase_cap),
                "desired_weight": desired,
                "value_floor_active": bool(value_floor),
                "overheat_domains": int(overheat_count),
                "overheat_reasons": ",".join(overheat_reasons),
                "reason": "; ".join(reasons),
            }
        )
        rows.append(result)
    return pd.DataFrame(rows).set_index("Date")


def _state_label(new_weight: float, previous_weight: float) -> str:
    if math.isclose(new_weight, previous_weight, abs_tol=1e-12):
        return "HOLD" if new_weight > 0 else "WAIT"
    if new_weight > previous_weight:
        return "BUY_MORE"
    return "REDUCE" if new_weight > 0 else "EXIT"


def simulate_weekly_strategy(
    features: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    rebalance_policy: str,
    rebalance_deadband: float,
    fee_bps: float,
    slippage_bps: float,
    additional_delay_days: int = 0,
    initial_capital: float = 1.0,
    idle_cash_annual_yield: float = 0.0,
) -> SimulationResult:
    """Execute Sunday decisions at the next available daily open."""

    if rebalance_policy not in {"target_change", "actual_weight"}:
        raise ValueError("rebalance_policy must be target_change or actual_weight")
    if not 0 <= rebalance_deadband < 1:
        raise ValueError("rebalance deadband must be in [0, 1)")
    if fee_bps < 0 or slippage_bps < 0 or additional_delay_days < 0:
        raise ValueError("Costs and delays must be non-negative")
    if initial_capital <= 0 or idle_cash_annual_yield < 0:
        raise ValueError("Initial capital must be positive and cash yield non-negative")
    required = {"open", "close"}
    if not required.issubset(features.columns):
        raise ValueError("Features require open and close")
    if not {"desired_weight", "is_decision_day"}.issubset(decisions.columns):
        raise ValueError("Decisions require desired_weight and is_decision_day")

    shift_days = 1 + additional_delay_days
    decision_target = decisions["desired_weight"].where(
        decisions["is_decision_day"].fillna(False)
    )
    execution_target = decision_target.shift(shift_days)
    execution_reason = decisions["reason"].where(
        decisions["is_decision_day"].fillna(False)
    ).shift(shift_days)
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    daily_cash_rate = (1 + idle_cash_annual_yield) ** (1 / 365) - 1

    cash = float(initial_capital)
    units = 0.0
    last_executed_target = 0.0
    previous_equity = float(initial_capital)
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for position, index in enumerate(features.index):
        if position > 0 and daily_cash_rate > 0:
            cash *= 1 + daily_cash_rate
        open_price = float(features.at[index, "open"])
        close_price = float(features.at[index, "close"])
        if not math.isfinite(open_price) or open_price <= 0:
            raise ValueError(f"Invalid open price on {index}")
        if not math.isfinite(close_price) or close_price <= 0:
            raise ValueError(f"Invalid close price on {index}")

        open_nav = cash + units * open_price
        actual_open_weight = units * open_price / open_nav if open_nav > 0 else 0.0
        desired_raw = execution_target.loc[index]
        fee_cost = 0.0
        slippage_cost = 0.0
        turnover = 0.0
        traded_units = 0.0
        trade_side = ""
        executed = False
        desired = math.nan

        if pd.notna(desired_raw):
            desired = float(np.clip(float(desired_raw), 0.0, 1.0))
            if rebalance_policy == "target_change":
                reference_weight = last_executed_target
            else:
                reference_weight = actual_open_weight
            force_exit = math.isclose(desired, 0.0, abs_tol=1e-12) and (
                units > 1e-15 or last_executed_target > 1e-15
            )
            should_trade = force_exit or (
                abs(desired - reference_weight) + 1e-12 >= rebalance_deadband
            )

            if should_trade:
                desired_units = open_nav * desired / open_price
                unit_change = desired_units - units
                if unit_change > 1e-15:
                    execution_price = open_price * (1 + slippage_rate)
                    max_units = cash / (execution_price * (1 + fee_rate))
                    bought = min(unit_change, max_units)
                    notional = bought * execution_price
                    fee_cost = notional * fee_rate
                    slippage_cost = bought * open_price * slippage_rate
                    cash -= notional + fee_cost
                    units += bought
                    traded_units = bought
                    trade_side = "BUY"
                elif unit_change < -1e-15:
                    execution_price = open_price * (1 - slippage_rate)
                    sold = min(-unit_change, units)
                    notional = sold * execution_price
                    fee_cost = notional * fee_rate
                    slippage_cost = sold * open_price * slippage_rate
                    cash += notional - fee_cost
                    units -= sold
                    traded_units = -sold
                    trade_side = "SELL"
                if trade_side:
                    turnover = abs(traded_units) * open_price / open_nav
                    signal_position = position - shift_days
                    signal_date = (
                        features.index[signal_position].date().isoformat()
                        if signal_position >= 0
                        else None
                    )
                    trade_rows.append(
                        {
                            "date": index,
                            "signal_date": signal_date,
                            "side": trade_side,
                            "state": _state_label(desired, actual_open_weight),
                            "desired_weight": desired,
                            "reference_weight": reference_weight,
                            "actual_open_weight": actual_open_weight,
                            "units": abs(traded_units),
                            "open_price": open_price,
                            "fee_cost": fee_cost,
                            "slippage_cost": slippage_cost,
                            "turnover": turnover,
                            "reason": execution_reason.loc[index],
                        }
                    )
                    executed = True
                last_executed_target = desired

        cash = max(cash, 0.0)
        units = max(units, 0.0)
        equity = cash + units * close_price
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "equity": equity,
                "daily_return": daily_return,
                "cash": cash,
                "btc_units": units,
                "actual_weight": actual_weight,
                "actual_open_weight": actual_open_weight,
                "executed_target": last_executed_target,
                "decision_target": desired,
                "executed_trade": executed,
                "turnover": turnover,
                "fee_cost": fee_cost,
                "slippage_cost": slippage_cost,
            }
        )
        previous_equity = equity

    return SimulationResult(
        daily=pd.DataFrame(daily_rows).set_index("Date"),
        trades=pd.DataFrame(trade_rows),
    )


def _metrics_row(
    item_id: str,
    simulation: SimulationResult,
    initial_capital_krw: float,
) -> dict[str, Any]:
    metrics = performance_metrics(simulation)
    terminal = float(metrics["terminal_wealth"])
    return {
        "item_id": item_id,
        "initial_capital_krw": initial_capital_krw,
        "terminal_wealth_krw": terminal,
        "profit_krw": terminal - initial_capital_krw,
        "capital_multiple": terminal / initial_capital_krw,
        **metrics,
    }


def _simulate_candidate(
    features: pd.DataFrame,
    candidate: HalvingCandidate,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
    additional_delay_days: int = 0,
    idle_cash_annual_yield: float = 0.0,
) -> tuple[pd.DataFrame, SimulationResult]:
    decisions = generate_halving_overlay_decisions(features, candidate)
    simulation = simulate_weekly_strategy(
        features,
        decisions,
        rebalance_policy=candidate.rebalance_policy,
        rebalance_deadband=candidate.parameters.rebalance_deadband,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        additional_delay_days=additional_delay_days,
        initial_capital=initial_capital_krw,
        idle_cash_annual_yield=idle_cash_annual_yield,
    )
    return decisions, simulation


def evaluate_candidates(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[
    pd.DataFrame,
    dict[str, tuple[HalvingCandidate, pd.DataFrame, SimulationResult]],
]:
    rows: list[dict[str, Any]] = []
    outputs: dict[
        str, tuple[HalvingCandidate, pd.DataFrame, SimulationResult]
    ] = {}
    for candidate in candidate_specs():
        decisions, simulation = _simulate_candidate(
            features,
            candidate,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "overlay_mode": candidate.overlay_mode,
                "target_annual_volatility": (
                    candidate.parameters.target_annual_volatility
                ),
                "rebalance_policy": candidate.rebalance_policy,
                "halving_required": candidate.halving_required,
                "selection_eligible": candidate.selection_eligible,
                "description": candidate.description,
                **_metrics_row(
                    candidate.candidate_id,
                    simulation,
                    initial_capital_krw,
                ),
            }
        )
        outputs[candidate.candidate_id] = (candidate, decisions, simulation)
    return pd.DataFrame(rows), outputs


def _period_metrics(simulation: SimulationResult, index: pd.Index) -> dict[str, Any]:
    daily = simulation.daily.reindex(index).dropna(subset=["daily_return"])
    if daily.empty:
        return {
            "total_return": np.nan,
            "cagr": np.nan,
            "mdd": np.nan,
            "sharpe": np.nan,
            "sortino": np.nan,
            "calmar": np.nan,
            "exposure": np.nan,
            "trades": 0,
        }
    metrics = annualized_metrics(daily["daily_return"], periods_per_year=365)
    if simulation.trades.empty or "date" not in simulation.trades:
        trades = 0
    else:
        dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        trades = int(
            ((dates >= daily.index.min()) & (dates <= daily.index.max())).sum()
        )
    return {
        **metrics,
        "exposure": float(daily["actual_weight"].mean()),
        "trades": trades,
    }


def completed_halving_epochs(features: pd.DataFrame) -> list[int]:
    completed: list[int] = []
    for epoch, group in features.groupby("halving_epoch"):
        if pd.isna(epoch):
            continue
        progress = pd.to_numeric(group["cycle_progress"], errors="coerce").dropna()
        if not progress.empty and progress.min() <= 0.05 and progress.max() >= 0.95:
            completed.append(int(epoch))
    return sorted(completed)


def candidate_cycle_metrics(
    features: pd.DataFrame,
    outputs: Mapping[
        str, tuple[HalvingCandidate, pd.DataFrame, SimulationResult]
    ],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    completed = set(completed_halving_epochs(features))
    for candidate_id, (_, _, simulation) in outputs.items():
        for epoch, group in features.groupby("halving_epoch"):
            if pd.isna(epoch) or len(group) < 30:
                continue
            metrics = _period_metrics(simulation, group.index)
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "halving_epoch": int(epoch),
                    "cycle_complete": int(epoch) in completed,
                    "start": group.index.min().date().isoformat(),
                    "end": group.index.max().date().isoformat(),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def evaluate_data_modes(
    synthetic_history: pd.DataFrame,
    upbit_history: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    base_params = MomentumVolatilityParameters()
    synthetic = add_momentum_volatility_features(synthetic_history, base_params)
    upbit = add_momentum_volatility_features(upbit_history, base_params)
    common = synthetic.index.intersection(upbit.index)
    ready = (
        synthetic.loc[common, "momentum_feature_ready"].fillna(False)
        & upbit.loc[common, "momentum_feature_ready"].fillna(False)
    )
    common = common[ready.to_numpy()]
    if len(common) < 730:
        raise BtcDataError("v0.6 data-mode overlap has fewer than 730 rows")

    rows: list[dict[str, Any]] = []
    for mode, history in (("synthetic", synthetic), ("upbit", upbit)):
        features = history.loc[common].copy()
        for candidate in candidate_specs():
            _, simulation = _simulate_candidate(
                features,
                candidate,
                initial_capital_krw=initial_capital_krw,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            rows.append(
                {
                    "data_mode": mode,
                    "candidate_id": candidate.candidate_id,
                    **_metrics_row(
                        candidate.candidate_id,
                        simulation,
                        initial_capital_krw,
                    ),
                }
            )
        buy_hold = simulate_strategy(
            features,
            make_buy_hold_signals(features),
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital_krw,
        )
        rows.append(
            {
                "data_mode": mode,
                "candidate_id": "buy_hold",
                **_metrics_row("buy_hold", buy_hold, initial_capital_krw),
            }
        )
    metadata = {
        "start": common.min().date().isoformat(),
        "end": common.max().date().isoformat(),
        "rows": int(len(common)),
    }
    return pd.DataFrame(rows), metadata


def rank_candidates(
    full_metrics: pd.DataFrame,
    data_modes: pd.DataFrame,
    cycle_metrics: pd.DataFrame,
) -> pd.DataFrame:
    full = full_metrics.copy()
    full = full.rename(
        columns={
            "cagr": "full_cagr",
            "mdd": "full_mdd",
            "calmar": "full_calmar",
            "terminal_wealth_krw": "full_terminal_wealth_krw",
        }
    )
    upbit = data_modes.loc[data_modes["data_mode"] == "upbit"].copy()
    upbit = upbit.rename(
        columns={
            "cagr": "upbit_cagr",
            "mdd": "upbit_mdd",
            "calmar": "upbit_calmar",
            "terminal_wealth_krw": "upbit_terminal_wealth_krw",
        }
    )
    merged = full.merge(
        upbit[
            [
                "candidate_id",
                "upbit_cagr",
                "upbit_mdd",
                "upbit_calmar",
                "upbit_terminal_wealth_krw",
            ]
        ],
        on="candidate_id",
        how="left",
    )
    completed = cycle_metrics.loc[cycle_metrics["cycle_complete"]].copy()
    cycle_floor = (
        completed.groupby("candidate_id")
        .agg(
            completed_cycle_min_cagr=("cagr", "min"),
            completed_cycle_worst_mdd=("mdd", "min"),
            completed_cycle_min_calmar=("calmar", "min"),
        )
        .reset_index()
    )
    merged = merged.merge(cycle_floor, on="candidate_id", how="left")
    merged["risk_gate_pass"] = (
        merged["selection_eligible"].fillna(False).astype(bool)
        & (merged["full_mdd"] >= -0.50)
        & (merged["upbit_mdd"] >= -0.50)
    )
    merged["robust_calmar"] = merged[["full_calmar", "upbit_calmar"]].min(axis=1)
    merged["robust_cagr"] = merged[["full_cagr", "upbit_cagr"]].min(axis=1)
    merged["rank"] = np.nan
    eligible = merged.loc[merged["risk_gate_pass"]].copy()
    eligible = eligible.sort_values(
        [
            "completed_cycle_min_calmar",
            "robust_calmar",
            "robust_cagr",
            "turnover",
        ],
        ascending=[False, False, False, True],
        na_position="last",
    )
    for rank, index in enumerate(eligible.index, start=1):
        merged.loc[index, "rank"] = rank
    return merged.sort_values(
        ["risk_gate_pass", "rank", "robust_calmar"],
        ascending=[False, True, False],
        na_position="last",
    )


def cycle_window_specs() -> list[CycleWindowCandidate]:
    rows: list[CycleWindowCandidate] = []
    for pre_start in (0.70, 0.75, 0.80, 0.85):
        for post_end in (0.25, 0.30, 0.35, 0.40, 0.45):
            for target_vol in (None, 0.40):
                suffix = "full" if target_vol is None else "vol40"
                rows.append(
                    CycleWindowCandidate(
                        candidate_id=(
                            f"window_pre{int(pre_start * 100):02d}_"
                            f"post{int(post_end * 100):02d}_{suffix}"
                        ),
                        pre_halving_start_progress=pre_start,
                        post_halving_end_progress=post_end,
                        target_annual_volatility=target_vol,
                    )
                )
    return rows


def generate_cycle_window_decisions(
    features: pd.DataFrame,
    candidate: CycleWindowCandidate,
    *,
    decision_weekday: int = DECISION_WEEKDAY,
) -> pd.DataFrame:
    if not 0 < candidate.pre_halving_start_progress < 1:
        raise ValueError("Pre-halving start progress must be in (0, 1)")
    if not 0 < candidate.post_halving_end_progress < 1:
        raise ValueError("Post-halving end progress must be in (0, 1)")
    rows: list[dict[str, Any]] = []
    for index, row in features.iterrows():
        phase = str(row.get("phase_label", "UNKNOWN"))
        progress = _safe_float(row, "cycle_progress")
        volatility = _safe_float(row, "realized_volatility")
        ready = (
            bool(row.get("momentum_feature_ready", False))
            and phase != "UNKNOWN"
            and math.isfinite(progress)
            and math.isfinite(volatility)
            and volatility > 0
        )
        is_decision = index.dayofweek == decision_weekday and ready
        desired = np.nan
        reason = "다음 주간 확정 신호 대기"
        if is_decision:
            in_window = (
                progress >= candidate.pre_halving_start_progress
                or progress <= candidate.post_halving_end_progress
            )
            desired = 1.0 if in_window else 0.0
            if in_window and candidate.target_annual_volatility is not None:
                desired = min(
                    1.0, candidate.target_annual_volatility / volatility
                )
            reason = (
                "반감기 전후 고정 보유창"
                if in_window
                else "고정 반감기 보유창 밖"
            )
        rows.append(
            {
                "Date": index,
                "is_decision_day": bool(is_decision),
                "desired_weight": desired,
                "reason": reason,
                "phase_label": phase,
                "cycle_progress": progress,
                "realized_volatility": volatility,
            }
        )
    return pd.DataFrame(rows).set_index("Date")


def evaluate_cycle_window_grid(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[
    pd.DataFrame,
    dict[str, tuple[CycleWindowCandidate, pd.DataFrame, SimulationResult]],
]:
    rows: list[dict[str, Any]] = []
    outputs: dict[
        str, tuple[CycleWindowCandidate, pd.DataFrame, SimulationResult]
    ] = {}
    for candidate in cycle_window_specs():
        decisions = generate_cycle_window_decisions(features, candidate)
        simulation = simulate_weekly_strategy(
            features,
            decisions,
            rebalance_policy="actual_weight",
            rebalance_deadband=candidate.rebalance_deadband,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital_krw,
        )
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "pre_halving_start_progress": (
                    candidate.pre_halving_start_progress
                ),
                "post_halving_end_progress": (
                    candidate.post_halving_end_progress
                ),
                "target_annual_volatility": candidate.target_annual_volatility,
                **_metrics_row(
                    candidate.candidate_id, simulation, initial_capital_krw
                ),
            }
        )
        outputs[candidate.candidate_id] = (candidate, decisions, simulation)
    return pd.DataFrame(rows), outputs


def _select_window_candidate(
    features: pd.DataFrame,
    outputs: Mapping[
        str, tuple[CycleWindowCandidate, pd.DataFrame, SimulationResult]
    ],
    train_epochs: set[int],
) -> tuple[str | None, dict[str, Any] | None]:
    train_index = features.index[
        features["halving_epoch"].isin(sorted(train_epochs))
    ]
    rows: list[tuple[str, dict[str, Any]]] = []
    for candidate_id, (_, _, simulation) in outputs.items():
        metrics = _period_metrics(simulation, train_index)
        if (
            math.isfinite(float(metrics["calmar"]))
            and float(metrics["mdd"]) >= -0.50
        ):
            rows.append((candidate_id, metrics))
    if not rows:
        return None, None
    rows.sort(
        key=lambda item: (
            float(item[1]["calmar"]),
            float(item[1]["cagr"]),
            -float(item[1]["trades"]),
        ),
        reverse=True,
    )
    return rows[0]


def cycle_window_walkforward(
    features: pd.DataFrame,
    outputs: Mapping[
        str, tuple[CycleWindowCandidate, pd.DataFrame, SimulationResult]
    ],
    buy_hold: SimulationResult,
) -> pd.DataFrame:
    completed = completed_halving_epochs(features)
    rows: list[dict[str, Any]] = []
    for test_epoch in completed:
        train_epochs = set(completed) - {test_epoch}
        if not train_epochs:
            continue
        candidate_id, train_metrics = _select_window_candidate(
            features, outputs, train_epochs
        )
        test_index = features.index[features["halving_epoch"] == test_epoch]
        if candidate_id is None or train_metrics is None:
            continue
        test_metrics = _period_metrics(outputs[candidate_id][2], test_index)
        hold_metrics = _period_metrics(buy_hold, test_index)
        rows.append(
            {
                "test_epoch": test_epoch,
                "test_is_complete": True,
                "train_epochs": ",".join(str(value) for value in sorted(train_epochs)),
                "selected_candidate": candidate_id,
                "train_cagr": train_metrics["cagr"],
                "train_mdd": train_metrics["mdd"],
                "train_calmar": train_metrics["calmar"],
                "test_total_return": test_metrics["total_return"],
                "test_cagr": test_metrics["cagr"],
                "test_mdd": test_metrics["mdd"],
                "buy_hold_test_total_return": hold_metrics["total_return"],
                "buy_hold_test_mdd": hold_metrics["mdd"],
            }
        )

    all_epochs = sorted(
        int(value)
        for value in features["halving_epoch"].dropna().unique()
    )
    current_epoch = all_epochs[-1] if all_epochs else None
    if current_epoch is not None and current_epoch not in completed and completed:
        candidate_id, train_metrics = _select_window_candidate(
            features, outputs, set(completed)
        )
        if candidate_id is not None and train_metrics is not None:
            test_index = features.index[
                features["halving_epoch"] == current_epoch
            ]
            test_metrics = _period_metrics(outputs[candidate_id][2], test_index)
            hold_metrics = _period_metrics(buy_hold, test_index)
            rows.append(
                {
                    "test_epoch": current_epoch,
                    "test_is_complete": False,
                    "train_epochs": ",".join(str(value) for value in completed),
                    "selected_candidate": candidate_id,
                    "train_cagr": train_metrics["cagr"],
                    "train_mdd": train_metrics["mdd"],
                    "train_calmar": train_metrics["calmar"],
                    "test_total_return": test_metrics["total_return"],
                    "test_cagr": test_metrics["cagr"],
                    "test_mdd": test_metrics["mdd"],
                    "buy_hold_test_total_return": hold_metrics["total_return"],
                    "buy_hold_test_mdd": hold_metrics["mdd"],
                }
            )
    return pd.DataFrame(rows)


def oracle_timing_bounds(
    features: pd.DataFrame,
    *,
    pre_window_start_progress: float = 0.60,
    post_window_end_progress: float = 0.50,
) -> pd.DataFrame:
    """Unattainable close-to-close upper bounds around each halving."""

    rows: list[dict[str, Any]] = []
    epochs = sorted(
        int(value)
        for value in features["halving_epoch"].dropna().unique()
    )
    for epoch in epochs:
        previous = features.loc[
            (features["halving_epoch"] == epoch - 1)
            & (features["cycle_progress"] >= pre_window_start_progress)
        ]
        post = features.loc[
            (features["halving_epoch"] == epoch)
            & (features["cycle_progress"] <= post_window_end_progress)
        ]
        if previous.empty or post.empty:
            continue
        buy_date = previous["close"].idxmin()
        buy_price = float(previous.loc[buy_date, "close"])
        eligible_post = post.loc[post.index > buy_date]
        if eligible_post.empty:
            continue
        sell_date = eligible_post["close"].idxmax()
        sell_price = float(eligible_post.loc[sell_date, "close"])
        max_progress = float(post["cycle_progress"].max())
        rows.append(
            {
                "halving_epoch": epoch,
                "pre_window_start_progress": pre_window_start_progress,
                "post_window_end_progress": post_window_end_progress,
                "buy_date": buy_date.date().isoformat(),
                "buy_price": buy_price,
                "sell_date": sell_date.date().isoformat(),
                "sell_price": sell_price,
                "holding_days": int((sell_date - buy_date).days),
                "gross_return": sell_price / buy_price - 1,
                "post_window_complete": max_progress >= post_window_end_progress - 0.01,
                "warning": "미래 가격을 사용한 도달 불가능 상한",
            }
        )
    return pd.DataFrame(rows)


def robustness_diagnostics(
    features: pd.DataFrame,
    candidate: HalvingCandidate,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    scenarios: list[
        tuple[str, HalvingCandidate, float, float, int, float]
    ] = [
        ("base", candidate, fee_bps, slippage_bps, 0, 0.0),
        ("double_cost", candidate, fee_bps * 2, slippage_bps * 2, 0, 0.0),
        ("delay_1d", candidate, fee_bps, slippage_bps, 1, 0.0),
        ("delay_2d", candidate, fee_bps, slippage_bps, 2, 0.0),
        (
            "deadband_05",
            replace(
                candidate,
                parameters=replace(
                    candidate.parameters, rebalance_deadband=0.05
                ),
            ),
            fee_bps,
            slippage_bps,
            0,
            0.0,
        ),
        (
            "deadband_15",
            replace(
                candidate,
                parameters=replace(
                    candidate.parameters, rebalance_deadband=0.15
                ),
            ),
            fee_bps,
            slippage_bps,
            0,
            0.0,
        ),
        ("idle_cash_3pct", candidate, fee_bps, slippage_bps, 0, 0.03),
    ]
    rows: list[dict[str, Any]] = []
    for scenario, spec, scenario_fee, scenario_slippage, delay, cash_yield in scenarios:
        _, simulation = _simulate_candidate(
            features,
            spec,
            initial_capital_krw=initial_capital_krw,
            fee_bps=scenario_fee,
            slippage_bps=scenario_slippage,
            additional_delay_days=delay,
            idle_cash_annual_yield=cash_yield,
        )
        rows.append(
            {
                "scenario": scenario,
                "candidate_id": spec.candidate_id,
                "fee_bps": scenario_fee,
                "slippage_bps": scenario_slippage,
                "additional_delay_days": delay,
                "rebalance_deadband": spec.parameters.rebalance_deadband,
                "idle_cash_annual_yield": cash_yield,
                **_metrics_row(scenario, simulation, initial_capital_krw),
            }
        )
    return pd.DataFrame(rows)


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):,.0f}원"


def _markdown_table(
    frame: pd.DataFrame,
    columns: Sequence[tuple[str, str, str]],
    *,
    max_rows: int | None = None,
) -> list[str]:
    selected = frame.head(max_rows) if max_rows is not None else frame
    headers = [label for _, label, _ in columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for _, row in selected.iterrows():
        values: list[str] = []
        for key, _, fmt in columns:
            value = row.get(key)
            if fmt == "pct":
                values.append(_fmt_pct(value))
            elif fmt == "krw":
                values.append(_fmt_krw(value))
            elif fmt == "int":
                values.append("n/a" if pd.isna(value) else str(int(value)))
            elif fmt == "float":
                values.append("n/a" if pd.isna(value) else f"{float(value):.3f}")
            elif fmt == "bool":
                values.append("예" if bool(value) else "아니오")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return lines


def build_report(
    *,
    manifest: Mapping[str, Any],
    candidates: pd.DataFrame,
    ranking: pd.DataFrame,
    data_modes: pd.DataFrame,
    cycle_metrics: pd.DataFrame,
    window_grid: pd.DataFrame,
    window_walkforward: pd.DataFrame,
    oracle: pd.DataFrame,
    robustness: pd.DataFrame,
    current_state: Mapping[str, Any],
) -> str:
    selected_id = str(manifest["research_recommendation"])
    selected_row = candidates.loc[
        candidates["candidate_id"] == selected_id
    ].iloc[0]
    lines = [
        "# BTC 반감기 필수 오버레이 연구 v0.6",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 연구기간: {manifest['data_start']}~{manifest['data_end']}",
        f"- 연구 추천 후보: `{selected_id}`",
        "- 상태: 과거 연구 완료 / 실전·웹·텔레그램 미승인",
        "",
        "> v0.6은 v0.5를 폐기하지 않고 무반감기 기준선으로 보존한 뒤,",
        "> 실제 block-height 기반 반감기 국면을 비중 상한·확인·가치매집·과열축소에 결합한다.",
        "",
        "## 핵심 결과",
        "",
        f"- 최종자산: {_fmt_krw(selected_row['terminal_wealth_krw'])}",
        f"- CAGR / MDD: {_fmt_pct(selected_row['cagr'])} / {_fmt_pct(selected_row['mdd'])}",
        f"- 평균 BTC 노출: {_fmt_pct(selected_row['exposure'])}",
        f"- 거래 횟수: {int(selected_row['trades'])}",
        "",
        "## 후보 비교",
        "",
    ]
    lines.extend(
        _markdown_table(
            ranking,
            [
                ("candidate_id", "후보", "text"),
                ("full_cagr", "전체 CAGR", "pct"),
                ("full_mdd", "전체 MDD", "pct"),
                ("upbit_cagr", "Upbit CAGR", "pct"),
                ("upbit_mdd", "Upbit MDD", "pct"),
                ("robust_calmar", "강건 Calmar", "float"),
                ("risk_gate_pass", "MDD 문턱", "bool"),
                ("rank", "순위", "int"),
            ],
        )
    )
    lines.extend(["", "## 실제 Upbit 중첩구간", ""])
    upbit_rows = data_modes.loc[
        data_modes["data_mode"] == "upbit"
    ].sort_values("calmar", ascending=False)
    lines.extend(
        _markdown_table(
            upbit_rows,
            [
                ("candidate_id", "후보", "text"),
                ("terminal_wealth_krw", "최종자산", "krw"),
                ("cagr", "CAGR", "pct"),
                ("mdd", "MDD", "pct"),
                ("calmar", "Calmar", "float"),
            ],
        )
    )
    lines.extend(["", "## 완료 반감기 epoch별 후보 성과", ""])
    selected_cycles = cycle_metrics.loc[
        cycle_metrics["candidate_id"] == selected_id
    ]
    lines.extend(
        _markdown_table(
            selected_cycles,
            [
                ("halving_epoch", "Epoch", "int"),
                ("cycle_complete", "완료", "bool"),
                ("start", "시작", "text"),
                ("end", "종료", "text"),
                ("total_return", "누적수익", "pct"),
                ("cagr", "CAGR", "pct"),
                ("mdd", "MDD", "pct"),
                ("exposure", "평균노출", "pct"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 반감기 전후 고정 보유창",
            "",
            "고정 날짜 전략은 반감기만으로 매매하는 단순 비교군이다. 전체기간 최고값을 실전 채택하지 않고, 완료 epoch 하나씩 제외한 결과를 함께 본다.",
            "",
        ]
    )
    top_windows = window_grid.loc[window_grid["mdd"] >= -0.50].sort_values(
        "calmar", ascending=False
    )
    lines.extend(
        _markdown_table(
            top_windows,
            [
                ("candidate_id", "후보", "text"),
                ("cagr", "CAGR", "pct"),
                ("mdd", "MDD", "pct"),
                ("calmar", "Calmar", "float"),
                ("trades", "거래", "int"),
            ],
            max_rows=10,
        )
    )
    lines.extend(["", "### Cycle holdout", ""])
    if window_walkforward.empty:
        lines.append("완료 epoch가 부족하여 계산하지 못했다.")
    else:
        lines.extend(
            _markdown_table(
                window_walkforward,
                [
                    ("test_epoch", "테스트 Epoch", "int"),
                    ("test_is_complete", "완료", "bool"),
                    ("selected_candidate", "훈련선택", "text"),
                    ("test_cagr", "테스트 CAGR", "pct"),
                    ("test_mdd", "테스트 MDD", "pct"),
                    ("buy_hold_test_total_return", "보유 누적", "pct"),
                ],
            )
        )
    lines.extend(
        [
            "",
            "## 신의 타이밍 상한",
            "",
            "아래 값은 반감기 전 구간의 사후 최저가와 반감기 후 구간의 사후 최고가를 사용하므로 실전 불가능하다.",
            "",
        ]
    )
    if oracle.empty:
        lines.append("계산 가능한 반감기 구간이 없다.")
    else:
        lines.extend(
            _markdown_table(
                oracle,
                [
                    ("halving_epoch", "Epoch", "int"),
                    ("buy_date", "사후 최저 매수일", "text"),
                    ("sell_date", "사후 최고 매도일", "text"),
                    ("gross_return", "도달불가 수익", "pct"),
                    ("holding_days", "보유일", "int"),
                    ("post_window_complete", "후행창 완료", "bool"),
                ],
            )
        )
    lines.extend(["", "## 강건성", ""])
    lines.extend(
        _markdown_table(
            robustness,
            [
                ("scenario", "조건", "text"),
                ("cagr", "CAGR", "pct"),
                ("mdd", "MDD", "pct"),
                ("calmar", "Calmar", "float"),
                ("trades", "거래", "int"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 최근 확정 주간판단",
            "",
            f"- 판단일: {current_state['date']}",
            f"- 반감기 국면: {current_state['phase_label']}",
            f"- 사이클 진행률: {_fmt_pct(current_state['cycle_progress'])}",
            f"- 양수 모멘텀: {current_state['positive_horizons']}",
            f"- 모멘텀 기본비중: {_fmt_pct(current_state['momentum_weight'])}",
            f"- 변동성 상한: {_fmt_pct(current_state['volatility_cap'])}",
            f"- 반감기 상한: {_fmt_pct(current_state['phase_cap'])}",
            f"- 연구 목표비중: {_fmt_pct(current_state['desired_weight'])}",
            f"- 근거: {current_state['reason']}",
            "",
            "## 판정",
            "",
            "- v0.5는 무반감기 비교 기준선으로 유지한다.",
            "- v0.6 후보는 실제 block-height 기반 반감기 입력이 없으면 의사결정하지 않는다.",
            "- 전체기간 최고 수익 한 점이 아니라 Upbit 중첩, 완료 사이클, 비용·지연·데드밴드 강건성을 함께 본다.",
            "- 현재 연구 결과는 2016년 이후 과거 데이터를 본 뒤 설계한 것이므로 순수 표본외 성과가 아니다.",
            "- 30개 확정 일봉 Shadow는 프로그램 운영검증이며 전략 수익성을 증명하지 않는다.",
            "- 자동주문은 금지하며 실전·웹·Telegram 연결은 사용자 승인 전까지 보류한다.",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _choose_research_candidate(ranking: pd.DataFrame) -> str:
    deployable = ranking.loc[ranking["selection_eligible"].fillna(False)].copy()
    if deployable.empty:
        raise BtcDataError("No halving-required v0.6 candidate is available")
    eligible = deployable.loc[deployable["risk_gate_pass"]].copy()
    if not eligible.empty:
        ranked = eligible.dropna(subset=["rank"]).sort_values("rank")
        return str((ranked if not ranked.empty else eligible).iloc[0]["candidate_id"])
    fallback = deployable.sort_values(
        ["robust_calmar", "full_mdd", "upbit_mdd"],
        ascending=[False, False, False],
        na_position="last",
    )
    return str(fallback.iloc[0]["candidate_id"])


def run_halving_research(
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
        raise BtcDataError("v0.6 research requires shadow mode and no orders")

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
    synthetic_history = build_feature_frame(
        synthetic,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    upbit_history = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    momentum_params = MomentumVolatilityParameters()
    feature_history = add_momentum_volatility_features(
        synthetic_history, momentum_params
    )
    features = feature_history.loc[pd.Timestamp(start_date) :].copy()
    ready = features["momentum_feature_ready"].fillna(False) & features[
        "phase_label"
    ].ne("UNKNOWN")
    if not ready.any() or ready.idxmax() > pd.Timestamp(start_date) + pd.Timedelta(
        days=7
    ):
        raise BtcDataError("v0.6 features cannot start near requested date")
    features = features.loc[ready.idxmax() :].copy()

    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    candidates, candidate_outputs = evaluate_candidates(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    cycle_metrics = candidate_cycle_metrics(features, candidate_outputs)
    data_modes, data_mode_metadata = evaluate_data_modes(
        synthetic_history,
        upbit_history,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    ranking = rank_candidates(candidates, data_modes, cycle_metrics)
    recommendation = _choose_research_candidate(ranking)
    selected_candidate, selected_decisions, selected_simulation = (
        candidate_outputs[recommendation]
    )

    buy_hold = simulate_strategy(
        features,
        make_buy_hold_signals(features),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital_krw,
    )
    window_grid, window_outputs = evaluate_cycle_window_grid(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    window_walkforward = cycle_window_walkforward(
        features, window_outputs, buy_hold
    )
    oracle = oracle_timing_bounds(features)
    robustness = robustness_diagnostics(
        features,
        selected_candidate,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    latest_decisions = selected_decisions.loc[
        selected_decisions["is_decision_day"]
    ]
    if latest_decisions.empty:
        raise BtcDataError("v0.6 has no completed weekly decision")
    latest_date = latest_decisions.index[-1]
    current_state = {
        "date": latest_date.date().isoformat(),
        **latest_decisions.iloc[-1].to_dict(),
    }

    output_map = {
        "candidates": paths.output / "btc_halving_v06_candidates.csv",
        "ranking": paths.output / "btc_halving_v06_ranking.csv",
        "signals": paths.output / "btc_halving_v06_signals.csv",
        "cycle_metrics": paths.output / "btc_halving_v06_cycle_metrics.csv",
        "data_modes": paths.output / "btc_halving_v06_data_modes.csv",
        "window_grid": paths.output / "btc_halving_v06_window_grid.csv",
        "window_walkforward": (
            paths.output / "btc_halving_v06_window_walkforward.csv"
        ),
        "oracle": paths.output / "btc_halving_v06_oracle.csv",
        "robustness": paths.output / "btc_halving_v06_robustness.csv",
        "equity": paths.output / "btc_halving_v06_equity.csv",
        "manifest": paths.output / "btc_halving_v06_manifest.json",
        "report": paths.output / "btc_halving_v06_report.md",
    }
    candidates.to_csv(output_map["candidates"], index=False, encoding="utf-8-sig")
    ranking.to_csv(output_map["ranking"], index=False, encoding="utf-8-sig")
    selected_decisions.to_csv(output_map["signals"], encoding="utf-8-sig")
    cycle_metrics.to_csv(
        output_map["cycle_metrics"], index=False, encoding="utf-8-sig"
    )
    data_modes.to_csv(output_map["data_modes"], index=False, encoding="utf-8-sig")
    window_grid.to_csv(
        output_map["window_grid"], index=False, encoding="utf-8-sig"
    )
    window_walkforward.to_csv(
        output_map["window_walkforward"], index=False, encoding="utf-8-sig"
    )
    oracle.to_csv(output_map["oracle"], index=False, encoding="utf-8-sig")
    robustness.to_csv(
        output_map["robustness"], index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(
        {
            candidate_id: simulation.daily["equity"]
            for candidate_id, (_, _, simulation) in candidate_outputs.items()
        }
    ).to_csv(output_map["equity"], encoding="utf-8-sig")

    manifest = {
        "schema_version": "btc-halving-research-0.6",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "approved_strategy": None,
        "research_recommendation": recommendation,
        "research_recommendation_is_live_approval": False,
        "data_mode": "synthetic KRW from BTC-USD and prior-known USD/KRW",
        "data_start": features.index.min().date().isoformat(),
        "data_end": features.index.max().date().isoformat(),
        "initial_capital_krw": initial_capital_krw,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "execution_rule": "closed Sunday UTC signal, next daily open",
        "halving_interval_blocks": HALVING_INTERVAL,
        "halving_required_for_v06": True,
        "v05_kept_as_nonhalving_benchmark": True,
        "candidate_count": int(len(candidates)),
        "completed_halving_epochs": completed_halving_epochs(features),
        "candidate_definitions": [
            {
                "candidate_id": item.candidate_id,
                "overlay_mode": item.overlay_mode,
                "rebalance_policy": item.rebalance_policy,
                "halving_required": item.halving_required,
                "selection_eligible": item.selection_eligible,
                "parameters": asdict(item.parameters),
            }
            for item in candidate_specs()
        ],
        "onchain_cache_fallback": onchain_fallback,
        "onchain_cache_error": onchain_error,
        "price_overlap": overlap,
        "data_mode_overlap": data_mode_metadata,
        "limitations": [
            "pre-Upbit prices are synthetic",
            "v0.6 was designed after observing v0.3-v0.5",
            "only a small number of completed halving cycles are available",
            "the current halving epoch is incomplete",
            "oracle timing uses future information and is not deployable",
            "tax is excluded",
            "no live advisory approval",
        ],
    }
    _write_json(output_map["manifest"], manifest)
    report = build_report(
        manifest=manifest,
        candidates=candidates,
        ranking=ranking,
        data_modes=data_modes,
        cycle_metrics=cycle_metrics,
        window_grid=window_grid,
        window_walkforward=window_walkforward,
        oracle=oracle,
        robustness=robustness,
        current_state=current_state,
    )
    output_map["report"].write_text(report, encoding="utf-8-sig")

    return {
        "manifest": manifest,
        "research_recommendation": recommendation,
        "primary_metrics": performance_metrics(selected_simulation),
        "current_state": current_state,
        "candidates": candidates,
        "ranking": ranking,
        "cycle_metrics": cycle_metrics,
        "data_modes": data_modes,
        "window_grid": window_grid,
        "window_walkforward": window_walkforward,
        "oracle": oracle,
        "robustness": robustness,
        "outputs": {key: str(value) for key, value in output_map.items()},
    }


def print_summary(result: Mapping[str, Any]) -> None:
    metrics = result["primary_metrics"]
    state = result["current_state"]
    print("BTC 반감기 필수 오버레이 v0.6 연구 결과")
    print(
        f"기간: {result['manifest']['data_start']}~"
        f"{result['manifest']['data_end']}"
    )
    print(f"연구 추천 후보: {result['research_recommendation']}")
    print(
        f"CAGR/MDD: {_fmt_pct(metrics['cagr'])} / "
        f"{_fmt_pct(metrics['mdd'])}"
    )
    print(
        f"최근 판단: {state['date']} / {state['phase_label']} / "
        f"목표 {_fmt_pct(state['desired_weight'])}"
    )
    print("과거 연구 전용이며 웹·Telegram·자동주문·실전 승인은 없습니다.")
    print(f"보고서: {result['outputs']['report']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BTC halving-required overlay and timing research v0.6"
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
    result = run_halving_research(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
