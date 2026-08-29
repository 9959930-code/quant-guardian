from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_engine import Candidate, FeatureStore, build_candidate_grid, candidate_id, effective_weight_panel, generate_targets, simulate_next_open
from equity_v2_research import add_selection_scores, select_research_winners


def synthetic_frames(days: int = 900) -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2010-01-01", periods=days)
    base = np.linspace(100.0, 220.0, days)
    wave = np.sin(np.arange(days) / 15) * 4
    close = base + wave
    frames: dict[str, pd.DataFrame] = {}
    multipliers = {"SPY": 1.0, "QQQ": 1.2, "XLK": 1.3, "SOXX": 1.4, "SMH": 1.45, "QLD": 1.7, "TQQQ": 2.1, "GLD": 0.8, "IEF": 0.6, "TLT": 0.7, "CASH": 0.01, "KRW=X": 0.2}
    for ticker, multiplier in multipliers.items():
        series = 100 + (close - 100) * multiplier
        if ticker == "CASH":
            series = 100 * np.cumprod(np.full(days, 1.00005))
        if ticker == "KRW=X":
            series = 1100 + np.linspace(0, 100, days)
        frames[ticker] = pd.DataFrame({"Open": pd.Series(series, index=index).shift(1).fillna(series[0]), "Close": series}, index=index)
    return frames


class CandidateGridTests(unittest.TestCase):
    def test_grid_is_large_and_contains_leverage(self) -> None:
        candidates = build_candidate_grid()
        self.assertGreater(len(candidates), 9000)
        ids = {candidate.candidate_id for candidate in candidates}
        self.assertEqual(len(ids), len(candidates))
        self.assertTrue(any(candidate.family == "leveraged_regime" and candidate.params.get("leverage_asset") == "TQQQ" for candidate in candidates))

    def test_fixed_mix_none_creates_one_target(self) -> None:
        frames = synthetic_frames()
        close = pd.concat({ticker: frame["Close"] for ticker, frame in frames.items()}, axis=1)
        store = FeatureStore(close)
        candidate = Candidate(candidate_id("fixed_mix", mix={"SPY": 0.5, "QQQ": 0.5}, frequency="none"), "fixed_mix", "core_20y", {"mix": {"SPY": 0.5, "QQQ": 0.5}, "frequency": "none"})
        targets = generate_targets(candidate, frames=frames, store=store, start=close.index[300], end=close.index[-1])
        self.assertEqual(len(targets), 1)
        self.assertAlmostEqual(float(targets.iloc[0].sum()), 1.0)


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frames = synthetic_frames()
        self.close = pd.concat({ticker: frame["Close"] for ticker, frame in self.frames.items()}, axis=1)
        self.store = FeatureStore(self.close)

    def test_future_price_change_does_not_change_past_trend_targets(self) -> None:
        candidate = Candidate("trend", "trend_hold", "core_20y", {"asset": "QQQ", "signal_mode": "self", "sma": 200, "slope_days": 20, "frequency": "weekly", "defense": "CASH"})
        end = self.close.index[700]
        original = generate_targets(candidate, frames=self.frames, store=self.store, start=self.close.index[250], end=end)
        modified_close = self.close.copy()
        modified_close.loc[modified_close.index > end, "QQQ"] *= 5
        modified = generate_targets(candidate, frames=self.frames, store=FeatureStore(modified_close), start=self.close.index[250], end=end)
        pd.testing.assert_frame_equal(original, modified)

    def test_rotation_leverage_cap_spills_to_qqq(self) -> None:
        candidate = Candidate("rotation", "rotation", "tqqq_actual", {"universe": ("TQQQ",), "lookbacks": (63,), "top_n": 1, "filter": "none", "score_mode": "raw", "frequency": "monthly", "defense": "CASH", "leverage_cap": 0.25})
        targets = generate_targets(candidate, frames=self.frames, store=self.store, start=self.close.index[300], end=self.close.index[-1])
        latest = targets.iloc[-1]
        self.assertAlmostEqual(float(latest["TQQQ"]), 0.25)
        self.assertAlmostEqual(float(latest["QQQ"]), 0.75)


class ExecutionTests(unittest.TestCase):
    def test_signal_executes_on_next_open(self) -> None:
        frames = synthetic_frames(40)
        index = frames["SPY"].index
        signal_date = index[10]
        targets = pd.DataFrame({"SPY": [1.0], "CASH": [0.0]}, index=[signal_date])
        simulation = simulate_next_open(targets=targets, frames=frames, start=index[0], end=index[-1], cost_bps=0, slippage_bps=0)
        self.assertFalse(simulation.trades.empty)
        self.assertEqual(pd.Timestamp(simulation.trades.iloc[0]["date"]), index[11])

    def test_effective_weights_are_shifted_one_day(self) -> None:
        frames = synthetic_frames(20)
        index = frames["SPY"].index
        targets = pd.DataFrame({"SPY": [1.0], "CASH": [0.0]}, index=[index[5]])
        panel = effective_weight_panel(targets, index=index, assets=["SPY", "CASH"], shift_days=1)
        self.assertEqual(float(panel.loc[index[5], "SPY"]), 0.0)
        self.assertEqual(float(panel.loc[index[6], "SPY"]), 1.0)


class SelectionTests(unittest.TestCase):
    def test_selection_uses_discovery_and_validation_not_holdout(self) -> None:
        frame = pd.DataFrame([
            {"candidate_id": "a", "family": "trend_hold", "track": "core_20y", "params_json": "{}", "status": "ok", "discovery_cagr": 0.20, "validation_cagr": 0.18, "holdout_cagr": -0.50, "discovery_mdd": -0.20, "validation_mdd": -0.20, "full_mdd": -0.25, "full_trades_per_year": 1.0, "full_leveraged_exposure": 0.0},
            {"candidate_id": "b", "family": "trend_hold", "track": "core_20y", "params_json": "{}", "status": "ok", "discovery_cagr": 0.10, "validation_cagr": 0.10, "holdout_cagr": 2.00, "discovery_mdd": -0.20, "validation_mdd": -0.20, "full_mdd": -0.25, "full_trades_per_year": 1.0, "full_leveraged_exposure": 0.0},
        ])
        scored = add_selection_scores(frame)
        winners = select_research_winners(scored)
        absolute = winners.loc[winners["category"] == "absolute_return"].iloc[0]
        self.assertEqual(absolute["candidate_id"], "a")


if __name__ == "__main__":
    unittest.main()
