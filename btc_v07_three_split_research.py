from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from btc_cycle_research import build_synthetic_krw_market
from btc_guardian import (
    BtcDataError,
    DEFAULT_CONFIG,
    ROOT,
    build_phase1_report,
    cache_key,
    closed_yahoo_daily_frame,
    iso_utc,
    load_config,
    read_price_cache,
    resolve_paths,
    utc_now,
)
from btc_halving_research import completed_halving_epochs
from btc_momentum_research import MomentumVolatilityParameters, add_momentum_volatility_features
from btc_research import SimulationResult, build_feature_frame, load_coinmetrics_history, performance_metrics
from btc_return_eval import simulate_candidate
from btc_return_models import Candidate, add_return_features, generate_return_decisions
from quant_guardian import annualized_metrics


STRATEGY_VERSION = "btc-v07-three-split-research-1.0"
DEFAULT_START_DATE = "2016-01-01"
DEFAULT_INITIAL_CAPITAL_KRW = 10_000_000.0
BASELINE_CANDIDATE = Candidate(
    candidate_id="window_pre65_post35_vol40_db10_actual",
    family="window",
    pre_start=0.65,
    exit_start=None,
    hard_end=0.35,
    target_vol=0.40,
    deadband=0.10,
    policy="actual_weight",
    exposure_mode="vol",
)


@dataclass(frozen=True)
class SplitVariant:
    variant_id: str
    entry_parts: int
    exit_parts: int
    description: str


VARIANTS = (
    SplitVariant("one_shot", 1, 1, "매수·매도 모두 목표비중까지 한 번에 조정"),
    SplitVariant("entry_3_exit_1", 3, 1, "매수만 3주 분할, 매도는 한 번에 조정"),
    SplitVariant("entry_1_exit_3", 1, 3, "매수는 한 번에, 매도만 3주 분할"),
    SplitVariant("entry_3_exit_3", 3, 3, "매수·매도 모두 3주 균등 분할"),
)


def _trade_to_weight(
    *,
    cash: float,
    units: float,
    open_price: float,
    target_weight: float,
    fee_rate: float,
    slippage_rate: float,
) -> tuple[float, float, dict[str, Any]]:
    open_nav = cash + units * open_price
    actual_open_weight = units * open_price / open_nav if open_nav > 0 else 0.0
    target = float(np.clip(target_weight, 0.0, 1.0))
    desired_units = open_nav * target / open_price
    unit_change = desired_units - units
    fee_cost = 0.0
    slippage_cost = 0.0
    traded_units = 0.0
    side = ""
    if unit_change > 1e-15:
        execution_price = open_price * (1 + slippage_rate)
        max_units = cash / (execution_price * (1 + fee_rate))
        bought = min(unit_change, max_units)
        notional = bought * execution_price
        fee_cost = notional * fee_rate
        slippage_cost = bought * open_price * slippage_rate
        cash -= notional + fee_cost
        units += bought
        traded_units = bought
        side = "BUY"
    elif unit_change < -1e-15:
        execution_price = open_price * (1 - slippage_rate)
        sold = min(-unit_change, units)
        notional = sold * execution_price
        fee_cost = notional * fee_rate
        slippage_cost = sold * open_price * slippage_rate
        cash += notional - fee_cost
        units -= sold
        traded_units = -sold
        side = "SELL"
    turnover = abs(traded_units) * open_price / open_nav if open_nav > 0 else 0.0
    return max(cash, 0.0), max(units, 0.0), {
        "side": side,
        "traded_units": traded_units,
        "fee_cost": fee_cost,
        "slippage_cost": slippage_cost,
        "turnover": turnover,
        "actual_open_weight": actual_open_weight,
        "open_nav": open_nav,
        "target_weight": target,
    }


def simulate_three_split(
    features: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    entry_parts: int,
    exit_parts: int,
    rebalance_deadband: float,
    fee_bps: float,
    slippage_bps: float,
    initial_capital: float,
) -> SimulationResult:
    """Execute each qualifying target change in 1 or 3 weekly equal-weight steps.

    A plan freezes the target observed at its start. At later weekly decisions, the
    remaining plan continues unless the new target differs from the frozen target by
    at least the deadband or reverses direction. In that case, the old plan is
    cancelled and a new plan starts from the then-current actual weight.
    """

    if entry_parts not in {1, 3} or exit_parts not in {1, 3}:
        raise ValueError("Entry and exit parts must be 1 or 3")
    if not 0 <= rebalance_deadband < 1:
        raise ValueError("Deadband must be in [0, 1)")
    required_features = {"open", "close"}
    required_decisions = {"desired_weight", "is_decision_day", "reason"}
    if not required_features.issubset(features.columns):
        raise ValueError("Features require open and close")
    if not required_decisions.issubset(decisions.columns):
        raise ValueError("Decisions require desired_weight, is_decision_day, reason")

    execution_target = decisions["desired_weight"].where(
        decisions["is_decision_day"].fillna(False)
    ).shift(1)
    execution_reason = decisions["reason"].where(
        decisions["is_decision_day"].fillna(False)
    ).shift(1)
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000

    cash = float(initial_capital)
    units = 0.0
    previous_equity = float(initial_capital)
    plan: dict[str, Any] | None = None
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for position, index in enumerate(features.index):
        open_price = float(features.at[index, "open"])
        close_price = float(features.at[index, "close"])
        if not math.isfinite(open_price) or open_price <= 0:
            raise ValueError(f"Invalid open price on {index}")
        if not math.isfinite(close_price) or close_price <= 0:
            raise ValueError(f"Invalid close price on {index}")

        open_nav = cash + units * open_price
        actual_open_weight = units * open_price / open_nav if open_nav > 0 else 0.0
        latest_raw = execution_target.loc[index]
        latest_target = math.nan
        plan_restarted = False

        if pd.notna(latest_raw):
            latest_target = float(np.clip(float(latest_raw), 0.0, 1.0))
            force_zero = math.isclose(latest_target, 0.0, abs_tol=1e-12) and units > 1e-15
            latest_gap = latest_target - actual_open_weight
            trigger = force_zero or abs(latest_gap) + 1e-12 >= rebalance_deadband

            if plan is not None:
                frozen_target = float(plan["final_target"])
                frozen_direction = math.copysign(1.0, frozen_target - actual_open_weight) if not math.isclose(frozen_target, actual_open_weight, abs_tol=1e-12) else 0.0
                latest_direction = math.copysign(1.0, latest_gap) if not math.isclose(latest_gap, 0.0, abs_tol=1e-12) else 0.0
                materially_new = abs(latest_target - frozen_target) + 1e-12 >= rebalance_deadband
                direction_reversal = (
                    frozen_direction != 0.0
                    and latest_direction != 0.0
                    and frozen_direction != latest_direction
                )
                if force_zero or materially_new or direction_reversal:
                    plan = None
                    plan_restarted = True

            if plan is None and trigger:
                parts = entry_parts if latest_target > actual_open_weight else exit_parts
                plan = {
                    "start_weight": actual_open_weight,
                    "final_target": latest_target,
                    "total_parts": parts,
                    "completed_parts": 0,
                    "reason": str(execution_reason.loc[index]),
                    "signal_date": (
                        features.index[position - 1].date().isoformat()
                        if position >= 1
                        else None
                    ),
                }
                plan_restarted = True

        fee_cost = 0.0
        slippage_cost = 0.0
        turnover = 0.0
        executed = False
        step_target = math.nan
        step_number = 0
        total_parts = 0
        trade_side = ""

        if pd.notna(latest_raw) and plan is not None:
            total_parts = int(plan["total_parts"])
            step_number = int(plan["completed_parts"]) + 1
            fraction = step_number / total_parts
            step_target = float(plan["start_weight"]) + (
                float(plan["final_target"]) - float(plan["start_weight"])
            ) * fraction
            cash, units, trade = _trade_to_weight(
                cash=cash,
                units=units,
                open_price=open_price,
                target_weight=step_target,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
            )
            fee_cost = float(trade["fee_cost"])
            slippage_cost = float(trade["slippage_cost"])
            turnover = float(trade["turnover"])
            trade_side = str(trade["side"])
            executed = bool(trade_side)
            if executed:
                trade_rows.append(
                    {
                        "date": index,
                        "signal_date": plan["signal_date"],
                        "side": trade_side,
                        "step_number": step_number,
                        "total_parts": total_parts,
                        "step_target": step_target,
                        "final_target": float(plan["final_target"]),
                        "actual_open_weight": float(trade["actual_open_weight"]),
                        "units": abs(float(trade["traded_units"])),
                        "open_price": open_price,
                        "fee_cost": fee_cost,
                        "slippage_cost": slippage_cost,
                        "turnover": turnover,
                        "plan_restarted": plan_restarted,
                        "reason": plan["reason"],
                    }
                )
            plan["completed_parts"] = step_number
            if step_number >= total_parts:
                plan = None

        equity = cash + units * close_price
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "equity": equity,
                "daily_return": daily_return,
                "cash": cash,
                "btc_units": units,
                "actual_weight": actual_weight,
                "actual_open_weight": actual_open_weight,
                "latest_target": latest_target,
                "step_target": step_target,
                "step_number": step_number,
                "total_parts": total_parts,
                "executed_trade": executed,
                "trade_side": trade_side,
                "plan_active_after": plan is not None,
                "plan_restarted": plan_restarted,
                "turnover": turnover,
                "fee_cost": fee_cost,
                "slippage_cost": slippage_cost,
            }
        )
        previous_equity = equity

    return SimulationResult(
        daily=pd.DataFrame(daily_rows).set_index("Date"),
        trades=pd.DataFrame(trade_rows),
    )


def metrics_row(
    *,
    data_mode: str,
    variant: SplitVariant,
    simulation: SimulationResult,
    initial_capital: float,
) -> dict[str, Any]:
    metrics = performance_metrics(simulation)
    terminal = float(metrics["terminal_wealth"])
    return {
        "data_mode": data_mode,
        "variant_id": variant.variant_id,
        "description": variant.description,
        "entry_parts": variant.entry_parts,
        "exit_parts": variant.exit_parts,
        "initial_capital_krw": initial_capital,
        "terminal_wealth_krw": terminal,
        "profit_krw": terminal - initial_capital,
        "capital_multiple": terminal / initial_capital,
        **metrics,
    }


def _period_metrics(simulation: SimulationResult, index: pd.Index) -> dict[str, Any]:
    daily = simulation.daily.reindex(index).dropna(subset=["daily_return"])
    metrics = annualized_metrics(daily["daily_return"], periods_per_year=365)
    if simulation.trades.empty:
        trades = 0
    else:
        dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        trades = int(((dates >= daily.index.min()) & (dates <= daily.index.max())).sum())
    return {
        **metrics,
        "exposure": float(daily["actual_weight"].mean()),
        "trades": trades,
    }


def load_feature_frames(
    *,
    refresh: bool,
    config_path: Path,
    now_utc: datetime,
    start_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow" or bool(btc_cfg.get("auto_order", False)):
        raise BtcDataError("Three-split research requires shadow mode and no orders")
    phase1 = build_phase1_report(refresh=refresh, config_path=config_path, now_utc=now_utc)
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 data gate is blocked")
    onchain, fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh, config=config, now_utc=now_utc
    )
    paths = resolve_paths(config)
    data_cfg = btc_cfg.get("data", {})
    runtime = btc_cfg.get("research_runtime", {})
    cache_dir = ROOT / config.get("settings", {}).get("cache_dir", "data/cache")
    usd = closed_yahoo_daily_frame(
        read_price_cache(cache_dir / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))),
        now_utc,
    )
    fx = closed_yahoo_daily_frame(
        read_price_cache(cache_dir / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))),
        now_utc,
    )
    synthetic = build_synthetic_krw_market(usd, fx)
    upbit = read_price_cache(
        paths.cache / f"upbit_{str(btc_cfg.get('execution_market', 'KRW-BTC')).replace('-', '_')}_daily.csv"
    )
    lag = int(runtime.get("onchain_lag_days", 2))
    minimum = int(runtime.get("percentile_min_periods", 730))
    synthetic_history = build_feature_frame(
        synthetic, usd, fx, onchain, onchain_lag_days=lag, percentile_min_periods=minimum
    )
    upbit_history = build_feature_frame(
        upbit, usd, fx, onchain, onchain_lag_days=lag, percentile_min_periods=minimum
    )
    momentum = MomentumVolatilityParameters()
    synthetic_features = add_return_features(
        add_momentum_volatility_features(synthetic_history, momentum)
    )
    upbit_features = add_return_features(
        add_momentum_volatility_features(upbit_history, momentum)
    )
    full = synthetic_features.loc[pd.Timestamp(start_date):].copy()
    ready = (
        full["momentum_feature_ready"].fillna(False)
        & full["phase_label"].ne("UNKNOWN")
        & full["wma40"].notna()
    )
    if not ready.any():
        raise BtcDataError("No research-ready synthetic row")
    full = full.loc[ready.idxmax():]

    common = full.index.intersection(upbit_features.index)
    common_ready = (
        full.loc[common, "momentum_feature_ready"].fillna(False)
        & upbit_features.loc[common, "momentum_feature_ready"].fillna(False)
        & upbit_features.loc[common, "phase_label"].ne("UNKNOWN")
        & upbit_features.loc[common, "wma40"].notna()
    )
    common = common[common_ready.to_numpy()]
    if len(common) < 730:
        raise BtcDataError("Upbit overlap has fewer than 730 rows")
    metadata = {
        "fee_bps": float(runtime.get("fee_bps", 5.0)),
        "slippage_bps": float(runtime.get("slippage_bps", 10.0)),
        "onchain_cache_fallback": fallback,
        "onchain_cache_error": onchain_error,
        "full_start": full.index.min().date().isoformat(),
        "full_end": full.index.max().date().isoformat(),
        "upbit_start": common.min().date().isoformat(),
        "upbit_end": common.max().date().isoformat(),
    }
    return full, upbit_features.loc[common].copy(), metadata


def evaluate_variants(
    features: pd.DataFrame,
    *,
    data_mode: str,
    initial_capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    decisions = generate_return_decisions(features, BASELINE_CANDIDATE)
    rows: list[dict[str, Any]] = []
    outputs: dict[str, SimulationResult] = {}
    for variant in VARIANTS:
        if variant.variant_id == "one_shot":
            _, simulation = simulate_candidate(
                features,
                BASELINE_CANDIDATE,
                capital=initial_capital,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
        else:
            simulation = simulate_three_split(
                features,
                decisions,
                entry_parts=variant.entry_parts,
                exit_parts=variant.exit_parts,
                rebalance_deadband=BASELINE_CANDIDATE.deadband,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                initial_capital=initial_capital,
            )
        outputs[variant.variant_id] = simulation
        rows.append(
            metrics_row(
                data_mode=data_mode,
                variant=variant,
                simulation=simulation,
                initial_capital=initial_capital,
            )
        )
    result = pd.DataFrame(rows)
    baseline = result.loc[result["variant_id"] == "one_shot"].iloc[0]
    result["terminal_delta_krw"] = result["terminal_wealth_krw"] - float(baseline["terminal_wealth_krw"])
    result["terminal_delta_pct"] = result["terminal_wealth_krw"] / float(baseline["terminal_wealth_krw"]) - 1
    result["cagr_delta_pp"] = (result["cagr"] - float(baseline["cagr"])) * 100
    result["mdd_delta_pp"] = (result["mdd"] - float(baseline["mdd"])) * 100
    return result, outputs


def cycle_comparison(features: pd.DataFrame, outputs: Mapping[str, SimulationResult]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    completed = completed_halving_epochs(features)
    for variant_id, simulation in outputs.items():
        for epoch in completed:
            index = features.index[features["halving_epoch"] == epoch]
            rows.append(
                {
                    "variant_id": variant_id,
                    "halving_epoch": epoch,
                    **_period_metrics(simulation, index),
                }
            )
    return pd.DataFrame(rows)


def _fmt_pct(value: Any) -> str:
    return "n/a" if pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if pd.isna(value) else f"{float(value):,.0f}원"


def build_report(
    manifest: Mapping[str, Any],
    comparison: pd.DataFrame,
    cycles: pd.DataFrame,
) -> str:
    lines = [
        "# BTC v0.7 일괄조정 vs 3주 분할 연구",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 전체기간: {manifest['full_period']}",
        f"- Upbit 비교기간: {manifest['upbit_period']}",
        "- 기준전략: `window_pre65_post35_vol40_db10_actual`",
        "- 초기자금: 10,000,000원",
        "- 상태: 과거 연구 / 실전·Telegram 미반영",
        "",
        "## 3분할 정의",
        "",
        "- 신호 다음 월요일 시가부터 3주에 걸쳐 목표비중 차이의 1/3·2/3·3/3으로 이동한다.",
        "- 분할 중 새 목표가 기존 최종목표와 10%p 이상 달라지거나 방향이 반전되면 기존 계획을 취소하고 현재 실제비중에서 새 3주 계획을 시작한다.",
        "- 매수와 매도를 따로 분리해 어떤 쪽이 성과 차이를 만드는지도 비교한다.",
        "",
    ]
    for mode in ("full", "upbit"):
        part = comparison.loc[comparison["data_mode"] == mode]
        title = "2016년 이후 전체기간" if mode == "full" else "실제 Upbit 중첩기간"
        lines.extend(
            [
                f"## {title}",
                "",
                "| 방식 | 최종자산 | CAGR | MDD | 거래 | CAGR 차이 | MDD 차이 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for _, row in part.iterrows():
            lines.append(
                f"| {row['variant_id']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
                f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | {int(row['trades'])} | "
                f"{float(row['cagr_delta_pp']):+.2f}%p | {float(row['mdd_delta_pp']):+.2f}%p |"
            )
        lines.append("")

    lines.extend(
        [
            "## 완료 반감기 사이클",
            "",
            "| 방식 | Epoch | CAGR | MDD | 평균노출 | 거래 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in cycles.iterrows():
        lines.append(
            f"| {row['variant_id']} | {int(row['halving_epoch'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['exposure'])} | {int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 제한",
            "",
            "- 이 비교는 v0.7을 선택한 뒤 같은 과거 데이터에서 수행한 후속 진단이므로 순수 표본외 검증이 아니다.",
            "- 3주 분할은 심리적·체결 위험을 줄일 수 있지만, 가격 경로에 따라 수익률과 MDD가 모두 좋아지거나 나빠질 수 있다.",
            "- 세금은 제외하고 수수료 5bps, 슬리피지 10bps를 적용한다.",
            "- 결과 확인 전 현재 Telegram의 일괄조정 규칙은 변경하지 않는다.",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def run_research(
    *,
    refresh: bool,
    config_path: Path,
    start_date: str,
    initial_capital_krw: float,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    full, upbit, metadata = load_feature_frames(
        refresh=refresh,
        config_path=config_path,
        now_utc=now,
        start_date=start_date,
    )
    full_result, full_outputs = evaluate_variants(
        full,
        data_mode="full",
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    upbit_result, _ = evaluate_variants(
        upbit,
        data_mode="upbit",
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    comparison = pd.concat([full_result, upbit_result], ignore_index=True)
    cycles = cycle_comparison(full, full_outputs)
    paths = resolve_paths(load_config(config_path))
    output = {
        "comparison": paths.output / "btc_v07_three_split_comparison.csv",
        "cycles": paths.output / "btc_v07_three_split_cycles.csv",
        "manifest": paths.output / "btc_v07_three_split_manifest.json",
        "report": paths.output / "btc_v07_three_split_report.md",
    }
    comparison.to_csv(output["comparison"], index=False, encoding="utf-8-sig")
    cycles.to_csv(output["cycles"], index=False, encoding="utf-8-sig")
    manifest = {
        "schema_version": "btc-v07-three-split-1.0",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "baseline_candidate": BASELINE_CANDIDATE.candidate_id,
        "split_definition": "three consecutive weekly opens at 1/3, 2/3, 3/3 of frozen target gap; restart on >=10pp target change or direction reversal",
        "full_period": f"{metadata['full_start']}~{metadata['full_end']}",
        "upbit_period": f"{metadata['upbit_start']}~{metadata['upbit_end']}",
        "fee_bps": metadata["fee_bps"],
        "slippage_bps": metadata["slippage_bps"],
        "initial_capital_krw": initial_capital_krw,
        "variants": [variant.__dict__ for variant in VARIANTS],
        "telegram_rule_changed": False,
    }
    write_json(output["manifest"], manifest)
    output["report"].write_text(build_report(manifest, comparison, cycles), encoding="utf-8-sig")
    return {
        "manifest": manifest,
        "comparison": comparison,
        "cycles": cycles,
        "outputs": {key: str(value) for key, value in output.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare BTC v0.7 one-shot and three-week split execution")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--initial-capital-krw", type=float, default=DEFAULT_INITIAL_CAPITAL_KRW)
    args = parser.parse_args()
    result = run_research(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print(result["comparison"].to_string(index=False))
    print(f"보고서: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
