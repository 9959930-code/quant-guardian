from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_rebalance_policy_research import (
    PolicySpec,
    PolicyTaxableSimulator,
    phase_a_policies,
    phase_b_policies,
)


def synthetic_frames() -> dict[str, pd.DataFrame]:
    index = pd.bdate_range("2010-01-04", periods=900)
    steps = np.arange(len(index), dtype=float)

    def frame(daily_growth: float) -> pd.DataFrame:
        close = 100.0 * np.power(1.0 + daily_growth, steps)
        open_price = np.r_[close[0], close[:-1]]
        return pd.DataFrame({"Open": open_price, "Close": close}, index=index)

    return {
        "QQQ": frame(0.00020),
        "QLD": frame(0.00040),
        "TQQQ": frame(0.00075),
        "CASH": frame(0.00002),
        "KRW=X": pd.DataFrame(
            {"Open": np.full(len(index), 1000.0), "Close": np.full(len(index), 1000.0)},
            index=index,
        ),
    }


class RebalancePolicyResearchTests(unittest.TestCase):
    def test_policy_grid_sizes(self) -> None:
        self.assertEqual(len(phase_a_policies()), 6)
        self.assertEqual(len(phase_b_policies(1)), 5)

    def test_contribution_only_never_policy_sells(self) -> None:
        frames = synthetic_frames()
        policy = PolicySpec(
            policy_id="contribution_only",
            contribution_mode="deficit_first",
            rebalance_mode="none",
        )
        result = PolicyTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=policy,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertEqual(result.metrics["policy_sell_count"], 0)
        self.assertEqual(result.metrics["rebalance_event_count"], 0)

    def test_annual_exact_rebalances_divergent_assets(self) -> None:
        frames = synthetic_frames()
        policy = PolicySpec(
            policy_id="exact_jan",
            contribution_mode="deficit_first",
            rebalance_mode="annual_exact",
            rebalance_month=1,
        )
        result = PolicyTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=policy,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertGreater(result.metrics["rebalance_event_count"], 0)
        self.assertGreater(result.metrics["policy_sell_count"], 0)

    def test_band_policy_trades_less_often_than_narrower_band(self) -> None:
        frames = synthetic_frames()
        narrow = PolicySpec(
            policy_id="band_5",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.05,
        )
        wide = PolicySpec(
            policy_id="band_15",
            contribution_mode="deficit_first",
            rebalance_mode="monthly_band",
            band=0.15,
        )
        narrow_result = PolicyTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=narrow,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        wide_result = PolicyTaxableSimulator(
            frames=frames,
            target_weights={"TQQQ": 0.5, "CASH": 0.5},
            policy=wide,
            start=frames["QQQ"].index[0],
            end=frames["QQQ"].index[-1],
        ).run()
        self.assertGreaterEqual(
            narrow_result.metrics["rebalance_event_count"],
            wide_result.metrics["rebalance_event_count"],
        )


if __name__ == "__main__":
    unittest.main()
