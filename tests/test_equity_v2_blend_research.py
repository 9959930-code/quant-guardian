from __future__ import annotations

import unittest

import pandas as pd

from equity_v2_blend_research import annual_rebalance_blend, no_rebalance_blend


class EquityV2BlendTests(unittest.TestCase):
    def setUp(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=520)
        self.equity = pd.DataFrame(
            {
                "aggressive": [1.0 * (1.001 ** number) for number in range(len(index))],
                "balanced": [1.0 * (1.0005 ** number) for number in range(len(index))],
            },
            index=index,
        )
        self.exposure = pd.DataFrame(
            {"aggressive": 1.0, "balanced": 0.5}, index=index
        )

    def test_no_rebalance_is_sum_of_independent_sleeves(self) -> None:
        result = no_rebalance_blend(self.equity, self.exposure, 0.5)
        expected = 0.5 * self.equity["aggressive"] + 0.5 * self.equity["balanced"]
        pd.testing.assert_series_equal(result.equity, expected)
        self.assertGreater(float(result.aggressive_capital.iloc[-1]), 0.5)
        self.assertGreater(float(result.tqqq_exposure.iloc[-1]), 0.5)

    def test_annual_rebalance_resets_capital_mix(self) -> None:
        result = annual_rebalance_blend(self.equity, self.exposure, 0.5)
        year_starts = result.equity.groupby(result.equity.index.year).head(1).index
        self.assertGreaterEqual(len(year_starts), 2)
        for date in year_starts[1:]:
            ratio = result.aggressive_capital.loc[date] / result.equity.loc[date]
            self.assertGreater(ratio, 0.49)
            self.assertLess(ratio, 0.51)


if __name__ == "__main__":
    unittest.main()
