from __future__ import annotations

import json
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import btc_fixed_advisory as core

VERSION = "btc-fixed-six-clock-hybrid-1.1"
SCHEMA = 5
LEGACY_VERSION = "btc-fixed-six-upbit-telegram-1.0"
LEGACY_SCHEMA = 4
INTERVAL = 210_000
WATCH = round(INTERVAL * 0.625)
ENTRY = round(INTERVAL * 0.65)
WARNING = round(INTERVAL * 0.35)
EXITS = tuple(round(INTERVAL * x) for x in (0.36, 0.37, 0.38))
EXIT_PCT = (36, 37, 38)

_ORIG_INITIAL = core._initial_state
_ORIG_VALIDATE = core._validate_state
_ORIG_SAVE = core.save_state


def offset(block: core.BlockContext) -> int:
    return int(block.height) % INTERVAL


def _trim(values: Any) -> list[int]:
    out: list[int] = []
    for value in list(values or []):
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item not in out:
            out.append(item)
    return out[-10:]


def prepare_pending(pending: dict[str, Any]) -> None:
    created = str(pending.get("created_at_utc") or core.iso_utc(core.utc_now()))
    defaults = {
        "original_created_at_utc": created,
        "age_anchor_at_utc": created,
        "last_recalculated_at_utc": None,
        "notice_24h_sent": False,
        "notice_72h_sent": False,
        "notice_7d_sent": False,
        "plan_expired": False,
        "policy_version": VERSION,
    }
    for key, value in defaults.items():
        pending.setdefault(key, value)


def apply_defaults(state: dict[str, Any]) -> dict[str, Any]:
    state["schema_version"] = SCHEMA
    state["strategy_version"] = VERSION
    strategy = state.setdefault("strategy", {})
    strategy["entry_watch_alerted_epochs"] = _trim(strategy.get("entry_watch_alerted_epochs"))
    strategy["entry_alerted_epochs"] = _trim(strategy.get("entry_alerted_epochs"))
    strategy["halving_alerted_epochs"] = _trim(strategy.get("halving_alerted_epochs"))
    legacy_exit = strategy.get("exit_alerted_epochs", [])
    strategy["exit_warning_alerted_epochs"] = _trim(
        strategy.get("exit_warning_alerted_epochs", legacy_exit)
    )
    strategy["exit_alerted_epochs"] = _trim(legacy_exit)
    height = strategy.get("last_block_height")
    strategy["last_block_offset"] = None if height is None else int(height) % INTERVAL
    pending = state.setdefault("telegram", {}).get("pending_sync")
    if isinstance(pending, dict):
        prepare_pending(pending)
    migrations = state.setdefault("migrations", [])
    if "btc-fixed-six-v4-to-clock-hybrid-v5" not in migrations:
        migrations.append("btc-fixed-six-v4-to-clock-hybrid-v5")
    return state


def initial_state(now: datetime, budget_krw: float = core.DEFAULT_BUDGET_KRW) -> dict[str, Any]:
    return apply_defaults(_ORIG_INITIAL(now, budget_krw))


def validate_state(state: Mapping[str, Any]) -> None:
    if int(state.get("schema_version", -1)) != SCHEMA or state.get("strategy_version") != VERSION:
        raise core.FixedStrategyError("BTC clock-hybrid 상태 버전이 올바르지 않습니다.")
    _ORIG_VALIDATE(state)
    strategy = state["strategy"]
    for key in (
        "entry_watch_alerted_epochs",
        "entry_alerted_epochs",
        "halving_alerted_epochs",
        "exit_warning_alerted_epochs",
    ):
        if not isinstance(strategy.get(key), list):
            raise core.FixedStrategyError(f"BTC 알림 이력 {key}가 올바르지 않습니다.")


def load_state(
    path: Path,
    *,
    now_utc: datetime | None = None,
    reset: bool = False,
    budget_krw: float = core.DEFAULT_BUDGET_KRW,
) -> tuple[dict[str, Any], bool]:
    now = now_utc or core.utc_now()
    if reset or not path.exists():
        return initial_state(now, budget_krw), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise core.FixedStrategyError(f"상태파일을 읽지 못했습니다: {exc}") from exc
    if not isinstance(state, dict):
        raise core.FixedStrategyError("BTC 상태파일 구조가 올바르지 않습니다.")
    version, schema = str(state.get("strategy_version", "")), int(state.get("schema_version", -1))
    migrated = version == LEGACY_VERSION and schema == LEGACY_SCHEMA
    if migrated:
        apply_defaults(state)
        core.append_audit(
            state,
            "STATE_MIGRATED_TO_CLOCK_HYBRID",
            {"from_version": LEGACY_VERSION, "to_version": VERSION},
            now,
        )
    elif version == VERSION and schema == SCHEMA:
        apply_defaults(state)
    else:
        raise core.FixedStrategyError(
            "알 수 없는 BTC 상태 버전입니다. 자산 기록 보호를 위해 자동 초기화하지 않습니다."
        )
    validate_state(state)
    if migrated:
        _ORIG_SAVE(path, state)
    return state, False


def _crossed(previous_height: int | None, current_height: int, threshold: int) -> bool:
    if previous_height is None or current_height <= previous_height:
        return False
    pe, ce = previous_height // INTERVAL, current_height // INTERVAL
    po, co = previous_height % INTERVAL, current_height % INTERVAL
    return (pe == ce and po < threshold <= co) or (ce > pe and co >= threshold)


def detect_block_events(
    state: dict[str, Any], block: core.BlockContext, now_utc: datetime
) -> list[dict[str, Any]]:
    strategy = state["strategy"]
    observe_eligibility(state, block, now_utc)
    prev_raw = strategy.get("last_block_height")
    prev = None if prev_raw is None else int(prev_raw)
    prev_epoch = strategy.get("last_block_epoch")
    phase = str(strategy.get("phase"))
    events: list[dict[str, Any]] = []

    if phase == "WAITING_ENTRY" and _crossed(prev, block.height, WATCH):
        if block.epoch not in strategy["entry_watch_alerted_epochs"]:
            strategy["entry_watch_alerted_epochs"].append(block.epoch)
            events.append({"type": "ENTRY_WATCH", "block": block})
    if phase == "WAITING_ENTRY" and _crossed(prev, block.height, ENTRY):
        if block.epoch not in strategy["entry_alerted_epochs"]:
            strategy["entry_alerted_epochs"].append(block.epoch)
            events.append({"type": "ENTRY_THRESHOLD", "block": block})
    if prev_epoch is not None and block.epoch > int(prev_epoch):
        if block.epoch not in strategy["halving_alerted_epochs"]:
            strategy["halving_alerted_epochs"].append(block.epoch)
            events.append({"type": "HALVING", "block": block})
    cycle_epoch = strategy.get("cycle_epoch")
    if cycle_epoch is not None and block.epoch > int(cycle_epoch) and _crossed(prev, block.height, WARNING):
        if block.epoch not in strategy["exit_warning_alerted_epochs"]:
            strategy["exit_warning_alerted_epochs"].append(block.epoch)
            events.append({"type": "EXIT_WARNING", "block": block})

    strategy.update(
        {
            "last_block_height": block.height,
            "last_block_epoch": block.epoch,
            "last_block_progress": block.cycle_progress,
            "last_block_offset": offset(block),
        }
    )
    for key in (
        "entry_watch_alerted_epochs",
        "entry_alerted_epochs",
        "halving_alerted_epochs",
        "exit_warning_alerted_epochs",
        "exit_alerted_epochs",
    ):
        strategy[key] = _trim(strategy.get(key))
    for event in events:
        core.append_audit(
            state,
            f"BLOCK_EVENT_{event['type']}",
            {"height": block.height, "offset": offset(block)},
            now_utc,
        )
    state["updated_at_utc"] = core.iso_utc(now_utc)
    return events


def official_due(now_utc: datetime) -> datetime | None:
    """Only the current week can be recovered; never replay many old weeks."""
    local = now_utc.astimezone(core.KST)
    monday = local.date() - timedelta(days=local.weekday())
    due = datetime.combine(monday, core.OFFICIAL_CHECK_TIME, tzinfo=core.KST)
    return due if local >= due else None


def official_action_due(now_utc: datetime, state: Mapping[str, Any]) -> bool:
    due = official_due(now_utc)
    return due is not None and state["strategy"].get("last_official_monday") != due.date().isoformat()


def eligibility_key(state: Mapping[str, Any], block: core.BlockContext) -> str | None:
    """Evidence is specific to a cycle AND a single uncompleted stage."""
    st = state["strategy"]
    phase, current = str(st["phase"]), offset(block)
    cycle = st.get("cycle_epoch")
    if state["telegram"].get("pending_sync"):
        return None
    if phase == "WAITING_ENTRY" and current >= ENTRY:
        return f"{block.epoch}:ENTRY:1"
    if phase == "ENTRY" and int(st.get("entry_steps_completed", 0)) < 3:
        return f"{cycle}:ENTRY:{int(st.get('entry_steps_completed', 0)) + 1}"
    if phase == "HOLD" and st.get("correction_buy_pending"):
        return f"{cycle}:CORRECTION:1"
    if cycle is not None and block.epoch > int(cycle):
        done = int(st.get("exit_steps_completed", 0)) if phase == "EXIT" else 0
        if phase in {"HOLD", "EXIT"} and done < 3 and current >= EXITS[done]:
            return f"{block.epoch}:EXIT:{done + 1}"
    return None


def observe_eligibility(state: dict[str, Any], block: core.BlockContext, now: datetime) -> None:
    st = state["strategy"]
    key = eligibility_key(state, block)
    evidence = st.get("official_eligibility") or {}
    if key != evidence.get("key"):
        st["official_eligibility"] = (
            {"key": key, "first_observed_at_utc": core.iso_utc(now), "height": block.height}
            if key else {}
        )


def _pending(
    state: dict[str, Any], instruction: core.OrderInstruction, now: datetime
) -> dict[str, Any]:
    created = core.iso_utc(now)
    item = {
        "id": core.random_operation_id(),
        "kind": instruction.kind,
        "step": instruction.step,
        "target_weight": instruction.target_weight,
        "side": instruction.side,
        "expected_amount_krw": instruction.expected_amount_krw,
        "target_btc_value_krw": instruction.target_btc_value_krw,
        "active_equity_krw": instruction.active_equity_krw,
        "reference_price_krw": instruction.reference_price_krw,
        "reason": instruction.reason,
        "created_at_utc": created,
        "first_reminder_at_utc": core.iso_utc(now + timedelta(minutes=core.FIRST_REMINDER_MINUTES)),
        "first_reminder_sent": False,
        "last_daily_reminder": None,
    }
    prepare_pending(item)
    state["telegram"]["pending_sync"] = item
    core.append_audit(state, "ORDER_INSTRUCTION_CREATED", item, now)
    return item


def _exit_reason(step: int) -> str:
    suffix = "최종" if step == 3 else f"{step}차"
    return f"반감기 진행률 {EXIT_PCT[step - 1]}% 도달에 따른 {suffix} 분할매도"


def create_official_order(
    state: dict[str, Any],
    *,
    block: core.BlockContext,
    price_krw: float,
    now_utc: datetime,
) -> dict[str, Any] | None:
    strategy, telegram = state["strategy"], state["telegram"]
    due = official_due(now_utc)
    if due is None or not official_action_due(now_utc, state):
        return None
    strategy["last_official_monday"] = due.date().isoformat()
    mode = "scheduled" if now_utc.astimezone(core.KST).weekday() == 0 else "catch_up"
    metadata = {
        "period": due.strftime("%G-W%V"), "due_at_utc": core.iso_utc(due),
        "executed_at_utc": core.iso_utc(now_utc), "mode": mode,
    }
    strategy["last_official_execution"] = metadata
    if telegram.get("pending_sync"):
        return {"type": "SYNC_BLOCK", "pending_sync": telegram["pending_sync"]}
    phase, current = str(strategy["phase"]), offset(block)
    key = eligibility_key(state, block)
    if mode == "catch_up" and key is not None:
        evidence = strategy.get("official_eligibility") or {}
        try:
            observed = core.parse_iso(evidence.get("first_observed_at_utc"))
        except (TypeError, ValueError):
            observed = None
        if evidence.get("key") != key or observed is None or observed > due:
            metadata["mode"] = "deferred_unproven_eligibility"
            core.append_audit(state, "OFFICIAL_CATCH_UP_DEFERRED", metadata, now_utc)
            return {
                "type": "CHECK_DEFERRED",
                "message": (
                    "[BTC 지연 점검 · 주문 보류]\n"
                    f"예정 점검: {due:%Y-%m-%d %H:%M KST}\n"
                    "현재 조건은 충족됐지만, 예정시각 이전에 충족됐다는 관측 이력이 없습니다.\n"
                    "주문 시점을 앞당기지 않도록 다음 공식 월요일에 다시 판단합니다.\n"
                    "여러 주의 주문을 한꺼번에 만들지 않습니다. 자동주문 없음."
                ),
            }
    core.append_audit(state, "OFFICIAL_CHECK_EXECUTED", metadata, now_utc)
    instruction: core.OrderInstruction | None = None

    if phase == "WAITING_ENTRY" and current >= ENTRY:
        if float(state["account"].get("reserve_next_krw", 0)) > 0:
            state["account"]["reserve_next_krw"] = 0.0
            core.append_audit(state, "NEXT_CYCLE_RESERVE_ACTIVATED", {}, now_utc)
        strategy.update({"cycle_epoch": block.epoch, "entry_steps_completed": 0, "exit_steps_completed": 0})
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
                reason=f"65% 진입 후 고정 6회 전략 {step}차 분할매수",
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
        elif cycle_epoch is not None and block.epoch > int(cycle_epoch) and current >= EXITS[0]:
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
        if completed < 3 and current >= EXITS[completed]:
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
    if mode == "catch_up":
        instruction = replace(instruction, reason=(
            instruction.reason + f" · 지연 점검(예정 {due:%Y-%m-%d %H:%M KST}, 현재가 재계산)"
        ))
    pending = _pending(state, instruction, now_utc)
    pending["official_execution"] = dict(metadata)
    return {"type": "ORDER", "instruction": instruction, "pending_sync": pending}


def next_condition_text(state: Mapping[str, Any], block: core.BlockContext) -> str:
    strategy, phase, current = state["strategy"], str(state["strategy"]["phase"]), offset(block)
    if phase == "WAITING_ENTRY":
        if current < WATCH:
            return f"62.5% 저점 관찰까지 {WATCH - current:,}블록"
        if current < ENTRY:
            return f"65% 1차 매수까지 {ENTRY - current:,}블록"
        return "다음 공식 월요일 1차 분할매수"
    if phase == "ENTRY":
        return f"다음 월요일 {int(strategy.get('entry_steps_completed', 0)) + 1}차 분할매수"
    if phase == "HOLD":
        cycle_epoch = strategy.get("cycle_epoch")
        if cycle_epoch is None or block.epoch <= int(cycle_epoch):
            return "다음 반감기 후 35% 고점 위험경고 대기"
        if current < WARNING:
            return f"35% 고점 위험경고까지 {WARNING - current:,}블록"
        if current < EXITS[0]:
            return f"36% 1차 매도까지 {EXITS[0] - current:,}블록"
        return "다음 공식 월요일 36% 1차 분할매도"
    if phase == "EXIT":
        done = int(strategy.get("exit_steps_completed", 0))
        if done >= 3:
            return "매도 완료"
        if current < EXITS[done]:
            return f"{EXIT_PCT[done]}% {done + 1}차 매도까지 {EXITS[done] - current:,}블록"
        return f"다음 공식 월요일 {EXIT_PCT[done]}% {done + 1}차 분할매도"
    return "-"


def stage_label(state: Mapping[str, Any]) -> str:
    strategy, phase = state["strategy"], str(state["strategy"]["phase"])
    if phase == "WAITING_ENTRY":
        return "62.5% 관찰·65% 매수구간 대기"
    if phase == "ENTRY":
        return f"3주 분할매수 {int(strategy.get('entry_steps_completed', 0))}/3 완료"
    if phase == "HOLD":
        return "BTC 100% 장기보유 · 35% 경고/36% 매도 대기"
    if phase == "EXIT":
        return f"36·37·38% 분할매도 {int(strategy.get('exit_steps_completed', 0))}/3 완료"
    return phase


def pending_summary(state: Mapping[str, Any]) -> str:
    telegram, parts = state["telegram"], []
    pending = telegram.get("pending_sync")
    if isinstance(pending, Mapping):
        expired = " · 주문안 만료" if pending.get("plan_expired") else ""
        parts.append(f"주문 후 잔고 동기화 대기: {pending.get('kind')} {pending.get('step')}차{expired}")
    operation = telegram.get("pending_operation")
    if isinstance(operation, Mapping):
        parts.append(f"확인 대기 작업: {operation.get('type')}")
    conversation = telegram.get("conversation")
    if isinstance(conversation, Mapping):
        parts.append(f"입력 대기: {conversation.get('type')}")
    return "\n".join(parts) if parts else "대기 중인 작업 없음"


def install() -> None:
    core.STRATEGY_VERSION = VERSION
    core.STATE_SCHEMA_VERSION = SCHEMA
    core.ENTRY_PROGRESS = 0.65
    core.EXIT_PROGRESS = 0.35
    core._initial_state = initial_state
    core._validate_state = validate_state
    core.load_state = load_state
    core.detect_block_events = detect_block_events
    core.official_action_due = official_action_due
    core.create_official_order = create_official_order
    core.next_condition_text = next_condition_text
    core.stage_label = stage_label
    core.pending_summary = pending_summary
