from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import btc_fixed_advisory as core


MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 상태 보기"}, {"text": "➕ 추가입금"}],
        [{"text": "🔄 잔고 동기화"}, {"text": "💰 시작예산 변경"}],
        [{"text": "⏳ 대기 작업"}, {"text": "❓ 도움말"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "메뉴를 선택하세요",
}


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        body_params: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                body_params[key] = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                body_params[key] = "true" if value else "false"
            else:
                body_params[key] = str(value)
        request = Request(
            f"{self.base_url}/{method}",
            data=urlencode(body_params).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramError(f"Telegram {method} 실패: {exc}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"Telegram {method} returned ok=false: {payload}")
        return payload.get("result")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        result = self._request(
            "getUpdates",
            {
                "offset": offset,
                "limit": 100,
                "timeout": 0,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        return list(result or [])

    def send_message(
        self,
        text: str,
        *,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        menu: bool = False,
    ) -> None:
        message = str(text).strip()
        if len(message) > 4096:
            message = message[:4040] + "\n\n[길이 제한으로 일부 생략]"
        reply_markup: dict[str, Any] | None = None
        if inline_keyboard is not None:
            reply_markup = {"inline_keyboard": inline_keyboard}
        elif menu:
            reply_markup = MENU_KEYBOARD
        self._request(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": message,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self._request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": False,
            },
        )

    def set_commands(self) -> None:
        self._request(
            "setMyCommands",
            {
                "commands": [
                    {"command": "status", "description": "현재 전략과 계좌 상태"},
                    {"command": "deposit", "description": "추가입금 등록"},
                    {"command": "sync", "description": "Upbit 잔고 동기화"},
                    {"command": "budget", "description": "첫 매수 전 시작예산 변경"},
                    {"command": "pending", "description": "처리 대기 작업 확인"},
                    {"command": "cancel", "description": "입력·확인 작업 취소"},
                ]
            },
        )


def krw(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{round(float(value)):,}원"


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.{digits}f}%"


def number(value: float, digits: int = 8) -> str:
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".") or "0"


def _block_from_state(state: Mapping[str, Any]) -> core.BlockContext | None:
    strategy = state["strategy"]
    height = strategy.get("last_block_height")
    epoch = strategy.get("last_block_epoch")
    progress = strategy.get("last_block_progress")
    if height is None or epoch is None or progress is None:
        return None
    return core.BlockContext(
        height=int(height),
        epoch=int(epoch),
        cycle_progress=float(progress),
        mempool_height=int(height),
        blockstream_height=int(height),
        observed_at_utc=str(state.get("updated_at_utc")),
    )


def _safe_snapshot(state: Mapping[str, Any], price: float | None) -> core.AccountSnapshot | None:
    use_price = price or core.current_price_from_state(state)
    if use_price is None:
        return None
    return core.account_snapshot(state, use_price)


def status_message(
    state: Mapping[str, Any],
    *,
    price: float | None,
    block: core.BlockContext | None,
    title: str = "현재 상태",
) -> str:
    snapshot = _safe_snapshot(state, price)
    strategy = state["strategy"]
    account = state["account"]
    lines = [
        "[Quant Guardian BTC · 고정 6회]",
        f"[{title}]",
        "",
        f"- 전략 단계: {core.stage_label(state)}",
        "- 매수: 33.3% → 66.7% → 100%",
        "- 보유 중 리밸런싱: 없음",
        "- 매도: 66.7% → 33.3% → 0%",
        "- 자동주문: 없음",
    ]
    if block is not None:
        lines.extend(
            [
                "",
                "[반감기]",
                f"- 블록 높이: {block.height:,}",
                f"- 현재 epoch: {block.epoch}",
                f"- 사이클 진행률: {pct(block.cycle_progress, 2)}",
                f"- 다음 조건: {core.next_condition_text(state, block)}",
            ]
        )
    if snapshot is not None:
        lines.extend(
            [
                "",
                "[계좌]",
                f"- 총 납입원금: {krw(snapshot.total_contributions_krw)}",
                f"- 현재 평가액: {krw(snapshot.total_equity_krw)}",
                f"- 원금 대비 손익: {krw(snapshot.profit_krw)}",
                f"- 원금 대비 수익률: {pct(snapshot.return_on_contributions)}",
                f"- 운용 고점 대비 낙폭: {pct(snapshot.current_drawdown)}",
                f"- 운용 중 최대낙폭: {pct(snapshot.max_drawdown)}",
                f"- BTC 수량: {number(snapshot.btc_quantity)} BTC",
                f"- BTC 평가액: {krw(snapshot.btc_value_krw)}",
                f"- 원화 잔액: {krw(snapshot.cash_krw)}",
                f"- 다음 사이클 대기자금: {krw(snapshot.reserve_next_krw)}",
                f"- Upbit 참고가격: {krw(snapshot.price_krw)}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "[계좌]",
                f"- 총 납입원금: {krw(account.get('total_contributions_krw'))}",
                f"- BTC 수량: {number(float(account.get('btc_quantity', 0)))} BTC",
                f"- 원화 잔액: {krw(account.get('cash_krw'))}",
                "- 가격 데이터가 없어 평가손익은 표시하지 못했습니다.",
            ]
        )
    lines.extend(["", "[처리 상태]", core.pending_summary(state)])
    return "\n".join(lines)


def initial_message(
    state: Mapping[str, Any],
    *,
    price: float | None,
    block: core.BlockContext | None,
) -> str:
    return (
        status_message(state, price=price, block=block, title="새 전략 등록 완료")
        + "\n\n시작예산은 10,000,000원, 현재 BTC는 0개로 초기화했습니다."
        + "\n명령어를 외울 필요 없이 아래 한글 메뉴를 사용하면 됩니다."
    )


def help_message() -> str:
    return "\n".join(
        [
            "[사용 방법]",
            "",
            "📊 상태 보기: 계좌·반감기·다음 행동 확인",
            "➕ 추가입금: 금액 입력 후 현재/다음 사이클 선택",
            "🔄 잔고 동기화: 실제 Upbit BTC 수량과 원화잔액 입력",
            "💰 시작예산 변경: 첫 매수 전에만 가능",
            "⏳ 대기 작업: 확인·동기화가 남아 있는지 확인",
            "",
            "상태 변경은 한글 버튼과 최종 확인 버튼으로 처리합니다.",
            "Upbit 주문은 자동으로 실행하지 않습니다.",
        ]
    )


def order_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "✅ 주문 완료 · 잔고 동기화", "callback_data": "sync:start"}],
        [{"text": "⏰ 30분 뒤 다시 알림", "callback_data": "sync:later"}],
    ]


def pending_operation_keyboard(operation: Mapping[str, Any]) -> list[list[dict[str, str]]]:
    operation_id = str(operation["id"])
    operation_type = str(operation["type"])
    stage = str(operation.get("stage", "confirm"))
    if operation_type == "deposit" and stage == "choice":
        options = operation.get("payload", {}).get("options", [])
        rows: list[list[dict[str, str]]] = []
        if "current" in options:
            rows.append(
                [{"text": "현재 사이클에 적용", "callback_data": f"dep:current:{operation_id}"}]
            )
        if "next" in options:
            rows.append(
                [{"text": "다음 사이클에 보관", "callback_data": f"dep:next:{operation_id}"}]
            )
        rows.append([{"text": "취소", "callback_data": f"op:cancel:{operation_id}"}])
        return rows
    return [
        [
            {"text": "확정", "callback_data": f"op:confirm:{operation_id}"},
            {"text": "취소", "callback_data": f"op:cancel:{operation_id}"},
        ]
    ]


def pending_operation_message(state: Mapping[str, Any], operation: Mapping[str, Any]) -> str:
    operation_type = str(operation["type"])
    payload = operation.get("payload", {})
    stage = str(operation.get("stage", "confirm"))
    if operation_type == "deposit":
        amount = float(payload["amount_krw"])
        if stage == "choice":
            phase = str(state["strategy"]["phase"])
            context = {
                "HOLD": "3차 매수 완료 후 보유 중입니다. 현재 사이클 적용 시 다음 월요일 보정매수 1회가 발생합니다.",
                "EXIT": "분할매도 중이므로 다음 사이클 대기자금으로만 보관할 수 있습니다.",
                "ENTRY": "3주 분할매수 중이므로 남은 매수 단계의 목표금액에 자동 반영됩니다.",
                "WAITING_ENTRY": "아직 매수 전이므로 다음 진입예산에 즉시 반영됩니다.",
            }.get(phase, "추가입금 처리방식을 선택하세요.")
            return "\n".join(
                [
                    "[추가입금 처리 선택]",
                    f"- 추가입금: {krw(amount)}",
                    f"- 현재 단계: {core.stage_label(state)}",
                    f"- 안내: {context}",
                    "",
                    "아래 버튼으로 처리방식을 선택하세요.",
                ]
            )
        choice = str(operation.get("choice"))
        if choice == "current":
            phase = str(state["strategy"]["phase"])
            if phase == "HOLD":
                effect = "다음 월요일 한 번의 보정매수에 반영"
            elif phase == "ENTRY":
                effect = "남은 2차·3차 매수 목표금액에 반영"
            else:
                effect = "다음 매수 사이클 예산에 반영"
        else:
            effect = "현재 사이클에는 투자하지 않고 다음 사이클 대기자금으로 보관"
        return "\n".join(
            [
                "[추가입금 최종 확인]",
                f"- 금액: {krw(amount)}",
                f"- 처리: {effect}",
                "",
                "확정 버튼을 눌러야 실제 추종계좌에 반영됩니다.",
            ]
        )
    if operation_type == "budget":
        return "\n".join(
            [
                "[시작예산 변경 확인]",
                f"- 새 시작예산: {krw(float(payload['amount_krw']))}",
                "- BTC 0개·현금대기 상태로 다시 설정",
                "",
                "첫 매수 전이라서만 변경할 수 있습니다.",
            ]
        )
    if operation_type == "sync":
        return "\n".join(
            [
                "[Upbit 잔고 동기화 확인]",
                f"- BTC 수량: {number(float(payload['btc_quantity']))} BTC",
                f"- 원화 잔액: {krw(float(payload['cash_krw']))}",
                "",
                "확정하면 다음 단계 주문액을 이 잔고 기준으로 계산합니다.",
            ]
        )
    return "확인 대기 작업이 있습니다."


def order_message(state: Mapping[str, Any], instruction: core.OrderInstruction) -> str:
    account = state["account"]
    side = "매수" if instruction.side == "BUY" else "매도"
    if instruction.kind == "ENTRY":
        title = f"{instruction.step}차 분할매수"
    elif instruction.kind == "EXIT":
        title = f"{instruction.step}차 분할매도"
    else:
        title = "추가입금 보정매수"
    return "\n".join(
        [
            "[Quant Guardian BTC · 고정 6회]",
            f"[{title}]",
            "",
            f"- 행동: {side}",
            f"- 이번 단계 목표 BTC 비중: {pct(instruction.target_weight)}",
            f"- 이번 주문 검토액: {krw(instruction.expected_amount_krw)}",
            f"- 단계 목표 BTC 평가액: {krw(instruction.target_btc_value_krw)}",
            f"- 현재 적용자금: {krw(instruction.active_equity_krw)}",
            f"- Upbit 참고가격: {krw(instruction.reference_price_krw)}",
            f"- 근거: {instruction.reason}",
            "",
            "Upbit에서 직접 주문한 뒤 아래 버튼을 눌러 실제 BTC 수량과 원화 잔액을 동기화하세요.",
            "동기화가 끝나기 전에는 다음 매수·매도 단계를 진행하지 않습니다.",
            f"현재 총 납입원금: {krw(account['total_contributions_krw'])}",
        ]
    )


def sync_reminder_message(state: Mapping[str, Any]) -> str:
    pending = state["telegram"].get("pending_sync") or {}
    return "\n".join(
        [
            "[잔고 동기화가 필요합니다]",
            f"- 대기 주문: {pending.get('kind', '-')} {pending.get('step', '-')}차",
            f"- 당시 주문 검토액: {krw(float(pending.get('expected_amount_krw', 0)))}",
            "",
            "다음 단계 금액을 정확히 계산하려면 실제 Upbit BTC 수량과 원화 잔액을 입력해야 합니다.",
        ]
    )


def block_event_message(event: Mapping[str, Any]) -> str:
    block: core.BlockContext = event["block"]
    event_type = str(event["type"])
    if event_type == "ENTRY_THRESHOLD":
        return "\n".join(
            [
                "[반감기 매수구간 진입 예고]",
                f"- 사이클 진행률: {pct(block.cycle_progress, 2)}",
                "- 기준 65%에 도달했습니다.",
                "- 공식 1차 매수 안내는 다음 월요일 09:17 KST 전후에 발송합니다.",
                "- 장중 자동주문은 없습니다.",
            ]
        )
    if event_type == "HALVING":
        return "\n".join(
            [
                "[Bitcoin 반감기 발생]",
                f"- 새 epoch: {block.epoch}",
                f"- 블록 높이: {block.height:,}",
                "- 고정 6회 전략상 보유를 계속합니다.",
                "- 이 알림 자체는 매매지시가 아닙니다.",
            ]
        )
    return "\n".join(
        [
            "[반감기 후 매도구간 진입 예고]",
            f"- 사이클 진행률: {pct(block.cycle_progress, 2)}",
            "- 반감기 후 기준 35%를 넘었습니다.",
            "- 공식 1차 매도 안내는 다음 월요일 09:17 KST 전후에 발송합니다.",
        ]
    )


def data_error_message(error: str) -> str:
    return "\n".join(
        [
            "[BTC 데이터 오류]",
            error,
            "",
            "새 매수·매도 단계는 중단하고 마지막 정상상태를 유지합니다.",
            "자동주문은 없습니다.",
        ]
    )


def data_recovery_message() -> str:
    return "[BTC 데이터 정상화]\nUpbit 가격과 블록 높이 검증이 다시 정상으로 돌아왔습니다."


def conversation_reminder_message(conversation: Mapping[str, Any]) -> str:
    kind = str(conversation.get("type"))
    label = {
        "deposit_amount": "추가입금 금액",
        "budget_amount": "새 시작예산",
        "sync_btc": "현재 BTC 수량",
        "sync_krw": "현재 원화 잔액",
    }.get(kind, "요청한 값")
    return f"[입력 대기]\n{label} 입력이 아직 완료되지 않았습니다.\n계속하려면 숫자를 입력하거나 취소 버튼을 눌러주세요."


def _conversation(kind: str, now_utc: datetime, **data: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "data": data,
        "created_at_utc": core.iso_utc(now_utc),
        "first_reminder_at_utc": core.iso_utc(now_utc + timedelta(minutes=core.FIRST_REMINDER_MINUTES)),
        "first_reminder_sent": False,
        "last_daily_reminder": None,
    }


def _is_authorized_message(message: Mapping[str, Any], chat_id: str) -> bool:
    chat = message.get("chat") or {}
    return str(chat.get("id")) == str(chat_id)


def _callback_chat_id(callback: Mapping[str, Any]) -> str | None:
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    value = chat.get("id")
    return None if value is None else str(value)


def _reset_operation_reminder(operation: dict[str, Any], now_utc: datetime) -> None:
    operation["first_reminder_at_utc"] = core.iso_utc(
        now_utc + timedelta(minutes=core.FIRST_REMINDER_MINUTES)
    )
    operation["first_reminder_sent"] = False
    operation["last_daily_reminder"] = None


def begin_deposit(state: dict[str, Any], client: TelegramClient, now_utc: datetime) -> None:
    state["telegram"]["conversation"] = _conversation("deposit_amount", now_utc)
    client.send_message(
        "[추가입금]\n추가한 원화 금액을 입력하세요.\n예: 3000000 또는 3,000,000",
        inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
    )


def begin_budget(state: dict[str, Any], client: TelegramClient, now_utc: datetime) -> None:
    if not core.can_change_start_budget(state):
        client.send_message(
            "시작예산은 첫 매수 전 BTC 0개·현금대기 상태에서만 바꿀 수 있습니다.\n매수 이후 증액은 ‘추가입금’을 사용하세요.",
            menu=True,
        )
        return
    state["telegram"]["conversation"] = _conversation("budget_amount", now_utc)
    client.send_message(
        "[시작예산 변경]\n새 시작예산을 입력하세요.\n예: 10000000",
        inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
    )


def begin_sync(state: dict[str, Any], client: TelegramClient, now_utc: datetime) -> None:
    state["telegram"]["conversation"] = _conversation("sync_btc", now_utc)
    client.send_message(
        "[잔고 동기화 1/2]\nUpbit에 표시된 현재 BTC 수량을 입력하세요.\n예: 0.03542187",
        inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
    )


def _create_deposit_operation(
    state: dict[str, Any], client: TelegramClient, amount: float, now_utc: datetime
) -> None:
    options = core.deposit_options(state)
    operation = core.create_pending_operation(
        state,
        operation_type="deposit",
        payload={"amount_krw": amount, "options": options},
        now_utc=now_utc,
    )
    state["telegram"]["conversation"] = None
    if len(options) == 1:
        operation["choice"] = options[0]
        operation["stage"] = "confirm"
    client.send_message(
        pending_operation_message(state, operation),
        inline_keyboard=pending_operation_keyboard(operation),
    )


def _create_budget_operation(
    state: dict[str, Any], client: TelegramClient, amount: float, now_utc: datetime
) -> None:
    operation = core.create_pending_operation(
        state,
        operation_type="budget",
        payload={"amount_krw": amount},
        now_utc=now_utc,
    )
    state["telegram"]["conversation"] = None
    client.send_message(
        pending_operation_message(state, operation),
        inline_keyboard=pending_operation_keyboard(operation),
    )


def _create_sync_operation(
    state: dict[str, Any],
    client: TelegramClient,
    btc_quantity: float,
    cash_krw: float,
    now_utc: datetime,
) -> None:
    operation = core.create_pending_operation(
        state,
        operation_type="sync",
        payload={"btc_quantity": btc_quantity, "cash_krw": cash_krw},
        now_utc=now_utc,
    )
    state["telegram"]["conversation"] = None
    client.send_message(
        pending_operation_message(state, operation),
        inline_keyboard=pending_operation_keyboard(operation),
    )


def handle_conversation_text(
    state: dict[str, Any],
    client: TelegramClient,
    text: str,
    now_utc: datetime,
) -> bool:
    conversation = state["telegram"].get("conversation")
    if not isinstance(conversation, Mapping):
        return False
    kind = str(conversation.get("type"))
    try:
        if kind == "deposit_amount":
            _create_deposit_operation(state, client, core.parse_krw_amount(text), now_utc)
            return True
        if kind == "budget_amount":
            _create_budget_operation(state, client, core.parse_krw_amount(text), now_utc)
            return True
        if kind == "sync_btc":
            btc = core.parse_btc_quantity(text)
            state["telegram"]["conversation"] = _conversation(
                "sync_krw", now_utc, btc_quantity=btc
            )
            client.send_message(
                "[잔고 동기화 2/2]\nUpbit에 표시된 현재 원화 잔액을 입력하세요.\n예: 6650000",
                inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
            )
            return True
        if kind == "sync_krw":
            cash = core.parse_krw_amount(text) if str(text).strip().replace(",", "") not in {"0", "0원"} else 0.0
            btc = float((conversation.get("data") or {}).get("btc_quantity", 0.0))
            _create_sync_operation(state, client, btc, cash, now_utc)
            return True
    except (ValueError, core.FixedStrategyError) as exc:
        client.send_message(f"입력값을 확인해 주세요.\n{exc}")
        return True
    return False


def handle_text_message(
    state: dict[str, Any],
    client: TelegramClient,
    text: str,
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    stripped = str(text).strip()
    if handle_conversation_text(state, client, stripped, now_utc):
        return

    lowered = stripped.lower()
    if lowered.startswith("/deposit "):
        try:
            _create_deposit_operation(
                state, client, core.parse_krw_amount(stripped.split(maxsplit=1)[1]), now_utc
            )
        except (ValueError, core.FixedStrategyError) as exc:
            client.send_message(f"추가입금 금액을 확인해 주세요.\n{exc}")
        return
    if lowered.startswith("/budget "):
        if not core.can_change_start_budget(state):
            client.send_message("첫 매수 이후에는 시작예산을 직접 바꿀 수 없습니다. 추가입금을 사용하세요.")
            return
        try:
            _create_budget_operation(
                state, client, core.parse_krw_amount(stripped.split(maxsplit=1)[1]), now_utc
            )
        except (ValueError, core.FixedStrategyError) as exc:
            client.send_message(f"시작예산을 확인해 주세요.\n{exc}")
        return

    if stripped in {"📊 상태 보기", "/status", "상태", "status"}:
        client.send_message(status_message(state, price=price, block=block), menu=True)
    elif stripped in {"➕ 추가입금", "/deposit", "추가입금"}:
        begin_deposit(state, client, now_utc)
    elif stripped in {"🔄 잔고 동기화", "/sync", "잔고 동기화", "동기화"}:
        begin_sync(state, client, now_utc)
    elif stripped in {"💰 시작예산 변경", "/budget", "예산 변경", "시작예산"}:
        begin_budget(state, client, now_utc)
    elif stripped in {"⏳ 대기 작업", "/pending", "대기 작업"}:
        client.send_message("[대기 작업]\n" + core.pending_summary(state), menu=True)
    elif stripped in {"/cancel", "취소"}:
        core.cancel_pending_operation(state, now_utc, reason="message")
        client.send_message("입력·확인 작업을 취소했습니다. 주문 후 잔고 동기화 대기는 취소되지 않습니다.", menu=True)
    elif stripped in {"❓ 도움말", "/start", "/help", "도움말"}:
        client.send_message(help_message(), menu=True)
    else:
        client.send_message("입력 내용을 이해하지 못했습니다. 아래 한글 메뉴를 선택해 주세요.", menu=True)


def _operation_by_id(state: Mapping[str, Any], operation_id: str) -> dict[str, Any] | None:
    operation = state["telegram"].get("pending_operation")
    if isinstance(operation, dict) and str(operation.get("id")) == operation_id:
        return operation
    return None


def _confirm_operation(
    state: dict[str, Any],
    client: TelegramClient,
    operation: dict[str, Any],
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    if not core.pending_operation_valid(operation, now_utc):
        core.cancel_pending_operation(state, now_utc, reason="expired")
        client.send_message("확인 시간이 지나 작업이 만료됐습니다. 메뉴에서 다시 시작해 주세요.", menu=True)
        return
    operation_type = str(operation["type"])
    payload = operation.get("payload", {})
    if operation_type == "deposit":
        choice = str(operation.get("choice"))
        result = core.apply_deposit(
            state,
            amount_krw=float(payload["amount_krw"]),
            mode=choice,
            now_utc=now_utc,
        )
        core.cancel_pending_operation(state, now_utc, reason="confirmed")
        if result["mode"] == "next":
            effect = "다음 사이클 대기자금으로 보관했습니다."
        elif result["correction_buy_scheduled"]:
            effect = "현재 사이클에 반영했습니다. 다음 월요일 보정매수 1회를 안내합니다."
        elif state["strategy"]["phase"] == "ENTRY":
            effect = "현재 3주 매수기간의 남은 단계 목표금액에 반영했습니다."
        else:
            effect = "다음 공식 매수예산에 반영했습니다."
        client.send_message(
            f"[추가입금 반영 완료]\n- 금액: {krw(result['amount_krw'])}\n- 처리: {effect}",
            menu=True,
        )
    elif operation_type == "budget":
        core.apply_start_budget(state, float(payload["amount_krw"]), now_utc)
        core.cancel_pending_operation(state, now_utc, reason="confirmed")
        client.send_message(
            f"[시작예산 변경 완료]\n새 시작예산: {krw(float(payload['amount_krw']))}\nBTC 0개·현금대기로 초기화했습니다.",
            menu=True,
        )
    elif operation_type == "sync":
        result = core.complete_pending_sync(
            state,
            btc_quantity=float(payload["btc_quantity"]),
            cash_krw=float(payload["cash_krw"]),
            now_utc=now_utc,
        )
        state["telegram"]["pending_operation"] = None
        completed = result.get("completed_order")
        detail = "수동 잔고 동기화가 완료됐습니다."
        if isinstance(completed, Mapping):
            detail = f"{completed.get('kind')} {completed.get('step')}차 주문 후 잔고 동기화를 완료했습니다."
        client.send_message(
            status_message(state, price=price, block=block, title=detail),
            menu=True,
        )


def handle_callback(
    state: dict[str, Any],
    client: TelegramClient,
    callback: Mapping[str, Any],
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    callback_id = str(callback.get("id"))
    data = str(callback.get("data", ""))
    client.answer_callback(callback_id, "처리 중입니다.")

    if data == "flow:cancel":
        core.cancel_pending_operation(state, now_utc, reason="button")
        client.send_message("입력 작업을 취소했습니다.", menu=True)
        return
    if data == "sync:start":
        begin_sync(state, client, now_utc)
        return
    if data == "sync:later":
        pending = state["telegram"].get("pending_sync")
        if isinstance(pending, dict):
            pending["first_reminder_at_utc"] = core.iso_utc(
                now_utc + timedelta(minutes=core.FIRST_REMINDER_MINUTES)
            )
            pending["first_reminder_sent"] = False
            client.send_message("30분 뒤 잔고 동기화를 다시 알려드리겠습니다.", menu=True)
        else:
            client.send_message("동기화 대기 주문이 없습니다.", menu=True)
        return

    parts = data.split(":")
    if len(parts) != 3:
        client.send_message("버튼 정보가 만료됐습니다. 메뉴에서 다시 시작해 주세요.", menu=True)
        return
    action, choice, operation_id = parts
    operation = _operation_by_id(state, operation_id)
    if operation is None:
        client.send_message("이 작업은 이미 처리됐거나 만료됐습니다.", menu=True)
        return

    if action == "dep" and choice in {"current", "next"}:
        options = operation.get("payload", {}).get("options", [])
        if choice not in options:
            client.send_message("현재 전략 단계에서는 이 선택을 사용할 수 없습니다.", menu=True)
            return
        operation["choice"] = choice
        operation["stage"] = "confirm"
        _reset_operation_reminder(operation, now_utc)
        client.send_message(
            pending_operation_message(state, operation),
            inline_keyboard=pending_operation_keyboard(operation),
        )
        return
    if action == "op" and choice == "cancel":
        core.cancel_pending_operation(state, now_utc, reason="button")
        client.send_message("확인 대기 작업을 취소했습니다.", menu=True)
        return
    if action == "op" and choice == "confirm":
        _confirm_operation(
            state,
            client,
            operation,
            now_utc=now_utc,
            price=price,
            block=block,
        )
        return
    client.send_message("처리할 수 없는 버튼입니다.", menu=True)


def discard_old_updates(state: dict[str, Any], client: TelegramClient) -> None:
    updates = client.get_updates(offset=None)
    if updates:
        state["telegram"]["last_update_id"] = max(int(item["update_id"]) for item in updates)
    else:
        state["telegram"]["last_update_id"] = 0


def process_updates(
    state: dict[str, Any],
    client: TelegramClient,
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    last_update = state["telegram"].get("last_update_id")
    offset = int(last_update) + 1 if last_update is not None else None
    updates = client.get_updates(offset=offset)
    for update in sorted(updates, key=lambda item: int(item["update_id"])):
        state["telegram"]["last_update_id"] = int(update["update_id"])
        message = update.get("message")
        if isinstance(message, Mapping):
            if not _is_authorized_message(message, client.chat_id):
                continue
            text = message.get("text")
            if text is not None:
                handle_text_message(
                    state,
                    client,
                    str(text),
                    now_utc=now_utc,
                    price=price,
                    block=block,
                )
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            if _callback_chat_id(callback) != client.chat_id:
                client.answer_callback(str(callback.get("id")), "권한이 없습니다.")
                continue
            handle_callback(
                state,
                client,
                callback,
                now_utc=now_utc,
                price=price,
                block=block,
            )


def process_reminders(
    state: dict[str, Any],
    client: TelegramClient,
    *,
    now_utc: datetime,
    daily_check: bool,
) -> None:
    telegram = state["telegram"]
    pending_sync = telegram.get("pending_sync")
    if isinstance(pending_sync, dict):
        kind = core.reminder_due(pending_sync, now_utc, daily_check=daily_check)
        if kind:
            client.send_message(sync_reminder_message(state), inline_keyboard=order_keyboard())
            core.mark_reminded(pending_sync, kind, now_utc)

    operation = telegram.get("pending_operation")
    if isinstance(operation, dict):
        if not core.pending_operation_valid(operation, now_utc):
            core.cancel_pending_operation(state, now_utc, reason="expired")
            client.send_message("확인 대기 작업이 24시간을 지나 만료됐습니다.", menu=True)
        else:
            kind = core.reminder_due(operation, now_utc, daily_check=daily_check)
            if kind:
                client.send_message(
                    "[확인 대기 작업 알림]\n" + pending_operation_message(state, operation),
                    inline_keyboard=pending_operation_keyboard(operation),
                )
                core.mark_reminded(operation, kind, now_utc)

    conversation = telegram.get("conversation")
    if isinstance(conversation, dict):
        kind = core.reminder_due(conversation, now_utc, daily_check=daily_check)
        if kind:
            client.send_message(
                conversation_reminder_message(conversation),
                inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
            )
            core.mark_reminded(conversation, kind, now_utc)


def _fetch_market_context(now_utc: datetime) -> tuple[float, core.BlockContext]:
    return core.fetch_upbit_price(), core.fetch_block_context(now_utc)


def run_service(
    *,
    state_path: Path,
    reset_state: bool = False,
    force_status: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or core.utc_now()
    state, initialized_new = core.load_state(
        state_path,
        now_utc=now,
        reset=reset_state,
        budget_krw=core.DEFAULT_BUDGET_KRW,
    )
    client = TelegramClient(core.env_bot_token(), core.env_chat_id())

    price: float | None = None
    block: core.BlockContext | None = None
    data_error: str | None = None
    try:
        price, block = _fetch_market_context(now)
        core.update_last_price(state, price, now)
        status_change = core.set_data_status(
            state, status="ok", error=None, now_utc=now
        )
    except Exception as exc:
        data_error = str(exc)
        price = core.current_price_from_state(state)
        block = _block_from_state(state)
        status_change = core.set_data_status(
            state, status="error", error=data_error, now_utc=now
        )

    if initialized_new or not bool(state["telegram"].get("initialized")):
        discard_old_updates(state, client)
        client.set_commands()
        client.send_message(
            initial_message(state, price=price, block=block),
            menu=True,
        )
        state["telegram"]["initialized"] = True
        core.append_audit(state, "TELEGRAM_FIXED_STRATEGY_INITIALIZED", {}, now)
    else:
        process_updates(
            state,
            client,
            now_utc=now,
            price=price,
            block=block,
        )

    if status_change == "error":
        client.send_message(data_error_message(data_error or "알 수 없는 데이터 오류"))
    elif status_change == "recovery":
        client.send_message(data_recovery_message())

    if block is not None and data_error is None:
        for event in core.detect_block_events(state, block, now):
            client.send_message(block_event_message(event))

    daily_due = core.daily_check_due(now, state)
    if daily_due and price is not None:
        core.record_daily_equity(state, price, now)

    if data_error is None and price is not None and block is not None and core.official_action_due(now, state):
        action = core.create_official_order(
            state,
            block=block,
            price_krw=price,
            now_utc=now,
        )
        if action and action["type"] == "SYNC_BLOCK":
            client.send_message(
                "[다음 단계 보류]\n이전 주문 후 잔고 동기화가 끝나지 않아 이번 주 다음 단계를 진행하지 않습니다.",
                inline_keyboard=order_keyboard(),
            )
        elif action and action["type"] == "CHECK_DEFERRED":
            client.send_message(action["message"], menu=True)
        elif action and action["type"] == "ORDER":
            instruction: core.OrderInstruction = action["instruction"]
            if instruction.side == "NONE":
                core.complete_pending_sync(
                    state,
                    btc_quantity=float(state["account"]["btc_quantity"]),
                    cash_krw=float(state["account"]["cash_krw"]),
                    now_utc=now,
                )
                client.send_message(
                    f"[{instruction.reason}]\n현재 실제비중이 이미 목표와 같아 주문 없이 단계를 완료했습니다.",
                    menu=True,
                )
            else:
                client.send_message(
                    order_message(state, instruction),
                    inline_keyboard=order_keyboard(),
                )

    if core.monthly_report_due(now, state):
        client.send_message(
            status_message(state, price=price, block=block, title="월간 상태보고"),
            menu=True,
        )
        core.mark_monthly_reported(state, now)

    process_reminders(
        state,
        client,
        now_utc=now,
        daily_check=daily_due,
    )

    if force_status:
        client.send_message(
            status_message(state, price=price, block=block, title="수동 상태보고"),
            menu=True,
        )

    state["updated_at_utc"] = core.iso_utc(now)
    core.save_state(state_path, state)
    return {
        "strategy_version": core.STRATEGY_VERSION,
        "state_schema_version": core.STATE_SCHEMA_VERSION,
        "state_path": str(state_path),
        "initialized_new": initialized_new,
        "data_status": state["strategy"]["last_data_status"],
        "price_krw": price,
        "block_height": None if block is None else block.height,
        "cycle_progress": None if block is None else block.cycle_progress,
        "phase": state["strategy"]["phase"],
        "pending": core.pending_summary(state),
        "auto_order": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quant Guardian BTC fixed six-trade Telegram service"
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("data/state/btc_fixed_state.json"),
    )
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--force-status", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_service(
            state_path=args.state_file,
            reset_state=args.reset_state,
            force_status=args.force_status,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (core.FixedStrategyError, TelegramError, ValueError) as exc:
        print(f"BTC fixed-six service failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
