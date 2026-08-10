from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from btc_fixed_six_trade_research import (
    Episode,
    execution_dates,
    find_completed_episodes,
    simulate_episode,
)


class FixedSixTradeTests(unittest.TestCase):
    def test_execution_dates_are_three_weekly_opens(self) -> None:
        index = pd.date_range("2026-01-01", periods=40, freq="D")
        dates = execution_dates(index, pd.Timestamp("2026-01-04"))
        self.assertEqual(
            dates,
            (
                pd.Timestamp("2026-01-05"),
                pd.Timestamp("2026-01-12"),
                pd.Timestamp("2026-01-19"),
            ),
        )

    def test_completed_episode_detects_65_to_next_35_window(self) -> None:
        index = pd.date_range("2020-01-05", periods=120, freq="W-SUN")
        progress = []
        epoch = []
        for i in range(len(index)):
            if i < 20:
                progress.append(0.50 + i * 0.008)
                epoch.append(3)
            elif i < 70:
                progress.append(0.66 + (i - 20) * 0.006)
                epoch.append(3)
            else:
                progress.append((i - 70) * 0.009)
                epoch.append(4)
        frame = pd.DataFrame(
            {
                "cycle_progress": np.array(progress) % 1.0,
                "halving_epoch": epoch,
            },
            index=index,
        )
        episodes = find_completed_episodes(frame)
        self.assertEqual(len(episodes), 1)
        self.assertGreaterEqual(
            float(frame.loc[episodes[0].entry_signal_date, "cycle_progress"]),
            0.65,
        )
        self.assertGreater(
            float(frame.loc[episodes[0].exit_signal_date, "cycle_progress"]),
            0.35,
        )

    def test_episode_has_exactly_six_trades_and_no_btc_after_exit(self) -> None:
        index = pd.date_range("2020-01-01", "2020-08-31", freq="D")
        close = pd.Series(
            np.linspace(100.0, 300.0, len(index)),
            index=index,
        )
        frame = pd.DataFrame(
            {
                "open": close.shift(1).fillna(close.iloc[0]),
                "close": close,
            },
            index=index,
        )
        episode = Episode(
            entry_signal_date=pd.Timestamp("2020-01-05"),
            halving_date=pd.Timestamp("2020-04-01"),
            exit_signal_date=pd.Timestamp("2020-08-02"),
        )
        result = simulate_episode(
            frame,
            episode,
            initial_capital_krw=1_000_000,
            fee_bps=5,
            slippage_bps=10,
        )
        self.assertEqual(result["trade_count"], 6)
        self.assertEqual(
            result["entry_dates"],
            ["2020-01-06", "2020-01-13", "2020-01-20"],
        )
        self.assertEqual(
            result["exit_dates"],
            ["2020-08-03", "2020-08-10", "2020-08-17"],
        )
        self.assertGreater(result["total_return"], 0)
        self.assertLessEqual(result["mdd"], 0)


if __name__ == "__main__":
    unittest.main()
