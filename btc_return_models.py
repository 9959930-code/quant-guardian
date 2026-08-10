from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family: str
    pre_start: float
    exit_start: float | None
    hard_end: float
    target_vol: float | None
    deadband: float
    policy: str
    trend_rule: str = "none"
    confirmation_weeks: int = 1
    exposure_mode: str = "full"
    risk_off_weight: float = 0.0


def _pct_id(value: float | None) -> str:
    return "none" if value is None else str(int(round(value * 100)))


def candidate_grid() -> list[Candidate]:
    rows: list[Candidate] = []
    for pre in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85):
        for end in (0.30, 0.35, 0.40, 0.45, 0.50):
            for vol in (None, 0.40, 0.45, 0.50, 0.55, 0.60):
                for deadband in (0.10, 0.15):
                    for policy in ("target_change", "actual_weight"):
                        cid = (
                            f"window_pre{_pct_id(pre)}_post{_pct_id(end)}_"
                            f"vol{_pct_id(vol)}_db{_pct_id(deadband)}_"
                            f"{'target' if policy == 'target_change' else 'actual'}"
                        )
                        rows.append(
                            Candidate(
                                cid,
                                "window",
                                pre,
                                None,
                                end,
                                vol,
                                deadband,
                                policy,
                                exposure_mode="full" if vol is None else "vol",
                            )
                        )
    modes = [
        ("full", None, 0.0),
        ("vol", 0.45, 0.0),
        ("vol", 0.50, 0.0),
        ("vol", 0.55, 0.0),
        ("asym", 0.45, 0.25),
        ("asym", 0.50, 0.25),
        ("asym", 0.55, 0.25),
        ("asym", 0.45, 0.50),
        ("asym", 0.50, 0.50),
        ("asym", 0.55, 0.50),
    ]
    for pre in (0.65, 0.70, 0.75):
        for exit_start in (0.20, 0.25, 0.30):
            for end in (0.40, 0.45, 0.50):
                for trend_rule in ("wma40", "two_of_three"):
                    for confirm in (1, 2, 3):
                        for mode, vol, risk_off in modes:
                            for deadband in (0.10, 0.15):
                                cid = (
                                    f"trend_pre{_pct_id(pre)}_exit{_pct_id(exit_start)}_"
                                    f"hard{_pct_id(end)}_{trend_rule}_c{confirm}_{mode}_"
                                    f"vol{_pct_id(vol)}_risk{_pct_id(risk_off)}_"
                                    f"db{_pct_id(deadband)}"
                                )
                                rows.append(
                                    Candidate(
                                        cid,
                                        "trend_exit",
                                        pre,
                                        exit_start,
                                        end,
                                        vol,
                                        deadband,
                                        "target_change",
                                        trend_rule,
                                        confirm,
                                        mode,
                                        risk_off,
                                    )
                                )
    if len({row.candidate_id for row in rows}) != len(rows):
        raise RuntimeError("Duplicate candidate IDs")
    return rows


def add_return_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["wma40_slope_28d"] = out["wma40"] / out["wma40"].shift(28) - 1
    out["trend_wma40"] = (out["close"] >= out["wma40"]) & (
        out["wma40_slope_28d"] >= 0
    )
    out["trend_sma200"] = (out["close"] >= out["sma200"]) & (
        out["sma200_slope_60d"] >= 0
    )
    out["trend_mom180"] = out["return_180d"] > 0
    out["trend_two_of_three"] = (
        out[["trend_wma40", "trend_sma200", "trend_mom180"]]
        .fillna(False)
        .sum(axis=1)
        >= 2
    )
    return out


def _trend(row: pd.Series, rule: str) -> bool:
    if rule == "wma40":
        return bool(row.get("trend_wma40", False))
    if rule == "two_of_three":
        return bool(row.get("trend_two_of_three", False))
    return True


def _active_weight(
    candidate: Candidate, volatility: float, trend_on: bool
) -> tuple[float, float]:
    if candidate.exposure_mode == "full":
        return 1.0, 1.0
    if candidate.target_vol is None:
        raise ValueError("Volatility mode needs target_vol")
    cap = min(1.0, candidate.target_vol / volatility)
    if candidate.exposure_mode == "vol":
        return cap, cap
    if trend_on:
        return 1.0, cap
    return min(candidate.risk_off_weight, cap), cap


def generate_return_decisions(
    features: pd.DataFrame, candidate: Candidate
) -> pd.DataFrame:
    required = {
        "momentum_feature_ready",
        "phase_label",
        "cycle_progress",
        "halving_epoch",
        "realized_volatility",
        "trend_wma40",
        "trend_two_of_three",
    }
    missing = sorted(required.difference(features.columns))
    if missing:
        raise ValueError(f"Missing v0.7 features: {', '.join(missing)}")
    active = False
    negative_streak = 0
    rows: list[dict[str, Any]] = []
    for index, row in features.iterrows():
        phase = str(row.get("phase_label", "UNKNOWN"))
        progress = float(row.get("cycle_progress", np.nan))
        epoch = row.get("halving_epoch", np.nan)
        volatility = float(row.get("realized_volatility", np.nan))
        ready = (
            bool(row.get("momentum_feature_ready", False))
            and phase != "UNKNOWN"
            and pd.notna(epoch)
            and math.isfinite(progress)
            and 0 <= progress < 1
            and math.isfinite(volatility)
            and volatility > 0
        )
        decision = index.dayofweek == 6 and ready
        desired = np.nan
        cap = np.nan
        trend_on = _trend(row, candidate.trend_rule) if ready else False
        reason = "다음 주간 확정 신호 대기"
        if decision:
            if candidate.family == "window":
                in_window = (
                    progress >= candidate.pre_start or progress <= candidate.hard_end
                )
                if in_window:
                    desired, cap = _active_weight(candidate, volatility, trend_on)
                    reason = "반감기 전후 보유창"
                else:
                    desired = 0.0
                    reason = "보유창 밖"
            else:
                if progress >= candidate.pre_start:
                    active = True
                    negative_streak = 0
                    reason = "다음 반감기 전 매집·보유"
                elif active and progress >= candidate.hard_end:
                    active = False
                    negative_streak = 0
                    reason = "반감기 후 최대 보유구간 종료"
                elif (
                    active
                    and candidate.exit_start is not None
                    and progress >= candidate.exit_start
                ):
                    negative_streak = 0 if trend_on else negative_streak + 1
                    reason = "반감기 후 추세 확인"
                if active:
                    desired, cap = _active_weight(candidate, volatility, trend_on)
                    if (
                        candidate.exit_start is not None
                        and progress >= candidate.exit_start
                        and negative_streak >= candidate.confirmation_weeks
                    ):
                        desired = min(desired, candidate.risk_off_weight)
                        reason = "반감기 후 추세약화로 위험축소"
                else:
                    desired = 0.0
            desired = float(np.clip(desired, 0.0, 1.0))
        rows.append(
            {
                "Date": index,
                "is_decision_day": bool(decision),
                "desired_weight": desired,
                "reason": reason,
                "phase_label": phase,
                "cycle_progress": progress,
                "halving_epoch": epoch,
                "trend_on": bool(trend_on),
                "negative_streak": int(negative_streak),
                "realized_volatility": volatility,
                "volatility_cap": cap,
            }
        )
    return pd.DataFrame(rows).set_index("Date")
