from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import btc_clock_hybrid_core as hybrid
import btc_clock_hybrid_runtime as hybrid_runtime
import btc_fixed_advisory as btc_core
import isa_leverage_core as isa_core
import portfolio_operational_alerts as operational


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def send_message(self, text: str, *, menu: bool = False, inline_keyboard=None) -> None:
        self.messages.append((str(text), bool(menu)))


class PortfolioOperationalAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep legacy core tests isolated during unittest discovery.
        hybrid_runtime.install()

    def setUp(self) -> None:
        self.monday = datetime(2026, 8, 31, 0, 17, tzinfo=UTC)

    def _paths(self, directory: str) -> tuple[Path, Path]:
        return (
            Path(directory) / "btc_state.json",
            Path(directory) / "isa_state.json",
        )

    def _write_states(
        self,
        btc_path: Path,
        isa_path: Path,
        *,
        now: datetime,
        initial_completed: bool = False,
    ) -> None:
        btc = btc_core._initial_state(now)
        btc["account"]["last_price_krw"] = 100_000_000.0
        btc["strategy"].update(
            {
                "last_block_height": 4 * hybrid.INTERVAL + 124_000,
                "last_block_epoch": 4,
                "last_block_progress": 124_000 / hybrid.INTERVAL,
            }
        )
        btc_core.save_state(btc_path, btc)

        isa = isa_core.new_state(now)
        isa["data"].update(
            {
                "status": "ok",
                "last_tiger_price_krw": 40_000.0,
                "last_quote_date": "2026-08-28",
                "last_fx_z": -0.50,
            }
        )
        if initial_completed:
            isa["account"].update(
                {"tiger_quantity": 250.0, "tiger_invested_krw": 10_000_000.0}
            )
            isa["strategy"].update(
                {
                    "initial_completed": True,
                    "initial_completed_at_utc": isa_core.iso_utc(now),
                    "monthly_start_period": "2026-09",
                }
            )
        isa_core.save_state(isa_path, isa, now_utc=now)

    @staticmethod
    def _btc_result() -> dict[str, Any]:
        height = 4 * hybrid.INTERVAL + 124_000
        return {
            "data_status": "ok",
            "price_krw": 100_000_000.0,
            "block_height": height,
            "cycle_progress": (height % hybrid.INTERVAL) / hybrid.INTERVAL,
            "phase": "WAITING_ENTRY",
            "pending": "대기 중인 작업 없음",
        }

    @staticmethod
    def _isa_result(initial_completed: bool = False) -> dict[str, Any]:
        return {
            "data_status": "ok",
            "initial_completed": initial_completed,
            "message_count": 0,
        }

    @staticmethod
    def _quotes() -> dict[str, isa_core.QuoteSnapshot]:
        rows = {
            "442580": ("PLUS 글로벌HBM반도체", 100_000.0),
            "0048J0": ("KODEX 미국머니마켓액티브", 14_500.0),
            "379810": ("KODEX 미국나스닥100", 30_000.0),
            isa_core.TIGER_CODE: (isa_core.TIGER_NAME, 40_000.0),
        }
        return {
            code: isa_core.QuoteSnapshot(code, name, "2026-08-28", close)
            for code, (name, close) in rows.items()
        }

    @staticmethod
    def _fx() -> isa_core.FxSnapshot:
        return isa_core.FxSnapshot("2026-08-28", 1_400.0, -0.50, "NORMAL")

    def test_weekly_heartbeat_includes_isa_initial_purchase_reminder_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            btc_path, isa_path = self._paths(directory)
            self._write_states(btc_path, isa_path, now=self.monday)
            client = FakeClient()

            first = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=self.monday,
                event_name="push",
                client=client,
                quote_fetcher=self._quotes,
                fx_fetcher=self._fx,
            )
            self.assertTrue(first["heartbeat_sent"])
            self.assertEqual(first["message_count"], 1)
            text = client.messages[0][0]
            self.assertIn("주간 heartbeat", text)
            self.assertIn("ISA 초기매수가 아직 완료 처리되지 않았습니다", text)
            self.assertIn("초기 주문 검토수량: 250주", text)
            self.assertIn("🔄 ISA 잔고 동기화", text)

            second = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=self.monday + timedelta(hours=1),
                event_name="schedule",
                client=client,
                quote_fetcher=self._quotes,
                fx_fetcher=self._fx,
            )
            self.assertFalse(second["heartbeat_sent"])
            self.assertEqual(second["message_count"], 0)
            self.assertEqual(len(client.messages), 1)

    def test_completed_isa_heartbeat_has_no_initial_purchase_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            btc_path, isa_path = self._paths(directory)
            self._write_states(
                btc_path,
                isa_path,
                now=self.monday,
                initial_completed=True,
            )
            client = FakeClient()
            result = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(True),
                now_utc=self.monday,
                event_name="schedule",
                client=client,
                quote_fetcher=self._quotes,
                fx_fetcher=self._fx,
            )
            self.assertTrue(result["heartbeat_sent"])
            text = client.messages[0][0]
            self.assertIn("초기 1,000만원 매수: 완료", text)
            self.assertNotIn("아직 완료 처리되지 않았습니다", text)

    def test_schedule_gap_is_reported_after_recovery_with_cooldown(self) -> None:
        tuesday = datetime(2026, 9, 1, 0, 17, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            btc_path, isa_path = self._paths(directory)
            self._write_states(btc_path, isa_path, now=tuesday)
            btc, _ = btc_core.load_state(btc_path, now_utc=tuesday)
            btc["operations"] = {
                "schema_version": 1,
                "last_scheduled_run_at_utc": btc_core.iso_utc(tuesday),
                "last_weekly_heartbeat_period": operational._week_key(
                    tuesday.astimezone(btc_core.KST)
                ),
                "last_weekly_heartbeat_at_utc": btc_core.iso_utc(tuesday),
                "last_gap_alert_at_utc": None,
            }
            btc_core.save_state(btc_path, btc)
            client = FakeClient()

            recovered = tuesday + timedelta(hours=2)
            first = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=recovered,
                event_name="schedule",
                client=client,
            )
            self.assertTrue(first["gap_alert_sent"])
            self.assertEqual(first["schedule_gap_minutes"], 120)
            self.assertIn("자동화 지연 감지 · 복구", client.messages[0][0])
            self.assertIn("2시간 0분", client.messages[0][0])

            second = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=recovered + timedelta(hours=2),
                event_name="schedule",
                client=client,
            )
            self.assertFalse(second["gap_alert_sent"])
            self.assertEqual(len(client.messages), 1)

    def test_operational_fields_preserve_asset_and_telegram_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            btc_path, isa_path = self._paths(directory)
            self._write_states(btc_path, isa_path, now=self.monday)
            btc, _ = btc_core.load_state(btc_path, now_utc=self.monday)
            btc["account"].update({"cash_krw": 7_000_000.0, "btc_quantity": 0.03})
            btc["telegram"]["last_update_id"] = 12345
            btc_core.save_state(btc_path, btc)

            operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=self.monday,
                event_name="push",
                client=FakeClient(),
                quote_fetcher=self._quotes,
                fx_fetcher=self._fx,
            )
            loaded, _ = btc_core.load_state(btc_path, now_utc=self.monday)
            self.assertEqual(loaded["account"]["cash_krw"], 7_000_000.0)
            self.assertEqual(loaded["account"]["btc_quantity"], 0.03)
            self.assertEqual(loaded["telegram"]["last_update_id"], 12345)
            self.assertEqual(loaded["operations"]["schema_version"], 1)
            self.assertEqual(
                loaded["operations"]["last_weekly_heartbeat_period"], "2026-W36"
            )

    def test_weekend_does_not_send_weekly_heartbeat(self) -> None:
        saturday = datetime(2026, 9, 5, 0, 17, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            btc_path, isa_path = self._paths(directory)
            self._write_states(btc_path, isa_path, now=saturday)
            client = FakeClient()
            result = operational.process_operational_alerts(
                btc_state_path=btc_path,
                isa_state_path=isa_path,
                btc_result=self._btc_result(),
                isa_result=self._isa_result(),
                now_utc=saturday,
                event_name="schedule",
                client=client,
            )
            self.assertFalse(result["heartbeat_sent"])
            self.assertEqual(client.messages, [])


if __name__ == "__main__":
    unittest.main()
