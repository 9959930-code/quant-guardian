from __future__ import annotations

import pandas as pd

import equity_v2_modern_engine as engine
import equity_v2_modern_research as research


_original_generate_targets = research.generate_targets
_original_stress_tables = research.stress_tables


def _build_primary_frames_with_calendar(*, cfg, paths, refresh):
    # Keep the original calibration process, then rebuild the price panel with
    # pre-2006 QQQ history so long moving averages are ready on the first live
    # research date. QLD still determines the common investable start.
    _, calibration, residuals, metadata = research.build_primary_frames(
        cfg=cfg,
        paths=paths,
        refresh=refresh,
    )
    qqq_raw = research.read_price("QQQ", paths, refresh=refresh)
    qld_raw = research.read_price("QLD", paths, refresh=refresh)
    tqqq_raw = research.read_price("TQQQ", paths, refresh=refresh)
    irx_raw = research.read_price("^IRX", paths, refresh=refresh)
    fx_raw = research.read_price("KRW=X", paths, refresh=refresh)

    synthetic_3x = research.dynamic_synthetic(
        qqq_raw,
        irx_raw["Close"],
        leverage=3.0,
        residual_drag=float(residuals["TQQQ_3x"]),
    )
    spliced_tqqq = research.splice_synthetic_actual(synthetic_3x, tqqq_raw)
    warmup_start = pd.Timestamp("2005-01-03")
    master = research._clean_ohlc(qqq_raw).loc[warmup_start:].index
    qqq = research.aligned_frame(qqq_raw, master)
    frames = {
        # The generic execution engine uses SPY only as a master US trading
        # calendar. This Nasdaq-only study intentionally uses QQQ's calendar.
        "SPY": qqq.copy(),
        "QQQ": qqq,
        "QLD": research.aligned_frame(qld_raw, master),
        "TQQQ": research.aligned_frame(spliced_tqqq, master),
        "CASH": research.cash_frame(master, irx_raw["Close"]),
        "KRW=X": research.aligned_frame(fx_raw, master).ffill().bfill(),
    }
    return frames, calibration, residuals, metadata


def _generate_targets_with_periodic_mix(candidate, *, store, start, end):
    if candidate.family != "fixed_mix":
        return _original_generate_targets(
            candidate,
            store=store,
            start=start,
            end=end,
        )

    index = store.close.loc[pd.Timestamp(start) : pd.Timestamp(end)].index
    if len(index) == 0:
        return pd.DataFrame()
    weights = engine.allocation_patterns()[str(candidate.params["allocation"])]
    frequency = str(candidate.params["frequency"])
    dates = [pd.Timestamp(index[0])]
    if frequency != "none":
        dates.extend(pd.Timestamp(value) for value in engine.schedule_dates(index, frequency))
    dates = list(dict.fromkeys(dates))
    return pd.DataFrame([weights for _ in dates], index=dates, dtype=float).fillna(0.0)


def _build_stress_frames_with_warmup(
    *,
    underlying,
    short_yield,
    residual_2x,
    residual_3x,
    start,
    end,
):
    underlying = research._clean_ohlc(underlying)
    synthetic_2x = research.dynamic_synthetic(
        underlying,
        short_yield,
        leverage=2.0,
        residual_drag=residual_2x,
    )
    synthetic_3x = research.dynamic_synthetic(
        underlying,
        short_yield,
        leverage=3.0,
        residual_drag=residual_3x,
    )
    warmup_start = max(
        pd.Timestamp(underlying.index.min()),
        pd.Timestamp(start) - pd.Timedelta(days=550),
    )
    index = underlying.loc[warmup_start : pd.Timestamp(end)].index
    fx = research.constant_fx(underlying.loc[index], 1000.0)
    return {
        "QQQ": research.aligned_frame(underlying, index),
        "QLD": research.aligned_frame(synthetic_2x, index),
        "TQQQ": research.aligned_frame(synthetic_3x, index),
        "CASH": research.cash_frame(index, short_yield),
        "KRW=X": fx,
    }


def _stress_tables_with_ndx_dotcom(
    finalists,
    candidate_map,
    *,
    qqq_raw,
    ndx_raw,
    short_yield,
    residuals,
    latest,
):
    # NDX supplies pre-1999 warmup history for the dot-com reference. The
    # modern 2006 selection itself continues to use actual QQQ/QLD/TQQQ data.
    return _original_stress_tables(
        finalists,
        candidate_map,
        qqq_raw=ndx_raw,
        ndx_raw=ndx_raw,
        short_yield=short_yield,
        residuals=residuals,
        latest=latest,
    )


research.build_primary_frames = _build_primary_frames_with_calendar
research.generate_targets = _generate_targets_with_periodic_mix
research.build_stress_frames = _build_stress_frames_with_warmup
research.stress_tables = _stress_tables_with_ndx_dotcom


if __name__ == "__main__":
    raise SystemExit(research.main())
