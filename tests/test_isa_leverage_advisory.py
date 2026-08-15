from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import isa_leverage_advisory as service
import isa_leverage_core as core
from isa_leverage_messages import initial_plan_message


class IsaLeverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        self.quotes = {
            "442580": core.QuoteSnapshot(
                "442580", "PLUS 글로벌HBM반도체", "2026-08-17", 100_000.0
            ),
            "0048J0": core.QuoteSnapshot(
                "0048J0", "KODEX 미국머니마켓액티브", "2026-08-17", 14_500.0
            ),
            "379810": core.QuoteSnapshot(
                "379810", "KODEX 미국나스닥100", "2026-08-17", 30_000.0
            ),
            "418660": core.QuoteSnapshot(
                "418660", core.TIGER_NAME, "2026-08-17", 40_000.0
            ),
        }
        self.fx = core.FxSnapshot("2026-08-14", 1_400.0, -0.5, "NORMAL")

    def test_state_has_approved_holdings_and_no_auto_order(self) -> None:
        state = core.new_state(self.now)
        self.assertFalse(state["strategy"]["auto_order"])
        holdings = {
            item["code"]: item["quantity"]
            for item in state["account"]["existing_holdings"]
        }
        self.assertEqual(holdings, {"442580": 7.0, "0048J0": 145.0, "379810": 70.0})
        core.validate_state(state)

    def test_purchase_plan_uses_whole_shares(self) -> None:
        plan = core.calculate_purchase_plan(10_000_000, 43_190)
        self.assertEqual(plan.shares, 231)
        self.assertEqual(plan.expected_order_krw, 9_976_890)

    def test_monthly_plan_only_after_completion_and_once_per_month(self) -> None:
        state = core.new_state(self.now)
        self.assertFalse(
            core.is_monthly_plan_due(
                state,
                now_kst=self.now.astimezone(core.KST),
                latest_quote_date="2026-08-17",
            )
        )
        state["strategy"]["initial_completed"] = True
        state["strategy"]["monthly_start_period"] = "2026-08"
        self.assertTrue(
            core.is_monthly_plan_due(
                state,
                now_kst=self.now.astimezone(core.KST),
                latest_quote_date="2026-08-17",
            )
        )
        state["strategy"]["last_monthly_plan_period"] = "2026-08"
        self.assertFalse(
            core.is_monthly_plan_due(
                state,
                now_kst=self.now.astimezone(core.KST),
                latest_quote_date="2026-08-17",
            )
        )

    def test_manual_completion_requires_real_balance(self) -> None:
        state = core.new_state(self.now)
        with self.assertRaises(core.IsaStrategyError):
            core.apply_manual_sync(
                state,
                tiger_quantity=None,
                tiger_invested_krw=None,
                isa_total_contributions_krw=None,
                mark_initial_completed=True,
                now_utc=self.now,
            )
        core.apply_manual_sync(
            state,
            tiger_quantity=231,
            tiger_invested_krw=9_976_890,
            isa_total_contributions_krw=20_000_000,
            mark_initial_completed=True,
            now_utc=self.now,
        )
        self.assertTrue(state["strategy"]["initial_completed"])
        self.assertEqual(state["strategy"]["monthly_start_period"], "2026-09")

    def test_fx_zones(self) -> None:
        self.assertEqual(core.fx_zone(0.0), "NORMAL")
        self.assertEqual(core.fx_zone(0.75), "WATCH")
        self.assertEqual(core.fx_zone(1.25), "HIGH")

    def test_initial_message_shows_holdings_and_safety(self) -> None:
        text = initial_plan_message(core.new_state(self.now), self.quotes, self.fx)
        self.assertIn("PLUS 글로벌HBM반도체", text)
        self.assertIn("70주", text)
        self.assertIn("자동주문", text)
        self.assertIn("10,000,000원", text)

    def test_service_sends_initial_once_then_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch.object(service, "fetch_quotes", return_value=self.quotes), patch.object(
                service, "fetch_fx_snapshot", return_value=self.fx
            ):
                first = service.run_service(
                    state_path=state_path,
                    now_utc=self.now,
                    dry_run=True,
                )
                second = service.run_service(
                    state_path=state_path,
                    now_utc=self.now,
                    dry_run=True,
                )
            self.assertEqual(first["message_count"], 1)
            self.assertEqual(second["message_count"], 0)


if __name__ == "__main__":
    unittest.main()
