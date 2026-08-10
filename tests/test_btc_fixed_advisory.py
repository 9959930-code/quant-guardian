from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import btc_fixed_advisory as core


class FixedSixStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 17, 0, 17, tzinfo=UTC)
        self.state = core._initial_state(self.now)
        self.price = 100_000_000.0

    def block(self, epoch: int = 4, progress: float = 0.66) -> core.BlockContext:
        height = epoch * 210_000 + round(progress * 210_000)
        return core.BlockContext(
            height=height,
            epoch=epoch,
            cycle_progress=progress,
            mempool_height=height,
            blockstream_height=height,
            observed_at_utc=core.iso_utc(self.now),
        )

    def test_initial_budget_is_ten_million(self) -> None:
        self.assertEqual(self.state["account"]["cash_krw"], 10_000_000)
        self.assertEqual(self.state["strategy"]["phase"], "WAITING_ENTRY")
        self.assertFalse(self.state["strategy"]["has_started_trading"])

    def test_three_entry_steps_then_hold_without_rebalancing(self) -> None:
        action1 = core.create_official_order(
            self.state, block=self.block(), price_krw=self.price, now_utc=self.now
        )
        self.assertEqual(action1["instruction"].target_weight, 1 / 3)
        core.complete_pending_sync(
            self.state, btc_quantity=0.033, cash_krw=6_700_000, now_utc=self.now
        )
        self.assertEqual(self.state["strategy"]["phase"], "ENTRY")
        self.state["strategy"]["last_official_monday"] = None

        action2 = core.create_official_order(
            self.state, block=self.block(), price_krw=self.price, now_utc=self.now
        )
        self.assertEqual(action2["instruction"].step, 2)
        self.assertAlmostEqual(action2["instruction"].target_weight, 2 / 3)
        core.complete_pending_sync(
            self.state, btc_quantity=0.066, cash_krw=3_400_000, now_utc=self.now
        )
        self.state["strategy"]["last_official_monday"] = None

        action3 = core.create_official_order(
            self.state, block=self.block(), price_krw=self.price, now_utc=self.now
        )
        self.assertEqual(action3["instruction"].step, 3)
        self.assertEqual(action3["instruction"].target_weight, 1.0)
        core.complete_pending_sync(
            self.state, btc_quantity=0.099, cash_krw=0, now_utc=self.now
        )
        self.assertEqual(self.state["strategy"]["phase"], "HOLD")
        self.assertEqual(self.state["strategy"]["entry_steps_completed"], 3)

        self.state["strategy"]["last_official_monday"] = None
        no_action = core.create_official_order(
            self.state, block=self.block(progress=0.90), price_krw=self.price, now_utc=self.now
        )
        self.assertIsNone(no_action)
        self.assertIsNone(self.state["telegram"]["pending_sync"])

    def test_exit_requires_next_epoch_and_progress_above_35(self) -> None:
        self.state["strategy"].update(
            {
                "phase": "HOLD",
                "cycle_epoch": 4,
                "entry_steps_completed": 3,
                "has_started_trading": True,
            }
        )
        self.state["account"].update({"btc_quantity": 0.1, "cash_krw": 0})
        before = core.create_official_order(
            self.state,
            block=self.block(epoch=5, progress=0.34),
            price_krw=self.price,
            now_utc=self.now,
        )
        self.assertIsNone(before)
        self.state["strategy"]["last_official_monday"] = None
        after = core.create_official_order(
            self.state,
            block=self.block(epoch=5, progress=0.36),
            price_krw=self.price,
            now_utc=self.now,
        )
        self.assertEqual(after["instruction"].kind, "EXIT")
        self.assertAlmostEqual(after["instruction"].target_weight, 2 / 3)

    def test_hold_deposit_can_schedule_one_correction_buy(self) -> None:
        self.state["strategy"].update(
            {
                "phase": "HOLD",
                "entry_steps_completed": 3,
                "cycle_epoch": 4,
                "has_started_trading": True,
            }
        )
        self.state["account"].update({"btc_quantity": 0.1, "cash_krw": 0})
        result = core.apply_deposit(
            self.state, amount_krw=3_000_000, mode="current", now_utc=self.now
        )
        self.assertTrue(result["correction_buy_scheduled"])
        self.assertTrue(self.state["strategy"]["correction_buy_pending"])
        self.assertEqual(self.state["account"]["cash_krw"], 3_000_000)

        action = core.create_official_order(
            self.state,
            block=self.block(epoch=4, progress=0.90),
            price_krw=self.price,
            now_utc=self.now,
        )
        self.assertEqual(action["instruction"].kind, "CORRECTION")
        self.assertEqual(action["instruction"].target_weight, 1.0)

    def test_exit_deposit_is_next_cycle_only(self) -> None:
        self.state["strategy"]["phase"] = "EXIT"
        self.assertEqual(core.deposit_options(self.state), ["next"])
        result = core.apply_deposit(
            self.state, amount_krw=2_000_000, mode="next", now_utc=self.now
        )
        self.assertFalse(result["correction_buy_scheduled"])
        self.assertEqual(self.state["account"]["reserve_next_krw"], 2_000_000)

    def test_budget_change_only_before_first_trade(self) -> None:
        core.apply_start_budget(self.state, 15_000_000, self.now)
        self.assertEqual(self.state["account"]["cash_krw"], 15_000_000)
        self.state["strategy"]["has_started_trading"] = True
        with self.assertRaises(core.FixedStrategyError):
            core.apply_start_budget(self.state, 20_000_000, self.now)

    def test_sync_completes_third_exit_and_activates_waiting_state(self) -> None:
        self.state["strategy"].update(
            {
                "phase": "EXIT",
                "cycle_epoch": 4,
                "exit_steps_completed": 2,
                "has_started_trading": True,
            }
        )
        self.state["telegram"]["pending_sync"] = {
            "kind": "EXIT",
            "step": 3,
        }
        core.complete_pending_sync(
            self.state, btc_quantity=0, cash_krw=50_000_000, now_utc=self.now
        )
        self.assertEqual(self.state["strategy"]["phase"], "WAITING_ENTRY")
        self.assertEqual(self.state["strategy"]["completed_cycles"], 1)
        self.assertIsNone(self.state["telegram"]["pending_sync"])

    def test_state_version_mismatch_resets_to_new_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"schema_version": 1}', encoding="utf-8")
            state, reset = core.load_state(path, now_utc=self.now)
            self.assertTrue(reset)
            self.assertEqual(state["account"]["cash_krw"], 10_000_000)


if __name__ == "__main__":
    unittest.main()
