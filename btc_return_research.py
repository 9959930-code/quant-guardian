from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from btc_cycle_research import build_synthetic_krw_market, overlap_diagnostics
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
from btc_research import build_feature_frame, load_coinmetrics_history, performance_metrics
from btc_return_eval import (
    BUFFERED_MDD_LIMIT,
    MDD_LIMIT,
    baseline_comparison,
    evaluate_crosschecks,
    evaluate_full_grid,
    robustness_diagnostics,
    select_candidates,
    simulate_candidate,
)
from btc_return_models import add_return_features, candidate_grid

STRATEGY_VERSION = "btc-return-first-v0.7"
DEFAULT_START_DATE = "2016-01-01"
DEFAULT_INITIAL_CAPITAL_KRW = 10_000_000.0


def _fmt_pct(value: Any) -> str:
    return "n/a" if pd.isna(value) else f"{float(value) * 100:.2f}%"


def _fmt_krw(value: Any) -> str:
    return "n/a" if pd.isna(value) else f"{float(value):,.0f}원"


def build_report(
    manifest: Mapping[str, Any],
    ranking: pd.DataFrame,
    comparison: pd.DataFrame,
    robust: pd.DataFrame,
    cycles: pd.DataFrame,
    current: Mapping[str, Any],
) -> str:
    lines = [
        "# BTC 수익률 우선 최적화 연구 v0.7",
        "",
        f"- 생성시각: {manifest['generated_at_utc']}",
        f"- 연구기간: {manifest['data_start']}~{manifest['data_end']}",
        f"- 공격형 후보: `{manifest['aggressive_candidate']}`",
        f"- 완충형 후보: `{manifest['buffered_candidate']}`",
        "- 상태: 과거 연구 / 실전·웹·Telegram 미승인",
        "",
        "> 목적함수를 Calmar 우선에서 수익률 우선으로 바꿨다. 단, 전체기간·Upbit 중첩·완료 반감기 사이클 모두 MDD -50% 이내를 요구한다.",
        "",
        "## 상위 강건 후보",
        "",
        "| 후보 | 전체 CAGR | 전체 MDD | Upbit CAGR | Upbit MDD | 완료사이클 최악 MDD | 강건 CAGR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in ranking.loc[ranking["risk_pass"]].head(15).iterrows():
        lines.append(
            f"| {row['candidate_id']} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['upbit_cagr'])} | "
            f"{_fmt_pct(row['upbit_mdd'])} | "
            f"{_fmt_pct(row['worst_completed_cycle_mdd'])} | "
            f"{_fmt_pct(row['robust_cagr'])} |"
        )
    lines.extend(
        [
            "",
            "## 기준전략 비교",
            "",
            "| 전략 | 최종자산 | CAGR | MDD | 평균노출 | 거래 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in comparison.iterrows():
        lines.append(
            f"| {row['candidate_id']} | {_fmt_krw(row['terminal_wealth_krw'])} | "
            f"{_fmt_pct(row['cagr'])} | {_fmt_pct(row['mdd'])} | "
            f"{_fmt_pct(row['exposure'])} | {int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 공격형 강건성",
            "",
            "| 조건 | CAGR | MDD | 최종자산 |",
            "|---|---:|---:|---:|",
        ]
    )
    for _, row in robust.iterrows():
        lines.append(
            f"| {row['scenario']} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_krw(row['terminal_wealth_krw'])} |"
        )
    selected_cycles = cycles.loc[
        cycles["candidate_id"] == manifest["aggressive_candidate"]
    ]
    lines.extend(
        [
            "",
            "## 공격형 완료 반감기 사이클",
            "",
            "| Epoch | CAGR | MDD | 평균노출 | 거래 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in selected_cycles.iterrows():
        lines.append(
            f"| {int(row['halving_epoch'])} | {_fmt_pct(row['cagr'])} | "
            f"{_fmt_pct(row['mdd'])} | {_fmt_pct(row['exposure'])} | "
            f"{int(row['trades'])} |"
        )
    lines.extend(
        [
            "",
            "## 최근 연구판단",
            "",
            f"- 판단일: {current['date']}",
            f"- 반감기 국면: {current['phase_label']}",
            f"- 사이클 진행률: {_fmt_pct(current['cycle_progress'])}",
            f"- 추세 확인: {'예' if current['trend_on'] else '아니오'}",
            f"- 목표비중: {_fmt_pct(current['desired_weight'])}",
            f"- 근거: {current['reason']}",
            "",
            "## 제한",
            "",
            "- v0.3~v0.6 결과를 본 뒤 넓은 그리드를 탐색했으므로 선택 편향이 크다.",
            "- 완료된 독립 반감기 사이클이 적어 최적 구간을 확정할 수 없다.",
            "- 현재 반감기 사이클은 진행 중이며 향후 성과가 크게 달라질 수 있다.",
            "- 세금은 제외했고 기본 대기현금 수익률은 0%다.",
            "- 자동주문은 금지하며 결과는 투자지시가 아니다.",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def run_research(
    *,
    refresh: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    now_utc: datetime | None = None,
    start_date: str = DEFAULT_START_DATE,
    initial_capital_krw: float = DEFAULT_INITIAL_CAPITAL_KRW,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow" or bool(
        btc_cfg.get("auto_order", False)
    ):
        raise BtcDataError("v0.7 requires shadow mode and no orders")
    phase1 = build_phase1_report(
        refresh=refresh, config_path=config_path, now_utc=now
    )
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 data gate is blocked")
    onchain, fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh, config=config, now_utc=now
    )
    paths = resolve_paths(config)
    data_cfg = btc_cfg.get("data", {})
    runtime = btc_cfg.get("research_runtime", {})
    cache_dir = ROOT / config.get("settings", {}).get("cache_dir", "data/cache")
    usd = closed_yahoo_daily_frame(
        read_price_cache(
            cache_dir / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))
        ),
        now,
    )
    fx = closed_yahoo_daily_frame(
        read_price_cache(
            cache_dir / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))
        ),
        now,
    )
    synthetic = build_synthetic_krw_market(usd, fx)
    upbit = read_price_cache(
        paths.cache
        / f"upbit_{str(btc_cfg.get('execution_market', 'KRW-BTC')).replace('-', '_')}_daily.csv"
    )
    lag = int(runtime.get("onchain_lag_days", 2))
    minimum = int(runtime.get("percentile_min_periods", 730))
    synthetic_history = build_feature_frame(
        synthetic,
        usd,
        fx,
        onchain,
        onchain_lag_days=lag,
        percentile_min_periods=minimum,
    )
    upbit_history = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=lag,
        percentile_min_periods=minimum,
    )
    momentum = MomentumVolatilityParameters()
    synthetic_features = add_return_features(
        add_momentum_volatility_features(synthetic_history, momentum)
    )
    upbit_features = add_return_features(
        add_momentum_volatility_features(upbit_history, momentum)
    )
    features = synthetic_features.loc[pd.Timestamp(start_date) :].copy()
    ready = (
        features["momentum_feature_ready"].fillna(False)
        & features["phase_label"].ne("UNKNOWN")
        & features["wma40"].notna()
    )
    if not ready.any():
        raise BtcDataError("No v0.7 research-ready row")
    features = features.loc[ready.idxmax() :]
    fee_bps = float(runtime.get("fee_bps", 5.0))
    slippage_bps = float(runtime.get("slippage_bps", 10.0))
    grid = candidate_grid()
    candidate_map = {row.candidate_id: row for row in grid}
    full = evaluate_full_grid(
        features,
        grid,
        capital=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    ranking, cycles = evaluate_crosschecks(
        full,
        candidate_map,
        features,
        upbit_features,
        capital=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    aggressive, buffered, insample = select_candidates(ranking)
    comparison, outputs = baseline_comparison(
        features,
        candidate_map,
        list(dict.fromkeys([aggressive, buffered, insample])),
        capital=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    robust = robustness_diagnostics(
        features,
        candidate_map[aggressive],
        capital=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    decisions, aggressive_sim = simulate_candidate(
        features,
        candidate_map[aggressive],
        capital=initial_capital_krw,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    decision_rows = decisions.loc[decisions["is_decision_day"]]
    latest = decision_rows.iloc[-1]
    current = {
        "date": decision_rows.index[-1].date().isoformat(),
        **latest.to_dict(),
    }
    output = {
        "grid": paths.output / "btc_return_v07_grid.csv",
        "ranking": paths.output / "btc_return_v07_ranking.csv",
        "cycles": paths.output / "btc_return_v07_cycles.csv",
        "comparison": paths.output / "btc_return_v07_comparison.csv",
        "robustness": paths.output / "btc_return_v07_robustness.csv",
        "signals": paths.output / "btc_return_v07_signals.csv",
        "equity": paths.output / "btc_return_v07_equity.csv",
        "manifest": paths.output / "btc_return_v07_manifest.json",
        "report": paths.output / "btc_return_v07_report.md",
    }
    full.to_csv(output["grid"], index=False, encoding="utf-8-sig")
    ranking.to_csv(output["ranking"], index=False, encoding="utf-8-sig")
    cycles.to_csv(output["cycles"], index=False, encoding="utf-8-sig")
    comparison.to_csv(output["comparison"], index=False, encoding="utf-8-sig")
    robust.to_csv(output["robustness"], index=False, encoding="utf-8-sig")
    decisions.to_csv(output["signals"], encoding="utf-8-sig")
    pd.DataFrame(
        {name: simulation.daily["equity"] for name, simulation in outputs.items()}
    ).to_csv(output["equity"], encoding="utf-8-sig")
    manifest = {
        "schema_version": "btc-return-first-0.7",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "approved_strategy": None,
        "objective": "maximize minimum of full-period and Upbit-overlap CAGR subject to MDD >= -50% in full, Upbit, and each completed halving epoch",
        "aggressive_candidate": aggressive,
        "buffered_candidate": buffered,
        "insample_highest_candidate": insample,
        "research_recommendation_is_live_approval": False,
        "data_start": features.index.min().date().isoformat(),
        "data_end": features.index.max().date().isoformat(),
        "candidate_count": len(grid),
        "crosschecked_count": len(ranking),
        "mdd_limit": MDD_LIMIT,
        "buffered_mdd_limit": BUFFERED_MDD_LIMIT,
        "aggressive_definition": asdict(candidate_map[aggressive]),
        "buffered_definition": asdict(candidate_map[buffered]),
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "execution_rule": "closed Sunday UTC signal, next daily open",
        "completed_halving_epochs": completed_halving_epochs(features),
        "onchain_cache_fallback": fallback,
        "onchain_cache_error": onchain_error,
        "price_overlap": overlap_diagnostics(synthetic, upbit),
        "limitations": [
            "designed after observing v0.3-v0.6",
            "broad grid search creates selection bias",
            "few completed halving cycles",
            "current epoch incomplete",
            "pre-Upbit prices synthetic",
            "tax excluded",
            "no live approval",
        ],
    }
    write_json(output["manifest"], manifest)
    output["report"].write_text(
        build_report(manifest, ranking, comparison, robust, cycles, current),
        encoding="utf-8-sig",
    )
    return {
        "manifest": manifest,
        "metrics": performance_metrics(aggressive_sim),
        "current": current,
        "outputs": {key: str(value) for key, value in output.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BTC return-first halving optimization v0.7"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument(
        "--initial-capital-krw",
        type=float,
        default=DEFAULT_INITIAL_CAPITAL_KRW,
    )
    args = parser.parse_args()
    result = run_research(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    metrics = result["metrics"]
    print("BTC 수익률 우선 최적화 v0.7")
    print(f"공격형: {result['manifest']['aggressive_candidate']}")
    print(f"CAGR/MDD: {_fmt_pct(metrics['cagr'])} / {_fmt_pct(metrics['mdd'])}")
    print(f"보고서: {result['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
