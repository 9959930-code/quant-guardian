from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from equity_v2_engine import (
    Candidate,
    FeatureStore,
    SimulationResult,
    close_panel,
    generate_targets,
    load_market_data,
    simulate_next_open,
)
from equity_v2_research import DEFAULT_COST_BPS, DEFAULT_SLIPPAGE_BPS
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


STRATEGY_VERSION = "equity-v2-two-strategy-blend-0.1"
START_DATE = pd.Timestamp("2010-03-01")
WEIGHTS = (0.0, 0.25, 0.50, 0.75, 1.0)

AGGRESSIVE = Candidate(
    candidate_id="aggressive_tqqq_pullback",
    family="pullback_hold",
    track="tqqq_actual",
    params={
        "asset": "TQQQ",
        "drawdown": -0.05,
        "entry_parts": 1,
        "exit_parts": 1,
        "exit_rule": "trailing",
        "frequency": "monthly",
        "recovery_sma": 20,
        "signal_mode": "underlying",
        "slope_days": 20,
        "trailing_stop": -0.20,
    },
)

BALANCED = Candidate(
    candidate_id="balanced_tqqq_breakout",
    family="breakout_hold",
    track="tqqq_actual",
    params={
        "asset": "TQQQ",
        "breakout_days": 20,
        "entry_parts": 1,
        "exit_parts": 1,
        "exit_rule": "sma",
        "exit_sma": 200,
        "frequency": "monthly",
        "recovery_sma": 100,
        "require_long_trend": True,
        "signal_mode": "underlying",
    },
)


@dataclass(frozen=True)
class BlendResult:
    label: str
    aggressive_weight: float
    rebalance: str
    equity: pd.Series
    aggressive_capital: pd.Series
    balanced_capital: pd.Series
    tqqq_exposure: pd.Series


def _metrics(equity: pd.Series, exposure: pd.Series) -> dict[str, Any]:
    equity = equity.dropna()
    exposure = exposure.reindex(equity.index).fillna(0.0)
    daily = equity.pct_change().dropna()
    elapsed_days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = elapsed_days / 365.25
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    drawdown = equity / equity.cummax() - 1
    trough_date = pd.Timestamp(drawdown.idxmin())
    peak_date = pd.Timestamp(equity.loc[:trough_date].idxmax())
    peak_value = float(equity.loc[peak_date])
    trough_value = float(equity.loc[trough_date])
    recovery_candidates = equity.loc[trough_date:]
    recovery_candidates = recovery_candidates.loc[recovery_candidates >= peak_value]
    recovery_date = (
        pd.Timestamp(recovery_candidates.index[0])
        if not recovery_candidates.empty
        else None
    )
    vol = float(daily.std(ddof=0) * math.sqrt(252))
    sharpe = float(daily.mean() * 252 / vol) if vol > 0 else np.nan
    downside = float(daily[daily < 0].std(ddof=0) * math.sqrt(252))
    sortino = float(daily.mean() * 252 / downside) if downside > 0 else np.nan
    mdd = float(drawdown.min())
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    year_end = equity.resample("YE").last()
    annual = year_end.pct_change()
    first_year = equity.index[0].year
    first_values = equity.loc[equity.index.year == first_year]
    if not first_values.empty:
        annual.iloc[0] = year_end.iloc[0] / first_values.iloc[0] - 1
    worst_year_date = annual.idxmin()
    return {
        "total_return": total_return,
        "capital_multiple": 1 + total_return,
        "cagr": cagr,
        "mdd": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "average_tqqq_exposure": float(exposure.mean()),
        "maximum_tqqq_exposure": float(exposure.max()),
        "mdd_peak_date": peak_date.date().isoformat(),
        "mdd_trough_date": trough_date.date().isoformat(),
        "mdd_recovery_date": (
            recovery_date.date().isoformat() if recovery_date is not None else None
        ),
        "mdd_recovery_days": (
            int((recovery_date - peak_date).days)
            if recovery_date is not None
            else None
        ),
        "worst_calendar_year": int(worst_year_date.year),
        "worst_calendar_return": float(annual.loc[worst_year_date]),
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
    }


def _period_metrics(
    result: BlendResult, start: str, end: str | pd.Timestamp
) -> dict[str, Any]:
    equity = result.equity.loc[pd.Timestamp(start):pd.Timestamp(end)]
    exposure = result.tqqq_exposure.reindex(equity.index)
    if len(equity) < 2:
        return {}
    normalized = equity / float(equity.iloc[0])
    return _metrics(normalized, exposure)


def _align_simulations(
    aggressive: SimulationResult, balanced: SimulationResult
) -> tuple[pd.DataFrame, pd.DataFrame]:
    index = aggressive.daily.index.intersection(balanced.daily.index)
    index = index[index >= START_DATE]
    if len(index) < 500:
        raise RuntimeError("The common TQQQ history is too short")
    equity = pd.DataFrame(
        {
            "aggressive": aggressive.daily.loc[index, "equity"],
            "balanced": balanced.daily.loc[index, "equity"],
        },
        index=index,
    )
    equity = equity.div(equity.iloc[0])
    exposure = pd.DataFrame(
        {
            "aggressive": aggressive.daily.loc[index, "leveraged_weight"],
            "balanced": balanced.daily.loc[index, "leveraged_weight"],
        },
        index=index,
    ).fillna(0.0)
    return equity, exposure


def no_rebalance_blend(
    equity: pd.DataFrame,
    exposure: pd.DataFrame,
    aggressive_weight: float,
) -> BlendResult:
    a = aggressive_weight * equity["aggressive"]
    b = (1 - aggressive_weight) * equity["balanced"]
    total = a + b
    tqqq = (
        a * exposure["aggressive"] + b * exposure["balanced"]
    ) / total.replace(0, np.nan)
    label = f"no_rebalance_aggressive_{int(round(aggressive_weight * 100))}"
    return BlendResult(label, aggressive_weight, "none", total, a, b, tqqq.fillna(0.0))


def annual_rebalance_blend(
    equity: pd.DataFrame,
    exposure: pd.DataFrame,
    aggressive_weight: float,
) -> BlendResult:
    returns = equity.pct_change().fillna(0.0)
    a_value = aggressive_weight
    b_value = 1 - aggressive_weight
    previous_year = equity.index[0].year
    a_rows: list[float] = []
    b_rows: list[float] = []
    exposure_rows: list[float] = []
    for date in equity.index:
        if date.year != previous_year:
            total = a_value + b_value
            a_value = total * aggressive_weight
            b_value = total * (1 - aggressive_weight)
            previous_year = date.year
        a_value *= 1 + float(returns.loc[date, "aggressive"])
        b_value *= 1 + float(returns.loc[date, "balanced"])
        total = a_value + b_value
        a_rows.append(a_value)
        b_rows.append(b_value)
        exposure_rows.append(
            (
                a_value * float(exposure.loc[date, "aggressive"])
                + b_value * float(exposure.loc[date, "balanced"])
            )
            / total
            if total > 0
            else 0.0
        )
    a = pd.Series(a_rows, index=equity.index)
    b = pd.Series(b_rows, index=equity.index)
    total = a + b
    label = f"annual_rebalance_aggressive_{int(round(aggressive_weight * 100))}"
    return BlendResult(
        label,
        aggressive_weight,
        "annual",
        total,
        a,
        b,
        pd.Series(exposure_rows, index=equity.index),
    )


def build_report(rows: pd.DataFrame, correlation: float) -> str:
    lines = [
        "# Equity v2 두 TQQQ 전략 혼합 연구",
        "",
        "- 공격형: QQQ 장기상승 중 5% 눌림·20일선 회복 후 TQQQ 매수, 월말 20% 추적청산",
        "- 균형형: QQQ 월말 20일 고점 돌파·200일선 위에서 TQQQ 매수, 월말 200일선 이탈 청산",
        "- 두 전략 모두 신호 다음 거래일 시가 체결, 수수료 5bp + 슬리피지 5bp",
        f"- 두 전략 일간 수익률 상관계수: {correlation:.3f}",
        "",
        "## 초기 자금만 나누고 전략 간 재조정하지 않는 경우",
        "",
        "| 공격형 비중 | 균형형 비중 | CAGR | MDD | Calmar | 최악 연도 | 평균 TQQQ 노출 | 최종 자산배수 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    subset = rows.loc[rows["rebalance"] == "none"]
    for _, row in subset.iterrows():
        lines.append(
            f"| {row['aggressive_weight'] * 100:.0f}% | "
            f"{(1-row['aggressive_weight']) * 100:.0f}% | "
            f"{row['cagr'] * 100:.2f}% | {row['mdd'] * 100:.2f}% | "
            f"{row['calmar']:.3f} | {int(row['worst_calendar_year'])} "
            f"({row['worst_calendar_return'] * 100:.2f}%) | "
            f"{row['average_tqqq_exposure'] * 100:.2f}% | "
            f"{row['capital_multiple']:.2f}배 |"
        )
    lines.extend(
        [
            "",
            "## 매년 첫 거래일에 원래 비율로 되돌리는 경우",
            "",
            "| 공격형 비중 | 균형형 비중 | CAGR | MDD | Calmar | 평균 TQQQ 노출 | 최종 자산배수 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    subset = rows.loc[rows["rebalance"] == "annual"]
    for _, row in subset.iterrows():
        lines.append(
            f"| {row['aggressive_weight'] * 100:.0f}% | "
            f"{(1-row['aggressive_weight']) * 100:.0f}% | "
            f"{row['cagr'] * 100:.2f}% | {row['mdd'] * 100:.2f}% | "
            f"{row['calmar']:.3f} | "
            f"{row['average_tqqq_exposure'] * 100:.2f}% | "
            f"{row['capital_multiple']:.2f}배 |"
        )
    lines.extend(
        [
            "",
            "## 해석",
            "",
            "- 두 전략은 자산분산이 아니라 같은 TQQQ의 진입·청산 규칙을 나누는 타이밍 분산이다.",
            "- 둘 다 투자 중일 때 전체 계좌는 사실상 TQQQ 100%이므로 레버리지 상품 고유위험은 남는다.",
            "- 공격형의 수익이 누적되면 무리밸런싱 계좌에서 공격형 비중이 자연스럽게 커진다.",
            "- 연 1회 리밸런싱은 공격형 쏠림을 막지만, 과거에는 최종자산을 다소 낮출 수 있다.",
            "- 최종 채택 전 25/75·50/50·75/25를 비용·지연·합성 2008 스트레스와 함께 비교해야 한다.",
        ]
    )
    return "\n".join(lines)


def run(
    *,
    refresh: bool,
    config_path: Path,
    cost_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, metadata = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    store = FeatureStore(close_panel(frames))
    latest = pd.Timestamp(frames["SPY"]["Close"].dropna().index.max())

    simulations: dict[str, SimulationResult] = {}
    for candidate in (AGGRESSIVE, BALANCED):
        targets = generate_targets(
            candidate,
            frames=frames,
            store=store,
            start=START_DATE,
            end=latest,
        )
        simulations[candidate.candidate_id] = simulate_next_open(
            targets=targets,
            frames=frames,
            start=START_DATE,
            end=latest,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        )

    equity, exposure = _align_simulations(
        simulations[AGGRESSIVE.candidate_id], simulations[BALANCED.candidate_id]
    )
    daily_correlation = float(
        equity.pct_change().dropna()["aggressive"].corr(
            equity.pct_change().dropna()["balanced"]
        )
    )

    results: list[BlendResult] = []
    for weight in WEIGHTS:
        results.append(no_rebalance_blend(equity, exposure, weight))
    for weight in (0.25, 0.50, 0.75):
        results.append(annual_rebalance_blend(equity, exposure, weight))

    rows: list[dict[str, Any]] = []
    curves = pd.DataFrame(index=equity.index)
    for result in results:
        metrics = _metrics(result.equity, result.tqqq_exposure)
        validation = _period_metrics(result, "2019-01-01", "2022-12-31")
        holdout = _period_metrics(result, "2023-01-01", latest)
        rows.append(
            {
                "label": result.label,
                "aggressive_weight": result.aggressive_weight,
                "balanced_weight": 1 - result.aggressive_weight,
                "rebalance": result.rebalance,
                **metrics,
                **{f"validation_{key}": value for key, value in validation.items()},
                **{f"holdout_{key}": value for key, value in holdout.items()},
            }
        )
        curves[result.label] = result.equity
    table = pd.DataFrame(rows)

    csv_path = paths.output / "equity_v2_blend_results.csv"
    curve_path = paths.output / "equity_v2_blend_curves.csv"
    report_path = paths.output / "equity_v2_blend_report.md"
    manifest_path = paths.output / "equity_v2_blend_manifest.json"
    table.to_csv(csv_path, index=False, encoding="utf-8-sig")
    curves.to_csv(curve_path, encoding="utf-8-sig")
    report_path.write_text(
        build_report(table, daily_correlation), encoding="utf-8-sig"
    )
    manifest = {
        "schema_version": "equity-v2-two-strategy-blend-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "daily_return_correlation": daily_correlation,
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
        "data_metadata": metadata,
        "outputs": {
            "results": str(csv_path),
            "curves": str(curve_path),
            "report": str(report_path),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"manifest": manifest, "results": table.to_dict(orient="records")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Blend the two selected Equity v2 TQQQ strategies")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    args = parser.parse_args()
    result = run(
        refresh=args.refresh,
        config_path=args.config,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
    )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
