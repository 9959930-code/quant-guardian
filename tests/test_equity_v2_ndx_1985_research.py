from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_ndx_1985_research import (
    dynamic_synthetic_3x,
    fixed_mix_dca,
    prepare_ndx_market,
)


def ndx_frame(days: int = 700, crash: bool = False) -> pd.DataFrame:
    index = pd.bdate_range("1985-01-31", periods=days)
    close = np.linspace(100.0, 220.0, days)
    if crash:
        close[250:330] = np.linspace(close[249], close[249] * 0.55, 80)
        close[330:] = np.linspace(close[329], 260.0, days - 330)
    open_ = pd.Series(close, index=index).shift(1).fillna(close[0]).to_numpy()
    return pd.DataFrame({"Open": open_, "Close": close}, index=index)


class NdxSyntheticTests(unittest.TestCase):
    def test_dynamic_synthetic_uses_short_rate_and_leverage(self) -> None:
        ndx = ndx_frame(300)
        rates = pd.Series(5.0, index=ndx.index)
        low_drag = dynamic_synthetic_3x(ndx, rates, residual_drag=0.0)
        high_drag = dynamic_synthetic_3x(ndx, rates, residual_drag=0.04)
        self.assertEqual(low_drag.index[0], ndx.index[0])
        self.assertGreater(float(low_drag["Close"].iloc[-1]), float(high_drag["Close"].iloc[-1]))
        self.assertTrue((low_drag["Close"] > 0).all())

    def test_prepare_market_starts_at_first_index_date(self) -> None:
        ndx = ndx_frame(300)
        synthetic = dynamic_synthetic_3x(
            ndx, pd.Series(4.0, index=ndx.index), residual_drag=0.01
        )
        market = prepare_ndx_market(
            ndx, synthetic, start=ndx.index[0], end=ndx.index[-1]
        )
        self.assertEqual(market.index[0], ndx.index[0])
        self.assertEqual(float(market.iloc[0]["qqq_drawdown"]), 0.0)
        self.assertTrue((market["fx_close"] == 1000.0).all())

    def test_fixed_mix_respects_initial_allocation(self) -> None:
        ndx = ndx_frame(520)
        synthetic = dynamic_synthetic_3x(
            ndx, pd.Series(4.0, index=ndx.index), residual_drag=0.01
        )
        market = prepare_ndx_market(
            ndx, synthetic, start=ndx.index[0], end=ndx.index[-1]
        )
        all_3x = fixed_mix_dca(
            market, initial_3x_weight=1.0, monthly_3x_weight=1.0
        )
        all_1x = fixed_mix_dca(
            market, initial_3x_weight=0.0, monthly_3x_weight=0.0
        )
        self.assertGreater(
            float(all_3x["after_tax_liquidation_value_krw"]),
            float(all_1x["after_tax_liquidation_value_krw"]),
        )
        self.assertEqual(float(all_3x["initial_3x_weight"]), 1.0)
        self.assertEqual(float(all_1x["initial_3x_weight"]), 0.0)


if __name__ == "__main__":
    unittest.main()
