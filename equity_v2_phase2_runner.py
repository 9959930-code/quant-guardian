from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

import equity_v2_phase2_research as research
from equity_v2_engine import Phase2Candidate if False else SimulationResult
from equity_v2_engine import simulate_close_to_close


_original_buy_hold_comparison = research.buy_hold_comparison
_original_shortlist = research.shortlist


def _buy_hold_with_multiple(*args, **kwargs):
    frame, simulations = _original_buy_hold_comparison(*args, **kwargs)
    if "capital_multiple" not in frame:
        frame["capital_multiple"] = 1.0 + frame["total_return"]
    return frame, simulations


def _quick_period(
    simulation: SimulationResult,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    daily = simulation.daily.loc[start:end]
    if len(daily) < 2:
        return {
            "cagr": np.nan,
            "mdd": np.nan,
            "calmar": np.nan,
            "trades_per_year": np.nan,
        }
    equity = daily["equity"]
    days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = days / 365.25
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    mdd = float((equity / equity.cummax() - 1).min())
    if simulation.trades.empty or "date" not in simulation.trades:
        trades = 0
    else:
        dates = pd.to_datetime(simulation.trades["date"], errors="coerce")
        trades = int(((dates >= equity.index[0]) & (dates <= equity.index[-1])).sum())
    return {
        "cagr": cagr,
        "mdd": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "trades_per_year": trades / years if years > 0 else np.nan,
    }


def _fast_broad_search(
    candidates: Iterable[research.Phase2Candidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: Any,
    latest: pd.Timestamp,
    cost_bps: float,
) -> pd.DataFrame:
    candidate_list = list(candidates)
    rows: list[dict[str, Any]] = []
    for number, candidate in enumerate(candidate_list, start=1):
        record = candidate.record()
        try:
            targets = research.generate_phase2_targets(
                candidate,
                store=store,
                start=research.ACTUAL_START,
                end=latest,
            )
            if targets.empty:
                raise ValueError("no target schedule")
            simulation = simulate_close_to_close(
                targets=targets,
                frames=frames,
                start=research.ACTUAL_START,
                end=latest,
                cost_bps=cost_bps,
            )
            values: dict[str, Any] = {}
            for prefix, (start, end) in research.DEV_FOLDS.items():
                metrics = _quick_period(simulation, start, end)
                values.update(
                    {f"{prefix}_{key}": value for key, value in metrics.items()}
                )
            dev = _quick_period(
                simulation, research.ACTUAL_START, research.LATEST_DEV_END
            )
            values.update({f"dev_all_{key}": value for key, value in dev.items()})
            values.update(research._selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 1000 == 0:
            print(
                f"phase2 fast broad: {number}/{len(candidate_list)}",
                flush=True,
            )
    return pd.DataFrame(rows)


def _shortlist_250(broad, candidate_map, maximum=400):
    return _original_shortlist(
        broad,
        candidate_map,
        maximum=min(250, int(maximum)),
    )


research.buy_hold_comparison = _buy_hold_with_multiple
research.broad_search = _fast_broad_search
research.shortlist = _shortlist_250


if __name__ == "__main__":
    raise SystemExit(research.main())
