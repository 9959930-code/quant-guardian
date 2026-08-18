from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import btc_fixed_advisory as core
import btc_fixed_telegram_bot as bot


STRATEGY_VERSION = "btc-fixed-six-clock-hybrid-1.1"
STATE_SCHEMA_VERSION = 5
LEGACY_STRATEGY_VERSION = "btc-fixed-six-upbit-telegram-1.0"
LEGACY_STATE_SCHEMA_VERSION = 4

HALVING_INTERVAL = 210_000
ENTRY_WATCH_PROGRESS = 0.625
ENTRY_PROGRESS = 0.65
EXIT_WARNING_PROGRESS = 0.35
EXIT_PROGRESS_TARGETS = (0.36, 0.37, 0.38)

ENTRY_WATCH_OFFSET = round(HALVING_INTERVAL * ENTRY_WATCH_PROGRESS)
ENTRY_OFFSET = round(HALVING_INTERVAL * ENTRY_PROGRESS)
EXIT_WARNING_OFFSET = round(HALVING_INTERVAL * EXIT_WARNING_PROGRESS)
EXIT_OFFSETS = tuple(
    round(HALVING_INTERVAL * progress) for progress in EXIT_PROGRESS_TARGETS
)

ESTIMATED_BLOCK_MINUTES = 10.0
ORDER_RECALCULATE_AFTER_HOURS = 24
ORDER_WARNING_AFTER_DAYS = 3
ORDER_STALE_AFTER_DAYS = 7

_INSTALLED = False

_ORIGINALS = {
    "strategy_version": core.STRATEGY_VERSION,
    "state_schema_version": core.STATE_SCHEMA_VERSION,
    "entry_progress": core.ENTRY_PROGRESS,
    "exit_progress": core.EXIT_PROGRESS,
    "initial_state": core._initial_state,
    "load_state": core.load_state,
    "detect_block_events": core.detect_block_events,
    "create_official_order": core.create_official_order,
    "next_condition_text": core.next_condition_text,
    "block_event_message": bot.block_event_message,
    "status_message": bot.status_message,
    "order_message": bot.order_message,
    "order_keyboard": bot.order_keyboard,
    "sync_reminder_message": bot.sync_reminder_message,
    "handle_callback": bot.handle_callback,
}


def block_offset(height: int) -> int:
    return int(height) % HALVING_INTERVAL


def remaining_blocks(height: int, target_offset: int) -> int:
    return max(0, int(target_offset) - block_offset(height))


def _schema_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _normalize_epoch_list(value: Any) -> list[int]:
    rows: list[int] = []
    if isinstance(value, list):
        for item in value:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed not in rows:
                rows.append(parsed)
    return rows[-20:]


def _ensure_hybrid_fields(state: dict[str, Any]) -> dict[str, Any]:
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["strategy_version"] = STRATEGY_VERSION
    strategy = state.setdefault("strategy", {})
    for key in (
        "entry_watch_alerted_epochs",
        "entry_alerted_epochs",
        "halving_alerted_epochs",
        "exit_alerted_epochs",
    ):
        strategy[key] = _normalize_epoch_list(strategy.get(key))
    funding: list[str] = []
    for item in strategy.get("funding_alerts_sent", []):
        text = str(item)
        if text not in funding:
            funding.append(text)
    strategy["funding_alerts_sent"] = funding[-20:]
    return state


def hybrid_initial_state(
    now_utc: datetime,
    budget_krw: float = core.DEFAULT_BUDGET_KRW,
) -> dict[str, Any]:
    return _ensure_hybrid_fields(
        _ORIGINALS["initial_state"](now_utc, budget_krw)
    )


def hybrid_load_state(
    path: Path,
    *,
    now_utc: datetime | None = None,
    reset: bool = False,
    budget_krw: float = core.DEFAULT_BUDGET_KRW,
) -> tuple[dict[str, Any], bool]:
    now = now_utc or core.utc_now()
    if reset or not path.exists():
        return hybrid_initial_state(now, budget_krw), True

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise core.FixedStrategyError(
            f"BTC 상태파일을 읽지 못했습니다: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise core.FixedStrategyError(
            "BTC 상태파일 구조가 올바르지 않아 자동 초기화하지 않았습니다."
        )

    schema = _schema_int(state.get("schema_version"))
    version = state.get("strategy_version")
    migrated = (
        schema == LEGACY_STATE_SCHEMA_VERSION
        and version == LEGACY_STRATEGY_VERSION
    )
    current = (
        schema == STATE_SCHEMA_VERSION
        and version == STRATEGY_VERSION
    )
    if not migrated and not current:
        raise core.FixedStrategyError(
            "알 수 없는 BTC 상태 버전입니다. 자산 기록 보호를 위해 "
            "자동 초기화하지 않았습니다."
        )

    if migrated:
        old_schema, old_version = schema, version
        _ensure_hybrid_fields(state)
        core.append_audit(
            state,
            "CLOCK_HYBRID_STATE_MIGRATED",
            {
                "from_schema": old_schema,
                "from_strategy_version": old_version,
                "to_schema": STATE_SCHEMA_VERSION,
                "to_strategy_version": STRATEGY_VERSION,
                "account_preserved": True,
                "telegram_state_preserved": True,
            },
            now,
        )
    else:
        _ensure_hybrid_fields(state)

    core._validate_state(state)
    return state, False


def estimated_entry_trigger_utc(
    block: core.BlockContext,
    now_utc: datetime,
) -> datetime:
    return now_utc.astimezone(UTC) + timedelta(
        minutes=remaining_blocks(block.height, ENTRY_OFFSET)
        * ESTIMATED_BLOCK_MINUTES
    )


def first_official_buy_kst(trigger_utc: datetime) -> datetime:
    local = trigger_utc.astimezone(core.KST)
    days_to_monday = (7 - local.weekday()) % 7
    if local.weekday() == 0 and local.time() <= core.OFFICIAL_CHECK_TIME:
        days_to_monday = 0
    elif days_to_monday == 0:
        days_to_monday = 7
    return datetime.combine(
        local.date() + timedelta(days=days_to_monday),
        core.OFFICIAL_CHECK_TIME,
        tzinfo=core.KST,
    )


def business_days_until(start_date: date, target_date: date) -> int:
    if target_date < start_date:
        return -1
    count = 0
    cursor = start_date
    while cursor < target_date:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


def _funding_preparation_event(
    state: dict[str, Any],
    block: core.BlockContext,
    now_utc: datetime,
) -> dict[str, Any] | None:
    strategy = state["strategy"]
    if strategy.get("phase") != "WAITING_ENTRY":
        return None
    if state.get("telegram", {}).get("pending_sync"):
        return None
    if block_offset(block.height) >= ENTRY_OFFSET:
        return None

    now_kst = now_utc.astimezone(core.KST)
    if (
        now_kst.weekday() >= 5
        or now_kst.time() < core.OFFICIAL_CHECK_TIME
    ):
        return None

    trigger_utc = estimated_entry_trigger_utc(block, now_utc)
    buy_kst = first_official_buy_kst(trigger_utc)
    days = business_days_until(now_kst.date(), buy_kst.date())
    if 0 <= days <= 3:
        lead = 3
    elif 3 < days <= 5:
        lead = 5
    else:
        return None

    key = f"{block.epoch}:{lead}"
    sent = list(strategy.get("funding_alerts_sent", []))
    if key in sent:
        return None
    sent.append(key)
    strategy["funding_alerts_sent"] = sent[-20:]

    account = state["account"]
    prepare_total = max(0.0, float(account.get("cash_krw", 0.0)))
    event = {
        "type": "ENTRY_FUNDING_PREP",
        "block": block,
        "lead_business_days": lead,
        "estimated_business_days": days,
        "estimated_trigger_kst": trigger_utc.astimezone(core.KST).isoformat(),
        "estimated_first_buy_kst": buy_kst.isoformat(),
        "remaining_blocks": remaining_blocks(block.height, ENTRY_OFFSET),
        "prepare_total_krw": prepare_total,
        "first_target_krw": prepare_total * core.ENTRY_TARGETS[0],
        "price_krw": account.get("last_price_krw"),
    }
    core.append_audit(
        state,
        "ENTRY_FUNDING_PREPARATION_ALERTED",
        {
            "epoch": block.epoch,
            "lead_business_days": lead,
            "estimated_business_days": days,
            "estimated_first_buy_kst": buy_kst.isoformat(),
            "remaining_blocks": event["remaining_blocks"],
        },
        now_utc,
    )
    return event


def _append_epoch_once(
    strategy: dict[str, Any],
    key: str,
    epoch: int,
) -> bool:
    rows = _normalize_epoch_list(strategy.get(key))
    if int(epoch) in rows:
        return False
    rows.append(int(epoch))
    strategy[key] = rows[-20:]
    return True


def hybrid_detect_block_events(
    state: dict[str, Any],
    block: core.BlockContext,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    strategy = state["strategy"]
    events: list[dict[str, Any]] = []

    funding = _funding_preparation_event(state, block, now_utc)
    if funding is not None:
        events.append(funding)

    phase = str(strategy.get("phase"))
    offset = block_offset(block.height)
    price = state.get("account", {}).get("last_price_krw")

    if (
        phase == "WAITING_ENTRY"
        and offset >= ENTRY_WATCH_OFFSET
        and _append_epoch_once(
            strategy, "entry_watch_alerted_epochs", block.epoch
        )
    ):
        event = {
            "type": "ENTRY_WATCH",
            "block": block,
            "remaining_blocks": remaining_blocks(block.height, ENTRY_OFFSET),
            "price_krw": price,
        }
        events.append(event)
        core.append_audit(
            state,
            "ENTRY_WATCH_ALERTED",
            {
                "epoch": block.epoch,
                "height": block.height,
                "remaining_blocks": event["remaining_blocks"],
            },
            now_utc,
        )

    if (
        phase == "WAITING_ENTRY"
        and offset >= ENTRY_OFFSET
        and _append_epoch_once(
            strategy, "entry_alerted_epochs", block.epoch
        )
    ):
        events.append(
            {
                "type": "ENTRY_THRESHOLD",
                "block": block,
                "price_krw": price,
            }
        )
        core.append_audit(
            state,
            "ENTRY_THRESHOLD_ALERTED",
            {"epoch": block.epoch, "height": block.height},
            now_utc,
        )

    previous_epoch = strategy.get("last_block_epoch")
    if (
        previous_epoch is not None
        and block.epoch > int(previous_epoch)
        and _append_epoch_once(
            strategy, "halving_alerted_epochs", block.epoch
        )
    ):
        events.append({"type": "HALVING", "block": block})
        core.append_audit(
            state,
            "HALVING_ALERTED",
            {"epoch": block.epoch, "height": block.height},
            now_utc,
        )

    cycle_epoch = strategy.get("cycle_epoch")
    if (
        phase in {"HOLD", "EXIT"}
        and cycle_epoch is not None
        and block.epoch > int(cycle_epoch)
        and offset >= EXIT_WARNING_OFFSET
        and _append_epoch_once(
            strategy, "exit_alerted_epochs", block.epoch
        )
    ):
        event = {
            "type": "EXIT_WARNING",
            "block": block,
            "remaining_blocks": remaining_blocks(
                block.height, EXIT_OFFSETS[0]
            ),
            "price_krw": price,
        }
        events.append(event)
        core.append_audit(
            state,
            "EXIT_WARNING_ALERTED",
            {
                "epoch": block.epoch,
                "height": block.height,
                "remaining_blocks": event["remaining_blocks"],
            },
            now_utc,
        )

    strategy["last_block_height"] = block.height
    strategy["last_block_epoch"] = block.epoch
    strategy["last_block_progress"] = block.cycle_progress
    state["updated_at_utc"] = core.iso_utc(now_utc)
    return events


def _exit_reason(step: int) -> str:
    progress = EXIT_PROGRESS_TARGETS[step - 1]
    suffix = "최종 분할매도" if step == 3 else f"{step}차 분할매도"
    return (
        f"반감기 진행률 {progress * 100:.0f}% 도달에 따른 {suffix}"
    )


def _exit_threshold_met(
    block: core.BlockContext,
    cycle_epoch: int,
    required_offset: int,
) -> bool:
    target_epoch = int(cycle_epoch) + 1
    if block.epoch > target_epoch:
        return True
    return (
        block.epoch == target_epoch
        and block_offset(block.height) >= required_offset
    )


def hybrid_create_official_order(
    state: dict[str, Any],
    *,
    block: core.BlockContext,
    price_krw: float,
    now_utc: datetime,
) -> dict[str, Any] | None:
    strategy = state["strategy"]
    telegram = state["telegram"]
    strategy["last_official_monday"] = (
        now_utc.astimezone(core.KST).date().isoformat()
    )
    state["updated_at_utc"] = core.iso_utc(now_utc)

    if telegram.get("pending_sync"):
        return {
            "type": "SYNC_BLOCK",
            "pending_sync": telegram["pending_sync"],
            "message": (
                "이전 주문 후 잔고 동기화가 끝나지 않아 "
                "다음 단계를 보류합니다."
            ),
        }

    phase = str(strategy.get("phase"))
    offset = block_offset(block.height)
    instruction: core.OrderInstruction | None = None

    if phase == "WAITING_ENTRY" and offset >= ENTRY_OFFSET:
        if float(state["account"].get("reserve_next_krw", 0.0)) > 0:
            state["account"]["reserve_next_krw"] = 0.0
            core.append_audit(
                state, "NEXT_CYCLE_RESERVE_ACTIVATED", {}, now_utc
            )
        strategy["cycle_epoch"] = block.epoch
        strategy["entry_steps_completed"] = 0
        strategy["exit_steps_completed"] = 0
        instruction = core._active_target_instruction(
            state,
            target_weight=core.ENTRY_TARGETS[0],
            price_krw=price_krw,
            kind="ENTRY",
            step=1,
            reason="반감기 진행률 65% 도달 후 1차 분할매수",
        )
    elif phase == "ENTRY":
        completed = int(strategy.get("entry_steps_completed", 0))
        if completed < 3:
            step = completed + 1
            instruction = core._active_target_instruction(
                state,
                target_weight=core.ENTRY_TARGETS[step - 1],
                price_krw=price_krw,
                kind="ENTRY",
                step=step,
                reason=f"고정 6회 전략 {step}차 분할매수",
            )
    elif phase == "HOLD":
        cycle_epoch = strategy.get("cycle_epoch")
        if bool(strategy.get("correction_buy_pending")):
            instruction = core._active_target_instruction(
                state,
                target_weight=1.0,
                price_krw=price_krw,
                kind="CORRECTION",
                step=1,
                reason="3차 매수 완료 후 현재 사이클 추가입금 보정매수",
            )
        elif (
            cycle_epoch is not None
            and _exit_threshold_met(
                block, int(cycle_epoch), EXIT_OFFSETS[0]
            )
        ):
            strategy["exit_steps_completed"] = 0
            instruction = core._active_target_instruction(
                state,
                target_weight=core.EXIT_TARGETS[0],
                price_krw=price_krw,
                kind="EXIT",
                step=1,
                reason=_exit_reason(1),
            )
    elif phase == "EXIT":
        completed = int(strategy.get("exit_steps_completed", 0))
        cycle_epoch = strategy.get("cycle_epoch")
        if (
            completed < 3
            and cycle_epoch is not None
            and _exit_threshold_met(
                block, int(cycle_epoch), EXIT_OFFSETS[completed]
            )
        ):
            step = completed + 1
            instruction = core._active_target_instruction(
                state,
                target_weight=core.EXIT_TARGETS[step - 1],
                price_krw=price_krw,
                kind="EXIT",
                step=step,
                reason=_exit_reason(step),
            )

    if instruction is None:
        return None
    pending = core._new_pending_sync(state, instruction, now_utc)
    return {
        "type": "ORDER",
        "instruction": instruction,
        "pending_sync": pending,
    }


def hybrid_next_condition_text(
    state: Mapping[str, Any],
    block: core.BlockContext,
) -> str:
    strategy = state["strategy"]
    phase = str(strategy.get("phase"))
    offset = block_offset(block.height)

    if phase == "WAITING_ENTRY":
        if offset < ENTRY_WATCH_OFFSET:
            return (
                "저점 관찰 62.5%까지 "
                f"{ENTRY_WATCH_OFFSET - offset:,}블록, "
                "매수 65%까지 "
                f"{ENTRY_OFFSET - offset:,}블록"
            )
        if offset < ENTRY_OFFSET:
            return (
                "저점 관찰 중 · 65% 1차 매수까지 "
                f"{ENTRY_OFFSET - offset:,}블록"
            )
        return "65% 도달 · 다음 공식 월요일 1차 분할매수"

    if phase == "ENTRY":
        completed = int(strategy.get("entry_steps_completed", 0))
        return f"다음 공식 월요일 {completed + 1}차 분할매수"

    if phase == "HOLD":
        if bool(strategy.get("correction_buy_pending")):
            return "다음 공식 월요일 추가입금 보정매수"
        cycle_epoch = strategy.get("cycle_epoch")
        if cycle_epoch is None or block.epoch <= int(cycle_epoch):
            return "다음 반감기 후 35% 경고, 36·37·38% 분할매도"
        if block.epoch > int(cycle_epoch) + 1:
            return "매도 임계구간 경과 · 다음 공식 월요일 1차 분할매도"
        if offset < EXIT_WARNING_OFFSET:
            return (
                "고점 위험경고 35%까지 "
                f"{EXIT_WARNING_OFFSET - offset:,}블록"
            )
        if offset < EXIT_OFFSETS[0]:
            return (
                "1차 매도 36%까지 "
                f"{EXIT_OFFSETS[0] - offset:,}블록"
            )
        return "36% 도달 · 다음 공식 월요일 1차 분할매도"

    if phase == "EXIT":
        completed = int(strategy.get("exit_steps_completed", 0))
        if completed >= 3:
            return "분할매도 완료"
        cycle_epoch = strategy.get("cycle_epoch")
        required_offset = EXIT_OFFSETS[completed]
        progress = EXIT_PROGRESS_TARGETS[completed]
        if (
            cycle_epoch is not None
            and not _exit_threshold_met(
                block, int(cycle_epoch), required_offset
            )
        ):
            return (
                f"{completed + 1}차 매도 {progress * 100:.0f}%까지 "
                f"{required_offset - offset:,}블록"
            )
        return f"다음 공식 월요일 {completed + 1}차 분할매도"
    return "-"


def _format_kst(value: str) -> str:
    return (
        datetime.fromisoformat(value)
        .astimezone(core.KST)
        .strftime("%Y-%m-%d %H:%M KST")
    )


def hybrid_block_event_message(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type"))
    block = event.get("block")
    if not isinstance(block, core.BlockContext):
        return _ORIGINALS["block_event_message"](event)

    if event_type == "ENTRY_FUNDING_PREP":
        return "\n".join(
            [
                (
                    "💰 BTC 첫 매수 자금준비 "
                    f"{int(event['lead_business_days'])}영업일 전"
                ),
                "",
                "블록 생성속도 10분 기준의 예상 일정입니다.",
                (
                    "- 예상 65% 도달: "
                    f"{_format_kst(str(event['estimated_trigger_kst']))}"
                ),
                (
                    "- 예상 1차 매수 점검: "
                    f"{_format_kst(str(event['estimated_first_buy_kst']))}"
                ),
                (
                    "- 현재 추정 영업일: "
                    f"{int(event['estimated_business_days'])}일"
                ),
                f"- 65%까지: {int(event['remaining_blocks']):,}블록",
                f"- 준비할 총 원화: {bot.krw(event['prepare_total_krw'])}",
                (
                    "- 1차 목표액 근사: "
                    f"{bot.krw(event['first_target_krw'])}"
                ),
                "",
                "실제 블록 속도에 따라 날짜는 앞뒤로 바뀔 수 있습니다.",
                "자동주문은 없으며 Upbit 자금 준비를 위한 사전 알림입니다.",
            ]
        )

    if event_type == "ENTRY_WATCH":
        return "\n".join(
            [
                "🟢 BTC 저점 관찰 구간 진입",
                "",
                "현재 반감기 사이클 진행률이 62.5%에 도달했습니다.",
                "아직 매수 주문은 없습니다.",
                "",
                f"- 현재 블록 높이: {block.height:,}",
                f"- 현재 진행률: {block.cycle_progress * 100:.2f}%",
                f"- 현재 BTC 가격: {bot.krw(event.get('price_krw'))}",
                (
                    "- 65% 매수 시작까지: "
                    f"{int(event['remaining_blocks']):,}블록"
                ),
                "",
                "65% 도달 후 공식 월요일부터 3회 분할매수가 시작됩니다.",
            ]
        )

    if event_type == "ENTRY_THRESHOLD":
        return "\n".join(
            [
                "🟢 BTC 65% 매수구간 도달",
                "",
                f"- 현재 블록 높이: {block.height:,}",
                f"- 현재 진행률: {block.cycle_progress * 100:.2f}%",
                f"- 현재 BTC 가격: {bot.krw(event.get('price_krw'))}",
                "",
                "1차 매수 주문안은 다음 공식 월요일 09:17 KST에 계산합니다.",
                "자동주문은 없습니다.",
            ]
        )

    if event_type == "EXIT_WARNING":
        return "\n".join(
            [
                "🟠 BTC 고점 위험구간 진입",
                "",
                "다음 반감기 이후 진행률이 35%에 도달했습니다.",
                "아직 매도 주문은 없습니다.",
                "",
                f"- 현재 블록 높이: {block.height:,}",
                f"- 현재 진행률: {block.cycle_progress * 100:.2f}%",
                f"- 현재 BTC 가격: {bot.krw(event.get('price_krw'))}",
                (
                    "- 1차 매도 36%까지: "
                    f"{int(event['remaining_blocks']):,}블록"
                ),
                "",
                "예정 매도 단계:",
                "- 36% → BTC 2/3 유지",
                "- 37% → BTC 1/3 유지",
                "- 38% → BTC 0% 전환",
            ]
        )

    return _ORIGINALS["block_event_message"](event)


def hybrid_status_message(
    state: Mapping[str, Any],
    *,
    price: float | None,
    block: core.BlockContext | None,
    title: str = "현재 상태",
) -> str:
    base = _ORIGINALS["status_message"](
        state, price=price, block=block, title=title
    )
    lines = [
        "",
        "[시계형 1.1 규칙]",
        "- 62.5%: 저점 관찰 알림",
        "- 65%: 3주 분할매수 시작",
        "- 다음 epoch 35%: 고점 위험경고",
        "- 36%·37%·38%: 각각 1/3 분할매도",
        "- 첫 매수 예상 5·3영업일 전: 자금준비 알림",
    ]
    if (
        block is not None
        and str(state["strategy"].get("phase")) == "WAITING_ENTRY"
        and block_offset(block.height) < ENTRY_OFFSET
    ):
        observed = core.parse_iso(block.observed_at_utc) or core.utc_now()
        buy_at = first_official_buy_kst(
            estimated_entry_trigger_utc(block, observed)
        )
        lines.append(
            f"- 현재 예상 첫 매수 점검: {buy_at:%Y-%m-%d %H:%M KST}"
        )
    return base + "\n" + "\n".join(lines)


def hybrid_order_message(
    state: Mapping[str, Any],
    instruction: core.OrderInstruction,
) -> str:
    return (
        _ORIGINALS["order_message"](state, instruction)
        + "\n\n[체결 지연 안전규칙]"
        + "\n- 24시간이 지나면 알림 당시 금액을 그대로 쓰지 마세요."
        + "\n- ‘현재 금액 다시 계산’으로 최신 가격·잔고 기준 주문액을 갱신하세요."
        + "\n- 3일 경과 시 오래된 주문안 주의, 7일 경과 시 기존 금액 사용 금지."
        + "\n- 매도는 매수보다 지연 민감도가 높습니다."
    )


def hybrid_order_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {
                "text": "✅ 주문 완료 · 잔고 동기화",
                "callback_data": "sync:start",
            }
        ],
        [
            {
                "text": "🔄 현재 금액 다시 계산",
                "callback_data": "order:refresh",
            }
        ],
        [
            {
                "text": "⏰ 30분 뒤 다시 알림",
                "callback_data": "sync:later",
            }
        ],
        [
            {
                "text": "❌ 주문안 취소",
                "callback_data": "order:cancel",
            }
        ],
    ]


def _pending_age(
    now_utc: datetime,
    pending: Mapping[str, Any],
) -> timedelta:
    created = core.parse_iso(str(pending.get("created_at_utc") or ""))
    if created is None:
        return timedelta(0)
    return max(timedelta(0), now_utc.astimezone(UTC) - created)


def hybrid_sync_reminder_message(state: Mapping[str, Any]) -> str:
    base = _ORIGINALS["sync_reminder_message"](state)
    pending = state.get("telegram", {}).get("pending_sync")
    if not isinstance(pending, Mapping):
        return base
    age = _pending_age(core.utc_now(), pending)
    if age >= timedelta(days=ORDER_STALE_AFTER_DAYS):
        note = (
            "7일 이상 지난 주문안입니다. 기존 금액으로 주문하지 말고 "
            "재계산하거나 취소하세요."
        )
    elif age >= timedelta(days=ORDER_WARNING_AFTER_DAYS):
        note = (
            "3일 이상 지난 주문안입니다. 가격경로가 달라졌으므로 "
            "현재 금액 재계산을 권합니다."
        )
    elif age >= timedelta(hours=ORDER_RECALCULATE_AFTER_HOURS):
        note = (
            "24시간이 지난 주문안입니다. 알림 당시 금액 대신 "
            "현재 금액을 다시 계산하세요."
        )
    else:
        note = (
            "주문하지 않았다면 현재 금액 다시 계산 또는 "
            "주문안 취소를 선택할 수 있습니다."
        )
    return base + "\n\n[주문안 상태]\n" + note


def refresh_pending_order(
    state: dict[str, Any],
    *,
    price_krw: float,
    now_utc: datetime,
) -> core.OrderInstruction:
    pending = state.get("telegram", {}).get("pending_sync")
    if not isinstance(pending, Mapping):
        raise core.FixedStrategyError(
            "다시 계산할 BTC 주문안이 없습니다."
        )
    instruction = core._active_target_instruction(
        state,
        target_weight=float(pending["target_weight"]),
        price_krw=float(price_krw),
        kind=str(pending["kind"]),
        step=int(pending["step"]),
        reason=str(
            pending.get("reason") or "현재 가격 기준 주문액 재계산"
        ),
    )
    old_id = pending.get("id")
    new_pending = core._new_pending_sync(state, instruction, now_utc)
    new_pending["refreshed_from_id"] = old_id
    core.append_audit(
        state,
        "ORDER_INSTRUCTION_REFRESHED",
        {
            "old_id": old_id,
            "new_id": new_pending.get("id"),
            "kind": instruction.kind,
            "step": instruction.step,
            "side": instruction.side,
            "expected_amount_krw": instruction.expected_amount_krw,
            "reference_price_krw": instruction.reference_price_krw,
        },
        now_utc,
    )
    return instruction


def cancel_pending_order(
    state: dict[str, Any],
    *,
    now_utc: datetime,
) -> bool:
    pending = state.get("telegram", {}).get("pending_sync")
    if not isinstance(pending, Mapping):
        return False
    core.append_audit(
        state,
        "ORDER_INSTRUCTION_CANCELLED",
        {"pending_sync": dict(pending)},
        now_utc,
    )
    state["telegram"]["pending_sync"] = None
    state["updated_at_utc"] = core.iso_utc(now_utc)
    return True


def hybrid_handle_callback(
    state: dict[str, Any],
    client: bot.TelegramClient,
    callback: Mapping[str, Any],
    *,
    now_utc: datetime,
    price: float | None,
    block: core.BlockContext | None,
) -> None:
    data = str(callback.get("data", ""))
    if data == "order:refresh":
        client.answer_callback(
            str(callback.get("id")), "현재 금액을 다시 계산합니다."
        )
        if price is None:
            client.send_message(
                "현재 BTC 가격을 확인하지 못해 주문액을 다시 계산할 수 없습니다.",
                menu=True,
            )
            return
        try:
            instruction = refresh_pending_order(
                state, price_krw=price, now_utc=now_utc
            )
        except core.FixedStrategyError as exc:
            client.send_message(str(exc), menu=True)
            return
        if instruction.side == "NONE":
            result = core.complete_pending_sync(
                state,
                btc_quantity=float(state["account"]["btc_quantity"]),
                cash_krw=float(state["account"]["cash_krw"]),
                now_utc=now_utc,
            )
            completed = result.get("completed_order")
            detail = (
                f"\n완료 단계: {completed.get('kind')} "
                f"{completed.get('step')}차"
                if isinstance(completed, Mapping)
                else ""
            )
            client.send_message(
                "현재 실제 비중이 이미 단계 목표와 같아 "
                "추가 주문 없이 단계를 완료했습니다."
                + detail,
                menu=True,
            )
            return
        client.send_message(
            hybrid_order_message(state, instruction),
            inline_keyboard=hybrid_order_keyboard(),
        )
        return

    if data == "order:cancel":
        client.answer_callback(
            str(callback.get("id")), "주문안을 취소합니다."
        )
        if cancel_pending_order(state, now_utc=now_utc):
            client.send_message(
                "BTC 주문안을 취소했습니다.\n"
                "전략 단계는 진행시키지 않았으며 "
                "다음 공식 월요일에 다시 판단합니다.",
                menu=True,
            )
        else:
            client.send_message(
                "취소할 BTC 주문안이 없습니다.", menu=True
            )
        return

    _ORIGINALS["handle_callback"](
        state,
        client,
        callback,
        now_utc=now_utc,
        price=price,
        block=block,
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    core.STRATEGY_VERSION = STRATEGY_VERSION
    core.STATE_SCHEMA_VERSION = STATE_SCHEMA_VERSION
    core.ENTRY_PROGRESS = ENTRY_PROGRESS
    core.EXIT_PROGRESS = EXIT_WARNING_PROGRESS
    core._initial_state = hybrid_initial_state
    core.load_state = hybrid_load_state
    core.detect_block_events = hybrid_detect_block_events
    core.create_official_order = hybrid_create_official_order
    core.next_condition_text = hybrid_next_condition_text

    bot.block_event_message = hybrid_block_event_message
    bot.status_message = hybrid_status_message
    bot.order_message = hybrid_order_message
    bot.order_keyboard = hybrid_order_keyboard
    bot.sync_reminder_message = hybrid_sync_reminder_message
    bot.handle_callback = hybrid_handle_callback
    _INSTALLED = True


def uninstall() -> None:
    global _INSTALLED
    if not _INSTALLED:
        return

    core.STRATEGY_VERSION = _ORIGINALS["strategy_version"]
    core.STATE_SCHEMA_VERSION = _ORIGINALS["state_schema_version"]
    core.ENTRY_PROGRESS = _ORIGINALS["entry_progress"]
    core.EXIT_PROGRESS = _ORIGINALS["exit_progress"]
    core._initial_state = _ORIGINALS["initial_state"]
    core.load_state = _ORIGINALS["load_state"]
    core.detect_block_events = _ORIGINALS["detect_block_events"]
    core.create_official_order = _ORIGINALS["create_official_order"]
    core.next_condition_text = _ORIGINALS["next_condition_text"]

    bot.block_event_message = _ORIGINALS["block_event_message"]
    bot.status_message = _ORIGINALS["status_message"]
    bot.order_message = _ORIGINALS["order_message"]
    bot.order_keyboard = _ORIGINALS["order_keyboard"]
    bot.sync_reminder_message = _ORIGINALS[
        "sync_reminder_message"
    ]
    bot.handle_callback = _ORIGINALS["handle_callback"]
    _INSTALLED = False
