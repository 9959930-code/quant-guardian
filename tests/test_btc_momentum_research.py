from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_momentum_research import (
    MomentumVolatilityParameters,
    add_momentum_volatility_features,
    generate_momentum_volatility_targets,
    volatility_candidate_specs,
)


def signal_features(days: int = 14) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    frame = pd.DataFrame(index=index)
    frame["momentum_feature_ready"] = True
    frame["realized_volatility"] = 0.45
    for horizon in (30, 90, 180, 365):
        frame[f"momentum_{horizon}d"] = 0.10
    return frame


class MomentumFeatureTests(unittest.TestCase):
    def test_momentum_uses_only_past_close(self) -> None:
        index = pd.date_range("2023-01-01", periods=500, freq="D")
        close = pd.Series(np.arange(1.0, 501.0), index=index)
        features = pd.DataFrame(
            {"close": close},
            index=index,
        )

        result = add_momentum_volatility_features(features)
        date = index[400]

        self.assertAlmostEqual(
            float(result.loc[date, "momentum_30d"]),
            float(close.loc[date] / close.iloc[370] - 1),
        )
        self.assertTrue(bool(result.loc[date, "momentum_feature_ready"]))

    def test_invalid_parameters_are_rejected(self) -> None:
        features = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.date_range("2024-01-01", periods=2, freq="D"),
        )

        with self.assertRaises(ValueError):
            add_momentum_volatility_features(
                features,
                MomentumVolatilityParameters(target_annual_volatility=0.0),
            )


class MomentumSignalTests(unittest.TestCase):
    def test_all_positive_momentum_is_capped_by_volatility(self) -> None:
        features = signal_features()
        features["realized_volatility"] = 0.90

        signals = generate_momentum_volatility_targets(features)
        first_sunday = pd.Timestamp("2024-01-07")

        self.assertEqual(
            float(signals.loc[first_sunday, "momentum_weight"]), 1.0
        )
        self.assertEqual(
            float(signals.loc[first_sunday, "volatility_cap"]), 0.5
        )
        self.assertEqual(float(signals.loc[first_sunday, "target_weight"]), 0.5)

    def test_two_positive_horizons_produce_half_weight(self) -> None:
        features = signal_features()
        features["realized_volatility"] = 0.20
        features["momentum_180d"] = -0.01
        features["momentum_365d"] = -0.01

        signals = generate_momentum_volatility_targets(features)

        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-07"), "target_weight"]),
            0.5,
        )

    def test_all_negative_momentum_exits(self) -> None:
        features = signal_features()
        second_week = features.index >= pd.Timestamp("2024-01-08")
        for horizon in (30, 90, 180, 365):
            features.loc[second_week, f"momentum_{horizon}d"] = -0.10

        signals = generate_momentum_volatility_targets(features)

        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-07"), "target_weight"]),
            1.0,
        )
        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-14"), "target_weight"]),
            0.0,
        )

    def test_deadband_blocks_small_rebalance(self) -> None:
        features = signal_features()
        features.loc[: pd.Timestamp("2024-01-07"), "realized_volatility"] = 0.90
        features.loc[pd.Timestamp("2024-01-08") :, "realized_volatility"] = (
            0.45 / 0.55
        )

        signals = generate_momentum_volatility_targets(features)

        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-07"), "target_weight"]),
            0.5,
        )
        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-14"), "desired_weight"]),
            0.55,
        )
        self.assertEqual(
            float(signals.loc[pd.Timestamp("2024-01-14"), "target_weight"]),
            0.5,
        )
        self.assertFalse(
            bool(signals.loc[pd.Timestamp("2024-01-14"), "rebalanced"])
        )

    def test_weight_changes_only_on_sunday(self) -> None:
        signals = generate_momentum_volatility_targets(signal_features())
        changed = signals["target_weight"].diff().fillna(0).ne(0)

        self.assertTrue(
            all(index.dayofweek == 6 for index in signals.index[changed])
        )

    def test_signal_does_not_require_halving_or_onchain_columns(self) -> None:
        signals = generate_momentum_volatility_targets(signal_features())

        self.assertFalse(signals.empty)
        self.assertNotIn("phase_label", signals.columns)

    def test_candidate_list_is_explicit_and_primary_is_fixed(self) -> None:
        candidates = volatility_candidate_specs()

        self.assertEqual(len(candidates), 4)
        self.assertEqual(len({item.candidate_id for item in candidates}), 4)
        primary = next(item for item in candidates if item.primary_reference)
        self.assertEqual(primary.candidate_id, "momentum_vol45_v05")
        self.assertEqual(primary.parameters.target_annual_volatility, 0.45)


if __name__ == "__main__":
    unittest.main()
