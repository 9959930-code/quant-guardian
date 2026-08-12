from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_dca_engine import DcaSimulator, StrategySpec, build_strategy_grid, xirr


def market(days: int = 900, crash: bool = False) -> pd.DataFrame:
    index = pd.bdate_range("2015-01-02", periods=days)
    qqq = np.linspace(100, 220, days)
    if crash:
        qqq[250:400] = np.linspace(qqq[249], qqq[249] * 0.55, 150)
        qqq[400:] = np.linspace(qqq[399], 260, days - 400)
    tqqq = 100 * np.cumprod(np.r_[1.0, 1 + 3 * np.diff(qqq) / qqq[:-1]])
    frame = pd.DataFrame(index=index)
    frame["qqq_open"] = pd.Series(qqq, index=index).shift(1).fillna(qqq[0])
    frame["qqq_close"] = qqq
    frame["tqqq_open"] = pd.Series(tqqq, index=index).shift(1).fillna(tqqq[0])
    frame["tqqq_close"] = tqqq
    frame["fx_open"] = 1200.0
    frame["fx_close"] = 1200.0
    frame["qqq_high_252"] = frame["qqq_close"].rolling(252, min_periods=20).max()
    frame["qqq_drawdown"] = frame["qqq_close"] / frame["qqq_high_252"] - 1
    for window in (20, 50, 100):
        frame[f"qqq_sma_{window}"] = frame["qqq_close"].rolling(window, min_periods=1).mean()
    return frame


class DcaResearchTests(unittest.TestCase):
    def test_grid_contains_baselines_and_many_candidates(self) -> None:
        specs = build_strategy_grid()
        self.assertGreater(len(specs), 1000)
        ids = {spec.strategy_id for spec in specs}
        self.assertEqual(len(ids), len(specs))
        self.assertIn("always_tqqq", ids)
        self.assertIn("always_qqq_contrib", ids)

    def test_always_tqqq_contributes_initial_and_monthly(self) -> None:
        result = DcaSimulator(
            market(520),
            StrategySpec("always_tqqq", "always_tqqq"),
            initial_capital_krw=80_000_000,
            monthly_contribution_krw=500_000,
        ).run()
        self.assertGreater(result.metrics["total_contributions_krw"], 80_000_000)
        self.assertEqual(result.metrics["final_qqq_weight"], 0.0)
        self.assertGreater(result.metrics["final_tqqq_weight"], 0.99)

    def test_recovery_conversion_sells_qqq(self) -> None:
        spec = StrategySpec(
            "conversion",
            "qqq_convert",
            drawdown=-0.20,
            recovery_ma=20,
            conversion_fraction=1.0,
            conversion_stages=3,
            stage_frequency="weekly",
            switch_months=6,
        )
        result = DcaSimulator(
            market(900, crash=True),
            spec,
            initial_capital_krw=80_000_000,
            monthly_contribution_krw=500_000,
        ).run()
        self.assertTrue((result.trades.get("reason") == "drawdown_conversion").any())
        self.assertGreater(result.metrics["conversion_trade_count"], 0)

    def test_xirr_simple_case(self) -> None:
        value = xirr(
            [
                (pd.Timestamp("2020-01-01"), -100.0),
                (pd.Timestamp("2021-01-01"), 110.0),
            ]
        )
        self.assertIsNotNone(value)
        self.assertAlmostEqual(float(value), 0.10, places=3)


if __name__ == "__main__":
    unittest.main()
