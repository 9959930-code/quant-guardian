from __future__ import annotations

import unittest
from datetime import UTC, datetime

import pandas as pd

import btc_clock_exit_compare_research as research


class ClockExitScheduleTests(unittest.TestCase):
    def test_monday_check_uses_same_monday_before_cutoff(self) -> None:
        before = datetime(2026, 1, 5, 0, 10, tzinfo=UTC)  # 09:10 KST Monday
        after = datetime(2026, 1, 5, 0, 20, tzinfo=UTC)   # 09:20 KST Monday
        self.assertEqual(research.first_monday_check(before).isoformat(), "2026-01-05")
        self.assertEqual(research.first_monday_check(after).isoformat(), "2026-01-12")

    def _table(self) -> pd.DataFrame:
        epoch = 1
        next_start = (epoch + 1) * research.INTERVAL
        rows = [
            {
                "height": epoch * research.INTERVAL + research.ENTRY_WATCH_OFFSET,
                "timestamp_utc": datetime(2020, 1, 1, tzinfo=UTC),
            },
            {
                "height": epoch * research.INTERVAL + research.ENTRY_OFFSET,
                "timestamp_utc": datetime(2020, 1, 1, tzinfo=UTC),
            },
            {
                "height": next_start + 73_500,
                "timestamp_utc": datetime(2021, 1, 1, tzinfo=UTC),
            },
            {
                "height": next_start + research.OLD_EXIT_FIRST_HEIGHT_OFFSET,
                "timestamp_utc": datetime(2021, 1, 1, tzinfo=UTC),
            },
        ]
        for offset, day in zip(research.NEW_EXIT_OFFSETS, (8, 22, 29)):
            rows.append(
                {
                    "height": next_start + offset,
                    "timestamp_utc": datetime(2021, 1, day, tzinfo=UTC),
                }
            )
        return pd.DataFrame(rows)

    def test_old_exit_is_three_consecutive_mondays(self) -> None:
        schedule = research.build_schedule("old_35_then_weekly", (1,), self._table())
        exits = [action for action in schedule if action.kind == "EXIT"]
        self.assertEqual(len(exits), 3)
        self.assertEqual((exits[1].action_date - exits[0].action_date).days, 7)
        self.assertEqual((exits[2].action_date - exits[1].action_date).days, 7)
        self.assertEqual([item.target_weight for item in exits], list(research.EXIT_TARGETS))

    def test_new_exit_waits_for_36_37_38_thresholds(self) -> None:
        schedule = research.build_schedule("new_36_37_38", (1,), self._table())
        exits = [action for action in schedule if action.kind == "EXIT"]
        self.assertEqual(
            [item.trigger_height % research.INTERVAL for item in exits],
            list(research.NEW_EXIT_OFFSETS),
        )
        self.assertTrue(exits[0].action_date < exits[1].action_date < exits[2].action_date)
        self.assertEqual(len(schedule), 6)

    def test_watch_and_warning_do_not_create_trades(self) -> None:
        old = research.build_schedule("old_35_then_weekly", (1,), self._table())
        new = research.build_schedule("new_36_37_38", (1,), self._table())
        self.assertEqual(len(old), 6)
        self.assertEqual(len(new), 6)
        self.assertNotIn(research.ENTRY_WATCH_OFFSET, [a.trigger_height % research.INTERVAL for a in new])
        self.assertNotIn(73_500, [a.trigger_height % research.INTERVAL for a in new])


class ClockExitSimulationTests(unittest.TestCase):
    def test_later_exit_wins_when_price_rises_after_old_exit(self) -> None:
        index = pd.date_range("2020-01-01", "2021-02-15", freq="D")
        frame = pd.DataFrame(index=index)
        frame["Open"] = 100.0
        frame["Close"] = 100.0
        frame.loc["2021-01-15":, ["Open", "Close"]] = 200.0

        table = ClockExitScheduleTests()._table()
        old_schedule = research.build_schedule("old_35_then_weekly", (1,), table)
        new_schedule = research.build_schedule("new_36_37_38", (1,), table)
        _, _, old_metrics = research.simulate(
            frame, old_schedule, market="synthetic", policy="old_35_then_weekly", initial_capital=1.0
        )
        _, _, new_metrics = research.simulate(
            frame, new_schedule, market="synthetic", policy="new_36_37_38", initial_capital=1.0
        )
        self.assertGreater(new_metrics["final_equity"], old_metrics["final_equity"])
        self.assertEqual(old_metrics["trade_count"], 6)
        self.assertEqual(new_metrics["trade_count"], 6)


if __name__ == "__main__":
    unittest.main()
