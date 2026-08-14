from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_annual_band_research import (
    AnnualBandTaxableSimulator,
    policy_grid,
)
from equity_v2_rebalance_policy_research import PolicySpec


def synthetic_frames() -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2010-01-04", periods=1100)
    steps = np.arange(len(index), dtype=float)

    def frame(daily_growth: float) -> pd.DataFrame:
        close = 100.0 * np.power(1.0 + daily_growth, steps)
        open_price = np.r_[close[0], close[:-1]]
        return pd.DataFrame({"Open": open_price, "Close": close}, index=index)

    return {
        "QQQ": frame(0.00020),
        "QLD": frame(0.00040),
        "TQQQ": frame(0.00090),
        "CASH": frame(0.00001),
        "KRW=X": pd.DataFrame(
            {
                "Open": np.full(len(index), 1000.0),
                "Close": np.full(len(index), 1000.0),
            },
            index=index,
        ),
    }


class AnnualBandResearchTests(unittest.TestCase):
    def test_policy_grid_contains_annual_and_monthly_bands(self) -> None:
        policies = policy_grid(1)
        self.assertEqual(len(policies), 9)
        modes = {policy.rebalance_mode for policy in policies}
        self.assertIn("annual_band", modes)
        self.assertIn("monthly_band", modes)
        self.assertIn("annual_exact", modes)
        self.assertIn("none", modes)

    def test_wide_annual_band_does_not_trade(self) -> None:
        frames = synthetic_frames()
        policy = PolicySpec(
            policy_id="annual_band_49pp",
            contribution_mode="deficit_first",
            rebalance_mode="annual_band",
            rebalance_month=1,
            band=0.49,
        )
        result = AnnualBandTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=policy,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertEqual(result.metrics["rebalance_event_count"], 0)
        self.assertEqual(result.metrics["policy_sell_count"], 0)

    def test_narrow_annual_band_rebalances_divergence(self) -> None:
        frames = synthetic_frames()
        policy = PolicySpec(
            policy_id="annual_band_05pp",
            contribution_mode="deficit_first",
            rebalance_mode="annual_band",
            rebalance_month=1,
            band=0.05,
        )
        result = AnnualBandTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=policy,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertGreater(result.metrics["rebalance_event_count"], 0)
        self.assertGreater(result.metrics["policy_sell_count"], 0)

    def test_monthly_band_checks_at_least_as_often_as_annual_band(self) -> None:
        frames = synthetic_frames()
        annual = PolicySpec(
            policy_id="annual_band_05pp",
            contribution_mode="deficit_first",
            rebalance_mode="annual_band",
            rebalance_month=1,
            band=0.05,
        )
        monthly = PolicySpec(
            policy_id="monthly_band_05pp",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.05,
        )
        annual_result = AnnualBandTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=annual,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        monthly_result = AnnualBandTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=monthly,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertGreaterEqual(
            monthly_result.metrics["rebalance_event_count"],
            annual_result.metrics["rebalance_event_count"],
        )


if __name__ == "__main__":
    unittest.main()
