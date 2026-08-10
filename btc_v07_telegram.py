from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def load_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"BTC advisory payload를 읽지 못했습니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("BTC advisory payload는 JSON object여야 합니다.")
    return payload


def send_message(token: str, chat_id: str, text: str) -> None:
    if len(text) > 4096:
        text = text[:4050] + "\n\n[메시지가 길어 일부를 생략했습니다.]"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send BTC v0.7 advisory to Telegram")
    parser.add_argument("--data-file", type=Path, default=Path("output/btc_v07_advisory.json"))
    parser.add_argument("--soft-fail", action="store_true")
    args = parser.parse_args()

    try:
        payload = load_payload(args.data_file)
        if not bool(payload.get("should_notify", False)):
            print("BTC v0.7 알림 조건이 없어 전송하지 않습니다.")
            return 0
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID GitHub Secret이 없습니다."
            )
        message = str(payload.get("message", "")).strip()
        if not message:
            raise RuntimeError("BTC v0.7 메시지가 비어 있습니다.")
        send_message(token, chat_id, message)
        print("BTC v0.7 Telegram 알림을 보냈습니다.")
        return 0
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"BTC v0.7 Telegram 알림 실패: {exc}", file=sys.stderr)
        return 0 if args.soft_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
