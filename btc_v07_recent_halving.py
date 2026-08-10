from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from btc_guardian import DEFAULT_CONFIG, iso_utc, resolve_paths, load_config, utc_now
from btc_return_models import generate_return_decisions
from btc_v07_split_engine import simulate_three_split
from btc_v07_three_split_research import (
    BASELINE_CANDIDATE,
    DEFAULT_INITIAL_CAPITAL_KRW,
    DEFAULT_START_DATE,
    load_feature_frames,
)


STRATEGY_VERSION = "btc-v07-latest-halving-three-split-1.0"


def _latest_completed_episode(
    features: pd.DataFrame,
    *,
    initial_capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    decisions = generate_return_decisions(features, BASELINE_CANDIDATE)
    simulation = simulate_three_split(
        features,
        decisions,
        entry_parts=3,
        exit_parts=3,
        rebalance_deadband=BASELINE_CANDIDATE.deadband,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital,
    )

    weekly = decisions.loc[decisions["is_decision_day"]].copy()
    weekly["previous_target"] = weekly["desired_weight"].shift(1).fillna(0.0)
    entries = weekly.loc[
        (weekly["desired_weight"] > 0)
        & (weekly["previous_target"] <= 0)
    ]
    exits = weekly.loc[
        (weekly["desired_weight"] <= 0)
        & (weekly["previous_target"] > 0)
    ]
    if entries.empty or exits.empty:
        raise RuntimeError("No completed holding episode found")

    exit_signal_date = pd.Timestamp(exits.index.max())
    eligible_entries = entries.loc[entries.index < exit_signal_date]
    if eligible_entries.empty:
        raise RuntimeError("Latest exit has no preceding entry")
    entry_signal_date = pd.Timestamp(eligible_entries.index.max())

    trades = simulation.trades.copy()
    if trades.empty:
        raise RuntimeError("Simulation produced no trades")
    trades["date"] = pd.to_datetime(trades["date"], errors="coerce")
    episode_trades = trades.loc[
        (trades["date"] > entry_signal_date)
        & (trades["date"] >= entry_signal_date)
    ].copy()
    entry_buys = episode_trades.loc[
        (episode_trades["side"] == "BUY")
        & (episode_trades["date"] < exit_signal_date + pd.Timedelta(days=1))
    ]
    final_exit_sells = episode_trades.loc[
        (episode_trades["side"] == "SELL")
        & np.isclose(
            pd.to_numeric(episode_trades["final_target"], errors="coerce"),
            0.0,
            atol=1e-12,
        )
        & (episode_trades["date"] > exit_signal_date)
    ]
    if entry_buys.empty or final_exit_sells.empty:
        raise RuntimeError("Could not identify entry or final exit trades")

    first_entry_date = pd.Timestamp(entry_buys["date"].min())
    final_exit_date = pd.Timestamp(final_exit_sells["date"].max())
    daily = simulation.daily.loc[:final_exit_date].copy()
    previous_dates = daily.index[daily.index < first_entry_date]
    start_mark_date = (
        pd.Timestamp(previous_dates.max())
        if len(previous_dates)
        else first_entry_date
    )
    period = simulation.daily.loc[start_mark_date:final_exit_date].copy()
    start_equity = float(period["equity"].iloc[0])
    end_equity = float(period["equity"].iloc[-1])
    total_return = end_equity / start_equity - 1
    elapsed_days = max(1, (final_exit_date - start_mark_date).days)
    cagr = (end_equity / start_equity) ** (365 / elapsed_days) - 1
    drawdown = period["equity"] / period["equity"].cummax() - 1
    mdd = float(drawdown.min())

    all_episode_trades = trades.loc[
        (trades["date"] >= first_entry_date)
        & (trades["date"] <= final_exit_date)
    ].copy()
    buys = all_episode_trades.loc[all_episode_trades["side"] == "BUY"]
    sells = all_episode_trades.loc[all_episode_trades["side"] == "SELL"]

    def weighted_price(frame: pd.DataFrame) -> float | None:
        if frame.empty:
            return None
        units = pd.to_numeric(frame["units"], errors="coerce")
        prices = pd.to_numeric(frame["open_price"], errors="coerce")
        total_units = float(units.sum())
        return None if total_units <= 0 else float((units * prices).sum() / total_units)

    first_open = float(features.loc[first_entry_date, "open"])
    final_open = float(features.loc[final_exit_date, "open"])
    btc_same_dates_return = final_open / first_open - 1
    epoch_at_halving = int(
        pd.to_numeric(features.loc[entry_signal_date:exit_signal_date, "halving_epoch"], errors="coerce")
        .dropna()
        .max()
    )
    halving_dates = features.index[
        pd.to_numeric(features["halving_epoch"], errors="coerce").diff().fillna(0) > 0
    ]
    halving_date = next(
        (
            pd.Timestamp(value)
            for value in halving_dates
            if entry_signal_date < pd.Timestamp(value) < exit_signal_date
        ),
        None,
    )

    return {
        "entry_signal_date": entry_signal_date.date().isoformat(),
        "first_entry_date": first_entry_date.date().isoformat(),
        "last_initial_entry_date": pd.Timestamp(entry_buys["date"].head(3).max()).date().isoformat(),
        "halving_date": None if halving_date is None else halving_date.date().isoformat(),
        "exit_signal_date": exit_signal_date.date().isoformat(),
        "first_exit_date": pd.Timestamp(final_exit_sells["date"].min()).date().isoformat(),
        "final_exit_date": final_exit_date.date().isoformat(),
        "halving_epoch_after_event": epoch_at_halving,
        "start_equity_krw": start_equity,
        "end_equity_krw": end_equity,
        "strategy_total_return": total_return,
        "strategy_cagr": cagr,
        "strategy_mdd": mdd,
        "average_exposure": float(period["actual_weight"].mean()),
        "trade_count": int(len(all_episode_trades)),
        "weighted_average_buy_price": weighted_price(buys),
        "weighted_average_sell_price": weighted_price(sells),
        "btc_return_first_entry_to_final_exit": btc_same_dates_return,
        "elapsed_days": elapsed_days,
        "five_million_start_value": 5_000_000.0,
        "five_million_end_value": 5_000_000.0 * (1 + total_return),
        "five_million_profit": 5_000_000.0 * total_return,
    }


def build_report(
    *,
    generated_at: str,
    full: Mapping[str, Any],
    upbit: Mapping[str, Any],
) -> str:
    def pct(value: Any) -> str:
        return f"{float(value) * 100:.2f}%"

    def krw(value: Any) -> str:
        return f"{float(value):,.0f}원"

    lines = [
        "# BTC v0.7 3분할 최근 반감기 거래구간 결과",
        "",
        f"- 생성시각: {generated_at}",
        "- 전략: `pre65→post35`, vol40, 실제비중 기준, 매수·매도 3주 분할",
        "- 체결: 일요일 확정 신호 다음 월요일 시가",
        "- 비용: 수수료 5bps + 슬리피지 10bps",
        "",
        "## 실제 Upbit 기준",
        "",
        f"- 매수신호: {upbit['entry_signal_date']}",
        f"- 1차·3차 초기매수: {upbit['first_entry_date']}~{upbit['last_initial_entry_date']}",
        f"- 실제 반감기 경계: {upbit['halving_date']}",
        f"- 매도신호: {upbit['exit_signal_date']}",
        f"- 1차·3차 최종매도: {upbit['first_exit_date']}~{upbit['final_exit_date']}",
        f"- 전략구간 수익률: **{pct(upbit['strategy_total_return'])}**",
        f"- 연환산 CAGR: **{pct(upbit['strategy_cagr'])}**",
        f"- 구간 MDD: **{pct(upbit['strategy_mdd'])}**",
        f"- 평균 BTC 노출: {pct(upbit['average_exposure'])}",
        f"- 500만원 예시: {krw(upbit['five_million_end_value'])} / 이익 {krw(upbit['five_million_profit'])}",
        f"- 동일 첫 진입~최종 청산 BTC 가격수익률: {pct(upbit['btc_return_first_entry_to_final_exit'])}",
        "",
        "## 2016 확장 합성원화 기준",
        "",
        f"- 전략구간 수익률: {pct(full['strategy_total_return'])}",
        f"- CAGR / MDD: {pct(full['strategy_cagr'])} / {pct(full['strategy_mdd'])}",
        f"- 500만원 예시: {krw(full['five_million_end_value'])}",
        "",
        "## 제한",
        "",
        "- v0.7과 3분할 규칙을 과거자료에서 선택한 뒤 계산한 결과이므로 순수 표본외 성과가 아니다.",
        "- 실제 사용자의 체결가격과 주문시간은 백테스트의 월요일 시가와 다를 수 있다.",
        "- 500만원 예시는 비율을 단순 적용한 값이며 세금은 제외한다.",
    ]
    return "\n".join(lines)


def run(
    *,
    refresh: bool,
    config_path: Path,
    start_date: str,
    initial_capital_krw: float,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    full_features, upbit_features, metadata = load_feature_frames(
        refresh=refresh,
        config_path=config_path,
        now_utc=now,
        start_date=start_date,
    )
    full = _latest_completed_episode(
        full_features,
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    upbit = _latest_completed_episode(
        upbit_features,
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    paths = resolve_paths(load_config(config_path))
    json_path = paths.output / "btc_v07_recent_halving.json"
    report_path = paths.output / "btc_v07_recent_halving.md"
    payload = {
        "schema_version": "btc-v07-recent-halving-1.0",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "full": full,
        "upbit": upbit,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    report_path.write_text(
        build_report(generated_at=payload["generated_at_utc"], full=full, upbit=upbit),
        encoding="utf-8-sig",
    )
    return {"payload": payload, "json": str(json_path), "report": str(report_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate latest halving episode for BTC v0.7 three-split")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--initial-capital-krw", type=float, default=DEFAULT_INITIAL_CAPITAL_KRW)
    args = parser.parse_args()
    result = run(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print(json.dumps(result["payload"], ensure_ascii=False, indent=2))
    print(f"보고서: {result['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
