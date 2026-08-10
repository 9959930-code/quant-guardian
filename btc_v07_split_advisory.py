from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import btc_v07_advisory as base
from btc_guardian import BtcDataError, DEFAULT_CONFIG, load_config, utc_now


STRATEGY_VERSION = "btc-v07-vol40-upbit-three-split-advisory-1.1"
STATE_SCHEMA_VERSION = 2
KST = ZoneInfo("Asia/Seoul")
ENTRY_PARTS = 3
EXIT_PARTS = 3
EPSILON = 1e-12


@dataclass(frozen=True)
class SplitStep:
    action: str
    should_trade: bool
    before_weight: float
    final_target_weight: float
    step_target_weight: float
    adjustment_krw: float
    final_target_btc_value_krw: float
    step_target_btc_value_krw: float
    model_equity_krw: float
    step_number: int
    total_parts: int
    remaining_steps: int
    plan_restarted: bool
    plan_active_after: bool


def _initial_state(budget_krw: float, now_utc: datetime) -> dict[str, Any]:
    state = base._initial_state(budget_krw, now_utc)
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["strategy_version"] = STRATEGY_VERSION
    state["split_plan"] = None
    state["execution_policy"] = {
        "entry_parts": ENTRY_PARTS,
        "exit_parts": EXIT_PARTS,
        "step_frequency": "weekly",
        "decision_weekday": "Sunday UTC close",
        "execution_time": "next Monday advisory run",
    }
    return state


def load_state(
    path: Path,
    *,
    budget_krw: float,
    now_utc: datetime,
    reset: bool = False,
) -> tuple[dict[str, Any], bool]:
    if reset or not path.exists():
        return _initial_state(budget_krw, now_utc), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BtcDataError(f"Invalid three-split advisory state: {exc}") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("strategy_version") != STRATEGY_VERSION
    ):
        return _initial_state(budget_krw, now_utc), True
    if "split_plan" not in state:
        state["split_plan"] = None
    return state, False


def _direction(gap: float) -> float:
    if math.isclose(gap, 0.0, abs_tol=EPSILON):
        return 0.0
    return math.copysign(1.0, gap)


def _action_label(
    *,
    adjustment_krw: float,
    step_number: int,
    total_parts: int,
    final_target_weight: float,
    current_btc_quantity: float,
) -> str:
    if adjustment_krw > 0:
        prefix = "신규 분할매수" if current_btc_quantity <= EPSILON else "분할매수"
        return (
            f"{step_number}차·최종 {prefix}"
            if step_number >= total_parts
            else f"{step_number}차 {prefix}"
        )
    if adjustment_krw < 0:
        if final_target_weight <= EPSILON and step_number >= total_parts:
            return "3차·최종 전량매도"
        return (
            "3차·최종 분할매도"
            if step_number >= total_parts
            else f"{step_number}차 분할매도"
        )
    return "보유" if final_target_weight > 0 else "현금대기"


def _idle_step(
    *,
    actual_weight: float,
    final_target_weight: float,
    equity_krw: float,
    plan: Mapping[str, Any] | None,
) -> SplitStep:
    step_number = 0
    total_parts = 0
    remaining_steps = 0
    plan_active = isinstance(plan, Mapping)
    if plan_active:
        total_parts = int(plan.get("total_parts", 0))
        completed = int(plan.get("completed_parts", 0))
        step_number = completed
        remaining_steps = max(0, total_parts - completed)
    return SplitStep(
        action="보유" if final_target_weight > 0 else "현금대기",
        should_trade=False,
        before_weight=actual_weight,
        final_target_weight=final_target_weight,
        step_target_weight=actual_weight,
        adjustment_krw=0.0,
        final_target_btc_value_krw=equity_krw * final_target_weight,
        step_target_btc_value_krw=equity_krw * actual_weight,
        model_equity_krw=equity_krw,
        step_number=step_number,
        total_parts=total_parts,
        remaining_steps=remaining_steps,
        plan_restarted=False,
        plan_active_after=plan_active,
    )


def plan_three_split_step(
    state: dict[str, Any],
    *,
    actual_weight: float,
    final_target_weight: float,
    equity_krw: float,
    current_btc_value_krw: float,
    current_btc_quantity: float,
    signal_date: str,
) -> SplitStep:
    target = float(min(1.0, max(0.0, final_target_weight)))
    plan = state.get("split_plan")
    if not isinstance(plan, dict):
        plan = None

    force_zero = math.isclose(target, 0.0, abs_tol=EPSILON) and current_btc_quantity > EPSILON
    latest_gap = target - actual_weight
    trigger = force_zero or abs(latest_gap) + EPSILON >= base.REBALANCE_DEADBAND
    plan_restarted = False

    if plan is not None:
        frozen_target = float(plan["final_target"])
        frozen_gap = frozen_target - actual_weight
        materially_new = (
            abs(target - frozen_target) + EPSILON >= base.REBALANCE_DEADBAND
        )
        direction_reversal = (
            _direction(frozen_gap) != 0.0
            and _direction(latest_gap) != 0.0
            and _direction(frozen_gap) != _direction(latest_gap)
        )
        zero_target_changed = force_zero and not math.isclose(
            frozen_target, 0.0, abs_tol=EPSILON
        )
        if materially_new or direction_reversal or zero_target_changed:
            plan = None
            state["split_plan"] = None
            plan_restarted = True

    if plan is None and trigger:
        parts = ENTRY_PARTS if target > actual_weight else EXIT_PARTS
        plan = {
            "start_weight": float(actual_weight),
            "final_target": target,
            "total_parts": int(parts),
            "completed_parts": 0,
            "signal_date": signal_date,
        }
        state["split_plan"] = plan
        plan_restarted = True

    if plan is None:
        return _idle_step(
            actual_weight=actual_weight,
            final_target_weight=target,
            equity_krw=equity_krw,
            plan=None,
        )

    total_parts = int(plan["total_parts"])
    step_number = int(plan["completed_parts"]) + 1
    fraction = step_number / total_parts
    step_target = float(plan["start_weight"]) + (
        float(plan["final_target"]) - float(plan["start_weight"])
    ) * fraction
    step_target = float(min(1.0, max(0.0, step_target)))
    step_value = equity_krw * step_target
    adjustment = step_value - current_btc_value_krw

    plan["completed_parts"] = step_number
    remaining = max(0, total_parts - step_number)
    if remaining == 0:
        state["split_plan"] = None
        active_after = False
    else:
        state["split_plan"] = plan
        active_after = True

    action = _action_label(
        adjustment_krw=adjustment,
        step_number=step_number,
        total_parts=total_parts,
        final_target_weight=float(plan["final_target"]),
        current_btc_quantity=current_btc_quantity,
    )
    return SplitStep(
        action=action,
        should_trade=not math.isclose(adjustment, 0.0, abs_tol=0.5),
        before_weight=actual_weight,
        final_target_weight=float(plan["final_target"]),
        step_target_weight=step_target,
        adjustment_krw=adjustment,
        final_target_btc_value_krw=equity_krw * float(plan["final_target"]),
        step_target_btc_value_krw=step_value,
        model_equity_krw=equity_krw,
        step_number=step_number,
        total_parts=total_parts,
        remaining_steps=remaining,
        plan_restarted=plan_restarted,
        plan_active_after=active_after,
    )


def apply_model_step(
    state: dict[str, Any],
    step: SplitStep,
    mark_price_krw: float,
) -> None:
    if not step.should_trade:
        return
    portfolio = state["portfolio"]
    cash = float(portfolio["cash_krw"])
    quantity = float(portfolio["btc_quantity"])
    if step.adjustment_krw > 0:
        gross_budget = min(step.adjustment_krw, cash)
        bought = gross_budget / (mark_price_krw * (1 + base.UPBIT_FEE_RATE))
        notional = bought * mark_price_krw
        fee = notional * base.UPBIT_FEE_RATE
        cash -= notional + fee
        quantity += bought
    elif step.adjustment_krw < 0:
        sold = min(-step.adjustment_krw / mark_price_krw, quantity)
        notional = sold * mark_price_krw
        fee = notional * base.UPBIT_FEE_RATE
        cash += notional - fee
        quantity -= sold
    portfolio["cash_krw"] = max(0.0, cash)
    portfolio["btc_quantity"] = max(0.0, quantity)


def _next_condition(signal: base.StrategySignal) -> str:
    progress = signal.cycle_progress
    if signal.in_holding_window and progress >= base.ENTRY_PROGRESS:
        return "다음 반감기 후 진행률 35% 초과 시 3주 분할매도 시작"
    if signal.in_holding_window:
        remaining = max(0.0, base.EXIT_PROGRESS - progress)
        return f"보유 종료선 35%까지 {remaining * 100:.2f}%p 남음"
    remaining = max(0.0, base.ENTRY_PROGRESS - progress)
    return (
        f"매수 시작선 65%까지 {remaining * 100:.2f}%p 남음; "
        "도달 후 3주 분할매수 시작"
    )


def build_message(payload: Mapping[str, Any]) -> str:
    if payload.get("data_status") != "ok":
        return "\n".join(
            [
                "[Quant Guardian BTC v0.7 · 3분할]",
                "",
                "[데이터 오류]",
                str(payload.get("error", "알 수 없는 오류")),
                "",
                "새 분할매수·매도 판단을 중단하고 마지막 정상상태를 유지합니다.",
                "자동주문은 없습니다.",
            ]
        )

    signal = payload["signal"]
    step = payload["plan"]
    performance = payload["performance"]
    heading = {
        "initial": "초기 등록 완료",
        "weekly": "주간 매매판단",
        "monthly": "월간 포함 주간판단",
        "recovery": "데이터 정상화",
        "manual": "수동 점검",
    }.get(str(payload.get("notification_reason")), "상태 알림")

    if step["should_trade"]:
        order_line = (
            f"- 이번 주 주문 검토액: {base._krw(abs(float(step['adjustment_krw'])))} "
            f"({'매수' if float(step['adjustment_krw']) > 0 else '매도'})"
        )
        stage_line = (
            f"- 분할 단계: {int(step['step_number'])}/{int(step['total_parts'])}"
        )
    elif step["plan_active_after"]:
        order_line = "- 이번 주 주문: 없음"
        stage_line = f"- 남은 분할: {int(step['remaining_steps'])}회"
    else:
        order_line = "- 이번 주 주문: 없음"
        stage_line = "- 진행 중인 분할계획: 없음"

    performance_lines = [
        f"- 추종계좌 누적수익률: {base._percent(performance.get('total_return'))}",
        f"- 추종계좌 최대낙폭: {base._percent(performance.get('max_drawdown'))}",
    ]
    if payload.get("monthly_summary"):
        performance_lines.insert(
            1,
            f"- 최근 약 30일 수익률: {base._percent(performance.get('return_30d'))}",
        )

    warning = (
        "500만원·0 BTC에서 시작한 추종모형입니다. 실제 Upbit 체결 후에는 "
        "이번 단계 목표평가액과 최종 목표평가액을 기준으로 직접 보정해야 합니다."
    )
    strategy_signal = base.StrategySignal(**signal)
    return "\n".join(
        [
            "[Quant Guardian BTC v0.7 · 3분할]",
            f"알림: {heading}",
            f"기준 주간봉: {signal['decision_date']} UTC 일봉 마감",
            "",
            "[현재 행동]",
            f"- 판단: {step['action']}",
            "- 실전 기준: vol40 위험균형형 · 매수/매도 3주 분할",
            f"- 최종 목표 BTC 비중: {base._percent(signal['target_weight'])}",
            f"- 이번 단계 목표 비중: {base._percent(step['step_target_weight'])}",
            f"- 조정 전 추종비중: {base._percent(step['before_weight'])}",
            stage_line,
            order_line,
            "",
            "[500만원 시작 추종계좌]",
            f"- 현재 추종자산: {base._krw(performance['equity_krw'])}",
            f"- 이번 단계 BTC 목표액: {base._krw(step['step_target_btc_value_krw'])}",
            f"- 최종 BTC 목표액: {base._krw(step['final_target_btc_value_krw'])}",
            f"- Upbit 참고가격: {base._krw(payload['upbit_price_krw'])}",
            "",
            "[반감기·위험]",
            f"- 사이클 진행률: {base._percent(signal['cycle_progress'], 2)}",
            f"- 현재 국면: {signal['phase_label']}",
            f"- 보유구간: {'예' if signal['in_holding_window'] else '아니오'}",
            f"- 63일 연환산 변동성: {base._percent(signal['realized_volatility'])}",
            f"- 변동성 허용비중: {base._percent(signal['volatility_cap'])}",
            f"- 다음 조건: {_next_condition(strategy_signal)}",
            "",
            "[성과]",
            *performance_lines,
            "",
            warning,
            "일요일 확정 종가 기반 주간 전략이며 장중 자동주문은 없습니다.",
        ]
    ).strip()


def _normal_run(
    *,
    state: dict[str, Any],
    initial: bool,
    now_utc: datetime,
    upbit_history: Any,
    halving: Mapping[str, Any],
    mark_price: float,
    force_notify: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now_kst = now_utc.astimezone(KST)
    is_monday = now_kst.weekday() == 0
    monthly_summary = is_monday and now_kst.day <= 7
    previous_status = str(state.get("last_data_status", "unknown"))

    fresh_signal = base.build_strategy_signal(upbit_history, halving)
    previous_weekly = state.get("weekly_signal")
    if initial or is_monday or not isinstance(previous_weekly, dict):
        active_signal = fresh_signal
        state["weekly_signal"] = asdict(fresh_signal)
    else:
        active_signal = base.StrategySignal(**previous_weekly)

    before = base._portfolio_snapshot(state, mark_price)
    if is_monday:
        step = plan_three_split_step(
            state,
            actual_weight=before["actual_weight"],
            final_target_weight=active_signal.target_weight,
            equity_krw=before["equity_krw"],
            current_btc_value_krw=before["btc_value_krw"],
            current_btc_quantity=before["btc_quantity"],
            signal_date=active_signal.decision_date,
        )
        apply_model_step(state, step, mark_price)
    else:
        step = _idle_step(
            actual_weight=before["actual_weight"],
            final_target_weight=active_signal.target_weight,
            equity_krw=before["equity_krw"],
            plan=state.get("split_plan"),
        )

    performance = base.update_performance(
        state,
        kst_date=now_kst.date(),
        mark_price_krw=mark_price,
    )
    if initial:
        reason = "initial"
    elif previous_status == "error":
        reason = "recovery"
    elif monthly_summary:
        reason = "monthly"
    elif is_monday:
        reason = "weekly"
    elif force_notify:
        reason = "manual"
    else:
        reason = "none"

    state["last_data_status"] = "ok"
    state["last_error"] = None
    state["updated_at_utc"] = now_utc.isoformat()
    payload: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_kst": now_kst.isoformat(),
        "data_status": "ok",
        "should_notify": reason != "none",
        "notification_reason": reason,
        "monthly_summary": monthly_summary,
        "upbit_price_krw": mark_price,
        "signal": asdict(active_signal),
        "plan": asdict(step),
        "performance": performance,
        "portfolio_after": base._portfolio_snapshot(state, mark_price),
        "execution_policy": {
            "entry_parts": ENTRY_PARTS,
            "exit_parts": EXIT_PARTS,
            "weekly_equal_weight_steps": True,
            "restart_threshold": base.REBALANCE_DEADBAND,
        },
        "auto_order": False,
    }
    payload["message"] = build_message(payload)
    return state, payload


def _error_run(
    *,
    state: dict[str, Any],
    initial: bool,
    now_utc: datetime,
    error: Exception,
    force_notify: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now_kst = now_utc.astimezone(KST)
    previous_status = str(state.get("last_data_status", "unknown"))
    should_notify = (
        initial
        or previous_status != "error"
        or now_kst.weekday() == 0
        or force_notify
    )
    state["last_data_status"] = "error"
    state["last_error"] = str(error)
    state["updated_at_utc"] = now_utc.isoformat()
    payload: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_kst": now_kst.isoformat(),
        "data_status": "error",
        "should_notify": should_notify,
        "notification_reason": "error",
        "error": str(error),
        "execution_policy": {
            "entry_parts": ENTRY_PARTS,
            "exit_parts": EXIT_PARTS,
        },
        "auto_order": False,
    }
    payload["message"] = build_message(payload)
    return state, payload


def run_advisory(
    *,
    refresh: bool,
    config_path: Path,
    state_path: Path,
    output_path: Path,
    budget_krw: float,
    now_utc: datetime | None = None,
    force_notify: bool = False,
    reset_model: bool = False,
) -> dict[str, Any]:
    if budget_krw <= 0:
        raise ValueError("Budget must be positive")
    now = now_utc or utc_now()
    config = load_config(config_path)
    state, initial = load_state(
        state_path,
        budget_krw=budget_krw,
        now_utc=now,
        reset=reset_model,
    )
    try:
        if str(config.get("btc", {}).get("run_mode", "shadow")) != "shadow":
            raise BtcDataError("BTC three-split advisory requires shadow mode")
        if bool(config.get("btc", {}).get("auto_order", False)):
            raise BtcDataError("BTC advisory refuses automatic-order configuration")
        history, price_fallback, price_error = base.load_upbit_history(
            refresh=refresh,
            config=config,
            now_utc=now,
        )
        halving, block_fallback, block_error = base.load_halving_context(
            refresh=refresh,
            config=config,
            now_utc=now,
        )
        try:
            mark_price = base.fetch_current_upbit_price(
                str(
                    config.get("btc", {}).get(
                        "execution_market", base.DEFAULT_MARKET
                    )
                )
            )
        except Exception:
            mark_price = base._finite_positive(
                history["Close"].iloc[-1], "latest Upbit close"
            )
        state, payload = _normal_run(
            state=state,
            initial=initial,
            now_utc=now,
            upbit_history=history,
            halving=halving,
            mark_price=mark_price,
            force_notify=force_notify,
        )
        payload["data_fallbacks"] = {
            "upbit_cache": price_fallback,
            "upbit_error": price_error,
            "block_cache": block_fallback,
            "block_error": block_error,
        }
    except Exception as exc:
        state, payload = _error_run(
            state=state,
            initial=initial,
            now_utc=now,
            error=exc,
            force_notify=force_notify,
        )
    base.save_json(state_path, state)
    base.save_json(output_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quant Guardian BTC v0.7 Upbit three-week split advisory"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("data/state/btc_v07_state.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/btc_v07_advisory.json"),
    )
    parser.add_argument(
        "--budget-krw", type=float, default=base.DEFAULT_BUDGET_KRW
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--force-notify", action="store_true")
    parser.add_argument("--reset-model", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run_advisory(
        refresh=args.refresh,
        config_path=args.config,
        state_path=args.state_file,
        output_path=args.output,
        budget_krw=args.budget_krw,
        force_notify=args.force_notify,
        reset_model=args.reset_model,
    )
    print(payload["message"])
    print(f"알림 전송 대상: {'예' if payload['should_notify'] else '아니오'}")
    print(f"JSON: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
