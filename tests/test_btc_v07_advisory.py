from __future__ import annotations

import math
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from btc_v07_advisory import (
    DEFAULT_BUDGET_KRW,
    StrategySignal,
    _initial_state,
    _normal_run,
    build_strategy_signal,
    is_holding_window,
    plan_rebalance,
    realized_volatility_at,
)


def history(days: int = 500) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    returns = np.resize(np.array([0.01, -0.005, 0.002, -0.001]), days)
    close = 100.0 * np.cumprod(1 + returns)
    return pd.DataFrame({"Close": close}, index=index)


class BtcV07StrategyTests(unittest.TestCase):
    def test_holding_window_wraps_halving_boundary(self) -> None:
        self.assertTrue(is_holding_window(0.70))
        self.assertTrue(is_holding_window(0.20))
        self.assertFalse(is_holding_window(0.50))

    def test_realized_volatility_uses_only_history_to_decision_date(self) -> None:
        frame = history()
        decision = pd.Timestamp("2024-12-29")
        result = realized_volatility_at(frame["Close"], decision)
        expected = (
            frame.loc[:decision, "Close"]
            .pct_change(fill_method=None)
            .dropna()
            .tail(63)
            .std(ddof=0)
            * math.sqrt(365)
        )
        self.assertAlmostEqual(result, float(expected))

    def test_outside_window_target_is_zero(self) -> None:
        signal = build_strategy_signal(
            history(),
            {"cycle_progress": 0.5752, "phase_label": "CONTRACTION_RECOVERY"},
        )
        self.assertFalse(signal.in_holding_window)
        self.assertEqual(signal.target_weight, 0.0)

    def test_inside_window_uses_vol40_cap(self) -> None:
        signal = build_strategy_signal(
            history(),
            {"cycle_progress": 0.70, "phase_label": "PRE_HALVING_ACCUMULATION"},
        )
        self.assertTrue(signal.in_holding_window)
        self.assertAlmostEqual(
            signal.target_weight,
            min(1.0, 0.40 / signal.realized_volatility),
        )

    def test_one_shot_entry_from_zero(self) -> None:
        plan = plan_rebalance(
            actual_weight=0.0,
            target_weight=0.80,
            equity_krw=5_000_000,
            current_btc_value_krw=0.0,
            current_btc_quantity=0.0,
        )
        self.assertTrue(plan.should_trade)
        self.assertEqual(plan.action, "신규매수")
        self.assertEqual(plan.adjustment_krw, 4_000_000)

    def test_deadband_blocks_small_adjustment(self) -> None:
        plan = plan_rebalance(
            actual_weight=0.64,
            target_weight=0.70,
            equity_krw=5_000_000,
            current_btc_value_krw=3_200_000,
            current_btc_quantity=0.05,
        )
        self.assertFalse(plan.should_trade)
        self.assertEqual(plan.action, "보유")

    def test_zero_target_forces_final_exit(self) -> None:
        plan = plan_rebalance(
            actual_weight=0.05,
            target_weight=0.0,
            equity_krw=5_000_000,
            current_btc_value_krw=250_000,
            current_btc_quantity=0.003,
        )
        self.assertTrue(plan.should_trade)
        self.assertEqual(plan.action, "전량매도")


class BtcV07NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = history()
        self.halving = {
            "cycle_progress": 0.5752,
            "phase_label": "CONTRACTION_RECOVERY",
        }

    def test_initial_run_notifies_even_on_tuesday(self) -> None:
        now = datetime(2026, 8, 11, 0, 17, tzinfo=UTC)
        state = _initial_state(DEFAULT_BUDGET_KRW, now)
        _, payload = _normal_run(
            state=state,
            initial=True,
            now_utc=now,
            upbit_history=self.frame,
            halving=self.halving,
            mark_price=90_000_000,
            force_notify=False,
        )
        self.assertTrue(payload["should_notify"])
        self.assertEqual(payload["notification_reason"], "initial")
        self.assertEqual(payload["plan"]["action"], "현금대기")

    def test_regular_tuesday_is_silent(self) -> None:
        now = datetime(2026, 8, 11, 0, 17, tzinfo=UTC)
        state = _initial_state(DEFAULT_BUDGET_KRW, now)
        state["weekly_signal"] = StrategySignal(
            decision_date="2026-08-09",
            cycle_progress=0.5752,
            phase_label="CONTRACTION_RECOVERY",
            in_holding_window=False,
            realized_volatility=0.40,
            volatility_cap=1.0,
            target_weight=0.0,
        ).__dict__
        state["last_data_status"] = "ok"
        _, payload = _normal_run(
            state=state,
            initial=False,
            now_utc=now,
            upbit_history=self.frame,
            halving=self.halving,
            mark_price=90_000_000,
            force_notify=False,
        )
        self.assertFalse(payload["should_notify"])
        self.assertEqual(payload["notification_reason"], "none")

    def test_monday_always_sends_weekly_judgment(self) -> None:
        now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        state = _initial_state(DEFAULT_BUDGET_KRW, now)
        state["last_data_status"] = "ok"
        _, payload = _normal_run(
            state=state,
            initial=False,
            now_utc=now,
            upbit_history=self.frame,
            halving=self.halving,
            mark_price=90_000_000,
            force_notify=False,
        )
        self.assertTrue(payload["should_notify"])
        self.assertEqual(payload["notification_reason"], "weekly")


if __name__ == "__main__":
    unittest.main()
