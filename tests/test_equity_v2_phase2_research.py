from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_engine import FeatureStore, SimulationResult
from equity_v2_phase2_research import (
    Phase2Candidate,
    _selection_fields,
    blend_sleeves,
    build_phase2_grid,
    generate_phase2_targets,
    synthetic_leverage_frame,
)


def synthetic_close(days: int = 900) -> pd.DataFrame:
    index = pd.bdate_range("2010-01-01", periods=days)
    trend = np.linspace(100.0, 250.0, days)
    wave = np.sin(np.arange(days) / 12) * 6
    qqq = trend + wave
    tqqq = 100 + (qqq - 100) * 2.4
    cash = 100 * np.cumprod(np.full(days, 1.00005))
    return pd.DataFrame(
        {"QQQ": qqq, "TQQQ": tqqq, "CASH": cash}, index=index
    )


def fake_simulation(multiplier: float, exposure: float) -> SimulationResult:
    index = pd.bdate_range("2010-03-01", periods=800)
    equity = pd.Series(multiplier ** (np.arange(len(index)) / len(index)), index=index)
    daily = pd.DataFrame(
        {
            "equity": equity,
            "daily_return": equity.pct_change().fillna(0.0),
            "risk_weight": exposure,
            "leveraged_weight": exposure,
        },
        index=index,
    )
    return SimulationResult(daily=daily, trades=pd.DataFrame(), metrics={})


class Phase2GridTests(unittest.TestCase):
    def test_grid_is_large_and_has_both_kinds(self) -> None:
        candidates = build_phase2_grid()
        self.assertGreater(len(candidates), 10000)
        self.assertEqual(len(candidates), len({item.candidate_id for item in candidates}))
        self.assertEqual({item.kind for item in candidates}, {"aggressive", "balanced"})


class Phase2SignalTests(unittest.TestCase):
    def test_balanced_split_entry_reaches_final_weight(self) -> None:
        close = synthetic_close()
        store = FeatureStore(close)
        candidate = Phase2Candidate(
            "balanced-test",
            "balanced",
            {
                "asset": "TQQQ",
                "breakout_days": 10,
                "long_ma": 50,
                "exit_ma": 50,
                "frequency": "weekly",
                "parts": 3,
                "tqqq_weight": 0.75,
                "remainder": "QQQ",
                "entry_confirm": 1,
                "exit_confirm": 1,
            },
        )
        targets = generate_phase2_targets(
            candidate, store=store, start=close.index[100], end=close.index[-1]
        )
        self.assertFalse(targets.empty)
        self.assertTrue((targets.get("TQQQ", 0) <= 0.75 + 1e-9).all())
        self.assertTrue((targets.get("QQQ", 0) <= 0.25 + 1e-9).all())
        self.assertTrue(
            ((targets.get("TQQQ", 0) - 0.75).abs() < 1e-8).any()
        )

    def test_synthetic_leverage_contains_severe_drawdown(self) -> None:
        index = pd.bdate_range("2007-01-01", periods=700)
        prices = np.concatenate(
            [np.linspace(100, 140, 200), np.linspace(140, 55, 250), np.linspace(55, 120, 250)]
        )
        qqq = pd.DataFrame(
            {
                "Open": pd.Series(prices, index=index).shift(1).fillna(prices[0]),
                "Close": prices,
            },
            index=index,
        )
        synthetic = synthetic_leverage_frame(qqq, annual_drag=0.025)
        drawdown = synthetic["Close"] / synthetic["Close"].cummax() - 1
        self.assertLess(float(drawdown.min()), -0.80)


class Phase2SelectionTests(unittest.TestCase):
    def test_selection_fields_ignore_holdout(self) -> None:
        base = {
            "dev_2010_2014_cagr": 0.20,
            "dev_2015_2018_cagr": 0.18,
            "dev_2019_2022_cagr": 0.16,
            "dev_2010_2014_mdd": -0.30,
            "dev_2015_2018_mdd": -0.35,
            "dev_2019_2022_mdd": -0.40,
            "dev_2010_2014_calmar": 0.67,
            "dev_2015_2018_calmar": 0.51,
            "dev_2019_2022_calmar": 0.40,
            "dev_all_trades_per_year": 1.0,
            "holdout_cagr": -0.90,
        }
        changed = dict(base)
        changed["holdout_cagr"] = 9.0
        self.assertEqual(
            _selection_fields(base)["selection_score"],
            _selection_fields(changed)["selection_score"],
        )

    def test_annual_blend_rebalances_sleeve_drift(self) -> None:
        aggressive = fake_simulation(4.0, 1.0)
        balanced = fake_simulation(1.5, 0.5)
        blended = blend_sleeves(
            aggressive,
            balanced,
            aggressive_weight=0.5,
            policy="annual",
            threshold=None,
            rebalance_cost_bps=0.0,
        )
        self.assertFalse(blended.daily.empty)
        self.assertGreaterEqual(len(blended.rebalance_events), 2)
        self.assertLessEqual(float(blended.daily["leveraged_weight"].max()), 1.0)


if __name__ == "__main__":
    unittest.main()
