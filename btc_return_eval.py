from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Mapping

import pandas as pd

from btc_halving_research import (
    candidate_specs as v06_candidate_specs,
    completed_halving_epochs,
    generate_halving_overlay_decisions,
    simulate_weekly_strategy,
)
from btc_research import (
    SimulationResult,
    make_buy_hold_signals,
    performance_metrics,
    simulate_strategy,
)
from quant_guardian import annualized_metrics

from btc_return_models import Candidate, generate_return_decisions

MDD_LIMIT = -0.50
BUFFERED_MDD_LIMIT = -0.45
TOP_CROSSCHECK = 350


def simulate_candidate(
    features: pd.DataFrame,
    candidate: Candidate,
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
    delay: int = 0,
    cash_yield: float = 0.0,
) -> tuple[pd.DataFrame, SimulationResult]:
    decisions = generate_return_decisions(features, candidate)
    simulation = simulate_weekly_strategy(
        features,
        decisions,
        rebalance_policy=candidate.policy,
        rebalance_deadband=candidate.deadband,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        additional_delay_days=delay,
        initial_capital=capital,
        idle_cash_annual_yield=cash_yield,
    )
    return decisions, simulation


def metric_row(
    candidate_id: str, simulation: SimulationResult, capital: float
) -> dict[str, Any]:
    metrics = performance_metrics(simulation)
    terminal = float(metrics["terminal_wealth"])
    return {
        "candidate_id": candidate_id,
        "terminal_wealth_krw": terminal,
        "capital_multiple": terminal / capital,
        **metrics,
    }


def period_metrics(simulation: SimulationResult, index: pd.Index) -> dict[str, Any]:
    daily = simulation.daily.reindex(index).dropna(subset=["daily_return"])
    metrics = annualized_metrics(daily["daily_return"], periods_per_year=365)
    if simulation.trades.empty:
        trades = 0
    else:
        dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        trades = int(
            ((dates >= daily.index.min()) & (dates <= daily.index.max())).sum()
        )
    return {
        **metrics,
        "exposure": float(daily["actual_weight"].mean()),
        "trades": trades,
    }


def evaluate_full_grid(
    features: pd.DataFrame,
    candidates: list[Candidate],
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        _, simulation = simulate_candidate(
            features,
            candidate,
            capital=capital,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        rows.append(
            {**asdict(candidate), **metric_row(candidate.candidate_id, simulation, capital)}
        )
    return pd.DataFrame(rows)


def evaluate_crosschecks(
    full: pd.DataFrame,
    candidates: Mapping[str, Candidate],
    synthetic: pd.DataFrame,
    upbit: pd.DataFrame,
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    shortlist = full.loc[full["mdd"] >= MDD_LIMIT].nlargest(
        TOP_CROSSCHECK, "cagr"
    )
    common = synthetic.index.intersection(upbit.index)
    ready = synthetic.loc[common, "momentum_feature_ready"].fillna(False) & upbit.loc[
        common, "momentum_feature_ready"
    ].fillna(False)
    common = common[ready.to_numpy()]
    if len(common) < 730:
        raise ValueError("Upbit overlap has fewer than 730 rows")
    completed = completed_halving_epochs(synthetic)
    ranking_rows: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []
    for _, full_row in shortlist.iterrows():
        cid = str(full_row["candidate_id"])
        candidate = candidates[cid]
        _, full_sim = simulate_candidate(
            synthetic,
            candidate,
            capital=capital,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        _, upbit_sim = simulate_candidate(
            upbit.loc[common],
            candidate,
            capital=capital,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        up = metric_row(cid, upbit_sim, capital)
        cycle_values: list[dict[str, Any]] = []
        for epoch in completed:
            idx = synthetic.index[synthetic["halving_epoch"] == epoch]
            metrics = period_metrics(full_sim, idx)
            item = {"candidate_id": cid, "halving_epoch": epoch, **metrics}
            cycle_rows.append(item)
            cycle_values.append(item)
        worst_cycle_mdd = min(float(item["mdd"]) for item in cycle_values)
        min_cycle_cagr = min(float(item["cagr"]) for item in cycle_values)
        risk_pass = (
            float(full_row["mdd"]) >= MDD_LIMIT
            and float(up["mdd"]) >= MDD_LIMIT
            and worst_cycle_mdd >= MDD_LIMIT
        )
        ranking_rows.append(
            {
                **full_row.to_dict(),
                "upbit_terminal_wealth_krw": up["terminal_wealth_krw"],
                "upbit_cagr": up["cagr"],
                "upbit_mdd": up["mdd"],
                "upbit_calmar": up["calmar"],
                "worst_completed_cycle_mdd": worst_cycle_mdd,
                "min_completed_cycle_cagr": min_cycle_cagr,
                "robust_cagr": min(float(full_row["cagr"]), float(up["cagr"])),
                "risk_pass": risk_pass,
            }
        )
    ranking = pd.DataFrame(ranking_rows).sort_values(
        ["risk_pass", "robust_cagr", "cagr"],
        ascending=[False, False, False],
    )
    return ranking, pd.DataFrame(cycle_rows)


def select_candidates(ranking: pd.DataFrame) -> tuple[str, str, str]:
    eligible = ranking.loc[ranking["risk_pass"]].copy()
    if eligible.empty:
        raise ValueError("No candidate passed the -50% risk gate")
    aggressive = str(eligible.iloc[0]["candidate_id"])
    buffered_rows = eligible.loc[
        (eligible["mdd"] >= BUFFERED_MDD_LIMIT)
        & (eligible["upbit_mdd"] >= BUFFERED_MDD_LIMIT)
        & (eligible["worst_completed_cycle_mdd"] >= BUFFERED_MDD_LIMIT)
    ]
    buffered = (
        aggressive
        if buffered_rows.empty
        else str(buffered_rows.iloc[0]["candidate_id"])
    )
    insample = str(ranking.sort_values("cagr", ascending=False).iloc[0]["candidate_id"])
    return aggressive, buffered, insample


def baseline_comparison(
    features: pd.DataFrame,
    candidates: Mapping[str, Candidate],
    selected: list[str],
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> tuple[pd.DataFrame, dict[str, SimulationResult]]:
    outputs: dict[str, SimulationResult] = {}
    for cid in selected:
        _, outputs[cid] = simulate_candidate(
            features,
            candidates[cid],
            capital=capital,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
    for spec in v06_candidate_specs():
        if spec.candidate_id not in {
            "v05_vol45_target_change",
            "v06_confirmation_vol40_actual",
        }:
            continue
        decisions = generate_halving_overlay_decisions(features, spec)
        outputs[spec.candidate_id] = simulate_weekly_strategy(
            features,
            decisions,
            rebalance_policy=spec.rebalance_policy,
            rebalance_deadband=spec.parameters.rebalance_deadband,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            initial_capital=capital,
        )
    outputs["buy_hold"] = simulate_strategy(
        features,
        make_buy_hold_signals(features),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        initial_capital=capital,
    )
    rows = [metric_row(name, sim, capital) for name, sim in outputs.items()]
    return pd.DataFrame(rows).sort_values("cagr", ascending=False), outputs


def robustness_diagnostics(
    features: pd.DataFrame,
    candidate: Candidate,
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    scenarios = [
        ("base", candidate, fee_bps, slippage_bps, 0, 0.0),
        ("double_cost", candidate, fee_bps * 2, slippage_bps * 2, 0, 0.0),
        ("delay_1d", candidate, fee_bps, slippage_bps, 1, 0.0),
        ("delay_2d", candidate, fee_bps, slippage_bps, 2, 0.0),
        (
            "deadband_minus5",
            replace(candidate, deadband=max(0.01, candidate.deadband - 0.05)),
            fee_bps,
            slippage_bps,
            0,
            0.0,
        ),
        (
            "deadband_plus5",
            replace(candidate, deadband=min(0.25, candidate.deadband + 0.05)),
            fee_bps,
            slippage_bps,
            0,
            0.0,
        ),
        ("idle_cash_3pct", candidate, fee_bps, slippage_bps, 0, 0.03),
    ]
    rows: list[dict[str, Any]] = []
    for name, spec, fee, slip, delay, cash_yield in scenarios:
        _, simulation = simulate_candidate(
            features,
            spec,
            capital=capital,
            fee_bps=fee,
            slippage_bps=slip,
            delay=delay,
            cash_yield=cash_yield,
        )
        rows.append({"scenario": name, **metric_row(name, simulation, capital)})
    return pd.DataFrame(rows)
