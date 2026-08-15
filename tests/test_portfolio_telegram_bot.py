from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import btc_fixed_advisory as btc_core
import isa_leverage_core as isa_core
import portfolio_telegram_bot as bot


class FakeClient:
    def __init__(self) -> None:
        self.chat_id = "123"
        self.messages: list[tuple[str, Any, bool]] = []
        self.callbacks: list[tuple[str, str | None]] = []

    def send_message(self, text: str, *, inline_keyboard=None, menu=False) -> None:
        self.messages.append((text, inline_keyboard, menu))

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))


class UnifiedPortfolioTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        self.btc_state = btc_core._initial_state(self.now)
        self.isa_state = isa_core.new_state(self.now)
        self.client = FakeClient()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.isa_path = Path(self.temp_dir.name) / "isa_state.json"
        bot.set_isa_context(self.isa_state, self.isa_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _quotes(self) -> dict[str, isa_core.QuoteSnapshot]:
        values = {
            "442580": 100_000.0,
            "0048J0": 14_500.0,
            "379810": 30_000.0,
            "418660": 40_000.0,
        }
        names = {
            item["code"]: item["name"] for item in isa_core.EXISTING_HOLDINGS
        }
        names[isa_core.TIGER_CODE] = isa_core.TIGER_NAME
        return {
            code: isa_core.QuoteSnapshot(
                code=code,
                name=names[code],
                date="2026-08-14",
                close=price,
            )
            for code, price in values.items()
        }

    def test_menu_distinguishes_btc_and_isa_sync(self) -> None:
        labels = [
            button["text"]
            for row in bot.UNIFIED_MENU_KEYBOARD["keyboard"]
            for button in row
        ]
        self.assertIn("🔄 BTC 잔고 동기화", labels)
        self.assertIn("🔄 ISA 잔고 동기화", labels)
        self.assertNotIn("🔄 잔고 동기화", labels)

    def test_isa_sync_can_complete_initial_purchase(self) -> None:
        bot.begin_isa_sync(self.btc_state, self.client, self.now)
        self.assertEqual(
            self.btc_state["telegram"]["conversation"]["type"],
            "isa_sync_tiger_quantity",
        )
        self.assertTrue(
            bot.handle_isa_conversation_text(
                self.btc_state, self.client, "231", self.now
            )
        )
        self.assertTrue(
            bot.handle_isa_conversation_text(
                self.btc_state, self.client, "9,986,000", self.now
            )
        )
        self.assertTrue(
            bot.handle_isa_conversation_text(
                self.btc_state, self.client, "21,700,000", self.now
            )
        )
        self.assertEqual(
            self.btc_state["telegram"]["conversation"]["type"],
            "isa_sync_confirm",
        )

        callback = {
            "id": "cb-isa",
            "data": "isa:sync:complete",
            "message": {"chat": {"id": 123}},
        }
        with patch.object(
            bot,
            "_fetch_isa_market_context",
            return_value=(
                self._quotes(),
                isa_core.FxSnapshot(
                    "2026-08-14", 1_416.85, -1.11, "NORMAL"
                ),
            ),
        ):
            handled = bot.handle_isa_callback(
                self.btc_state,
                self.client,
                callback,
                now_utc=self.now,
            )

        self.assertTrue(handled)
        self.assertEqual(self.isa_state["account"]["tiger_quantity"], 231.0)
        self.assertEqual(
            self.isa_state["account"]["tiger_invested_krw"], 9_986_000.0
        )
        self.assertEqual(
            self.isa_state["account"]["isa_total_contributions_krw"],
            21_700_000.0,
        )
        self.assertTrue(self.isa_state["strategy"]["initial_completed"])
        self.assertEqual(self.isa_state["strategy"]["monthly_start_period"], "2026-09")
        self.assertIsNone(self.btc_state["telegram"]["conversation"])
        self.assertTrue(self.isa_path.exists())
        self.assertIn("텔레그램 잔고동기화 완료", self.client.messages[-1][0])

    def test_partial_sync_keeps_initial_plan_blocked(self) -> None:
        bot.begin_isa_sync(self.btc_state, self.client, self.now)
        bot.handle_isa_conversation_text(
            self.btc_state, self.client, "50", self.now
        )
        bot.handle_isa_conversation_text(
            self.btc_state, self.client, "2,000,000", self.now
        )
        bot.handle_isa_conversation_text(
            self.btc_state, self.client, "-", self.now
        )
        callback = {
            "id": "cb-partial",
            "data": "isa:sync:partial",
            "message": {"chat": {"id": 123}},
        }
        with patch.object(
            bot,
            "_fetch_isa_market_context",
            return_value=(self._quotes(), None),
        ):
            bot.handle_isa_callback(
                self.btc_state,
                self.client,
                callback,
                now_utc=self.now,
            )
        self.assertFalse(self.isa_state["strategy"]["initial_completed"])
        self.assertEqual(self.isa_state["account"]["tiger_quantity"], 50.0)
        self.assertIsNone(
            self.isa_state["account"]["isa_total_contributions_krw"]
        )

    def test_isa_status_command_is_routed_to_isa(self) -> None:
        with patch.object(bot, "send_isa_status") as status, patch.object(
            bot, "_ORIGINAL_HANDLE_TEXT_MESSAGE"
        ) as btc_handler:
            bot.unified_handle_text_message(
                self.btc_state,
                self.client,
                "📈 ISA 상태",
                now_utc=self.now,
                price=90_000_000.0,
                block=None,
            )
        status.assert_called_once_with(self.client)
        btc_handler.assert_not_called()

    def test_new_btc_sync_label_routes_to_existing_btc_flow(self) -> None:
        with patch.object(bot, "_ORIGINAL_HANDLE_TEXT_MESSAGE") as btc_handler:
            bot.unified_handle_text_message(
                self.btc_state,
                self.client,
                "🔄 BTC 잔고 동기화",
                now_utc=self.now,
                price=90_000_000.0,
                block=None,
            )
        args = btc_handler.call_args.args
        self.assertEqual(args[2], "🔄 잔고 동기화")

    def test_isa_conversation_reminder_is_explicit(self) -> None:
        message = bot.unified_conversation_reminder_message(
            {"type": "isa_sync_tiger_invested"}
        )
        self.assertIn("ISA TIGER 누적투입원금", message)


if __name__ == "__main__":
    unittest.main()
