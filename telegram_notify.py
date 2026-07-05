from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://9959930-code.github.io/quant-guardian/"


def load_daily_payload(path: str | None, url: str | None) -> dict:
    if url:
        request = Request(url, headers={"User-Agent": "quant-guardian-telegram/0.2"})
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    data_path = Path(path or "output/daily.json")
    return json.loads(data_path.read_text(encoding="utf-8-sig"))


def pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def signed_pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.1f}%"


def build_message(payload: dict, site_url: str) -> str:
    advice = payload.get("daily_advice", {})
    regime = payload.get("regime", {})
    metrics = payload.get("qg_core_metrics", {})
    benchmarks = payload.get("benchmarks", {})
    candidates = advice.get("top_candidates", [])[:5]
    plan = payload.get("plan", [])[:8]

    candidate_lines = [
        (
            f"{idx}. {item['ticker']} | {item.get('sector', '-')} | "
            f"{item.get('score', 0):.1f}점 | 12-1M {signed_pct(item.get('mom_12_1_pct'))} | "
            f"RSI {item.get('rsi14', '-')}"
        )
        for idx, item in enumerate(candidates, start=1)
    ] or ["강한 신규 후보 없음"]

    plan_lines = [
        f"- {row.get('asset', '-')}: {float(row.get('weight', 0)) * 100:.1f}% ({row.get('type', '-')})"
        for row in plan
    ]

    return "\n".join(
        [
            "[Quant Guardian QG-Core]",
            "",
            f"오늘의 행동: {advice.get('action', '유지 / 관찰')}",
            f"판단 이유: {advice.get('summary', '-')}",
            "",
            "[시장 모드]",
            f"- 기준일: {regime.get('as_of', '-')}",
            f"- 모드: {regime.get('regime', '-')} ({regime.get('score', '-')}/{regime.get('max_score', '-')})",
            f"- ETF 1순위: {advice.get('top_etf_execution', '-')} (신호 기준 {advice.get('top_etf_signal', '-')})",
            f"- ETF 점수: {advice.get('top_etf_score', '-')}",
            "",
            "[QG-Core 비중]",
            *plan_lines,
            "",
            "[상위 대형주 후보]",
            *candidate_lines,
            "",
            "[백테스트 참고]",
            f"- QG-Core CAGR/MDD: {pct(metrics.get('cagr_pct'))} / {pct(metrics.get('mdd_pct'))}",
            f"- SPY CAGR: {pct(benchmarks.get('spy_cagr_pct'))}",
            f"- QQQ CAGR: {pct(benchmarks.get('qqq_cagr_pct'))}",
            "",
            "자동주문 아님. 실제 매매 전 계좌 비중, 세금, 수수료, 환율, 실적 일정을 직접 확인.",
            site_url,
        ]
    ).strip()


def send_message(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Quant Guardian daily summary to Telegram")
    parser.add_argument("--data-file", default="output/daily.json")
    parser.add_argument("--data-url")
    parser.add_argument("--site-url", default=os.getenv("QUANT_GUARDIAN_URL", DEFAULT_URL))
    parser.add_argument("--soft-fail", action="store_true", help="Do not fail the workflow if Telegram fails")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets are not configured; skipping notification.")
        return 0

    try:
        payload = load_daily_payload(args.data_file, args.data_url)
        message = build_message(payload, args.site_url)
        send_message(token, chat_id, message)
        print("Telegram notification sent.")
        return 0
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"Telegram notification failed: {exc}", file=sys.stderr)
        return 0 if args.soft_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
