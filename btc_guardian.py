from __future__ import annotations

import argparse
import json
import math
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from quant_guardian import cache_key, fetch_yahoo_price


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
USER_AGENT = "quant-guardian-btc/0.1 shadow-research"
HALVING_INTERVAL = 210_000
INITIAL_SUBSIDY_BTC = 50.0
DEFAULT_PHASES = {
    "HALVING_TRANSITION": (0.00, 0.08),
    "POST_HALVING_EXPANSION": (0.08, 0.32),
    "LATE_EXPANSION_DISTRIBUTION": (0.32, 0.50),
    "CONTRACTION_RECOVERY": (0.50, 0.75),
    "PRE_HALVING_ACCUMULATION": (0.75, 1.00),
}
DEFAULT_ONCHAIN_METRICS = (
    "CapMVRVCur",
    "HashRate",
    "FeeTotNtv",
    "IssTotNtv",
    "IssTotUSD",
    "PriceUSD",
    "CapMrktCurUSD",
    "SplyCur",
)

JsonFetcher = Callable[[str], Any]
TextFetcher = Callable[[str], str]


class BtcDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyCandle:
    provider: str
    symbol: str
    quote_currency: str
    open_time_utc: datetime
    close_time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume_base: float | None
    volume_quote: float | None
    is_final: bool
    fetched_at_utc: datetime
    available_at_utc: datetime
    source_revision: str

    def __post_init__(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if self.open_time_utc.tzinfo is None or self.close_time_utc.tzinfo is None:
            raise ValueError("Candle timestamps must be timezone-aware")
        if self.open_time_utc >= self.close_time_utc:
            raise ValueError("Candle open time must precede close time")
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise ValueError("Candle prices must be finite and positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Candle OHLC values are inconsistent")
        for volume in (self.volume_base, self.volume_quote):
            if volume is not None and (not math.isfinite(volume) or volume < 0):
                raise ValueError("Candle volume must be finite and non-negative")


@dataclass(frozen=True)
class HalvingContext:
    tip_height: int
    tip_time_utc: datetime
    epoch: int
    epoch_start_height: int
    next_halving_height: int
    blocks_since_halving: int
    blocks_to_halving: int
    cycle_progress: float
    days_since_halving: float
    block_subsidy_btc: float
    estimated_days_to_halving: float
    estimated_next_halving_utc: datetime
    blocks_per_day_30: float
    blocks_per_day_90: float
    annualized_new_supply_pct: float | None
    phase_label: str
    source_primary: str
    source_backup: str | None
    verified: bool


@dataclass(frozen=True)
class SourceCheck:
    source: str
    status: str
    critical: bool
    message: str
    observation_time_utc: str | None = None
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class BtcPaths:
    cache: Path
    output: Path


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def fetch_text(url: str, retries: int = 2, pause: float = 0.5) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain"},
            )
            with urlopen(request, timeout=25) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(pause)
    raise BtcDataError(f"Could not fetch {url}: {last_error}")


def fetch_json(url: str) -> Any:
    try:
        return json.loads(fetch_text(url))
    except json.JSONDecodeError as exc:
        raise BtcDataError(f"Invalid JSON from {url}: {exc}") from exc


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_paths(cfg: dict) -> BtcPaths:
    settings = cfg.get("settings", {})
    cache = ROOT / settings.get("cache_dir", "data/cache") / "btc"
    output = ROOT / settings.get("output_dir", "output")
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    return BtcPaths(cache=cache, output=output)


def parse_upbit_candles(
    payload: Sequence[Mapping[str, Any]],
    *,
    fetched_at_utc: datetime | None = None,
    as_of_utc: datetime | None = None,
) -> list[DailyCandle]:
    fetched_at = fetched_at_utc or utc_now()
    as_of = as_of_utc or fetched_at
    candles: list[DailyCandle] = []
    for row in payload:
        try:
            open_time = parse_utc(str(row["candle_date_time_utc"]))
            close_time = open_time + timedelta(days=1)
            market = str(row.get("market", "KRW-BTC"))
            quote_currency = market.split("-", maxsplit=1)[0]
            candle = DailyCandle(
                provider="upbit",
                symbol=market,
                quote_currency=quote_currency,
                open_time_utc=open_time,
                close_time_utc=close_time,
                open=float(row["opening_price"]),
                high=float(row["high_price"]),
                low=float(row["low_price"]),
                close=float(row["trade_price"]),
                volume_base=_optional_float(row.get("candle_acc_trade_volume")),
                volume_quote=_optional_float(row.get("candle_acc_trade_price")),
                is_final=close_time <= as_of,
                fetched_at_utc=fetched_at,
                available_at_utc=close_time,
                source_revision=str(row.get("timestamp", "unknown")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BtcDataError(f"Invalid Upbit candle: {exc}") from exc
        candles.append(candle)
    return candles


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def fetch_upbit_history(
    market: str,
    start_date: date,
    *,
    fetcher: JsonFetcher = fetch_json,
    as_of_utc: datetime | None = None,
    pause: float = 0.12,
    max_pages: int = 40,
) -> list[DailyCandle]:
    as_of = as_of_utc or utc_now()
    rows_by_time: dict[datetime, DailyCandle] = {}
    cursor: datetime | None = None
    previous_oldest: datetime | None = None

    for page in range(max_pages):
        query: dict[str, str | int] = {"market": market, "count": 200}
        if cursor is not None:
            query["to"] = iso_utc(cursor)
        url = "https://api.upbit.com/v1/candles/days?" + urlencode(query)
        payload = fetcher(url)
        if not isinstance(payload, list):
            raise BtcDataError("Upbit returned a non-list candle response")
        if not payload:
            break

        parsed = parse_upbit_candles(payload, fetched_at_utc=as_of, as_of_utc=as_of)
        for candle in parsed:
            if candle.is_final and candle.open_time_utc.date() >= start_date:
                rows_by_time[candle.open_time_utc] = candle

        oldest = min(candle.open_time_utc for candle in parsed)
        if oldest.date() <= start_date or len(payload) < 200:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            raise BtcDataError("Upbit pagination did not move backward")
        previous_oldest = oldest
        cursor = oldest
        if pause > 0 and page + 1 < max_pages:
            time.sleep(pause)
    else:
        raise BtcDataError(f"Upbit history exceeded {max_pages} pages")

    candles = sorted(rows_by_time.values(), key=lambda item: item.open_time_utc)
    if not candles:
        raise BtcDataError("Upbit returned no finalized candles in the requested range")
    return candles


def candles_to_frame(candles: Sequence[DailyCandle]) -> pd.DataFrame:
    rows = []
    for candle in candles:
        rows.append(
            {
                "Date": candle.open_time_utc.date().isoformat(),
                "Open": candle.open,
                "High": candle.high,
                "Low": candle.low,
                "Close": candle.close,
                "Volume": candle.volume_base,
                "QuoteVolume": candle.volume_quote,
                "CloseTimeUTC": iso_utc(candle.close_time_utc),
                "FetchedAtUTC": iso_utc(candle.fetched_at_utc),
                "SourceRevision": candle.source_revision,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise BtcDataError("Cannot create a price frame from zero candles")
    frame["Date"] = pd.to_datetime(frame["Date"])
    return frame.drop_duplicates("Date", keep="last").sort_values("Date").set_index("Date")


def read_price_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise BtcDataError(f"Cache does not exist: {path}")
    frame = pd.read_csv(path)
    if frame.empty or "Date" not in frame or "Close" not in frame:
        raise BtcDataError(f"Invalid price cache: {path}")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date", "Close"]).drop_duplicates("Date").sort_values("Date")
    if frame.empty:
        raise BtcDataError(f"Price cache has no usable rows: {path}")
    for column in ("Open", "High", "Low", "Close", "Volume", "QuoteVolume"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.set_index("Date")


def write_price_cache(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.reset_index().to_csv(path, index=False, encoding="utf-8")


@dataclass(frozen=True)
class EsploraProvider:
    name: str
    base_url: str
    text_fetcher: TextFetcher = fetch_text
    json_fetcher: JsonFetcher = fetch_json

    def tip_height(self) -> int:
        raw = self.text_fetcher(f"{self.base_url.rstrip('/')}/blocks/tip/height").strip()
        try:
            height = int(raw)
        except ValueError as exc:
            raise BtcDataError(f"{self.name} returned invalid tip height: {raw!r}") from exc
        if height < 0:
            raise BtcDataError(f"{self.name} returned a negative tip height")
        return height

    def block_time(self, height: int) -> datetime:
        base = self.base_url.rstrip("/")
        block_hash = self.text_fetcher(f"{base}/block-height/{height}").strip()
        if not block_hash:
            raise BtcDataError(f"{self.name} returned an empty hash for height {height}")
        payload = self.json_fetcher(f"{base}/block/{block_hash}")
        try:
            timestamp = int(payload["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BtcDataError(f"{self.name} returned invalid block data at {height}") from exc
        return datetime.fromtimestamp(timestamp, UTC)


def phase_for_progress(
    progress: float,
    phases: Mapping[str, tuple[float, float]] = DEFAULT_PHASES,
) -> str:
    if not 0 <= progress < 1:
        raise ValueError("Cycle progress must be in [0, 1)")
    for label, (start, end) in phases.items():
        if start <= progress < end or math.isclose(progress, start):
            return label
    raise ValueError(f"No phase covers progress {progress}")


def theoretical_supply_btc(height: int) -> float:
    if height < 0:
        raise ValueError("Block height must be non-negative")
    remaining_blocks = height + 1
    subsidy = INITIAL_SUBSIDY_BTC
    total = 0.0
    while remaining_blocks > 0 and subsidy > 0:
        blocks = min(remaining_blocks, HALVING_INTERVAL)
        total += blocks * subsidy
        remaining_blocks -= blocks
        subsidy /= 2
    return total


def calculate_halving_context(
    *,
    tip_height: int,
    tip_time_utc: datetime,
    epoch_start_time_utc: datetime,
    blocks_per_day_30: float,
    blocks_per_day_90: float,
    source_primary: str,
    source_backup: str | None,
    verified: bool,
) -> HalvingContext:
    if tip_height < 0:
        raise ValueError("Tip height must be non-negative")
    if tip_time_utc.tzinfo is None or epoch_start_time_utc.tzinfo is None:
        raise ValueError("Block timestamps must be timezone-aware")
    if blocks_per_day_30 <= 0 or blocks_per_day_90 <= 0:
        raise ValueError("Block production rates must be positive")

    epoch = tip_height // HALVING_INTERVAL
    epoch_start_height = epoch * HALVING_INTERVAL
    next_halving_height = (epoch + 1) * HALVING_INTERVAL
    blocks_since = tip_height - epoch_start_height
    blocks_to = next_halving_height - tip_height
    progress = blocks_since / HALVING_INTERVAL
    subsidy = INITIAL_SUBSIDY_BTC / (2**epoch)
    blocks_per_day = _weighted_median(
        (blocks_per_day_30, blocks_per_day_90),
        (2.0, 1.0),
    )
    estimated_days = blocks_to / blocks_per_day
    estimated_halving = tip_time_utc + timedelta(days=estimated_days)
    days_since = max(0.0, (tip_time_utc - epoch_start_time_utc).total_seconds() / 86_400)
    supply = theoretical_supply_btc(tip_height)
    annualized_supply = subsidy * blocks_per_day * 365 / supply * 100 if supply > 0 else None
    return HalvingContext(
        tip_height=tip_height,
        tip_time_utc=tip_time_utc.astimezone(UTC),
        epoch=epoch,
        epoch_start_height=epoch_start_height,
        next_halving_height=next_halving_height,
        blocks_since_halving=blocks_since,
        blocks_to_halving=blocks_to,
        cycle_progress=progress,
        days_since_halving=days_since,
        block_subsidy_btc=subsidy,
        estimated_days_to_halving=estimated_days,
        estimated_next_halving_utc=estimated_halving.astimezone(UTC),
        blocks_per_day_30=blocks_per_day_30,
        blocks_per_day_90=blocks_per_day_90,
        annualized_new_supply_pct=annualized_supply,
        phase_label=phase_for_progress(progress),
        source_primary=source_primary,
        source_backup=source_backup,
        verified=verified,
    )


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    if len(values) != len(weights) or not values:
        raise ValueError("Values and weights must have the same non-zero length")
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        raise ValueError("Weights must sum to a positive value")
    midpoint = total / 2
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return pairs[-1][0]


def build_halving_context(
    primary: EsploraProvider,
    backup: EsploraProvider,
    *,
    max_height_gap: int = 3,
) -> HalvingContext:
    observations: list[tuple[EsploraProvider, int]] = []
    errors: list[str] = []
    for provider in (primary, backup):
        try:
            observations.append((provider, provider.tip_height()))
        except Exception as exc:
            errors.append(f"{provider.name}: {exc}")
    if not observations:
        raise BtcDataError("Both block-height providers failed: " + " | ".join(errors))

    verified = len(observations) == 2
    if verified:
        gap = abs(observations[0][1] - observations[1][1])
        if gap > max_height_gap:
            raise BtcDataError(
                f"Block-height providers disagree by {gap} blocks, limit is {max_height_gap}"
            )
        tip_height = min(height for _, height in observations)
    else:
        tip_height = observations[0][1]

    selected = next(provider for provider, height in observations if height >= tip_height)
    tip_time = selected.block_time(tip_height)
    epoch_start_height = (tip_height // HALVING_INTERVAL) * HALVING_INTERVAL
    epoch_start_time = selected.block_time(epoch_start_height)

    def production_rate(days: int) -> float:
        target_height = max(epoch_start_height, tip_height - days * 144)
        target_time = selected.block_time(target_height)
        elapsed_days = (tip_time - target_time).total_seconds() / 86_400
        if elapsed_days <= 0:
            raise BtcDataError(f"Invalid {days}-day block timestamp window")
        return (tip_height - target_height) / elapsed_days

    source_backup = None
    if len(observations) == 2:
        source_backup = next(provider.name for provider, _ in observations if provider is not selected)
    return calculate_halving_context(
        tip_height=tip_height,
        tip_time_utc=tip_time,
        epoch_start_time_utc=epoch_start_time,
        blocks_per_day_30=production_rate(30),
        blocks_per_day_90=production_rate(90),
        source_primary=selected.name,
        source_backup=source_backup,
        verified=verified,
    )


def coinmetrics_daily_metrics(payload: Mapping[str, Any], asset: str = "btc") -> set[str]:
    records = payload.get("data")
    if not isinstance(records, list):
        raise BtcDataError("Coin Metrics catalog is missing its data array")
    record = next((item for item in records if item.get("asset") == asset), None)
    if not isinstance(record, Mapping):
        raise BtcDataError(f"Coin Metrics catalog has no {asset} record")
    metrics = record.get("metrics")
    if not isinstance(metrics, list):
        raise BtcDataError(f"Coin Metrics catalog has no metric list for {asset}")

    available: set[str] = set()
    for item in metrics:
        if not isinstance(item, Mapping):
            continue
        metric = item.get("metric")
        frequencies = item.get("frequencies")
        if not isinstance(metric, str) or not isinstance(frequencies, list):
            continue
        if any(
            isinstance(entry, Mapping) and entry.get("frequency") == "1d"
            for entry in frequencies
        ):
            available.add(metric)
    return available


def check_coinmetrics_catalog(
    payload: Mapping[str, Any],
    required_metrics: Sequence[str] = DEFAULT_ONCHAIN_METRICS,
) -> dict[str, Any]:
    available = coinmetrics_daily_metrics(payload)
    required = set(required_metrics)
    return {
        "required": sorted(required),
        "available": sorted(required & available),
        "missing": sorted(required - available),
        "catalog_daily_metric_count": len(available),
    }


def derive_onchain_values(values: Mapping[str, float]) -> dict[str, float | str]:
    try:
        mvrv = float(values["CapMVRVCur"])
        market_cap = float(values["CapMrktCurUSD"])
        supply = float(values["SplyCur"])
        issuance_usd = float(values["IssTotUSD"])
        fees_btc = float(values["FeeTotNtv"])
        price_usd = float(values["PriceUSD"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BtcDataError(f"On-chain derivation input is incomplete: {exc}") from exc
    if min(mvrv, market_cap, supply, price_usd) <= 0 or issuance_usd < 0 or fees_btc < 0:
        raise BtcDataError("On-chain derivation input is outside its valid range")
    realized_cap = market_cap / mvrv
    return {
        "realized_cap_usd": realized_cap,
        "realized_price_usd": realized_cap / supply,
        "nupl": 1 - 1 / mvrv,
        "miner_revenue_usd": issuance_usd + fees_btc * price_usd,
        "valuation_evidence_family": "mvrv-derived",
    }


def halving_context_to_dict(context: HalvingContext) -> dict[str, Any]:
    payload = asdict(context)
    payload["tip_time_utc"] = iso_utc(context.tip_time_utc)
    payload["estimated_next_halving_utc"] = iso_utc(context.estimated_next_halving_utc)
    return payload


def _load_or_refresh_yahoo(
    ticker: str,
    cache_dir: Path,
    *,
    refresh: bool,
) -> tuple[pd.DataFrame, bool, str | None]:
    cache_file = cache_dir / cache_key("yahoo", ticker)
    try:
        if refresh:
            frame = fetch_yahoo_price(ticker)
            write_price_cache(frame, cache_file)
            return frame, False, None
        return read_price_cache(cache_file), False, None
    except Exception as exc:
        if refresh and cache_file.exists():
            return read_price_cache(cache_file), True, str(exc)
        raise BtcDataError(f"Yahoo {ticker} unavailable: {exc}") from exc


def _load_or_refresh_upbit(
    market: str,
    start_date: date,
    cache_file: Path,
    *,
    refresh: bool,
    as_of_utc: datetime,
) -> tuple[pd.DataFrame, bool, str | None]:
    try:
        if refresh:
            candles = fetch_upbit_history(market, start_date, as_of_utc=as_of_utc)
            frame = candles_to_frame(candles)
            write_price_cache(frame, cache_file)
            return frame, False, None
        return read_price_cache(cache_file), False, None
    except Exception as exc:
        if refresh and cache_file.exists():
            return read_price_cache(cache_file), True, str(exc)
        raise BtcDataError(f"Upbit {market} unavailable: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BtcDataError(f"Invalid JSON cache {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BtcDataError(f"JSON cache must contain an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _source_check(
    source: str,
    *,
    status: str,
    critical: bool,
    message: str,
    observation: datetime | None = None,
    details: Mapping[str, Any] | None = None,
) -> SourceCheck:
    return SourceCheck(
        source=source,
        status=status,
        critical=critical,
        message=message,
        observation_time_utc=iso_utc(observation) if observation else None,
        details=details,
    )


def _latest_price_time(frame: pd.DataFrame) -> datetime:
    stamp = pd.Timestamp(frame.index.max())
    return stamp.to_pydatetime().replace(tzinfo=UTC)


def closed_yahoo_daily_frame(frame: pd.DataFrame, as_of_utc: datetime) -> pd.DataFrame:
    cutoff = pd.Timestamp(as_of_utc.astimezone(UTC).date())
    closed = frame.loc[pd.DatetimeIndex(frame.index) < cutoff].copy()
    if closed.empty:
        raise BtcDataError("Yahoo has no fully closed UTC daily candle")
    return closed


def _status_from_checks(checks: Sequence[SourceCheck]) -> str:
    if any(item.status == "error" and item.critical for item in checks):
        return "error"
    if any(item.status in {"error", "warning", "stale"} for item in checks):
        return "warning"
    return "ok"


def build_phase1_report(
    *,
    refresh: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    now = now_utc or utc_now()
    btc = cfg.get("btc", {})
    data_cfg = btc.get("data", {})
    if str(btc.get("run_mode", "shadow")) != "shadow":
        raise BtcDataError("BTC phase 1 can run only in shadow mode")
    if bool(btc.get("auto_order", False)):
        raise BtcDataError("BTC phase 1 refuses configurations with automatic orders enabled")
    market = str(btc.get("execution_market", "KRW-BTC"))
    usd_symbol = str(data_cfg.get("usd_symbol", "BTC-USD"))
    fx_symbol = str(data_cfg.get("fx_symbol", "KRW=X"))
    start_date = date.fromisoformat(str(data_cfg.get("upbit_start_date", "2017-09-25")))
    max_height_gap = int(data_cfg.get("max_block_height_gap", 3))
    block_staleness_hours = float(data_cfg.get("block_staleness_hours", 1.0))
    required_metrics = tuple(data_cfg.get("coinmetrics_required_metrics", DEFAULT_ONCHAIN_METRICS))
    checks: list[SourceCheck] = []
    report: dict[str, Any] = {
        "schema_version": "btc-phase1-0.1",
        "generated_at_utc": iso_utc(now),
        "run_mode": str(btc.get("run_mode", "shadow")),
        "auto_order": bool(btc.get("auto_order", False)),
        "market": market,
    }

    upbit_frame: pd.DataFrame | None = None
    usd_frame: pd.DataFrame | None = None
    fx_frame: pd.DataFrame | None = None

    upbit_cache = paths.cache / f"upbit_{market.replace('-', '_')}_daily.csv"
    try:
        upbit_frame, fallback, error = _load_or_refresh_upbit(
            market,
            start_date,
            upbit_cache,
            refresh=refresh,
            as_of_utc=now,
        )
        latest = _latest_price_time(upbit_frame) + timedelta(days=1)
        expected_close = datetime(now.year, now.month, now.day, tzinfo=UTC)
        missing_days = max(0, (expected_close.date() - latest.date()).days)
        status = "error" if missing_days > 0 else ("stale" if fallback else "ok")
        message = f"확정 일봉 {len(upbit_frame):,}개, 최근 마감 {iso_utc(latest)}"
        if fallback:
            message += f"; 새 수집 실패로 캐시 사용: {error}"
        checks.append(
            _source_check(
                "upbit_daily",
                status=status,
                critical=True,
                message=message,
                observation=latest,
                details={"rows": len(upbit_frame), "missing_days": missing_days},
            )
        )
    except Exception as exc:
        checks.append(
            _source_check("upbit_daily", status="error", critical=True, message=str(exc))
        )

    yahoo_cache_dir = ROOT / cfg.get("settings", {}).get("cache_dir", "data/cache")
    for source, ticker, critical in (
        ("btc_usd_daily", usd_symbol, True),
        ("usdkrw_daily", fx_symbol, False),
    ):
        try:
            frame, fallback, error = _load_or_refresh_yahoo(
                ticker,
                yahoo_cache_dir,
                refresh=refresh,
            )
            frame = closed_yahoo_daily_frame(frame, now)
            if source == "btc_usd_daily":
                usd_frame = frame
            else:
                fx_frame = frame
            latest_open = _latest_price_time(frame)
            latest_close = latest_open + timedelta(days=1)
            age_days = max(0, (now.date() - latest_close.date()).days)
            stale = fallback or age_days > (1 if critical else 4)
            status = "error" if critical and age_days > 1 else ("stale" if stale else "ok")
            message = (
                f"최근 확정 일봉 {latest_open.date().isoformat()}, "
                f"종가 {float(frame['Close'].iloc[-1]):,.4f}"
            )
            if fallback:
                message += f"; 새 수집 실패로 캐시 사용: {error}"
            checks.append(
                _source_check(
                    source,
                    status=status,
                    critical=critical,
                    message=message,
                    observation=latest_close,
                    details={"rows": len(frame), "age_days": age_days},
                )
            )
        except Exception as exc:
            checks.append(_source_check(source, status="error", critical=critical, message=str(exc)))

    if upbit_frame is not None and usd_frame is not None and fx_frame is not None:
        combined = pd.concat(
            {
                "upbit_krw": upbit_frame["Close"],
                "btc_usd": usd_frame["Close"],
                "usdkrw": fx_frame["Close"],
            },
            axis=1,
        ).sort_index().ffill().dropna()
        if not combined.empty:
            row = combined.iloc[-1]
            implied_usd = float(row["upbit_krw"] / row["usdkrw"])
            premium = implied_usd / float(row["btc_usd"]) - 1
            report["market_crosscheck"] = {
                "date": combined.index[-1].date().isoformat(),
                "upbit_krw": float(row["upbit_krw"]),
                "btc_usd": float(row["btc_usd"]),
                "usdkrw": float(row["usdkrw"]),
                "upbit_implied_usd": implied_usd,
                "krw_market_premium_pct": premium * 100,
            }

    block_cache = paths.cache / "halving_context.json"
    try:
        if refresh:
            context = build_halving_context(
                EsploraProvider("mempool.space", "https://mempool.space/api"),
                EsploraProvider("blockstream.info", "https://blockstream.info/api"),
                max_height_gap=max_height_gap,
            )
            context_payload = halving_context_to_dict(context)
            _write_json(block_cache, context_payload)
        else:
            context_payload = _read_json(block_cache)
        report["halving"] = context_payload
        verified = bool(context_payload.get("verified"))
        tip_time = parse_utc(str(context_payload["tip_time_utc"]))
        tip_age_hours = max(0.0, (now - tip_time).total_seconds() / 3_600)
        if tip_age_hours > block_staleness_hours:
            block_status = "error"
        else:
            block_status = "ok" if verified else "warning"
        checks.append(
            _source_check(
                "bitcoin_block_tip",
                status=block_status,
                critical=True,
                message=(
                    f"높이 {int(context_payload['tip_height']):,}, "
                    f"사이클 {float(context_payload['cycle_progress']) * 100:.2f}%, "
                    f"{context_payload['phase_label']}"
                    + (
                        f"; 마지막 블록 {tip_age_hours:.1f}시간 경과"
                        if block_status == "error"
                        else ""
                    )
                ),
                observation=tip_time,
                details={
                    "verified": verified,
                    "primary": context_payload.get("source_primary"),
                    "backup": context_payload.get("source_backup"),
                    "tip_age_hours": tip_age_hours,
                },
            )
        )
    except Exception as exc:
        checks.append(
            _source_check("bitcoin_block_tip", status="error", critical=True, message=str(exc))
        )

    catalog_cache = paths.cache / "coinmetrics_catalog_btc.json"
    try:
        if refresh:
            endpoint = "https://community-api.coinmetrics.io/v4/catalog-v2/asset-metrics?assets=btc"
            catalog_payload = fetch_json(endpoint)
            if not isinstance(catalog_payload, dict):
                raise BtcDataError("Coin Metrics returned a non-object catalog")
            _write_json(catalog_cache, catalog_payload)
        else:
            catalog_payload = _read_json(catalog_cache)
        catalog_check = check_coinmetrics_catalog(catalog_payload, required_metrics)
        report["coinmetrics_catalog"] = catalog_check
        missing = catalog_check["missing"]
        checks.append(
            _source_check(
                "coinmetrics_catalog",
                status="warning" if missing else "ok",
                critical=False,
                message=(
                    f"무료 일봉 필수 후보 {len(catalog_check['available'])}/{len(catalog_check['required'])}개 확인"
                    + (f"; 미확인: {', '.join(missing)}" if missing else "")
                ),
                observation=now,
                details=catalog_check,
            )
        )
    except Exception as exc:
        checks.append(
            _source_check("coinmetrics_catalog", status="error", critical=False, message=str(exc))
        )

    report["sources"] = [asdict(item) for item in checks]
    report["overall_status"] = _status_from_checks(checks)
    report["data_gate"] = "pass" if report["overall_status"] != "error" else "blocked"
    report["signal_calculation_allowed"] = report["data_gate"] == "pass"
    report["advisory_actions_enabled"] = False
    report["notice_ko"] = (
        "모의운영용 데이터 점검입니다. 자동주문을 하지 않으며, 매수·매도 전략은 아직 승인되지 않았습니다."
    )
    output_path = paths.output / "btc_data_report.json"
    _write_json(output_path, report)
    report["output_path"] = str(output_path)
    return report


def print_report_summary(report: Mapping[str, Any]) -> None:
    labels = {"ok": "정상", "warning": "주의", "stale": "지연", "error": "오류"}
    print("Quant Guardian BTC 1단계 데이터 점검")
    print(f"전체 상태: {labels.get(str(report['overall_status']), report['overall_status'])}")
    print(f"실행 모드: {report['run_mode']} / 자동주문: {'켜짐' if report['auto_order'] else '꺼짐'}")
    for item in report.get("sources", []):
        status = labels.get(str(item["status"]), item["status"])
        print(f"- {item['source']}: {status} - {item['message']}")
    if report.get("halving"):
        halving = report["halving"]
        print(
            f"반감기 진행률: {float(halving['cycle_progress']) * 100:.2f}% "
            f"({halving['phase_label']})"
        )
    print("자동주문 없음. 이 단계는 데이터와 반감기 계산 검증용입니다.")
    print(f"JSON 저장: {report['output_path']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant Guardian BTC shadow research tools")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    subparsers = parser.add_subparsers(dest="command", required=True)
    data_report = subparsers.add_parser("data-report", help="Build the BTC phase-1 data report")
    data_report.add_argument("--refresh", action="store_true", help="Refresh all free data sources")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "data-report":
            report = build_phase1_report(
                refresh=args.refresh,
                config_path=Path(args.config),
            )
            print_report_summary(report)
            return 1 if report["overall_status"] == "error" else 0
    except (BtcDataError, ValueError, OSError) as exc:
        print(f"BTC 데이터 점검 실패: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
