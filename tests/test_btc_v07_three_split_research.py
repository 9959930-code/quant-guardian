from __future__ import annotations

import unittest

import pandas as pd

from btc_v07_three_split_research import simulate_three_split


def simple_features(days: int = 35, price: float = 100.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    return pd.DataFrame({"open": price, "close": price}, index=index)


def weekly_decisions(features: pd.DataFrame, targets: list[float]) -> pd.DataFrame:
    decisions = pd.DataFrame(index=features.index)
    decisions["desired_weight"] = float("nan")
    decisions["is_decision_day"] = False
    decisions["reason"] = "대기"
    sundays = [index for index in features.index if index.dayofweek == 6]
    for index, target in zip(sundays, targets):
        decisions.loc[index, "desired_weight"] = target
        decisions.loc[index, "is_decision_day"] = True
        decisions.loc[index, "reason"] = "테스트"
    return decisions


class ThreeSplitSimulatorTests(unittest.TestCase):
    def test_entry_three_parts_reaches_target_in_three_weeks(self) -> None:
        features = simple_features()
        decisions = weekly_decisions(features, [0.90, 0.90, 0.90, 0.90])
        result = simulate_three_split(
            features,
            decisions,
            entry_parts=3,
            exit_parts=1,
            rebalance_deadband=0.10,
            fee_bps=0,
            slippage_bps=0,
            initial_capital=1_000_000,
        )
        self.assertEqual(list(result.trades["step_number"][:3]), [1, 2, 3])
        self.assertEqual(list(result.trades["total_parts"][:3]), [3, 3, 3])
        self.assertAlmostEqual(float(result.trades.iloc[0]["step_target"]), 0.30)
        self.assertAlmostEqual(float(result.trades.iloc[1]["step_target"]), 0.60)
        self.assertAlmostEqual(float(result.trades.iloc[2]["step_target"]), 0.90)

    def test_exit_three_parts_reaches_zero_in_three_weeks(self) -> None:
        features = simple_features(days=56)
        decisions = weekly_decisions(
            features,
            [0.90, 0.90, 0.90, 0.90, 0.0, 0.0, 0.0, 0.0],
        )
        result = simulate_three_split(
            features,
            decisions,
            entry_parts=1,
            exit_parts=3,
            rebalance_deadband=0.10,
            fee_bps=0,
            slippage_bps=0,
            initial_capital=1_000_000,
        )
        sells = result.trades.loc[result.trades["side"] == "SELL"]
        self.assertEqual(list(sells["step_number"][:3]), [1, 2, 3])
        self.assertAlmostEqual(float(sells.iloc[0]["step_target"]), 0.60)
        self.assertAlmostEqual(float(sells.iloc[1]["step_target"]), 0.30)
        self.assertAlmostEqual(float(sells.iloc[2]["step_target"]), 0.0)

    def test_material_target_change_restarts_plan(self) -> None:
        features = simple_features(days=35)
        decisions = weekly_decisions(features, [0.90, 0.50, 0.50, 0.50])
        result = simulate_three_split(
            features,
            decisions,
            entry_parts=3,
            exit_parts=3,
            rebalance_deadband=0.10,
            fee_bps=0,
            slippage_bps=0,
            initial_capital=1_000_000,
        )
        self.assertTrue(bool(result.trades.iloc[1]["plan_restarted"]))
        self.assertAlmostEqual(float(result.trades.iloc[1]["final_target"]), 0.50)

    def test_small_target_change_does_not_restart(self) -> None:
        features = simple_features(days=35)
        decisions = weekly_decisions(features, [0.90, 0.85, 0.85, 0.85])
        result = simulate_three_split(
            features,
            decisions,
            entry_parts=3,
            exit_parts=3,
            rebalance_deadband=0.10,
            fee_bps=0,
            slippage_bps=0,
            initial_capital=1_000_000,
        )
        self.assertFalse(bool(result.trades.iloc[1]["plan_restarted"]))
        self.assertAlmostEqual(float(result.trades.iloc[1]["final_target"]), 0.90)

    def test_split_does_not_create_negative_cash_or_units(self) -> None:
        features = simple_features(days=56, price=123.45)
        decisions = weekly_decisions(
            features,
            [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        )
        result = simulate_three_split(
            features,
            decisions,
            entry_parts=3,
            exit_parts=3,
            rebalance_deadband=0.10,
            fee_bps=5,
            slippage_bps=10,
            initial_capital=1_000_000,
        )
        self.assertTrue((result.daily["cash"] >= 0).all())
        self.assertTrue((result.daily["btc_units"] >= 0).all())


if __name__ == "__main__":
    unittest.main()
