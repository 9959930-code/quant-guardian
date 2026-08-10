from __future__ import annotations

import unittest
from datetime import UTC, datetime

import numpy as np
import pandas as pd

import btc_v07_advisory as base
from btc_v07_split_advisory import (
    STRATEGY_VERSION,
    _initial_state,
    _normal_run,
    plan_three_split_step,
)


def history(days: int = 500) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    returns = np.resize(np.array([0.01, -0.005, 0.002, -0.001]), days)
    close = 100.0 * np.cumprod(1 + returns)
    return pd.DataFrame({"Close": close}, index=index)


class ThreeSplitPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 10, 0, 17, tzinfo=UTC)
        self.state = _initial_state(1_000_000, self.now)

    def test_state_uses_new_strategy_version_and_empty_plan(self) -> None:
        self.assertEqual(self.state["strategy_version"], STRATEGY_VERSION)
        self.assertEqual(self.state["schema_version"], 2)
        self.assertIsNone(self.state["split_plan"])

    def test_entry_reaches_target_in_three_weekly_steps(self) -> None:
        step1 = plan_three_split_step(
            self.state,
            actual_weight=0.0,
            final_target_weight=0.90,
            equity_krw=1_000_000,
            current_btc_value_krw=0.0,
            current_btc_quantity=0.0,
            signal_date="2026-01-04",
        )
        self.assertAlmostEqual(step1.step_target_weight, 0.30)
        self.assertEqual(step1.step_number, 1)
        self.assertTrue(step1.plan_active_after)

        step2 = plan_three_split_step(
            self.state,
            actual_weight=0.30,
            final_target_weight=0.90,
            equity_krw=1_000_000,
            current_btc_value_krw=300_000,
            current_btc_quantity=0.01,
            signal_date="2026-01-11",
        )
        self.assertAlmostEqual(step2.step_target_weight, 0.60)
        self.assertEqual(step2.step_number, 2)

        step3 = plan_three_split_step(
            self.state,
            actual_weight=0.60,
            final_target_weight=0.90,
            equity_krw=1_000_000,
            current_btc_value_krw=600_000,
            current_btc_quantity=0.02,
            signal_date="2026-01-18",
        )
        self.assertAlmostEqual(step3.step_target_weight, 0.90)
        self.assertEqual(step3.step_number, 3)
        self.assertFalse(step3.plan_active_after)
        self.assertIsNone(self.state["split_plan"])

    def test_repeated_zero_target_continues_three_week_exit(self) -> None:
        step1 = plan_three_split_step(
            self.state,
            actual_weight=0.90,
            final_target_weight=0.0,
            equity_krw=1_000_000,
            current_btc_value_krw=900_000,
            current_btc_quantity=0.01,
            signal_date="2026-06-07",
        )
        self.assertAlmostEqual(step1.step_target_weight, 0.60)
        self.assertEqual(step1.step_number, 1)

        step2 = plan_three_split_step(
            self.state,
            actual_weight=0.60,
            final_target_weight=0.0,
            equity_krw=1_000_000,
            current_btc_value_krw=600_000,
            current_btc_quantity=0.006,
            signal_date="2026-06-14",
        )
        self.assertAlmostEqual(step2.step_target_weight, 0.30)
        self.assertEqual(step2.step_number, 2)
        self.assertFalse(step2.plan_restarted)

        step3 = plan_three_split_step(
            self.state,
            actual_weight=0.30,
            final_target_weight=0.0,
            equity_krw=1_000_000,
            current_btc_value_krw=300_000,
            current_btc_quantity=0.003,
            signal_date="2026-06-21",
        )
        self.assertAlmostEqual(step3.step_target_weight, 0.0)
        self.assertEqual(step3.step_number, 3)
        self.assertEqual(step3.action, "3차·최종 전량매도")
        self.assertIsNone(self.state["split_plan"])

    def test_small_target_change_keeps_frozen_plan(self) -> None:
        first = plan_three_split_step(
            self.state,
            actual_weight=0.0,
            final_target_weight=0.90,
            equity_krw=1_000_000,
            current_btc_value_krw=0.0,
            current_btc_quantity=0.0,
            signal_date="2026-01-04",
        )
        self.assertTrue(first.plan_restarted)
        second = plan_three_split_step(
            self.state,
            actual_weight=0.30,
            final_target_weight=0.85,
            equity_krw=1_000_000,
            current_btc_value_krw=300_000,
            current_btc_quantity=0.01,
            signal_date="2026-01-11",
        )
        self.assertFalse(second.plan_restarted)
        self.assertAlmostEqual(second.final_target_weight, 0.90)
        self.assertAlmostEqual(second.step_target_weight, 0.60)

    def test_material_target_change_restarts_from_current_weight(self) -> None:
        plan_three_split_step(
            self.state,
            actual_weight=0.0,
            final_target_weight=0.90,
            equity_krw=1_000_000,
            current_btc_value_krw=0.0,
            current_btc_quantity=0.0,
            signal_date="2026-01-04",
        )
        restarted = plan_three_split_step(
            self.state,
            actual_weight=0.30,
            final_target_weight=0.50,
            equity_krw=1_000_000,
            current_btc_value_krw=300_000,
            current_btc_quantity=0.01,
            signal_date="2026-01-11",
        )
        self.assertTrue(restarted.plan_restarted)
        self.assertAlmostEqual(restarted.final_target_weight, 0.50)
        self.assertAlmostEqual(
            restarted.step_target_weight,
            0.30 + (0.50 - 0.30) / 3,
        )


class ThreeSplitNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = history()
        self.halving = {
            "cycle_progress": 0.5752,
            "phase_label": "CONTRACTION_RECOVERY",
        }

    def test_initial_tuesday_registers_without_off_schedule_trade(self) -> None:
        now = datetime(2026, 8, 11, 0, 17, tzinfo=UTC)
        state = _initial_state(base.DEFAULT_BUDGET_KRW, now)
        state, payload = _normal_run(
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
        self.assertFalse(payload["plan"]["should_trade"])
        self.assertEqual(payload["plan"]["action"], "현금대기")
        self.assertIsNone(state["split_plan"])

    def test_monday_in_holding_window_starts_first_split(self) -> None:
        now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        state = _initial_state(base.DEFAULT_BUDGET_KRW, now)
        state["last_data_status"] = "ok"
        state, payload = _normal_run(
            state=state,
            initial=False,
            now_utc=now,
            upbit_history=self.frame,
            halving={
                "cycle_progress": 0.70,
                "phase_label": "PRE_HALVING_ACCUMULATION",
            },
            mark_price=90_000_000,
            force_notify=False,
        )
        self.assertTrue(payload["plan"]["should_trade"])
        self.assertEqual(payload["plan"]["step_number"], 1)
        self.assertEqual(payload["plan"]["total_parts"], 3)
        self.assertIn("1차", payload["plan"]["action"])
        self.assertIsNotNone(state["split_plan"])


if __name__ == "__main__":
    unittest.main()
