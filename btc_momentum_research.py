from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from btc_cycle_research import (
    build_synthetic_krw_market,
    generate_cycle_trend_targets,
    overlap_diagnostics,
)
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
from btc_research import (
    ResearchParameters,
    SimulationResult,
    build_feature_frame,
    generate_state_targets,
    load_coinmetrics_history,
    make_buy_hold_signals,
    performance_metrics,
    simulate_strategy,
)
from quant_guardian import annualized_metrics


STRATEGY_VERSION = "btc-momentum-volatility-v0.5"
DEFAULT_START_DATE = "2016-01-01"
DEFAULT_INITIAL_CAPITAL_KRW = 10_000_000.0


@dataclass(frozen=True)
class MomentumVolatilityParameters:
    momentum_horizons_days: tuple[int, ...] = (30, 90, 180, 365)
    volatility_lookback_days: int = 63
    target_annual_volatility: float = 0.45
    rebalance_deadband: float = 0.10
    max_weight: float = 1.0


@dataclass(frozen=True)
class MomentumCandidate:
    candidate_id: str
    parameters: MomentumVolatilityParameters
    research_origin: str
    primary_reference: bool = False


def _validate_parameters(parameters: MomentumVolatilityParameters) -> None:
    if not parameters.momentum_horizons_days:
        raise ValueError("At least one momentum horizon is required")
    if any(horizon < 2 for horizon in parameters.momentum_horizons_days):
        raise ValueError("Momentum horizons must be at least two days")
    if parameters.volatility_lookback_days < 2:
        raise ValueError("Volatility lookback must be at least two days")
    if not 0 < parameters.target_annual_volatility <= 1:
        raise ValueError("Target annual volatility must be between zero and one")
    if not 0 <= parameters.rebalance_deadband < 1:
        raise ValueError("Rebalance deadband must be between zero and one")
    if not 0 < parameters.max_weight <= 1:
        raise ValueError("Maximum weight must be between zero and one")


def add_momentum_volatility_features(
    features: pd.DataFrame,
    parameters: MomentumVolatilityParameters | None = None,
) -> pd.DataFrame:
    params = parameters or MomentumVolatilityParameters()
    _validate_parameters(params)
    frame = features.copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    for horizon in params.momentum_horizons_days:
        frame[f"momentum_{horizon}d"] = close.pct_change(
            horizon, fill_method=None
        )
    frame["realized_volatility"] = (
        close.pct_change(fill_method=None)
        .rolling(
            params.volatility_lookback_days,
            min_periods=params.volatility_lookback_days,
        )
        .std(ddof=0)
        * math.sqrt(365)
    )
    required = [
        *(f"momentum_{horizon}d" for horizon in params.momentum_horizons_days),
        "realized_volatility",
    ]
    frame["momentum_feature_ready"] = (
        frame[required].notna().all(axis=1)
        & frame["realized_volatility"].gt(0)
        & close.notna()
        & close.gt(0)
    )
    return frame


def _state_label(current: float, previous: float) -> str:
    if math.isclose(current, previous, abs_tol=1e-12):
        return "HOLD" if current > 0 else "WAIT"
    if current > previous:
        return "BUY_MORE"
    return "REDUCE" if current > 0 else "EXIT"


def generate_momentum_volatility_targets(
    features: pd.DataFrame,
    parameters: MomentumVolatilityParameters | None = None,
) -> pd.DataFrame:
    """Create a weekly long-only target from momentum breadth and a risk budget."""
    params = parameters or MomentumVolatilityParameters()
    _validate_parameters(params)
    required = {
        "momentum_feature_ready",
        "realized_volatility",
        *(f"momentum_{horizon}d" for horizon in params.momentum_horizons_days),
    }
    missing = sorted(required.difference(features.columns))
    if missing:
        raise ValueError(f"Missing momentum features: {', '.join(missing)}")

    current = 0.0
    last_decision: dict[str, Any] = {
        "positive_momentum_count": 0,
        "positive_horizons": "없음",
        "momentum_weight": 0.0,
        "realized_volatility": np.nan,
        "volatility_cap": 0.0,
        "desired_weight": 0.0,
    }
    rows: list[dict[str, Any]] = []

    for index, row in features.iterrows():
        previous = current
        is_decision_day = index.dayofweek == 6 and bool(
            row.get("momentum_feature_ready", False)
        )
        rebalanced = False
        reason = "다음 주간 확정 신호 대기"

        if is_decision_day:
            positive = [
                horizon
                for horizon in params.momentum_horizons_days
                if float(row[f"momentum_{horizon}d"]) > 0
            ]
            positive_count = len(positive)
            momentum_weight = (
                positive_count / len(params.momentum_horizons_days)
            ) * params.max_weight
            realized_volatility = float(row["realized_volatility"])
            volatility_cap = min(
                params.max_weight,
                params.target_annual_volatility / realized_volatility,
            )
            desired = min(momentum_weight, volatility_cap)

            if math.isclose(desired, 0.0, abs_tol=1e-12):
                current = 0.0
                rebalanced = not math.isclose(previous, current, abs_tol=1e-12)
                reason = "1·3·6·12개월 모멘텀이 모두 0% 이하"
            elif abs(desired - current) >= params.rebalance_deadband:
                current = desired
                rebalanced = True
                if volatility_cap < momentum_weight:
                    reason = "상승 모멘텀은 유지되지만 변동성 예산으로 비중 제한"
                else:
                    reason = "상승 모멘텀 개수에 따라 목표비중 조정"
            else:
                reason = "새 목표와 기존 비중 차이가 10%p 미만이라 유지"

            last_decision = {
                "positive_momentum_count": positive_count,
                "positive_horizons": (
                    ",".join(f"{horizon}일" for horizon in positive)
                    if positive
                    else "없음"
                ),
                "momentum_weight": float(momentum_weight),
                "realized_volatility": realized_volatility,
                "volatility_cap": float(volatility_cap),
                "desired_weight": float(desired),
            }

        rows.append(
            {
                "Date": index,
                "state": _state_label(current, previous),
                "target_weight": float(current),
                "previous_weight": float(previous),
                "is_decision_day": bool(is_decision_day),
                "rebalanced": bool(rebalanced),
                **last_decision,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows).set_index("Date")


def volatility_candidate_specs() -> list[MomentumCandidate]:
    base = MomentumVolatilityParameters()
    origin = "post-v0.4-exploratory-freeze-2026-08-10"
    return [
        MomentumCandidate(
            "momentum_vol45_v05",
            base,
            research_origin=origin,
            primary_reference=True,
        ),
        MomentumCandidate(
            "momentum_vol35_sensitivity",
            replace(base, target_annual_volatility=0.35),
            research_origin="adjacent-risk-budget-sensitivity",
        ),
        MomentumCandidate(
            "momentum_vol40_sensitivity",
            replace(base, target_annual_volatility=0.40),
            research_origin="adjacent-risk-budget-sensitivity",
        ),
        MomentumCandidate(
            "momentum_vol50_sensitivity",
            replace(base, target_annual_volatility=0.50),
            research_origin="adjacent-risk-budget-sensitivity",
        ),
    ]


def _simulate(
    features: pd.DataFrame,
    parameters: MomentumVolatilityParameters,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
    additional_delay_days: int = 0,
) -> tuple[pd.DataFrame, SimulationResult]:
    signals = generate_momentum_volatility_targets(features, parameters)
    simulation = simulate_strategy(
        features,
        signals,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        additional_delay_days=additional_delay_days,
        initial_capital=initial_capital_krw,
    )
    return signals, simulation


def _metrics_row(
    item_id: str,
    simulation: SimulationResult,
    initial_capital_krw: float,
) -> dict[str, Any]:
    metrics = performance_metrics(simulation)
    terminal = float(metrics["terminal_wealth"])
    return {
        "item_id": item_id,
        "initial_capital_krw": initial_capital_krw,
        "terminal_wealth_krw": terminal,
        "profit_krw": terminal - initial_capital_krw,
        "capital_multiple": terminal / initial_capital_krw,
        **metrics,
    }


def evaluate_volatility_candidates(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, tuple[pd.DataFrame, SimulationResult]]]:
    rows: list[dict[str, Any]] = []
    outputs: dict[str, tuple[pd.DataFrame, SimulationResult]] = {}
    for candidate in volatility_candidate_specs():
        signals, simulation = _simulate(
            features,
            candidate.parameters,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "target_annual_volatility": (
                    candidate.parameters.target_annual_volatility
                ),
                "research_origin": candidate.research_origin,
                "primary_reference": candidate.primary_reference,
                **_metrics_row(
                    candidate.candidate_id,
                    simulation,
                    initial_capital_krw,
                ),
            }
        )
        outputs[candidate.candidate_id] = (signals, simulation)
    return pd.DataFrame(rows), outputs


def evaluate_full_period_comparison(
    features: pd.DataFrame,
    primary_signals: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    signal_map = {
        "v0.3_core_tactical": generate_state_targets(
            features,
            ResearchParameters(),
            sell_policy="core_tactical",
            use_halving=True,
        ),
        "v0.4_cycle_trend": generate_cycle_trend_targets(features),
        "v0.5_momentum_vol45": primary_signals,
        "buy_hold": make_buy_hold_signals(features),
    }
    rows: list[dict[str, Any]] = []
    for strategy_id, signals in signal_map.items():
        simulation = simulate_strategy(
            features,
            signals,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=initial_capital_krw,
        )
        rows.append(
            {
                "strategy_id": strategy_id,
                **_metrics_row(strategy_id, simulation, initial_capital_krw),
            }
        )
    return pd.DataFrame(rows)


def evaluate_horizon_sensitivity(
    feature_history: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    base = MomentumVolatilityParameters()
    horizon_sets: Sequence[tuple[str, tuple[int, ...]]] = [
        ("1_3_6_12_month", (30, 90, 180, 365)),
        ("2_4_8_12_month", (60, 120, 240, 365)),
        ("1_4_8_12_month", (30, 120, 240, 365)),
    ]
    rows: list[dict[str, Any]] = []
    for sensitivity_id, horizons in horizon_sets:
        params = replace(base, momentum_horizons_days=horizons)
        features = add_momentum_volatility_features(feature_history, params).loc[
            start_date:end_date
        ]
        _, simulation = _simulate(
            features,
            params,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        rows.append(
            {
                "sensitivity_id": sensitivity_id,
                "horizons_days": ",".join(str(value) for value in horizons),
                **_metrics_row(sensitivity_id, simulation, initial_capital_krw),
            }
        )
    return pd.DataFrame(rows)


def cycle_metrics(
    features: pd.DataFrame,
    simulation: SimulationResult,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for epoch, epoch_features in features.groupby("halving_epoch"):
        if pd.isna(epoch) or len(epoch_features) < 30:
            continue
        daily = simulation.daily.reindex(epoch_features.index).dropna()
        metrics = annualized_metrics(daily["daily_return"], periods_per_year=365)
        rows.append(
            {
                "halving_epoch": int(epoch),
                "start": daily.index.min().date().isoformat(),
                "end": daily.index.max().date().isoformat(),
                "total_return": metrics["total_return"],
                "cagr": metrics["cagr"],
                "mdd": metrics["mdd"],
                "exposure": float(daily["actual_weight"].mean()),
            }
        )
    return pd.DataFrame(rows)


def robustness_diagnostics(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    params = MomentumVolatilityParameters()
    scenarios = [
        ("base", fee_bps, slippage_bps, 0),
        ("double_cost", fee_bps * 2, slippage_bps * 2, 0),
        ("delay_1d", fee_bps, slippage_bps, 1),
        ("delay_2d", fee_bps, slippage_bps, 2),
    ]
    rows: list[dict[str, Any]] = []
    for scenario, scenario_fee, scenario_slippage, delay in scenarios:
        _, simulation = _simulate(
            features,
            params,
            initial_capital_krw=initial_capital_krw,
            fee_bps=scenario_fee,
            slippage_bps=scenario_slippage,
            additional_delay_days=delay,
        )
        rows.append(
            {
                "scenario": scenario,
                "fee_bps": scenario_fee,
                "slippage_bps": scenario_slippage,
                "additional_delay_days": delay,
                **_metrics_row(scenario, simulation, initial_capital_krw),
            }
        )
    return pd.DataFrame(rows)


def data_mode_strategy_comparison(
    synthetic_history: pd.DataFrame,
    upbit_history: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    params = MomentumVolatilityParameters()
    synthetic = add_momentum_volatility_features(synthetic_history, params)
    upbit = add_momentum_volatility_features(upbit_history, params)
    common = synthetic.index.intersection(upbit.index)
    ready = (
        synthetic.loc[common, "momentum_feature_ready"]
        & upbit.loc[common, "momentum_feature_ready"]
    )
    common = common[ready.to_numpy()]
    if len(common) < 730:
        raise BtcDataError("Momentum data-mode overlap has fewer than 730 rows")

    rows: list[dict[str, Any]] = []
    for mode, history in (("synthetic", synthetic), ("upbit", upbit)):
        features = history.loc[common].copy()
        primary_signals = generate_momentum_volatility_targets(features, params)
        signal_map = {
            "v0.3_core_tactical": generate_state_targets(
                features,
                ResearchParameters(),
                sell_policy="core_tactical",
                use_halving=True,
            ),
            "v0.4_cycle_trend": generate_cycle_trend_targets(features),
            "v0.5_momentum_vol45": primary_signals,
            "buy_hold": make_buy_hold_signals(features),
        }
        for strategy_id, signals in signal_map.items():
            simulation = simulate_strategy(
                features,
                signals,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                initial_capital=initial_capital_krw,
            )
            rows.append(
                {
                    "data_mode": mode,
                    "strategy_id": strategy_id,
                    **_metrics_row(strategy_id, simulation, initial_capital_krw),
                }
            )
    metadata = {
        "start": common.min().date().isoformat(),
        "end": common.max().date().isoformat(),
        "rows": int(len(common)),
    }
    return pd.DataFrame(rows), metadata


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{float(value):,.0f}원"


def build_report(
    candidates: pd.DataFrame,
    comparison: pd.DataFrame,
    cycles: pd.DataFrame,
    robustness: pd.DataFrame,
    horizons: pd.DataFrame,
    data_modes: pd.DataFrame,
    overlap: Mapping[str, Any],
    data_mode_metadata: Mapping[str, Any],
    manifest: Mapping[str, Any],
    current_state: Mapping[str, Any],
) -> str:
    primary = candidates.loc[
        candidates["candidate_id"] == "momentum_vol45_v05"
    ].iloc[0]
    lines = [
        "# BTC 모멘텀·변동성 전략 연구 v0.5",
        "",
        f"생성시각: {manifest['generated_at_utc']}",
        f"연구기간: {manifest['data_start']}~{manifest['data_end']}",
        "",
        "> 과거 데이터 연구 결과이며 자동주문·실전 승인 신호가 아닙니다.",
        "",
        "## 규칙",
        "",
        "1. 매주 일요일 1·3·6·12개월 수익률의 양수 개수를 센다.",
        "2. 양수 하나당 BTC 기본비중 25%를 부여한다.",
        "3. 63일 실현변동성으로 연 45% 위험예산 상한을 계산한다.",
        "4. 모멘텀 비중과 변동성 상한 중 작은 값을 목표비중으로 사용한다.",
        "5. 기존 비중과 10%p 이상 차이 날 때만 다음 일봉 시가에 조정한다.",
        "",
        "## 1,000만 원 결과",
        f"- 최종자산: {_fmt_krw(primary['terminal_wealth_krw'])}",
        f"- 순이익: {_fmt_krw(primary['profit_krw'])}",
        f"- 누적수익: {_fmt_pct(primary['total_return'])}",
        f"- CAGR / MDD: {_fmt_pct(primary['cagr'])} / {_fmt_pct(primary['mdd'])}",
        f"- 거래 횟수 / 평균 BTC 노출: {int(primary['trades'])} / {_fmt_pct(primary['exposure'])}",
        "",
        "## 전체기간 비교",
        "",
        "| 전략 | 최종자산 | CAGR | MDD | 거래 |",
        "|---|---:|---:|---:|---:|",
    ]
    for _, row in comparison.iterrows():
        lines.append(
            f"| {row['strategy_id']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 위험예산 인접값",
            "",
            "| 후보 | 목표변동성 | 최종자산 | CAGR | MDD |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in candidates.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {_fmt_pct(row['target_annual_volatility'])} | "
            f"{_fmt_krw(row['terminal_wealth_krw'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} |"
        )
    lines.extend(
        [
            "",
            "## 반감기 epoch별 v0.5",
            "",
            "| Epoch | 기간 | 누적수익 | CAGR | MDD | 평균노출 |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in cycles.iterrows():
        lines.append(
            f"| {int(row['halving_epoch'])} | {row['start']}~{row['end']} | "
            f"{_fmt_pct(row['total_return'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['exposure'])} |"
        )
    lines.extend(
        [
            "",
            "## 비용·체결지연",
            "",
            "| 조건 | 최종자산 | CAGR | MDD |",
            "|---|---:|---:|---:|",
        ]
    )
    for _, row in robustness.iterrows():
        lines.append(
            f"| {row['scenario']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} |"
        )
    lines.extend(
        [
            "",
            "## 모멘텀 기간 민감도",
            "",
            "| 기간 | CAGR | MDD |",
            "|---|---:|---:|",
        ]
    )
    for _, row in horizons.iterrows():
        lines.append(
            f"| {row['horizons_days']} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} |"
        )
    lines.extend(
        [
            "",
            "## 실제 Upbit 교차검증",
            f"- 비교기간: {data_mode_metadata['start']}~{data_mode_metadata['end']}",
            f"- 합성/Upbit 일간수익률 상관: {float(overlap['daily_return_correlation']):.4f}",
            "",
            "| 데이터 | 전략 | 최종자산 | CAGR | MDD |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for _, row in data_modes.iterrows():
        lines.append(
            f"| {row['data_mode']} | {row['strategy_id']} | "
            f"{_fmt_krw(row['terminal_wealth_krw'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} |"
        )
    lines.extend(
        [
            "",
            "## 최근 확정 주간판단",
            f"- 판단일: {current_state['date']}",
            f"- 목표 BTC 비중: {_fmt_pct(current_state['target_weight'])}",
            f"- 양수 모멘텀: {current_state['positive_horizons']}",
            f"- 63일 연환산 변동성: {_fmt_pct(current_state['realized_volatility'])}",
            f"- 근거: {current_state['reason']}",
            "",
            "## 제한",
            "- v0.4 실패를 본 뒤 설계했으므로 2016~현재는 순수 표본외 결과가 아닙니다.",
            "- 2016~Upbit 상장 전 구간은 BTC-USD와 전일 USD/KRW 합성가격입니다.",
            "- 수수료·슬리피지는 반영했지만 세금과 대기자금 이자는 제외했습니다.",
            "- 목표변동성 45%는 예상수익률 45%라는 뜻이 아닙니다.",
            "- 현재 반감기 epoch는 아직 끝나지 않았습니다.",
            "- 실전 승인, 웹·Telegram 연결, 자동주문은 모두 보류 상태입니다.",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def run_momentum_research(
    *,
    refresh: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    now_utc: datetime | None = None,
    start_date: str = DEFAULT_START_DATE,
    initial_capital_krw: float = DEFAULT_INITIAL_CAPITAL_KRW,
) -> dict[str, Any]:
    if initial_capital_krw <= 0:
        raise ValueError("Initial capital must be positive")
    now = now_utc or utc_now()
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow" or bool(
        btc_cfg.get("auto_order", False)
    ):
        raise BtcDataError("Momentum research requires shadow mode and no orders")

    phase1 = build_phase1_report(
        refresh=refresh, config_path=config_path, now_utc=now
    )
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 critical data gate is blocked")
    onchain, onchain_fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh,
        config=config,
        now_utc=now,
    )

    paths = resolve_paths(config)
    data_cfg = btc_cfg.get("data", {})
    runtime = btc_cfg.get("research_runtime", {})
    yahoo_cache = ROOT / config.get("settings", {}).get(
        "cache_dir", "data/cache"
    )
    usd = read_price_cache(
        yahoo_cache
        / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))
    )
    fx = read_price_cache(
        yahoo_cache / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))
    )
    usd = closed_yahoo_daily_frame(usd, now)
    fx = closed_yahoo_daily_frame(fx, now)
    synthetic = build_synthetic_krw_market(usd, fx)
    upbit = read_price_cache(
        paths.cache
        / f"upbit_{str(btc_cfg.get('execution_market', 'KRW-BTC')).replace('-', '_')}_daily.csv"
    )
    overlap = overlap_diagnostics(synthetic, upbit)

    onchain_lag_days = int(runtime.get("onchain_lag_days", 2))
    percentile_min_periods = int(runtime.get("percentile_min_periods", 730))
    synthetic_history = build_feature_frame(
        synthetic,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    upbit_history = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=onchain_lag_days,
        percentile_min_periods=percentile_min_periods,
    )
    params = MomentumVolatilityParameters()
    feature_history = add_momentum_volatility_features(synthetic_history, params)
    features = feature_history.loc[pd.Timestamp(start_date) :].copy()
    ready = features["momentum_feature_ready"].fillna(False)
    if not ready.any() or ready.idxmax() > pd.Timestamp(start_date) + pd.Timedelta(
        days=7
    ):
        raise BtcDataError("Momentum features cannot start near requested date")
    features = features.loc[ready.idxmax() :].copy()

    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    candidates, candidate_outputs = evaluate_volatility_candidates(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    primary_signals, primary_simulation = candidate_outputs[
        "momentum_vol45_v05"
    ]
    comparison = evaluate_full_period_comparison(
        features,
        primary_signals,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    cycles = cycle_metrics(features, primary_simulation)
    robustness = robustness_diagnostics(
        features,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    horizon_sensitivity = evaluate_horizon_sensitivity(
        synthetic_history,
        start_date=features.index.min().date().isoformat(),
        end_date=features.index.max().date().isoformat(),
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    data_modes, data_mode_metadata = data_mode_strategy_comparison(
        synthetic_history,
        upbit_history,
        initial_capital_krw=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )

    decision_rows = primary_signals.loc[primary_signals["is_decision_day"]]
    if decision_rows.empty:
        raise BtcDataError("Momentum strategy has no completed weekly decision")
    decision_date = decision_rows.index[-1]
    current_state = {
        "date": decision_date.date().isoformat(),
        **decision_rows.iloc[-1].to_dict(),
    }

    output_map = {
        "candidates": paths.output / "btc_momentum_v05_candidates.csv",
        "comparison": paths.output / "btc_momentum_v05_comparison.csv",
        "signals": paths.output / "btc_momentum_v05_signals.csv",
        "cycles": paths.output / "btc_momentum_v05_cycle_metrics.csv",
        "robustness": paths.output / "btc_momentum_v05_robustness.csv",
        "horizons": paths.output / "btc_momentum_v05_horizon_sensitivity.csv",
        "data_modes": paths.output / "btc_momentum_v05_data_modes.csv",
        "equity": paths.output / "btc_momentum_v05_equity.csv",
        "manifest": paths.output / "btc_momentum_v05_manifest.json",
        "report": paths.output / "btc_momentum_v05_report.md",
    }
    candidates.to_csv(output_map["candidates"], index=False, encoding="utf-8-sig")
    comparison.to_csv(output_map["comparison"], index=False, encoding="utf-8-sig")
    primary_signals.to_csv(output_map["signals"], encoding="utf-8-sig")
    cycles.to_csv(output_map["cycles"], index=False, encoding="utf-8-sig")
    robustness.to_csv(output_map["robustness"], index=False, encoding="utf-8-sig")
    horizon_sensitivity.to_csv(
        output_map["horizons"], index=False, encoding="utf-8-sig"
    )
    data_modes.to_csv(output_map["data_modes"], index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {
            candidate_id: simulation.daily["equity"]
            for candidate_id, (_, simulation) in candidate_outputs.items()
        }
    ).to_csv(output_map["equity"], encoding="utf-8-sig")

    manifest = {
        "schema_version": "btc-momentum-research-0.5",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "approved_strategy": None,
        "data_mode": "synthetic KRW from BTC-USD and prior-known USD/KRW",
        "data_start": features.index.min().date().isoformat(),
        "data_end": features.index.max().date().isoformat(),
        "initial_capital_krw": initial_capital_krw,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "execution_rule": "closed Sunday UTC signal, next daily open",
        "parameters": asdict(params),
        "research_origin": "designed after reviewing v0.3 and v0.4 failures",
        "selection_rule": "v0.5 primary frozen at 45% volatility for formal study",
        "onchain_used_for_signal": False,
        "halving_used_for_signal": False,
        "onchain_cache_fallback": onchain_fallback,
        "onchain_cache_error": onchain_error,
        "price_overlap": overlap,
        "data_mode_overlap": data_mode_metadata,
        "limitations": [
            "pre-Upbit prices are synthetic",
            "the strategy is not pristine out-of-sample",
            "tax and idle-cash interest are excluded",
            "the current halving epoch is incomplete",
            "no live advisory approval",
        ],
    }
    _write_json(output_map["manifest"], manifest)
    report = build_report(
        candidates,
        comparison,
        cycles,
        robustness,
        horizon_sensitivity,
        data_modes,
        overlap,
        data_mode_metadata,
        manifest,
        current_state,
    )
    output_map["report"].write_text(report, encoding="utf-8-sig")

    return {
        "manifest": manifest,
        "primary_metrics": performance_metrics(primary_simulation),
        "current_state": current_state,
        "candidates": candidates,
        "comparison": comparison,
        "cycles": cycles,
        "robustness": robustness,
        "horizons": horizon_sensitivity,
        "data_modes": data_modes,
        "outputs": {key: str(value) for key, value in output_map.items()},
    }


def print_summary(result: Mapping[str, Any]) -> None:
    metrics = result["primary_metrics"]
    capital = float(result["manifest"]["initial_capital_krw"])
    state = result["current_state"]
    print("BTC 모멘텀·변동성 v0.5 연구 결과")
    print(f"기간: {result['manifest']['data_start']}~{result['manifest']['data_end']}")
    print(
        f"초기 {_fmt_krw(capital)} -> {_fmt_krw(metrics['terminal_wealth'])}"
    )
    print(
        f"누적수익/CAGR/MDD: {_fmt_pct(metrics['total_return'])} / "
        f"{_fmt_pct(metrics['cagr'])} / {_fmt_pct(metrics['mdd'])}"
    )
    print(
        f"최근 확정 판단: {state['date']} / 목표 BTC 비중 "
        f"{_fmt_pct(state['target_weight'])}"
    )
    print("과거 연구·모의운영 전용이며 자동주문과 실전 승인은 없습니다.")
    print(f"보고서: {result['outputs']['report']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BTC multi-horizon momentum and volatility-budget research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument(
        "--initial-capital-krw",
        type=float,
        default=DEFAULT_INITIAL_CAPITAL_KRW,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_momentum_research(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
