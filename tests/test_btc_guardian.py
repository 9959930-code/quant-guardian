from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import pandas as pd

from btc_guardian import (
    BtcDataError,
    build_halving_context,
    calculate_halving_context,
    check_coinmetrics_catalog,
    closed_yahoo_daily_frame,
    derive_onchain_values,
    load_config,
    parse_upbit_candles,
    phase_for_progress,
    theoretical_supply_btc,
)


def upbit_row(day: str, *, open_price: float = 100.0, close_price: float = 110.0) -> dict:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": f"{day}T00:00:00",
        "opening_price": open_price,
        "high_price": max(open_price, close_price) + 5,
        "low_price": min(open_price, close_price) - 5,
        "trade_price": close_price,
        "candle_acc_trade_volume": 12.5,
        "candle_acc_trade_price": 1_300.0,
        "timestamp": 123456789,
    }


class FakeBlockProvider:
    def __init__(self, name: str, tip: int, genesis: datetime) -> None:
        self.name = name
        self._tip = tip
        self._genesis = genesis

    def tip_height(self) -> int:
        return self._tip

    def block_time(self, height: int) -> datetime:
        return self._genesis + timedelta(minutes=10 * height)


class UpbitCandleTests(unittest.TestCase):
    def test_marks_only_closed_utc_candle_as_final(self) -> None:
        now = datetime(2026, 8, 9, 1, tzinfo=UTC)
        candles = parse_upbit_candles(
            [upbit_row("2026-08-09"), upbit_row("2026-08-08")],
            fetched_at_utc=now,
            as_of_utc=now,
        )

        self.assertFalse(candles[0].is_final)
        self.assertTrue(candles[1].is_final)
        self.assertEqual(candles[1].quote_currency, "KRW")

    def test_rejects_inconsistent_ohlc(self) -> None:
        row = upbit_row("2026-08-08")
        row["high_price"] = 90

        with self.assertRaises(BtcDataError):
            parse_upbit_candles([row], as_of_utc=datetime(2026, 8, 10, tzinfo=UTC))


class HalvingTests(unittest.TestCase):
    def test_protocol_math_at_fourth_halving_boundary(self) -> None:
        boundary = datetime(2024, 4, 20, tzinfo=UTC)
        context = calculate_halving_context(
            tip_height=840_000,
            tip_time_utc=boundary,
            epoch_start_time_utc=boundary,
            blocks_per_day_30=144.0,
            blocks_per_day_90=143.5,
            source_primary="test-a",
            source_backup="test-b",
            verified=True,
        )

        self.assertEqual(context.epoch, 4)
        self.assertEqual(context.next_halving_height, 1_050_000)
        self.assertEqual(context.blocks_since_halving, 0)
        self.assertAlmostEqual(context.block_subsidy_btc, 3.125)
        self.assertAlmostEqual(context.cycle_progress, 0.0)
        self.assertEqual(context.phase_label, "HALVING_TRANSITION")
        self.assertGreater(context.annualized_new_supply_pct, 0.5)
        self.assertLess(context.annualized_new_supply_pct, 1.5)

    def test_phase_boundaries_are_non_overlapping(self) -> None:
        self.assertEqual(phase_for_progress(0.07999), "HALVING_TRANSITION")
        self.assertEqual(phase_for_progress(0.08), "POST_HALVING_EXPANSION")
        self.assertEqual(phase_for_progress(0.50), "CONTRACTION_RECOVERY")
        self.assertEqual(phase_for_progress(0.999), "PRE_HALVING_ACCUMULATION")

    def test_two_block_sources_use_agreed_height(self) -> None:
        genesis = datetime(2009, 1, 3, tzinfo=UTC)
        context = build_halving_context(
            FakeBlockProvider("primary", 900_001, genesis),
            FakeBlockProvider("backup", 900_000, genesis),
            max_height_gap=3,
        )

        self.assertTrue(context.verified)
        self.assertEqual(context.tip_height, 900_000)
        self.assertAlmostEqual(context.blocks_per_day_30, 144.0)
        self.assertEqual(context.source_backup, "backup")

    def test_rejects_large_block_height_disagreement(self) -> None:
        genesis = datetime(2009, 1, 3, tzinfo=UTC)

        with self.assertRaises(BtcDataError):
            build_halving_context(
                FakeBlockProvider("primary", 900_010, genesis),
                FakeBlockProvider("backup", 900_000, genesis),
                max_height_gap=3,
            )

    def test_theoretical_supply_respects_halving(self) -> None:
        self.assertEqual(theoretical_supply_btc(209_999), 10_500_000)
        self.assertEqual(theoretical_supply_btc(210_000), 10_500_025)


class CoinMetricsTests(unittest.TestCase):
    def test_catalog_checks_daily_frequency(self) -> None:
        payload = {
            "data": [
                {
                    "asset": "btc",
                    "metrics": [
                        {"metric": "CapMVRVCur", "frequencies": [{"frequency": "1d"}]},
                        {"metric": "HashRate", "frequencies": [{"frequency": "1b"}]},
                    ],
                }
            ]
        }

        result = check_coinmetrics_catalog(payload, ["CapMVRVCur", "HashRate", "SOPR"])

        self.assertEqual(result["available"], ["CapMVRVCur"])
        self.assertEqual(result["missing"], ["HashRate", "SOPR"])

    def test_mvrv_derivatives_share_one_evidence_family(self) -> None:
        result = derive_onchain_values(
            {
                "CapMVRVCur": 2.0,
                "CapMrktCurUSD": 2_000.0,
                "SplyCur": 10.0,
                "IssTotUSD": 30.0,
                "FeeTotNtv": 0.01,
                "PriceUSD": 100.0,
            }
        )

        self.assertEqual(result["realized_cap_usd"], 1_000.0)
        self.assertEqual(result["realized_price_usd"], 100.0)
        self.assertEqual(result["nupl"], 0.5)
        self.assertEqual(result["miner_revenue_usd"], 31.0)
        self.assertEqual(result["valuation_evidence_family"], "mvrv-derived")


class ClosedCandleTests(unittest.TestCase):
    def test_yahoo_current_utc_day_is_excluded(self) -> None:
        frame = pd.DataFrame(
            {"Close": [100.0, 110.0]},
            index=pd.to_datetime(["2026-08-07", "2026-08-08"]),
        )

        closed = closed_yahoo_daily_frame(frame, datetime(2026, 8, 8, 18, tzinfo=UTC))

        self.assertEqual(list(closed.index), [pd.Timestamp("2026-08-07")])


class RuntimeSafetyTests(unittest.TestCase):
    def test_btc_runtime_starts_in_shadow_without_orders(self) -> None:
        btc = load_config()["btc"]

        self.assertEqual(btc["run_mode"], "shadow")
        self.assertFalse(btc["auto_order"])


if __name__ == "__main__":
    unittest.main()
