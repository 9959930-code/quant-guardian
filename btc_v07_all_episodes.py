from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_guardian import DEFAULT_CONFIG, iso_utc, load_config, resolve_paths, utc_now
from btc_return_models import generate_return_decisions
from btc_v07_split_engine import simulate_three_split
from btc_v07_three_split_research import (
    BASELINE_CANDIDATE,
    DEFAULT_INITIAL_CAPITAL_KRW,
    DEFAULT_START_DATE,
    load_feature_frames,
)


def _metrics_for_episode(
    features: pd.DataFrame,
    simulation: Any,
    trades: pd.DataFrame,
    entry_signal_date: pd.Timestamp,
    exit_signal_date: pd.Timestamp,
    next_entry_signal: pd.Timestamp | None,
) -> dict[str, Any]:
    limit = next_entry_signal if next_entry_signal is not None else pd.Timestamp.max
    scoped = trades.loc[
        (trades["date"] > entry_signal_date) & (trades["date"] < limit)
    ].copy()
    buys = scoped.loc[
        (scoped["side"] == "BUY") & (scoped["date"] <= exit_signal_date)
    ]
    zero_sells = scoped.loc[
        (scoped["side"] == "SELL")
        & (scoped["date"] > exit_signal_date)
        & np.isclose(
            pd.to_numeric(scoped["final_target"], errors="coerce"),
            0.0,
            atol=1e-12,
        )
    ]
    if buys.empty or zero_sells.empty:
        raise RuntimeError(
            f"Incomplete episode {entry_signal_date.date()} -> {exit_signal_date.date()}"
        )
    first_entry = pd.Timestamp(buys["date"].min())
    final_exit = pd.Timestamp(zero_sells["date"].max())
    prior = simulation.daily.index[simulation.daily.index < first_entry]
    start_date = pd.Timestamp(prior.max()) if len(prior) else first_entry
    period = simulation.daily.loc[start_date:final_exit].copy()
    start_equity = float(period["equity"].iloc[0])
    end_equity = float(period["equity"].iloc[-1])
    total_return = end_equity / start_equity - 1
    elapsed_days = max(1, (final_exit - start_date).days)
    cagr = (end_equity / start_equity) ** (365 / elapsed_days) - 1
    mdd = float((period["equity"] / period["equity"].cummax() - 1).min())
    boundary = features.loc[entry_signal_date:exit_signal_date, "halving_epoch"]
    changes = boundary.index[pd.to_numeric(boundary, errors="coerce").diff().fillna(0) > 0]
    halving_date = pd.Timestamp(changes[0]) if len(changes) else None
    episode_trades = scoped.loc[
        (scoped["date"] >= first_entry) & (scoped["date"] <= final_exit)
    ]
    return {
        "entry_signal_date": entry_signal_date.date().isoformat(),
        "first_entry_date": first_entry.date().isoformat(),
        "halving_date": None if halving_date is None else halving_date.date().isoformat(),
        "exit_signal_date": exit_signal_date.date().isoformat(),
        "final_exit_date": final_exit.date().isoformat(),
        "elapsed_days": elapsed_days,
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "average_exposure": float(period["actual_weight"].mean()),
        "trade_count": int(len(episode_trades)),
        "five_million_end_value": 5_000_000 * (1 + total_return),
        "five_million_profit": 5_000_000 * total_return,
    }


def all_completed_episodes(
    features: pd.DataFrame,
    *,
    initial_capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> list[dict[str, Any]]:
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
    trades = simulation.trades.copy()
    trades["date"] = pd.to_datetime(trades["date"], errors="coerce")
    weekly = decisions.loc[decisions["is_decision_day"]].copy()
    weekly["previous_target"] = weekly["desired_weight"].shift(1).fillna(0.0)
    entries = list(
        weekly.index[
            (weekly["desired_weight"] > 0) & (weekly["previous_target"] <= 0)
        ]
    )
    exits = list(
        weekly.index[
            (weekly["desired_weight"] <= 0) & (weekly["previous_target"] > 0)
        ]
    )
    results: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        following_exits = [value for value in exits if value > entry]
        if not following_exits:
            continue
        exit_date = pd.Timestamp(following_exits[0])
        next_entries = [value for value in entries if value > exit_date]
        next_entry = pd.Timestamp(next_entries[0]) if next_entries else None
        results.append(
            _metrics_for_episode(
                features,
                simulation,
                trades,
                pd.Timestamp(entry),
                exit_date,
                next_entry,
            )
        )
    return results


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
    full = all_completed_episodes(
        full_features,
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    upbit = all_completed_episodes(
        upbit_features,
        initial_capital=initial_capital_krw,
        fee_bps=metadata["fee_bps"],
        slippage_bps=metadata["slippage_bps"],
    )
    payload = {
        "schema_version": "btc-v07-all-episodes-1.0",
        "generated_at_utc": iso_utc(now),
        "mode": "shadow-research",
        "auto_order": False,
        "full": full,
        "upbit": upbit,
    }
    paths = resolve_paths(load_config(config_path))
    output = paths.output / "btc_v07_all_episodes.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate all completed BTC v0.7 three-split episodes")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--initial-capital-krw", type=float, default=DEFAULT_INITIAL_CAPITAL_KRW)
    args = parser.parse_args()
    payload = run(
        refresh=args.refresh,
        config_path=args.config,
        start_date=args.start_date,
        initial_capital_krw=args.initial_capital_krw,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
