from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

import equity_v2_rebalance_policy_research as base
from equity_v2_modern_engine import ALL_ASSETS, TaxableSimulation, first_trading_days
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


STRATEGY_VERSION = "equity-v2-annual-band-0.1"
DEV_XIRR_TOLERANCE = 0.0025
MDD_TOLERANCE = 0.02


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


class AnnualBandTaxableSimulator(base.PolicyTaxableSimulator):
    """Adds annual-only band checks while preserving the policy simulator rules."""

    def run(self) -> TaxableSimulation:
        index = self._build_index()
        if len(index) < 250:
            raise ValueError("taxable simulation period is too short")
        monthly_dates = first_trading_days(index)
        month_map = base._first_trading_days_by_month(index)
        first_date = pd.Timestamp(index[0])
        initial_month = first_date.to_period("M")
        daily_rows: list[dict[str, Any]] = []
        previous_year = first_date.year

        self._contribute_with_allocations(
            first_date,
            self.initial,
            self.target_weights,
            "initial_allocation",
        )
        self.nav_units = self.initial

        for date in index:
            date = pd.Timestamp(date)
            if date.year != previous_year:
                self._pay_tax(date, previous_year)
                previous_year = date.year

            is_monthly = date in monthly_dates
            is_initial_month = date.to_period("M") == initial_month
            is_policy_month = (
                self.policy.rebalance_month is not None
                and month_map.get((date.year, self.policy.rebalance_month)) == date
            )

            if (
                self.policy.legacy_order
                and self.policy.rebalance_mode == "annual_exact"
                and is_policy_month
            ):
                self._execute_rebalance(date, "annual_exact_legacy")

            if is_monthly and not is_initial_month:
                before = self._value_krw(date, "Open")
                unit_price = before / self.nav_units if self.nav_units > 0 else 1.0
                self.nav_units += self.monthly / unit_price
                self._monthly_contribution(date, self.monthly)

            monthly_breach = (
                is_monthly
                and self.policy.rebalance_mode == "monthly_band"
                and self._band_breached(date)
            )
            annual_breach = (
                is_policy_month
                and self.policy.rebalance_mode == "annual_band"
                and self._band_breached(date)
            )

            if is_monthly:
                self._record_weights(
                    date,
                    "post_contribution_pre_rebalance",
                    band_breach=monthly_breach or annual_breach,
                )

            if monthly_breach:
                self._execute_rebalance(date, "monthly_band_rebalance")
            elif annual_breach:
                self._execute_rebalance(date, "annual_band_rebalance")

            if (
                not self.policy.legacy_order
                and self.policy.rebalance_mode == "annual_exact"
                and is_policy_month
            ):
                self._execute_rebalance(date, "annual_exact_deficit_first")

            if is_monthly:
                self._record_weights(date, "post_policy")

            value = self._value_krw(date, "Close")
            nav = value / self.nav_units if self.nav_units > 0 else 0.0
            daily_rows.append(
                {
                    "Date": date,
                    "value_krw": value,
                    "nav": nav,
                    "contributions_krw": self.total_contributions,
                    "profit_vs_contributions": value - self.total_contributions,
                }
            )
        return self._finalize(index, daily_rows)


def policy_grid(rebalance_month: int = 1) -> list[base.PolicySpec]:
    month_name = next(
        name for name, month in base.REBALANCE_MONTHS.items()
        if month == rebalance_month
    )
    rows = [
        base.PolicySpec(
            policy_id=f"legacy_exact_{month_name}",
            contribution_mode="fixed_target",
            rebalance_mode="annual_exact",
            rebalance_month=rebalance_month,
            legacy_order=True,
        ),
        base.PolicySpec(
            policy_id=f"deficit_exact_{month_name}",
            contribution_mode="deficit_first",
            rebalance_mode="annual_exact",
            rebalance_month=rebalance_month,
        ),
    ]
    for band in (0.05, 0.10, 0.15):
        band_pp = int(round(band * 100))
        rows.append(
            base.PolicySpec(
                policy_id=f"deficit_annual_band_{band_pp:02d}pp_{month_name}",
                contribution_mode="deficit_first",
                rebalance_mode="annual_band",
                rebalance_month=rebalance_month,
                band=band,
            )
        )
    for band in (0.05, 0.10, 0.15):
        band_pp = int(round(band * 100))
        rows.append(
            base.PolicySpec(
                policy_id=f"deficit_monthly_band_{band_pp:02d}pp",
                contribution_mode="deficit_first",
                rebalance_mode="monthly_band",
                band=band,
            )
        )
    rows.append(
        base.PolicySpec(
            policy_id="deficit_contribution_only",
            contribution_mode="deficit_first",
            rebalance_mode="none",
        )
    )
    return rows


def run_period(
    *,
    portfolio: Mapping[str, float],
    policy: base.PolicySpec,
    frames: Mapping[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[dict[str, Any], AnnualBandTaxableSimulator, TaxableSimulation]:
    actual_start = base.valid_start(frames, pd.Timestamp(start))
    actual_end = min(
        pd.Timestamp(end),
        min(
            pd.Timestamp(frames[asset]["Close"].dropna().index.max())
            for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
        ),
    )
    simulator = AnnualBandTaxableSimulator(
        frames=frames,
        target_weights=portfolio,
        policy=policy,
        start=actual_start,
        end=actual_end,
    )
    result = simulator.run()
    return result.metrics, simulator, result


def evaluate_policy(
    *,
    portfolio_id: str,
    portfolio: Mapping[str, float],
    policy: base.PolicySpec,
    frames: Mapping[str, pd.DataFrame],
    latest: pd.Timestamp,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "weights_json": json.dumps(dict(portfolio), sort_keys=True),
        **policy.record(),
    }
    full, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=base.FIXED_START,
        end=latest,
    )
    row.update({f"full_{key}": value for key, value in full.items()})

    fold_xirrs: list[float] = []
    fold_mdds: list[float] = []
    for prefix, (fold_start, fold_end) in base.DEV_FOLDS.items():
        metrics, _, _ = run_period(
            portfolio=portfolio,
            policy=policy,
            frames=frames,
            start=fold_start,
            end=fold_end,
        )
        row.update({f"{prefix}_{key}": value for key, value in metrics.items()})
        fold_xirrs.append(float(metrics["after_tax_xirr"]))
        fold_mdds.append(float(metrics["mdd"]))

    holdout, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=base.HOLDOUT_START,
        end=latest,
    )
    row.update({f"holdout_{key}": value for key, value in holdout.items()})

    actual, _, _ = run_period(
        portfolio=portfolio,
        policy=policy,
        frames=frames,
        start=base.ACTUAL_TQQQ_START,
        end=latest,
    )
    row.update({f"actual_tqqq_{key}": value for key, value in actual.items()})

    dev_mean = float(np.mean(fold_xirrs))
    dev_min = float(np.min(fold_xirrs))
    dev_worst_mdd = float(np.min(fold_mdds))
    row.update(
        {
            "dev_mean_after_tax_xirr": dev_mean,
            "dev_min_after_tax_xirr": dev_min,
            "dev_worst_mdd": dev_worst_mdd,
        }
    )
    return row


def choose_tax_aware_policy(frame: pd.DataFrame, portfolio_id: str) -> pd.Series:
    candidates = frame.loc[
        (frame["portfolio_id"] == portfolio_id)
        & (frame["contribution_mode"] == "deficit_first")
    ].copy()
    if candidates.empty:
        raise ValueError(f"no tax-aware policies for {portfolio_id}")

    best_min = float(candidates["dev_min_after_tax_xirr"].max())
    candidates = candidates.loc[
        candidates["dev_min_after_tax_xirr"] >= best_min - DEV_XIRR_TOLERANCE
    ].copy()

    best_mean = float(candidates["dev_mean_after_tax_xirr"].max())
    candidates = candidates.loc[
        candidates["dev_mean_after_tax_xirr"] >= best_mean - DEV_XIRR_TOLERANCE
    ].copy()

    best_mdd = float(candidates["dev_worst_mdd"].max())
    mdd_eligible = candidates.loc[
        candidates["dev_worst_mdd"] >= best_mdd - MDD_TOLERANCE
    ].copy()
    if not mdd_eligible.empty:
        candidates = mdd_eligible

    return candidates.sort_values(
        [
            "full_policy_sell_count",
            "full_policy_sell_notional_krw",
            "dev_min_after_tax_xirr",
            "dev_mean_after_tax_xirr",
            "dev_worst_mdd",
        ],
        ascending=[True, True, False, False, False],
    ).iloc[0]


def selected_details(
    *,
    selections: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tax_rows: list[dict[str, Any]] = []
    trade_rows: list[pd.DataFrame] = []
    weight_rows: list[pd.DataFrame] = []
    for _, selected in selections.iterrows():
        portfolio_id = str(selected["portfolio_id"])
        policy = base.PolicySpec(
            policy_id=str(selected["policy_id"]),
            contribution_mode=str(selected["contribution_mode"]),
            rebalance_mode=str(selected["rebalance_mode"]),
            rebalance_month=(
                int(selected["rebalance_month"])
                if pd.notna(selected["rebalance_month"])
                else None
            ),
            band=float(selected["band"]) if pd.notna(selected["band"]) else None,
            legacy_order=bool(selected["legacy_order"]),
        )
        _, simulator, result = run_period(
            portfolio=base.PORTFOLIOS[portfolio_id],
            policy=policy,
            frames=frames,
            start=base.FIXED_START,
            end=latest,
        )
        for year, gain in sorted(simulator.realized_by_year.items()):
            tax_rows.append(
                {
                    "portfolio_id": portfolio_id,
                    "policy_id": policy.policy_id,
                    "year": year,
                    "realized_gain_krw": gain,
                    "estimated_tax_before_payment_timing_krw": max(
                        0.0, gain - base.ANNUAL_DEDUCTION_KRW
                    )
                    * base.TAX_RATE,
                }
            )
        if not result.trades.empty:
            trades = result.trades.copy()
            trades.insert(0, "policy_id", policy.policy_id)
            trades.insert(0, "portfolio_id", portfolio_id)
            trade_rows.append(trades)
        observations = pd.DataFrame(simulator.weight_observations)
        if not observations.empty:
            observations.insert(0, "policy_id", policy.policy_id)
            observations.insert(0, "portfolio_id", portfolio_id)
            weight_rows.append(observations)
    return (
        pd.DataFrame(tax_rows),
        pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame(),
        pd.concat(weight_rows, ignore_index=True) if weight_rows else pd.DataFrame(),
    )


def stress_selected(
    *,
    selections: pd.DataFrame,
    raw: Mapping[str, pd.DataFrame],
    residuals: Mapping[str, float],
) -> pd.DataFrame:
    scenarios = {
        "dotcom_1999_2003": (base.DOTCOM_START, base.DOTCOM_END),
        "ndx_1985_reference": (base.NDX_START, base.FIXED_END),
    }
    rows: list[dict[str, Any]] = []
    for scenario, (start, end) in scenarios.items():
        frames = base.build_stress_frames(
            underlying=raw["^NDX"],
            short_yield=raw["^IRX"]["Close"],
            residual_2x=float(residuals["QLD_2x"]),
            residual_3x=float(residuals["TQQQ_3x"]),
            start=start,
            end=end,
        )
        for _, selected in selections.iterrows():
            portfolio_id = str(selected["portfolio_id"])
            policy = base.PolicySpec(
                policy_id=str(selected["policy_id"]),
                contribution_mode=str(selected["contribution_mode"]),
                rebalance_mode=str(selected["rebalance_mode"]),
                rebalance_month=(
                    int(selected["rebalance_month"])
                    if pd.notna(selected["rebalance_month"])
                    else None
                ),
                band=float(selected["band"])
                if pd.notna(selected["band"])
                else None,
                legacy_order=bool(selected["legacy_order"]),
            )
            metrics, _, _ = run_period(
                portfolio=base.PORTFOLIOS[portfolio_id],
                policy=policy,
                frames=frames,
                start=start,
                end=end,
            )
            rows.append(
                {
                    "scenario": scenario,
                    "portfolio_id": portfolio_id,
                    "policy_id": policy.policy_id,
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def build_report(
    *,
    results: pd.DataFrame,
    selections: pd.DataFrame,
    stress: pd.DataFrame,
    snapshot_manifest: Mapping[str, Any],
) -> str:
    lines = [
        "# Equity v2 연간 밴드 추가 연구",
        "",
        f"- 고정 연구기간: {base.FIXED_START.date().isoformat()}~{base.FIXED_END.date().isoformat()}",
        "- 이전 운영정책 연구와 동일한 6개 입력 CSV 스냅샷 사용",
        "- 1월 첫 거래일 정확복원, 1월 연간 밴드, 월간 밴드, 신규입금만 보정을 동일 조건으로 비교",
        "- 초기 8,000만원, 월 50만원, 일반 과세계좌 세금·비용 근사",
        "- BTC, 기존 Equity v1 사이트·Telegram, 자동주문 변경 없음",
        "",
        "## 1. 전체 비교",
        "",
        "| 포트폴리오 | 정책 | 개발 평균 세후 XIRR | 개발 최저 세후 XIRR | 개발 최악 MDD | 전체 세후 XIRR | 전체 MDD | 정책매도 횟수 | 리밸런싱 이벤트 | 정책매도액 | 세후 최종액 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for portfolio_id in base.PORTFOLIOS:
        subset = results.loc[results["portfolio_id"] == portfolio_id].sort_values(
            ["dev_min_after_tax_xirr", "dev_mean_after_tax_xirr"],
            ascending=[False, False],
        )
        for _, row in subset.iterrows():
            lines.append(
                f"| {portfolio_id} | {row['policy_id']} | "
                f"{_fmt_pct(row['dev_mean_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
                f"{_fmt_pct(row['dev_worst_mdd'])} | "
                f"{_fmt_pct(row['full_after_tax_xirr'])} | "
                f"{_fmt_pct(row['full_mdd'])} | "
                f"{int(row['full_policy_sell_count'])} | "
                f"{int(row['full_rebalance_event_count'])} | "
                f"{_fmt_krw(row['full_policy_sell_notional_krw'])} | "
                f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
            )

    lines.extend(
        [
            "",
            "## 2. 세금·거래 우선 실전 선택",
            "",
            f"개발 최저·평균 XIRR은 각각 최고치에서 {DEV_XIRR_TOLERANCE * 100:.2f}%p 이내, "
            f"개발 최악 MDD는 후보군 최고치에서 {MDD_TOLERANCE * 100:.0f}%p 이내를 요구한 뒤 "
            "정책매도 횟수와 매도금액이 작은 순으로 선택했다.",
            "",
            "| 포트폴리오 | 선택 정책 | 개발 최저 XIRR | 전체 세후 XIRR | 전체 MDD | 정책매도 횟수 | 총 세금 | 세후 최종액 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in selections.iterrows():
        lines.append(
            f"| {row['portfolio_id']} | {row['policy_id']} | "
            f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
            f"{_fmt_pct(row['full_after_tax_xirr'])} | "
            f"{_fmt_pct(row['full_mdd'])} | "
            f"{int(row['full_policy_sell_count'])} | "
            f"{_fmt_krw(row['full_tax_paid_krw'] + row['full_terminal_tax_krw'])} | "
            f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 3. 선택 정책 스트레스 참고",
            "",
            "| 시나리오 | 포트폴리오 | 정책 | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in stress.iterrows():
        lines.append(
            f"| {row['scenario']} | {row['portfolio_id']} | {row['policy_id']} | "
            f"{_fmt_pct(row['after_tax_xirr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['minimum_vs_contributions'])} | "
            f"{_fmt_krw(row['after_tax_liquidation_value_krw'])} |"
        )

    lines.extend(
        [
            "",
            "## 4. 해석 원칙",
            "",
            "- 연간 밴드는 1월 첫 거래일에만 확인하며, 밴드 밖이면 목표비중까지 복원한다.",
            "- 월간 밴드는 매월 첫 거래일에 확인하므로 같은 밴드라도 거래시점과 결과가 다르다.",
            "- 신규입금만 보정은 목표비중을 보장하지 않으며, 높은 전체수익이 레버리지 비중의 상향 드리프트에서 나올 수 있다.",
            "- 홀드아웃과 1985년 스트레스는 정책 선택에 사용하지 않았다.",
            "- CASH는 실제 SGOV가 아니라 단기금리 기반 합성 현금성 시계열이다.",
            "",
            "## 5. 재현성",
            "",
            f"- 입력 스냅샷 파일 수: {len(snapshot_manifest['files'])}",
            f"- 고정 종료일: {snapshot_manifest['fixed_end']}",
            "- 입력 SHA-256과 상세 CSV는 artifact에 포함했다.",
        ]
    )
    return "\n".join(lines)


def run(*, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    paths.output.mkdir(parents=True, exist_ok=True)
    frames, raw, calibration, residuals = base.build_frozen_frames(
        paths=paths,
        refresh=False,
    )
    latest = min(
        base.FIXED_END,
        min(
            pd.Timestamp(frames[asset]["Close"].dropna().index.max())
            for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
        ),
    )
    snapshot_manifest = base.write_input_snapshot(raw=raw, output_dir=paths.output)

    policies = policy_grid(1)
    rows: list[dict[str, Any]] = []
    total = len(base.PORTFOLIOS) * len(policies)
    counter = 0
    for portfolio_id, portfolio in base.PORTFOLIOS.items():
        for policy in policies:
            counter += 1
            print(
                f"annual-band study: {counter}/{total} {portfolio_id} {policy.policy_id}",
                flush=True,
            )
            rows.append(
                evaluate_policy(
                    portfolio_id=portfolio_id,
                    portfolio=portfolio,
                    policy=policy,
                    frames=frames,
                    latest=latest,
                )
            )
    results = pd.DataFrame(rows)
    selections = pd.DataFrame(
        [
            choose_tax_aware_policy(results, portfolio_id)
            for portfolio_id in base.PORTFOLIOS
        ]
    ).reset_index(drop=True)
    yearly_tax, selected_trades, selected_weights = selected_details(
        selections=selections,
        frames=frames,
        latest=latest,
    )
    stress = stress_selected(
        selections=selections,
        raw=raw,
        residuals=residuals,
    )
    report = build_report(
        results=results,
        selections=selections,
        stress=stress,
        snapshot_manifest=snapshot_manifest,
    )

    outputs = {
        "results": paths.output / "equity_v2_annual_band_results.csv",
        "selected": paths.output / "equity_v2_annual_band_selected.csv",
        "stress": paths.output / "equity_v2_annual_band_stress.csv",
        "yearly_tax": paths.output / "equity_v2_annual_band_yearly_tax.csv",
        "selected_trades": paths.output / "equity_v2_annual_band_selected_trades.csv",
        "selected_weights": paths.output / "equity_v2_annual_band_selected_weights.csv",
        "calibration": paths.output / "equity_v2_annual_band_calibration.csv",
        "report": paths.output / "equity_v2_annual_band_report.md",
        "manifest": paths.output / "equity_v2_annual_band_manifest.json",
    }
    results.to_csv(outputs["results"], index=False, encoding="utf-8")
    selections.to_csv(outputs["selected"], index=False, encoding="utf-8")
    stress.to_csv(outputs["stress"], index=False, encoding="utf-8")
    yearly_tax.to_csv(outputs["yearly_tax"], index=False, encoding="utf-8")
    selected_trades.to_csv(outputs["selected_trades"], index=False, encoding="utf-8")
    selected_weights.to_csv(outputs["selected_weights"], index=False, encoding="utf-8")
    calibration.to_csv(outputs["calibration"], index=False, encoding="utf-8")
    outputs["report"].write_text(report, encoding="utf-8")

    manifest = {
        "schema_version": "equity-v2-annual-band-result-1",
        "strategy_version": STRATEGY_VERSION,
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "fixed_start": base.FIXED_START.date().isoformat(),
        "fixed_end": latest.date().isoformat(),
        "policy_count": int(len(results)),
        "selected_count": int(len(selections)),
        "selected_policies": {
            str(row["portfolio_id"]): str(row["policy_id"])
            for _, row in selections.iterrows()
        },
        "dev_xirr_tolerance": DEV_XIRR_TOLERANCE,
        "mdd_tolerance": MDD_TOLERANCE,
        "residuals": residuals,
        "input_snapshot": snapshot_manifest,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "outputs": {key: str(value) for key, value in outputs.items()},
    }
    _write_json(outputs["manifest"], manifest)
    return {"manifest": manifest, "outputs": outputs}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Annual-only versus monthly rebalance band research"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run(config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
