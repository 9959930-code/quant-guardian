from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import btc_clock_hybrid_core as hybrid
import btc_clock_hybrid_runtime as runtime
import btc_clock_hybrid_telegram as hybrid_tg

runtime.install()

import btc_fixed_advisory as core  # noqa: E402
import btc_fixed_telegram_bot as bot  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any, bool]] = []

    def send_message(self, text: str, *, inline_keyboard=None, menu=False) -> None:
        self.messages.append((text, inline_keyboard, menu))


class ClockHybridRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)

    def state(self) -> dict[str, Any]:
        return core._initial_state(self.now)

    def block(self, epoch: int, offset: int) -> core.BlockContext:
        height = epoch * hybrid.INTERVAL + offset
        return core.BlockContext(
            height=height,
            epoch=epoch,
            cycle_progress=offset / hybrid.INTERVAL,
            mempool_height=height,
            blockstream_height=height,
            observed_at_utc=core.iso_utc(self.now),
        )

    def test_legacy_state_migrates_without_losing_assets_or_pending(self) -> None:
        legacy = hybrid._ORIG_INITIAL(self.now)
        legacy["schema_version"] = hybrid.LEGACY_SCHEMA
        legacy["strategy_version"] = hybrid.LEGACY_VERSION
        legacy["account"].update({"cash_krw": 7_654_321.0, "btc_quantity": 0.025})
        legacy["strategy"].update(
            {"phase": "HOLD", "cycle_epoch": 4, "exit_alerted_epochs": [5]}
        )
        legacy["telegram"]["pending_sync"] = {
            "kind": "ENTRY",
            "step": 2,
            "target_weight": 2 / 3,
            "side": "BUY",
            "expected_amount_krw": 1_000_000.0,
            "target_btc_value_krw": 2_000_000.0,
            "active_equity_krw": 3_000_000.0,
            "reference_price_krw": 90_000_000.0,
            "reason": "legacy pending",
            "created_at_utc": core.iso_utc(self.now - timedelta(hours=2)),
            "first_reminder_at_utc": core.iso_utc(self.now),
            "first_reminder_sent": False,
            "last_daily_reminder": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded, initialized = core.load_state(path, now_utc=self.now)
            self.assertFalse(initialized)
            self.assertEqual(loaded["schema_version"], 5)
            self.assertEqual(loaded["strategy_version"], hybrid.VERSION)
            self.assertEqual(loaded["account"]["cash_krw"], 7_654_321.0)
            self.assertEqual(loaded["account"]["btc_quantity"], 0.025)
            self.assertEqual(loaded["strategy"]["phase"], "HOLD")
            self.assertEqual(loaded["strategy"]["exit_warning_alerted_epochs"], [5])
            self.assertEqual(loaded["telegram"]["pending_sync"]["kind"], "ENTRY")
            self.assertIn("age_anchor_at_utc", loaded["telegram"]["pending_sync"])

    def test_unknown_state_version_is_never_silently_reset(self) -> None:
        state = self.state()
        state.update({"schema_version": 99, "strategy_version": "unknown"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(core.FixedStrategyError):
                core.load_state(path, now_utc=self.now)

    def test_625_watch_is_alert_only_at_exact_block(self) -> None:
        state, epoch = self.state(), 4
        state["strategy"].update(
            {
                "last_block_height": epoch * hybrid.INTERVAL + hybrid.WATCH - 1,
                "last_block_epoch": epoch,
                "last_block_progress": (hybrid.WATCH - 1) / hybrid.INTERVAL,
            }
        )
        block = self.block(epoch, hybrid.WATCH)
        events = core.detect_block_events(state, block, self.now)
        self.assertEqual([event["type"] for event in events], ["ENTRY_WATCH"])
        self.assertIsNone(
            core.create_official_order(
                state, block=block, price_krw=90_000_000.0, now_utc=self.now
            )
        )

    def test_entry_starts_at_exact_65_percent_block(self) -> None:
        before = self.state()
        self.assertIsNone(
            core.create_official_order(
                before,
                block=self.block(4, hybrid.ENTRY - 1),
                price_krw=90_000_000.0,
                now_utc=self.now,
            )
        )
        exact = self.state()
        result = core.create_official_order(
            exact,
            block=self.block(4, hybrid.ENTRY),
            price_krw=90_000_000.0,
            now_utc=self.now,
        )
        self.assertEqual(result["instruction"].kind, "ENTRY")
        self.assertEqual(result["instruction"].step, 1)

    def test_35_percent_is_warning_only(self) -> None:
        state, epoch = self.state(), 4
        state["strategy"].update(
            {
                "phase": "HOLD",
                "cycle_epoch": 3,
                "last_block_height": epoch * hybrid.INTERVAL + hybrid.WARNING - 1,
                "last_block_epoch": epoch,
                "last_block_progress": (hybrid.WARNING - 1) / hybrid.INTERVAL,
            }
        )
        block = self.block(epoch, hybrid.WARNING)
        events = core.detect_block_events(state, block, self.now)
        self.assertEqual([event["type"] for event in events], ["EXIT_WARNING"])
        self.assertIsNone(
            core.create_official_order(
                state, block=block, price_krw=90_000_000.0, now_utc=self.now
            )
        )

    def test_exit_steps_wait_for_36_37_38_boundaries(self) -> None:
        state = self.state()
        state["strategy"].update(
            {"phase": "HOLD", "cycle_epoch": 3, "entry_steps_completed": 3, "has_started_trading": True}
        )
        state["account"].update({"btc_quantity": 1.0, "cash_krw": 0.0})
        self.assertIsNone(
            core.create_official_order(
                state,
                block=self.block(4, hybrid.EXITS[0] - 1),
                price_krw=90_000_000.0,
                now_utc=self.now,
            )
        )
        first = core.create_official_order(
            state,
            block=self.block(4, hybrid.EXITS[0]),
            price_krw=90_000_000.0,
            now_utc=self.now + timedelta(days=7),
        )
        self.assertEqual(first["instruction"].step, 1)
        core.complete_pending_sync(
            state,
            btc_quantity=2 / 3,
            cash_krw=30_000_000.0,
            now_utc=self.now + timedelta(days=7, hours=1),
        )
        self.assertIsNone(
            core.create_official_order(
                state,
                block=self.block(4, hybrid.EXITS[1] - 1),
                price_krw=90_000_000.0,
                now_utc=self.now + timedelta(days=14),
            )
        )
        second = core.create_official_order(
            state,
            block=self.block(4, hybrid.EXITS[1]),
            price_krw=90_000_000.0,
            now_utc=self.now + timedelta(days=21),
        )
        self.assertEqual(second["instruction"].step, 2)
        core.complete_pending_sync(
            state,
            btc_quantity=1 / 3,
            cash_krw=60_000_000.0,
            now_utc=self.now + timedelta(days=21, hours=1),
        )
        third = core.create_official_order(
            state,
            block=self.block(4, hybrid.EXITS[2]),
            price_krw=90_000_000.0,
            now_utc=self.now + timedelta(days=28),
        )
        self.assertEqual(third["instruction"].step, 3)

    def test_large_jump_creates_one_step_and_pending_blocks_next(self) -> None:
        state = self.state()
        state["strategy"].update({"phase": "HOLD", "cycle_epoch": 3, "entry_steps_completed": 3})
        state["account"].update({"btc_quantity": 1.0, "cash_krw": 0.0})
        result = core.create_official_order(
            state,
            block=self.block(4, hybrid.EXITS[2] + 500),
            price_krw=90_000_000.0,
            now_utc=self.now,
        )
        self.assertEqual(result["instruction"].step, 1)
        blocked = core.create_official_order(
            state,
            block=self.block(4, hybrid.EXITS[2] + 500),
            price_krw=90_000_000.0,
            now_utc=self.now + timedelta(days=7),
        )
        self.assertEqual(blocked["type"], "SYNC_BLOCK")

    def test_order_plan_recalculates_after_24h_and_expires_after_7d(self) -> None:
        state = self.state()
        core.create_official_order(
            state,
            block=self.block(4, hybrid.ENTRY),
            price_krw=90_000_000.0,
            now_utc=self.now,
        )
        state["account"]["last_price_krw"] = 95_000_000.0
        client = FakeClient()
        bot.process_reminders(
            state, client, now_utc=self.now + timedelta(hours=25), daily_check=False
        )
        pending = state["telegram"]["pending_sync"]
        self.assertTrue(pending["notice_24h_sent"])
        self.assertEqual(pending["reference_price_krw"], 95_000_000.0)
        self.assertIn("24시간 경과", client.messages[0][0])
        bot.process_reminders(
            state, client, now_utc=self.now + timedelta(days=8), daily_check=False
        )
        self.assertTrue(pending["plan_expired"])
        self.assertTrue(any("7일 경과" in message[0] for message in client.messages))

    def test_manual_recalculation_restarts_plan_age(self) -> None:
        state = self.state()
        core.create_official_order(
            state,
            block=self.block(4, hybrid.ENTRY),
            price_krw=90_000_000.0,
            now_utc=self.now,
        )
        pending = state["telegram"]["pending_sync"]
        pending.update({"plan_expired": True, "notice_7d_sent": True})
        instruction = hybrid_tg.refresh_pending(
            state, price=92_000_000.0, now=self.now + timedelta(days=8), reset_age=True
        )
        self.assertFalse(pending["plan_expired"])
        self.assertFalse(pending["notice_7d_sent"])
        self.assertEqual(instruction.reference_price_krw, 92_000_000.0)
        labels = [button["text"] for row in bot.order_keyboard() for button in row]
        self.assertIn("🔄 현재가로 주문안 재계산", labels)


if __name__ == "__main__":
    unittest.main()
