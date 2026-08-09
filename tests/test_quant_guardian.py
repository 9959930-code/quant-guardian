from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quant_guardian import (
    _equity_weight_map,
    _profile_from_market,
    load_config,
    technical_snapshot,
)
from telegram_notify import build_message, parse_capital


def synthetic_price(direction: float, periods: int = 620) -> pd.DataFrame:
    index = pd.bdate_range("2022-01-03", periods=periods)
    trend = np.linspace(100, 100 + direction, periods)
    wave = np.sin(np.arange(periods) / 9) * 2.5
    close = pd.Series(trend + wave, index=index)
    return pd.DataFrame(
        {
            "Open": close.shift(1).fillna(close.iloc[0]),
            "High": close + 1.2,
            "Low": close - 1.2,
            "Close": close,
            "Volume": 2_000_000 + np.arange(periods) * 100,
        },
        index=index,
    )


class TechnicalSnapshotTests(unittest.TestCase):
    def test_indicator_families_are_bounded_and_complete(self) -> None:
        price = synthetic_price(90)
        result = technical_snapshot("TEST", price, price["Close"], 18)

        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertGreaterEqual(result["trend_score"], 0)
        self.assertLessEqual(result["trend_score"], 40)
        self.assertLessEqual(result["momentum_score"], 25)
        self.assertLessEqual(result["timing_score"], 20)
        self.assertLessEqual(result["risk_score"], 15)
        self.assertIn(result["ichimoku_state"], {"구름대 위", "구름대 안", "구름대 아래"})
        self.assertIn(
            result["action"],
            {"신규매수", "분할매수", "보유·관찰", "보유·추격매수 대기", "비중축소", "매도·대기"},
        )

    def test_falling_long_term_trend_does_not_create_buy_signal(self) -> None:
        price = synthetic_price(-65)
        result = technical_snapshot("DOWN", price, price["Close"], 34)

        self.assertFalse(result["above_200d"])
        self.assertIn(result["action"], {"비중축소", "매도·대기"})


class PortfolioRuleTests(unittest.TestCase):
    def test_satellite_weight_is_capped_and_broad_index_is_included(self) -> None:
        cfg = load_config()
        scores = pd.DataFrame(
            [
                {"ticker": "SMH", "score": 92.0, "action": "신규매수"},
                {"ticker": "SPY", "score": 82.0, "action": "분할매수"},
                {"ticker": "QQQ", "score": 78.0, "action": "분할매수"},
            ]
        )
        weights = _equity_weight_map(scores, 0.90, cfg)

        self.assertIn("SPY", weights)
        self.assertIn("SMH", weights)
        self.assertLessEqual(weights["SMH"], cfg["qg_core"]["max_satellite_weight"])
        self.assertAlmostEqual(sum(weights.values()), 0.90)

    def test_both_indexes_below_200_day_line_leads_to_exit(self) -> None:
        cfg = load_config()
        spy = {"score": 80, "above_200d": False}
        qqq = {"score": 80, "above_200d": False}
        profile, score = _profile_from_market(spy, qqq, 35, cfg)

        self.assertEqual(profile, "exit")
        self.assertLess(score, 35)


class TelegramMessageTests(unittest.TestCase):
    def test_korean_action_message_contains_amount_and_no_old_labels(self) -> None:
        payload = {
            "daily_advice": {
                "action": "분할매수",
                "as_of": "2026-08-07",
                "target_equity_weight": 0.90,
                "top_etf_signal": "QQQ",
                "top_etf_execution": "QQQM",
                "top_etf_last": 600,
                "top_etf_timing": "이번 주 2회로 나눕니다.",
                "positives": ["200일선 위"],
                "cautions": ["추격 주의"],
            },
            "market_decision": {"as_of": "2026-08-07"},
            "top_etfs": [{"ticker": "QQQ", "score": 77, "rsi14": 60, "ichimoku_state": "구름대 위", "above_200d": True}],
            "plan": [{"asset": "QQQM", "weight": 0.50, "action": "분할매수"}],
            "qg_core_metrics": {"cagr_pct": 15.2, "mdd_pct": -21.4},
            "benchmarks": {"spy_cagr_pct": 15.5, "qqq_cagr_pct": 20.5},
        }
        message = build_message(payload, "https://example.com", 10_000_000)

        self.assertIn("오늘 할 일", message)
        self.assertIn("목표 5,000,000원", message)
        self.assertIn("실행 시점", message)
        self.assertNotIn("공격", message)
        self.assertNotIn("중립", message)
        self.assertNotIn("방어", message)

    def test_capital_parser_accepts_commas_and_rejects_invalid_value(self) -> None:
        self.assertEqual(parse_capital("10,000,000"), 10_000_000)
        self.assertIsNone(parse_capital("모름"))
        self.assertIsNone(parse_capital("0"))


if __name__ == "__main__":
    unittest.main()
