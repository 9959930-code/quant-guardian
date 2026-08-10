from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Any

import btc_fixed_advisory as core
import btc_fixed_telegram_bot as bot


class FakeClient:
    def __init__(self) -> None:
        self.chat_id = "123"
        self.messages: list[tuple[str, Any, bool]] = []
        self.callbacks: list[tuple[str, str | None]] = []

    def send_message(self, text: str, *, inline_keyboard=None, menu=False) -> None:
        self.messages.append((text, inline_keyboard, menu))

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))


class TelegramFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 11, 0, 17, tzinfo=UTC)
        self.state = core._initial_state(self.now)
        self.client = FakeClient()
        self.block = core.BlockContext(
            height=962_000,
            epoch=4,
            cycle_progress=0.58095,
            mempool_height=962_000,
            blockstream_height=962_000,
            observed_at_utc=core.iso_utc(self.now),
        )
        self.price = 90_000_000.0

    def test_menu_deposit_amount_creates_confirm_operation(self) -> None:
        bot.begin_deposit(self.state, self.client, self.now)
        self.assertEqual(self.state["telegram"]["conversation"]["type"], "deposit_amount")
        handled = bot.handle_conversation_text(
            self.state, self.client, "3,000,000", self.now
        )
        self.assertTrue(handled)
        operation = self.state["telegram"]["pending_operation"]
        self.assertEqual(operation["type"], "deposit")
        self.assertEqual(operation["choice"], "current")
        self.assertEqual(operation["stage"], "confirm")

    def test_hold_deposit_offers_current_and_next_buttons(self) -> None:
        self.state["strategy"].update(
            {"phase": "HOLD", "entry_steps_completed": 3, "has_started_trading": True}
        )
        bot._create_deposit_operation(
            self.state, self.client, 2_000_000, self.now
        )
        operation = self.state["telegram"]["pending_operation"]
        self.assertEqual(operation["stage"], "choice")
        keyboard = self.client.messages[-1][1]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertIn("현재 사이클에 적용", labels)
        self.assertIn("다음 사이클에 보관", labels)

    def test_sync_flow_collects_btc_and_krw(self) -> None:
        bot.begin_sync(self.state, self.client, self.now)
        bot.handle_conversation_text(self.state, self.client, "0.035", self.now)
        self.assertEqual(self.state["telegram"]["conversation"]["type"], "sync_krw")
        bot.handle_conversation_text(self.state, self.client, "6650000", self.now)
        operation = self.state["telegram"]["pending_operation"]
        self.assertEqual(operation["type"], "sync")
        self.assertEqual(operation["payload"]["btc_quantity"], 0.035)
        self.assertEqual(operation["payload"]["cash_krw"], 6_650_000)

    def test_confirm_budget_changes_starting_cash(self) -> None:
        bot._create_budget_operation(self.state, self.client, 12_000_000, self.now)
        operation = self.state["telegram"]["pending_operation"]
        callback = {
            "id": "cb1",
            "data": f"op:confirm:{operation['id']}",
            "message": {"chat": {"id": 123}},
        }
        bot.handle_callback(
            self.state,
            self.client,
            callback,
            now_utc=self.now,
            price=self.price,
            block=self.block,
        )
        self.assertEqual(self.state["account"]["cash_krw"], 12_000_000)
        self.assertIsNone(self.state["telegram"]["pending_operation"])

    def test_confirm_sync_completes_pending_entry(self) -> None:
        self.state["telegram"]["pending_sync"] = {"kind": "ENTRY", "step": 1}
        bot._create_sync_operation(
            self.state, self.client, 0.035, 6_650_000, self.now
        )
        operation = self.state["telegram"]["pending_operation"]
        bot._confirm_operation(
            self.state,
            self.client,
            operation,
            now_utc=self.now,
            price=self.price,
            block=self.block,
        )
        self.assertEqual(self.state["strategy"]["phase"], "ENTRY")
        self.assertEqual(self.state["strategy"]["entry_steps_completed"], 1)
        self.assertIsNone(self.state["telegram"]["pending_sync"])

    def test_reminder_due_after_thirty_minutes(self) -> None:
        pending = {
            "first_reminder_at_utc": core.iso_utc(self.now),
            "first_reminder_sent": False,
            "last_daily_reminder": None,
        }
        self.assertEqual(core.reminder_due(pending, self.now, daily_check=False), "first")
        core.mark_reminded(pending, "first", self.now)
        self.assertIsNone(core.reminder_due(pending, self.now, daily_check=False))


if __name__ == "__main__":
    unittest.main()
