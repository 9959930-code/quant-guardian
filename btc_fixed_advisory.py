from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


STRATEGY_VERSION = "btc-fixed-six-upbit-telegram-1.0"
STATE_SCHEMA_VERSION = 4
KST = ZoneInfo("Asia/Seoul")
DEFAULT_BUDGET_KRW = 10_000_000.0
ENTRY_PROGRESS = 0.65
EXIT_PROGRESS = 0.35
ENTRY_TARGETS = (1 / 3, 2 / 3, 1.0)
EXIT_TARGETS = (2 / 3, 1 / 3, 0.0)
UPBIT_MARKET = "KRW-BTC"
CONFIRM_TTL_MINUTES = 24 * 60
FIRST_REMINDER_MINUTES = 30
MAX_AUDIT_ROWS = 300
MAX_HISTORY_ROWS = 800
OFFICIAL_CHECK_TIME = time(9, 17)


class FixedStrategyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlockContext:
    height: int
    epoch: int
    cycle_progress: float
    mempool_height: int
    blockstream_height: int
    observed_at_utc: str


@dataclass(frozen=True)
class AccountSnapshot:
    price_krw: float
    cash_krw: float
    btc_quantity: float
    btc_value_krw: float
    reserve_next_krw: float
    total_equity_krw: float
    active_equity_krw: float
    actual_weight: float
    active_weight: float
    total_contributions_krw: float
    profit_krw: float
    return_on_contributions: float
    peak_equity_krw: float
    current_drawdown: float
    max_drawdown: float


@dataclass(frozen=True)
class OrderInstruction:
    kind: str
    step: int
    target_weight: float
    side: str
    expected_amount_krw: float
    target_btc_value_krw: float
    active_equity_krw: float
    reference_price_krw: float
    reason: str


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def random_operation_id() -> str:
    return secrets.token_hex(4)


def parse_krw_amount(text: str) -> float:
    normalized = (
        str(text)
        .strip()
        .replace(",", "")
        .replace("원", "")
        .replace(" ", "")
    )
    if not normalized:
        raise ValueError("금액이 비어 있습니다.")
    value = float(normalized)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("0보다 큰 금액을 입력해야 합니다.")
    if value > 10_000_000_000:
        raise ValueError("입력금액이 너무 큽니다.")
    return float(round(value))


def parse_btc_quantity(text: str) -> float:
    normalized = str(text).strip().replace(",", "").replace("BTC", "").replace("btc", "")
    if not normalized:
        raise ValueError("BTC 수량이 비어 있습니다.")
    value = float(normalized)
    if not math.isfinite(value) or value < 0:
        raise ValueError("BTC 수량은 0 이상이어야 합니다.")
    if value > 1_000:
        raise ValueError("BTC 수량이 너무 큽니다.")
    return value


def _request_text(url: str, timeout: int = 20) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "QuantGuardian-BTC-FixedSix/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


def _request_json(url: str, timeout: int = 20) -> Any:
    return json.loads(_request_text(url, timeout=timeout))


def fetch_upbit_price(market: str = UPBIT_MARKET) -> float:
    payload = _request_json(
        "https://api.upbit.com/v1/ticker?" + urlencode({"markets": market})
    )
    if not isinstance(payload, list) or not payload:
        raise FixedStrategyError("Upbit ticker returned no rows")
    value = float(payload[0].get("trade_price"))
    if not math.isfinite(value) or value <= 0:
        raise FixedStrategyError("Upbit ticker price is invalid")
    return value


def fetch_block_context(now_utc: datetime | None = None, max_height_gap: int = 3) -> BlockContext:
    now = now_utc or utc_now()
    try:
        mempool = int(_request_text("https://mempool.space/api/blocks/tip/height"))
        blockstream = int(_request_text("https://blockstream.info/api/blocks/tip/height"))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        raise FixedStrategyError(f"블록 높이 조회 실패: {exc}") from exc
    if mempool <= 0 or blockstream <= 0:
        raise FixedStrategyError("블록 높이가 올바르지 않습니다.")
    if abs(mempool - blockstream) > max_height_gap:
        raise FixedStrategyError(
            f"블록 높이 공급자 불일치: mempool={mempool}, blockstream={blockstream}"
        )
    agreed = min(mempool, blockstream)
    return BlockContext(
        height=agreed,
        epoch=agreed // 210_000,
        cycle_progress=(agreed % 210_000) / 210_000,
        mempool_height=mempool,
        blockstream_height=blockstream,
        observed_at_utc=iso_utc(now),
    )


def _initial_state(now_utc: datetime, budget_krw: float = DEFAULT_BUDGET_KRW) -> dict[str, Any]:
    budget = float(round(budget_krw))
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "created_at_utc": iso_utc(now_utc),
        "updated_at_utc": iso_utc(now_utc),
        "account": {
            "initial_budget_krw": budget,
            "total_contributions_krw": budget,
            "cash_krw": budget,
            "btc_quantity": 0.0,
            "reserve_next_krw": 0.0,
            "peak_equity_krw": budget,
            "max_drawdown": 0.0,
            "last_price_krw": None,
            "history": [],
        },
        "strategy": {
            "phase": "WAITING_ENTRY",
            "cycle_epoch": None,
            "entry_steps_completed": 0,
            "exit_steps_completed": 0,
            "correction_buy_pending": False,
            "has_started_trading": False,
            "completed_cycles": 0,
            "last_official_monday": None,
            "last_daily_check": None,
            "last_monthly_report": None,
            "last_block_height": None,
            "last_block_epoch": None,
            "last_block_progress": None,
            "entry_alerted_epochs": [],
            "halving_alerted_epochs": [],
            "exit_alerted_epochs": [],
            "last_data_status": "unknown",
            "last_error": None,
        },
        "telegram": {
            "initialized": False,
            "last_update_id": None,
            "conversation": None,
            "pending_operation": None,
            "pending_sync": None,
        },
        "audit": [],
    }


def load_state(
    path: Path,
    *,
    now_utc: datetime | None = None,
    reset: bool = False,
    budget_krw: float = DEFAULT_BUDGET_KRW,
) -> tuple[dict[str, Any], bool]:
    now = now_utc or utc_now()
    if reset or not path.exists():
        return _initial_state(now, budget_krw), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixedStrategyError(f"상태파일을 읽지 못했습니다: {exc}") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("strategy_version") != STRATEGY_VERSION
    ):
        return _initial_state(now, budget_krw), True
    _validate_state(state)
    return state, False


def _validate_state(state: Mapping[str, Any]) -> None:
    account = state.get("account")
    strategy = state.get("strategy")
    telegram = state.get("telegram")
    if not isinstance(account, Mapping) or not isinstance(strategy, Mapping) or not isinstance(telegram, Mapping):
        raise FixedStrategyError("상태파일 구조가 올바르지 않습니다.")
    cash = float(account.get("cash_krw", -1))
    btc = float(account.get("btc_quantity", -1))
    reserve = float(account.get("reserve_next_krw", -1))
    if not all(math.isfinite(value) and value >= 0 for value in (cash, btc, reserve)):
        raise FixedStrategyError("계좌 상태에 음수 또는 비정상 값이 있습니다.")
    if reserve > cash + 1:
        raise FixedStrategyError("다음 사이클 대기자금이 원화잔액보다 큽니다.")
    if strategy.get("phase") not in {"WAITING_ENTRY", "ENTRY", "HOLD", "EXIT"}:
        raise FixedStrategyError("전략 단계가 올바르지 않습니다.")


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def state_digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append_audit(state: dict[str, Any], event: str, details: Mapping[str, Any] | None = None, now_utc: datetime | None = None) -> None:
    now = now_utc or utc_now()
    rows = list(state.get("audit", []))
    rows.append(
        {
            "time_utc": iso_utc(now),
            "event": event,
            "details": dict(details or {}),
        }
    )
    state["audit"] = rows[-MAX_AUDIT_ROWS:]
    state["updated_at_utc"] = iso_utc(now)


def account_snapshot(state: Mapping[str, Any], price_krw: float) -> AccountSnapshot:
    account = state["account"]
    price = float(price_krw)
    cash = float(account["cash_krw"])
    btc = float(account["btc_quantity"])
    reserve = min(float(account.get("reserve_next_krw", 0.0)), cash)
    btc_value = btc * price
    total_equity = cash + btc_value
    active_equity = max(0.0, cash - reserve) + btc_value
    actual_weight = btc_value / total_equity if total_equity > 0 else 0.0
    active_weight = btc_value / active_equity if active_equity > 0 else 0.0
    contributions = float(account["total_contributions_krw"])
    profit = total_equity - contributions
    return_on_contributions = total_equity / contributions - 1 if contributions > 0 else 0.0
    peak = max(float(account.get("peak_equity_krw", total_equity)), total_equity)
    current_drawdown = total_equity / peak - 1 if peak > 0 else 0.0
    return AccountSnapshot(
        price_krw=price,
        cash_krw=cash,
        btc_quantity=btc,
        btc_value_krw=btc_value,
        reserve_next_krw=reserve,
        total_equity_krw=total_equity,
        active_equity_krw=active_equity,
        actual_weight=actual_weight,
        active_weight=active_weight,
        total_contributions_krw=contributions,
        profit_krw=profit,
        return_on_contributions=return_on_contributions,
        peak_equity_krw=peak,
        current_drawdown=current_drawdown,
        max_drawdown=float(account.get("max_drawdown", 0.0)),
    )


def record_daily_equity(state: dict[str, Any], price_krw: float, now_utc: datetime | None = None) -> AccountSnapshot:
    now = now_utc or utc_now()
    kst_date = now.astimezone(KST).date().isoformat()
    snapshot = account_snapshot(state, price_krw)
    account = state["account"]
    peak = max(float(account.get("peak_equity_krw", 0.0)), snapshot.total_equity_krw)
    current_drawdown = snapshot.total_equity_krw / peak - 1 if peak > 0 else 0.0
    account["peak_equity_krw"] = peak
    account["max_drawdown"] = min(float(account.get("max_drawdown", 0.0)), current_drawdown)
    account["last_price_krw"] = float(price_krw)
    history = list(account.get("history", []))
    row = {
        "date": kst_date,
        "equity_krw": snapshot.total_equity_krw,
        "total_contributions_krw": snapshot.total_contributions_krw,
    }
    if history and history[-1].get("date") == kst_date:
        history[-1] = row
    else:
        history.append(row)
    account["history"] = history[-MAX_HISTORY_ROWS:]
    state["strategy"]["last_daily_check"] = kst_date
    state["updated_at_utc"] = iso_utc(now)
    return account_snapshot(state, price_krw)


def _active_target_instruction(
    state: Mapping[str, Any],
    *,
    target_weight: float,
    price_krw: float,
    kind: str,
    step: int,
    reason: str,
) -> OrderInstruction:
    snapshot = account_snapshot(state, price_krw)
    target = min(1.0, max(0.0, float(target_weight)))
    target_value = snapshot.active_equity_krw * target
    adjustment = target_value - snapshot.btc_value_krw
    if adjustment > 0.5:
        side = "BUY"
    elif adjustment < -0.5:
        side = "SELL"
    else:
        side = "NONE"
    return OrderInstruction(
        kind=kind,
        step=step,
        target_weight=target,
        side=side,
        expected_amount_krw=abs(adjustment),
        target_btc_value_krw=target_value,
        active_equity_krw=snapshot.active_equity_krw,
        reference_price_krw=float(price_krw),
        reason=reason,
    )


def official_action_due(now_utc: datetime, state: Mapping[str, Any]) -> bool:
    kst = now_utc.astimezone(KST)
    if kst.weekday() != 0 or kst.time() < OFFICIAL_CHECK_TIME:
        return False
    return state["strategy"].get("last_official_monday") != kst.date().isoformat()


def daily_check_due(now_utc: datetime, state: Mapping[str, Any]) -> bool:
    kst = now_utc.astimezone(KST)
    if kst.time() < OFFICIAL_CHECK_TIME:
        return False
    return state["strategy"].get("last_daily_check") != kst.date().isoformat()


def monthly_report_due(now_utc: datetime, state: Mapping[str, Any]) -> bool:
    kst = now_utc.astimezone(KST)
    if kst.weekday() != 0 or kst.day > 7 or kst.time() < OFFICIAL_CHECK_TIME:
        return False
    key = f"{kst.year:04d}-{kst.month:02d}"
    return state["strategy"].get("last_monthly_report") != key


def mark_monthly_reported(state: dict[str, Any], now_utc: datetime) -> None:
    kst = now_utc.astimezone(KST)
    state["strategy"]["last_monthly_report"] = f"{kst.year:04d}-{kst.month:02d}"
    state["updated_at_utc"] = iso_utc(now_utc)


def _new_pending_sync(
    state: dict[str, Any],
    instruction: OrderInstruction,
    now_utc: datetime,
) -> dict[str, Any]:
    pending = {
        "id": random_operation_id(),
        "kind": instruction.kind,
        "step": instruction.step,
        "target_weight": instruction.target_weight,
        "side": instruction.side,
        "expected_amount_krw": instruction.expected_amount_krw,
        "target_btc_value_krw": instruction.target_btc_value_krw,
        "active_equity_krw": instruction.active_equity_krw,
        "reference_price_krw": instruction.reference_price_krw,
        "reason": instruction.reason,
        "created_at_utc": iso_utc(now_utc),
        "first_reminder_at_utc": iso_utc(now_utc + timedelta(minutes=FIRST_REMINDER_MINUTES)),
        "first_reminder_sent": False,
        "last_daily_reminder": None,
    }
    state["telegram"]["pending_sync"] = pending
    append_audit(state, "ORDER_INSTRUCTION_CREATED", pending, now_utc)
    return pending


def create_official_order(
    state: dict[str, Any],
    *,
    block: BlockContext,
    price_krw: float,
    now_utc: datetime,
) -> dict[str, Any] | None:
    strategy = state["strategy"]
    telegram = state["telegram"]
    kst_date = now_utc.astimezone(KST).date().isoformat()
    strategy["last_official_monday"] = kst_date
    state["updated_at_utc"] = iso_utc(now_utc)

    if telegram.get("pending_sync"):
        return {
            "type": "SYNC_BLOCK",
            "pending_sync": telegram["pending_sync"],
            "message": "이전 주문 후 잔고 동기화가 끝나지 않아 다음 단계를 보류합니다.",
        }

    phase = strategy["phase"]
    instruction: OrderInstruction | None = None

    if phase == "WAITING_ENTRY":
        if block.cycle_progress >= ENTRY_PROGRESS:
            if float(state["account"].get("reserve_next_krw", 0.0)) > 0:
                state["account"]["reserve_next_krw"] = 0.0
                append_audit(state, "NEXT_CYCLE_RESERVE_ACTIVATED", {}, now_utc)
            strategy["cycle_epoch"] = block.epoch
            strategy["entry_steps_completed"] = 0
            strategy["exit_steps_completed"] = 0
            instruction = _active_target_instruction(
                state,
                target_weight=ENTRY_TARGETS[0],
                price_krw=price_krw,
                kind="ENTRY",
                step=1,
                reason="반감기 진행률 65% 도달 후 1차 분할매수",
            )
    elif phase == "ENTRY":
        completed = int(strategy.get("entry_steps_completed", 0))
        if completed < 3:
            step = completed + 1
            instruction = _active_target_instruction(
                state,
                target_weight=ENTRY_TARGETS[step - 1],
                price_krw=price_krw,
                kind="ENTRY",
                step=step,
                reason=f"고정 6회 전략 {step}차 분할매수",
            )
    elif phase == "HOLD":
        cycle_epoch = strategy.get("cycle_epoch")
        if bool(strategy.get("correction_buy_pending")):
            instruction = _active_target_instruction(
                state,
                target_weight=1.0,
                price_krw=price_krw,
                kind="CORRECTION",
                step=1,
                reason="3차 매수 완료 후 현재 사이클 추가입금 보정매수",
            )
        elif cycle_epoch is not None and block.epoch > int(cycle_epoch) and block.cycle_progress > EXIT_PROGRESS:
            strategy["exit_steps_completed"] = 0
            instruction = _active_target_instruction(
                state,
                target_weight=EXIT_TARGETS[0],
                price_krw=price_krw,
                kind="EXIT",
                step=1,
                reason="다음 반감기 후 진행률 35% 초과에 따른 1차 분할매도",
            )
    elif phase == "EXIT":
        completed = int(strategy.get("exit_steps_completed", 0))
        if completed < 3:
            step = completed + 1
            instruction = _active_target_instruction(
                state,
                target_weight=EXIT_TARGETS[step - 1],
                price_krw=price_krw,
                kind="EXIT",
                step=step,
                reason=f"고정 6회 전략 {step}차 분할매도",
            )

    if instruction is None:
        return None
    pending = _new_pending_sync(state, instruction, now_utc)
    return {"type": "ORDER", "instruction": instruction, "pending_sync": pending}


def complete_pending_sync(
    state: dict[str, Any],
    *,
    btc_quantity: float,
    cash_krw: float,
    now_utc: datetime,
) -> dict[str, Any]:
    telegram = state["telegram"]
    pending = telegram.get("pending_sync")
    state["account"]["btc_quantity"] = float(btc_quantity)
    state["account"]["cash_krw"] = float(round(cash_krw))
    if float(state["account"].get("reserve_next_krw", 0.0)) > float(cash_krw):
        state["account"]["reserve_next_krw"] = float(round(cash_krw))

    result = {"completed_order": None}
    if isinstance(pending, Mapping):
        kind = str(pending.get("kind"))
        step = int(pending.get("step", 0))
        strategy = state["strategy"]
        if kind == "ENTRY":
            strategy["phase"] = "HOLD" if step >= 3 else "ENTRY"
            strategy["entry_steps_completed"] = step
            strategy["has_started_trading"] = True
        elif kind == "EXIT":
            strategy["exit_steps_completed"] = step
            if step >= 3:
                strategy["phase"] = "WAITING_ENTRY"
                strategy["cycle_epoch"] = None
                strategy["entry_steps_completed"] = 0
                strategy["exit_steps_completed"] = 0
                strategy["correction_buy_pending"] = False
                strategy["completed_cycles"] = int(strategy.get("completed_cycles", 0)) + 1
                state["account"]["reserve_next_krw"] = 0.0
            else:
                strategy["phase"] = "EXIT"
        elif kind == "CORRECTION":
            strategy["phase"] = "HOLD"
            strategy["correction_buy_pending"] = False
        result["completed_order"] = dict(pending)
        append_audit(
            state,
            "ORDER_SYNC_CONFIRMED",
            {"kind": kind, "step": step, "btc_quantity": btc_quantity, "cash_krw": cash_krw},
            now_utc,
        )
    else:
        append_audit(
            state,
            "MANUAL_BALANCE_SYNC",
            {"btc_quantity": btc_quantity, "cash_krw": cash_krw},
            now_utc,
        )
    telegram["pending_sync"] = None
    telegram["conversation"] = None
    state["updated_at_utc"] = iso_utc(now_utc)
    return result


def can_change_start_budget(state: Mapping[str, Any]) -> bool:
    strategy = state["strategy"]
    account = state["account"]
    return (
        not bool(strategy.get("has_started_trading"))
        and strategy.get("phase") == "WAITING_ENTRY"
        and float(account.get("btc_quantity", 0.0)) <= 1e-15
        and state["telegram"].get("pending_sync") is None
    )


def apply_start_budget(state: dict[str, Any], amount_krw: float, now_utc: datetime) -> None:
    if not can_change_start_budget(state):
        raise FixedStrategyError("첫 매수 전 현금대기 상태에서만 시작예산을 변경할 수 있습니다.")
    amount = float(round(amount_krw))
    account = state["account"]
    account["initial_budget_krw"] = amount
    account["total_contributions_krw"] = amount
    account["cash_krw"] = amount
    account["btc_quantity"] = 0.0
    account["reserve_next_krw"] = 0.0
    account["peak_equity_krw"] = amount
    account["max_drawdown"] = 0.0
    account["history"] = []
    append_audit(state, "START_BUDGET_CHANGED", {"amount_krw": amount}, now_utc)


def deposit_options(state: Mapping[str, Any]) -> list[str]:
    phase = str(state["strategy"].get("phase"))
    entry_completed = int(state["strategy"].get("entry_steps_completed", 0))
    if phase == "ENTRY" and entry_completed < 3:
        return ["current"]
    if phase == "HOLD":
        return ["current", "next"]
    if phase == "EXIT":
        return ["next"]
    return ["current"]


def apply_deposit(
    state: dict[str, Any],
    *,
    amount_krw: float,
    mode: str,
    now_utc: datetime,
) -> dict[str, Any]:
    if mode not in {"current", "next"}:
        raise ValueError("deposit mode must be current or next")
    allowed = deposit_options(state)
    if mode not in allowed:
        raise FixedStrategyError("현재 전략 단계에서는 선택한 추가입금 방식을 사용할 수 없습니다.")
    amount = float(round(amount_krw))
    account = state["account"]
    account["cash_krw"] = float(account["cash_krw"]) + amount
    account["total_contributions_krw"] = float(account["total_contributions_krw"]) + amount
    account["peak_equity_krw"] = float(account.get("peak_equity_krw", 0.0)) + amount
    result = {
        "amount_krw": amount,
        "mode": mode,
        "correction_buy_scheduled": False,
    }
    if mode == "next":
        account["reserve_next_krw"] = float(account.get("reserve_next_krw", 0.0)) + amount
    else:
        phase = str(state["strategy"].get("phase"))
        if phase == "HOLD" and int(state["strategy"].get("entry_steps_completed", 0)) >= 3:
            state["strategy"]["correction_buy_pending"] = True
            result["correction_buy_scheduled"] = True
    append_audit(state, "DEPOSIT_APPLIED", result, now_utc)
    return result


def create_pending_operation(
    state: dict[str, Any],
    *,
    operation_type: str,
    payload: Mapping[str, Any],
    now_utc: datetime,
) -> dict[str, Any]:
    operation = {
        "id": random_operation_id(),
        "type": operation_type,
        "payload": dict(payload),
        "stage": "choice" if operation_type == "deposit" else "confirm",
        "choice": None,
        "created_at_utc": iso_utc(now_utc),
        "expires_at_utc": iso_utc(now_utc + timedelta(minutes=CONFIRM_TTL_MINUTES)),
        "first_reminder_at_utc": iso_utc(now_utc + timedelta(minutes=FIRST_REMINDER_MINUTES)),
        "first_reminder_sent": False,
        "last_daily_reminder": None,
    }
    state["telegram"]["pending_operation"] = operation
    append_audit(state, "PENDING_OPERATION_CREATED", {"type": operation_type, **dict(payload)}, now_utc)
    return operation


def pending_operation_valid(operation: Mapping[str, Any], now_utc: datetime) -> bool:
    expires = parse_iso(str(operation.get("expires_at_utc")))
    return expires is not None and now_utc <= expires


def cancel_pending_operation(state: dict[str, Any], now_utc: datetime, reason: str = "user") -> None:
    operation = state["telegram"].get("pending_operation")
    if operation:
        append_audit(state, "PENDING_OPERATION_CANCELLED", {"reason": reason, "operation": operation}, now_utc)
    state["telegram"]["pending_operation"] = None
    state["telegram"]["conversation"] = None


def set_data_status(
    state: dict[str, Any],
    *,
    status: str,
    error: str | None,
    now_utc: datetime,
) -> str | None:
    strategy = state["strategy"]
    previous = str(strategy.get("last_data_status", "unknown"))
    strategy["last_data_status"] = status
    strategy["last_error"] = error
    state["updated_at_utc"] = iso_utc(now_utc)
    if status == "error" and previous != "error":
        append_audit(state, "DATA_ERROR", {"error": error}, now_utc)
        return "error"
    if status == "ok" and previous == "error":
        append_audit(state, "DATA_RECOVERED", {}, now_utc)
        return "recovery"
    return None


def detect_block_events(
    state: dict[str, Any],
    block: BlockContext,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    strategy = state["strategy"]
    previous_epoch = strategy.get("last_block_epoch")
    previous_progress = strategy.get("last_block_progress")
    events: list[dict[str, Any]] = []

    if previous_epoch is not None and previous_progress is not None:
        prev_epoch = int(previous_epoch)
        prev_progress = float(previous_progress)
        if block.epoch == prev_epoch and prev_progress < ENTRY_PROGRESS <= block.cycle_progress:
            if block.epoch not in strategy["entry_alerted_epochs"]:
                strategy["entry_alerted_epochs"].append(block.epoch)
                events.append({"type": "ENTRY_THRESHOLD", "block": block})
        if block.epoch > prev_epoch:
            if block.epoch not in strategy["halving_alerted_epochs"]:
                strategy["halving_alerted_epochs"].append(block.epoch)
                events.append({"type": "HALVING", "block": block})
        cycle_epoch = strategy.get("cycle_epoch")
        if (
            block.epoch == prev_epoch
            and prev_progress <= EXIT_PROGRESS < block.cycle_progress
            and cycle_epoch is not None
            and block.epoch > int(cycle_epoch)
            and block.epoch not in strategy["exit_alerted_epochs"]
        ):
            strategy["exit_alerted_epochs"].append(block.epoch)
            events.append({"type": "EXIT_THRESHOLD", "block": block})

    strategy["last_block_height"] = block.height
    strategy["last_block_epoch"] = block.epoch
    strategy["last_block_progress"] = block.cycle_progress
    strategy["entry_alerted_epochs"] = list(strategy["entry_alerted_epochs"])[-10:]
    strategy["halving_alerted_epochs"] = list(strategy["halving_alerted_epochs"])[-10:]
    strategy["exit_alerted_epochs"] = list(strategy["exit_alerted_epochs"])[-10:]
    state["updated_at_utc"] = iso_utc(now_utc)
    for event in events:
        append_audit(state, f"BLOCK_EVENT_{event['type']}", {"height": block.height, "progress": block.cycle_progress}, now_utc)
    return events


def reminder_due(item: Mapping[str, Any], now_utc: datetime, *, daily_check: bool) -> str | None:
    first_at = parse_iso(str(item.get("first_reminder_at_utc")))
    if not bool(item.get("first_reminder_sent")) and first_at and now_utc >= first_at:
        return "first"
    if daily_check:
        today = now_utc.astimezone(KST).date().isoformat()
        if item.get("last_daily_reminder") != today:
            return "daily"
    return None


def mark_reminded(item: dict[str, Any], kind: str, now_utc: datetime) -> None:
    if kind == "first":
        item["first_reminder_sent"] = True
    elif kind == "daily":
        item["last_daily_reminder"] = now_utc.astimezone(KST).date().isoformat()


def stage_label(state: Mapping[str, Any]) -> str:
    strategy = state["strategy"]
    phase = strategy["phase"]
    if phase == "WAITING_ENTRY":
        return "매수구간 대기"
    if phase == "ENTRY":
        return f"3주 분할매수 {int(strategy.get('entry_steps_completed', 0))}/3 완료"
    if phase == "HOLD":
        return "BTC 100% 장기보유"
    if phase == "EXIT":
        return f"3주 분할매도 {int(strategy.get('exit_steps_completed', 0))}/3 완료"
    return str(phase)


def next_condition_text(state: Mapping[str, Any], block: BlockContext) -> str:
    strategy = state["strategy"]
    phase = strategy["phase"]
    if phase == "WAITING_ENTRY":
        remaining = max(0.0, ENTRY_PROGRESS - block.cycle_progress)
        return f"반감기 진행률 65%까지 {remaining * 100:.2f}%p"
    if phase == "ENTRY":
        return f"다음 월요일 {int(strategy.get('entry_steps_completed', 0)) + 1}차 분할매수"
    if phase == "HOLD":
        cycle_epoch = strategy.get("cycle_epoch")
        if cycle_epoch is not None and block.epoch > int(cycle_epoch):
            remaining = max(0.0, EXIT_PROGRESS - block.cycle_progress)
            return f"반감기 후 매도선 35%까지 {remaining * 100:.2f}%p"
        return "다음 반감기 후 진행률 35% 초과 시 분할매도"
    if phase == "EXIT":
        return f"다음 월요일 {int(strategy.get('exit_steps_completed', 0)) + 1}차 분할매도"
    return "-"


def pending_summary(state: Mapping[str, Any]) -> str:
    telegram = state["telegram"]
    parts: list[str] = []
    pending_sync = telegram.get("pending_sync")
    if isinstance(pending_sync, Mapping):
        parts.append(
            f"주문 후 잔고 동기화 대기: {pending_sync.get('kind')} {pending_sync.get('step')}차"
        )
    pending_operation = telegram.get("pending_operation")
    if isinstance(pending_operation, Mapping):
        parts.append(f"확인 대기 작업: {pending_operation.get('type')}")
    conversation = telegram.get("conversation")
    if isinstance(conversation, Mapping):
        parts.append(f"입력 대기: {conversation.get('type')}")
    return "\n".join(parts) if parts else "대기 중인 작업 없음"


def current_price_from_state(state: Mapping[str, Any]) -> float | None:
    value = state["account"].get("last_price_krw")
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def update_last_price(state: dict[str, Any], price_krw: float, now_utc: datetime) -> None:
    state["account"]["last_price_krw"] = float(price_krw)
    state["updated_at_utc"] = iso_utc(now_utc)


def env_chat_id() -> str:
    value = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not value:
        raise FixedStrategyError("TELEGRAM_CHAT_ID가 없습니다.")
    return value


def env_bot_token() -> str:
    value = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not value:
        raise FixedStrategyError("TELEGRAM_BOT_TOKEN이 없습니다.")
    return value
