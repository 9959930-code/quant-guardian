from __future__ import annotations

import pandas as pd

import equity_v2_modern_engine as engine
import equity_v2_modern_research as research


_original_build_primary_frames = research.build_primary_frames
_original_generate_targets = research.generate_targets


def _build_primary_frames_with_calendar(*args, **kwargs):
    frames, calibration, residuals, metadata = _original_build_primary_frames(
        *args, **kwargs
    )
    # The generic execution engine uses SPY only as its master US trading
    # calendar. This Nasdaq-only study intentionally uses QQQ's calendar.
    frames = dict(frames)
    frames["SPY"] = frames["QQQ"].copy()
    return frames, calibration, residuals, metadata


def _generate_targets_with_initial_mix(candidate, *, store, start, end):
    targets = _original_generate_targets(
        candidate,
        store=store,
        start=start,
        end=end,
    )
    if candidate.family != "fixed_mix":
        return targets

    index = store.close.loc[pd.Timestamp(start) : pd.Timestamp(end)].index
    if len(index) == 0:
        return targets
    first_date = pd.Timestamp(index[0])
    weights = engine.allocation_patterns()[str(candidate.params["allocation"])]
    first = pd.DataFrame([weights], index=[first_date], dtype=float)
    if targets.empty:
        return first
    combined = pd.concat([first, targets], axis=0).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    return combined.fillna(0.0)


research.build_primary_frames = _build_primary_frames_with_calendar
research.generate_targets = _generate_targets_with_initial_mix


if __name__ == "__main__":
    raise SystemExit(research.main())
