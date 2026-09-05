from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError

import btc_clock_hybrid_core as hybrid_core
import btc_clock_hybrid_telegram as hybrid_telegram
import btc_fixed_advisory as core
import btc_fixed_telegram_bot as bot


ESTIMATED_BLOCK_MINUTES = 10.0
FUNDING_ALERT_LEADS = (5, 3)
FUNDING_MIGRATION = "btc-clock-hybrid-entry-funding-alerts-v1"

BLOCK_HEIGHT_TIMEOUT_SECONDS = 8
BLOCK_HEIGHT_RETRY_DELAY_SECONDS = 0.35
BLOCK_HEIGHT_MIN_QUORUM = 2
BLOCK_HEIGHT_PROVIDERS = (
    (
        "mempool.space",
        "https://mempool.space/api/blocks/tip/height",
        2,
    ),
    (
        "blockstream.info",
        "https://blockstream.info/api/blocks/tip/height",
        2,
    ),
    (
        "blockchain.info",
        "https://blockchain.info/q/getblockcount",
        1,
    ),
)

_INSTALLED = False
_BASE_APPLY_DEFAULTS = hybrid_core.apply_defaults
_BASE_DETECT_EVENTS: Callable[..., list[dict[str, Any]]] | None = None
_BASE_BLOCK_EVENT_MESSAGE: Callable[[Mapping[str, Any]], str] | None = None
_BASE_STATUS_MESSAGE: Callable[..., str] | None = None


def _fetch_block_height_provider(
    name: str,
    url: str,
    attempts: int,
) -> tuple[str, int | None, str | None]:
    errors: list[str] = []
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            height = int(
                core._request_text(
                    url,
                    timeout=BLOCK_HEIGHT_TIMEOUT_SECONDS,
                )
            )
            if height <= 0:
                raise ValueError("0보다 큰 블록 높이가 필요합니다.")
            return name, height, None
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            OSError,
        ) as exc:
            errors.append(
                f"{attempt}차 {type(exc).__name__}: {exc}"
            )
            if attempt < attempts:
                time.sleep(
                    BLOCK_HEIGHT_RETRY_DELAY_SECONDS * attempt
                )
    return name, None, " | ".join(errors)


def _select_height_quorum(
    heights: Mapping[str, int],
    *,
    max_height_gap: int,
) -> list[tuple[str, int]]:
    ordered = sorted(
        (
            (str(name), int(height))
            for name, height in heights.items()
        ),
        key=lambda item: item[1],
    )
    candidates: list[list[tuple[str, int]]] = []
    for start, (_, first_height) in enumerate(ordered):
        cluster: list[tuple[str, int]] = []
        for item in ordered[start:]:
            if item[1] - first_height > max_height_gap:
                break
            cluster.append(item)
        if len(cluster) >= BLOCK_HEIGHT_MIN_QUORUM:
            candidates.append(cluster)
    if not candidates:
        return []
    return max(
        candidates,
        key=lambda cluster: (
            len(cluster),
            min(height for _, height in cluster),
        ),
    )


def fetch_resilient_block_context(
    now_utc: datetime | None = None,
    max_height_gap: int = 3,
) -> core.BlockContext:
    if max_height_gap < 0:
        raise ValueError("max_height_gap은 0 이상이어야 합니다.")

    now = now_utc or core.utc_now()
    heights: dict[str, int] = {}
    failures: dict[str, str] = {}

    with ThreadPoolExecutor(
        max_workers=len(BLOCK_HEIGHT_PROVIDERS),
        thread_name_prefix="btc-block-height",
    ) as executor:
        futures = [
            executor.submit(
                _fetch_block_height_provider,
                name,
                url,
                attempts,
            )
            for name, url, attempts in BLOCK_HEIGHT_PROVIDERS
        ]
        for future in as_completed(futures):
            name, height, error = future.result()
            if height is not None:
                heights[name] = height
            else:
                failures[name] = error or "알 수 없는 오류"

    quorum = _select_height_quorum(
        heights,
        max_height_gap=max_height_gap,
    )
    if len(quorum) < BLOCK_HEIGHT_MIN_QUORUM:
        details: list[str] = []
        for name, _, _ in BLOCK_HEIGHT_PROVIDERS:
            if name in heights:
                details.append(f"{name}={heights[name]:,}")
            else:
                details.append(
                    f"{name}=실패({failures.get(name, '응답 없음')})"
                )
        raise core.FixedStrategyError(
            "블록 높이 조회 실패: 독립 공급자 2개 이상 합의 필요; "
            + ", ".join(details)
        )

    agreed_heights = dict(quorum)
    agreed = min(agreed_heights.values())
    return core.BlockContext(
        height=agreed,
        epoch=agreed // 210_000,
        cycle_progress=(agreed % 210_000) / 210_000,
        mempool_height=agreed_heights.get(
            "mempool.space",
            agreed,
        ),
        blockstream_height=agreed_heights.get(
            "blockstream.info",
            agreed,
        ),
        observed_at_utc=core.iso_utc(now),
    )


def _normalize_funding_alerts(value: Any) -> list[str]:
    rows: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = str(item)
            if text not in rows:
                rows.append(text)
    return rows[-20:]


def apply_defaults(state: dict[str, Any]) -> dict[str, Any]:
    state = _BASE_APPLY_DEFAULTS(state)
    strategy = state.setdefault("strategy", {})
    strategy["entry_funding_alerts_sent"] = _normalize_funding_alerts(
        strategy.get("entry_funding_alerts_sent")
    )
    migrations = state.setdefault("migrations", [])
    if FUNDING_MIGRATION not in migrations:
        migrations.append(FUNDING_MIGRATION)
    return state


def estimated_entry_trigger_utc(
    block: core.BlockContext,
    now_utc: datetime,
) -> datetime:
    remaining = max(0, hybrid_core.ENTRY - hybrid_core.offset(block))
    return now_utc.astimezone(UTC) + timedelta(
        minutes=remaining * ESTIMATED_BLOCK_MINUTES
    )


def first_official_buy_kst(trigger_utc: datetime) -> datetime:
    local = trigger_utc.astimezone(core.KST)
    days_to_monday = (7 - local.weekday()) % 7
    if (
        local.weekday() == 0
        and local.time() <= core.OFFICIAL_CHECK_TIME
    ):
        days_to_monday = 0
    elif days_to_monday == 0:
        days_to_monday = 7
    return datetime.combine(
        local.date() + timedelta(days=days_to_monday),
        core.OFFICIAL_CHECK_TIME,
        tzinfo=core.KST,
    )


def business_days_until(start_date: date, target_date: date) -> int:
    if target_date < start_date:
        return -1
    count = 0
    cursor = start_date
    while cursor < target_date:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


def _funding_event(
    state: dict[str, Any],
    block: core.BlockContext,
    now_utc: datetime,
) -> dict[str, Any] | None:
    strategy = state["strategy"]
    if str(strategy.get("phase")) != "WAITING_ENTRY":
        return None
    if state.get("telegram", {}).get("pending_sync"):
        return None

    current_offset = hybrid_core.offset(block)
    if current_offset >= hybrid_core.ENTRY:
        return None

    now_kst = now_utc.astimezone(core.KST)
    if (
        now_kst.weekday() >= 5
        or now_kst.time() < core.OFFICIAL_CHECK_TIME
    ):
        return None

    trigger_utc = estimated_entry_trigger_utc(block, now_utc)
    first_buy = first_official_buy_kst(trigger_utc)
    days = business_days_until(now_kst.date(), first_buy.date())
    if 0 <= days <= 3:
        lead = 3
    elif 3 < days <= 5:
        lead = 5
    else:
        return None

    key = f"{block.epoch}:{lead}"
    sent = _normalize_funding_alerts(
        strategy.get("entry_funding_alerts_sent")
    )
    if key in sent:
        return None
    skipped = f"{block.epoch}:5"
    if lead == 3 and skipped not in sent:
        sent.append(skipped)
        core.append_audit(state, "ENTRY_FUNDING_EARLIER_ALERT_SUPERSEDED", {"epoch": block.epoch}, now_utc)
    sent.append(key)
    strategy["entry_funding_alerts_sent"] = sent[-20:]

    account = state["account"]
    total_cash = max(0.0, float(account.get("cash_krw", 0.0)))
    event = {
        "type": "ENTRY_FUNDING_PREP",
        "block": block,
        "lead_business_days": lead,
        "estimated_business_days": days,
        "estimated_trigger_kst": trigger_utc.astimezone(core.KST).isoformat(),
        "estimated_first_buy_kst": first_buy.isoformat(),
        "remaining_blocks": max(
            0, hybrid_core.ENTRY - current_offset
        ),
        "prepare_total_krw": total_cash,
        "first_target_krw": total_cash * core.ENTRY_TARGETS[0],
        "price_krw": account.get("last_price_krw"),
    }
    core.append_audit(
        state,
        "ENTRY_FUNDING_PREPARATION_ALERTED",
        {
            "epoch": block.epoch,
            "lead_business_days": lead,
            "estimated_business_days": days,
            "estimated_first_buy_kst": first_buy.isoformat(),
            "remaining_blocks": event["remaining_blocks"],
        },
        now_utc,
    )
    state["updated_at_utc"] = core.iso_utc(now_utc)
    return event


def detect_block_events(
    state: dict[str, Any],
    block: core.BlockContext,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    if _BASE_DETECT_EVENTS is None:
        raise core.FixedStrategyError(
            "BTC funding-alert runtime is not installed."
        )
    events = _BASE_DETECT_EVENTS(state, block, now_utc)
    funding = _funding_event(state, block, now_utc)
    if funding is not None:
        events.insert(0, funding)
    return events


def _format_kst(value: str) -> str:
    return (
        datetime.fromisoformat(value)
        .astimezone(core.KST)
        .strftime("%Y-%m-%d %H:%M KST")
    )


def block_event_message(event: Mapping[str, Any]) -> str:
    if str(event.get("type")) != "ENTRY_FUNDING_PREP":
        if _BASE_BLOCK_EVENT_MESSAGE is None:
            return "[BTC 블록 이벤트]\n알림 런타임이 준비되지 않았습니다."
        return _BASE_BLOCK_EVENT_MESSAGE(event)

    return "\n".join(
        [
            (
                "💰 BTC 첫 매수 자금준비 "
                f"{int(event['lead_business_days'])}영업일 전(예상)"
            ),
            "",
            "현재 블록속도를 10분/블록으로 놓은 예상 일정입니다.",
            (
                "- 예상 65% 도달: "
                f"{_format_kst(str(event['estimated_trigger_kst']))}"
            ),
            (
                "- 예상 1차 매수 점검: "
                f"{_format_kst(str(event['estimated_first_buy_kst']))}"
            ),
            (
                "- 현재 추정 영업일: "
                f"{int(event['estimated_business_days'])}일"
            ),
            f"- 65%까지: {int(event['remaining_blocks']):,}블록",
            f"- 현재 BTC 가격: {bot.krw(event.get('price_krw'))}",
            f"- 준비할 총 원화: {bot.krw(event['prepare_total_krw'])}",
            (
                "- 1차 목표액 근사: "
                f"{bot.krw(event['first_target_krw'])}"
            ),
            "",
            "블록 생성속도에 따라 예상일은 앞뒤로 바뀔 수 있습니다.",
            "주말만 제외한 근사 영업일이며 한국 공휴일은 별도 반영하지 않습니다.",
            "자금준비용 알림이며 자동이체·자동주문은 없습니다.",
        ]
    )


def status_message(
    state: Mapping[str, Any],
    *,
    price: float | None,
    block: core.BlockContext | None,
    title: str = "현재 상태",
) -> str:
    if _BASE_STATUS_MESSAGE is None:
        raise core.FixedStrategyError(
            "BTC funding-alert status runtime is not installed."
        )
    text = _BASE_STATUS_MESSAGE(
        state,
        price=price,
        block=block,
        title=title,
    )
    if (
        block is None
        or str(state["strategy"].get("phase")) != "WAITING_ENTRY"
        or hybrid_core.offset(block) >= hybrid_core.ENTRY
    ):
        return text
    observed = core.parse_iso(block.observed_at_utc) or core.utc_now()
    first_buy = first_official_buy_kst(
        estimated_entry_trigger_utc(block, observed)
    )
    return (
        text
        + "\n\n[첫 매수 자금준비]"
        + "\n- 약 5영업일 전·3영업일 전 알림"
        + f"\n- 현재 예상 1차 매수 점검: {first_buy:%Y-%m-%d %H:%M KST}"
        + "\n- 10분/블록·주말 제외 기준 근사"
    )


def install() -> None:
    global _INSTALLED
    global _BASE_DETECT_EVENTS
    global _BASE_BLOCK_EVENT_MESSAGE
    global _BASE_STATUS_MESSAGE

    if _INSTALLED:
        return

    hybrid_core.apply_defaults = apply_defaults
    hybrid_core.install()
    hybrid_telegram.install()

    _BASE_DETECT_EVENTS = core.detect_block_events
    _BASE_BLOCK_EVENT_MESSAGE = bot.block_event_message
    _BASE_STATUS_MESSAGE = bot.status_message

    core.fetch_block_context = fetch_resilient_block_context
    core.detect_block_events = detect_block_events
    bot.block_event_message = block_event_message
    bot.status_message = status_message
    _INSTALLED = True
