from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

# Importing the runner applies the modern-study calendar, warmup, and periodic
# rebalancing compatibility patches before we call the shared research module.
import equity_v2_modern_runner  # noqa: F401
import equity_v2_modern_research as research
from equity_v2_engine import FeatureStore, close_panel, schedule_dates
from equity_v2_modern_engine import GenericTaxableSimulator
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


WEIGHT_STEP = 0.05
FREQUENCY = "annual"


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


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


def _fmt_pct(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if not _finite(value) else f"{float(value):,.0f}원"


def weight_grid(step: float = WEIGHT_STEP) -> list[dict[str, float]]:
    units = int(round(1.0 / step))
    rows: list[dict[str, float]] = []
    for t_units in range(units + 1):
        for l_units in range(units - t_units + 1):
            q_units = units - t_units - l_units
            rows.append(
                {
                    "TQQQ": t_units / units,
                    "QLD": l_units / units,
                    "QQQ": q_units / units,
                }
            )
    return rows


def build_targets(
    weights: Mapping[str, float],
    *,
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
    frequency: str = FREQUENCY,
) -> pd.DataFrame:
    index = store.close.loc[pd.Timestamp(start) : pd.Timestamp(end)].index
    if len(index) == 0:
        raise ValueError("empty target index")
    dates = [pd.Timestamp(index[0])]
    if frequency != "none":
        dates.extend(pd.Timestamp(value) for value in schedule_dates(index, frequency))
    dates = list(dict.fromkeys(dates))
    row = {asset: float(weights.get(asset, 0.0)) for asset in ("QQQ", "QLD", "TQQQ")}
    return pd.DataFrame([row for _ in dates], index=dates, dtype=float)


def run_period(
    weights: Mapping[str, float],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    actual_start = research.valid_start(frames, pd.Timestamp(start))
    actual_end = min(
        pd.Timestamp(end),
        min(
            pd.Timestamp(frames[asset]["Close"].dropna().index.max())
            for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
        ),
    )
    targets = build_targets(
        weights,
        store=store,
        start=actual_start,
        end=actual_end,
    )
    simulation = GenericTaxableSimulator(
        frames=frames,
        targets=targets,
        start=actual_start,
        end=actual_end,
    ).run()
    return simulation.metrics


def evaluate_mix(
    weights: Mapping[str, float],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: FeatureStore,
    start: pd.Timestamp,
    latest: pd.Timestamp,
) -> dict[str, Any]:
    t = float(weights["TQQQ"])
    l = float(weights["QLD"])
    q = float(weights["QQQ"])
    candidate_id = f"mix_t{round(t * 100):02d}_l{round(l * 100):02d}_q{round(q * 100):02d}_annual"
    values: dict[str, Any] = {
        "candidate_id": candidate_id,
        "tqqq_weight": t,
        "qld_weight": l,
        "qqq_weight": q,
        "all_three_positive": bool(t > 0 and l > 0 and q > 0),
        "effective_daily_leverage": 3.0 * t + 2.0 * l + q,
        "frequency": FREQUENCY,
    }

    full = run_period(
        weights,
        frames=frames,
        store=store,
        start=start,
        end=latest,
    )
    values.update({f"full_{key}": value for key, value in full.items()})

    fold_xirrs: list[float] = []
    fold_mdds: list[float] = []
    for prefix, (fold_start, fold_end) in research.DEV_FOLDS.items():
        metrics = run_period(
            weights,
            frames=frames,
            store=store,
            start=fold_start,
            end=fold_end,
        )
        values.update({f"{prefix}_{key}": value for key, value in metrics.items()})
        fold_xirrs.append(float(metrics["after_tax_xirr"]))
        fold_mdds.append(float(metrics["mdd"]))

    holdout = run_period(
        weights,
        frames=frames,
        store=store,
        start=research.HOLDOUT_START,
        end=latest,
    )
    values.update({f"holdout_{key}": value for key, value in holdout.items()})

    actual_start = research.valid_start(frames, research.ACTUAL_TQQQ_START)
    actual = run_period(
        weights,
        frames=frames,
        store=store,
        start=actual_start,
        end=latest,
    )
    values.update({f"actual_tqqq_{key}": value for key, value in actual.items()})

    dev_mean = float(np.mean(fold_xirrs))
    dev_min = float(np.min(fold_xirrs))
    dev_worst_mdd = float(np.min(fold_mdds))
    trade_count = float(full.get("trade_count", 0.0) or 0.0)
    score = 0.60 * dev_mean + 0.40 * dev_min - 0.0003 * math.log1p(max(0.0, trade_count))
    values.update(
        {
            "dev_mean_after_tax_xirr": dev_mean,
            "dev_min_after_tax_xirr": dev_min,
            "dev_worst_mdd": dev_worst_mdd,
            "taxable_selection_score": score,
        }
    )
    return values


def pareto_frontier(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[int] = []
    xirr = frame["full_after_tax_xirr"].to_numpy(dtype=float)
    mdd = frame["full_mdd"].to_numpy(dtype=float)
    for i in range(len(frame)):
        dominated = np.any(
            (xirr >= xirr[i])
            & (mdd >= mdd[i])
            & ((xirr > xirr[i]) | (mdd > mdd[i]))
        )
        if not dominated:
            rows.append(i)
    return frame.iloc[rows].sort_values("full_mdd", ascending=False)


def pick_rows(results: pd.DataFrame) -> dict[str, pd.Series]:
    all_three = results.loc[results["all_three_positive"]].copy()
    qld = results.loc[
        (results["tqqq_weight"] == 0)
        & (results["qld_weight"] == 1)
        & (results["qqq_weight"] == 0)
    ].iloc[0]
    t50_q50 = results.loc[
        (results["tqqq_weight"] == 0.5)
        & (results["qld_weight"] == 0)
        & (results["qqq_weight"] == 0.5)
    ].iloc[0]
    best_score = all_three.nlargest(1, "taxable_selection_score").iloc[0]
    best_return = all_three.nlargest(1, "full_after_tax_xirr").iloc[0]
    not_worse_mdd = all_three.loc[all_three["full_mdd"] >= float(qld["full_mdd"])]
    beat_qld_return = all_three.loc[
        all_three["full_after_tax_xirr"] >= float(qld["full_after_tax_xirr"])
    ]
    leverage_2x = all_three.loc[
        (all_three["effective_daily_leverage"] - 2.0).abs() < 1e-12
    ]
    picks = {
        "qld_100": qld,
        "tqqq50_qqq50": t50_q50,
        "best_all_three_score": best_score,
        "best_all_three_return": best_return,
        "best_all_three_not_worse_mdd_than_qld": not_worse_mdd.nlargest(
            1, "full_after_tax_xirr"
        ).iloc[0],
        "lowest_mdd_while_beating_qld_return": beat_qld_return.nlargest(
            1, "full_mdd"
        ).iloc[0],
        "best_all_three_at_2x": leverage_2x.nlargest(
            1, "taxable_selection_score"
        ).iloc[0],
    }
    return picks


def row_table(row: pd.Series) -> str:
    return (
        f"| {row['candidate_id']} | {row['tqqq_weight'] * 100:.0f}% | "
        f"{row['qld_weight'] * 100:.0f}% | {row['qqq_weight'] * 100:.0f}% | "
        f"{row['effective_daily_leverage']:.2f}x | "
        f"{_fmt_pct(row['full_after_tax_xirr'])} | {_fmt_pct(row['full_mdd'])} | "
        f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
        f"{_fmt_pct(row['dev_worst_mdd'])} | "
        f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} |"
    )


def build_report(results: pd.DataFrame, picks: Mapping[str, pd.Series], *, start: pd.Timestamp, latest: pd.Timestamp) -> str:
    frontier = pareto_frontier(results)
    lines = [
        "# TQQQ·QLD·QQQ 3자산 혼합 집중 연구",
        "",
        f"- 기간: {start.date().isoformat()}~{latest.date().isoformat()}",
        "- 초기 8,000만원, 월 50만원, 일반 과세계좌",
        "- 5% 간격으로 TQQQ·QLD·QQQ 비중 합계 100%",
        "- 매월 신규자금도 목표비중으로 투입",
        "- 연 1회 목표비중으로 전체 계좌 복원",
        "- QQQ는 실전 QQQM의 장기 대용 시계열",
        f"- 총 조합 수: {len(results):,}개, 세 자산 모두 양수인 조합: {int(results['all_three_positive'].sum()):,}개",
        "",
        "## 핵심 비교",
        "",
        "| 후보 | TQQQ | QLD | QQQ | 명목 일간 레버리지 | 전체 세후 XIRR | 전체 MDD | 개발 최저 세후 XIRR | 개발 최악 MDD | 세후 최종액 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in (
        "qld_100",
        "tqqq50_qqq50",
        "best_all_three_score",
        "best_all_three_not_worse_mdd_than_qld",
        "lowest_mdd_while_beating_qld_return",
        "best_all_three_at_2x",
        "best_all_three_return",
    ):
        lines.append(row_table(picks[key]))

    lines.extend(
        [
            "",
            "## 위험·수익 파레토 전선",
            "",
            "| 후보 | TQQQ | QLD | QQQ | 명목 일간 레버리지 | 전체 세후 XIRR | 전체 MDD | 개발 최저 세후 XIRR | 개발 최악 MDD | 세후 최종액 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in frontier.iterrows():
        lines.append(row_table(row))

    lines.extend(
        [
            "",
            "## 해석 원칙",
            "",
            "- 세 상품은 모두 Nasdaq-100의 같은 방향 노출이므로 종목분산 효과는 거의 없다.",
            "- 혼합의 효과는 목표 레버리지 미세조정, 일일 재설정 경로, 연간 리밸런싱과 세금에서 발생한다.",
            "- 같은 명목 레버리지라도 QLD 100%와 TQQQ·QQQ 조합은 경로가 달라 결과가 다를 수 있다.",
            "- 5% 격자이므로 진짜 연속 최적점은 인접 비중 사이에 있을 수 있다.",
            "- 연구 전용이며 실전·Telegram·자동주문에는 반영하지 않았다.",
        ]
    )
    return "\n".join(lines)


def run(*, refresh: bool, config_path: Path) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    paths.output.mkdir(parents=True, exist_ok=True)
    frames, _, residuals, metadata = research.build_primary_frames(
        cfg=cfg,
        paths=paths,
        refresh=refresh,
    )
    start = research.valid_start(frames, research.MODERN_START)
    latest = min(
        pd.Timestamp(frames[asset]["Close"].dropna().index.max())
        for asset in ("QQQ", "QLD", "TQQQ", "CASH", "KRW=X")
    )
    store = FeatureStore(close_panel(frames))

    rows: list[dict[str, Any]] = []
    combinations = weight_grid()
    for number, weights in enumerate(combinations, start=1):
        rows.append(
            evaluate_mix(
                weights,
                frames=frames,
                store=store,
                start=start,
                latest=latest,
            )
        )
        if number % 25 == 0:
            print(f"three-asset mix: {number}/{len(combinations)}", flush=True)

    results = pd.DataFrame(rows)
    picks = pick_rows(results)
    report = build_report(results, picks, start=start, latest=latest)
    now = datetime.now(UTC)
    manifest = {
        "schema_version": "equity-v2-three-asset-mix-0.1",
        "generated_at_utc": now.isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "start": start.date().isoformat(),
        "end": latest.date().isoformat(),
        "weight_step": WEIGHT_STEP,
        "frequency": FREQUENCY,
        "combination_count": len(results),
        "all_three_positive_count": int(results["all_three_positive"].sum()),
        "residual_drags": residuals,
        "data_metadata": metadata,
        "picks": {
            key: {
                "candidate_id": row["candidate_id"],
                "tqqq_weight": float(row["tqqq_weight"]),
                "qld_weight": float(row["qld_weight"]),
                "qqq_weight": float(row["qqq_weight"]),
                "effective_daily_leverage": float(row["effective_daily_leverage"]),
                "full_after_tax_xirr": float(row["full_after_tax_xirr"]),
                "full_mdd": float(row["full_mdd"]),
                "dev_min_after_tax_xirr": float(row["dev_min_after_tax_xirr"]),
                "dev_worst_mdd": float(row["dev_worst_mdd"]),
                "full_after_tax_liquidation_value_krw": float(
                    row["full_after_tax_liquidation_value_krw"]
                ),
            }
            for key, row in picks.items()
        },
        "limitations": [
            "QQQ is used as the historical proxy for QQQM",
            "pre-2010 TQQQ is synthetic and calibrated to actual TQQQ",
            "5 percentage point weight grid",
            "annual rebalancing only",
            "tax accounting is an average-cost approximation",
            "2026 is incomplete",
        ],
    }

    csv_path = paths.output / "equity_v2_three_asset_mix_annual.csv"
    report_path = paths.output / "equity_v2_three_asset_mix_report.md"
    manifest_path = paths.output / "equity_v2_three_asset_mix_manifest.json"
    frontier_path = paths.output / "equity_v2_three_asset_mix_pareto.csv"
    results.to_csv(csv_path, index=False, encoding="utf-8-sig")
    pareto_frontier(results).to_csv(frontier_path, index=False, encoding="utf-8-sig")
    report_path.write_text(report, encoding="utf-8-sig")
    manifest_path.write_text(
        json.dumps(_json_ready(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(report)
    print(json.dumps(_json_ready(manifest), ensure_ascii=False, indent=2))
    return {
        "csv": str(csv_path),
        "pareto": str(frontier_path),
        "report": str(report_path),
        "manifest": str(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Focused TQQQ/QLD/QQQ annual-rebalanced mix study")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    run(refresh=args.refresh, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
