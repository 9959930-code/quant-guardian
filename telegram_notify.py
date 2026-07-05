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
        request = Request(url, headers={"User-Agent": "quant-guardian-telegram/0.1"})
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    data_path = Path(path or "output/daily.json")
    return json.loads(data_path.read_text(encoding="utf-8-sig"))


def pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def build_message(payload: dict, site_url: str) -> str:
    advice = payload.get("daily_advice", {})
    regime = payload.get("regime", {})
    signal = payload.get("signal", {})
    candidates = advice.get("top_candidates", [])[:5]

    candidate_text = ", ".join(
        f"{item['ticker']}({item.get('score', 0):.1f})" for item in candidates
    ) or "신규 강한 후보 없음"
    steps = "\n".join(f"- {step}" for step in advice.get("steps", [])[:3])

    return "\n".join(
        [
            "퀀트 가디언 Daily",
            "",
            f"오늘의 행동: {advice.get('action', '유지/관찰')}",
            f"시장 모드: {regime.get('regime', '-')} ({regime.get('score', '-')}점)",
            f"ETF 코어: {signal.get('current_signal', '-')}",
            f"12개월 모멘텀: {pct(signal.get('latest_momentum_pct'))}",
            f"상위 후보: {candidate_text}",
            f"기준일: {signal.get('as_of', '-')}",
            f"생성: {payload.get('generated_at', '-')}",
            "",
            steps,
            "",
            "자동주문 아님. 실제 매매 전 직접 확인.",
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
