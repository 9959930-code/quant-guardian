from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from btc_guardian import BtcDataError, candles_to_frame, fetch_json, fetch_text, fetch_upbit_history
from quant_guardian import fetch_yahoo_price


VERSION = "btc-clock-exit-compare-0.1"
END_DATE = date(2026, 8, 15)
AS_OF_UTC = datetime(2026, 8, 17, tzinfo=UTC)
UPBIT_START = date(2017, 9, 25)
BTCUSD_START = date(2014, 9, 17)
KST = ZoneInfo("Asia/Seoul")
CHECK_TIME = dt_time(9, 17)
INTERVAL = 210_000
ENTRY_OFFSET = 136_500
ENTRY_WATCH_OFFSET = 131_250
OLD_EXIT_FIRST_HEIGHT_OFFSET = 73_501  # current code uses progress > 0.35
NEW_EXIT_OFFSETS = (75_600, 77_700, 79_800)
ENTRY_TARGETS = (1 / 3, 2 / 3, 1.0)
EXIT_TARGETS = (2 / 3, 1 / 3, 0.0)
INITIAL_CAPITAL = 10_000_000.0
FEE_BPS = 5.0
SLIPPAGE_BPS = 10.0
OUTPUT = Path("output")


@dataclass(frozen=True)
class Action:
    policy: str
    cycle_epoch: int
    kind: str
    step: int
    action_date: date
    target_weight: float
    trigger_height: int
    trigger_time_utc: datetime
    reason: str


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_block_time(height: int) -> tuple[datetime, str]:
    errors: list[str] = []
    for provider, base in (
        ("blockstream.info", "https://blockstream.info/api"),
        ("mempool.space", "https://mempool.space/api"),
    ):
        try:
            block_hash = fetch_text(f"{base}/block-height/{height}", retries=3, pause=1.0).strip()
            payload = fetch_json(f"{base}/block/{block_hash}")
            return datetime.fromtimestamp(int(payload["timestamp"]), tz=UTC), provider
        except Exception as exc:  # pragma: no cover - live network fallback
            errors.append(f"{provider}: {exc}")
            time.sleep(0.2)
    raise BtcDataError(f"historical block lookup failed at {height}: {' | '.join(errors)}")


def trigger_heights(entry_epochs: Iterable[int]) -> list[int]:
    values: set[int] = set()
    for epoch in entry_epochs:
        values.add(epoch * INTERVAL + ENTRY_WATCH_OFFSET)
        values.add(epoch * INTERVAL + ENTRY_OFFSET)
        next_start = (epoch + 1) * INTERVAL
        values.add(next_start + 73_500)  # 35% warning only
        values.add(next_start + OLD_EXIT_FIRST_HEIGHT_OFFSET)
        values.update(next_start + offset for offset in NEW_EXIT_OFFSETS)
    return sorted(values)


def fetch_trigger_table(entry_epochs: Iterable[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for height in trigger_heights(entry_epochs):
        timestamp, provider = _fetch_block_time(height)
        rows.append(
            {
                "height": height,
                "timestamp_utc": timestamp,
                "provider": provider,
                "epoch": height // INTERVAL,
                "offset": height % INTERVAL,
                "progress": (height % INTERVAL) / INTERVAL,
            }
        )
    return pd.DataFrame(rows).sort_values("height").reset_index(drop=True)


def first_monday_check(timestamp_utc: datetime) -> date:
    local = timestamp_utc.astimezone(KST)
    current = local.date()
    if current.weekday() == 0:
        cutoff = datetime.combine(current, CHECK_TIME, tzinfo=KST)
        if local <= cutoff:
            return current
    days = (7 - current.weekday()) % 7
    return current + timedelta(days=days or 7)


def _time_for_height(table: pd.DataFrame, height: int) -> datetime:
    row = table.loc[table["height"] == height]
    if row.empty:
        raise KeyError(f"missing trigger height {height}")
    return pd.Timestamp(row.iloc[0]["timestamp_utc"]).to_pydatetime().astimezone(UTC)


def build_schedule(policy: str, epochs: Iterable[int], table: pd.DataFrame) -> list[Action]:
    if policy not in {"old_35_then_weekly", "new_36_37_38"}:
        raise ValueError(policy)
    actions: list[Action] = []
    for epoch in sorted(epochs):
        entry_height = epoch * INTERVAL + ENTRY_OFFSET
        entry_time = _time_for_height(table, entry_height)
        first_entry = first_monday_check(entry_time)
        for step, target in enumerate(ENTRY_TARGETS, start=1):
            actions.append(
                Action(
                    policy,
                    epoch,
                    "ENTRY",
                    step,
                    first_entry + timedelta(days=7 * (step - 1)),
                    target,
                    entry_height,
                    entry_time,
                    "65% 도달 1차 분할매수" if step == 1 else f"기존 {step}차 주간 분할매수",
                )
            )

        next_start = (epoch + 1) * INTERVAL
        if policy == "old_35_then_weekly":
            height = next_start + OLD_EXIT_FIRST_HEIGHT_OFFSET
            trigger_time = _time_for_height(table, height)
            first_exit = first_monday_check(trigger_time)
            for step, target in enumerate(EXIT_TARGETS, start=1):
                actions.append(
                    Action(
                        policy,
                        epoch,
                        "EXIT",
                        step,
                        first_exit + timedelta(days=7 * (step - 1)),
                        target,
                        height,
                        trigger_time,
                        "35% 초과 1차 분할매도" if step == 1 else f"기존 {step}차 주간 분할매도",
                    )
                )
        else:
            previous: date | None = None
            for step, (offset, target) in enumerate(zip(NEW_EXIT_OFFSETS, EXIT_TARGETS), start=1):
                height = next_start + offset
                trigger_time = _time_for_height(table, height)
                action_date = first_monday_check(trigger_time)
                if previous is not None and action_date <= previous:
                    action_date = previous + timedelta(days=7)
                previous = action_date
                progress = offset / INTERVAL
                actions.append(
                    Action(
                        policy,
                        epoch,
                        "EXIT",
                        step,
                        action_date,
                        target,
                        height,
                        trigger_time,
                        f"{progress:.0%} 도달 {'최종' if step == 3 else str(step) + '차'} 분할매도",
                    )
                )
    return sorted(actions, key=lambda x: (x.action_date, x.kind, x.step))


def _clean(frame: pd.DataFrame, start: date) -> pd.DataFrame:
    result = frame.copy().sort_index()
    result.index = pd.DatetimeIndex(result.index).tz_localize(None)
    for col in ("Open", "Close", "High", "Low", "Volume"):
        if col in result:
            result[col] = pd.to_numeric(result[col], errors="coerce")
    result = result.loc[pd.Timestamp(start) : pd.Timestamp(END_DATE)].dropna(subset=["Open", "Close"])
    if result.empty:
        raise BtcDataError("empty fixed research price frame")
    return result


def fetch_prices() -> dict[str, pd.DataFrame]:
    upbit = candles_to_frame(
        fetch_upbit_history(
            "KRW-BTC",
            UPBIT_START,
            as_of_utc=AS_OF_UTC,
            pause=0.12,
            max_pages=40,
        )
    )
    btcusd = fetch_yahoo_price("BTC-USD")
    return {
        "upbit_krw": _clean(upbit, UPBIT_START),
        "btc_usd": _clean(btcusd, BTCUSD_START),
    }


def _rebalance(
    cash: float,
    units: float,
    open_price: float,
    target: float,
) -> tuple[float, float, dict[str, float | str]]:
    fee_rate = FEE_BPS / 10_000
    slip_rate = SLIPPAGE_BPS / 10_000
    nav = cash + units * open_price
    desired_units = nav * target / open_price
    change = desired_units - units
    side = "NONE"
    fee = slip = notional = traded = 0.0
    if change > 1e-15:
        side = "BUY"
        execution = open_price * (1 + slip_rate)
        bought = min(change, cash / (execution * (1 + fee_rate)))
        notional = bought * execution
        fee = notional * fee_rate
        slip = bought * open_price * slip_rate
        cash -= notional + fee
        units += bought
        traded = bought
    elif change < -1e-15:
        side = "SELL"
        execution = open_price * (1 - slip_rate)
        sold = min(-change, units)
        notional = sold * execution
        fee = notional * fee_rate
        slip = sold * open_price * slip_rate
        cash += notional - fee
        units -= sold
        traded = -sold
    return max(cash, 0.0), max(units, 0.0), {
        "side": side,
        "fee": fee,
        "slippage": slip,
        "notional": notional,
        "traded_units": traded,
        "nav_before": nav,
    }


def simulate(
    prices: pd.DataFrame,
    schedule: Iterable[Action],
    *,
    market: str,
    policy: str,
    initial_capital: float = INITIAL_CAPITAL,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    action_map = {pd.Timestamp(action.action_date): action for action in schedule}
    cash = float(initial_capital)
    units = 0.0
    target = 0.0
    previous_equity = float(initial_capital)
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for index, row in prices.iterrows():
        open_price = float(row["Open"])
        close_price = float(row["Close"])
        fee = slip = 0.0
        action = action_map.get(pd.Timestamp(index))
        if action is not None:
            cash, units, trade = _rebalance(cash, units, open_price, action.target_weight)
            target = action.target_weight
            fee = float(trade["fee"])
            slip = float(trade["slippage"])
            trade_rows.append(
                {
                    "market": market,
                    "policy": policy,
                    "cycle_epoch": action.cycle_epoch,
                    "kind": action.kind,
                    "step": action.step,
                    "action_date": index.date().isoformat(),
                    "target_weight": action.target_weight,
                    "side": trade["side"],
                    "reference_open": open_price,
                    "notional": trade["notional"],
                    "fee_cost": fee,
                    "slippage_cost": slip,
                    "nav_before": trade["nav_before"],
                    "trigger_height": action.trigger_height,
                    "trigger_time_utc": action.trigger_time_utc.isoformat(),
                    "reason": action.reason,
                }
            )
        equity = cash + units * close_price
        weight = units * close_price / equity if equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "market": market,
                "policy": policy,
                "equity": equity,
                "cash": cash,
                "units": units,
                "actual_weight": weight,
                "target_weight": target,
                "daily_return": equity / previous_equity - 1 if previous_equity > 0 else 0.0,
                "fee_cost": fee,
                "slippage_cost": slip,
            }
        )
        previous_equity = equity

    daily = pd.DataFrame(daily_rows).set_index("Date")
    trades = pd.DataFrame(trade_rows)
    years = (daily.index[-1] - daily.index[0]).days / 365.25
    final = float(daily["equity"].iloc[-1])
    drawdown = daily["equity"] / daily["equity"].cummax() - 1
    metrics = {
        "market": market,
        "policy": policy,
        "start": daily.index[0].date().isoformat(),
        "end": daily.index[-1].date().isoformat(),
        "initial_capital": initial_capital,
        "final_equity": final,
        "total_return": final / initial_capital - 1,
        "cagr": (final / initial_capital) ** (1 / years) - 1,
        "mdd": float(drawdown.min()),
        "average_btc_weight": float(daily["actual_weight"].mean()),
        "invested_day_ratio": float((daily["actual_weight"] > 0.01).mean()),
        "trade_count": int(len(trades)),
        "fee_cost_total": float(trades["fee_cost"].sum()),
        "slippage_cost_total": float(trades["slippage_cost"].sum()),
    }
    return daily, trades, metrics


def cycle_table(
    prices: pd.DataFrame,
    schedules: Mapping[str, list[Action]],
    *,
    market: str,
    epochs: Iterable[int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for epoch in epochs:
        outcomes: dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]] = {}
        for policy, schedule in schedules.items():
            selected = [a for a in schedule if a.cycle_epoch == epoch]
            if len(selected) != 6:
                continue
            start = pd.Timestamp(min(a.action_date for a in selected)) - pd.Timedelta(days=1)
            end = pd.Timestamp(max(a.action_date for a in selected)) + pd.Timedelta(days=1)
            frame = prices.loc[start:end]
            if frame.empty:
                continue
            outcomes[policy] = simulate(frame, selected, market=market, policy=policy, initial_capital=1.0)
        if set(outcomes) != {"old_35_then_weekly", "new_36_37_38"}:
            continue
        old_daily, old_trades, old_metrics = outcomes["old_35_then_weekly"]
        new_daily, new_trades, new_metrics = outcomes["new_36_37_38"]
        del old_daily, new_daily
        rows.append(
            {
                "market": market,
                "cycle_epoch": epoch,
                "entry_dates": ",".join(old_trades.loc[old_trades["kind"] == "ENTRY", "action_date"]),
                "old_exit_dates": ",".join(old_trades.loc[old_trades["kind"] == "EXIT", "action_date"]),
                "new_exit_dates": ",".join(new_trades.loc[new_trades["kind"] == "EXIT", "action_date"]),
                "old_cycle_multiple": old_metrics["final_equity"],
                "new_cycle_multiple": new_metrics["final_equity"],
                "new_vs_old_pct": new_metrics["final_equity"] / old_metrics["final_equity"] - 1,
                "old_cycle_mdd": old_metrics["mdd"],
                "new_cycle_mdd": new_metrics["mdd"],
            }
        )
    return pd.DataFrame(rows)


def report_text(summary: pd.DataFrame, cycles: pd.DataFrame, triggers: pd.DataFrame) -> str:
    lines = [
        "# BTC 고정 6회 매도규칙 비교",
        "",
        f"- 기간 종료: {END_DATE.isoformat()}",
        "- 초기자금: 10,000,000원",
        f"- 편도비용: 수수료 {FEE_BPS:.1f}bp + 슬리피지 {SLIPPAGE_BPS:.1f}bp",
        "- 실행근사: 임계블록 이후 첫 월요일 09:17 KST, 당일 일봉 시가",
        "- 62.5% 관찰 및 35% 경고는 주문이 아니므로 수익률에 직접 영향 없음",
        "",
        "## 전체 결과",
        "",
        "| 시장 | 정책 | 최종액 | 총수익률 | CAGR | MDD | 평균 BTC 비중 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"| {row['market']} | {row['policy']} | {row['final_equity']:,.2f} | "
            f"{row['total_return']:.2%} | {row['cagr']:.2%} | {row['mdd']:.2%} | "
            f"{row['average_btc_weight']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 새 규칙 - 기존 규칙",
            "",
            "| 시장 | 최종액 차이 | 상대차이 | CAGR 차이 | MDD 차이 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for market, group in summary.groupby("market"):
        old = group.loc[group["policy"] == "old_35_then_weekly"].iloc[0]
        new = group.loc[group["policy"] == "new_36_37_38"].iloc[0]
        lines.append(
            f"| {market} | {new['final_equity'] - old['final_equity']:,.2f} | "
            f"{new['final_equity'] / old['final_equity'] - 1:+.2%} | "
            f"{(new['cagr'] - old['cagr']) * 100:+.2f}%p | "
            f"{(new['mdd'] - old['mdd']) * 100:+.2f}%p |"
        )
    lines.extend(
        [
            "",
            "## 사이클별 독립 비교",
            "",
            "| 시장 | 진입 epoch | 기존 배수 | 새 배수 | 새/기존 | 기존 매도일 | 새 매도일 |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for _, row in cycles.iterrows():
        lines.append(
            f"| {row['market']} | {int(row['cycle_epoch'])} | {row['old_cycle_multiple']:.4f} | "
            f"{row['new_cycle_multiple']:.4f} | {row['new_vs_old_pct']:+.2%} | "
            f"{row['old_exit_dates']} | {row['new_exit_dates']} |"
        )
    lines.extend(
        [
            "",
            "## 해석",
            "",
            "- 두 정책의 매수일과 매수비중은 동일하다.",
            "- 차이는 기존이 35% 직후부터 3주 연속 매도하고, 새 규칙이 36·37·38%까지 기다린다는 점뿐이다.",
            "- 35% 이후 상승 지속 시 새 규칙이 유리하고, 급락 시 불리하다.",
            "- 실제 수동 체결 지연, 장중 가격, 슬리피지에 따라 결과가 달라질 수 있다.",
            "- 표본은 Upbit 완결 사이클 2개와 BTC-USD 참고 사이클 3개로 작다.",
            f"- 임계블록 {len(triggers)}개를 {', '.join(sorted(set(triggers['provider'])))}에서 조회했다.",
        ]
    )
    return "\n".join(lines)


def run() -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    snapshot = OUTPUT / "btc_clock_exit_input_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    epochs = (1, 2, 3)
    triggers = fetch_trigger_table(epochs)
    triggers.to_csv(snapshot / "trigger_blocks.csv", index=False, encoding="utf-8")
    prices = fetch_prices()
    for market, frame in prices.items():
        frame.reset_index(names="Date").to_csv(snapshot / f"{market}.csv", index=False, encoding="utf-8")

    schedules = {
        policy: build_schedule(policy, epochs, triggers)
        for policy in ("old_35_then_weekly", "new_36_37_38")
    }
    metrics_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    daily_frames: list[pd.DataFrame] = []
    for market, frame in prices.items():
        for policy, schedule in schedules.items():
            daily, trades, metrics = simulate(frame, schedule, market=market, policy=policy)
            metrics_rows.append(metrics)
            trade_frames.append(trades)
            daily_frames.append(daily.reset_index())

    summary = pd.DataFrame(metrics_rows)
    trades = pd.concat(trade_frames, ignore_index=True)
    daily = pd.concat(daily_frames, ignore_index=True)
    cycles = pd.concat(
        [
            cycle_table(prices["upbit_krw"], schedules, market="upbit_krw", epochs=(2, 3)),
            cycle_table(prices["btc_usd"], schedules, market="btc_usd", epochs=(1, 2, 3)),
        ],
        ignore_index=True,
    )

    paths = {
        "summary": OUTPUT / "btc_clock_exit_summary.csv",
        "trades": OUTPUT / "btc_clock_exit_trades.csv",
        "daily": OUTPUT / "btc_clock_exit_daily.csv",
        "cycles": OUTPUT / "btc_clock_exit_cycles.csv",
        "report": OUTPUT / "btc_clock_exit_report.md",
        "manifest": OUTPUT / "btc_clock_exit_manifest.json",
    }
    summary.to_csv(paths["summary"], index=False, encoding="utf-8")
    trades.to_csv(paths["trades"], index=False, encoding="utf-8")
    daily.to_csv(paths["daily"], index=False, encoding="utf-8")
    cycles.to_csv(paths["cycles"], index=False, encoding="utf-8")
    paths["report"].write_text(report_text(summary, cycles, triggers), encoding="utf-8")

    manifest = {
        "version": VERSION,
        "end_date": END_DATE.isoformat(),
        "as_of_utc": AS_OF_UTC.isoformat(),
        "initial_capital": INITIAL_CAPITAL,
        "fee_bps": FEE_BPS,
        "slippage_bps": SLIPPAGE_BPS,
        "entry_progress": 0.65,
        "old_exit": "35% first qualifying Monday, then two consecutive Mondays",
        "new_exit": [0.36, 0.37, 0.38],
        "warning_only": {"entry_watch": 0.625, "exit_warning": 0.35},
        "input_files": [
            {"path": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in sorted(snapshot.iterdir())
        ],
        "outputs": {key: str(value) for key, value in paths.items()},
    }
    paths["manifest"].write_text(
        json.dumps(_json_ready(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(_json_ready({"manifest": manifest, "summary": metrics_rows}), ensure_ascii=False, indent=2))
    print(paths["report"].read_text(encoding="utf-8"))
    return {"manifest": manifest, "summary": metrics_rows}


def main() -> int:
    argparse.ArgumentParser(description="BTC fixed-six exit clock comparison").parse_args()
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
