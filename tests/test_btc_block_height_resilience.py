from __future__ import annotations

import threading
import unittest
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import btc_clock_hybrid_runtime as runtime
import btc_fixed_advisory as core


class BlockHeightProviderResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 21, 11, 34, tzinfo=UTC)

    def request_side_effect(
        self,
        responses: dict[str, Any],
    ) -> tuple[Any, dict[str, int]]:
        lock = threading.Lock()
        calls: dict[str, int] = defaultdict(int)

        def request(url: str, timeout: int = 20) -> str:
            self.assertEqual(
                timeout,
                runtime.BLOCK_HEIGHT_TIMEOUT_SECONDS,
            )
            with lock:
                index = calls[url]
                calls[url] += 1
                response = responses[url]
                if isinstance(response, list):
                    response = response[min(index, len(response) - 1)]
            if isinstance(response, BaseException):
                raise response
            return str(response)

        return request, calls

    def fetch(self, responses: dict[str, Any]) -> core.BlockContext:
        request, _ = self.request_side_effect(responses)
        with (
            patch.object(core, "_request_text", side_effect=request),
            patch.object(runtime.time, "sleep", return_value=None),
        ):
            return runtime.fetch_resilient_block_context(self.now)

    def test_one_provider_failure_uses_two_provider_quorum(self) -> None:
        block = self.fetch(
            {
                runtime.BLOCK_HEIGHT_PROVIDERS[0][1]: OSError(
                    101,
                    "Network is unreachable",
                ),
                runtime.BLOCK_HEIGHT_PROVIDERS[1][1]: "963427",
                runtime.BLOCK_HEIGHT_PROVIDERS[2][1]: "963428",
            }
        )

        self.assertEqual(block.height, 963427)
        self.assertEqual(block.mempool_height, 963427)
        self.assertEqual(block.blockstream_height, 963427)
        self.assertEqual(block.epoch, 4)

    def test_outlier_provider_is_excluded_from_quorum(self) -> None:
        block = self.fetch(
            {
                runtime.BLOCK_HEIGHT_PROVIDERS[0][1]: "963430",
                runtime.BLOCK_HEIGHT_PROVIDERS[1][1]: "963431",
                runtime.BLOCK_HEIGHT_PROVIDERS[2][1]: "900000",
            }
        )

        self.assertEqual(block.height, 963430)
        self.assertEqual(block.mempool_height, 963430)
        self.assertEqual(block.blockstream_height, 963431)

    def test_single_provider_is_rejected_with_diagnostics(self) -> None:
        request, _ = self.request_side_effect(
            {
                runtime.BLOCK_HEIGHT_PROVIDERS[0][1]: "963430",
                runtime.BLOCK_HEIGHT_PROVIDERS[1][1]: OSError(
                    101,
                    "Network is unreachable",
                ),
                runtime.BLOCK_HEIGHT_PROVIDERS[2][1]: TimeoutError(
                    "timed out"
                ),
            }
        )

        with (
            patch.object(core, "_request_text", side_effect=request),
            patch.object(runtime.time, "sleep", return_value=None),
            self.assertRaises(core.FixedStrategyError) as caught,
        ):
            runtime.fetch_resilient_block_context(self.now)

        message = str(caught.exception)
        self.assertIn("2개 이상 합의", message)
        self.assertIn("mempool.space=963,430", message)
        self.assertIn("blockstream.info=실패", message)
        self.assertIn("blockchain.info=실패", message)

    def test_transient_failure_is_retried(self) -> None:
        request, calls = self.request_side_effect(
            {
                runtime.BLOCK_HEIGHT_PROVIDERS[0][1]: [
                    OSError(101, "Network is unreachable"),
                    "963430",
                ],
                runtime.BLOCK_HEIGHT_PROVIDERS[1][1]: "963431",
                runtime.BLOCK_HEIGHT_PROVIDERS[2][1]: OSError(
                    101,
                    "Network is unreachable",
                ),
            }
        )

        with (
            patch.object(core, "_request_text", side_effect=request),
            patch.object(runtime.time, "sleep", return_value=None),
        ):
            block = runtime.fetch_resilient_block_context(self.now)

        self.assertEqual(block.height, 963430)
        self.assertEqual(
            calls[runtime.BLOCK_HEIGHT_PROVIDERS[0][1]],
            2,
        )


if __name__ == "__main__":
    unittest.main()
