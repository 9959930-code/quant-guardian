from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_halving_research import (
    CycleWindowCandidate,
    HalvingCandidate,
    HalvingOverlayParameters,
    candidate_specs,
    generate_cycle_window_decisions,
    generate_halving_overlay_decisions,
    simulate_weekly_strategy,
)


def overlay_features(days: int = 21) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    frame = pd.DataFrame(index=index)
    frame["open"] = 100.0
    frame["close"] = 100.0
    frame["momentum_feature_ready"] = True
    frame["realized_volatility"] = 0.40
    frame["phase_label"] = "POST_HALVING_EXPANSION"
    frame["cycle_progress"] = 0.20
    for horizon in (30, 90, 180, 365):
        frame[f"momentum_{horizon}d"] = 0.10
    frame["drawdown_365"] = -0.20
    frame["mvrv_percentile"] = 0.50
    frame["price_realized_ratio"] = 1.50
    frame["weekly_rsi14"] = 60.0
    frame["close_sma200_ratio"] = 1.10
    frame["return_180d"] = 0.20
    return frame


def candidate(
    overlay_mode: str,
    *,
    target_volatility: float = 0.40,
    policy: str = "actual_weight",
) -> HalvingCandidate:
    return HalvingCandidate(
        candidate_id=f"test_{overlay_mode}",
        overlay_mode=overlay_mode,
        parameters=HalvingOverlayParameters(
            target_annual_volatility=target_volatility
        ),
        rebalance_policy=policy,
        halving_required=overlay_mode != "none",
        selection_eligible=overlay_mode != "none",
        research_origin="unit-test",
        description="unit-test",
    )


class HalvingOverlayDecisionTests(unittest.TestCase):
    def test_phase_cap_limits_late_cycle_weight(self) -> None:
        features = overlay_features()
        features["phase_label"] = "LATE_EXPANSION_DISTRIBUTION"
        features["cycle_progress"] = 0.40

        decisions = generate_halving_overlay_decisions(
            features, candidate("phase_cap", target_volatility=0.45)
        )
        sunday = pd.Timestamp("2024-01-07")

        self.assertTrue(bool(decisions.loc[sunday, "is_decision_day"]))
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "base_weight"]), 1.0
        )
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "phase_cap"]), 0.75
        )
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "desired_weight"]), 0.75
        )

    def test_confirmation_requires_all_four_for_high_late_weight(self) -> None:
        features = overlay_features()
        features["phase_label"] = "LATE_EXPANSION_DISTRIBUTION"
        features["cycle_progress"] = 0.40
        features["momentum_365d"] = -0.01

        decisions = generate_halving_overlay_decisions(
            features, candidate("confirmation")
        )
        sunday = pd.Timestamp("2024-01-07")

        self.assertEqual(
            int(decisions.loc[sunday, "positive_momentum_count"]), 3
        )
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "desired_weight"]), 0.50
        )

    def test_cycle_value_can_open_small_pre_halving_floor(self) -> None:
        features = overlay_features()
        features["phase_label"] = "PRE_HALVING_ACCUMULATION"
        features["cycle_progress"] = 0.85
        for horizon in (30, 90, 180, 365):
            features[f"momentum_{horizon}d"] = -0.10
        features["drawdown_365"] = -0.55
        features["mvrv_percentile"] = 0.20
        features["price_realized_ratio"] = 1.00

        decisions = generate_halving_overlay_decisions(
            features, candidate("cycle_value")
        )
        sunday = pd.Timestamp("2024-01-07")

        self.assertTrue(bool(decisions.loc[sunday, "value_floor_active"]))
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "desired_weight"]), 0.25
        )

    def test_cycle_value_reduces_late_cycle_overheat(self) -> None:
        features = overlay_features()
        features["phase_label"] = "LATE_EXPANSION_DISTRIBUTION"
        features["cycle_progress"] = 0.40
        features["mvrv_percentile"] = 0.95
        features["weekly_rsi14"] = 80.0

        decisions = generate_halving_overlay_decisions(
            features, candidate("cycle_value", target_volatility=0.45)
        )
        sunday = pd.Timestamp("2024-01-07")

        self.assertGreaterEqual(
            int(decisions.loc[sunday, "overheat_domains"]), 2
        )
        self.assertAlmostEqual(
            float(decisions.loc[sunday, "desired_weight"]), 0.50
        )

    def test_missing_halving_context_blocks_deployable_decision(self) -> None:
        features = overlay_features()
        features["phase_label"] = "UNKNOWN"
        features["cycle_progress"] = np.nan

        decisions = generate_halving_overlay_decisions(
            features, candidate("phase_cap")
        )

        self.assertFalse(bool(decisions["is_decision_day"].any()))

    def test_every_selection_candidate_requires_halving(self) -> None:
        specs = candidate_specs()
        deployable = [item for item in specs if item.selection_eligible]

        self.assertTrue(deployable)
        self.assertTrue(all(item.halving_required for item in deployable))
        self.assertTrue(
            any(not item.halving_required for item in specs),
            "v0.5 must remain as a non-halving benchmark",
        )


class RebalancePolicyTests(unittest.TestCase):
    @staticmethod
    def drifting_market() -> tuple[pd.DataFrame, pd.DataFrame]:
        index = pd.date_range("2024-01-01", periods=15, freq="D")
        features = pd.DataFrame(index=index)
        features["open"] = 100.0
        features["close"] = 100.0
        features.loc[pd.Timestamp("2024-01-09") :, ["open", "close"]] = 200.0

        decisions = pd.DataFrame(index=index)
        decisions["is_decision_day"] = False
        decisions["desired_weight"] = np.nan
        decisions["reason"] = "hold"
        for day in (pd.Timestamp("2024-01-07"), pd.Timestamp("2024-01-14")):
            decisions.loc[day, "is_decision_day"] = True
            decisions.loc[day, "desired_weight"] = 0.50
            decisions.loc[day, "reason"] = "weekly target"
        return features, decisions

    def test_actual_weight_policy_corrects_price_drift(self) -> None:
        features, decisions = self.drifting_market()

        result = simulate_weekly_strategy(
            features,
            decisions,
            rebalance_policy="actual_weight",
            rebalance_deadband=0.10,
            fee_bps=0.0,
            slippage_bps=0.0,
            initial_capital=1_000.0,
        )

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades.iloc[0]["side"], "BUY")
        self.assertEqual(result.trades.iloc[1]["side"], "SELL")
        self.assertAlmostEqual(
            float(result.daily.iloc[-1]["actual_weight"]), 0.50, places=6
        )

    def test_previous_target_policy_does_not_correct_same_target_drift(self) -> None:
        features, decisions = self.drifting_market()

        result = simulate_weekly_strategy(
            features,
            decisions,
            rebalance_policy="target_change",
            rebalance_deadband=0.10,
            fee_bps=0.0,
            slippage_bps=0.0,
            initial_capital=1_000.0,
        )

        self.assertEqual(len(result.trades), 1)
        self.assertGreater(
            float(result.daily.iloc[-1]["actual_weight"]), 0.60
        )


class CycleWindowTests(unittest.TestCase):
    def test_window_wraps_across_halving_boundary(self) -> None:
        features = overlay_features()
        features.loc[
            pd.Timestamp("2024-01-01") : pd.Timestamp("2024-01-07"),
            "cycle_progress",
        ] = 0.85
        features.loc[
            pd.Timestamp("2024-01-08") : pd.Timestamp("2024-01-14"),
            "cycle_progress",
        ] = 0.10
        features.loc[pd.Timestamp("2024-01-15") :, "cycle_progress"] = 0.50
        spec = CycleWindowCandidate(
            candidate_id="window",
            pre_halving_start_progress=0.80,
            post_halving_end_progress=0.30,
            target_annual_volatility=None,
        )

        decisions = generate_cycle_window_decisions(features, spec)

        self.assertAlmostEqual(
            float(decisions.loc[pd.Timestamp("2024-01-07"), "desired_weight"]),
            1.0,
        )
        self.assertAlmostEqual(
            float(decisions.loc[pd.Timestamp("2024-01-14"), "desired_weight"]),
            1.0,
        )
        self.assertAlmostEqual(
            float(decisions.loc[pd.Timestamp("2024-01-21"), "desired_weight"]),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
