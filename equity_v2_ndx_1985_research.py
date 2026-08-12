from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from equity_v2_dca_engine import (
    ANNUAL_DEDUCTION_KRW,
    COMMISSION_BPS,
    FX_SPREAD_BPS,
    INITIAL_CAPITAL_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    SLIPPAGE_BPS,
    TAX_RATE,
    DcaSimulator,
    StrategySpec,
    first_trading_days,
    xirr,
)
from equity_v2_qqq_synthetic_long import recovery_statistics
from quant_guardian import DEFAULT_CONFIG, load_config, read_price, resolve_paths


STRATEGY_VERSION = "equity-v2-ndx-1985-0.1"
NDX_REQUESTED_START = pd.Timestamp("1985-01-31")
ACTUAL_TQQQ_START = pd.Timestamp("2010-02-09")
TRADING_DAYS = 252
LEVERAGE = 3.0
CALIBRATION_GRID = np.arange(-0.06, 0.1201, 0.0005)
START_SCENARIOS = {
    "ndx_launch": pd.Timestamp("1985-01-31"),
    "pre_1987_crash": pd.Timestamp("1987-08-25"),
    "pre_1990_recession": pd.Timestamp("1990-07-16"),
    "dotcom_peak": pd.Timestamp("2000-03-10"),
    "pre_gfc_peak": pd.Timestamp("2007-10-09"),
    "tqqq_actual_start": ACTUAL_TQQQ_START,
}

BASELINE_SPECS = {
    "initial_3x_monthly_3x": StrategySpec("always_tqqq", "always_tqqq"),
    "initial_3x_monthly_1x": StrategySpec(
        "always_qqq_contrib", "always_qqq"
    ),
    "initial_3x_monthly_50_50": StrategySpec(
        "split_50_50", "split_50_50"
    ),
    "drawdown_conversion": StrategySpec(
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


def _clean_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame[["Open", "Close"]].copy().sort_index()
    output.index = pd.DatetimeIndex(output.index).tz_localize(None)
    output["Open"] = pd.to_numeric(output["Open"], errors="coerce")
    output["Close"] = pd.to_numeric(output["Close"], errors="coerce")
    return output.dropna(subset=["Open", "Close"])


def dynamic_synthetic_3x(
    ndx: pd.DataFrame,
    short_yield: pd.Series,
    *,
    residual_drag: float,
) -> pd.DataFrame:
    frame = _clean_ohlc(ndx)
    rates = (
        pd.to_numeric(short_yield, errors="coerce")
        .reindex(frame.index)
        .ffill()
        .bfill()
        .clip(lower=-1.0, upper=30.0)
        / 100.0
    )
    opens: list[float] = []
    closes: list[float] = []
    prior_index_close: float | None = None
    prior_synthetic_close = 100.0
    for date, row in frame.iterrows():
        index_open = float(row["Open"])
        index_close = float(row["Close"])
        if prior_index_close is None:
            synthetic_open = 100.0
            synthetic_close = 100.0
        else:
            overnight = index_open / prior_index_close - 1.0
            full_day = index_close / prior_index_close - 1.0
            annual_financing = (LEVERAGE - 1.0) * float(rates.loc[date])
            daily_drag = (annual_financing + residual_drag) / TRADING_DAYS
            synthetic_open = prior_synthetic_close * max(
                0.0001, 1.0 + LEVERAGE * overnight
            )
            synthetic_close = prior_synthetic_close * max(
                0.0001, 1.0 + LEVERAGE * full_day - daily_drag
            )
        opens.append(synthetic_open)
        closes.append(synthetic_close)
        prior_index_close = index_close
        prior_synthetic_close = synthetic_close
    return pd.DataFrame({"Open": opens, "Close": closes}, index=frame.index)


def calibrate_residual_drag(
    ndx: pd.DataFrame,
    short_yield: pd.Series,
    actual_tqqq: pd.DataFrame,
) -> tuple[pd.DataFrame, float]:
    ndx_cal = _clean_ohlc(ndx).loc[ACTUAL_TQQQ_START:]
    actual = _clean_ohlc(actual_tqqq).loc[ACTUAL_TQQQ_START:]
    index = ndx_cal.index.intersection(actual.index)
    ndx_cal = ndx_cal.loc[index]
    actual = actual.loc[index]
    actual_norm = actual["Close"] / float(actual["Close"].iloc[0])
    actual_returns = actual["Close"].pct_change().dropna()
    rows: list[dict[str, Any]] = []
    best_drag = 0.0
    best_error = float("inf")
    for residual in CALIBRATION_GRID:
        synthetic = dynamic_synthetic_3x(
            ndx_cal,
            short_yield,
            residual_drag=float(residual),
        )
        synthetic_norm = synthetic["Close"] / float(synthetic["Close"].iloc[0])
        common = actual_norm.index.intersection(synthetic_norm.index)
        terminal_ratio = float(
            synthetic_norm.loc[common].iloc[-1] / actual_norm.loc[common].iloc[-1]
        )
        log_error = abs(math.log(max(1e-12, terminal_ratio)))
        synthetic_returns = synthetic["Close"].pct_change().dropna()
        return_dates = actual_returns.index.intersection(synthetic_returns.index)
        rmse = float(
            np.sqrt(
                np.mean(
                    (
                        actual_returns.loc[return_dates].to_numpy()
                        - synthetic_returns.loc[return_dates].to_numpy()
                    )
                    ** 2
                )
            )
        )
        correlation = float(
            actual_returns.loc[return_dates].corr(
                synthetic_returns.loc[return_dates]
            )
        )
        rows.append(
            {
                "residual_drag": float(residual),
                "terminal_ratio_synthetic_to_actual": terminal_ratio,
                "log_terminal_error": log_error,
                "daily_return_rmse": rmse,
                "daily_return_correlation": correlation,
            }
        )
        if log_error < best_error:
            best_error = log_error
            best_drag = float(residual)
    return pd.DataFrame(rows), best_drag


def prepare_ndx_market(
    ndx: pd.DataFrame,
    synthetic_3x: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    ndx = _clean_ohlc(ndx)
    synthetic_3x = _clean_ohlc(synthetic_3x)
    index = ndx.loc[start:end].index.intersection(synthetic_3x.loc[start:end].index)
    output = pd.DataFrame(index=index)
    output["qqq_open"] = ndx.loc[index, "Open"]
    output["qqq_close"] = ndx.loc[index, "Close"]
    output["tqqq_open"] = synthetic_3x.loc[index, "Open"]
    output["tqqq_close"] = synthetic_3x.loc[index, "Close"]
    output["fx_open"] = 1000.0
    output["fx_close"] = 1000.0
    high = output["qqq_close"].rolling(252, min_periods=1).max()
    output["qqq_high_252"] = high
    output["qqq_drawdown"] = output["qqq_close"] / high - 1.0
    if len(output) >= 126:
        output.iloc[:126, output.columns.get_loc("qqq_drawdown")] = 0.0
    for window in (20, 50, 100):
        output[f"qqq_sma_{window}"] = output["qqq_close"].rolling(
            window, min_periods=window
        ).mean()
    return output


def run_dca(market: pd.DataFrame, spec: StrategySpec) -> dict[str, Any]:
    result = DcaSimulator(
        market,
        spec,
        initial_capital_krw=INITIAL_CAPITAL_KRW,
        monthly_contribution_krw=MONTHLY_CONTRIBUTION_KRW,
        tax_deduction_krw=ANNUAL_DEDUCTION_KRW,
    ).run()
    return {**result.metrics, **recovery_statistics(result.daily)}


def fixed_mix_dca(
    market: pd.DataFrame,
    *,
    initial_3x_weight: float,
    monthly_3x_weight: float,
) -> dict[str, Any]:
    trade_rate = (COMMISSION_BPS + SLIPPAGE_BPS) / 10_000
    fx_spread = FX_SPREAD_BPS / 10_000
    positions = {
        "3x": {"units": 0.0, "basis": 0.0},
        "1x": {"units": 0.0, "basis": 0.0},
    }
    contributions = 0.0
    nav_units = 0.0
    cashflows: list[tuple[pd.Timestamp, float]] = []
    rows: list[dict[str, Any]] = []
    monthly_dates = first_trading_days(market.index)
    first_date = pd.Timestamp(market.index[0])
    initial_month = first_date.to_period("M")

    def account_value(row: pd.Series, suffix: str) -> float:
        return (
            positions["3x"]["units"] * float(row[f"tqqq_{suffix}"])
            + positions["1x"]["units"] * float(row[f"qqq_{suffix}"])
        ) * float(row[f"fx_{suffix}"])

    def buy(asset: str, amount_krw: float, row: pd.Series) -> None:
        if amount_krw <= 0:
            return
        price_col = "tqqq_open" if asset == "3x" else "qqq_open"
        fx = float(row["fx_open"])
        price = float(row[price_col])
        usd = amount_krw / (fx * (1 + fx_spread))
        units = usd / (1 + trade_rate) / price
        positions[asset]["units"] += units
        positions[asset]["basis"] += amount_krw

    for date, row in market.iterrows():
        date = pd.Timestamp(date)
        contribution = 0.0
        weight = monthly_3x_weight
        if date == first_date:
            contribution = INITIAL_CAPITAL_KRW
            weight = initial_3x_weight
        elif date in monthly_dates and date.to_period("M") != initial_month:
            contribution = MONTHLY_CONTRIBUTION_KRW
        if contribution > 0:
            before = account_value(row, "open")
            unit_price = before / nav_units if nav_units > 0 else 1.0
            nav_units += contribution / unit_price
            buy("3x", contribution * weight, row)
            buy("1x", contribution * (1 - weight), row)
            contributions += contribution
            cashflows.append((date, -contribution))
        value = account_value(row, "close")
        rows.append(
            {
                "Date": date,
                "value_krw": value,
                "contributions_krw": contributions,
                "nav": value / nav_units if nav_units > 0 else 0.0,
            }
        )

    daily = pd.DataFrame(rows).set_index("Date")
    final_date = pd.Timestamp(daily.index[-1])
    final_row = market.iloc[-1]
    liquidation = 0.0
    basis = 0.0
    for asset in ("3x", "1x"):
        price_col = "tqqq_close" if asset == "3x" else "qqq_close"
        gross_usd = positions[asset]["units"] * float(final_row[price_col])
        proceeds = gross_usd * (1 - trade_rate) * float(final_row["fx_close"]) * (
            1 - fx_spread
        )
        liquidation += proceeds
        basis += positions[asset]["basis"]
    terminal_tax = max(0.0, liquidation - basis - ANNUAL_DEDUCTION_KRW) * TAX_RATE
    after_tax = liquidation - terminal_tax
    cashflows.append((final_date, after_tax))
    nav = daily["nav"]
    metrics = {
        "initial_3x_weight": initial_3x_weight,
        "monthly_3x_weight": monthly_3x_weight,
        "total_contributions_krw": contributions,
        "pre_tax_value_krw": float(daily["value_krw"].iloc[-1]),
        "after_tax_liquidation_value_krw": after_tax,
        "after_tax_xirr": xirr(cashflows),
        "mdd": float((nav / nav.cummax() - 1).min()),
        "minimum_vs_contributions": float(
            (daily["value_krw"] / daily["contributions_krw"] - 1).min()
        ),
        "terminal_tax_krw": terminal_tax,
    }
    return {**metrics, **recovery_statistics(daily)}


def scenario_table(
    ndx: pd.DataFrame,
    synthetic: pd.DataFrame,
    *,
    latest: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scenario, requested_start in START_SCENARIOS.items():
        market = prepare_ndx_market(
            ndx,
            synthetic,
            start=requested_start,
            end=latest,
        )
        if len(market) < 500:
            continue
        for name, spec in BASELINE_SPECS.items():
            rows.append(
                {
                    "start_scenario": scenario,
                    "requested_start": requested_start.date().isoformat(),
                    "actual_start": market.index[0].date().isoformat(),
                    "strategy": name,
                    **run_dca(market, spec),
                }
            )
        for weight in (0.0, 0.25, 0.50, 0.75, 1.0):
            rows.append(
                {
                    "start_scenario": scenario,
                    "requested_start": requested_start.date().isoformat(),
                    "actual_start": market.index[0].date().isoformat(),
                    "strategy": f"fixed_mix_{int(weight * 100)}pct_3x",
                    **fixed_mix_dca(
                        market,
                        initial_3x_weight=weight,
                        monthly_3x_weight=weight,
                    ),
                }
            )
    return pd.DataFrame(rows)


def actual_validation(
    ndx: pd.DataFrame,
    short_yield: pd.Series,
    actual_tqqq: pd.DataFrame,
    *,
    latest: pd.Timestamp,
    residual_drag: float,
) -> pd.DataFrame:
    synthetic = dynamic_synthetic_3x(
        ndx.loc[ACTUAL_TQQQ_START:latest],
        short_yield,
        residual_drag=residual_drag,
    )
    synthetic_market = prepare_ndx_market(
        ndx,
        synthetic,
        start=ACTUAL_TQQQ_START,
        end=latest,
    )
    actual = _clean_ohlc(actual_tqqq)
    actual_market = prepare_ndx_market(
        ndx,
        actual,
        start=ACTUAL_TQQQ_START,
        end=latest,
    )
    rows = []
    for model, market in (
        ("dynamic_synthetic", synthetic_market),
        ("actual_tqqq", actual_market),
    ):
        metrics = run_dca(market, BASELINE_SPECS["initial_3x_monthly_3x"])
        rows.append({"model": model, **metrics})
    return pd.DataFrame(rows)


def build_report(
    manifest: Mapping[str, Any],
    results: pd.DataFrame,
    validation: pd.DataFrame,
    sensitivity: pd.DataFrame,
) -> str:
    launch = results.loc[results["start_scenario"] == "ndx_launch"].copy()
    always = results.loc[
        results["strategy"] == "initial_3x_monthly_3x"
    ].sort_values("actual_start")
    mix = launch.loc[launch["strategy"].str.startswith("fixed_mix_")].sort_values(
        "initial_3x_weight"
    )
    other = launch.loc[
        launch["strategy"].isin(
            [
                "initial_3x_monthly_3x",
                "initial_3x_monthly_1x",
                "initial_3x_monthly_50_50",
                "drawdown_conversion",
            ]
        )
    ]
    lines = [
        "# Nasdaq-100 1985 기반 합성 TQQQ 장기 연구",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- Nasdaq-100 데이터: {manifest['ndx_data_start']}~{manifest['data_end']}",
        f"- 초기자금: {_fmt_krw(manifest['initial_capital_krw'])}",
        f"- 월 신규자금: {_fmt_krw(manifest['monthly_contribution_krw'])}",
        "- 환율 1,000원 고정, 과세계좌 근사",
        "- 실전·Telegram 미반영",
        "",
        "## 합성모형",
        "",
        "```text",
        "합성 3배 일수익률",
        "= Nasdaq-100 일수익률 × 3",
        "- 2배 단기금리 금융비용",
        "- 실제 TQQQ 구간으로 보정한 잔여 드래그",
        "```",
        "",
        f"- 보정 잔여 드래그: {manifest['calibrated_residual_drag'] * 100:.2f}%/년",
        f"- 2010년 이후 합성/실제 최종배수 비율: {manifest['calibration_terminal_ratio']:.4f}",
        f"- 일수익률 상관계수: {manifest['calibration_daily_correlation']:.6f}",
        "",
        "## 1985년 데이터 시작부터",
        "",
        "| 전략 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 | 고점 회복일 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for _, row in other.iterrows():
        lines.append(
            f"| {row['strategy']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} | "
            f"{row.get('mdd_recovery_date') or '미회복'} |"
        )
    lines.extend(
        [
            "",
            "## 초기자금과 월 적립금을 같은 비율로 나눈 경우",
            "",
            "| 합성 3배 | 1배 NDX | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in mix.iterrows():
        lines.append(
            f"| {row['initial_3x_weight'] * 100:.0f}% | "
            f"{(1 - row['initial_3x_weight']) * 100:.0f}% | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 항상 합성 3배 적립의 시작시점 민감도",
            "",
            "| 시작시나리오 | 실제 시작 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in always.iterrows():
        lines.append(
            f"| {row['start_scenario']} | {row['actual_start']} | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 2010년 이후 실제 TQQQ 검증",
            "",
            "| 모델 | 세후 XIRR | MDD | 세후 최종액 |",
            "|---|---:|---:|---:|",
        ]
    )
    for _, row in validation.iterrows():
        lines.append(
            f"| {row['model']} | {_fmt_pct(row['after_tax_xirr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 잔여 드래그 민감도 — 1985년부터 항상 3배",
            "",
            "| 보정 대비 잔여 드래그 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in sensitivity.sort_values("residual_offset") .iterrows():
        lines.append(
            f"| {row['residual_offset'] * 100:+.1f}%p | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )
    lines.extend(
        [
            "",
            "## 주의",
            "",
            "- 2010년 이전 3배 시계열은 실제 TQQQ가 아니라 Nasdaq-100 가격지수로 만든 합성값이다.",
            "- Nasdaq-100 가격지수는 배당을 포함한 총수익지수가 아니며, 합성모형 보정이 그 차이 일부를 흡수한다.",
            "- 단기금리는 ^IRX를 사용했고 잔여 드래그는 실제 TQQQ 2010년 이후 경로로 보정했다.",
            "- 세금은 평균취득가·최종청산을 사용하는 전략 비교용 근사다.",
            "- 과거 결과는 미래수익을 보장하지 않는다.",
        ]
    )
    return "\n".join(lines)


def run(*, refresh: bool, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    ndx = read_price("^NDX", paths, refresh=refresh)
    short_rate = read_price("^IRX", paths, refresh=refresh)["Close"]
    actual_tqqq = read_price("TQQQ", paths, refresh=refresh)
    latest = min(
        pd.Timestamp(ndx["Close"].dropna().index.max()),
        pd.Timestamp(actual_tqqq["Close"].dropna().index.max()),
    )
    calibration, residual = calibrate_residual_drag(ndx, short_rate, actual_tqqq)
    best = calibration.sort_values("log_terminal_error").iloc[0]
    synthetic = dynamic_synthetic_3x(
        ndx.loc[:latest],
        short_rate,
        residual_drag=residual,
    )
    results = scenario_table(ndx, synthetic, latest=latest)
    validation = actual_validation(
        ndx,
        short_rate,
        actual_tqqq,
        latest=latest,
        residual_drag=residual,
    )
    sensitivity_rows: list[dict[str, Any]] = []
    for offset in (-0.02, 0.0, 0.02):
        model = dynamic_synthetic_3x(
            ndx.loc[:latest],
            short_rate,
            residual_drag=residual + offset,
        )
        market = prepare_ndx_market(
            ndx,
            model,
            start=NDX_REQUESTED_START,
            end=latest,
        )
        metrics = run_dca(market, BASELINE_SPECS["initial_3x_monthly_3x"])
        sensitivity_rows.append(
            {
                "residual_offset": offset,
                "residual_drag": residual + offset,
                **metrics,
            }
        )
    sensitivity = pd.DataFrame(sensitivity_rows)

    outputs = {
        "results": paths.output / "equity_v2_ndx_1985_results.csv",
        "calibration": paths.output / "equity_v2_ndx_1985_calibration.csv",
        "validation": paths.output / "equity_v2_ndx_1985_validation.csv",
        "sensitivity": paths.output / "equity_v2_ndx_1985_sensitivity.csv",
        "manifest": paths.output / "equity_v2_ndx_1985_manifest.json",
        "report": paths.output / "equity_v2_ndx_1985_report.md",
    }
    results.to_csv(outputs["results"], index=False, encoding="utf-8-sig")
    calibration.to_csv(outputs["calibration"], index=False, encoding="utf-8-sig")
    validation.to_csv(outputs["validation"], index=False, encoding="utf-8-sig")
    sensitivity.to_csv(outputs["sensitivity"], index=False, encoding="utf-8-sig")
    manifest = {
        "schema_version": "equity-v2-ndx-1985-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "ndx_requested_start": NDX_REQUESTED_START.date().isoformat(),
        "ndx_data_start": _clean_ohlc(ndx).index.min().date().isoformat(),
        "data_end": latest.date().isoformat(),
        "calibrated_residual_drag": residual,
        "calibration_terminal_ratio": float(
            best["terminal_ratio_synthetic_to_actual"]
        ),
        "calibration_daily_correlation": float(
            best["daily_return_correlation"]
        ),
        "calibration_daily_rmse": float(best["daily_return_rmse"]),
        "limitations": [
            "pre-2010 3x series is synthetic",
            "NDX is a price index rather than a total-return index",
            "short-rate financing is approximated with IRX",
            "tax accounting is approximate",
            "constant FX is used",
            "no live approval",
        ],
    }
    _write_json(outputs["manifest"], manifest)
    outputs["report"].write_text(
        build_report(manifest, results, validation, sensitivity),
        encoding="utf-8-sig",
    )
    return {
        "manifest": manifest,
        "outputs": {key: str(value) for key, value in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nasdaq-100 1985 synthetic TQQQ research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run(refresh=args.refresh, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
