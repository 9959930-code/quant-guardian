from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from isa_leverage_core import (
    KST,
    STATE_SCHEMA_VERSION,
    STRATEGY_VERSION,
    TIGER_CODE,
    IsaStrategyError,
    append_audit,
    apply_manual_sync,
    fetch_fx_snapshot,
    fetch_quotes,
    is_monthly_plan_due,
    load_state,
    positive_float,
    save_state,
    utc_now,
)
from isa_leverage_messages import (
    fx_zone_change_message,
    initial_plan_message,
    monthly_plan_message,
    status_message,
)


class TelegramClient:
    """Outbound-only client; deliberately never consumes BTC bot updates."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token, self.chat_id = token.strip(), str(chat_id).strip()
        if not self.token or not self.chat_id:
            raise IsaStrategyError("Telegram 시크릿이 없습니다.")

    def send_message(self, text: str) -> None:
        message = str(text).strip()
        if len(message) > 4096:
            message = message[:4040] + "\n\n[길이 제한으로 일부 생략]"
        body = urlencode(
            {"chat_id": self.chat_id, "text": message, "disable_web_page_preview": "true"}
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise IsaStrategyError(f"Telegram 전송 실패: {exc}") from exc
        if not payload.get("ok"):
            raise IsaStrategyError(f"Telegram API ok=false: {payload}")


def run_service(
    *,
    state_path: Path,
    reset_state: bool = False,
    force_status: bool = False,
    resend_initial: bool = False,
    mark_initial_completed: bool = False,
    tiger_quantity: float | None = None,
    tiger_invested_krw: float | None = None,
    isa_total_contributions_krw: float | None = None,
    now_utc: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    now_kst = now.astimezone(KST)
    state, initialized_new = load_state(state_path, reset=reset_state, now_utc=now)
    manual_sync = apply_manual_sync(
        state,
        tiger_quantity=tiger_quantity,
        tiger_invested_krw=tiger_invested_krw,
        isa_total_contributions_krw=isa_total_contributions_krw,
        mark_initial_completed=mark_initial_completed,
        now_utc=now,
    )

    messages: list[str] = []
    quotes = None
    fx = None
    data_error: str | None = None
    previous_status = str(state["data"].get("status", "unknown"))
    try:
        quotes = fetch_quotes()
        state["data"]["last_quote_date"] = quotes[TIGER_CODE].date
        state["data"]["last_tiger_price_krw"] = quotes[TIGER_CODE].close
        state["data"]["status"] = "ok"
        state["data"]["last_error"] = None
        try:
            fx = fetch_fx_snapshot()
            state["data"]["last_fx_date"] = fx.date
            state["data"]["last_fx_z"] = fx.z_52w
        except IsaStrategyError as exc:
            state["data"]["last_error"] = str(exc)
    except IsaStrategyError as exc:
        data_error = str(exc)
        state["data"]["status"] = "error"
        state["data"]["last_error"] = data_error

    state["data"]["last_check_at_utc"] = now.isoformat()

    if quotes is not None:
        strategy = state["strategy"]
        if initialized_new or resend_initial or not bool(strategy.get("initial_plan_sent")):
            messages.append(initial_plan_message(state, quotes, fx))
            strategy["initial_plan_sent"] = True
            strategy["initial_plan_sent_at_utc"] = now.isoformat()
            append_audit(state, "INITIAL_PLAN_SENT", {}, now)
        if manual_sync:
            messages.append(status_message(state, quotes, fx, title="수동 잔고동기화 완료"))
        if is_monthly_plan_due(
            state, now_kst=now_kst, latest_quote_date=quotes[TIGER_CODE].date
        ):
            period = now_kst.strftime("%Y-%m")
            message = monthly_plan_message(state, quotes, fx, period)
            message += "\n\n[해당 월 미발송분 점검]\n현재 시세 기준이며 과거 가격으로 소급 주문하지 않습니다.\n알림 발송은 매수 완료를 의미하지 않습니다. 이전 달 적립금을 자동 합산하지 않습니다."
            messages.append(message)
            strategy["last_monthly_plan_generated_at_utc"] = now.isoformat()
            strategy["last_monthly_plan_period"] = period
            append_audit(state, "MONTHLY_PLAN_SENT", {"period": period}, now)
        if fx is not None:
            previous_zone = strategy.get("last_fx_zone")
            if previous_zone is not None and previous_zone != fx.zone:
                messages.append(fx_zone_change_message(str(previous_zone), fx))
            strategy["last_fx_zone"] = fx.zone
        if force_status:
            messages.append(status_message(state, quotes, fx, title="수동 상태보고"))

    if data_error is not None and previous_status != "error":
        messages.append(
            "[ISA 데이터 오류]\n"
            + data_error
            + "\n\n새 주문안은 만들지 않고 마지막 상태를 유지합니다. 자동주문은 없습니다."
        )
    elif quotes is not None and previous_status == "error":
        messages.append("[ISA 데이터 정상화]\n국내 ETF 시세 조회가 다시 정상으로 돌아왔습니다.")

    if dry_run:
        for message in messages:
            print("--- TELEGRAM DRY RUN ---")
            print(message)
    else:
        client = TelegramClient(
            os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
        )
        for message in messages:
            client.send_message(message)

    save_state(state_path, state, now_utc=now)
    return {
        "strategy_version": STRATEGY_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "state_path": str(state_path),
        "initialized_new": initialized_new,
        "initial_completed": bool(state["strategy"].get("initial_completed")),
        "last_monthly_plan_period": state["strategy"].get("last_monthly_plan_period"),
        "tiger_quantity": float(state["account"].get("tiger_quantity", 0)),
        "tiger_invested_krw": float(state["account"].get("tiger_invested_krw", 0)),
        "data_status": state["data"].get("status"),
        "data_checked_this_run": True,
        "last_quote_date": state["data"].get("last_quote_date"),
        "fx_zone": state["strategy"].get("last_fx_zone"),
        "message_count": len(messages),
        "auto_order": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ISA TIGER leverage Telegram advisory")
    parser.add_argument(
        "--state-file", type=Path, default=Path("data/state/isa_leverage_state.json")
    )
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--force-status", action="store_true")
    parser.add_argument("--resend-initial", action="store_true")
    parser.add_argument("--mark-initial-completed", action="store_true")
    parser.add_argument("--tiger-quantity")
    parser.add_argument("--tiger-invested-krw")
    parser.add_argument("--isa-total-contributions-krw")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_service(
            state_path=args.state_file,
            reset_state=args.reset_state,
            force_status=args.force_status,
            resend_initial=args.resend_initial,
            mark_initial_completed=args.mark_initial_completed,
            tiger_quantity=positive_float(args.tiger_quantity, "TIGER 수량"),
            tiger_invested_krw=positive_float(args.tiger_invested_krw, "TIGER 누적원금"),
            isa_total_contributions_krw=positive_float(
                args.isa_total_contributions_krw, "ISA 누적 납입원금"
            ),
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except IsaStrategyError as exc:
        print(f"ISA leverage service failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
