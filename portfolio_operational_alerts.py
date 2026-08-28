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


SERVICE_VERSION = "portfolio-telegram-btc-isa-1.1"
OPERATIONS_SCHEMA_VERSION = 1
WEEKLY_HEARTBEAT_TIME = time(9, 17)
SCHEDULE_GAP_ALERT_AFTER = timedelta(minutes=90)
SCHEDULE_GAP_ALERT_COOLDOWN = timedelta(hours=12)

_INSTALLED = False
_ORIGINAL_RUN_SERVICE = app.run_service


def _parse_iso(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _operations(state: dict[str, Any]) -> dict[str, Any]:
    operations = state.setdefault("operations", {})
    operations.setdefault("schema_version", OPERATIONS_SCHEMA_VERSION)
    operations.setdefault("last_scheduled_run_at_utc", None)
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
    pending = str(btc_result.get("pending") or "대기 중인 작업 없음")

    lines = [
        "[Quant Guardian 주간 heartbeat]",
        f"- 기준: {now_kst:%Y-%m-%d %H:%M KST}",
        "- BTC·ISA 자동화가 이번 실행까지 정상적으로 도달했습니다.",
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
            plan = isa_core.calculate_purchase_plan(
                isa_core.INITIAL_INVESTMENT_KRW, float(tiger_price)
            )
        except (TypeError, ValueError, isa_core.IsaStrategyError):
            plan = None
        if plan is not None:
            lines.extend(
                [
                    f"- 최신 기준가격: {_krw(tiger_price)} · {tiger_date or '-'}",
                    f"- 초기 주문 검토수량: {plan.shares:,}주",
                    f"- 예상 주문금액: {_krw(plan.expected_order_krw)}",
                    f"- 예상 잔여현금: {_krw(plan.expected_remainder_krw)}",
                ]
            )
        else:
            lines.append("- 초기 주문수량: 시세 확인 후 ISA 상태 메뉴에서 재확인")
        lines.extend(
            [
                f"- 환율 Shadow: {_fx_text(fx, isa_state)}",
                "",
                "⚠️ ISA 초기매수가 아직 완료 처리되지 않았습니다.",
                "직접 매수한 뒤 ‘🔄 ISA 잔고 동기화’에서 총수량·누적원금을 입력하고",
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


def _build_gap_message(previous: datetime, now_utc: datetime) -> str:
    return "\n".join(
        [
            "[Quant Guardian 자동화 지연 감지 · 복구]",
            f"- 직전 예약 실행: {_format_kst(previous)}",
            f"- 이번 예약 실행: {_format_kst(now_utc)}",
            f"- 실행 간격: {_format_duration(now_utc - previous)}",
            f"- 경고 기준: {_format_duration(SCHEDULE_GAP_ALERT_AFTER)}",
            "",
            "현재 실행에서는 BTC·ISA 상태 복원, 전략 점검과 상태 저장까지 다시 완료했습니다.",
            "GitHub Actions가 멈춰 있는 동안에는 Telegram 알림을 보낼 수 없으므로",
            "이 알림은 다음 실행이 재개된 뒤 전달되는 복구형 감지입니다.",
            "자동주문은 없습니다.",
        ]
    )


def process_operational_alerts(
    *,
    btc_state_path: Path,
    isa_state_path: Path,
    btc_result: Mapping[str, Any],
    isa_result: Mapping[str, Any],
    now_utc: datetime | None = None,
    event_name: str | None = None,
    client: btc_bot.TelegramClient | Any | None = None,
    quote_fetcher: Callable[[], Mapping[str, isa_core.QuoteSnapshot]] | None = None,
    fx_fetcher: Callable[[], isa_core.FxSnapshot] | None = None,
) -> dict[str, Any]:
    now = (now_utc or btc_core.utc_now()).astimezone(UTC)
    event = str(event_name if event_name is not None else os.getenv("GITHUB_EVENT_NAME", ""))
    btc_state, _ = btc_core.load_state(btc_state_path, now_utc=now)
    isa_state, _ = isa_core.load_state(isa_state_path, now_utc=now)
    operations = _operations(btc_state)
    messages: list[str] = []
    gap_minutes: int | None = None
    gap_alert_sent = False
    heartbeat_sent = False

    if event == "schedule":
        previous = _parse_iso(operations.get("last_scheduled_run_at_utc"))
        if previous is not None and now > previous:
            gap = now - previous
            gap_minutes = int(gap.total_seconds() // 60)
            last_alert = _parse_iso(operations.get("last_gap_alert_at_utc"))
            cooldown_elapsed = (
                last_alert is None or now - last_alert >= SCHEDULE_GAP_ALERT_COOLDOWN
            )
            if gap >= SCHEDULE_GAP_ALERT_AFTER and cooldown_elapsed:
                messages.append(_build_gap_message(previous, now))
                operations["last_gap_alert_at_utc"] = btc_core.iso_utc(now)
                gap_alert_sent = True
                btc_core.append_audit(
                    btc_state,
                    "PORTFOLIO_SCHEDULE_GAP_RECOVERED",
                    {"gap_minutes": gap_minutes},
                    now,
                )
        operations["last_scheduled_run_at_utc"] = btc_core.iso_utc(now)

    now_kst = now.astimezone(btc_core.KST)
    if event in {"schedule", "push", "workflow_dispatch"} and _weekly_heartbeat_due(
        operations, now_kst
    ):
        quotes: Mapping[str, isa_core.QuoteSnapshot] | None = None
        fx: isa_core.FxSnapshot | None = None
        try:
            quotes = (quote_fetcher or isa_core.fetch_quotes)()
        except isa_core.IsaStrategyError:
            quotes = None
        try:
            fx = (fx_fetcher or isa_core.fetch_fx_snapshot)()
        except isa_core.IsaStrategyError:
            fx = None
        messages.append(
            _build_weekly_heartbeat(
                btc_state=btc_state,
                isa_state=isa_state,
                btc_result=btc_result,
                isa_result=isa_result,
                now_utc=now,
                quotes=quotes,
                fx=fx,
            )
        )
        operations["last_weekly_heartbeat_period"] = _week_key(now_kst)
        operations["last_weekly_heartbeat_at_utc"] = btc_core.iso_utc(now)
        heartbeat_sent = True
        btc_core.append_audit(
            btc_state,
            "PORTFOLIO_WEEKLY_HEARTBEAT_SENT",
            {
                "period": _week_key(now_kst),
                "isa_initial_completed": bool(
                    isa_state["strategy"].get("initial_completed")
                ),
            },
            now,
        )

    if messages:
        outbound = client or btc_bot.TelegramClient(
            btc_core.env_bot_token(), btc_core.env_chat_id()
        )
        for message in messages:
            outbound.send_message(message, menu=True)

    btc_state["updated_at_utc"] = btc_core.iso_utc(now)
    btc_core.save_state(btc_state_path, btc_state)
    return {
        "operations_schema_version": OPERATIONS_SCHEMA_VERSION,
        "event_name": event,
        "message_count": len(messages),
        "heartbeat_sent": heartbeat_sent,
        "gap_alert_sent": gap_alert_sent,
        "schedule_gap_minutes": gap_minutes,
        "last_scheduled_run_at_utc": operations.get("last_scheduled_run_at_utc"),
        "last_weekly_heartbeat_period": operations.get(
            "last_weekly_heartbeat_period"
        ),
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
