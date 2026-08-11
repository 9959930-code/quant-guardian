from __future__ import annotations

from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

import equity_v2_phase2_research as research


_original_buy_hold_comparison = research.buy_hold_comparison
_original_shortlist = research.shortlist


def _buy_hold_with_multiple(*args, **kwargs):
    frame, simulations = _original_buy_hold_comparison(*args, **kwargs)
    if "capital_multiple" not in frame:
        frame["capital_multiple"] = 1.0 + frame["total_return"]
    return frame, simulations


def _array_metrics(
    equity: np.ndarray,
    index: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp,
    trade_dates: pd.DatetimeIndex,
) -> dict[str, Any]:
    left = int(index.searchsorted(start, side="left"))
    right = int(index.searchsorted(end, side="right"))
    if right - left < 2:
        return {
            "cagr": np.nan,
            "mdd": np.nan,
            "calmar": np.nan,
            "trades_per_year": np.nan,
        }
    values = equity[left:right]
    dates = index[left:right]
    days = max(1, (dates[-1] - dates[0]).days)
    years = days / 365.25
    cagr = float((values[-1] / values[0]) ** (1 / years) - 1)
    peaks = np.maximum.accumulate(values)
    mdd = float(np.min(values / peaks - 1))
    trades = int(((trade_dates >= dates[0]) & (trade_dates <= dates[-1])).sum())
    return {
        "cagr": cagr,
        "mdd": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "trades_per_year": trades / years if years > 0 else np.nan,
    }


def _fast_portfolio_path(
    targets: pd.DataFrame,
    *,
    index: pd.DatetimeIndex,
    asset_returns: np.ndarray,
    cost_rate: float,
) -> tuple[np.ndarray, pd.DatetimeIndex]:
    # Asset column order is TQQQ, QQQ, CASH.
    current = np.array([0.0, 0.0, 1.0], dtype=float)
    portfolio_returns = np.zeros(len(index), dtype=float)
    cost_adjustments = np.zeros(len(index), dtype=float)
    last_position = 0
    trade_positions: list[int] = []
    pending: dict[int, np.ndarray] = {}

    for signal_date, row in targets.sort_index().iterrows():
        position = int(index.searchsorted(pd.Timestamp(signal_date), side="right"))
        if position >= len(index):
            continue
        desired = np.array(
            [
                float(row.get("TQQQ", 0.0) or 0.0),
                float(row.get("QQQ", 0.0) or 0.0),
                float(row.get("CASH", 0.0) or 0.0),
            ],
            dtype=float,
        )
        desired = np.nan_to_num(desired, nan=0.0, posinf=0.0, neginf=0.0)
        desired = np.clip(desired, 0.0, None)
        total = float(desired.sum())
        if total < 1.0 - 1e-10:
            desired[2] += 1.0 - total
            total = float(desired.sum())
        desired = desired / total if total > 0 else np.array([0.0, 0.0, 1.0])
        pending[position] = desired

    for position in sorted(pending):
        if position > last_position:
            portfolio_returns[last_position:position] = (
                asset_returns[last_position:position] @ current
            )
        desired = pending[position]
        risk_turnover = float(np.abs(desired[:2] - current[:2]).sum())
        current = desired
        if risk_turnover > 1e-12:
            trade_positions.append(position)
            cost_adjustments[position] += risk_turnover * cost_rate
        last_position = position

    if last_position < len(index):
        portfolio_returns[last_position:] = asset_returns[last_position:] @ current
    portfolio_returns -= cost_adjustments
    equity = np.cumprod(1.0 + portfolio_returns)
    trade_dates = (
        index[np.array(trade_positions, dtype=int)]
        if trade_positions
        else pd.DatetimeIndex([])
    )
    return equity, trade_dates


def _fast_broad_search(
    candidates: Iterable[research.Phase2Candidate],
    *,
    frames: Mapping[str, pd.DataFrame],
    store: Any,
    latest: pd.Timestamp,
    cost_bps: float,
) -> pd.DataFrame:
    candidate_list = list(candidates)
    common = frames["SPY"].loc[research.ACTUAL_START:latest].index
    valid = pd.Series(True, index=common)
    for asset in ("TQQQ", "QQQ", "CASH"):
        valid &= frames[asset]["Close"].reindex(common).notna()
    index = common[valid.to_numpy()]
    close = pd.concat(
        {
            asset: frames[asset]["Close"].reindex(index)
            for asset in ("TQQQ", "QQQ", "CASH")
        },
        axis=1,
    )
    asset_returns = close.pct_change().fillna(0.0).to_numpy(dtype=float)
    cost_rate = float(cost_bps) / 10_000
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
            equity, trade_dates = _fast_portfolio_path(
                targets,
                index=index,
                asset_returns=asset_returns,
                cost_rate=cost_rate,
            )
            values: dict[str, Any] = {}
            for prefix, (start, end) in research.DEV_FOLDS.items():
                metrics = _array_metrics(equity, index, start, end, trade_dates)
                values.update(
                    {f"{prefix}_{key}": value for key, value in metrics.items()}
                )
            dev = _array_metrics(
                equity,
                index,
                research.ACTUAL_START,
                research.LATEST_DEV_END,
                trade_dates,
            )
            values.update({f"dev_all_{key}": value for key, value in dev.items()})
            values.update(research._selection_fields(values))
            rows.append({**record, "status": "ok", "error": None, **values})
        except Exception as exc:
            rows.append({**record, "status": "error", "error": str(exc)})
        if number % 1000 == 0:
            print(
                f"phase2 vector broad: {number}/{len(candidate_list)}",
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
