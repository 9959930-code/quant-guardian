from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_return_models import Candidate, add_return_features, candidate_grid, generate_return_decisions


def features(days: int = 42) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    frame = pd.DataFrame(index=index)
    frame["momentum_feature_ready"] = True
    frame["phase_label"] = "POST_HALVING_EXPANSION"
    frame["cycle_progress"] = 0.25
    frame["halving_epoch"] = 4
    frame["realized_volatility"] = 0.50
    frame["close"] = 100.0
    frame["wma40"] = 90.0
    frame["sma200"] = 85.0
    frame["sma200_slope_60d"] = 0.05
    frame["return_180d"] = 0.20
    frame["trend_wma40"] = True
    frame["trend_two_of_three"] = True
    return frame


class ReturnOptimizationTests(unittest.TestCase):
    def test_grid_is_large_and_unique(self) -> None:
        rows = candidate_grid()
        self.assertGreater(len(rows), 2500)
        self.assertEqual(len(rows), len({row.candidate_id for row in rows}))

    def test_return_feature_uses_past_wma_only(self) -> None:
        index = pd.date_range("2023-01-01", periods=100, freq="D")
        frame = pd.DataFrame(index=index)
        frame["close"] = np.arange(100.0, 200.0)
        frame["wma40"] = np.arange(80.0, 180.0)
        frame["sma200"] = 100.0
        frame["sma200_slope_60d"] = 0.01
        frame["return_180d"] = 0.10
        out = add_return_features(frame)
        date = index[70]
        expected = frame.loc[date, "wma40"] / frame.iloc[42]["wma40"] - 1
        self.assertAlmostEqual(float(out.loc[date, "wma40_slope_28d"]), float(expected))

    def test_window_volatility_cap(self) -> None:
        frame = features()
        candidate = Candidate(
            "test", "window", 0.70, None, 0.40, 0.50, 0.10, "target_change",
            exposure_mode="vol",
        )
        result = generate_return_decisions(frame, candidate)
        sunday = result.index[result.index.dayofweek == 6][0]
        self.assertEqual(float(result.loc[sunday, "desired_weight"]), 1.0)

    def test_asymmetric_keeps_full_weight_in_uptrend(self) -> None:
        frame = features()
        frame["realized_volatility"] = 1.0
        frame["cycle_progress"] = 0.75
        candidate = Candidate(
            "test", "trend_exit", 0.70, 0.20, 0.45, 0.50, 0.10,
            "target_change", "wma40", 1, "asym", 0.25,
        )
        result = generate_return_decisions(frame, candidate)
        sunday = result.index[result.index.dayofweek == 6][0]
        self.assertEqual(float(result.loc[sunday, "desired_weight"]), 1.0)

    def test_trend_exit_reduces_after_confirmation(self) -> None:
        frame = features()
        frame.loc[: pd.Timestamp("2024-01-14"), "cycle_progress"] = 0.75
        frame.loc[pd.Timestamp("2024-01-15") :, "cycle_progress"] = 0.25
        frame.loc[pd.Timestamp("2024-01-15") :, "trend_wma40"] = False
        frame.loc[pd.Timestamp("2024-01-15") :, "trend_two_of_three"] = False
        candidate = Candidate(
            "test", "trend_exit", 0.70, 0.20, 0.45, None, 0.10,
            "target_change", "wma40", 2, "full", 0.0,
        )
        result = generate_return_decisions(frame, candidate)
        sundays = result.loc[result["is_decision_day"]]
        self.assertEqual(float(sundays.iloc[-1]["desired_weight"]), 0.0)

    def test_missing_halving_context_blocks_decision(self) -> None:
        frame = features()
        frame["phase_label"] = "UNKNOWN"
        candidate = Candidate(
            "test", "window", 0.70, None, 0.40, None, 0.10, "target_change"
        )
        result = generate_return_decisions(frame, candidate)
        self.assertFalse(bool(result["is_decision_day"].any()))


if __name__ == "__main__":
    unittest.main()
