from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from btc_guardian import (
    BtcDataError,
    DEFAULT_CONFIG,
    EsploraProvider,
    build_halving_context,
    candles_to_frame,
    fetch_json,
    fetch_upbit_history,
    halving_context_to_dict,
    load_config,
    read_price_cache,
    resolve_paths,
    utc_now,
    write_price_cache,
)


STRATEGY_VERSION = "btc-v07-vol40-upbit-advisory-1.0"
KST = ZoneInfo("Asia/Seoul")
ENTRY_PROGRESS = 0.65
EXIT_PROGRESS = 0.35
TARGET_ANNUAL_VOLATILITY = 0.40
VOLATILITY_LOOKBACK_DAYS = 63
REBALANCE_DEADBAND = 0.10
DEFAULT_BUDGET_KRW = 5_000_000.0
DEFAULT_MARKET = "KRW-BTC"
UPBIT_FEE_RATE = 0.0005
HISTORY_LIMIT = 400


@dataclass(frozen=True)
class StrategySignal:
    decision_date: str
    cycle_progress: float
    phase_label: str
    in_holding_window: bool
    realized_volatility: float
    volatility_cap: float
    target_weight: float
    entry_progress: float = ENTRY_PROGRESS
    exit_progress: float = EXIT_PROGRESS
    target_annual_volatility: float = TARGET_ANNUAL_VOLATILITY
    volatility_lookback_days: int = VOLATILITY_LOOKBACK_DAYS
    rebalance_deadband: float = REBALANCE_DEADBAND


@dataclass(frozen=True)
class RebalancePlan:
    action: str
    should_trade: bool
    before_weight: float
    target_weight: float
    adjustment_krw: float
    target_btc_value_krw: float
    model_equity_krw: float


def _finite_positive(value: Any, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise BtcDataError(f"{name} must be finite and positive")
    return numeric


def is_holding_window(progress: float) -> bool:
    if not 0 <= progress < 1:
        raise ValueError("Cycle progress must be in [0, 1)")
    return progress >= ENTRY_PROGRESS or progress <= EXIT_PROGRESS


def latest_completed_sunday(index: pd.DatetimeIndex) -> pd.Timestamp:
    normalized = pd.DatetimeIndex(index).tz_localize(None)
    sundays = normalized[normalized.dayofweek == 6]
    if sundays.empty:
        raise BtcDataError("Upbit history has no completed Sunday candle")
    return pd.Timestamp(sundays.max())


def realized_volatility_at(
    close: pd.Series,
    decision_date: pd.Timestamp,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
) -> float:
    values = pd.to_numeric(close, errors="coerce").loc[:decision_date].dropna()
    if len(values) < lookback_days + 1:
        raise BtcDataError(
            f"Need at least {lookback_days + 1} closes for volatility, got {len(values)}"
        )
    returns = values.pct_change(fill_method=None).dropna().tail(lookback_days)
    if len(returns) != lookback_days:
        raise BtcDataError("Volatility window is incomplete")
    volatility = float(returns.std(ddof=0) * math.sqrt(365))
    return _finite_positive(volatility, "realized volatility")


def build_strategy_signal(
    upbit_history: pd.DataFrame,
    halving: Mapping[str, Any],
) -> StrategySignal:
    if "Close" not in upbit_history:
        raise BtcDataError("Upbit history is missing Close")
    decision_date = latest_completed_sunday(pd.DatetimeIndex(upbit_history.index))
    volatility = realized_volatility_at(upbit_history["Close"], decision_date)
    progress = float(halving["cycle_progress"])
    holding = is_holding_window(progress)
    cap = min(1.0, TARGET_ANNUAL_VOLATILITY / volatility)
    target = cap if holding else 0.0
    return StrategySignal(
        decision_date=decision_date.date().isoformat(),
        cycle_progress=progress,
        phase_label=str(halving.get("phase_label", "UNKNOWN")),
        in_holding_window=holding,
        realized_volatility=volatility,
        volatility_cap=cap,
        target_weight=target,
    )


def _initial_state(budget_krw: float, now_utc: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "created_at_utc": now_utc.astimezone(UTC).isoformat(),
        "updated_at_utc": now_utc.astimezone(UTC).isoformat(),
        "last_data_status": "unknown",
        "last_error": None,
        "weekly_signal": None,
        "portfolio": {
            "initial_capital_krw": float(budget_krw),
            "cash_krw": float(budget_krw),
            "btc_quantity": 0.0,
            "start_equity_krw": float(budget_krw),
            "peak_equity_krw": float(budget_krw),
            "max_drawdown": 0.0,
            "last_mark_price_krw": None,
        },
        "history": [],
    }


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
        raise BtcDataError(f"Invalid advisory state: {exc}") from exc
    if not isinstance(state, dict) or state.get("strategy_version") != STRATEGY_VERSION:
        return _initial_state(budget_krw, now_utc), True
    return state, False


def save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _portfolio_snapshot(state: Mapping[str, Any], mark_price_krw: float) -> dict[str, float]:
    portfolio = state["portfolio"]
    cash = float(portfolio["cash_krw"])
    quantity = float(portfolio["btc_quantity"])
    btc_value = quantity * mark_price_krw
    equity = cash + btc_value
    weight = btc_value / equity if equity > 0 else 0.0
    return {
        "cash_krw": cash,
        "btc_quantity": quantity,
        "btc_value_krw": btc_value,
        "equity_krw": equity,
        "actual_weight": weight,
    }


def plan_rebalance(
    *,
    actual_weight: float,
    target_weight: float,
    equity_krw: float,
    current_btc_value_krw: float,
    current_btc_quantity: float,
) -> RebalancePlan:
    force_exit = math.isclose(target_weight, 0.0, abs_tol=1e-12) and current_btc_quantity > 0
    difference = target_weight - actual_weight
    should_trade = force_exit or abs(difference) >= REBALANCE_DEADBAND
    target_value = equity_krw * target_weight
    adjustment = target_value - current_btc_value_krw if should_trade else 0.0
    if not should_trade:
        action = "보유" if target_weight > 0 else "현금대기"
    elif adjustment > 0:
        action = "신규매수" if current_btc_quantity <= 0 else "추가매수"
    elif target_weight <= 0:
        action = "전량매도"
    else:
        action = "비중축소"
    return RebalancePlan(
        action=action,
        should_trade=should_trade,
        before_weight=actual_weight,
        target_weight=target_weight,
        adjustment_krw=adjustment,
        target_btc_value_krw=target_value,
        model_equity_krw=equity_krw,
    )


def apply_model_rebalance(
    state: dict[str, Any],
    plan: RebalancePlan,
    mark_price_krw: float,
) -> None:
    if not plan.should_trade:
        return
    portfolio = state["portfolio"]
    cash = float(portfolio["cash_krw"])
    quantity = float(portfolio["btc_quantity"])
    if plan.adjustment_krw > 0:
        gross_budget = min(plan.adjustment_krw, cash)
        bought = gross_budget / (mark_price_krw * (1 + UPBIT_FEE_RATE))
        notional = bought * mark_price_krw
        fee = notional * UPBIT_FEE_RATE
        cash -= notional + fee
        quantity += bought
    elif plan.adjustment_krw < 0:
        sold = min(-plan.adjustment_krw / mark_price_krw, quantity)
        notional = sold * mark_price_krw
        fee = notional * UPBIT_FEE_RATE
        cash += notional - fee
        quantity -= sold
    portfolio["cash_krw"] = max(0.0, cash)
    portfolio["btc_quantity"] = max(0.0, quantity)


def update_performance(
    state: dict[str, Any],
    *,
    kst_date: date,
    mark_price_krw: float,
) -> dict[str, float | None]:
    snapshot = _portfolio_snapshot(state, mark_price_krw)
    portfolio = state["portfolio"]
    peak = max(float(portfolio.get("peak_equity_krw", 0)), snapshot["equity_krw"])
    drawdown = snapshot["equity_krw"] / peak - 1 if peak > 0 else 0.0
    max_drawdown = min(float(portfolio.get("max_drawdown", 0)), drawdown)
    portfolio["peak_equity_krw"] = peak
    portfolio["max_drawdown"] = max_drawdown
    portfolio["last_mark_price_krw"] = mark_price_krw

    history = list(state.get("history", []))
    row = {"date": kst_date.isoformat(), "equity_krw": snapshot["equity_krw"]}
    if history and history[-1].get("date") == row["date"]:
        history[-1] = row
    else:
        history.append(row)
    state["history"] = history[-HISTORY_LIMIT:]

    start_equity = float(portfolio["start_equity_krw"])
    total_return = snapshot["equity_krw"] / start_equity - 1 if start_equity > 0 else None
    cutoff = kst_date - timedelta(days=30)
    prior = next(
        (
            item
            for item in state["history"]
            if date.fromisoformat(str(item["date"])) >= cutoff
        ),
        None,
    )
    return_30d = None
    if prior and float(prior["equity_krw"]) > 0:
        return_30d = snapshot["equity_krw"] / float(prior["equity_krw"]) - 1
    return {
        **snapshot,
        "total_return": total_return,
        "return_30d": return_30d,
        "max_drawdown": max_drawdown,
    }


def fetch_current_upbit_price(market: str = DEFAULT_MARKET) -> float:
    url = "https://api.upbit.com/v1/ticker?" + urlencode({"markets": market})
    payload = fetch_json(url)
    if not isinstance(payload, list) or not payload:
        raise BtcDataError("Upbit ticker returned no data")
    return _finite_positive(payload[0].get("trade_price"), "Upbit trade price")


def load_upbit_history(
    *,
    refresh: bool,
    config: Mapping[str, Any],
    now_utc: datetime,
) -> tuple[pd.DataFrame, bool, str | None]:
    btc = config.get("btc", {})
    data_cfg = btc.get("data", {})
    market = str(btc.get("execution_market", DEFAULT_MARKET))
    start = date.fromisoformat(str(data_cfg.get("upbit_start_date", "2017-09-25")))
    paths = resolve_paths(dict(config))
    cache_file = paths.cache / f"upbit_{market.replace('-', '_')}_daily.csv"
    try:
        if refresh:
            candles = fetch_upbit_history(market, start, as_of_utc=now_utc)
            frame = candles_to_frame(candles)
            write_price_cache(frame, cache_file)
        else:
            frame = read_price_cache(cache_file)
        fallback = False
        error = None
    except Exception as exc:
        if refresh and cache_file.exists():
            frame = read_price_cache(cache_file)
            fallback = True
            error = str(exc)
        else:
            raise BtcDataError(f"Upbit history unavailable: {exc}") from exc
    latest = pd.Timestamp(frame.index.max()).date()
    expected = now_utc.astimezone(UTC).date() - timedelta(days=1)
    if latest < expected:
        raise BtcDataError(
            f"Upbit finalized candle is stale: latest {latest}, expected at least {expected}"
        )
    return frame, fallback, error


def load_halving_context(
    *,
    refresh: bool,
    config: Mapping[str, Any],
    now_utc: datetime,
) -> tuple[dict[str, Any], bool, str | None]:
    paths = resolve_paths(dict(config))
    cache_file = paths.cache / "halving_context_v07.json"
    max_gap = int(config.get("btc", {}).get("data", {}).get("max_block_height_gap", 3))
    try:
        if refresh:
            context = build_halving_context(
                EsploraProvider("mempool.space", "https://mempool.space/api"),
                EsploraProvider("blockstream.info", "https://blockstream.info/api"),
                max_height_gap=max_gap,
            )
            payload = halving_context_to_dict(context)
            save_json(cache_file, payload)
        else:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        fallback = False
        error = None
    except Exception as exc:
        if refresh and cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            fallback = True
            error = str(exc)
        else:
            raise BtcDataError(f"Halving context unavailable: {exc}") from exc
    tip_time = datetime.fromisoformat(str(payload["tip_time_utc"]).replace("Z", "+00:00"))
    age_hours = (now_utc - tip_time.astimezone(UTC)).total_seconds() / 3600
    if age_hours > 6:
        raise BtcDataError(f"Halving block tip is stale by {age_hours:.1f} hours")
    return payload, fallback, error


def _percent(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _krw(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{round(value):,}원"


def _next_condition(signal: StrategySignal) -> str:
    progress = signal.cycle_progress
    if signal.in_holding_window and progress >= ENTRY_PROGRESS:
        return "다음 반감기 후 사이클 진행률 35%를 넘으면 전량매도 검토"
    if signal.in_holding_window:
        remaining = max(0.0, EXIT_PROGRESS - progress)
        return f"사이클 진행률 35%까지 {remaining * 100:.2f}%p 남음"
    remaining = max(0.0, ENTRY_PROGRESS - progress)
    return (
        f"사이클 진행률 65%까지 {remaining * 100:.2f}%p 남음; "
        "도달 후 월요일 확정 신호에서 매수비중 계산"
    )


def build_message(payload: Mapping[str, Any]) -> str:
    if payload.get("data_status") != "ok":
        return "\n".join(
            [
                "[Quant Guardian BTC v0.7]",
                "",
                "[데이터 오류]",
                str(payload.get("error", "알 수 없는 오류")),
                "",
                "새 매수·매도 판단을 중단하고 마지막 정상 판단을 유지합니다.",
                "자동주문은 없습니다.",
            ]
        )
    signal = payload["signal"]
    plan = payload["plan"]
    performance = payload["performance"]
    heading = {
        "initial": "초기 등록 완료",
        "weekly": "주간 매매판단",
        "monthly": "월간 포함 주간판단",
        "recovery": "데이터 정상화",
        "manual": "수동 점검",
    }.get(str(payload.get("notification_reason")), "상태 알림")
    adjustment = float(plan["adjustment_krw"])
    if plan["should_trade"]:
        order_line = (
            f"- 1회 조정 검토액: {_krw(abs(adjustment))} "
            f"({'매수' if adjustment > 0 else '매도'})"
        )
    else:
        order_line = "- 이번 주 주문: 없음"
    performance_lines = [
        f"- 추종계좌 누적수익률: {_percent(performance.get('total_return'))}",
        f"- 추종계좌 최대낙폭: {_percent(performance.get('max_drawdown'))}",
    ]
    if payload.get("monthly_summary"):
        performance_lines.insert(
            1, f"- 최근 약 30일 수익률: {_percent(performance.get('return_30d'))}"
        )
    warning = (
        "현재 보유 0 BTC에서 시작한 500만원 추종모형입니다. 실제 체결 후 Upbit 계좌와 "
        "차이가 생기면 목표평가액을 기준으로 직접 보정해야 합니다."
    )
    return "\n".join(
        [
            "[Quant Guardian BTC v0.7]",
            f"알림: {heading}",
            f"기준 주간봉: {signal['decision_date']} UTC 일봉 마감",
            "",
            "[현재 행동]",
            f"- 판단: {plan['action']}",
            f"- 실전 기준: vol40 위험균형형",
            f"- 목표 BTC 비중: {_percent(signal['target_weight'])}",
            f"- 조정 전 추종비중: {_percent(plan['before_weight'])}",
            order_line,
            "",
            "[500만원 시작 추종계좌]",
            f"- 현재 추종자산: {_krw(performance['equity_krw'])}",
            f"- 목표 BTC 평가액: {_krw(plan['target_btc_value_krw'])}",
            f"- Upbit 참고가격: {_krw(payload['upbit_price_krw'])}",
            "",
            "[반감기·위험]",
            f"- 사이클 진행률: {_percent(signal['cycle_progress'], 2)}",
            f"- 현재 국면: {signal['phase_label']}",
            f"- 보유구간: {'예' if signal['in_holding_window'] else '아니오'}",
            f"- 63일 연환산 변동성: {_percent(signal['realized_volatility'])}",
            f"- 변동성 허용비중: {_percent(signal['volatility_cap'])}",
            f"- 다음 조건: {_next_condition(StrategySignal(**signal))}",
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
    upbit_history: pd.DataFrame,
    halving: Mapping[str, Any],
    mark_price: float,
    force_notify: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now_kst = now_utc.astimezone(KST)
    is_monday = now_kst.weekday() == 0
    monthly_summary = is_monday and now_kst.day <= 7
    previous_status = str(state.get("last_data_status", "unknown"))

    fresh_signal = build_strategy_signal(upbit_history, halving)
    previous_weekly = state.get("weekly_signal")
    if initial or is_monday or not isinstance(previous_weekly, dict):
        active_signal = fresh_signal
        state["weekly_signal"] = asdict(fresh_signal)
    else:
        active_signal = StrategySignal(**previous_weekly)

    before = _portfolio_snapshot(state, mark_price)
    should_evaluate_trade = initial or is_monday
    if should_evaluate_trade:
        plan = plan_rebalance(
            actual_weight=before["actual_weight"],
            target_weight=active_signal.target_weight,
            equity_krw=before["equity_krw"],
            current_btc_value_krw=before["btc_value_krw"],
            current_btc_quantity=before["btc_quantity"],
        )
        apply_model_rebalance(state, plan, mark_price)
    else:
        plan = RebalancePlan(
            action="보유" if active_signal.target_weight > 0 else "현금대기",
            should_trade=False,
            before_weight=before["actual_weight"],
            target_weight=active_signal.target_weight,
            adjustment_krw=0.0,
            target_btc_value_krw=before["equity_krw"] * active_signal.target_weight,
            model_equity_krw=before["equity_krw"],
        )

    performance = update_performance(
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
    should_notify = reason != "none"
    state["last_data_status"] = "ok"
    state["last_error"] = None
    state["updated_at_utc"] = now_utc.isoformat()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_kst": now_kst.isoformat(),
        "data_status": "ok",
        "should_notify": should_notify,
        "notification_reason": reason,
        "monthly_summary": monthly_summary,
        "upbit_price_krw": mark_price,
        "signal": asdict(active_signal),
        "plan": asdict(plan),
        "performance": performance,
        "portfolio_after": _portfolio_snapshot(state, mark_price),
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
    should_notify = initial or previous_status != "error" or now_kst.weekday() == 0 or force_notify
    state["last_data_status"] = "error"
    state["last_error"] = str(error)
    state["updated_at_utc"] = now_utc.isoformat()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_kst": now_kst.isoformat(),
        "data_status": "error",
        "should_notify": should_notify,
        "notification_reason": "error",
        "error": str(error),
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
            raise BtcDataError("BTC advisory requires shadow mode")
        if bool(config.get("btc", {}).get("auto_order", False)):
            raise BtcDataError("BTC advisory refuses automatic-order configuration")
        history, price_fallback, price_error = load_upbit_history(
            refresh=refresh,
            config=config,
            now_utc=now,
        )
        halving, block_fallback, block_error = load_halving_context(
            refresh=refresh,
            config=config,
            now_utc=now,
        )
        try:
            mark_price = fetch_current_upbit_price(
                str(config.get("btc", {}).get("execution_market", DEFAULT_MARKET))
            )
        except Exception:
            mark_price = _finite_positive(history["Close"].iloc[-1], "latest Upbit close")
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
    save_json(state_path, state)
    save_json(output_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant Guardian BTC v0.7 Upbit advisory")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-file", type=Path, default=Path("data/state/btc_v07_state.json"))
    parser.add_argument("--output", type=Path, default=Path("output/btc_v07_advisory.json"))
    parser.add_argument("--budget-krw", type=float, default=DEFAULT_BUDGET_KRW)
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
