from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

STRATEGY_VERSION = "isa-tiger-leverage-telegram-1.0"
STATE_SCHEMA_VERSION = 1
KST = timezone(timedelta(hours=9))
INITIAL_INVESTMENT_KRW = 10_000_000.0
MONTHLY_CONTRIBUTION_KRW = 500_000.0
TIGER_CODE = "418660"
TIGER_NAME = "TIGER 미국나스닥100레버리지(합성)"
FX_WINDOW = 252
FX_SELL_Z = 1.25
FX_WATCH_Z = 0.75

EXISTING_HOLDINGS: tuple[dict[str, Any], ...] = (
    {"code": "442580", "name": "PLUS 글로벌HBM반도체", "quantity": 7.0},
    {"code": "0048J0", "name": "KODEX 미국머니마켓액티브", "quantity": 145.0},
    {"code": "379810", "name": "KODEX 미국나스닥100", "quantity": 70.0},
)


class IsaStrategyError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuoteSnapshot:
    code: str
    name: str
    date: str
    close: float


@dataclass(frozen=True)
class FxSnapshot:
    date: str
    usdkrw: float
    z_52w: float
    zone: str


@dataclass(frozen=True)
class PurchasePlan:
    budget_krw: float
    reference_price_krw: float
    shares: int
    expected_order_krw: float
    expected_remainder_krw: float


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def krw(value: float | int | None) -> str:
    return "-" if value is None else f"{round(float(value)):,}원"


def pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{float(value) * 100:.{digits}f}%"


def number(value: float | int | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    if digits <= 0:
        return f"{round(float(value)):,}"
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".") or "0"


def positive_float(value: str | float | int | None, label: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise IsaStrategyError(f"{label} 값이 숫자가 아닙니다.") from exc
    if parsed < 0:
        raise IsaStrategyError(f"{label} 값은 0 이상이어야 합니다.")
    return parsed


def new_state(now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or utc_now()
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "created_at_utc": iso_utc(now),
        "updated_at_utc": iso_utc(now),
        "strategy": {
            "initial_investment_krw": INITIAL_INVESTMENT_KRW,
            "monthly_contribution_krw": MONTHLY_CONTRIBUTION_KRW,
            "initial_plan_sent": False,
            "initial_plan_sent_at_utc": None,
            "initial_completed": False,
            "initial_completed_at_utc": None,
            "monthly_start_period": None,
            "last_monthly_plan_period": None,
            "last_fx_zone": None,
            "auto_order": False,
        },
        "account": {
            "existing_holdings": [dict(item) for item in EXISTING_HOLDINGS],
            "tiger_quantity": 0.0,
            "tiger_invested_krw": 0.0,
            "isa_total_contributions_krw": None,
        },
        "data": {
            "status": "unknown",
            "last_error": None,
            "last_quote_date": None,
            "last_tiger_price_krw": None,
            "last_fx_date": None,
            "last_fx_z": None,
        },
        "audit": [],
    }


def append_audit(
    state: dict[str, Any], event: str, payload: Mapping[str, Any], now_utc: datetime
) -> None:
    entries = state.setdefault("audit", [])
    entries.append({"at_utc": iso_utc(now_utc), "event": event, "payload": dict(payload)})
    if len(entries) > 200:
        del entries[:-200]


def validate_state(state: Mapping[str, Any]) -> None:
    if int(state.get("schema_version", -1)) != STATE_SCHEMA_VERSION:
        raise IsaStrategyError("ISA 상태 스키마 버전이 맞지 않습니다.")
    if state.get("strategy_version") != STRATEGY_VERSION:
        raise IsaStrategyError("ISA 전략 버전이 맞지 않습니다.")
    strategy = state.get("strategy") or {}
    account = state.get("account") or {}
    if bool(strategy.get("auto_order")):
        raise IsaStrategyError("자동주문 상태는 허용되지 않습니다.")
    if float(account.get("tiger_quantity", 0)) < 0:
        raise IsaStrategyError("TIGER 수량이 음수입니다.")
    if float(account.get("tiger_invested_krw", 0)) < 0:
        raise IsaStrategyError("TIGER 누적원금이 음수입니다.")
    expected = {(item["code"], float(item["quantity"])) for item in EXISTING_HOLDINGS}
    actual = {
        (str(item.get("code")), float(item.get("quantity", -1)))
        for item in account.get("existing_holdings", [])
    }
    if actual != expected:
        raise IsaStrategyError("기존 ISA 보유수량이 승인값과 다릅니다.")


def load_state(
    path: Path, *, reset: bool = False, now_utc: datetime | None = None
) -> tuple[dict[str, Any], bool]:
    now = now_utc or utc_now()
    if reset or not path.exists():
        return new_state(now), True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IsaStrategyError(f"ISA 상태파일을 읽지 못했습니다: {exc}") from exc
    validate_state(state)
    return state, False


def save_state(path: Path, state: dict[str, Any], *, now_utc: datetime | None = None) -> None:
    now = now_utc or utc_now()
    validate_state(state)
    state["updated_at_utc"] = iso_utc(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def next_month_period(now_kst: datetime) -> str:
    year = now_kst.year + (1 if now_kst.month == 12 else 0)
    month = 1 if now_kst.month == 12 else now_kst.month + 1
    return f"{year:04d}-{month:02d}"


def apply_manual_sync(
    state: dict[str, Any],
    *,
    tiger_quantity: float | None,
    tiger_invested_krw: float | None,
    isa_total_contributions_krw: float | None,
    mark_initial_completed: bool,
    now_utc: datetime,
) -> bool:
    account = state["account"]
    changed = False
    for key, value in (
        ("tiger_quantity", tiger_quantity),
        ("tiger_invested_krw", tiger_invested_krw),
        ("isa_total_contributions_krw", isa_total_contributions_krw),
    ):
        if value is not None:
            account[key] = float(value)
            changed = True
    if mark_initial_completed:
        if float(account.get("tiger_quantity", 0)) <= 0:
            raise IsaStrategyError("초기매수 완료 처리에는 실제 TIGER 수량이 필요합니다.")
        if float(account.get("tiger_invested_krw", 0)) <= 0:
            raise IsaStrategyError("초기매수 완료 처리에는 TIGER 누적투입원금이 필요합니다.")
        strategy = state["strategy"]
        strategy["initial_completed"] = True
        strategy["initial_completed_at_utc"] = iso_utc(now_utc)
        strategy["monthly_start_period"] = strategy.get("monthly_start_period") or next_month_period(
            now_utc.astimezone(KST)
        )
        changed = True
    if changed:
        append_audit(
            state,
            "MANUAL_ACCOUNT_SYNC",
            {
                "tiger_quantity": account.get("tiger_quantity"),
                "tiger_invested_krw": account.get("tiger_invested_krw"),
                "isa_total_contributions_krw": account.get("isa_total_contributions_krw"),
                "initial_completed": state["strategy"].get("initial_completed"),
            },
            now_utc,
        )
    return changed


def fetch_naver_history(code: str, *, count: int = 420) -> list[tuple[str, float]]:
    url = (
        "https://fchart.stock.naver.com/sise.nhn"
        f"?symbol={quote(code, safe='')}&timeframe=day&count={int(count)}&requestType=0"
    )
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 quant-guardian-isa/1.0",
            "Referer": "https://finance.naver.com/",
        },
    )
    try:
        with urlopen(request, timeout=40) as response:
            payload = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise IsaStrategyError(f"네이버 시세 조회 실패({code}): {exc}") from exc
    text: str | None = None
    for encoding in ("utf-8", "euc-kr", "cp949"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise IsaStrategyError(f"네이버 시세 디코딩 실패({code})")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise IsaStrategyError(f"네이버 시세 XML 파싱 실패({code})") from exc
    deduplicated: dict[str, float] = {}
    for item in root.iter("item"):
        values = str(item.attrib.get("data", "")).split("|")
        if len(values) < 5:
            continue
        try:
            parsed_close = float(values[4])
        except ValueError:
            continue
        date = values[0]
        if len(date) == 8 and parsed_close > 0:
            deduplicated[f"{date[:4]}-{date[4:6]}-{date[6:]}"] = parsed_close
    rows = sorted(deduplicated.items())
    if not rows:
        raise IsaStrategyError(f"네이버 시세가 비어 있습니다({code})")
    return rows


def fetch_quotes() -> dict[str, QuoteSnapshot]:
    quotes: dict[str, QuoteSnapshot] = {}
    instruments = [*EXISTING_HOLDINGS, {"code": TIGER_CODE, "name": TIGER_NAME}]
    for instrument in instruments:
        date, close = fetch_naver_history(str(instrument["code"]), count=20)[-1]
        quotes[str(instrument["code"])] = QuoteSnapshot(
            str(instrument["code"]), str(instrument["name"]), date, float(close)
        )
    return quotes


def fetch_yahoo_close_series(symbol: str = "KRW=X") -> list[tuple[str, float]]:
    period2 = int((utc_now() + timedelta(days=2)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        f"?period1=0&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    try:
        with urlopen(Request(url, headers={"User-Agent": "quant-guardian-isa/1.0"}), timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise IsaStrategyError(f"Yahoo 환율 조회 실패: {exc}") from exc
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise IsaStrategyError(f"Yahoo 환율 오류: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise IsaStrategyError("Yahoo 환율 결과가 없습니다.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators", {})
    closes = (
        (indicators.get("adjclose") or [{}])[0].get("adjclose")
        or (indicators.get("quote") or [{}])[0].get("close")
        or []
    )
    rows = [
        (datetime.fromtimestamp(int(stamp), tz=UTC).date().isoformat(), float(close))
        for stamp, close in zip(timestamps, closes)
        if close is not None and float(close) > 0
    ]
    if len(rows) < FX_WINDOW:
        raise IsaStrategyError("52주 환율 z점수를 계산할 데이터가 부족합니다.")
    return rows


def fx_zone(z_score: float) -> str:
    if z_score >= FX_SELL_Z:
        return "HIGH"
    if z_score >= FX_WATCH_Z:
        return "WATCH"
    return "NORMAL"


def calculate_fx_snapshot(
    rows: Iterable[tuple[str, float]], *, window: int = FX_WINDOW
) -> FxSnapshot:
    values = [(str(date), float(close)) for date, close in rows if float(close) > 0]
    if len(values) < window:
        raise IsaStrategyError("환율 z점수 계산 데이터가 부족합니다.")
    window_rows = values[-window:]
    logs = [math.log(close) for _, close in window_rows]
    std = statistics.pstdev(logs)
    if std <= 0:
        raise IsaStrategyError("환율 표준편차가 0입니다.")
    date, latest = window_rows[-1]
    z_score = (math.log(latest) - statistics.fmean(logs)) / std
    return FxSnapshot(date, latest, z_score, fx_zone(z_score))


def fetch_fx_snapshot() -> FxSnapshot:
    return calculate_fx_snapshot(fetch_yahoo_close_series())


def calculate_purchase_plan(budget_krw: float, reference_price_krw: float) -> PurchasePlan:
    budget, price = float(budget_krw), float(reference_price_krw)
    if budget <= 0 or price <= 0:
        raise IsaStrategyError("매수예산과 기준가격은 0보다 커야 합니다.")
    shares = int(math.floor(budget / price))
    expected = shares * price
    return PurchasePlan(budget, price, shares, expected, budget - expected)


def portfolio_values(
    state: Mapping[str, Any],
    quotes: Mapping[str, QuoteSnapshot],
    *,
    proposed_tiger_budget_krw: float = 0.0,
) -> dict[str, float]:
    account = state["account"]
    values = {
        str(item["code"]): float(item["quantity"]) * quotes[str(item["code"])].close
        for item in account["existing_holdings"]
    }
    values[TIGER_CODE] = float(account.get("tiger_quantity", 0)) * quotes[TIGER_CODE].close
    total_before = sum(values.values())
    tiger_after = values[TIGER_CODE] + float(proposed_tiger_budget_krw)
    total_after = total_before + float(proposed_tiger_budget_krw)
    nasdaq_value_after = values.get("379810", 0.0) + 2.0 * tiger_after
    return {
        **{f"value_{code}": value for code, value in values.items()},
        "total_before": total_before,
        "total_after": total_after,
        "tiger_after": tiger_after,
        "tiger_weight_after": tiger_after / total_after if total_after > 0 else 0.0,
        "nasdaq_multiple_after": nasdaq_value_after / total_after if total_after > 0 else 0.0,
    }


def is_monthly_plan_due(
    state: Mapping[str, Any], *, now_kst: datetime, latest_quote_date: str
) -> bool:
    strategy = state["strategy"]
    if not bool(strategy.get("initial_completed")):
        return False
    period = now_kst.strftime("%Y-%m")
    start_period = strategy.get("monthly_start_period")
    if start_period and period < str(start_period):
        return False
    if strategy.get("last_monthly_plan_period") == period:
        return False
    try:
        quote_period = datetime.fromisoformat(latest_quote_date).strftime("%Y-%m")
    except ValueError:
        return False
    return quote_period == period and now_kst.weekday() < 5
