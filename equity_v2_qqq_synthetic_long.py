from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from equity_v2_dca_engine import (
    ANNUAL_DEDUCTION_KRW,
    INITIAL_CAPITAL_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    DcaSimulator,
    StrategySpec,
    prepare_market,
    synthetic_tqqq_from_qqq,
    xirr,
)
from equity_v2_engine import load_market_data
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


STRATEGY_VERSION = "equity-v2-qqq-synthetic-long-0.1"
FIXED_DRAGS = (0.01, 0.025, 0.04)
ACTUAL_TQQQ_START = pd.Timestamp("2010-03-01")
START_SCENARIOS = {
    "qqq_early_history": pd.Timestamp("1999-03-10"),
    "dotcom_peak": pd.Timestamp("2000-03-10"),
    "pre_gfc_peak": pd.Timestamp("2007-10-09"),
    "tqqq_actual_start": ACTUAL_TQQQ_START,
}

BASELINE_SPECS = {
    "always_tqqq": StrategySpec("always_tqqq", "always_tqqq"),
    "initial_tqqq_monthly_qqq": StrategySpec(
        "always_qqq_contrib", "always_qqq"
    ),
    "monthly_50_50": StrategySpec("split_50_50", "split_50_50"),
    "optimized_conversion": StrategySpec(
        "convert_dd15_ma0_f100_s3_weekly_nm3",
        "qqq_convert",
        drawdown=-0.15,
        recovery_ma=None,
        conversion_fraction=1.0,
        conversion_stages=3,
        stage_frequency="weekly",
        switch_months=3,
    ),
}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_pct(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value):,.0f}원"


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_ready(dict(payload)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def constant_fx_frames(
    frames: Mapping[str, pd.DataFrame], value: float = 1000.0
) -> dict[str, pd.DataFrame]:
    output = dict(frames)
    fx = frames["QQQ"][["Open", "Close"]].copy()
    fx.loc[:, "Open"] = value
    fx.loc[:, "Close"] = value
    output["KRW=X"] = fx
    return output


def calibration_table(
    frames: Mapping[str, pd.DataFrame],
    *,
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, float]:
    qqq = frames["QQQ"][["Open", "Close"]].loc[ACTUAL_TQQQ_START:latest].dropna()
    actual = frames["TQQQ"][["Open", "Close"]].loc[ACTUAL_TQQQ_START:latest].dropna()
    index = qqq.index.intersection(actual.index)
    qqq = qqq.loc[index]
    actual = actual.loc[index]
    actual_normalized = actual["Close"] / float(actual["Close"].iloc[0])
    actual_returns = actual["Close"].pct_change().dropna()
    rows: list[dict[str, Any]] = []
    best_drag = 0.0
    best_error = float("inf")
    for drag in np.arange(0.0, 0.0801, 0.0005):
        synthetic = synthetic_tqqq_from_qqq(qqq, annual_drag=float(drag))
        synthetic_normalized = synthetic["Close"] / float(synthetic["Close"].iloc[0])
        aligned = actual_normalized.index.intersection(synthetic_normalized.index)
        terminal_ratio = float(
            synthetic_normalized.loc[aligned].iloc[-1]
            / actual_normalized.loc[aligned].iloc[-1]
        )
        log_error = abs(math.log(max(1e-12, terminal_ratio)))
        synthetic_returns = synthetic["Close"].pct_change().dropna()
        common_returns = actual_returns.index.intersection(synthetic_returns.index)
        rmse = float(
            np.sqrt(
                np.mean(
                    (
                        actual_returns.loc[common_returns].to_numpy()
                        - synthetic_returns.loc[common_returns].to_numpy()
                    )
                    ** 2
                )
            )
        )
        correlation = float(
            actual_returns.loc[common_returns].corr(
                synthetic_returns.loc[common_returns]
            )
        )
        rows.append(
            {
                "annual_drag": float(drag),
                "terminal_ratio_synthetic_to_actual": terminal_ratio,
                "log_terminal_error": log_error,
                "daily_return_rmse": rmse,
                "daily_return_correlation": correlation,
            }
        )
        if log_error < best_error:
            best_error = log_error
            best_drag = float(drag)
    return pd.DataFrame(rows), best_drag


def recovery_statistics(daily: pd.DataFrame) -> dict[str, Any]:
    if daily.empty:
        return {}
    nav = daily["nav"].astype(float)
    drawdown = nav / nav.cummax() - 1
    trough = pd.Timestamp(drawdown.idxmin())
    peak = pd.Timestamp(nav.loc[:trough].idxmax())
    peak_value = float(nav.loc[peak])
    recovered = nav.loc[trough:]
    recovered = recovered.loc[recovered >= peak_value]
    recovery_date = pd.Timestamp(recovered.index[0]) if not recovered.empty else None
    principal_ratio = daily["value_krw"] / daily["contributions_krw"]
    min_principal_date = pd.Timestamp(principal_ratio.idxmin())
    principal_recovery = principal_ratio.loc[min_principal_date:]
    principal_recovery = principal_recovery.loc[principal_recovery >= 1.0]
    principal_recovery_date = (
        pd.Timestamp(principal_recovery.index[0])
        if not principal_recovery.empty
        else None
    )
    return {
        "mdd_peak_date": peak.date().isoformat(),
        "mdd_trough_date": trough.date().isoformat(),
        "mdd_recovery_date": (
            recovery_date.date().isoformat() if recovery_date is not None else None
        ),
        "mdd_recovery_days": (
            int((recovery_date - peak).days) if recovery_date is not None else None
        ),
        "minimum_principal_date": min_principal_date.date().isoformat(),
        "principal_recovery_date": (
            principal_recovery_date.date().isoformat()
            if principal_recovery_date is not None
            else None
        ),
    }


def run_dca(
    market: pd.DataFrame,
    spec: StrategySpec,
    *,
    deduction: float = ANNUAL_DEDUCTION_KRW,
) -> dict[str, Any]:
    result = DcaSimulator(
        market,
        spec,
        initial_capital_krw=INITIAL_CAPITAL_KRW,
        monthly_contribution_krw=MONTHLY_CONTRIBUTION_KRW,
        tax_deduction_krw=deduction,
    ).run()
    return {**result.metrics, **recovery_statistics(result.daily)}


def simple_qqq_dca(market: pd.DataFrame) -> dict[str, Any]:
    # Initial and monthly contributions both purchase QQQ; no intermediate sale.
    units = 0.0
    basis = 0.0
    contributions = 0.0
    cashflows: list[tuple[pd.Timestamp, float]] = []
    nav_units = 0.0
    daily_rows: list[dict[str, Any]] = []
    months = pd.Series(market.index, index=market.index).groupby(
        market.index.to_period("M")
    )
    monthly_dates = {pd.Timestamp(group.index.min()) for _, group in months}
    first_date = pd.Timestamp(market.index[0])
    initial_month = first_date.to_period("M")
    for date, row in market.iterrows():
        date = pd.Timestamp(date)
        contribution = 0.0
        if date == first_date:
            contribution = INITIAL_CAPITAL_KRW
        elif date in monthly_dates and date.to_period("M") != initial_month:
            contribution = MONTHLY_CONTRIBUTION_KRW
        if contribution > 0:
            fx = float(row["fx_open"])
            price = float(row["qqq_open"])
            usd = contribution / (fx * 1.001)
            usd_net = usd / 1.001
            bought = usd_net / price
            value_before = units * price * fx
            unit_price = value_before / nav_units if nav_units > 0 else 1.0
            nav_units += contribution / unit_price
            units += bought
            basis += contribution
            contributions += contribution
            cashflows.append((date, -contribution))
        value = units * float(row["qqq_close"]) * float(row["fx_close"])
        daily_rows.append(
            {
                "Date": date,
                "value_krw": value,
                "contributions_krw": contributions,
                "nav": value / nav_units if nav_units > 0 else 0.0,
            }
        )
    daily = pd.DataFrame(daily_rows).set_index("Date")
    final_date = pd.Timestamp(daily.index[-1])
    final_row = market.iloc[-1]
    gross = units * float(final_row["qqq_close"]) * 0.999 * float(final_row["fx_close"]) * 0.999
    gain = gross - basis
    tax = max(0.0, gain - ANNUAL_DEDUCTION_KRW) * 0.22
    after_tax = gross - tax
    cashflows.append((final_date, after_tax))
    after_tax_xirr = xirr(cashflows)
    nav = daily["nav"]
    metrics = {
        "strategy_id": "initial_qqq_monthly_qqq",
        "total_contributions_krw": contributions,
        "pre_tax_value_krw": float(daily["value_krw"].iloc[-1]),
        "after_tax_liquidation_value_krw": after_tax,
        "after_tax_xirr": after_tax_xirr,
        "mdd": float((nav / nav.cummax() - 1).min()),
        "minimum_vs_contributions": float(
            (daily["value_krw"] / daily["contributions_krw"] - 1).min()
        ),
        "terminal_tax_krw": tax,
    }
    return {**metrics, **recovery_statistics(daily)}


def scenario_rows(
    frames: Mapping[str, pd.DataFrame],
    *,
    latest: pd.Timestamp,
    drags: list[float],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    constant_frames = constant_fx_frames(frames)
    for drag in drags:
        synthetic = synthetic_tqqq_from_qqq(
            constant_frames["QQQ"], annual_drag=drag
        )
        for start_name, requested_start in START_SCENARIOS.items():
            market = prepare_market(
                constant_frames,
                requested_start,
                latest,
                synthetic_tqqq=synthetic,
            )
            if len(market) < 500:
                continue
            for strategy_name, spec in BASELINE_SPECS.items():
                metrics = run_dca(market, spec)
                rows.append(
                    {
                        "model": "synthetic_qqq_3x",
                        "annual_drag": drag,
                        "start_scenario": start_name,
                        "requested_start": requested_start.date().isoformat(),
                        "actual_start": market.index[0].date().isoformat(),
                        "strategy": strategy_name,
                        **metrics,
                    }
                )
            qqq_metrics = simple_qqq_dca(market)
            rows.append(
                {
                    "model": "qqq_actual",
                    "annual_drag": drag,
                    "start_scenario": start_name,
                    "requested_start": requested_start.date().isoformat(),
                    "actual_start": market.index[0].date().isoformat(),
                    "strategy": "initial_qqq_monthly_qqq",
                    **qqq_metrics,
                }
            )
    return pd.DataFrame(rows)


def actual_validation_rows(
    frames: Mapping[str, pd.DataFrame], latest: pd.Timestamp
) -> pd.DataFrame:
    constant_frames = constant_fx_frames(frames)
    market = prepare_market(constant_frames, ACTUAL_TQQQ_START, latest)
    rows: list[dict[str, Any]] = []
    for name, spec in BASELINE_SPECS.items():
        rows.append(
            {
                "model": "actual_tqqq",
                "annual_drag": np.nan,
                "start_scenario": "tqqq_actual_start",
                "requested_start": ACTUAL_TQQQ_START.date().isoformat(),
                "actual_start": market.index[0].date().isoformat(),
                "strategy": name,
                **run_dca(market, spec),
            }
        )
    rows.append(
        {
            "model": "qqq_actual",
            "annual_drag": np.nan,
            "start_scenario": "tqqq_actual_start",
            "requested_start": ACTUAL_TQQQ_START.date().isoformat(),
            "actual_start": market.index[0].date().isoformat(),
            "strategy": "initial_qqq_monthly_qqq",
            **simple_qqq_dca(market),
        }
    )
    return pd.DataFrame(rows)


def build_report(
    manifest: Mapping[str, Any],
    results: pd.DataFrame,
    calibration: pd.DataFrame,
) -> str:
    calibrated = float(manifest["calibrated_annual_drag"])
    base = results.loc[
        (results["model"] == "synthetic_qqq_3x")
        & np.isclose(results["annual_drag"], calibrated)
        & (results["start_scenario"] == "qqq_early_history")
    ]
    starts = results.loc[
        (results["model"] == "synthetic_qqq_3x")
        & np.isclose(results["annual_drag"], calibrated)
        & (results["strategy"] == "always_tqqq")
    ]
    sensitivity = results.loc[
        (results["model"] == "synthetic_qqq_3x")
        & (results["start_scenario"] == "qqq_early_history")
        & (results["strategy"] == "always_tqqq")
    ]
    actual = results.loc[
        (results["model"] == "actual_tqqq")
        & (results["start_scenario"] == "tqqq_actual_start")
    ]
    lines = [
        "# QQQ 기반 합성 TQQQ 장기 백테스트",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- QQQ 데이터 종료: {manifest['data_end']}",
        f"- 실제 TQQQ 구간으로 보정한 고정 연간 드래그: {calibrated * 100:.2f}%",
        "- 합성식: QQQ 일수익률 3배 - 일할 연간 드래그",
        "- 환율은 1,000원으로 고정해 미국자산 자체의 장기경로만 비교",
        "- 초기자금 8,000만원 + 월 50만원 + 과세계좌 근사",
        "",
        "## QQQ 초기 역사부터 최신까지",
        "",
        "| 전략 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 | MDD 회복일 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for _, row in base.iterrows():
        lines.append(
            f"| {row['strategy']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} | "
            f"{row.get('mdd_recovery_date') or '미회복'} |"
        )
    lines.extend(
        [
            "",
            "## 항상 합성 TQQQ 적립의 시작시점 민감도",
            "",
            "| 시작 | 실제 계산 시작 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in starts.sort_values("actual_start").iterrows():
        lines.append(
            f"| {row['start_scenario']} | {row['actual_start']} | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 합성비용 민감도 — QQQ 초기 역사부터 항상 TQQQ",
            "",
            "| 연간 드래그 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in sensitivity.sort_values("annual_drag").iterrows():
        lines.append(
            f"| {row['annual_drag'] * 100:.2f}% | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 2010년 이후 실제 TQQQ 검증",
            "",
            "| 전략 | 세후 XIRR | MDD | 세후 최종액 |",
            "|---|---:|---:|---:|",
        ]
    )
    for _, row in actual.iterrows():
        lines.append(
            f"| {row['strategy']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    best = calibration.sort_values("log_terminal_error").iloc[0]
    lines.extend(
        [
            "",
            "## 합성모형 보정",
            "",
            f"- 실제 TQQQ와 종가 최종배수가 가장 가까운 드래그: {best['annual_drag'] * 100:.2f}%",
            f"- 합성/실제 최종배수 비율: {best['terminal_ratio_synthetic_to_actual']:.4f}",
            f"- 일수익률 상관계수: {best['daily_return_correlation']:.6f}",
            f"- 일수익률 RMSE: {best['daily_return_rmse']:.6f}",
            "",
            "## 주의",
            "",
            "- 2010년 이전 수치는 실제 TQQQ 거래가격이 아니라 QQQ로 만든 합성값이다.",
            "- 고정 드래그는 시기별 금리·스왑비용·추적오차 변화를 완전히 재현하지 못한다.",
            "- QQQ 데이터 초반 126거래일은 52주 고점 계산 준비기간 때문에 제외된다.",
            "- 합성결과는 장기 스트레스와 시작시점 민감도를 확인하는 용도이며 실제 수익을 보장하지 않는다.",
        ]
    )
    return "\n".join(lines)


def run(*, refresh: bool, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, metadata = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    latest = pd.Timestamp(frames["QQQ"]["Close"].dropna().index.max())
    calibration, calibrated_drag = calibration_table(frames, latest=latest)
    drags = sorted(set([*FIXED_DRAGS, round(calibrated_drag, 4)]))
    synthetic_results = scenario_rows(frames, latest=latest, drags=drags)
    actual_results = actual_validation_rows(frames, latest)
    results = pd.concat([synthetic_results, actual_results], ignore_index=True)

    outputs = {
        "results": paths.output / "equity_v2_qqq_synthetic_long_results.csv",
        "calibration": paths.output / "equity_v2_qqq_synthetic_calibration.csv",
        "manifest": paths.output / "equity_v2_qqq_synthetic_long_manifest.json",
        "report": paths.output / "equity_v2_qqq_synthetic_long_report.md",
    }
    results.to_csv(outputs["results"], index=False, encoding="utf-8-sig")
    calibration.to_csv(outputs["calibration"], index=False, encoding="utf-8-sig")
    manifest = {
        "schema_version": "equity-v2-qqq-synthetic-long-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "data_start": frames["QQQ"]["Close"].dropna().index.min().date().isoformat(),
        "data_end": latest.date().isoformat(),
        "calibrated_annual_drag": calibrated_drag,
        "fixed_drag_scenarios": drags,
        "start_scenarios": {
            key: value.date().isoformat() for key, value in START_SCENARIOS.items()
        },
        "data_metadata": metadata,
        "limitations": [
            "pre-2010 TQQQ is synthetic",
            "fixed annual drag is simplified",
            "constant FX is used for long-history comparison",
            "tax calculation is approximate",
            "no live approval",
        ],
    }
    _write_json(outputs["manifest"], manifest)
    outputs["report"].write_text(
        build_report(manifest, results, calibration), encoding="utf-8-sig"
    )
    return {
        "manifest": manifest,
        "outputs": {key: str(value) for key, value in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Full-history QQQ-based synthetic TQQQ study")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run(refresh=args.refresh, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
