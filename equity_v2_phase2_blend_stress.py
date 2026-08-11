from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from equity_v2_engine import FeatureStore, SimulationResult, close_panel, load_market_data, simulate_next_open
from equity_v2_phase2_research import (
    BLEND_REBALANCE_COST_BPS,
    DEFAULT_COST_BPS,
    DEFAULT_SLIPPAGE_BPS,
    Phase2Candidate,
    SYNTHETIC_START,
    generate_phase2_targets,
    synthetic_leverage_frame,
)
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


AGGRESSIVE = Phase2Candidate(
    "aggressive_5e75284d86fbe5",
    "aggressive",
    {
        "asset": "TQQQ",
        "pullback": 0.075,
        "recovery_ma": 10,
        "long_ma": 220,
        "trailing_stop": 0.20,
        "frequency": "monthly",
        "parts": 2,
        "tqqq_weight": 1.0,
        "remainder": "CASH",
        "slope_days": 20,
        "confirmation": 1,
    },
)

BALANCED = Phase2Candidate(
    "balanced_1e4c9c5be6cc20",
    "balanced",
    {
        "asset": "TQQQ",
        "breakout_days": 20,
        "long_ma": 180,
        "exit_ma": 200,
        "frequency": "monthly",
        "parts": 1,
        "tqqq_weight": 1.0,
        "remainder": "QQQ",
        "entry_confirm": 1,
        "exit_confirm": 1,
    },
)


def metrics(equity: pd.Series) -> dict[str, Any]:
    equity = equity.dropna()
    daily = equity.pct_change().dropna()
    days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = days / 365.25
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((1 + total) ** (1 / years) - 1)
    mdd = float((equity / equity.cummax() - 1).min())
    vol = float(daily.std(ddof=0) * math.sqrt(252))
    return {
        "total_return": total,
        "capital_multiple": 1 + total,
        "cagr": cagr,
        "mdd": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "sharpe": float(daily.mean() * 252 / vol) if vol > 0 else np.nan,
    }


def blend(
    aggressive: SimulationResult,
    balanced: SimulationResult,
    *,
    aggressive_weight: float,
    policy: str,
    threshold: float | None,
    cost_bps: float,
) -> tuple[pd.Series, int]:
    index = aggressive.daily.index.intersection(balanced.daily.index)
    index = index[index >= SYNTHETIC_START]
    a_returns = aggressive.daily.loc[index, "equity"].pct_change().fillna(0.0)
    b_returns = balanced.daily.loc[index, "equity"].pct_change().fillna(0.0)
    a_value = float(aggressive_weight)
    b_value = float(1 - aggressive_weight)
    prior_year = index[0].year
    prior_month = index[0].to_period("M")
    cost_rate = cost_bps / 10_000
    values: list[float] = []
    events = 0

    for date in index:
        total_before = a_value + b_value
        current_a = a_value / total_before if total_before > 0 else aggressive_weight
        rebalance = False
        if policy == "annual" and date.year != prior_year:
            rebalance = True
        elif policy == "threshold" and date.to_period("M") != prior_month:
            rebalance = threshold is not None and abs(current_a - aggressive_weight) >= threshold
        if rebalance and total_before > 0:
            transferred = abs(total_before * aggressive_weight - a_value)
            net = max(0.0, total_before - transferred * cost_rate)
            a_value = net * aggressive_weight
            b_value = net * (1 - aggressive_weight)
            events += 1
        a_value *= 1 + float(a_returns.loc[date])
        b_value *= 1 + float(b_returns.loc[date])
        values.append(a_value + b_value)
        prior_year = date.year
        prior_month = date.to_period("M")
    return pd.Series(values, index=index), events


def run(
    *,
    refresh: bool,
    config_path: Path,
    cost_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    base_frames, _ = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    latest = pd.Timestamp(base_frames["SPY"]["Close"].dropna().index.max())
    rows: list[dict[str, Any]] = []

    for drag in (0.01, 0.025, 0.04):
        synthetic_asset = f"TQQQ_SYNTH_{str(drag).replace('.', 'p')}"
        frames = dict(base_frames)
        frames[synthetic_asset] = synthetic_leverage_frame(
            base_frames["QQQ"], annual_drag=drag
        ).reindex(base_frames["SPY"].index)
        store = FeatureStore(close_panel(frames))
        simulations: dict[str, SimulationResult] = {}
        for name, candidate in (("aggressive", AGGRESSIVE), ("balanced", BALANCED)):
            params = dict(candidate.params)
            params["asset"] = synthetic_asset
            spec = replace(candidate, candidate_id=f"{candidate.candidate_id}_{synthetic_asset}", params=params)
            targets = generate_phase2_targets(spec, store=store, start=SYNTHETIC_START, end=latest)
            simulations[name] = simulate_next_open(
                targets=targets,
                frames=frames,
                start=SYNTHETIC_START,
                end=latest,
                cost_bps=cost_bps,
                slippage_bps=slippage_bps,
            )

        cases = [
            ("blend_50_50_threshold10", 0.50, "threshold", 0.10),
            ("blend_40_60_threshold10", 0.40, "threshold", 0.10),
            ("blend_50_50_annual", 0.50, "annual", None),
            ("blend_50_50_none", 0.50, "none", None),
        ]
        for label, weight, policy, threshold in cases:
            equity, events = blend(
                simulations["aggressive"],
                simulations["balanced"],
                aggressive_weight=weight,
                policy=policy,
                threshold=threshold,
                cost_bps=BLEND_REBALANCE_COST_BPS,
            )
            full = metrics(equity)
            crisis = metrics(equity.loc["2007-10-01":"2009-12-31"])
            rows.append(
                {
                    "annual_drag": drag,
                    "strategy": label,
                    "aggressive_weight": weight,
                    "policy": policy,
                    "threshold": threshold,
                    "rebalance_events": events,
                    **{f"full_{key}": value for key, value in full.items()},
                    **{f"gfc_{key}": value for key, value in crisis.items()},
                }
            )

    result = pd.DataFrame(rows)
    csv_path = paths.output / "equity_v2_phase2_synthetic_blend_stress.csv"
    json_path = paths.output / "equity_v2_phase2_synthetic_blend_stress.json"
    result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    json_path.write_text(
        json.dumps(result.to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(result.to_string(index=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic 2008 stress for selected Equity v2 blends")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    args = parser.parse_args()
    run(
        refresh=args.refresh,
        config_path=args.config,
        cost_bps=args.cost_bps,
        slippage_bps=args.slippage_bps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
