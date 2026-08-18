from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import btc_clock_hybrid_core as hybrid
import btc_clock_hybrid_runtime as runtime
import btc_fixed_advisory as core
import btc_fixed_telegram_bot as bot


class EntryFundingAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        runtime.install()

    def setUp(self) -> None:
        self.monday = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        self.state = core._initial_state(self.monday)
        self.state["account"]["last_price_krw"] = 90_000_000.0

    @staticmethod
    def block(epoch: int, offset: int, observed: datetime) -> core.BlockContext:
        height = epoch * hybrid.INTERVAL + offset
        return core.BlockContext(
            height=height,
            epoch=epoch,
            cycle_progress=offset / hybrid.INTERVAL,
            mempool_height=height,
            blockstream_height=height,
            observed_at_utc=core.iso_utc(observed),
        )

    @staticmethod
    def funding_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            event
            for event in events
            if event["type"] == "ENTRY_FUNDING_PREP"
        ]

    def test_state_defaults_add_funding_history_without_schema_reset(self) -> None:
        self.assertEqual(self.state["schema_version"], 5)
        self.assertEqual(
            self.state["strategy_version"], hybrid.VERSION
        )
        self.assertEqual(
            self.state["strategy"]["entry_funding_alerts_sent"], []
        )
        self.assertIn(
            runtime.FUNDING_MIGRATION, self.state["migrations"]
        )

    def test_existing_schema5_state_is_enriched_without_losing_assets(self) -> None:
        state = self.state
        state["strategy"].pop("entry_funding_alerts_sent")
        state["migrations"].remove(runtime.FUNDING_MIGRATION)
        state["account"].update(
            {"cash_krw": 7_500_000.0, "btc_quantity": 0.025}
        )
        state["telegram"]["last_update_id"] = 12345

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded, initialized = core.load_state(
                path, now_utc=self.monday
            )

        self.assertFalse(initialized)
        self.assertEqual(loaded["account"]["cash_krw"], 7_500_000.0)
        self.assertEqual(loaded["account"]["btc_quantity"], 0.025)
        self.assertEqual(loaded["telegram"]["last_update_id"], 12345)
        self.assertEqual(
            loaded["strategy"]["entry_funding_alerts_sent"], []
        )
        self.assertIn(runtime.FUNDING_MIGRATION, loaded["migrations"])

    def test_five_and_three_business_day_alerts_are_sent_once(self) -> None:
        five_block = self.block(
            4,
            hybrid.ENTRY - 5 * 144,
            self.monday,
        )
        five = self.funding_events(
            core.detect_block_events(
                self.state, five_block, self.monday
            )
        )
        self.assertEqual(len(five), 1)
        self.assertEqual(five[0]["lead_business_days"], 5)
        self.assertEqual(five[0]["estimated_business_days"], 5)
        self.assertEqual(five[0]["remaining_blocks"], 720)

        wednesday = datetime(2026, 8, 19, 0, 17, tzinfo=UTC)
        three_block = self.block(
            4,
            hybrid.ENTRY - 3 * 144,
            wednesday,
        )
        three = self.funding_events(
            core.detect_block_events(
                self.state, three_block, wednesday
            )
        )
        self.assertEqual(len(three), 1)
        self.assertEqual(three[0]["lead_business_days"], 3)
        self.assertEqual(three[0]["estimated_business_days"], 3)
        self.assertEqual(three[0]["remaining_blocks"], 432)

        duplicate = self.funding_events(
            core.detect_block_events(
                self.state, three_block, wednesday
            )
        )
        self.assertEqual(duplicate, [])
        self.assertEqual(
            self.state["strategy"]["entry_funding_alerts_sent"],
            ["4:5", "4:3"],
        )

    def test_funding_alert_is_silent_on_weekend_and_before_0917(self) -> None:
        saturday = datetime(2026, 8, 22, 0, 17, tzinfo=UTC)
        block = self.block(4, hybrid.ENTRY - 5 * 144, saturday)
        self.assertEqual(
            self.funding_events(
                core.detect_block_events(
                    self.state, block, saturday
                )
            ),
            [],
        )

        state = core._initial_state(self.monday)
        early = datetime(2026, 8, 16, 23, 0, tzinfo=UTC)
        block = self.block(4, hybrid.ENTRY - 5 * 144, early)
        self.assertEqual(
            self.funding_events(
                core.detect_block_events(state, block, early)
            ),
            [],
        )

    def test_funding_alert_only_applies_to_waiting_entry(self) -> None:
        self.state["strategy"]["phase"] = "HOLD"
        block = self.block(
            4, hybrid.ENTRY - 5 * 144, self.monday
        )
        self.assertEqual(
            self.funding_events(
                core.detect_block_events(
                    self.state, block, self.monday
                )
            ),
            [],
        )

    def test_message_and_status_explain_estimate_and_amounts(self) -> None:
        block = self.block(
            4, hybrid.ENTRY - 5 * 144, self.monday
        )
        event = self.funding_events(
            core.detect_block_events(
                self.state, block, self.monday
            )
        )[0]
        message = bot.block_event_message(event)
        self.assertIn("첫 매수 자금준비 5영업일 전", message)
        self.assertIn("10분/블록", message)
        self.assertIn("준비할 총 원화: 10,000,000원", message)
        self.assertIn("1차 목표액 근사: 3,333,333원", message)
        self.assertIn("자동이체·자동주문은 없습니다", message)

        status = bot.status_message(
            self.state,
            price=90_000_000.0,
            block=block,
        )
        self.assertIn("첫 매수 자금준비", status)
        self.assertIn("5영업일 전·3영업일 전", status)
        self.assertIn("현재 예상 1차 매수 점검", status)


if __name__ == "__main__":
    unittest.main()
