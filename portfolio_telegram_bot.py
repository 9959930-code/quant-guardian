from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import btc_fixed_advisory as btc_core
import btc_fixed_telegram_bot as btc_bot
import isa_leverage_advisory as isa_advisory
import isa_leverage_core as isa_core
import isa_leverage_messages as isa_messages


MENU_VERSION = 2
UNIFIED_MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "₿ BTC 상태"}, {"text": "📈 ISA 상태"}],
        [{"text": "🔄 BTC 잔고 동기화"}, {"text": "🔄 ISA 잔고 동기화"}],
        [{"text": "➕ BTC 추가입금"}, {"text": "💰 BTC 시작예산"}],
        [{"text": "⏳ 대기 작업"}, {"text": "❓ 도움말"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "BTC 또는 ISA 메뉴를 선택하세요",
}

_ORIGINAL_SET_COMMANDS = btc_bot.TelegramClient.set_commands
_ORIGINAL_HANDLE_CONVERSATION_TEXT = btc_bot.handle_conversation_text
_ORIGINAL_HANDLE_TEXT_MESSAGE = btc_bot.handle_text_message
_ORIGINAL_HANDLE_CALLBACK = btc_bot.handle_callback
_ORIGINAL_HELP_MESSAGE = btc_bot.help_message
_ORIGINAL_CONVERSATION_REMINDER_MESSAGE = btc_bot.conversation_reminder_message

_ISA_STATE: dict[str, Any] | None = None
_ISA_STATE_PATH: Path | None = None


def _require_isa_state() -> dict[str, Any]:
    if _ISA_STATE is None:
        raise isa_core.IsaStrategyError("ISA 상태가 초기화되지 않았습니다.")
    return _ISA_STATE


def _require_isa_state_path() -> Path:
    if _ISA_STATE_PATH is None:
        raise isa_core.IsaStrategyError("ISA 상태파일 경로가 초기화되지 않았습니다.")
    return _ISA_STATE_PATH


def set_isa_context(state: dict[str, Any], path: Path) -> None:
    global _ISA_STATE, _ISA_STATE_PATH
    _ISA_STATE = state
    _ISA_STATE_PATH = path


def unified_set_commands(self: btc_bot.TelegramClient) -> None:
    self._request(
        "setMyCommands",
        {
            "commands": [
                {"command": "status", "description": "BTC 현재 전략과 계좌 상태"},
                {"command": "sync", "description": "BTC Upbit 잔고 동기화"},
                {"command": "isa_status", "description": "ISA 레버리지 계좌 상태"},
                {"command": "isa_sync", "description": "ISA TIGER 잔고 동기화"},
                {"command": "deposit", "description": "BTC 추가입금 등록"},
                {"command": "budget", "description": "BTC 첫 매수 전 시작예산 변경"},
                {"command": "pending", "description": "BTC·ISA 입력 대기 작업 확인"},
                {"command": "cancel", "description": "현재 입력·확인 작업 취소"},
            ]
        },
    )


def unified_help_message() -> str:
    return "\n".join(
        [
            "[Quant Guardian 사용 방법]",
            "",
            "₿ BTC 상태: 반감기 단계와 Upbit 추종계좌 확인",
            "🔄 BTC 잔고 동기화: 실제 BTC 수량과 원화잔액 입력",
            "➕ BTC 추가입금: 고정 6회 전략 예산에 추가입금 반영",
            "💰 BTC 시작예산: 첫 매수 전에만 변경",
            "",
            "📈 ISA 상태: 기존 ETF와 TIGER 평가상태 확인",
            "🔄 ISA 잔고 동기화: TIGER 총수량·누적원금·ISA 납입원금 입력",
            "",
            "BTC와 ISA 주문은 모두 직접 실행합니다.",
            "자동주문은 없으며, ISA의 기존 3개 ETF 수량은 현재 승인값으로 유지됩니다.",
        ]
    )


def _parse_nonnegative(
    text: str,
    *,
    label: str,
    maximum: float,
    allow_skip: bool = False,
) -> float | None:
    normalized = (
        str(text)
        .strip()
        .replace(",", "")
        .replace("원", "")
        .replace("주", "")
        .replace(" ", "")
    )
    if allow_skip and normalized.lower() in {"-", "skip", "건너뛰기", "유지", "모름"}:
        return None
    if normalized == "":
        raise ValueError(f"{label} 값이 비어 있습니다.")
    value = float(normalized)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} 값은 0 이상이어야 합니다.")
    if value > maximum:
        raise ValueError(f"{label} 값이 너무 큽니다.")
    return float(value)


def _active_conversation(btc_state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = (btc_state.get("telegram") or {}).get("conversation")
    return value if isinstance(value, Mapping) else None


def _set_isa_conversation(
    btc_state: dict[str, Any],
    kind: str,
    now_utc: datetime,
    **data: Any,
) -> None:
    btc_state["telegram"]["conversation"] = btc_bot._conversation(kind, now_utc, **data)


def begin_isa_sync(
    btc_state: dict[str, Any],
    client: btc_bot.TelegramClient,
    now_utc: datetime,
) -> None:
    conversation = _active_conversation(btc_state)
    if conversation is not None:
        client.send_message(
            "다른 숫자 입력이 진행 중입니다. 먼저 입력을 끝내거나 ‘취소’를 눌러 주세요.",
            menu=True,
        )
        return
    _set_isa_conversation(btc_state, "isa_sync_tiger_quantity", now_utc)
    current = float(_require_isa_state()["account"].get("tiger_quantity", 0))
    client.send_message(
        "\n".join(
            [
                "[ISA 잔고 동기화 1/3]",
                "현재 ISA에 표시된 TIGER 미국나스닥100레버리지의 총 수량을 입력하세요.",
                f"현재 저장값: {isa_core.number(current)}주",
                "예: 231",
            ]
        ),
        inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
    )


def _isa_sync_confirmation_message(
    data: Mapping[str, Any],
    *,
    initial_completed: bool,
) -> str:
    contribution = data.get("isa_total_contributions_krw")
    contribution_text = (
        "기존 저장값 유지"
        if contribution is None
        else isa_core.krw(float(contribution))
    )
    lines = [
        "[ISA 잔고 동기화 최종 확인]",
        "",
        f"- TIGER 총수량: {isa_core.number(float(data['tiger_quantity']))}주",
        f"- TIGER 누적투입원금: {isa_core.krw(float(data['tiger_invested_krw']))}",
        f"- ISA 누적 납입원금: {contribution_text}",
        "- 기존 HBM·머니마켓·나스닥100 수량: 변경 없음",
        "",
    ]
    if initial_completed:
        lines.append("확정하면 현재 저장값을 위 값으로 교체합니다.")
    else:
        lines.extend(
            [
                "초기 1,000만원 매수가 끝났다면 ‘초기매수 완료’를 선택하세요.",
                "아직 일부만 체결했다면 ‘잔고만 갱신’을 선택하세요.",
            ]
        )
    lines.append("자동주문은 없습니다.")
    return "\n".join(lines)


def _isa_sync_keyboard(*, initial_completed: bool) -> list[list[dict[str, str]]]:
    if initial_completed:
        return [
            [
                {"text": "확정", "callback_data": "isa:sync:confirm"},
                {"text": "취소", "callback_data": "isa:sync:cancel"},
            ]
        ]
    return [
        [{"text": "✅ 초기매수 완료", "callback_data": "isa:sync:complete"}],
        [{"text": "📝 잔고만 갱신", "callback_data": "isa:sync:partial"}],
        [{"text": "취소", "callback_data": "isa:sync:cancel"}],
    ]


def handle_isa_conversation_text(
    btc_state: dict[str, Any],
    client: btc_bot.TelegramClient,
    text: str,
    now_utc: datetime,
) -> bool:
    conversation = _active_conversation(btc_state)
    if conversation is None:
        return False
    kind = str(conversation.get("type", ""))
    if not kind.startswith("isa_"):
        return False
    data = dict(conversation.get("data") or {})
    try:
        if kind == "isa_sync_tiger_quantity":
            quantity = _parse_nonnegative(
                text, label="TIGER 수량", maximum=10_000_000
            )
            if quantity is None or abs(quantity - round(quantity)) > 1e-9:
                raise ValueError("TIGER 수량은 정수 주 단위로 입력해야 합니다.")
            quantity = float(round(quantity))
            _set_isa_conversation(
                btc_state,
                "isa_sync_tiger_invested",
                now_utc,
                tiger_quantity=quantity,
            )
            current = float(
                _require_isa_state()["account"].get("tiger_invested_krw", 0)
            )
            client.send_message(
                "\n".join(
                    [
                        "[ISA 잔고 동기화 2/3]",
                        "TIGER에 실제로 투입한 누적 매수원금을 입력하세요.",
                        f"현재 저장값: {isa_core.krw(current)}",
                        "평가액이 아니라 누적 매수대금입니다. 예: 9986000",
                    ]
                ),
                inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
            )
            return True
        if kind == "isa_sync_tiger_invested":
            invested = _parse_nonnegative(
                text, label="TIGER 누적투입원금", maximum=100_000_000_000
            )
            _set_isa_conversation(
                btc_state,
                "isa_sync_total_contributions",
                now_utc,
                tiger_quantity=float(data["tiger_quantity"]),
                tiger_invested_krw=invested,
            )
            current = _require_isa_state()["account"].get(
                "isa_total_contributions_krw"
            )
            client.send_message(
                "\n".join(
                    [
                        "[ISA 잔고 동기화 3/3]",
                        "ISA 계좌의 누적 납입원금을 입력하세요.",
                        f"현재 저장값: {isa_core.krw(current)}",
                        "모르면 ‘-’를 입력하면 기존 값을 유지합니다.",
                    ]
                ),
                inline_keyboard=[[{"text": "취소", "callback_data": "flow:cancel"}]],
            )
            return True
        if kind == "isa_sync_total_contributions":
            total = _parse_nonnegative(
                text,
                label="ISA 누적 납입원금",
                maximum=100_000_000_000,
                allow_skip=True,
            )
            payload = {
                "tiger_quantity": float(data["tiger_quantity"]),
                "tiger_invested_krw": float(data["tiger_invested_krw"]),
                "isa_total_contributions_krw": total,
            }
            _set_isa_conversation(
                btc_state, "isa_sync_confirm", now_utc, **payload
            )
            initial_completed = bool(
                _require_isa_state()["strategy"].get("initial_completed")
            )
            client.send_message(
                _isa_sync_confirmation_message(
                    payload, initial_completed=initial_completed
                ),
                inline_keyboard=_isa_sync_keyboard(
                    initial_completed=initial_completed
                ),
            )
            return True
        if kind == "isa_sync_confirm":
            client.send_message("아래 확인 버튼을 눌러 동기화를 완료해 주세요.")
            return True
    except (ValueError, TypeError, KeyError, isa_core.IsaStrategyError) as exc:
        client.send_message(f"입력값을 확인해 주세요.\n{exc}")
        return True
    return True


def _fetch_isa_market_context() -> tuple[
    dict[str, isa_core.QuoteSnapshot], isa_core.FxSnapshot | None
]:
    quotes = isa_core.fetch_quotes()
    try:
        fx = isa_core.fetch_fx_snapshot()
    except isa_core.IsaStrategyError:
        fx = None
    return quotes, fx


def send_isa_status(
    client: btc_bot.TelegramClient,
    *,
    title: str = "상태보고",
) -> None:
    state = _require_isa_state()
    try:
        quotes, fx = _fetch_isa_market_context()
        client.send_message(
            isa_messages.status_message(state, quotes, fx, title=title),
            menu=True,
        )
    except isa_core.IsaStrategyError as exc:
        account = state["account"]
        client.send_message(
            "\n".join(
                [
                    "[Quant Guardian ISA · 저장 상태]",
                    f"- TIGER 수량: {isa_core.number(account.get('tiger_quantity'))}주",
                    f"- TIGER 누적투입원금: {isa_core.krw(account.get('tiger_invested_krw'))}",
                    f"- ISA 누적 납입원금: {isa_core.krw(account.get('isa_total_contributions_krw'))}",
                    f"- 시세 조회 오류: {exc}",
                    "- 자동주문 없음",
                ]
            ),
            menu=True,
        )


def handle_isa_callback(
    btc_state: dict[str, Any],
    client: btc_bot.TelegramClient,
    callback: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> bool:
    data = str(callback.get("data", ""))
    if not data.startswith("isa:sync:"):
        return False
    callback_id = str(callback.get("id", ""))
    client.answer_callback(callback_id, "처리 중입니다.")
    action = data.rsplit(":", 1)[-1]
    if action == "cancel":
        btc_state["telegram"]["conversation"] = None
        client.send_message("ISA 잔고 동기화를 취소했습니다.", menu=True)
        return True

    conversation = _active_conversation(btc_state)
    if (
        conversation is None
        or str(conversation.get("type")) != "isa_sync_confirm"
    ):
        client.send_message(
            "ISA 동기화 입력이 만료됐습니다. 메뉴에서 다시 시작해 주세요.",
            menu=True,
        )
        return True

    payload = dict(conversation.get("data") or {})
    isa_state = _require_isa_state()
    initial_completed = bool(isa_state["strategy"].get("initial_completed"))
    if action not in {"confirm", "complete", "partial"}:
        client.send_message("처리할 수 없는 ISA 동기화 버튼입니다.", menu=True)
        return True
    if action == "confirm" and not initial_completed:
        client.send_message(
            "초기매수 완료 또는 잔고만 갱신 중 하나를 선택해 주세요.",
            menu=True,
        )
        return True

    mark_initial_completed = action == "complete"
    try:
        isa_core.apply_manual_sync(
            isa_state,
            tiger_quantity=float(payload["tiger_quantity"]),
            tiger_invested_krw=float(payload["tiger_invested_krw"]),
            isa_total_contributions_krw=payload.get(
                "isa_total_contributions_krw"
            ),
            mark_initial_completed=mark_initial_completed,
            now_utc=now_utc,
        )
        isa_core.save_state(
            _require_isa_state_path(), isa_state, now_utc=now_utc
        )
    except (KeyError, TypeError, ValueError, isa_core.IsaStrategyError) as exc:
        client.send_message(f"ISA 잔고 동기화에 실패했습니다.\n{exc}", menu=True)
        return True

    btc_state["telegram"]["conversation"] = None
    if action == "partial":
        client.send_message(
            "ISA 잔고를 갱신했습니다. 초기 1,000만원 매수는 아직 미완료 상태로 유지합니다."
        )
    send_isa_status(client, title="텔레그램 잔고동기화 완료")
    return True


def unified_handle_conversation_text(
    state: dict[str, Any],
    client: btc_bot.TelegramClient,
    text: str,
    now_utc: datetime,
) -> bool:
    if handle_isa_conversation_text(state, client, text, now_utc):
        return True
    return _ORIGINAL_HANDLE_CONVERSATION_TEXT(state, client, text, now_utc)


def unified_handle_text_message(
    state: dict[str, Any],
    client: btc_bot.TelegramClient,
    text: str,
    *,
    now_utc: datetime,
    price: float | None,
    block: btc_core.BlockContext | None,
) -> None:
    stripped = str(text).strip()
    lowered = stripped.lower()

    if stripped in {"📈 ISA 상태", "ISA 상태", "/isa_status"} or lowered == "isa status":
        send_isa_status(client)
        return
    if stripped in {
        "🔄 ISA 잔고 동기화",
        "ISA 잔고 동기화",
        "ISA 동기화",
        "/isa_sync",
    } or lowered == "isa sync":
        begin_isa_sync(state, client, now_utc)
        return

    translations = {
        "₿ BTC 상태": "📊 상태 보기",
        "🔄 BTC 잔고 동기화": "🔄 잔고 동기화",
        "➕ BTC 추가입금": "➕ 추가입금",
        "💰 BTC 시작예산": "💰 시작예산 변경",
    }
    _ORIGINAL_HANDLE_TEXT_MESSAGE(
        state,
        client,
        translations.get(stripped, stripped),
        now_utc=now_utc,
        price=price,
        block=block,
    )


def unified_handle_callback(
    state: dict[str, Any],
    client: btc_bot.TelegramClient,
    callback: Mapping[str, Any],
    *,
    now_utc: datetime,
    price: float | None,
    block: btc_core.BlockContext | None,
) -> None:
    if handle_isa_callback(state, client, callback, now_utc=now_utc):
        return
    _ORIGINAL_HANDLE_CALLBACK(
        state,
        client,
        callback,
        now_utc=now_utc,
        price=price,
        block=block,
    )


def unified_conversation_reminder_message(
    conversation: Mapping[str, Any],
) -> str:
    kind = str(conversation.get("type", ""))
    labels = {
        "isa_sync_tiger_quantity": "ISA TIGER 총수량",
        "isa_sync_tiger_invested": "ISA TIGER 누적투입원금",
        "isa_sync_total_contributions": "ISA 누적 납입원금",
        "isa_sync_confirm": "ISA 잔고동기화 최종 확인",
    }
    if kind in labels:
        return (
            f"[ISA 입력 대기]\n{labels[kind]} 입력이 아직 완료되지 않았습니다.\n"
            "계속 입력하거나 취소 버튼을 눌러 주세요."
        )
    return _ORIGINAL_CONVERSATION_REMINDER_MESSAGE(conversation)


def install_unified_patches() -> None:
    btc_bot.MENU_KEYBOARD = UNIFIED_MENU_KEYBOARD
    btc_bot.TelegramClient.set_commands = unified_set_commands
    btc_bot.help_message = unified_help_message
    btc_bot.handle_conversation_text = unified_handle_conversation_text
    btc_bot.handle_text_message = unified_handle_text_message
    btc_bot.handle_callback = unified_handle_callback
    btc_bot.conversation_reminder_message = (
        unified_conversation_reminder_message
    )


def ensure_unified_menu(
    btc_state_path: Path,
    *,
    now_utc: datetime,
) -> bool:
    state, _ = btc_core.load_state(btc_state_path, now_utc=now_utc)
    telegram = state["telegram"]
    if int(telegram.get("unified_menu_version", 0)) >= MENU_VERSION:
        return False
    client = btc_bot.TelegramClient(
        btc_core.env_bot_token(), btc_core.env_chat_id()
    )
    client.set_commands()
    client.send_message(
        "\n".join(
            [
                "[Quant Guardian 메뉴 업데이트]",
                "BTC와 ISA 메뉴를 구분했습니다.",
                "‘🔄 BTC 잔고 동기화’는 Upbit용이고,",
                "‘🔄 ISA 잔고 동기화’는 TIGER 수량·원금 입력용입니다.",
            ]
        ),
        menu=True,
    )
    telegram["unified_menu_version"] = MENU_VERSION
    btc_core.append_audit(
        state,
        "UNIFIED_BTC_ISA_MENU_ENABLED",
        {"menu_version": MENU_VERSION},
        now_utc,
    )
    btc_core.save_state(btc_state_path, state)
    return True


def _isa_outbound_due(
    state: Mapping[str, Any],
    *,
    now_utc: datetime,
    initialized_new: bool,
    force_status: bool,
) -> bool:
    if force_status or initialized_new:
        return True
    if not bool(state["strategy"].get("initial_plan_sent")):
        return True
    now_kst = now_utc.astimezone(isa_core.KST)
    if now_kst.weekday() >= 5 or (now_kst.hour, now_kst.minute) < (9, 17):
        return False
    data = state["data"]
    try:
        checked = datetime.fromisoformat(str(data.get("last_check_at_utc", "")).replace("Z", "+00:00"))
        checked = checked.astimezone(isa_core.KST)
    except (TypeError, ValueError):
        checked = None
    if checked is None or checked.date() != now_kst.date():
        return True
    strategy = state["strategy"]
    period = now_kst.strftime("%Y-%m")
    pending_month = (
        bool(strategy.get("initial_completed"))
        and period >= str(strategy.get("monthly_start_period") or period)
        and strategy.get("last_monthly_plan_period") != period
    )
    return (pending_month or data.get("status") != "ok") and now_kst - checked >= timedelta(hours=1)


def run_service(
    *,
    btc_state_path: Path,
    isa_state_path: Path,
    reset_btc_state: bool = False,
    reset_isa_state: bool = False,
    force_btc_status: bool = False,
    force_isa_status: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or btc_core.utc_now()
    isa_state, isa_initialized_new = isa_core.load_state(
        isa_state_path, reset=reset_isa_state, now_utc=now
    )
    set_isa_context(isa_state, isa_state_path)
    install_unified_patches()

    btc_result = btc_bot.run_service(
        state_path=btc_state_path,
        reset_state=reset_btc_state,
        force_status=force_btc_status,
        now_utc=now,
    )
    isa_core.save_state(isa_state_path, isa_state, now_utc=now)
    menu_updated = ensure_unified_menu(btc_state_path, now_utc=now)

    if _isa_outbound_due(
        isa_state,
        now_utc=now,
        initialized_new=isa_initialized_new,
        force_status=force_isa_status,
    ):
        isa_result = isa_advisory.run_service(
            state_path=isa_state_path,
            reset_state=False,
            force_status=force_isa_status,
            now_utc=now,
        )
    else:
        isa_result = {
            "strategy_version": isa_core.STRATEGY_VERSION,
            "state_schema_version": isa_core.STATE_SCHEMA_VERSION,
            "state_path": str(isa_state_path),
            "initialized_new": False,
            "initial_completed": bool(
                isa_state["strategy"].get("initial_completed")
            ),
            "last_monthly_plan_period": isa_state["strategy"].get(
                "last_monthly_plan_period"
            ),
            "tiger_quantity": float(
                isa_state["account"].get("tiger_quantity", 0)
            ),
            "tiger_invested_krw": float(
                isa_state["account"].get("tiger_invested_krw", 0)
            ),
            "data_status": isa_state["data"].get("status"),
            "data_checked_this_run": False,
            "message_count": 0,
            "auto_order": False,
        }

    return {
        "service_version": "portfolio-telegram-btc-isa-1.0",
        "menu_updated": menu_updated,
        "btc": btc_result,
        "isa": isa_result,
        "auto_order": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified BTC and ISA Telegram service"
    )
    parser.add_argument(
        "--btc-state-file",
        type=Path,
        default=Path("data/state/btc_fixed_state.json"),
    )
    parser.add_argument(
        "--isa-state-file",
        type=Path,
        default=Path("data/state/isa_leverage_state.json"),
    )
    parser.add_argument("--reset-btc-state", action="store_true")
    parser.add_argument("--reset-isa-state", action="store_true")
    parser.add_argument("--force-btc-status", action="store_true")
    parser.add_argument("--force-isa-status", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_service(
            btc_state_path=args.btc_state_file,
            isa_state_path=args.isa_state_file,
            reset_btc_state=args.reset_btc_state,
            reset_isa_state=args.reset_isa_state,
            force_btc_status=args.force_btc_status,
            force_isa_status=args.force_isa_status,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        btc_core.FixedStrategyError,
        btc_bot.TelegramError,
        isa_core.IsaStrategyError,
        ValueError,
    ) as exc:
        print(f"Unified Telegram service failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
