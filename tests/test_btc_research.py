from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_guardian import load_config
from btc_research import (
    ResearchParameters,
    build_feature_frame,
    candidate_specs,
    expanding_percentile,
    generate_state_targets,
    make_ma_trend_signals,
    parse_coinmetrics_history,
    simulate_monthly_dca,
    simulate_strategy,
    trim_to_research_window,
)


def synthetic_market(
    periods: int = 520,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    index = pd.date_range("2020-01-01", periods=periods, freq="D")
    trend = np.linspace(10_000_000, 30_000_000, periods)
    wave = np.sin(np.arange(periods) / 4) * 1_200_000
    close = pd.Series(trend + wave, index=index)
    upbit = pd.DataFrame(
        {
            "Open": close.shift(1).fillna(close.iloc[0]),
            "High": close * 1.02,
            "Low": close * 0.98,
            "Close": close,
            "Volume": np.linspace(100, 200, periods),
            "QuoteVolume": close * np.linspace(100, 200, periods),
        },
        index=index,
    )
    usd = pd.DataFrame({"Close": np.linspace(8_000, 24_000, periods)}, index=index)
    fx = pd.DataFrame({"Close": np.linspace(1_150, 1_250, periods)}, index=index)
    onchain = pd.DataFrame(
        {
            "BlkCnt": np.full(periods, 144.0),
            "CapMVRVCur": np.linspace(0.8, 2.4, periods),
            "HashRate": np.linspace(100, 200, periods),
            "FeeTotNtv": np.linspace(10, 20, periods),
            "IssTotNtv": np.full(periods, 900.0),
            "IssTotUSD": np.linspace(1_000_000, 3_000_000, periods),
            "PriceUSD": np.linspace(8_000, 24_000, periods),
            "CapMrktCurUSD": np.linspace(150e9, 450e9, periods),
            "SplyCur": np.linspace(18e6, 19e6, periods),
        },
        index=index,
    )
    return upbit, usd, fx, onchain


def neutral_feature_frame(periods: int = 20) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=periods, freq="D")
    return pd.DataFrame(
        {
            "phase_label": "PRE_HALVING_ACCUMULATION",
            "drawdown_365": 0.0,
            "close_sma200_ratio": 1.0,
            "rsi14": 50.0,
            "return_30d": 0.0,
            "mvrv_percentile": 0.5,
            "price_realized_ratio": 2.0,
            "return_180d": 0.0,
            "weekly_rsi14": 50.0,
            "kimchi_premium": 0.0,
            "kimchi_premium_percentile": 0.5,
            "close": 100.0,
            "sma50": 100.0,
            "sma200": 100.0,
            "sma200_slope_60d": 0.0,
            "wma40": 100.0,
            "ppo_hist": 0.0,
            "return_90d": 0.0,
            "feature_ready": True,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
        },
        index=index,
    )


class FeatureTests(unittest.TestCase):
    def test_expanding_percentile_excludes_current_observation(self) -> None:
        series = pd.Series([1.0, 100.0, 50.0])

        result = expanding_percentile(series, min_periods=2)

        self.assertTrue(pd.isna(result.iloc[0]))
        self.assertTrue(pd.isna(result.iloc[1]))
        self.assertEqual(result.iloc[2], 0.5)

    def test_feature_frame_delays_fx_and_onchain(self) -> None:
        upbit, usd, fx, onchain = synthetic_market()

        features = build_feature_frame(
            upbit,
            usd,
            fx,
            onchain,
            onchain_lag_days=2,
            percentile_min_periods=20,
        )

        self.assertTrue(pd.isna(features["CapMVRVCur"].iloc[0]))
        self.assertTrue(pd.isna(features["CapMVRVCur"].iloc[1]))
        self.assertEqual(features["CapMVRVCur"].iloc[2], onchain["CapMVRVCur"].iloc[0])
        self.assertTrue(pd.isna(features["usdkrw_close"].iloc[0]))
        self.assertEqual(features["usdkrw_close"].iloc[1], fx["Close"].iloc[0])
        self.assertGreater(int(features["feature_ready"].sum()), 250)

    def test_onchain_percentile_keeps_history_before_upbit_listing(self) -> None:
        upbit, usd, fx, onchain = synthetic_market()
        early_index = pd.date_range(
            end=upbit.index[0] - pd.Timedelta(days=1), periods=400
        )
        early = pd.DataFrame(index=early_index, columns=onchain.columns, dtype=float)
        for column in onchain.columns:
            early[column] = float(onchain[column].iloc[0])
        early["BlkCnt"] = 144.0
        extended_onchain = pd.concat([early, onchain])

        features = build_feature_frame(
            upbit,
            usd,
            fx,
            extended_onchain,
            onchain_lag_days=2,
            percentile_min_periods=365,
        )

        self.assertTrue(pd.notna(features["mvrv_percentile"].iloc[0]))

    def test_research_window_starts_at_first_ready_row(self) -> None:
        features = neutral_feature_frame(10)
        features.loc[features.index[:3], "feature_ready"] = False

        trimmed = trim_to_research_window(features)

        self.assertEqual(trimmed.index[0], features.index[3])
        self.assertEqual(len(trimmed), 7)

    def test_coinmetrics_parser_converts_strings(self) -> None:
        rows = [
            {
                "asset": "btc",
                "time": "2026-01-01T00:00:00Z",
                "BlkCnt": "144",
                "CapMVRVCur": "1.25",
            }
        ]

        frame = parse_coinmetrics_history(rows, ["BlkCnt", "CapMVRVCur"])

        self.assertEqual(frame["BlkCnt"].iloc[0], 144)
        self.assertEqual(frame["CapMVRVCur"].iloc[0], 1.25)


class StateMachineTests(unittest.TestCase):
    def test_halving_phase_alone_never_buys(self) -> None:
        features = neutral_feature_frame()

        signals = generate_state_targets(
            features, ResearchParameters(), use_halving=True
        )

        self.assertEqual(float(signals["target_weight"].max()), 0.0)

    def test_deep_value_signal_does_not_reduce_existing_weight(self) -> None:
        features = neutral_feature_frame(26)
        features.loc[features.index[:15], "close"] = 120.0
        features.loc[features.index[:15], "sma200"] = 100.0
        features.loc[features.index[:15], "sma50"] = 105.0
        features.loc[features.index[:15], "wma40"] = 100.0
        features.loc[features.index[:15], "sma200_slope_60d"] = 0.05
        features.loc[features.index[:15], "ppo_hist"] = 1.0
        deep = features.index[15:]
        features.loc[deep, "drawdown_365"] = -0.60
        features.loc[deep, "close_sma200_ratio"] = 0.75
        features.loc[deep, "rsi14"] = 20.0
        features.loc[deep, "return_30d"] = -0.20
        features.loc[deep, "return_90d"] = 0.0
        features.loc[deep, "price_realized_ratio"] = 0.9

        signals = generate_state_targets(
            features, ResearchParameters(), use_halving=True
        )

        self.assertEqual(signals["target_weight"].iloc[14], 1.0)
        self.assertEqual(float(signals.loc[deep, "target_weight"].min()), 1.0)

    def test_bear_market_cap_limits_reentry_after_staged_reduction(self) -> None:
        features = neutral_feature_frame(36)
        bullish = features.index[:20]
        features.loc[bullish, "close"] = 120.0
        features.loc[bullish, "sma50"] = 105.0
        features.loc[bullish, "sma200"] = 100.0
        features.loc[bullish, "wma40"] = 100.0
        features.loc[bullish, "sma200_slope_60d"] = 0.05
        features.loc[bullish, "ppo_hist"] = 1.0
        bearish = features.index[20:]
        features.loc[bearish, "close"] = 70.0
        features.loc[bearish, "sma50"] = 65.0
        features.loc[bearish, "sma200"] = 100.0
        features.loc[bearish, "wma40"] = 95.0
        features.loc[bearish, "sma200_slope_60d"] = -0.10
        features.loc[bearish, "ppo_hist"] = 1.0
        features.loc[bearish, "drawdown_365"] = -0.60
        features.loc[bearish, "close_sma200_ratio"] = 0.70
        features.loc[bearish, "rsi14"] = 25.0
        features.loc[bearish, "return_30d"] = -0.20
        features.loc[bearish, "price_realized_ratio"] = 0.90

        signals = generate_state_targets(
            features,
            ResearchParameters(),
            use_halving=True,
            bear_market_max_weight=0.25,
        )

        self.assertEqual(signals["target_weight"].iloc[19], 1.0)
        self.assertLessEqual(
            float(signals.loc[bearish[-5:], "target_weight"].max()), 0.25
        )
        self.assertTrue(bool(signals.loc[bearish[-1], "bear_regime"]))

    def test_signal_executes_on_following_open(self) -> None:
        features = neutral_feature_frame(6)
        signals = pd.DataFrame(
            {
                "target_weight": [0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                "state": [
                    "WAIT",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                ],
            },
            index=features.index,
        )

        simulation = simulate_strategy(features, signals, fee_bps=0, slippage_bps=0)

        self.assertEqual(len(simulation.trades), 1)
        self.assertEqual(
            pd.Timestamp(simulation.trades.iloc[0]["date"]), features.index[2]
        )
        self.assertEqual(
            simulation.trades.iloc[0]["signal_date"],
            features.index[1].date().isoformat(),
        )

    def test_ma_benchmark_requires_persistence_and_ignores_one_day_reversal(
        self,
    ) -> None:
        features = neutral_feature_frame(9)
        features["close"] = [100, 102, 102, 102, 102, 98, 102, 102, 102]

        signals = make_ma_trend_signals(
            features,
            "sma200",
            band=0.01,
            persistence_days=3,
        )

        self.assertEqual(signals["target_weight"].iloc[2], 0.0)
        self.assertEqual(signals["target_weight"].iloc[3], 1.0)
        self.assertEqual(signals["target_weight"].iloc[5], 1.0)

    def test_costs_reduce_terminal_wealth(self) -> None:
        features = neutral_feature_frame(6)
        features["close"] = [100, 100, 110, 120, 130, 140]
        features["open"] = [100, 100, 100, 110, 120, 130]
        signals = pd.DataFrame(
            {
                "target_weight": [0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                "state": [
                    "WAIT",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                    "FULL_HOLD",
                ],
            },
            index=features.index,
        )

        free = simulate_strategy(features, signals, fee_bps=0, slippage_bps=0)
        costly = simulate_strategy(features, signals, fee_bps=5, slippage_bps=10)

        self.assertLess(costly.daily["equity"].iloc[-1], free.daily["equity"].iloc[-1])

    def test_monthly_dca_tracks_external_contributions_and_costs(self) -> None:
        features = neutral_feature_frame(70)
        features["open"] = np.linspace(100, 150, len(features))
        features["close"] = features["open"] * 1.01

        free, free_contributed = simulate_monthly_dca(
            features,
            fee_bps=0,
            slippage_bps=0,
        )
        costly, costly_contributed = simulate_monthly_dca(
            features,
            fee_bps=5,
            slippage_bps=10,
        )

        self.assertEqual(free_contributed, 3.0)
        self.assertEqual(costly_contributed, free_contributed)
        self.assertEqual(len(free.trades), 3)
        self.assertAlmostEqual(float(free.daily["external_flow"].sum()), 3.0)
        self.assertLess(costly.daily["equity"].iloc[-1], free.daily["equity"].iloc[-1])

    def test_candidate_search_is_small_and_not_cartesian(self) -> None:
        specs = candidate_specs(load_config())

        self.assertEqual(len(specs), 9)
        self.assertEqual(len({spec.candidate_id for spec in specs}), 9)
        self.assertTrue(any(not spec.use_halving for spec in specs))
        challenger = next(
            spec for spec in specs if spec.candidate_id == "bear_reentry_core_cap"
        )
        self.assertEqual(challenger.bear_market_max_weight, 0.25)
        self.assertFalse(challenger.selection_eligible)
        self.assertTrue(challenger.research_origin.startswith("post-"))


if __name__ == "__main__":
    unittest.main()
