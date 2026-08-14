from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_engine import FeatureStore
from equity_v2_modern_engine import (
    GenericTaxableSimulator,
    ModernCandidate,
    build_candidate_grid,
    generate_targets,
)
from equity_v2_modern_research import dynamic_synthetic, selection_fields


def synthetic_frames(days: int = 1200) -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2006-01-02", periods=days)
    qqq = np.linspace(100.0, 250.0, days) + np.sin(np.arange(days) / 18) * 5
    qld = 100.0 * np.cumprod(
        np.r_[1.0, 1.0 + 2.0 * np.diff(qqq) / qqq[:-1]]
    )
    tqqq = 100.0 * np.cumprod(
        np.r_[1.0, 1.0 + 3.0 * np.diff(qqq) / qqq[:-1]]
    )
    cash = 100.0 * np.cumprod(np.full(days, 1.0001))
    fx = np.linspace(1000.0, 1200.0, days)
    frames: dict[str, pd.DataFrame] = {}
    for asset, values in {
        "QQQ": qqq,
        "QLD": qld,
        "TQQQ": tqqq,
        "CASH": cash,
        "KRW=X": fx,
    }.items():
        frames[asset] = pd.DataFrame(
            {
                "Open": pd.Series(values, index=index).shift(1).fillna(values[0]),
                "Close": values,
            },
            index=index,
        )
    return frames


class ModernGridTests(unittest.TestCase):
    def test_grid_is_large_and_unique(self) -> None:
        candidates = build_candidate_grid()
        self.assertGreater(len(candidates), 8000)
        self.assertEqual(len(candidates), len({item.candidate_id for item in candidates}))
        self.assertTrue(any(item.family == "breakout" for item in candidates))
        self.assertTrue(any(item.family == "tiered" for item in candidates))


class ModernSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frames = synthetic_frames()
        close = pd.concat(
            {asset: frame["Close"] for asset, frame in self.frames.items()},
            axis=1,
        )
        self.store = FeatureStore(close)

    def test_trend_candidate_moves_from_cash_to_risk(self) -> None:
        candidate = ModernCandidate(
            "trend_test",
            "trend",
            {
                "entry_ma": 150,
                "exit_ma": 200,
                "frequency": "monthly",
                "confirm": 1,
                "slope_days": 0,
                "allocation": "t50_q50",
                "risk_off": "CASH",
            },
        )
        targets = generate_targets(
            candidate,
            store=self.store,
            start=self.store.close.index[250],
            end=self.store.close.index[-1],
        )
        self.assertFalse(targets.empty)
        self.assertIn("CASH", targets.columns)
        self.assertIn("TQQQ", targets.columns)
        self.assertTrue((targets["TQQQ"].fillna(0) > 0).any())

    def test_taxable_simulation_accepts_monthly_contributions(self) -> None:
        candidate = ModernCandidate("hold", "buy_hold", {"asset": "QQQ"})
        start = self.store.close.index[250]
        end = self.store.close.index[-1]
        targets = generate_targets(
            candidate,
            store=self.store,
            start=start,
            end=end,
        )
        result = GenericTaxableSimulator(
            frames=self.frames,
            targets=targets,
            start=start,
            end=end,
            initial_capital_krw=80_000_000,
            monthly_contribution_krw=500_000,
        ).run()
        self.assertGreater(result.metrics["total_contributions_krw"], 80_000_000)
        self.assertGreater(result.metrics["after_tax_liquidation_value_krw"], 0)
        self.assertIsNotNone(result.metrics["after_tax_xirr"])


class SyntheticTests(unittest.TestCase):
    def test_dynamic_synthetic_uses_leverage(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=260)
        close = 100.0 * np.cumprod(np.full(len(index), 1.001))
        underlying = pd.DataFrame(
            {
                "Open": pd.Series(close, index=index).shift(1).fillna(close[0]),
                "Close": close,
            },
            index=index,
        )
        rates = pd.Series(2.0, index=index)
        two = dynamic_synthetic(underlying, rates, leverage=2.0, residual_drag=0.0)
        three = dynamic_synthetic(underlying, rates, leverage=3.0, residual_drag=0.0)
        self.assertGreater(float(three["Close"].iloc[-1]), float(two["Close"].iloc[-1]))

    def test_selection_fields_ignore_holdout(self) -> None:
        row = {
            "gfc_qe_cagr": 0.20,
            "post_gfc_cagr": 0.18,
            "covid_inflation_cagr": 0.16,
            "gfc_qe_mdd": -0.40,
            "post_gfc_mdd": -0.35,
            "covid_inflation_mdd": -0.45,
            "gfc_qe_calmar": 0.5,
            "post_gfc_calmar": 0.5,
            "covid_inflation_calmar": 0.4,
            "full_trades_per_year": 1.0,
            "holdout_cagr": -0.90,
        }
        changed = dict(row)
        changed["holdout_cagr"] = 9.0
        self.assertEqual(
            selection_fields(row)["selection_score"],
            selection_fields(changed)["selection_score"],
        )


if __name__ == "__main__":
    unittest.main()
