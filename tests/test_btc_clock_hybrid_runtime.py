from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import btc_clock_hybrid_runtime as runtime
import btc_fixed_advisory as core


class ClockHybridRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime.install()
        self.now = datetime(2026, 1, 5, 0, 17, tzinfo=UTC)
        self.state = runtime.hybrid_initial_state(self.now)
        self.state["account"]["last_price_krw"] = 100_000_000.0

    def tearDown(self) -> None:
        runtime.uninstall()

    @staticmethod
    def block(epoch: int, offset: int) -> core.BlockContext:
        height = epoch * runtime.HALVING_INTERVAL + offset
        return core.BlockContext(
            height=height,
            epoch=epoch,
            cycle_progress=offset / runtime.HALVING_INTERVAL,
            mempool_height=height,
            blockstream_height=height,
            observed_at_utc="2026-01-05T00:17:00Z",
        )

    def _prepare_hold_state(self) -> None:
        self.state["strategy"].update(
            {
                "phase": "HOLD",
                "cycle_epoch": 3,
                "entry_steps_completed": 3,
                "exit_steps_completed": 0,
                "has_started_trading": True,
            }
        )
        self.state["account"].update(
            {
                "cash_krw": 0.0,
                "btc_quantity": 0.1,
                "total_contributions_krw": 10_000_000.0,
            }
        )

    def test_new_state_uses_clock_hybrid_version(self) -> None:
        self.assertEqual(self.state["schema_version"], 5)
        self.assertEqual(
            self.state["strategy_version"],
            "btc-fixed-six-clock-hybrid-1.1",
        )
        self.assertIn(
            "entry_watch_alerted_epochs", self.state["strategy"]
        )
        self.assertIn("funding_alerts_sent", self.state["strategy"])

    def test_legacy_state_migration_preserves_account_and_telegram(self) -> None:
        legacy = runtime.hybrid_initial_state(self.now)
        legacy["schema_version"] = 4
        legacy["strategy_version"] = runtime.LEGACY_STRATEGY_VERSION
        legacy["account"].update(
            {
                "cash_krw": 6_500_000.0,
                "btc_quantity": 0.035,
                "total_contributions_krw": 10_000_000.0,
            }
        )
        legacy["telegram"]["last_update_id"] = 98765
        legacy["telegram"]["pending_sync"] = {
            "kind": "ENTRY",
            "step": 1,
        }
        legacy["strategy"].pop("entry_watch_alerted_epochs", None)
        legacy["strategy"].pop("funding_alerts_sent", None)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
            )
            migrated, initialized = core.load_state(
                path, now_utc=self.now
            )

        self.assertFalse(initialized)
        self.assertEqual(migrated["schema_version"], 5)
        self.assertEqual(
            migrated["strategy_version"],
            runtime.STRATEGY_VERSION,
        )
        self.assertEqual(migrated["account"]["cash_krw"], 6_500_000.0)
        self.assertEqual(migrated["account"]["btc_quantity"], 0.035)
        self.assertEqual(
            migrated["telegram"]["last_update_id"], 98765
        )
        self.assertEqual(
            migrated["telegram"]["pending_sync"]["step"], 1
        )
        events = [row["event"] for row in migrated["audit"]]
        self.assertIn("CLOCK_HYBRID_STATE_MIGRATED", events)

    def test_unknown_state_version_is_not_silently_reset(self) -> None:
        unknown = runtime.hybrid_initial_state(self.now)
        unknown["schema_version"] = 999
        unknown["strategy_version"] = "unknown"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(unknown), encoding="utf-8")
            with self.assertRaises(core.FixedStrategyError):
                core.load_state(path, now_utc=self.now)

    def test_62_5_boundary_sends_watch_only_once_and_no_order(self) -> None:
        sunday = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
        before = self.block(4, runtime.ENTRY_WATCH_OFFSET - 1)
        exact = self.block(4, runtime.ENTRY_WATCH_OFFSET)

        self.assertNotIn(
            "ENTRY_WATCH",
            {
                event["type"]
                for event in core.detect_block_events(
                    self.state, before, sunday
                )
            },
        )
        first = core.detect_block_events(self.state, exact, sunday)
        second = core.detect_block_events(self.state, exact, sunday)
        self.assertEqual(
            [event["type"] for event in first].count("ENTRY_WATCH"),
            1,
        )
        self.assertNotIn(
            "ENTRY_WATCH", [event["type"] for event in second]
        )
        self.assertIsNone(
            core.create_official_order(
                self.state,
                block=exact,
                price_krw=100_000_000.0,
                now_utc=self.now,
            )
        )

    def test_65_exact_starts_first_entry(self) -> None:
        block = self.block(4, runtime.ENTRY_OFFSET)
        action = core.create_official_order(
            self.state,
            block=block,
            price_krw=100_000_000.0,
            now_utc=self.now,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action["type"], "ORDER")
        instruction = action["instruction"]
        self.assertEqual(instruction.kind, "ENTRY")
        self.assertEqual(instruction.step, 1)
        self.assertAlmostEqual(instruction.target_weight, 1 / 3)

    def test_funding_alerts_arrive_at_five_and_three_business_days(self) -> None:
        monday = datetime(2026, 1, 5, 0, 17, tzinfo=UTC)
        five_day_block = self.block(
            4, runtime.ENTRY_OFFSET - 5 * 144
        )
        five_events = core.detect_block_events(
            self.state, five_day_block, monday
        )
        funding5 = [
            event
            for event in five_events
            if event["type"] == "ENTRY_FUNDING_PREP"
        ]
        self.assertEqual(len(funding5), 1)
        self.assertEqual(funding5[0]["lead_business_days"], 5)
        self.assertEqual(funding5[0]["estimated_business_days"], 5)

        wednesday = datetime(2026, 1, 7, 0, 17, tzinfo=UTC)
        three_day_block = self.block(
            4, runtime.ENTRY_OFFSET - 3 * 144
        )
        three_events = core.detect_block_events(
            self.state, three_day_block, wednesday
        )
        funding3 = [
            event
            for event in three_events
            if event["type"] == "ENTRY_FUNDING_PREP"
        ]
        self.assertEqual(len(funding3), 1)
        self.assertEqual(funding3[0]["lead_business_days"], 3)
        self.assertEqual(funding3[0]["estimated_business_days"], 3)

        duplicate = core.detect_block_events(
            self.state, three_day_block, wednesday
        )
        self.assertNotIn(
            "ENTRY_FUNDING_PREP",
            [event["type"] for event in duplicate],
        )

    def test_35_warns_but_does_not_sell(self) -> None:
        self._prepare_hold_state()
        block = self.block(4, runtime.EXIT_WARNING_OFFSET)
        events = core.detect_block_events(self.state, block, self.now)
        self.assertIn("EXIT_WARNING", [event["type"] for event in events])
        self.assertIsNone(
            core.create_official_order(
                self.state,
                block=block,
                price_krw=100_000_000.0,
                now_utc=self.now,
            )
        )

    def test_36_37_38_exit_steps_require_each_threshold(self) -> None:
        self._prepare_hold_state()

        first = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[0]),
            price_krw=100_000_000.0,
            now_utc=self.now,
        )
        self.assertEqual(first["instruction"].step, 1)
        core.complete_pending_sync(
            self.state,
            btc_quantity=0.0666666667,
            cash_krw=3_333_333.0,
            now_utc=self.now,
        )

        below_second = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[1] - 1),
            price_krw=100_000_000.0,
            now_utc=datetime(2026, 1, 12, 0, 17, tzinfo=UTC),
        )
        self.assertIsNone(below_second)

        second = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[1]),
            price_krw=100_000_000.0,
            now_utc=datetime(2026, 1, 19, 0, 17, tzinfo=UTC),
        )
        self.assertEqual(second["instruction"].step, 2)
        core.complete_pending_sync(
            self.state,
            btc_quantity=0.0333333333,
            cash_krw=6_666_667.0,
            now_utc=datetime(2026, 1, 19, 0, 30, tzinfo=UTC),
        )

        below_third = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[2] - 1),
            price_krw=100_000_000.0,
            now_utc=datetime(2026, 1, 26, 0, 17, tzinfo=UTC),
        )
        self.assertIsNone(below_third)

        third = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[2]),
            price_krw=100_000_000.0,
            now_utc=datetime(2026, 2, 2, 0, 17, tzinfo=UTC),
        )
        self.assertEqual(third["instruction"].step, 3)
        self.assertEqual(third["instruction"].target_weight, 0.0)

    def test_large_threshold_jump_still_creates_one_step_per_check(self) -> None:
        self._prepare_hold_state()
        now = datetime(2026, 1, 5, 0, 17, tzinfo=UTC)
        action = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[2] + 1_000),
            price_krw=100_000_000.0,
            now_utc=now,
        )
        self.assertEqual(action["instruction"].step, 1)
        self.assertFalse(core.official_action_due(now, self.state))

    def test_pending_sync_blocks_next_order(self) -> None:
        self._prepare_hold_state()
        self.state["telegram"]["pending_sync"] = {
            "kind": "ENTRY",
            "step": 3,
        }
        action = core.create_official_order(
            self.state,
            block=self.block(4, runtime.EXIT_OFFSETS[2]),
            price_krw=100_000_000.0,
            now_utc=self.now,
        )
        self.assertEqual(action["type"], "SYNC_BLOCK")

    def test_refresh_and_cancel_keep_strategy_stage_safe(self) -> None:
        instruction = core._active_target_instruction(
            self.state,
            target_weight=1 / 3,
            price_krw=100_000_000.0,
            kind="ENTRY",
            step=1,
            reason="test",
        )
        original = core._new_pending_sync(
            self.state, instruction, self.now
        )
        original_id = original["id"]

        refreshed = runtime.refresh_pending_order(
            self.state,
            price_krw=90_000_000.0,
            now_utc=datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
        )
        self.assertEqual(refreshed.kind, "ENTRY")
        self.assertNotEqual(
            self.state["telegram"]["pending_sync"]["id"],
            original_id,
        )
        self.assertEqual(self.state["strategy"]["phase"], "WAITING_ENTRY")

        self.assertTrue(
            runtime.cancel_pending_order(
                self.state,
                now_utc=datetime(2026, 1, 5, 1, 5, tzinfo=UTC),
            )
        )
        self.assertIsNone(self.state["telegram"]["pending_sync"])
        self.assertEqual(self.state["strategy"]["phase"], "WAITING_ENTRY")

    def test_order_keyboard_has_refresh_and_cancel(self) -> None:
        labels = [
            button["text"]
            for row in runtime.hybrid_order_keyboard()
            for button in row
        ]
        self.assertIn("🔄 현재 금액 다시 계산", labels)
        self.assertIn("❌ 주문안 취소", labels)


if __name__ == "__main__":
    unittest.main()
