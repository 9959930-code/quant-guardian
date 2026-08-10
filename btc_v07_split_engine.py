from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from btc_research import SimulationResult
from btc_v07_three_split_research import _trade_to_weight


def simulate_three_split(
    features: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    entry_parts: int,
    exit_parts: int,
    rebalance_deadband: float,
    fee_bps: float,
    slippage_bps: float,
    initial_capital: float,
) -> SimulationResult:
    """Execute qualifying target changes in one or three weekly equal-weight steps.

    The final target observed when a split starts is frozen. A later weekly target
    restarts the split only when it differs from the frozen target by at least the
    deadband or reverses direction. Repeated zero targets therefore continue an
    existing three-week exit instead of restarting it every week.
    """

    if entry_parts not in {1, 3} or exit_parts not in {1, 3}:
        raise ValueError("Entry and exit parts must be 1 or 3")
    if not 0 <= rebalance_deadband < 1:
        raise ValueError("Deadband must be in [0, 1)")
    if not {"open", "close"}.issubset(features.columns):
        raise ValueError("Features require open and close")
    if not {"desired_weight", "is_decision_day", "reason"}.issubset(
        decisions.columns
    ):
        raise ValueError(
            "Decisions require desired_weight, is_decision_day, reason"
        )

    execution_target = decisions["desired_weight"].where(
        decisions["is_decision_day"].fillna(False)
    ).shift(1)
    execution_reason = decisions["reason"].where(
        decisions["is_decision_day"].fillna(False)
    ).shift(1)
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000

    cash = float(initial_capital)
    units = 0.0
    previous_equity = float(initial_capital)
    plan: dict[str, Any] | None = None
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for position, index in enumerate(features.index):
        open_price = float(features.at[index, "open"])
        close_price = float(features.at[index, "close"])
        if not math.isfinite(open_price) or open_price <= 0:
            raise ValueError(f"Invalid open price on {index}")
        if not math.isfinite(close_price) or close_price <= 0:
            raise ValueError(f"Invalid close price on {index}")

        open_nav = cash + units * open_price
        actual_open_weight = units * open_price / open_nav if open_nav > 0 else 0.0
        latest_raw = execution_target.loc[index]
        latest_target = math.nan
        plan_restarted = False

        if pd.notna(latest_raw):
            latest_target = float(np.clip(float(latest_raw), 0.0, 1.0))
            force_zero = (
                math.isclose(latest_target, 0.0, abs_tol=1e-12)
                and units > 1e-15
            )
            latest_gap = latest_target - actual_open_weight
            trigger = force_zero or (
                abs(latest_gap) + 1e-12 >= rebalance_deadband
            )

            if plan is not None:
                frozen_target = float(plan["final_target"])
                frozen_gap = frozen_target - actual_open_weight
                frozen_direction = (
                    math.copysign(1.0, frozen_gap)
                    if not math.isclose(frozen_gap, 0.0, abs_tol=1e-12)
                    else 0.0
                )
                latest_direction = (
                    math.copysign(1.0, latest_gap)
                    if not math.isclose(latest_gap, 0.0, abs_tol=1e-12)
                    else 0.0
                )
                materially_new = (
                    abs(latest_target - frozen_target) + 1e-12
                    >= rebalance_deadband
                )
                direction_reversal = (
                    frozen_direction != 0.0
                    and latest_direction != 0.0
                    and frozen_direction != latest_direction
                )
                zero_target_changed = force_zero and not math.isclose(
                    frozen_target, 0.0, abs_tol=1e-12
                )
                if materially_new or direction_reversal or zero_target_changed:
                    plan = None
                    plan_restarted = True

            if plan is None and trigger:
                parts = (
                    entry_parts
                    if latest_target > actual_open_weight
                    else exit_parts
                )
                plan = {
                    "start_weight": actual_open_weight,
                    "final_target": latest_target,
                    "total_parts": parts,
                    "completed_parts": 0,
                    "reason": str(execution_reason.loc[index]),
                    "signal_date": (
                        features.index[position - 1].date().isoformat()
                        if position >= 1
                        else None
                    ),
                }
                plan_restarted = True

        fee_cost = 0.0
        slippage_cost = 0.0
        turnover = 0.0
        executed = False
        step_target = math.nan
        step_number = 0
        total_parts = 0
        trade_side = ""

        if pd.notna(latest_raw) and plan is not None:
            total_parts = int(plan["total_parts"])
            step_number = int(plan["completed_parts"]) + 1
            fraction = step_number / total_parts
            step_target = float(plan["start_weight"]) + (
                float(plan["final_target"]) - float(plan["start_weight"])
            ) * fraction
            cash, units, trade = _trade_to_weight(
                cash=cash,
                units=units,
                open_price=open_price,
                target_weight=step_target,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
            )
            fee_cost = float(trade["fee_cost"])
            slippage_cost = float(trade["slippage_cost"])
            turnover = float(trade["turnover"])
            trade_side = str(trade["side"])
            executed = bool(trade_side)
            if executed:
                trade_rows.append(
                    {
                        "date": index,
                        "signal_date": plan["signal_date"],
                        "side": trade_side,
                        "step_number": step_number,
                        "total_parts": total_parts,
                        "step_target": step_target,
                        "final_target": float(plan["final_target"]),
                        "actual_open_weight": float(
                            trade["actual_open_weight"]
                        ),
                        "units": abs(float(trade["traded_units"])),
                        "open_price": open_price,
                        "fee_cost": fee_cost,
                        "slippage_cost": slippage_cost,
                        "turnover": turnover,
                        "plan_restarted": plan_restarted,
                        "reason": plan["reason"],
                    }
                )
            plan["completed_parts"] = step_number
            if step_number >= total_parts:
                plan = None

        equity = cash + units * close_price
        daily_return = (
            equity / previous_equity - 1 if previous_equity > 0 else 0.0
        )
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
                "latest_target": latest_target,
                "step_target": step_target,
                "step_number": step_number,
                "total_parts": total_parts,
                "executed_trade": executed,
                "trade_side": trade_side,
                "plan_active_after": plan is not None,
                "plan_restarted": plan_restarted,
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
