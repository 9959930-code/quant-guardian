from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

import btc_clock_exit_compare_research as research


MARKET_EPOCHS = {
    "upbit_krw": (2, 3),
    "btc_usd": (1, 2, 3),
}


def run() -> dict[str, Any]:
    research.OUTPUT.mkdir(parents=True, exist_ok=True)
    snapshot = research.OUTPUT / "btc_clock_exit_input_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)

    all_epochs = (1, 2, 3)
    triggers = research.fetch_trigger_table(all_epochs)
    triggers.to_csv(snapshot / "trigger_blocks.csv", index=False, encoding="utf-8")

    prices = research.fetch_prices()
    for market, frame in prices.items():
        frame.reset_index(names="Date").to_csv(
            snapshot / f"{market}.csv", index=False, encoding="utf-8"
        )

    full_schedules = {
        policy: research.build_schedule(policy, all_epochs, triggers)
        for policy in ("old_35_then_weekly", "new_36_37_38")
    }

    metrics_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    daily_frames: list[pd.DataFrame] = []
    market_schedules: dict[str, dict[str, list[research.Action]]] = {}

    for market, frame in prices.items():
        allowed = set(MARKET_EPOCHS[market])
        schedules = {
            policy: [action for action in schedule if action.cycle_epoch in allowed]
            for policy, schedule in full_schedules.items()
        }
        market_schedules[market] = schedules
        for policy, schedule in schedules.items():
            daily, trades, metrics = research.simulate(
                frame, schedule, market=market, policy=policy
            )
            metrics_rows.append(metrics)
            trade_frames.append(trades)
            daily_frames.append(daily.reset_index())

    summary = pd.DataFrame(metrics_rows)
    trades = pd.concat(trade_frames, ignore_index=True)
    daily = pd.concat(daily_frames, ignore_index=True)
    cycles = pd.concat(
        [
            research.cycle_table(
                prices[market],
                market_schedules[market],
                market=market,
                epochs=MARKET_EPOCHS[market],
            )
            for market in ("upbit_krw", "btc_usd")
        ],
        ignore_index=True,
    )

    paths = {
        "summary": research.OUTPUT / "btc_clock_exit_summary.csv",
        "trades": research.OUTPUT / "btc_clock_exit_trades.csv",
        "daily": research.OUTPUT / "btc_clock_exit_daily.csv",
        "cycles": research.OUTPUT / "btc_clock_exit_cycles.csv",
        "report": research.OUTPUT / "btc_clock_exit_report.md",
        "manifest": research.OUTPUT / "btc_clock_exit_manifest.json",
    }
    summary.to_csv(paths["summary"], index=False, encoding="utf-8")
    trades.to_csv(paths["trades"], index=False, encoding="utf-8")
    daily.to_csv(paths["daily"], index=False, encoding="utf-8")
    cycles.to_csv(paths["cycles"], index=False, encoding="utf-8")
    paths["report"].write_text(
        research.report_text(summary, cycles, triggers), encoding="utf-8"
    )

    manifest = {
        "version": research.VERSION,
        "end_date": research.END_DATE.isoformat(),
        "as_of_utc": research.AS_OF_UTC.isoformat(),
        "initial_capital": research.INITIAL_CAPITAL,
        "fee_bps": research.FEE_BPS,
        "slippage_bps": research.SLIPPAGE_BPS,
        "entry_progress": 0.65,
        "old_exit": "35% first qualifying Monday, then two consecutive Mondays",
        "new_exit": [0.36, 0.37, 0.38],
        "warning_only": {"entry_watch": 0.625, "exit_warning": 0.35},
        "market_epochs": {key: list(value) for key, value in MARKET_EPOCHS.items()},
        "input_files": [
            {
                "path": path.name,
                "sha256": research._sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(snapshot.iterdir())
        ],
        "outputs": {key: str(value) for key, value in paths.items()},
    }
    paths["manifest"].write_text(
        json.dumps(research._json_ready(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = {"manifest": manifest, "summary": metrics_rows}
    print(json.dumps(research._json_ready(payload), ensure_ascii=False, indent=2))
    print(paths["report"].read_text(encoding="utf-8"))
    return payload


if __name__ == "__main__":
    run()
