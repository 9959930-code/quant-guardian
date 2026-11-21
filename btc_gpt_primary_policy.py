"""GPT-primary BTC policy reference (2026-11-22).

Pure computation only: no exchange IO, Telegram, trading, account mutations,
workflow dispatch, or ChatGPT task API. The legacy engine is retired.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

INTERVAL = 210_000
WATCH_OFFSET = 131_250
ENTRY_OFFSET = 136_500
EXIT_WARNING_OFFSET = 73_500
EXIT_REVIEW_OFFSET = 77_700
ENTRY_WEIGHTS = (0.20, 0.40, 0.60, 0.80, 1.00)
KST = ZoneInfo("Asia/Seoul")
OFFICIAL_TIME = time(9, 17)


@dataclass(frozen=True)
class BTCPolicySnapshot:
    height: int
    epoch: int
    offset: int
    cycle_progress_pct: float
    watching: bool
    entry_threshold_reached: bool


def snapshot(height: int) -> BTCPolicySnapshot:
    if not isinstance(height, int) or isinstance(height, bool) or height < 0:
        raise ValueError("A verified nonnegative mainnet height is required")
    epoch, offset = divmod(height, INTERVAL)
    return BTCPolicySnapshot(
        height=height,
        epoch=epoch,
        offset=offset,
        cycle_progress_pct=100.0 * offset / INTERVAL,
        watching=offset >= WATCH_OFFSET,
        entry_threshold_reached=offset >= ENTRY_OFFSET,
    )


def exit_status(height: int, *, confirmed_entry_epoch: int | None) -> str:
    """No exit signal in the same epoch or without a confirmed entry."""
    s = snapshot(height)
    if confirmed_entry_epoch is None or s.epoch <= confirmed_entry_epoch:
        return "NO_EXIT_SIGNAL"
    if s.offset >= EXIT_REVIEW_OFFSET:
        return "EXIT_REVIEW_ALL"
    if s.offset >= EXIT_WARNING_OFFSET:
        return "EXIT_WARNING_ONLY"
    return "HOLD"


def review_step(
    height: int,
    now: datetime,
    *,
    entry_steps_completed: int | None,
    account_verified: bool,
    pending_order: bool | None,
    prior_tranche_fill_verified: bool,
) -> tuple[str, float | None]:
    """Eligibility advisory; never returns an executable order.

    entry_steps_completed is confirmed 0..5, None if unknown.
    pending_order must be explicitly False to proceed.
    Mondays 09:17 KST only, never on other weekdays or before 09:17.
    """
    if now.tzinfo is None:
        raise ValueError("Pass a timezone-aware timestamp")
    local = now.astimezone(KST)
    s = snapshot(height)
    if not s.entry_threshold_reached:
        return "WAIT_65_PERCENT", None
    if local.weekday() != 0 or local.time() < OFFICIAL_TIME:
        return "WAIT_OFFICIAL_MONDAY", None
    if entry_steps_completed is None or not 0 <= entry_steps_completed <= 5:
        return "DEFERRED_UNVERIFIED_STEP", None
    if entry_steps_completed == 5:
        return "HOLD", 1.0
    if not account_verified or pending_order is not False:
        return "DEFERRED_ACCOUNT_OR_PENDING", None
    if entry_steps_completed > 0 and not prior_tranche_fill_verified:
        return "DEFERRED_PRIOR_FILL", None
    return f"ENTRY_REVIEW_{entry_steps_completed + 1}", ENTRY_WEIGHTS[entry_steps_completed]


def first_official_monday_after(moment: datetime) -> datetime:
    """First Monday 09:17 KST not before the threshold observation."""
    if moment.tzinfo is None:
        raise ValueError("Pass a timezone-aware timestamp")
    local = moment.astimezone(KST)
    candidate = datetime.combine(local.date(), OFFICIAL_TIME, tzinfo=KST)
    candidate += timedelta(days=(7 - candidate.weekday()) % 7)
    if candidate < local:
        candidate += timedelta(days=7)
    return candidate


def target_adjustment_krw(
    target_weight: float, *, btc_valued_krw: float, strategy_cash_krw: float,
    reserve_next_krw: float = 0,
) -> float:
    """Reference target allocation gap, NOT a validated order quantity."""
    values = (target_weight, btc_valued_krw, strategy_cash_krw, reserve_next_krw)
    import math
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Inputs must be finite")
    if not (0 <= target_weight <= 1 and btc_valued_krw >= 0 and strategy_cash_krw >= 0
            and 0 <= reserve_next_krw <= strategy_cash_krw):
        raise ValueError("Invalid allocation inputs")
    active_equity = btc_valued_krw + strategy_cash_krw - reserve_next_krw
    return target_weight * active_equity - btc_valued_krw
