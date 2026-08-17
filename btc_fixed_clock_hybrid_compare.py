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

import numpy as np
import pandas as pd

from btc_guardian import (
    BtcDataError,
    candles_to_frame,
    fetch_json,
    fetch_text,
    fetch_upbit_history,
)
from quant_guardian import fetch_yahoo_price


RESEARCH_VERSION = "btc-fixed-clock-hybrid-compare-0.1"
FIXED_END = date(2026, 8, 15)
AS_OF_UTC = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
UPBIT_START = date(2017, 9, 25)
BTCUSD_START = date(2014, 9, 17)
KST = UTC + timedelta(hours=9)
OFFICIAL_CHECK = dt_time(9, 17)
HALVING_INTERVAL = 210_000
INITIAL_CAPITAL_KRW = 10_000_000.0
FEE_BPS = 5.0
SLIPPAGE_BPS = 10.0
ENTRY_OFFSET = round(HALVING_INTERVAL * 0.65)
ENTRY_WATCH_OFFSET = round(HALVING_INTERVAL * 0.625)
OLD_EXIT_OFFSET_FIRST_QUALIFYING = round(HALVING_INTERVAL * 0.35) + 1
NEW_EXIT_OFFSETS = tuple(round(HALVING_INTERVAL * value) for value in (0.36, 0.37, 0.38))
ENTRY_TARGETS = (1 / 3, 2 / 3, 1.0)
EXIT_TARGETS = (2 / 3, 1 / 3, 0.0)
OUTPUT_DIR = Path("output")


@dataclass(frozen=True)
class ScheduledAction:
    policy: str
    cycle_epoch: int
    kind: str
    step: int
    action_date: date
    target_weight: float
    trigger_height: int
    trigger_time_utc: datetime
    trigger_progress: float
    reason: str


@dataclass
class SimulationResult:
    policy: str
    market: str
    daily: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_ready(dict(payload)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_block_timestamp(height: int) -> tuple[datetime, str]:
    errors: list[str] = []
    for provider, base in (
        ("blockstream.info", "https://blockstream.info/api"),
        ("mempool.space", "https://mempool.space/api"),
    ):
        try:
            block_hash = fetch_text(f"{base}/block-height/{height}", retries=3, pause=1.0).strip()
            if not block_hash:
                raise BtcDataError("empty block hash")
            payload = fetch_json(f"{base}/block/{block_hash}")
            timestamp = int(payload["timestamp"])
            return datetime.fromtimestamp(timestamp, tz=UTC), provider
        except Exception as exc:  # pragma: no cover - exercised by live research
            errors.append(f"{provider}: {exc}")
            time.sleep(0.2)
    raise BtcDataError(f"Could not resolve historical block {height}: {' | '.join(errors)}")


def required_trigger_heights(entry_epochs: Iterable[int]) -> list[int]:
    heights: set[int] = set()
    for epoch in entry_epochs:
        heights.add(epoch * HALVING_INTERVAL + ENTRY_WATCH_OFFSET)
        heights.add(epoch * HALVING_INTERVAL + ENTRY_OFFSET)
        next_start = (epoch + 1) * HALVING_INTERVAL
        heights.add(next_start + round(HALVING_INTERVAL * 0.35))
        heights.add(next_start + OLD_EXIT_OFFSET_FIRST_QUALIFYING)
        heights.update(next_start + offset for offset in NEW_EXIT_OFFSETS)
    return sorted(heights)


def fetch_trigger_table(entry_epochs: Iterable[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for height in required_trigger_heights(entry_epochs):
        timestamp, provider = _fetch_block_timestamp(height)
        rows.append(
            {
                "height": height,
                "timestamp_utc": timestamp,
                "provider": provider,
                "epoch": height // HALVING_INTERVAL,
                "offset": height % HALVING_INTERVAL,
                "cycle_progress": (height % HALVING_INTERVAL) / HALVING_INTERVAL,
            }
        )
    return pd.DataFrame(rows).sort_values("height").reset_index(drop=True)


def first_official_monday(trigger_time_utc: datetime) -> date:
    local = trigger_time_utc.astimezone(KST)
    current = local.date()
    if current.weekday() == 0:
        official = datetime.combine(current, OFFICIAL_CHECK, tzinfo=KST)
        if local <= official:
            return current
    days = (7 - current.weekday()) % 7
    if days == 0:
        days = 7
    return current + timedelta(days=days)


def _height_timestamp(trigger_table: pd.DataFrame, height: int) -> datetime:
    matched = trigger_table.loc[trigger_table["height"] == height, "timestamp_utc"]
    if matched.empty:
        raise KeyError(f"Missing trigger height {height}")
    return pd.Timestamp(matched.iloc[0]).to_pydatetime().astimezone(UTC)


def build_schedule(
    policy: str,
    entry_epochs: Iterable[int],
    trigger_table: pd.DataFrame,
) -> list[ScheduledAction]:
    if policy not in {"old_35_then_weekly", "new_36_37_38"}:
        raise ValueError(f"Unsupported policy: {policy}")
    actions: list[ScheduledAction] = []
    for epoch in sorted(entry_epochs):
        entry_height = epoch * HALVING_INTERVAL + ENTRY_OFFSET
        entry_time = _height_timestamp(trigger_table, entry_height)
        first_entry = first_official_monday(entry_time)
        for step, target in enumerate(ENTRY_TARGETS, start=1):
            action_date = first_entry + timedelta(days=7 * (step - 1))
            actions.append(
                ScheduledAction(
                    policy=policy,
                    cycle_epoch=epoch,
                    kind="ENTRY",
                    step=step,
                    action_date=action_date,
                    target_weight=target,
                    trigger_height=entry_height,
                    trigger_time_utc=entry_time,
                    trigger_progress=0.65,
                    reason=(
                        "반감기 진행률 65% 도달 후 1차 분할매수"
                        if step == 1
                        else f"고정 6회 전략 {step}차 분할매수"
                    ),
                )
            )

        next_start = (epoch + 1) * HALVING_INTERVAL
        if policy == "old_35_then_weekly":
            trigger_height = next_start + OLD_EXIT_OFFSET_FIRST_QUALIFYING
            trigger_time = _height_timestamp(trigger_table, trigger_height)
            first_exit = first_official_monday(trigger_time)
            for step, target in enumerate(EXIT_TARGETS, start=1):
                actions.append(
                    ScheduledAction(
                        policy=policy,
                        cycle_epoch=epoch,
                        kind="EXIT",
                        step=step,
                        action_date=first_exit + timedelta(days=7 * (step - 1)),
                        target_weight=target,
                        trigger_height=trigger_height,
                        trigger_time_utc=trigger_time,
                        trigger_progress=trigger_height % HALVING_INTERVAL / HALVING_INTERVAL,
                        reason=(
                            "다음 반감기 후 진행률 35% 초과에 따른 1차 분할매도"
                            if step == 1
                            else f"고정 6회 전략 {step}차 분할매도"
                        ),
                    )
                )
        else:
            previous_date: date | None = None
            for step, (offset, target) in enumerate(zip(NEW_EXIT_OFFSETS, EXIT_TARGETS), start=1):
                trigger_height = next_start + offset
                trigger_time = _height_timestamp(trigger_table, trigger_height)
                raw_date = first_official_monday(trigger_time)
                action_date = raw_date
                if previous_date is not None and action_date <= previous_date:
                    action_date = previous_date + timedelta(days=7)
                previous_date = action_date
                threshold = offset / HALVING_INTERVAL
                actions.append(
                    ScheduledAction(
                        policy=policy,
                        cycle_epoch=epoch,
                        kind="EXIT",
                        step=step,
                        action_date=action_date,
                        target_weight=target,
                        trigger_height=trigger_height,
                        trigger_time_utc=trigger_time,
                        trigger_progress=threshold,
                        reason=(
                            f"반감기 진행률 {threshold * 100:.0f}% 도달에 따른 "
                            + ("최종 분할매도" if step == 3 else f"{step}차 분할매도")
                        ),
                    )
                )
    return sorted(actions, key=lambda item: (item.action_date, item.kind, item.step))


def _clean_price_frame(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    output = frame.copy().sort_index()
    output.index = pd.DatetimeIndex(output.index).tz_localize(None)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in output:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.loc[pd.Timestamp(start) : pd.Timestamp(end)]
    output = output.dropna(subset=["Open", "Close"])
    if output.empty:
        raise BtcDataError("Price frame is empty after fixed-date filtering")
    return output


def fetch_price_inputs() -> dict[str, pd.DataFrame]:
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
        "upbit_krw": _clean_price_frame(upbit, UPBIT_START, FIXED_END),
        "btc_usd": _clean_price_frame(btcusd, BTCUSD_START, FIXED_END),
    }


def _trade_to_target(
    *,
    cash: float,
    units: float,
    open_price: float,
    target_weight: float,
    fee_rate: float,
    slippage_rate: float,
) -> tuple[float, float, dict[str, float | str]]:
    nav = cash + units * open_price
    target = min(1.0, max(0.0, float(target_weight)))
    desired_units = nav * target / open_price
    change = desired_units - units
    side = "NONE"
    notional = 0.0
    fee_cost = 0.0
    slippage_cost = 0.0
    traded_units = 0.0

    if change > 1e-15:
        side = "BUY"
        execution_price = open_price * (1 + slippage_rate)
        max_units = cash / (execution_price * (1 + fee_rate))
        bought = min(change, max_units)
        notional = bought * execution_price
        fee_cost = notional * fee_rate
        slippage_cost = bought * open_price * slippage_rate
        cash -= notional + fee_cost
        units += bought
        traded_units = bought
    elif change < -1e-15:
        side = "SELL"
        execution_price = open_price * (1 - slippage_rate)
        sold = min(-change, units)
        notional = sold * execution_price
        fee_cost = notional * fee_rate
        slippage_cost = sold * open_price * slippage_rate
        cash += notional - fee_cost
        units -= sold
        traded_units = -sold

    return max(cash, 0.0), max(units, 0.0), {
        "side": side,
        "traded_units": traded_units,
        "notional": notional,
        "fee_cost": fee_cost,
        "slippage_cost": slippage_cost,
        "nav_before": nav,
        "nav_after_open": cash + units * open_price,
    }


def simulate(
    prices: pd.DataFrame,
    schedule: Iterable[ScheduledAction],
    *,
    policy: str,
    market: str,
    initial_capital: float = INITIAL_CAPITAL_KRW,
) -> SimulationResult:
    action_map = {pd.Timestamp(item.action_date): item for item in schedule}
    fee_rate = FEE_BPS / 10_000
    slippage_rate = SLIPPAGE_BPS / 10_000
    cash = float(initial_capital)
    units = 0.0
    last_target = 0.0
    previous_equity = float(initial_capital)
    daily_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for index, row in prices.iterrows():
        open_price = float(row["Open"])
        close_price = float(row["Close"])
        fee_cost = 0.0
        slippage_cost = 0.0
        action = action_map.get(pd.Timestamp(index))
        if action is not None:
            cash, units, trade = _trade_to_target(
                cash=cash,
                units=units,
                open_price=open_price,
                target_weight=action.target_weight,
                fee_rate=fee_rate,
                slippage_rate=slippage_rate,
            )
            fee_cost = float(trade["fee_cost"])
            slippage_cost = float(trade["slippage_cost"])
            last_target = action.target_weight
            trade_rows.append(
                {
                    "market": market,
                    "policy": policy,
                    "cycle_epoch": action.cycle_epoch,
                    "kind": action.kind,
                    "step": action.step,
                    "action_date": pd.Timestamp(index).date().isoformat(),
                    "target_weight": action.target_weight,
                    "side": trade["side"],
                    "reference_open": open_price,
                    "traded_units": trade["traded_units"],
                    "notional": trade["notional"],
                    "fee_cost": trade["fee_cost"],
                    "slippage_cost": trade["slippage_cost"],
                    "nav_before": trade["nav_before"],
                    "nav_after_open": trade["nav_after_open"],
                    "trigger_height": action.trigger_height,
                    "trigger_time_utc": action.trigger_time_utc.isoformat(),
                    "trigger_progress": action.trigger_progress,
                    "reason": action.reason,
                }
            )

        equity = cash + units * close_price
        actual_weight = units * close_price / equity if equity > 0 else 0.0
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        daily_rows.append(
            {
                "Date": index,
                "market": market,
                "policy": policy,
                "open": open_price,
                "close": close_price,
                "cash": cash,
                "units": units,
                "equity": equity,
                "daily_return": daily_return,
                "actual_weight": actual_weight,
                "target_weight": last_target,
                "fee_cost": fee_cost,
                "slippage_cost": slippage_cost,
            }
        )
        previous_equity = equity

    daily = pd.DataFrame(daily_rows).set_index("Date")
    trades = pd.DataFrame(trade_rows)
    years = max(1e-12, (daily.index[-1] - daily.index[0]).days / 365.25)
    drawdown = daily["equity"] / daily["equity"].cummax() - 1.0
    metrics = {
        "market": market,
        "policy": policy,
        "start": daily.index[0].date().isoformat(),
        "end": daily.index[-1].date().isoformat(),
        "initial_capital": float(initial_capital),
        "final_equity": float(daily["equity"].iloc[-1]),
        "total_return": float(daily["equity"].iloc[-1] / initial_capital - 1.0),
        "cagr": float((daily["equity"].iloc[-1] / initial_capital) ** (1 / years) - 1.0),
        "mdd": float(drawdown.min()),
        "average_btc_weight": float(daily["actual_weight"].mean()),
        "invested_day_ratio": float((daily["actual_weight"] > 0.01).mean()),
        "trade_count": int(len(trades)),
        "fee_cost_total": float(trades["fee_cost"].sum()) if not trades.empty else 0.0,
        "slippage_cost_total": float(trades["slippage_cost"].sum()) if not trades.empty else 0.0,
    }
    return SimulationResult(policy=policy, market=market, daily=daily, trades=trades, metrics=metrics)


def cycle_results(
    prices: pd.DataFrame,
    schedules: Mapping[str, list[ScheduledAction]],
    *,
    market: str,
    epochs: Iterable[int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for epoch in epochs:
        policy_results: dict[str, SimulationResult] = {}
        for policy, schedule in schedules.items():
            cycle_schedule = [item for item in schedule if item.cycle_epoch == epoch]
            if len(cycle_schedule) != 6:
                continue
            start = pd.Timestamp(min(item.action_date for item in cycle_schedule)) - pd.Timedelta(days=1)
            end = pd.Timestamp(max(item.action_date for item in cycle_schedule)) + pd.Timedelta(days=1)
            sliced = prices.loc[start:end]
            if len(sliced) < 10:
                continue
            policy_results[policy] = simulate(
                sliced,
                cycle_schedule,
                policy=policy,
                market=f"{market}_cycle_{epoch}",
                initial_capital=1.0,
            )
        if set(policy_results) != {"old_35_then_weekly", "new_36_37_38"}:
            continue
        old = policy_results["old_35_then_weekly"]
        new = policy_results["new_36_37_38"]
        old_entry = old.trades.loc[old.trades["kind"] == "ENTRY", "action_date"].tolist()
        old_exit = old.trades.loc[old.trades["kind"] == "EXIT", "action_date"].tolist()
        new_exit = new.trades.loc[new.trades["kind"] == "EXIT", "action_date"].tolist()
        rows.append(
            {
                "market": market,
                "cycle_epoch": epoch,
                "entry_dates": ",".join(old_entry),
                "old_exit_dates": ",".join(old_exit),
                "new_exit_dates": ",".join(new_exit),
                "old_cycle_multiple": old.metrics["final_equity"],
                "new_cycle_multiple": new.metrics["final_equity"],
                "new_minus_old_multiple": new.metrics["final_equity"] - old.metrics["final_equity"],
                "new_vs_old_pct": new.metrics["final_equity"] / old.metrics["final_equity"] - 1.0,
                "old_cycle_mdd": old.metrics["mdd"],
                "new_cycle_mdd": new.metrics["mdd"],
            }
        )
    return pd.DataFrame(rows)


def build_report(
    summary: pd.DataFrame,
    cycles: pd.DataFrame,
    triggers: pd.DataFrame,
) -> str:
    lines = [
        "# BTC 고정 6회: 기존 35% 후 주간매도 vs 36·37·38% 임계매도",
        "",
        f"- 연구 버전: `{RESEARCH_VERSION}`",
        f"- 고정 종료일: `{FIXED_END.isoformat()}`",
        "- 초기자금: 10,000,000원",
        f"- 비용: 수수료 {FEE_BPS:.1f}bp + 슬리피지 {SLIPPAGE_BPS:.1f}bp (편도)",
        "- 주문시점: 임계블록 이후 첫 월요일 09:17 KST, 해당 일 Upbit/Yahoo 일봉 시가 근사",
        "- 수동 주문·잔고동기화는 당일 완료된 이상적 비교",
        "- 62.5% 관찰 알림과 35% 경고 알림은 주문이 아니므로 수익률에 직접 영향 없음",
        "",
        "## 1. 전체기간 결과",
        "",
        "| 시장 | 정책 | 최종액 | 총수익률 | CAGR | MDD | 평균 BTC 비중 | 거래수 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in summary.iterrows():
        unit = "원" if row["market"] == "upbit_krw" else "지수"
        lines.append(
            f"| {row['market']} | {row['policy']} | {row['final_equity']:,.2f}{unit} | "
            f"{row['total_return']:.2%} | {row['cagr']:.2%} | {row['mdd']:.2%} | "
            f"{row['average_btc_weight']:.2%} | {int(row['trade_count'])} |"
        )

    lines.extend(
        [
            "",
            "## 2. 정책 차이",
            "",
            "| 시장 | 새 정책 최종액 차이 | 상대차이 | CAGR 차이 | MDD 차이 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for market, group in summary.groupby("market"):
        old = group.loc[group["policy"] == "old_35_then_weekly"].iloc[0]
        new = group.loc[group["policy"] == "new_36_37_38"].iloc[0]
        lines.append(
            f"| {market} | {new['final_equity'] - old['final_equity']:,.2f} | "
            f"{new['final_equity'] / old['final_equity'] - 1:.2%} | "
            f"{new['cagr'] - old['cagr']:+.2%p} | {new['mdd'] - old['mdd']:+.2%p} |"
        )

    lines.extend(
        [
            "",
            "## 3. 사이클별 독립 비교",
            "",
            "각 사이클 시작자금을 1로 다시 놓고 동일한 매수일 이후 매도규칙만 비교했다.",
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
            "## 4. 해석 원칙",
            "",
            "- 매수 규칙은 두 정책이 완전히 같으므로 결과 차이는 매도시점에서만 발생한다.",
            "- 새 정책은 35%에서 주문하지 않고 36·37·38%까지 더 오래 보유한다.",
            "- 상승이 이어진 사이클에서는 유리할 수 있지만, 35% 이후 급락한 사이클에서는 불리하다.",
            "- 실제 수동주문이 며칠 늦어지면 결과가 달라질 수 있다.",
            "- 표본은 Upbit 완결 사이클 2개, BTC-USD 참고구간 3개로 작다.",
            "",
            "## 5. 임계블록 소스",
            "",
            f"- 조회한 임계블록: {len(triggers)}개",
            f"- 공급자: {', '.join(sorted(set(triggers['provider'])))}",
        ]
    )
    return "\n".join(lines)


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = OUTPUT_DIR / "btc_clock_hybrid_input_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)

    entry_epochs = (1, 2, 3)
    trigger_table = fetch_trigger_table(entry_epochs)
    trigger_table.to_csv(snapshot / "trigger_blocks.csv", index=False, encoding="utf-8")

    prices = fetch_price_inputs()
    prices["upbit_krw"].reset_index(names="Date").to_csv(
        snapshot / "upbit_krw_btc.csv", index=False, encoding="utf-8"
    )
    prices["btc_usd"].reset_index(names="Date").to_csv(
        snapshot / "yahoo_btc_usd.csv", index=False, encoding="utf-8"
    )

    schedules = {
        policy: build_schedule(policy, entry_epochs, trigger_table)
        for policy in ("old_35_then_weekly", "new_36_37_38")
    }

    results: list[SimulationResult] = []
    all_trades: list[pd.DataFrame] = []
    all_daily: list[pd.DataFrame] = []
    for market, frame in prices.items():
        for policy, schedule in schedules.items():
            result = simulate(frame, schedule, policy=policy, market=market)
            results.append(result)
            all_trades.append(result.trades)
            daily = result.daily.reset_index()
            all_daily.append(daily)

    summary = pd.DataFrame([result.metrics for result in results])
    trades = pd.concat(all_trades, ignore_index=True)
    daily = pd.concat(all_daily, ignore_index=True)
    cycles = pd.concat(
        [
            cycle_results(
                prices["upbit_krw"], schedules, market="upbit_krw", epochs=(2, 3)
            ),
            cycle_results(
                prices["btc_usd"], schedules, market="btc_usd", epochs=(1, 2, 3)
            ),
        ],
        ignore_index=True,
    )

    summary_path = OUTPUT_DIR / "btc_clock_hybrid_summary.csv"
    trades_path = OUTPUT_DIR / "btc_clock_hybrid_trades.csv"
    daily_path = OUTPUT_DIR / "btc_clock_hybrid_daily.csv"
    cycles_path = OUTPUT_DIR / "btc_clock_hybrid_cycles.csv"
    report_path = OUTPUT_DIR / "btc_clock_hybrid_report.md"
    manifest_path = OUTPUT_DIR / "btc_clock_hybrid_manifest.json"
    summary.to_csv(summary_path, index=False, encoding="utf-8")
    trades.to_csv(trades_path, index=False, encoding="utf-8")
    daily.to_csv(daily_path, index=False, encoding="utf-8")
    cycles.to_csv(cycles_path, index=False, encoding="utf-8")
    report_path.write_text(build_report(summary, cycles, trigger_table), encoding="utf-8")

    files = []
    for path in sorted(snapshot.iterdir()):
        files.append(
            {
                "path": path.name,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "research_version": RESEARCH_VERSION,
        "fixed_end": FIXED_END.isoformat(),
        "as_of_utc": AS_OF_UTC.isoformat(),
        "initial_capital_krw": INITIAL_CAPITAL_KRW,
        "fee_bps": FEE_BPS,
        "slippage_bps": SLIPPAGE_BPS,
        "entry_watch_progress": 0.625,
        "entry_progress": 0.65,
        "old_exit_progress": 0.35,
        "new_exit_progresses": [0.36, 0.37, 0.38],
        "official_check": "Monday 09:17 Asia/Seoul",
        "execution_price": "same-date daily open plus/minus fixed slippage",
        "input_files": files,
        "outputs": {
            "summary": str(summary_path),
            "trades": str(trades_path),
            "daily": str(daily_path),
            "cycles": str(cycles_path),
            "report": str(report_path),
        },
    }
    _write_json(manifest_path, manifest)
    return {"manifest": manifest, "summary": summary.to_dict(orient="records")}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare fixed-six 35%-weekly exits with 36/37/38 clock exits"
    )
    parser.parse_args()
    result = run()
    print(json.dumps(_json_ready(result), ensure_ascii=False, indent=2))
    print((OUTPUT_DIR / "btc_clock_hybrid_report.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
