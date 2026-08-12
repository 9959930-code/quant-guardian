from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from equity_v2_qqq_synthetic_long import constant_fx_frames, recovery_statistics


class QqqSyntheticLongTests(unittest.TestCase):
    def test_constant_fx_replaces_open_and_close(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=5)
        frames = {
            "QQQ": pd.DataFrame({"Open": np.arange(5) + 100, "Close": np.arange(5) + 101}, index=index),
            "KRW=X": pd.DataFrame({"Open": 1100.0, "Close": 1110.0}, index=index),
        }
        output = constant_fx_frames(frames, value=1000.0)
        self.assertTrue((output["KRW=X"]["Open"] == 1000.0).all())
        self.assertTrue((output["KRW=X"]["Close"] == 1000.0).all())
        self.assertIsNot(output["KRW=X"], frames["KRW=X"])

    def test_recovery_statistics_identifies_trough_and_recovery(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=6)
        daily = pd.DataFrame(
            {
                "nav": [1.0, 1.2, 0.6, 0.9, 1.2, 1.3],
                "value_krw": [100, 120, 60, 90, 120, 130],
                "contributions_krw": [100, 100, 100, 100, 100, 100],
            },
            index=index,
        )
        stats = recovery_statistics(daily)
        self.assertEqual(stats["mdd_peak_date"], index[1].date().isoformat())
        self.assertEqual(stats["mdd_trough_date"], index[2].date().isoformat())
        self.assertEqual(stats["mdd_recovery_date"], index[4].date().isoformat())
        self.assertEqual(stats["minimum_principal_date"], index[2].date().isoformat())
        self.assertEqual(stats["principal_recovery_date"], index[4].date().isoformat())


if __name__ == "__main__":
    unittest.main()
