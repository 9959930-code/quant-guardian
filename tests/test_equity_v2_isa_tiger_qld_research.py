from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_isa_tiger_qld_research import (
    ISA_TOTAL_CONTRIBUTION_LIMIT_KRW,
    _leveraged_returns,
    build_fx_target_schedule,
    build_leverage_scenarios,
    simulate_dca,
)


class IsaTigerQldResearchTests(unittest.TestCase):
    def test_isa_cap_stops_at_100m(self) -> None:
        index = pd.bdate_range("2006-08-01", "2026-08-12")
        risk = pd.Series(100.0, index=index)
        cash = pd.Series(100.0, index=index)
        result = simulate_dca(
            risk_price=risk,
            cash_price=cash,
            target_schedule={index[0]: 1.0},
            start=index[0],
            end=index[-1],
            total_contribution_cap_krw=ISA_TOTAL_CONTRIBUTION_LIMIT_KRW,
            one_way_cost=0.0,
            tax_mode="isa",
        )
        self.assertAlmostEqual(
            result.metrics["total_contributions_krw"],
            ISA_TOTAL_CONTRIBUTION_LIMIT_KRW,
            places=2,
        )
        self.assertEqual(result.metrics["contribution_count"], 181)

    def test_double_krw_model_has_two_times_fx_exposure(self) -> None:
        index = pd.bdate_range("2025-01-01", periods=4)
        q = pd.Series([0.0, 0.0, 0.0, 0.0], index=index)
        f = pd.Series([0.0, 0.01, 0.0, 0.0], index=index)
        rate = pd.Series(0.0, index=index)
        result = _leveraged_returns(
            q,
            f,
            rate,
            model="double_krw",
            residual_drag=0.0,
            us_lag=0,
            fx_lag=0,
        )
        self.assertAlmostEqual(float(result.iloc[1]), 0.02, places=10)

    def test_fx_schedule_uses_hysteresis_and_next_day(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=360)
        fx = pd.Series(100.0, index=index)
        fx.iloc[270:305] = np.linspace(100.0, 155.0, 35)
        fx.iloc[305:] = np.linspace(155.0, 100.0, len(fx) - 305)
        schedule, log = build_fx_target_schedule(
            fx,
            index,
            sell_z=1.25,
            buy_z=0.75,
            execution_delay_days=1,
        )
        self.assertFalse(log.empty)
        self.assertIn("REDUCE", set(log["action"]))
        self.assertIn("RESTORE", set(log["action"]))
        self.assertTrue(
            (pd.to_datetime(log["execution_date"]) > pd.to_datetime(log["decision_date"])).all()
        )
        self.assertTrue(any(abs(value - 0.25) < 1e-12 for value in schedule.values()))
        self.assertTrue(any(abs(value - 1.0) < 1e-12 for value in schedule.values()))

    def test_tiger_new_money_adds_more_fx_beta_than_qld(self) -> None:
        table = build_leverage_scenarios().set_index("scenario")
        qld = table.loc["add_1_5m_new_money_to_qld"]
        tiger = table.loc["add_1_5m_new_money_to_tiger"]
        self.assertAlmostEqual(
            qld["nominal_gross_leverage"],
            tiger["nominal_gross_leverage"],
            places=12,
        )
        self.assertGreater(tiger["usdkrw_beta"], qld["usdkrw_beta"])


if __name__ == "__main__":
    unittest.main()
