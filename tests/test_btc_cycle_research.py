from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_cycle_research import (
    CycleTrendParameters,
    build_synthetic_krw_market,
    cycle_candidate_specs,
    generate_cycle_trend_targets,
)


def cycle_features(
    *,
    days: int = 140,
    phase: str = "POST_HALVING_EXPANSION",
    bullish: bool = True,
    overheated: bool = False,
    deep_value: bool = False,
) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    wma40 = np.linspace(90.0, 105.0, days)
    close = wma40 * (1.10 if bullish else 0.85)
    if not bullish:
        wma40 = np.linspace(105.0, 90.0, days)
        close = wma40 * 0.85
    return pd.DataFrame(
        {
            "feature_ready": True,
            "close": close,
            "wma40": wma40,
            "phase_label": phase,
            "cycle_progress": 0.20,
            "mvrv_percentile": 0.95 if overheated else 0.10 if deep_value else 0.50,
            "weekly_rsi14": 80.0 if overheated else 55.0,
            "close_sma200_ratio": 2.0 if overheated else 1.10,
            "return_180d": 1.3 if overheated else 0.20,
            "drawdown_365": -0.55 if deep_value else -0.10,
        },
        index=index,
    )


class SyntheticKrwTests(unittest.TestCase):
    def test_uses_prior_labeled_fx_close(self) -> None:
        index = pd.date_range("2024-01-01", periods=4, freq="D")
        usd = pd.DataFrame(
            {
                "Open": [10.0, 20.0, 30.0, 40.0],
                "High": [11.0, 21.0, 31.0, 41.0],
                "Low": [9.0, 19.0, 29.0, 39.0],
                "Close": [10.5, 20.5, 30.5, 40.5],
                "Volume": [1.0, 2.0, 3.0, 4.0],
            },
            index=index,
        )
        fx = pd.DataFrame({"Close": [1_000.0, 1_100.0, 1_200.0, 1_300.0]}, index=index)

        result = build_synthetic_krw_market(usd, fx)

        self.assertNotIn(index[0], result.index)
        self.assertEqual(result.loc[index[1], "Close"], 20.5 * 1_000.0)
        self.assertEqual(result.loc[index[2], "Open"], 30.0 * 1_100.0)


class CycleTrendSignalTests(unittest.TestCase):
    def test_halving_phase_alone_does_not_buy(self) -> None:
        features = cycle_features(bullish=False)

        signals = generate_cycle_trend_targets(features)

        self.assertEqual(float(signals["target_weight"].max()), 0.0)

    def test_post_halving_uptrend_reaches_full_weight(self) -> None:
        features = cycle_features()

        signals = generate_cycle_trend_targets(features)

        self.assertEqual(float(signals["target_weight"].max()), 1.0)
        changed = signals["target_weight"].diff().fillna(0).ne(0)
        self.assertTrue(all(index.dayofweek == 6 for index in signals.index[changed]))

    def test_late_cycle_overheat_caps_weight_at_half(self) -> None:
        features = cycle_features(
            phase="LATE_EXPANSION_DISTRIBUTION",
            overheated=True,
        )

        signals = generate_cycle_trend_targets(features)

        self.assertEqual(float(signals["target_weight"].max()), 0.50)

    def test_pre_halving_deep_value_allows_only_seed_weight(self) -> None:
        features = cycle_features(
            phase="PRE_HALVING_ACCUMULATION",
            bullish=False,
            deep_value=True,
        )

        signals = generate_cycle_trend_targets(features)

        self.assertEqual(float(signals["target_weight"].max()), 0.20)

    def test_confirmation_variant_is_explicit_and_small(self) -> None:
        specs = cycle_candidate_specs()

        self.assertEqual(len(specs), 8)
        self.assertEqual(len({spec.candidate_id for spec in specs}), 8)
        primary = next(
            spec for spec in specs if spec.candidate_id == "cycle_trend_core_v04"
        )
        self.assertEqual(primary.parameters, CycleTrendParameters())

    def test_late_cycle_overheat_latch_blocks_immediate_reincrease(self) -> None:
        features = cycle_features(
            phase="LATE_EXPANSION_DISTRIBUTION",
            overheated=True,
        )
        features.loc[features.index[70] :, "mvrv_percentile"] = 0.50
        features.loc[features.index[70] :, "weekly_rsi14"] = 55.0
        features.loc[features.index[70] :, "close_sma200_ratio"] = 1.10
        features.loc[features.index[70] :, "return_180d"] = 0.20
        parameters = CycleTrendParameters(latch_late_cycle_overheat=True)

        signals = generate_cycle_trend_targets(features, parameters)

        self.assertEqual(float(signals["target_weight"].max()), 0.50)


if __name__ == "__main__":
    unittest.main()
