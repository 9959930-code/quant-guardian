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
from btc_research import build_feature_frame, load_coinmetrics_history
from btc_v07_three_split_research import _trade_to_weight


STRATEGY_VERSION = "btc-fixed-six-trade-halving-1.0"
FULL_START_DATE = "2014-09-17"
ENTRY_PROGRESS = 0.65
EXIT_PROGRESS = 0.35
INITIAL_CAPITAL_KRW = 10_000_000.0
TARGET_STEPS_ENTRY = (1 / 3, 2 / 3, 1.0)
TARGET_STEPS_EXIT = (2 / 3, 1 / 3, 0.0)


@dataclass(frozen=True)
class Episode:
    entry_signal_date: pd.Timestamp
    exit_signal_date: pd.Timestamp
    halving_date: pd.Timestamp


def load_raw_feature_frames(
    *,
    refresh: bool,
    config_path: Path,
    now_utc: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    config = load_config(config_path)
    btc_cfg = config.get("btc", {})
    if str(btc_cfg.get("run_mode", "shadow")) != "shadow":
        raise BtcDataError("Fixed six-trade research requires shadow mode")
    if bool(btc_cfg.get("auto_order", False)):
        raise BtcDataError("Fixed six-trade research refuses automatic orders")

    phase1 = build_phase1_report(
        refresh=refresh,
        config_path=config_path,
        now_utc=now_utc,
    )
    if phase1["data_gate"] != "pass":
        raise BtcDataError("Phase 1 data gate is blocked")

    onchain, fallback, onchain_error = load_coinmetrics_history(
        refresh=refresh,
        config=config,
        now_utc=now_utc,
    )
    paths = resolve_paths(config)
    data_cfg = btc_cfg.get("data", {})
    runtime = btc_cfg.get("research_runtime", {})
    cache_dir = ROOT / config.get("settings", {}).get("cache_dir", "data/cache")

    usd = closed_yahoo_daily_frame(
        read_price_cache(
            cache_dir
            / cache_key("yahoo", str(data_cfg.get("usd_symbol", "BTC-USD")))
        ),
        now_utc,
    )
    fx = closed_yahoo_daily_frame(
        read_price_cache(
            cache_dir
            / cache_key("yahoo", str(data_cfg.get("fx_symbol", "KRW=X")))
        ),
        now_utc,
    )
    synthetic = build_synthetic_krw_market(usd, fx)
    upbit = read_price_cache(
        paths.cache
        / f"upbit_{str(btc_cfg.get('execution_market', 'KRW-BTC')).replace('-', '_')}_daily.csv"
    )

    lag = int(runtime.get("onchain_lag_days", 2))
    minimum = int(runtime.get("percentile_min_periods", 730))
    full = build_feature_frame(
        synthetic,
        usd,
        fx,
        onchain,
        onchain_lag_days=lag,
        percentile_min_periods=minimum,
    )
    actual = build_feature_frame(
        upbit,
        usd,
        fx,
        onchain,
        onchain_lag_days=lag,
        percentile_min_periods=minimum,
    )

    required = ["open", "close", "block_height", "halving_epoch", "cycle_progress"]
    full = full.loc[pd.Timestamp(FULL_START_DATE) :].dropna(subset=required).copy()
    actual = actual.dropna(subset=required).copy()
    if full.empty or actual.empty:
        raise BtcDataError("Fixed six-trade research has no usable price rows")

    metadata = {
        "fee_bps": float(runtime.get("fee_bps", 5.0)),
        "slippage_bps": float(runtime.get("slippage_bps", 10.0)),
        "full_start": full.index.min().date().isoformat(),
        "full_end": full.index.max().date().isoformat(),
        "upbit_start": actual.index.min().date().isoformat(),
        "upbit_end": actual.index.max().date().isoformat(),
        "onchain_cache_fallback": fallback,
        "onchain_cache_error": onchain_error,
    }
    return full, actual, metadata


def find_completed_episodes(features: pd.DataFrame) -> list[Episode]:
    weekly = features.loc[features.index.dayofweek == 6].copy()
    if weekly.empty:
        raise ValueError("No Sunday rows in feature frame")
    progress = pd.to_numeric(weekly["cycle_progress"], errors="coerce")
    epoch = pd.to_numeric(weekly["halving_epoch"], errors="coerce")
    holding = (progress >= ENTRY_PROGRESS) | (progress <= EXIT_PROGRESS)
    previous_holding = holding.shift(1)

    entry_dates = list(
        weekly.index[
            (previous_holding == False)  # noqa: E712
            & holding
            & (progress >= ENTRY_PROGRESS)
        ]
    )
    exit_dates = list(
        weekly.index[
            (previous_holding == True)  # noqa: E712
            & (~holding)
            & (progress > EXIT_PROGRESS)
        ]
    )

    changes = weekly.index[epoch.diff().fillna(0) > 0]
    episodes: list[Episode] = []
    for entry in entry_dates:
        exits = [value for value in exit_dates if value > entry]
        if not exits:
            continue
        exit_date = pd.Timestamp(exits[0])
        halvings = [value for value in changes if entry < value < exit_date]
        if not halvings:
            continue
        episodes.append(
            Episode(
                entry_signal_date=pd.Timestamp(entry),
                exit_signal_date=exit_date,
                halving_date=pd.Timestamp(halvings[0]),
            )
        )
    return episodes


def _next_index_on_or_after(index: pd.DatetimeIndex, target: pd.Timestamp) -> pd.Timestamp:
    position = index.searchsorted(target, side="left")
    if position >= len(index):
        raise ValueError(f"No price row on or after {target.date()}")
    return pd.Timestamp(index[position])


def execution_dates(
    index: pd.DatetimeIndex,
    signal_date: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    first = _next_index_on_or_after(index, signal_date + pd.Timedelta(days=1))
    second = _next_index_on_or_after(index, first + pd.Timedelta(days=7))
    third = _next_index_on_or_after(index, second + pd.Timedelta(days=7))
    return first, second, third


def simulate_episode(
    features: pd.DataFrame,
    episode: Episode,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    entry_dates = execution_dates(features.index, episode.entry_signal_date)
    exit_dates = execution_dates(features.index, episode.exit_signal_date)
    if entry_dates[-1] >= exit_dates[0]:
        raise ValueError("Entry completion overlaps exit start")

    prior = features.index[features.index < entry_dates[0]]
    mark_start = pd.Timestamp(prior.max()) if len(prior) else entry_dates[0]
    end_date = exit_dates[-1]
    period = features.loc[mark_start:end_date].copy()
    if period.empty:
        raise ValueError("Episode period is empty")

    targets: dict[pd.Timestamp, float] = {
        **dict(zip(entry_dates, TARGET_STEPS_ENTRY, strict=True)),
        **dict(zip(exit_dates, TARGET_STEPS_EXIT, strict=True)),
    }
    fee_rate = fee_bps / 10_000
    slippage_rate = slippage_bps / 10_000
    cash = float(initial_capital_krw)
    units = 0.0
    previous_equity = float(initial_capital_krw)
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for index, row in period.iterrows():
        open_price = float(row["open"])
        close_price = float(row["close"])
        if not math.isfinite(open_price) or open_price <= 0:
            raise ValueError(f"Invalid open price on {index}")
        if not math.isfinite(close_price) or close_price <= 0:
            raise ValueError(f"Invalid close price on {index}")

        if index in targets:
            target = targets[index]
            cash, units, trade = _trade_to_weight(
                cash=cash,
                units=units,
                open_price=open_price,
                target_weight=target,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
            )
            side = str(trade["side"])
            if side:
                trade_rows.append(
                    {
                        "date": index,
                        "side": side,
                        "target_weight": target,
                        "units": abs(float(trade["traded_units"])),
                        "open_price": open_price,
                        "fee_cost": float(trade["fee_cost"]),
                        "slippage_cost": float(trade["slippage_cost"]),
                    }
                )

        equity = cash + units * close_price
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "equity": equity,
                "cash": cash,
                "btc_units": units,
                "actual_weight": actual_weight,
                "daily_return": daily_return,
            }
        )
        previous_equity = equity

    daily = pd.DataFrame(daily_rows).set_index("Date")
    trades = pd.DataFrame(trade_rows)
    if len(trades) != 6:
        raise RuntimeError(f"Expected exactly six trades, got {len(trades)}")
    if units > 1e-10:
        raise RuntimeError("Final BTC units are not zero")

    start_equity = float(daily["equity"].iloc[0])
    end_equity = float(daily["equity"].iloc[-1])
    total_return = end_equity / start_equity - 1
    elapsed_days = max(1, (end_date - mark_start).days)
    cagr = (end_equity / start_equity) ** (365 / elapsed_days) - 1
    mdd = float((daily["equity"] / daily["equity"].cummax() - 1).min())

    buys = trades.loc[trades["side"] == "BUY"]
    sells = trades.loc[trades["side"] == "SELL"]

    def weighted_price(frame: pd.DataFrame) -> float:
        units_series = pd.to_numeric(frame["units"], errors="coerce")
        prices = pd.to_numeric(frame["open_price"], errors="coerce")
        return float((units_series * prices).sum() / units_series.sum())

    first_open = float(features.loc[entry_dates[0], "open"])
    final_open = float(features.loc[exit_dates[-1], "open"])
    return {
        "entry_signal_date": episode.entry_signal_date.date().isoformat(),
        "entry_dates": [value.date().isoformat() for value in entry_dates],
        "halving_date": episode.halving_date.date().isoformat(),
        "exit_signal_date": episode.exit_signal_date.date().isoformat(),
        "exit_dates": [value.date().isoformat() for value in exit_dates],
        "elapsed_days": elapsed_days,
        "trade_count": len(trades),
        "total_return": total_return,
        "capital_multiple": end_equity / start_equity,
        "cagr": cagr,
        "mdd": mdd,
        "average_exposure": float(daily["actual_weight"].mean()),
        "weighted_average_buy_price": weighted_price(buys),
        "weighted_average_sell_price": weighted_price(sells),
        "btc_return_first_entry_to_final_exit": final_open / first_open - 1,
        "initial_capital_krw": initial_capital_krw,
        "terminal_wealth_krw": end_equity,
        "profit_krw": end_equity - initial_capital_krw,
        "five_million_end_value": 5_000_000 * (1 + total_return),
        "five_million_profit": 5_000_000 * total_return,
        "fee_cost": float(trades["fee_cost"].sum()),
        "slippage_cost": float(trades["slippage_cost"].sum()),
    }


def evaluate_all(
    features: pd.DataFrame,
    *,
    initial_capital_krw: float,
    fee_bps: float,
    slippage_bps: float,
) -> list[dict[str, Any]]:
    return [
        simulate_episode(
            features,
            episode,
            initial_capital_krw=initial_capital_krw,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        for episode in find_completed_episodes(features)
    ]


def build_report(payload: Mapping[str, Any]) -> str:
    def pct(value: Any) -> str:
        return f"{float(value) * 100:.2f}%"

    def krw(value: Any) -> str:
        return f"{float(value):,.0f}원"

    lines = [
        "# BTC 반감기 고정 6회 거래 전략 결과",
        "",
        f"- 생성시각: {payload['generated_at_utc']}",
        "- 진입: 반감기 진행률 65% 도달 후 3주간 33.3%→66.7%→100%",
        "- 보유: 중간 리밸런싱 없음",
        "- 청산: 다음 반감기 후 진행률 35% 초과 시 3주간 66.7%→33.3%→0%",
        "- 비용: 수수료 5bps + 슬리피지 10bps",
        "- 주문 수: 사이클당 정확히 6회",
        "",
        "## 실제 Upbit 가격 기준",
        "",
        "| 반감기 | 진입 | 최종청산 | 누적수익률 | CAGR | MDD | 500만원 최종액 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in payload["upbit"]:
        lines.append(
            f"| {row['halving_date'][:4]} | {row['entry_dates'][0]} | "
            f"{row['exit_dates'][-1]} | {pct(row['total_return'])} | "
            f"{pct(row['cagr'])} | {pct(row['mdd'])} | "
            f"{krw(row['five_million_end_value'])} |"
        )

    lines.extend(
        [
            "",
            "## 합성 원화가격 기준",
            "",
            "| 반감기 | 진입 | 최종청산 | 누적수익률 | CAGR | MDD | 500만원 최종액 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in payload["full"]:
        lines.append(
            f"| {row['halving_date'][:4]} | {row['entry_dates'][0]} | "
            f"{row['exit_dates'][-1]} | {pct(row['total_return'])} | "
            f"{pct(row['cagr'])} | {pct(row['mdd'])} | "
            f"{krw(row['five_million_end_value'])} |"
        )

    lines.extend(
        [
            "",
            "## 해석 제한",
            "",
            "- 2016 반감기는 Upbit 실제가격 이력이 부족해 합성 원화가격을 사용한다.",
            "- 반감기 임계값 65%·35%와 3주 분할은 과거자료를 본 뒤 정한 규칙이므로 선택 편향이 있다.",
            "- 중간 리밸런싱이 없기 때문에 급락 시 BTC 100% 노출을 유지하고 MDD가 커질 수 있다.",
            "- 세금은 제외하며 미래 수익률을 보장하지 않는다.",
        ]
    )
    return "\n".join(lines)


def run_research(
    *,
    refresh: bool,
    config_path: Path,
    initial_capital_krw: float,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    now = now_utc or utc_now()
    full, upbit, metadata = load_raw_feature_frames(
        refresh=refresh,
        config_path=config_path,
        now_utc=now,
    )
    payload = {
        "schema_version": "btc-fixed-six-trade-1.0",
        "strategy_version": STRATEGY_VERSION,
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "definition": {
            "entry_progress": ENTRY_PROGRESS,
            "exit_progress": EXIT_PROGRESS,
            "entry_target_weights": list(TARGET_STEPS_ENTRY),
            "exit_target_weights": list(TARGET_STEPS_EXIT),
            "intermediate_rebalancing": False,
            "trades_per_completed_cycle": 6,
        },
        "metadata": metadata,
        "full": evaluate_all(
            full,
            initial_capital_krw=initial_capital_krw,
            fee_bps=metadata["fee_bps"],
            slippage_bps=metadata["slippage_bps"],
        ),
        "upbit": evaluate_all(
            upbit,
            initial_capital_krw=initial_capital_krw,
            fee_bps=metadata["fee_bps"],
            slippage_bps=metadata["slippage_bps"],
        ),
    }
    paths = resolve_paths(load_config(config_path))
    json_path = paths.output / "btc_fixed_six_trade_results.json"
    report_path = paths.output / "btc_fixed_six_trade_report.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    report_path.write_text(build_report(payload), encoding="utf-8-sig")
    return {
        "payload": payload,
        "json": str(json_path),
        "report": str(report_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest fixed six-trade BTC halving strategy"
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--initial-capital-krw",
        type=float,
        default=INITIAL_CAPITAL_KRW,
    )
    args = parser.parse_args()
    result = run_research(
        refresh=args.refresh,
        config_path=args.config,
        initial_capital_krw=args.initial_capital_krw,
    )
    print(json.dumps(result["payload"], ensure_ascii=False, indent=2))
    print(f"보고서: {result['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
