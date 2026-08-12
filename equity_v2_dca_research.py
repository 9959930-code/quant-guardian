from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from equity_v2_dca_engine import (
    ANNUAL_DEDUCTION_KRW,
    INITIAL_CAPITAL_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    DcaSimulator,
    SimulationResult,
    StrategySpec,
    build_strategy_grid,
    prepare_market,
    synthetic_tqqq_from_qqq,
)
from equity_v2_engine import load_market_data
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


STRATEGY_VERSION = "equity-v2-dca-taxable-0.1"
ACTUAL_START = pd.Timestamp("2010-03-01")
DEV_FOLDS = {
    "dev_2010_2014": (pd.Timestamp("2010-03-01"), pd.Timestamp("2014-12-31")),
    "dev_2015_2018": (pd.Timestamp("2015-01-01"), pd.Timestamp("2018-12-31")),
    "dev_2019_2022": (pd.Timestamp("2019-01-01"), pd.Timestamp("2022-12-31")),
}
HOLDOUT_START = pd.Timestamp("2023-01-01")


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


def run_spec(
    market: pd.DataFrame,
    spec: StrategySpec,
    *,
    deduction: float = ANNUAL_DEDUCTION_KRW,
) -> SimulationResult:
    return DcaSimulator(
        market,
        spec,
        initial_capital_krw=INITIAL_CAPITAL_KRW,
        monthly_contribution_krw=MONTHLY_CONTRIBUTION_KRW,
        tax_deduction_krw=deduction,
    ).run()


def _fold_market(
    full_market: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    market = full_market.loc[start:end].copy()
    if len(market) < 250:
        raise ValueError(f"fold too short: {start}~{end}")
    return market


def broad_search(
    specs: Iterable[StrategySpec],
    *,
    actual_market: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    specs = list(specs)
    fold_markets = {
        name: _fold_market(actual_market, start, end)
        for name, (start, end) in DEV_FOLDS.items()
    }
    for number, spec in enumerate(specs, start=1):
        record = {**asdict(spec), "direct_ladder": json.dumps(spec.direct_ladder)}
        try:
            values: dict[str, Any] = {}
            xirrs: list[float] = []
            mdds: list[float] = []
            for name, market in fold_markets.items():
                result = run_spec(market, spec)
                metrics = result.metrics
                for key in (
                    "pre_tax_xirr",
                    "after_tax_xirr",
                    "mdd",
                    "minimum_vs_contributions",
                    "pre_tax_value_krw",
                    "after_tax_liquidation_value_krw",
                    "tax_paid_krw",
                    "terminal_tax_krw",
                    "trade_count",
                    "conversion_trade_count",
                ):
                    values[f"{name}_{key}"] = metrics.get(key)
                if _finite(metrics.get("after_tax_xirr")):
                    xirrs.append(float(metrics["after_tax_xirr"]))
                if _finite(metrics.get("mdd")):
                    mdds.append(float(metrics["mdd"]))
            if len(xirrs) != len(DEV_FOLDS):
                raise ValueError("missing fold XIRR")
            mean_xirr = float(np.mean(xirrs))
            min_xirr = float(np.min(xirrs))
            worst_mdd = float(np.min(mdds))
            score = 0.60 * mean_xirr + 0.40 * min_xirr
            rows.append(
                {
                    **record,
                    "status": "ok",
                    "error": None,
                    **values,
                    "dev_mean_after_tax_xirr": mean_xirr,
                    "dev_min_after_tax_xirr": min_xirr,
                    "dev_worst_mdd": worst_mdd,
                    "selection_score": score,
                }
            )
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 250 == 0:
            print(f"DCA broad {number}/{len(specs)}", flush=True)
    return pd.DataFrame(rows)


def shortlist(
    broad: pd.DataFrame,
    spec_map: Mapping[str, StrategySpec],
    maximum: int = 120,
) -> list[StrategySpec]:
    valid = broad.loc[
        broad["status"].eq("ok") & broad["selection_score"].notna()
    ].copy()
    selected: set[str] = set(
        valid.loc[
            valid["strategy_id"].isin(
                ["always_tqqq", "always_qqq_contrib", "split_50_50"]
            ),
            "strategy_id",
        ]
    )
    selected.update(valid.nlargest(40, "selection_score")["strategy_id"])
    selected.update(valid.nlargest(25, "dev_mean_after_tax_xirr")["strategy_id"])
    for mdd_limit in (-0.95, -0.85, -0.75, -0.65, -0.55):
        eligible = valid.loc[valid["dev_worst_mdd"] >= mdd_limit]
        selected.update(eligible.nlargest(20, "selection_score")["strategy_id"])
    for _, group in valid.groupby("mode"):
        selected.update(group.nlargest(10, "selection_score")["strategy_id"])
    ranked = valid.loc[valid["strategy_id"].isin(selected)].sort_values(
        "selection_score", ascending=False
    )
    if len(ranked) > maximum:
        baselines = {"always_tqqq", "always_qqq_contrib", "split_50_50"}
        keep = set(ranked.head(maximum - len(baselines))["strategy_id"]) | baselines
        ranked = ranked.loc[ranked["strategy_id"].isin(keep)]
    return [spec_map[strategy_id] for strategy_id in ranked["strategy_id"]]


def full_evaluate(
    specs: Iterable[StrategySpec],
    *,
    actual_market: pd.DataFrame,
    latest: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    rows: list[dict[str, Any]] = []
    simulations: dict[str, SimulationResult] = {}
    holdout_market = _fold_market(actual_market, HOLDOUT_START, latest)
    for spec in specs:
        record = {**asdict(spec), "direct_ladder": json.dumps(spec.direct_ladder)}
        try:
            full = run_spec(actual_market, spec)
            holdout = run_spec(holdout_market, spec)
            no_deduction = run_spec(actual_market, spec, deduction=0.0)
            simulations[spec.strategy_id] = full
            row = {
                **record,
                "status": "ok",
                "error": None,
                **{f"full_{key}": value for key, value in full.metrics.items()},
                **{f"holdout_{key}": value for key, value in holdout.metrics.items()},
                **{
                    f"no_deduction_{key}": value
                    for key, value in no_deduction.metrics.items()
                    if key
                    in {
                        "after_tax_xirr",
                        "after_tax_liquidation_value_krw",
                        "tax_paid_krw",
                        "terminal_tax_krw",
                    }
                },
            }
            rows.append(row)
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
    return pd.DataFrame(rows), simulations


def select_winners(broad: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    merged = full.merge(
        broad[
            [
                "strategy_id",
                "dev_mean_after_tax_xirr",
                "dev_min_after_tax_xirr",
                "dev_worst_mdd",
                "selection_score",
            ]
        ],
        on="strategy_id",
        how="left",
    )
    valid = merged.loc[merged["status"].eq("ok")].copy()
    categories = [
        ("max_after_tax_xirr", None),
        ("mdd_85", -0.85),
        ("mdd_75", -0.75),
        ("mdd_65", -0.65),
        ("mdd_55", -0.55),
    ]
    rows: list[dict[str, Any]] = []
    for category, mdd_limit in categories:
        eligible = valid
        if mdd_limit is not None:
            eligible = eligible.loc[eligible["dev_worst_mdd"] >= mdd_limit]
        if eligible.empty:
            continue
        best = eligible.sort_values(
            ["selection_score", "dev_mean_after_tax_xirr"], ascending=False
        ).iloc[0]
        rows.append({"category": category, **best.to_dict()})
    for baseline in ("always_tqqq", "always_qqq_contrib", "split_50_50"):
        row = valid.loc[valid["strategy_id"] == baseline]
        if not row.empty:
            rows.append({"category": baseline, **row.iloc[0].to_dict()})
    return pd.DataFrame(rows).drop_duplicates("category")


def rolling_five_years(
    selected: Mapping[str, StrategySpec],
    *,
    actual_market: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year in range(2010, 2022):
        start = max(pd.Timestamp(f"{year}-01-01"), ACTUAL_START)
        end = pd.Timestamp(f"{year + 5}-12-31")
        market = actual_market.loc[start:end]
        if len(market) < 900:
            continue
        for name, spec in selected.items():
            result = run_spec(market, spec)
            rows.append(
                {
                    "start_year": year,
                    "strategy": name,
                    "after_tax_xirr": result.metrics["after_tax_xirr"],
                    "pre_tax_xirr": result.metrics["pre_tax_xirr"],
                    "mdd": result.metrics["mdd"],
                    "after_tax_value_krw": result.metrics[
                        "after_tax_liquidation_value_krw"
                    ],
                }
            )
    return pd.DataFrame(rows)


def synthetic_stress(
    selected: Mapping[str, StrategySpec],
    *,
    frames: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    synthetic = synthetic_tqqq_from_qqq(frames["QQQ"], annual_drag=0.025)
    periods = {
        "dotcom_2000_2006": (
            pd.Timestamp("2000-01-03"),
            pd.Timestamp("2006-12-29"),
        ),
        "gfc_2006_2012": (
            pd.Timestamp("2006-07-03"),
            pd.Timestamp("2012-12-31"),
        ),
    }
    rows: list[dict[str, Any]] = []
    for period, (start, end) in periods.items():
        market = prepare_market(frames, start, end, synthetic_tqqq=synthetic)
        if len(market) < 500:
            continue
        for name, spec in selected.items():
            result = run_spec(market, spec)
            rows.append(
                {
                    "period": period,
                    "strategy": name,
                    "pre_tax_xirr": result.metrics["pre_tax_xirr"],
                    "after_tax_xirr": result.metrics["after_tax_xirr"],
                    "mdd": result.metrics["mdd"],
                    "minimum_vs_contributions": result.metrics[
                        "minimum_vs_contributions"
                    ],
                    "after_tax_value_krw": result.metrics[
                        "after_tax_liquidation_value_krw"
                    ],
                }
            )
    return pd.DataFrame(rows)


def current_state(actual_market: pd.DataFrame) -> dict[str, Any]:
    latest = actual_market.iloc[-1]
    return {
        "date": actual_market.index[-1].date().isoformat(),
        "qqq_close": float(latest["qqq_close"]),
        "qqq_drawdown": float(latest["qqq_drawdown"]),
        "above_sma20": bool(latest["qqq_close"] > latest["qqq_sma_20"]),
        "above_sma50": bool(latest["qqq_close"] > latest["qqq_sma_50"]),
        "above_sma100": bool(latest["qqq_close"] > latest["qqq_sma_100"]),
    }


def build_report(
    *,
    manifest: Mapping[str, Any],
    winners: pd.DataFrame,
    rolling: pd.DataFrame,
    synthetic: pd.DataFrame,
) -> str:
    lines = [
        "# Equity v2 적립식·과세계좌 연구 결과",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 초기자금: {_fmt_krw(manifest['initial_capital_krw'])}",
        f"- 월 신규자금: {_fmt_krw(manifest['monthly_contribution_krw'])}",
        "- 초기자금은 TQQQ에 일괄 투자",
        "- 실제 연구기간: 2010-03~최신",
        "- 과세계좌 기본 시나리오: 연 250만원 기본공제 후 22% 세율 근사",
        "- 공제 소진 시나리오도 별도 계산",
        "- BTC·실전 주식 Telegram·사이트 변경 없음",
        "",
        "## 개발구간 선정 후보와 기준선",
        "",
        "| 구분 | 전략 | 개발 평균 세후 XIRR | 개발 최저 세후 XIRR | 개발 최악 MDD | 전체 세전 XIRR | 전체 세후 XIRR | 전체 MDD | 세후 최종액 | 공제 소진 세후 최종액 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in winners.iterrows():
        lines.append(
            f"| {row['category']} | `{row['strategy_id']}` | "
            f"{_fmt_pct(row['dev_mean_after_tax_xirr'])} | "
            f"{_fmt_pct(row['dev_min_after_tax_xirr'])} | "
            f"{_fmt_pct(row['dev_worst_mdd'])} | "
            f"{_fmt_pct(row['full_pre_tax_xirr'])} | "
            f"{_fmt_pct(row['full_after_tax_xirr'])} | "
            f"{_fmt_pct(row['full_mdd'])} | "
            f"{_fmt_krw(row['full_after_tax_liquidation_value_krw'])} | "
            f"{_fmt_krw(row['no_deduction_after_tax_liquidation_value_krw'])} |"
        )

    if not rolling.empty:
        lines.extend(
            [
                "",
                "## 5년 시작시점 반복검증",
                "",
                "| 전략 | 중앙 세후 XIRR | 최저 세후 XIRR | 중앙 MDD | 최악 MDD |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for strategy, group in rolling.groupby("strategy"):
            lines.append(
                f"| {strategy} | {_fmt_pct(group['after_tax_xirr'].median())} | "
                f"{_fmt_pct(group['after_tax_xirr'].min())} | "
                f"{_fmt_pct(group['mdd'].median())} | {_fmt_pct(group['mdd'].min())} |"
            )

    if not synthetic.empty:
        lines.extend(
            [
                "",
                "## 닷컴버블·금융위기 합성 스트레스",
                "",
                "| 기간 | 전략 | 세전 XIRR | 세후 XIRR | MDD | 납입원금 대비 최저 | 세후 최종액 |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for _, row in synthetic.iterrows():
            lines.append(
                f"| {row['period']} | {row['strategy']} | "
                f"{_fmt_pct(row['pre_tax_xirr'])} | {_fmt_pct(row['after_tax_xirr'])} | "
                f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['minimum_vs_contributions'])} | "
                f"{_fmt_krw(row['after_tax_value_krw'])} |"
            )

    state = manifest["current_state"]
    lines.extend(
        [
            "",
            "## 최신 QQQ 상태",
            "",
            f"- 기준일: {state['date']}",
            f"- 52주 고점 대비 낙폭: {_fmt_pct(state['qqq_drawdown'])}",
            f"- 20일선 위: {'예' if state['above_sma20'] else '아니오'}",
            f"- 50일선 위: {'예' if state['above_sma50'] else '아니오'}",
            f"- 100일선 위: {'예' if state['above_sma100'] else '아니오'}",
            "",
            "## 해석 제한",
            "",
            "- QQQM의 2020년 이전 가격은 QQQ를 대용으로 사용했다.",
            "- 과세계좌 계산은 평균취득가와 연간 손익통산을 단순화한 근사다.",
            "- 연 250만원 기본공제가 다른 해외주식 거래에 이미 사용될 수 있어 공제 0원 민감도를 함께 표시했다.",
            "- 닷컴·2008 스트레스의 TQQQ는 QQQ 일일수익률 3배와 연 2.5% 드래그를 적용한 합성값이다.",
            "- 과거 최적 전환규칙은 미래 수익을 보장하지 않는다.",
        ]
    )
    return "\n".join(lines)


def run_research(
    *,
    refresh: bool,
    config_path: Path,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(UTC)
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, metadata = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    latest = pd.Timestamp(frames["QQQ"]["Close"].dropna().index.max())
    actual_market = prepare_market(frames, ACTUAL_START, latest)
    specs = build_strategy_grid()
    spec_map = {spec.strategy_id: spec for spec in specs}
    print(f"DCA candidates: {len(specs):,}", flush=True)
    broad = broad_search(specs, actual_market=actual_market)
    short = shortlist(broad, spec_map, maximum=120)
    full, _ = full_evaluate(short, actual_market=actual_market, latest=latest)
    winners = select_winners(broad, full)
    selected_specs: dict[str, StrategySpec] = {}
    for _, row in winners.iterrows():
        selected_specs[str(row["category"])] = spec_map[str(row["strategy_id"])]
    rolling = rolling_five_years(selected_specs, actual_market=actual_market)
    synthetic = synthetic_stress(selected_specs, frames=frames)

    outputs = {
        "broad": paths.output / "equity_v2_dca_broad.csv",
        "full": paths.output / "equity_v2_dca_full.csv",
        "winners": paths.output / "equity_v2_dca_winners.csv",
        "rolling": paths.output / "equity_v2_dca_rolling.csv",
        "synthetic": paths.output / "equity_v2_dca_synthetic.csv",
        "manifest": paths.output / "equity_v2_dca_manifest.json",
        "report": paths.output / "equity_v2_dca_report.md",
    }
    broad.to_csv(outputs["broad"], index=False, encoding="utf-8-sig")
    full.to_csv(outputs["full"], index=False, encoding="utf-8-sig")
    winners.to_csv(outputs["winners"], index=False, encoding="utf-8-sig")
    rolling.to_csv(outputs["rolling"], index=False, encoding="utf-8-sig")
    synthetic.to_csv(outputs["synthetic"], index=False, encoding="utf-8-sig")

    manifest = {
        "schema_version": "equity-v2-dca-taxable-0.1",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": now.isoformat(),
        "mode": "research-only",
        "btc_changed": False,
        "live_equity_changed": False,
        "auto_order": False,
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
        "actual_start": actual_market.index[0].date().isoformat(),
        "data_end": actual_market.index[-1].date().isoformat(),
        "candidate_count": len(specs),
        "broad_success_count": int((broad["status"] == "ok").sum()),
        "shortlist_count": len(short),
        "annual_tax_deduction_krw": ANNUAL_DEDUCTION_KRW,
        "tax_rate": 0.22,
        "current_state": current_state(actual_market),
        "selected": [
            {"category": row["category"], "strategy_id": row["strategy_id"]}
            for _, row in winners.iterrows()
        ],
        "data_metadata": metadata,
        "limitations": [
            "QQQ proxies QQQM before 2020",
            "tax lot accounting is an average-cost approximation",
            "other overseas stock gains may consume the annual deduction",
            "synthetic TQQQ is approximate",
            "no live approval",
        ],
    }
    _write_json(outputs["manifest"], manifest)
    outputs["report"].write_text(
        build_report(
            manifest=manifest,
            winners=winners,
            rolling=rolling,
            synthetic=synthetic,
        ),
        encoding="utf-8-sig",
    )
    return {
        "manifest": manifest,
        "outputs": {key: str(value) for key, value in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Equity v2 DCA and taxable-account research"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    result = run_research(refresh=args.refresh, config_path=args.config)
    print(json.dumps(_json_ready(result["manifest"]), ensure_ascii=False, indent=2))
    print(f"report: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
