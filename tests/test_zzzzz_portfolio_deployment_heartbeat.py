from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import btc_clock_hybrid_core as hybrid
import btc_clock_hybrid_runtime as hybrid_runtime
import btc_fixed_advisory as btc_core
import isa_leverage_core as isa_core
import portfolio_operational_alerts as operational
import portfolio_operational_delivery as delivery


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str, *, menu: bool = False, inline_keyboard=None) -> None:
        self.messages.append(str(text))


class PortfolioDeploymentHeartbeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        hybrid_runtime.install()
        delivery.install()

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

    @staticmethod
    def _results() -> tuple[dict[str, Any], dict[str, Any]]:
        height = 4 * hybrid.INTERVAL + 124_000
        return (
            {
                "data_status": "ok",
                "price_krw": 100_000_000.0,
                "block_height": height,
                "cycle_progress": (height % hybrid.INTERVAL) / hybrid.INTERVAL,
                "phase": "WAITING_ENTRY",
                "pending": "대기 중인 작업 없음",
            },
            {
                "data_status": "ok",
                "initial_completed": False,
                "message_count": 0,
            },
        )

    @staticmethod
    def _write_states(btc_path: Path, isa_path: Path, now: datetime) -> None:
        btc = btc_core._initial_state(now)
        btc["account"]["last_price_krw"] = 100_000_000.0
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
        isa_core.save_state(isa_path, isa, now_utc=now)

    def test_weekend_push_sends_first_heartbeat(self) -> None:
        saturday = datetime(2026, 8, 29, 0, 17, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            btc_path = Path(directory) / "btc.json"
            isa_path = Path(directory) / "isa.json"
            self._write_states(btc_path, isa_path, saturday)
            btc_result, isa_result = self._results()
            client = FakeClient()
            with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "push"}, clear=False):
                result = operational.process_operational_alerts(
                    btc_state_path=btc_path,
                    isa_state_path=isa_path,
                    btc_result=btc_result,
                    isa_result=isa_result,
                    now_utc=saturday,
                    client=client,
                    quote_fetcher=self._quotes,
                    fx_fetcher=self._fx,
                )
            self.assertTrue(result["heartbeat_sent"])
            self.assertEqual(result["message_count"], 1)
            self.assertIn("Quant Guardian 주간 heartbeat", client.messages[0])
            self.assertIn("ISA 초기매수가 아직 완료 처리되지 않았습니다", client.messages[0])

    def test_weekend_schedule_remains_silent(self) -> None:
        saturday = datetime(2026, 8, 29, 0, 17, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            btc_path = Path(directory) / "btc.json"
            isa_path = Path(directory) / "isa.json"
            self._write_states(btc_path, isa_path, saturday)
            btc_result, isa_result = self._results()
            client = FakeClient()
            with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}, clear=False):
                result = operational.process_operational_alerts(
                    btc_state_path=btc_path,
                    isa_state_path=isa_path,
                    btc_result=btc_result,
                    isa_result=isa_result,
                    now_utc=saturday,
                    client=client,
                    quote_fetcher=self._quotes,
                    fx_fetcher=self._fx,
                )
            self.assertFalse(result["heartbeat_sent"])
            self.assertEqual(client.messages, [])


if __name__ == "__main__":
    unittest.main()
