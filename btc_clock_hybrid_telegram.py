from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

import btc_fixed_advisory as core
import btc_fixed_telegram_bot as bot
import btc_clock_hybrid_core as hybrid

RECALC_AFTER = timedelta(hours=24)
WARN_AFTER = timedelta(days=3)
EXPIRE_AFTER = timedelta(days=7)

_ORIG_STATUS = bot.status_message
_ORIG_ORDER_MESSAGE = bot.order_message
_ORIG_CALLBACK = bot.handle_callback
_ORIG_REMINDERS = bot.process_reminders


def block_event_message(event: Mapping[str, Any]) -> str:
    block: core.BlockContext = event["block"]
    kind, current = str(event["type"]), hybrid.offset(block)
    if kind == "ENTRY_WATCH":
        return "\n".join(
            [
                "🟢 BTC 저점 관찰 구간 진입",
                "",
                "현재 반감기 사이클 진행률이 62.5%에 도달했습니다.",
                "아직 매수 주문은 없습니다.",
                "",
                f"- 현재 블록 높이: {block.height:,}",
                f"- 현재 진행률: {block.cycle_progress * 100:.2f}%",
                f"- 65% 매수 시작까지: {max(0, hybrid.ENTRY - current):,}블록",
                "",
                "65% 도달 후 3회 분할매수가 시작됩니다.",
            ]
        )
    if kind == "ENTRY_THRESHOLD":
        return "\n".join(
            [
                "[반감기 매수구간 진입 예고]",
                f"- 사이클 진행률: {block.cycle_progress * 100:.2f}%",
                "- 기준 65%에 도달했습니다.",
                "- 공식 1차 매수 안내는 다음 월요일 09:17 KST 전후에 발송합니다.",
                "- 자동주문은 없습니다.",
            ]
        )
    if kind == "HALVING":
        return "\n".join(
            [
                "[Bitcoin 반감기 발생]",
                f"- 새 epoch: {block.epoch}",
                f"- 블록 높이: {block.height:,}",
                "- 고정 6회 전략상 보유를 계속합니다.",
                "- 이 알림 자체는 매매지시가 아닙니다.",
            ]
        )
    if kind == "EXIT_WARNING":
        return "\n".join(
            [
                "🟠 BTC 고점 위험구간 진입",
                "",
                "다음 반감기 이후 진행률이 35%에 도달했습니다.",
                "아직 매도 주문은 없습니다.",
                "",
                f"- 현재 블록 높이: {block.height:,}",
                f"- 현재 진행률: {block.cycle_progress * 100:.2f}%",
                f"- 1차 매도 36%까지: {max(0, hybrid.EXITS[0] - current):,}블록",
                "",
                "36% → BTC 2/3 유지",
                "37% → BTC 1/3 유지",
                "38% → BTC 0% 전환",
            ]
        )
    return "[BTC 블록 이벤트]\n알 수 없는 이벤트입니다."


def status_message(
    state: Mapping[str, Any],
    *,
    price: float | None,
    block: core.BlockContext | None,
    title: str = "현재 상태",
) -> str:
    text = _ORIG_STATUS(state, price=price, block=block, title=title)
    return text.replace(
        "- 매수: 33.3% → 66.7% → 100%",
        "- 매수: 62.5% 관찰 → 65% 진입 → 33.3%·66.7%·100%",
    ).replace(
        "- 매도: 66.7% → 33.3% → 0%",
        "- 매도: 35% 경고 → 36%·37%·38%에서 66.7%·33.3%·0%",
    )


def order_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "✅ 주문 완료 · 잔고 동기화", "callback_data": "sync:start"}],
        [{"text": "🔄 현재가로 주문안 재계산", "callback_data": "sync:refresh"}],
        [{"text": "⏰ 30분 뒤 다시 알림", "callback_data": "sync:later"}],
    ]


def order_message(state: Mapping[str, Any], instruction: core.OrderInstruction) -> str:
    return (
        _ORIG_ORDER_MESSAGE(state, instruction)
        + "\n\n[체결 지연 안전장치]"
        + "\n- 24시간: 현재가 기준 재계산"
        + "\n- 3일: 지연 주의와 재계산"
        + "\n- 7일: 기존 주문안 만료"
    )


def _age(pending: Mapping[str, Any], now: datetime) -> timedelta:
    anchor = core.parse_iso(str(pending.get("age_anchor_at_utc")))
    if anchor is None:
        anchor = core.parse_iso(str(pending.get("created_at_utc")))
    return timedelta(0) if anchor is None else max(timedelta(0), now - anchor)


def refresh_pending(
    state: dict[str, Any], *, price: float, now: datetime, reset_age: bool
) -> core.OrderInstruction:
    pending = state["telegram"].get("pending_sync")
    if not isinstance(pending, dict):
        raise core.FixedStrategyError("재계산할 BTC 주문안이 없습니다.")
    instruction = core._active_target_instruction(
        state,
        target_weight=float(pending["target_weight"]),
        price_krw=float(price),
        kind=str(pending["kind"]),
        step=int(pending["step"]),
        reason=str(pending["reason"]),
    )
    pending.update(
        {
            "side": instruction.side,
            "expected_amount_krw": instruction.expected_amount_krw,
            "target_btc_value_krw": instruction.target_btc_value_krw,
            "active_equity_krw": instruction.active_equity_krw,
            "reference_price_krw": instruction.reference_price_krw,
            "last_recalculated_at_utc": core.iso_utc(now),
            "plan_expired": False,
            "policy_version": hybrid.VERSION,
        }
    )
    if reset_age:
        pending.update(
            {
                "age_anchor_at_utc": core.iso_utc(now),
                "notice_24h_sent": False,
                "notice_72h_sent": False,
                "notice_7d_sent": False,
                "first_reminder_at_utc": core.iso_utc(
                    now + timedelta(minutes=core.FIRST_REMINDER_MINUTES)
                ),
                "first_reminder_sent": False,
                "last_daily_reminder": None,
            }
        )
    core.append_audit(
        state,
        "ORDER_INSTRUCTION_RECALCULATED",
        {
            "kind": instruction.kind,
            "step": instruction.step,
            "reference_price_krw": instruction.reference_price_krw,
            "expected_amount_krw": instruction.expected_amount_krw,
            "reset_age": reset_age,
        },
        now,
    )
    return instruction


def sync_reminder_message(state: Mapping[str, Any]) -> str:
    pending = state["telegram"].get("pending_sync") or {}
    status = "만료" if pending.get("plan_expired") else "유효"
    return "\n".join(
        [
            "[잔고 동기화가 필요합니다]",
            f"- 대기 주문: {pending.get('kind', '-')} {pending.get('step', '-')}차",
            f"- 주문 검토액: {bot.krw(float(pending.get('expected_amount_krw', 0)))}",
            f"- 주문안 상태: {status}",
            "",
            "이미 주문했다면 실제 BTC·원화잔액을 동기화하세요.",
            "아직 주문하지 않았다면 현재가 재계산 버튼을 사용하세요.",
        ]
    )


def _age_notice(
    state: dict[str, Any], client: bot.TelegramClient, now: datetime
) -> bool:
    pending = state["telegram"].get("pending_sync")
    if not isinstance(pending, dict):
        return False
    hybrid.prepare_pending(pending)
    elapsed, price = _age(pending, now), core.current_price_from_state(state)
    today = now.astimezone(core.KST).date().isoformat()

    if elapsed >= EXPIRE_AFTER and not pending.get("notice_7d_sent"):
        pending.update(
            {
                "notice_7d_sent": True,
                "plan_expired": True,
                "first_reminder_sent": True,
                "last_daily_reminder": today,
            }
        )
        core.append_audit(state, "ORDER_INSTRUCTION_EXPIRED", {}, now)
        client.send_message(
            "\n".join(
                [
                    "[BTC 주문안 7일 경과 · 만료]",
                    "기존 주문금액은 더 이상 사용하지 마세요.",
                    "",
                    "이미 주문했다면 잔고 동기화,",
                    "아직 주문하지 않았다면 현재가 재계산을 선택하세요.",
                    "다음 전략 단계는 잔고동기화 전까지 진행하지 않습니다.",
                ]
            ),
            inline_keyboard=order_keyboard(),
        )
        return True

    if elapsed >= WARN_AFTER and not pending.get("notice_72h_sent"):
        pending.update(
            {"notice_24h_sent": True, "notice_72h_sent": True, "first_reminder_sent": True, "last_daily_reminder": today}
        )
        if price is not None:
            instruction = refresh_pending(state, price=price, now=now, reset_age=False)
            client.send_message(
                "[BTC 주문안 3일 경과 · 체결 지연 주의]\n현재가로 다시 계산했습니다.\n\n"
                + order_message(state, instruction),
                inline_keyboard=order_keyboard(),
            )
        else:
            client.send_message("[BTC 주문안 3일 경과]\n가격 데이터가 없어 재계산하지 못했습니다.", inline_keyboard=order_keyboard())
        return True

    if elapsed >= RECALC_AFTER and not pending.get("notice_24h_sent"):
        pending.update(
            {"notice_24h_sent": True, "first_reminder_sent": True, "last_daily_reminder": today}
        )
        if price is not None:
            instruction = refresh_pending(state, price=price, now=now, reset_age=False)
            client.send_message(
                "[BTC 주문안 24시간 경과 · 자동 재계산]\n아래 최신 금액을 사용하세요.\n\n"
                + order_message(state, instruction),
                inline_keyboard=order_keyboard(),
            )
        else:
            client.send_message("[BTC 주문안 24시간 경과]\n가격 데이터가 없어 재계산하지 못했습니다.", inline_keyboard=order_keyboard())
        return True
    return False


def handle_callback(
    state: dict[str, Any],
    client: bot.TelegramClient,
    callback: Mapping[str, Any],
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    if str(callback.get("data", "")) != "sync:refresh":
        _ORIG_CALLBACK(state, client, callback, now_utc=now_utc, price=price, block=block)
        return
    client.answer_callback(str(callback.get("id", "")), "현재가로 재계산합니다.")
    current = price or core.current_price_from_state(state)
    if current is None:
        client.send_message("현재 BTC 가격을 확인하지 못했습니다.", menu=True)
        return
    try:
        instruction = refresh_pending(state, price=current, now=now_utc, reset_age=True)
    except core.FixedStrategyError as exc:
        client.send_message(str(exc), menu=True)
        return
    client.send_message(
        "[BTC 주문안 현재가 재계산 완료]\n\n" + order_message(state, instruction),
        inline_keyboard=order_keyboard(),
    )


def process_reminders(
    state: dict[str, Any],
    client: bot.TelegramClient,
    *,
    now_utc: datetime,
    daily_check: bool,
) -> None:
    _age_notice(state, client, now_utc)
    _ORIG_REMINDERS(state, client, now_utc=now_utc, daily_check=daily_check)


def install() -> None:
    bot.status_message = status_message
    bot.block_event_message = block_event_message
    bot.order_keyboard = order_keyboard
    bot.order_message = order_message
    bot.sync_reminder_message = sync_reminder_message
    bot.handle_callback = handle_callback
    bot.process_reminders = process_reminders
