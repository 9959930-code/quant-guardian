from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

import btc_gpt_primary_policy as p


class GPTPrimaryPolicyTests(unittest.TestCase):
    def test_mainnet_thresholds(self):
        self.assertFalse(p.snapshot(976_499).entry_threshold_reached)
        self.assertTrue(p.snapshot(976_500).entry_threshold_reached)
        self.assertEqual(p.snapshot(976_500).epoch, 4)
        self.assertEqual(p.snapshot(976_500).offset, 136_500)

    def test_exit_35_warning_and_37_all_only_next_epoch(self):
        self.assertEqual(p.exit_status(1_123_500, confirmed_entry_epoch=4), "EXIT_WARNING_ONLY")
        self.assertEqual(p.exit_status(1_127_699, confirmed_entry_epoch=4), "EXIT_WARNING_ONLY")
        self.assertEqual(p.exit_status(1_127_700, confirmed_entry_epoch=4), "EXIT_REVIEW_ALL")
        self.assertEqual(p.exit_status(1_127_700, confirmed_entry_epoch=None), "NO_EXIT_SIGNAL")
        self.assertEqual(p.exit_status(1_127_700, confirmed_entry_epoch=5), "NO_EXIT_SIGNAL")

    def test_monday_0917_and_only_confirmed_next_tranche(self):
        monday = datetime(2026, 11, 23, 9, 17, tzinfo=ZoneInfo("Asia/Seoul"))
        gate = dict(entry_steps_completed=0, account_verified=True,
                    pending_order=False, prior_tranche_fill_verified=True)
        self.assertEqual(p.review_step(976_500, monday, **gate), ("ENTRY_REVIEW_1", 0.2))
        self.assertEqual(p.review_step(976_499, monday, **gate)[0], "WAIT_65_PERCENT")
        self.assertEqual(p.review_step(976_500, monday.replace(minute=16), **gate)[0], "WAIT_OFFICIAL_MONDAY")
        self.assertEqual(p.review_step(976_500, monday.replace(day=24), **gate)[0], "WAIT_OFFICIAL_MONDAY")
        self.assertEqual(p.review_step(976_500, monday, **{**gate, "pending_order":None})[0], "DEFERRED_ACCOUNT_OR_PENDING")
        self.assertEqual(p.review_step(976_500, monday, **{**gate, "entry_steps_completed":2, "prior_tranche_fill_verified":False})[0], "DEFERRED_PRIOR_FILL")
        self.assertEqual(p.review_step(976_500, monday, **{**gate, "entry_steps_completed":4}), ("ENTRY_REVIEW_5", 1.0))
        self.assertEqual(p.review_step(976_500, monday, **{**gate, "entry_steps_completed":5}), ("HOLD", 1.0))

    def test_first_official_monday_after_event(self):
        kst = ZoneInfo("Asia/Seoul")
        monday = datetime(2026, 11, 23, 9, 17, tzinfo=kst)
        self.assertEqual(p.first_official_monday_after(monday), monday)
        self.assertEqual(p.first_official_monday_after(monday.replace(minute=18)).date().isoformat(), "2026-11-30")
        self.assertEqual(p.first_official_monday_after(monday.replace(day=22)), monday)

    def test_cumulative_weight_gap(self):
        self.assertEqual(p.target_adjustment_krw(.4, btc_valued_krw=2_400_000, strategy_cash_krw=8_000_000), 1_760_000)
        self.assertEqual(p.target_adjustment_krw(0, btc_valued_krw=2_400_000, strategy_cash_krw=0), -2_400_000)


if __name__ == "__main__":
    unittest.main()
