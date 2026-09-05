"""Fail closed on missing/mixed state caches. Never initialize or choose a newer account."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

BTC_PATH = Path("data/state/btc_fixed_state.json")
ISA_PATH = Path("data/state/isa_leverage_state.json")
BASELINE_PATH = Path("output/portfolio_state_baseline.json")


def read_valid_state(path: Path, kind: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{kind} 상태 복원 실패: 새 계좌로 자동 초기화하지 않습니다.")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = (5, "btc-fixed-six-clock-hybrid-1.1") if kind == "BTC" else (1, "isa-tiger-leverage-telegram-1.0")
    if (value.get("schema_version"), value.get("strategy_version")) != expected:
        raise ValueError(f"{kind} 상태 버전을 확인해 주세요. 자동 초기화 금지.")
    account = value.get("account")
    if not isinstance(account, dict):
        raise ValueError(f"{kind} 계좌 구조 오류")
    fields = ("cash_krw", "btc_quantity", "reserve_next_krw", "total_contributions_krw") if kind == "BTC" else ("tiger_quantity", "tiger_invested_krw")
    for field in fields:
        number = float(account.get(field, -1))
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{kind} {field} 비정상. 실행 중단.")
    if kind == "BTC":
        if account["reserve_next_krw"] > account["cash_krw"] + 1:
            raise ValueError("BTC 대기자금이 현금을 초과합니다.")
        if value["strategy"].get("phase") not in {"WAITING_ENTRY", "ENTRY", "HOLD", "EXIT"}:
            raise ValueError("BTC 전략 단계 오류")
        if not isinstance(value.get("telegram"), dict):
            raise ValueError("BTC Telegram 상태 오류")
    else:
        st = value["strategy"]
        if st.get("auto_order") is not False:
            raise ValueError("ISA 자동주문은 허용되지 않습니다.")
        if st.get("initial_investment_krw") != 10_000_000 or st.get("monthly_contribution_krw") != 500_000:
            raise ValueError("ISA 승인 예산과 다릅니다.")
        holdings = {x["code"]: x["quantity"] for x in account.get("existing_holdings", [])}
        if holdings != {"442580": 7.0, "0048J0": 145.0, "379810": 70.0}:
            raise ValueError("ISA 기존 보유수량이 승인값과 다릅니다.")
    return value


def validate_cache_pair(btc_key: str, isa_key: str, *, reset_btc: bool = False, reset_isa: bool = False) -> None:
    prefixes = ("btc-fixed-six-state-", "isa-tiger-leverage-state-")
    for key, prefix, resetting in zip((btc_key, isa_key), prefixes, (reset_btc, reset_isa)):
        if not resetting and not key.startswith(prefix):
            raise ValueError("상태 캐시 복원 증거가 없습니다. 초기화하지 않고 중단합니다.")
    if not (reset_btc or reset_isa) and btc_key[len(prefixes[0]):] != isa_key[len(prefixes[1]):]:
        raise ValueError("BTC·ISA가 서로 다른 실행의 캐시입니다. 임의 최신값 선택 금지.")


def run(mode: str) -> None:
    reset_btc = os.getenv("MANUAL_RESET_BTC_STATE") == "true"
    reset_isa = os.getenv("MANUAL_RESET_ISA_STATE") == "true"
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" and os.getenv("QG_TRIGGER_SOURCE", "manual") == "manual"
    lightweight = os.getenv("QG_SERVICE_ONLY") == "true"
    if (reset_btc or reset_isa) and (not is_manual or lightweight):
        raise ValueError("복구 트리거는 상태 초기화를 요청할 수 없습니다.")
    if mode == "preflight":
        validate_cache_pair(os.getenv("BTC_CACHE_KEY", ""), os.getenv("ISA_CACHE_KEY", ""), reset_btc=reset_btc, reset_isa=reset_isa)
        before = {}
        for kind, path, resetting in (("BTC", BTC_PATH, reset_btc), ("ISA", ISA_PATH, reset_isa)):
            if not resetting:
                state = read_valid_state(path, kind)
                before[kind] = {"created_at_utc": state.get("created_at_utc"), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                if kind == "BTC":
                    before[kind]["last_update_id"] = state["telegram"].get("last_update_id")
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(before), encoding="utf-8")
    else:
        before = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        for kind, path in (("BTC", BTC_PATH), ("ISA", ISA_PATH)):
            state = read_valid_state(path, kind)
            original = before.get(kind)
            if original and state.get("created_at_utc") != original.get("created_at_utc"):
                raise ValueError(f"{kind} 계좌가 재초기화된 것으로 보여 저장을 중단합니다.")
            if kind == "BTC" and original and original.get("last_update_id") is not None:
                if int(state["telegram"].get("last_update_id") or -1) < int(original["last_update_id"]):
                    raise ValueError("Telegram update 위치가 역행했습니다.")
    print(f"Portfolio state {mode}: validated; no account reset requested by recovery")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "finalize"))
    try:
        run(parser.parse_args().mode)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(str(exc))
