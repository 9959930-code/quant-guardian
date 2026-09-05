from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import btc_clock_hybrid_core as hybrid_core
import btc_fixed_advisory as btc_core
import btc_fixed_telegram_bot as btc_bot
import isa_leverage_core as isa_core
import portfolio_telegram_bot as app


SERVICE_VERSION = "portfolio-telegram-btc-isa-1.2"
OPERATIONS_SCHEMA_VERSION = 1
WEEKLY_HEARTBEAT_TIME = time(9, 17)
SCHEDULE_GAP_ALERT_AFTER = timedelta(hours=3)
SENSITIVE_GAP_ALERT_AFTER = timedelta(minutes=90)
SCHEDULE_GAP_ALERT_COOLDOWN = timedelta(hours=12)

_INSTALLED = False
_ORIGINAL_RUN_SERVICE = app.run_service


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _operations(state: dict[str, Any]) -> dict[str, Any]:
    operations = state.setdefault("operations", {})
    operations.setdefault("schema_version", OPERATIONS_SCHEMA_VERSION)
    operations.setdefault("last_scheduled_run_at_utc", None)
    operations.setdefault("last_service_completed_at_utc", operations.get("last_scheduled_run_at_utc"))
    operations.setdefault("last_gap_severity", "none")
    operations.setdefault("last_weekly_heartbeat_period", None)
    operations.setdefault("last_weekly_heartbeat_at_utc", None)
    operations.setdefault("last_gap_alert_at_utc", None)
    return operations


def _week_key(now_kst: datetime) -> str:
    iso_year, iso_week, _ = now_kst.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _weekly_heartbeat_due(operations: Mapping[str, Any], now_kst: datetime) -> bool:
    if now_kst.weekday() >= 5 or now_kst.time() < WEEKLY_HEARTBEAT_TIME:
        return False
    return operations.get("last_weekly_heartbeat_period") != _week_key(now_kst)


def _format_kst(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(btc_core.KST).strftime("%Y-%m-%d %H:%M KST")


def _format_duration(value: timedelta) -> str:
    total_minutes = max(0, int(value.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)


def _krw(value: Any) -> str:
    try:
        return f"{round(float(value)):,}원"
    except (TypeError, ValueError):
        return "-"


def _number(value: Any, digits: int = 4) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if digits <= 0:
        return f"{round(numeric):,}"
    return f"{numeric:.{digits}f}".rstrip("0").rstrip(".") or "0"


def _btc_next_action(state: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    strategy = state.get("strategy") or {}
    phase = str(strategy.get("phase", result.get("phase", "-")))
    raw_height = result.get("block_height")
    if raw_height is None:
        raw_height = strategy.get("last_block_height")
    try:
        current = int(raw_height) % hybrid_core.INTERVAL
    except (TypeError, ValueError):
        current = None

    if phase == "WAITING_ENTRY":
        if current is None:
            return "62.5% 관찰·65% 매수구간 대기"
        if current < hybrid_core.WATCH:
            return f"62.5% 저점 관찰까지 {hybrid_core.WATCH - current:,}블록"
        if current < hybrid_core.ENTRY:
            return f"65% 1차 매수까지 {hybrid_core.ENTRY - current:,}블록"
        return "다음 공식 월요일 1차 분할매수"
    if phase == "ENTRY":
        completed = int(strategy.get("entry_steps_completed", 0))
        return f"분할매수 {completed}/3 완료 · 다음 {min(3, completed + 1)}차 확인"
    if phase == "HOLD":
        cycle_epoch = strategy.get("cycle_epoch")
        if current is None or cycle_epoch is None:
            return "다음 반감기 후 35% 경고·36% 매도 대기"
        current_epoch = int(raw_height) // hybrid_core.INTERVAL
        if current_epoch <= int(cycle_epoch):
            return "다음 반감기 후 35% 고점 위험경고 대기"
        if current < hybrid_core.WARNING:
            return f"35% 고점 위험경고까지 {hybrid_core.WARNING - current:,}블록"
        if current < hybrid_core.EXITS[0]:
            return f"36% 1차 매도까지 {hybrid_core.EXITS[0] - current:,}블록"
        return "다음 공식 월요일 36% 1차 분할매도"
    if phase == "EXIT":
        completed = int(strategy.get("exit_steps_completed", 0))
        if completed >= 3:
            return "매도 완료"
        if current is None:
            return f"{hybrid_core.EXIT_PCT[completed]}% {completed + 1}차 매도 대기"
        threshold = hybrid_core.EXITS[completed]
        if current < threshold:
            return (
                f"{hybrid_core.EXIT_PCT[completed]}% {completed + 1}차 매도까지 "
                f"{threshold - current:,}블록"
            )
        return (
            f"다음 공식 월요일 {hybrid_core.EXIT_PCT[completed]}% "
            f"{completed + 1}차 분할매도"
        )
    return "전략 단계 확인 필요"


def _fx_text(fx: isa_core.FxSnapshot | None, state: Mapping[str, Any]) -> str:
    if fx is not None:
        label = {"NORMAL": "정상", "WATCH": "주의", "HIGH": "과열경고"}.get(
            fx.zone, fx.zone
        )
        return f"{label} · z {fx.z_52w:+.2f} · USD/KRW {fx.usdkrw:,.2f}"
    data = state.get("data") or {}
    z_value = data.get("last_fx_z")
    if z_value is None:
        return "계산 불가"
    try:
        z_score = float(z_value)
    except (TypeError, ValueError):
        return "계산 불가"
    zone = isa_core.fx_zone(z_score)
    label = {"NORMAL": "정상", "WATCH": "주의", "HIGH": "과열경고"}[zone]
    return f"{label} · z {z_score:+.2f}"


def _build_weekly_heartbeat(
    *,
    btc_state: Mapping[str, Any],
    isa_state: Mapping[str, Any],
    btc_result: Mapping[str, Any],
    isa_result: Mapping[str, Any],
    now_utc: datetime,
    quotes: Mapping[str, isa_core.QuoteSnapshot] | None,
    fx: isa_core.FxSnapshot | None,
) -> str:
    now_kst = now_utc.astimezone(btc_core.KST)
    strategy = btc_state.get("strategy") or {}
    phase = str(strategy.get("phase", btc_result.get("phase", "-")))
    progress = btc_result.get("cycle_progress")
    progress_text = "-"
    try:
        progress_text = f"{float(progress) * 100:.2f}%"
    except (TypeError, ValueError):
        pass
    btc_status = "정상" if btc_result.get("data_status") == "ok" else "확인 필요"
    isa_status = "정상" if isa_result.get("data_status") == "ok" else "확인 필요"
    if isa_result.get("data_checked_this_run") is False:
        isa_status = f"이번 실행 미조회 · 마지막 기록 {isa_status}"
    pending = str(btc_result.get("pending") or "대기 중인 작업 없음")

    lines = [
        "[Quant Guardian 주간 heartbeat]",
        f"- 기준: {now_kst:%Y-%m-%d %H:%M KST}",
        "- 봇 실행 보고입니다. 예약 주기 보장이나 거래소 잔고 자동조회 결과는 아닙니다.",
        "",
        "[BTC]",
        f"- 데이터: {btc_status}",
        f"- 현재가: {_krw(btc_result.get('price_krw'))}",
        f"- 사이클 진행률: {progress_text}",
        f"- 단계: {phase}",
        f"- 다음 조건: {_btc_next_action(btc_state, btc_result)}",
        f"- 대기 작업: {pending}",
        "",
        "[ISA]",
        f"- 데이터: {isa_status}",
    ]

    isa_strategy = isa_state.get("strategy") or {}
    isa_account = isa_state.get("account") or {}
    initial_completed = bool(isa_strategy.get("initial_completed"))
    if not initial_completed:
        lines.extend(
            [
                "- 초기 1,000만원 매수: 미완료",
                f"- 현재 TIGER 수량: {_number(isa_account.get('tiger_quantity'))}주",
                f"- 누적 TIGER 투입원금: {_krw(isa_account.get('tiger_invested_krw'))}",
            ]
        )
        tiger_price = None
        tiger_date = None
        if quotes is not None and isa_core.TIGER_CODE in quotes:
            tiger = quotes[isa_core.TIGER_CODE]
            tiger_price, tiger_date = tiger.close, tiger.date
        else:
            data = isa_state.get("data") or {}
            tiger_price = data.get("last_tiger_price_krw")
            tiger_date = data.get("last_quote_date")
        try:
            remaining = isa_core.remaining_initial_budget(isa_state)
            fresh_date = datetime.fromisoformat(str(tiger_date)).date()
            fresh = 0 <= (now_kst.date() - fresh_date).days <= 7
            plan = isa_core.calculate_purchase_plan(remaining, float(tiger_price)) if fresh and remaining > 0 else None
        except (TypeError, ValueError, isa_core.IsaStrategyError):
            plan = None
        if plan is not None:
            lines.extend(
                [
                    f"- 기준가격(종가): {_krw(tiger_price)} · {tiger_date or '-'}",
                    f"- 기록된 매수원금 차감 후 잔여예산: {_krw(remaining)}",
                    f"- 초기 주문 검토수량: {plan.shares:,}주",
                    f"- 예상 주문금액: {_krw(plan.expected_order_krw)}",
                    f"- 예상 잔여현금: {_krw(plan.expected_remainder_krw)}",
                ]
            )
        else:
            lines.append("- 추가 주문안 보류: 예산·시세·실제 체결 여부를 확인하고 먼저 잔고동기화해 주세요.")
        lines.extend(
            [
                f"- 환율 Shadow: {_fx_text(fx, isa_state)}",
                "",
                "⚠️ ISA 초기매수가 아직 완료 처리되지 않았습니다.",
                "이미 매수했다면 재매수하지 말고 ‘🔄 ISA 잔고 동기화’에 총수량·누적원금을 입력하고",
                "‘✅ 초기매수 완료’를 선택해야 다음 달부터 월 50만원 알림이 시작됩니다.",
            ]
        )
    else:
        lines.extend(
            [
                "- 초기 1,000만원 매수: 완료",
                f"- TIGER 수량: {_number(isa_account.get('tiger_quantity'))}주",
                f"- TIGER 누적투입원금: {_krw(isa_account.get('tiger_invested_krw'))}",
                f"- 최근 월간매수 알림: {isa_strategy.get('last_monthly_plan_period') or '-'}",
                f"- 환율 Shadow: {_fx_text(fx, isa_state)}",
            ]
        )

    lines.extend(
        [
            "",
            "이 메시지는 주 1회 운영 확인용입니다.",
            "BTC·ISA 모두 자동주문은 없으며 실제 주문 후 잔고동기화가 필요합니다.",
        ]
    )
    return "\n".join(lines)


def sensitive_reasons(btc: Mapping[str, Any], isa: Mapping[str, Any], now: datetime) -> list[str]:
    reasons: list[str] = []
    tg, st = btc.get("telegram", {}), btc.get("strategy", {})
    if any(tg.get(key) for key in ("pending_sync", "pending_operation", "conversation")):
        reasons.append("주문 또는 잔고 입력 대기")
    phase = st.get("phase")
    if phase in {"ENTRY", "EXIT"}:
        reasons.append("BTC 분할 주문 진행 중")
    height = st.get("last_block_height")
    if height is not None:
        current = int(height) % hybrid_core.INTERVAL
        targets = []
        if phase == "WAITING_ENTRY":
            targets = [hybrid_core.WATCH, hybrid_core.ENTRY]
        elif phase == "HOLD" and st.get("cycle_epoch") is not None:
            if int(height) // hybrid_core.INTERVAL > int(st["cycle_epoch"]):
                targets = [hybrid_core.WARNING, *hybrid_core.EXITS]
        if any(0 <= t - current <= 7 * 144 for t in targets) or (phase == "WAITING_ENTRY" and current >= hybrid_core.ENTRY):
            reasons.append("BTC 임계구간 근접")
    local = now.astimezone(isa_core.KST)
    ist = isa.get("strategy", {})
    period = local.strftime("%Y-%m")
    if (local.weekday() < 5 and (local.hour, local.minute) >= (9, 17)
        and ist.get("initial_completed")
        and period >= str(ist.get("monthly_start_period") or period)
        and ist.get("last_monthly_plan_period") != period):
        reasons.append("ISA 이번 달 매수안 미발송")
    return reasons


def _build_gap_message(previous: datetime, now: datetime, *, reasons: list[str], severity: str) -> str:
    threshold = SENSITIVE_GAP_ALERT_AFTER if reasons else SCHEDULE_GAP_ALERT_AFTER
    return "\n".join([
        f"[Quant Guardian 실행 공백 감지 · 재개 · {severity}]",
        f"- 직전 서비스 완료: {_format_kst(previous)}",
        f"- 이번 서비스 실행: {_format_kst(now)}",
        f"- 실행 간격: {_format_duration(now - previous)}",
        f"- 경고 기준: {_format_duration(threshold)}",
        f"- 민감 조건: {', '.join(reasons) if reasons else '없음'}",
        "",
        "이 BTC·ISA 워크플로의 실행 공백을 확인했습니다. GitHub 전체 장애 여부는 확인되지 않았습니다.",
        "현재 실행이 전략 점검 단계까지 재개됐습니다. 최종 상태 백업 결과는 워크플로 종료 후 확인됩니다.",
        "외부 감시기가 가동되기 전에는 실행 중단 중 경고할 수 없으며, 이 메시지는 재개 후 보고입니다.",
        "자동주문 없음 · 오래된 주문 금액을 그대로 사용하지 마세요.",
    ])


def process_operational_alerts(
    *, btc_state_path: Path, isa_state_path: Path,
    btc_result: Mapping[str, Any], isa_result: Mapping[str, Any],
    now_utc: datetime | None = None, event_name: str | None = None,
    client: Any | None = None,
    quote_fetcher: Callable[[], Mapping[str, isa_core.QuoteSnapshot]] | None = None,
    fx_fetcher: Callable[[], isa_core.FxSnapshot] | None = None,
) -> dict[str, Any]:
    now = (now_utc or btc_core.utc_now()).astimezone(UTC)
    event = str(event_name if event_name is not None else os.getenv("GITHUB_EVENT_NAME", ""))
    btc, _ = btc_core.load_state(btc_state_path, now_utc=now)
    isa, _ = isa_core.load_state(isa_state_path, now_utc=now)
    ops = _operations(btc)
    count, gap_minutes = 0, None
    gap_sent = heartbeat_sent = False
    outbound = client

    def send(text: str) -> None:
        nonlocal outbound, count
        if outbound is None:
            outbound = btc_bot.TelegramClient(btc_core.env_bot_token(), btc_core.env_chat_id())
        outbound.send_message(text, menu=True)
        count += 1

    def persist() -> None:
        btc["updated_at_utc"] = btc_core.iso_utc(now)
        btc_core.save_state(btc_state_path, btc)

    operational_event = event in {"schedule", "push", "workflow_dispatch"}
    previous = _parse_iso(ops.get("last_service_completed_at_utc"))
    reasons = sensitive_reasons(btc, isa, now)
    severity = "none"
    if operational_event and previous is not None and now > previous:
        gap = now - previous
        gap_minutes = int(gap.total_seconds() // 60)
        threshold = SENSITIVE_GAP_ALERT_AFTER if reasons else SCHEDULE_GAP_ALERT_AFTER
        severity = "중요" if gap >= timedelta(hours=12) or (reasons and gap >= timedelta(hours=3)) else "주의"
        last_alert = _parse_iso(ops.get("last_gap_alert_at_utc"))
        cooldown = last_alert is None or now - last_alert >= SCHEDULE_GAP_ALERT_COOLDOWN
        escalate = severity == "중요" and ops.get("last_gap_severity") != "중요"
        if gap >= threshold and (cooldown or escalate):
            send(_build_gap_message(previous, now, reasons=reasons, severity=severity))
            ops["last_gap_alert_at_utc"] = btc_core.iso_utc(now)
            ops["last_gap_severity"] = severity
            gap_sent = True
            btc_core.append_audit(btc, "PORTFOLIO_EXECUTION_GAP_REPORTED", {"minutes": gap_minutes, "reasons": reasons}, now)
            persist()  # Save delivery markers only after a successful API response.

    if operational_event and _weekly_heartbeat_due(ops, now.astimezone(btc_core.KST)):
        try:
            quotes = (quote_fetcher or isa_core.fetch_quotes)()
        except isa_core.IsaStrategyError:
            quotes = None
        try:
            fx = (fx_fetcher or isa_core.fetch_fx_snapshot)()
        except isa_core.IsaStrategyError:
            fx = None
        send(_build_weekly_heartbeat(btc_state=btc, isa_state=isa,
            btc_result=btc_result, isa_result=isa_result, now_utc=now, quotes=quotes, fx=fx))
        ops["last_weekly_heartbeat_period"] = _week_key(now.astimezone(btc_core.KST))
        ops["last_weekly_heartbeat_at_utc"] = btc_core.iso_utc(now)
        heartbeat_sent = True
        btc_core.append_audit(btc, "PORTFOLIO_WEEKLY_HEARTBEAT_SENT", {"period": ops["last_weekly_heartbeat_period"]}, now)
        persist()

    if operational_event:
        ops["last_service_completed_at_utc"] = btc_core.iso_utc(now)
        ops["last_execution_event"] = event
        ops["last_trigger_source"] = os.getenv("QG_TRIGGER_SOURCE", event)
    if event == "schedule":
        ops["last_scheduled_run_at_utc"] = btc_core.iso_utc(now)
    data_ok = btc_result.get("data_status") == "ok" and (
        isa_result.get("data_status") == "ok"
    )
    ops["last_data_checks_ok"] = data_ok
    persist()
    return {
        "operations_schema_version": OPERATIONS_SCHEMA_VERSION,
        "event_name": event, "message_count": count,
        "heartbeat_sent": heartbeat_sent, "gap_alert_sent": gap_sent,
        "schedule_gap_minutes": gap_minutes, "sensitive_reasons": reasons,
        "data_checks_ok": data_ok,
        "last_service_completed_at_utc": ops.get("last_service_completed_at_utc"),
        "last_scheduled_run_at_utc": ops.get("last_scheduled_run_at_utc"),
        "last_weekly_heartbeat_period": ops.get("last_weekly_heartbeat_period"),
    }


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
    result = _ORIGINAL_RUN_SERVICE(
        btc_state_path=btc_state_path,
        isa_state_path=isa_state_path,
        reset_btc_state=reset_btc_state,
        reset_isa_state=reset_isa_state,
        force_btc_status=force_btc_status,
        force_isa_status=force_isa_status,
        now_utc=now,
    )
    result["service_version"] = SERVICE_VERSION
    result["operational"] = process_operational_alerts(
        btc_state_path=btc_state_path,
        isa_state_path=isa_state_path,
        btc_result=result["btc"],
        isa_result=result["isa"],
        now_utc=now,
    )
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    app.run_service = run_service
    _INSTALLED = True
