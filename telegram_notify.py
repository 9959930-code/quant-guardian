from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://9959930-code.github.io/quant-guardian/"


def load_daily_payload(path: str | None, url: str | None) -> dict:
    if url:
        request = Request(url, headers={"User-Agent": "quant-guardian-telegram/1.0"})
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    data_path = Path(path or "output/daily.json")
    return json.loads(data_path.read_text(encoding="utf-8-sig"))


def pct(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}%"


def signed_pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.1f}%"


def krw(value: float) -> str:
    return f"{round(value):,}원"


def parse_capital(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return amount if amount > 0 else None


def build_message(payload: dict, site_url: str, capital_krw: float | None = None) -> str:
    advice = payload.get("daily_advice", {})
    decision = payload.get("market_decision") or payload.get("regime", {})
    etfs = payload.get("top_etfs", [])
    top = etfs[0] if etfs else {}
    plan = payload.get("plan", [])
    metrics = payload.get("qg_core_metrics", {})
    benchmarks = payload.get("benchmarks", {})

    plan_lines = []
    for row in plan:
        weight = float(row.get("weight", 0))
        amount = f" · 목표 {krw(capital_krw * weight)}" if capital_krw else ""
        plan_lines.append(
            f"- {row.get('asset', '-')}: {weight * 100:.1f}%{amount} · {row.get('action', '-')}"
        )
    if not plan_lines:
        plan_lines = ["- 계산된 목표 비중 없음"]

    positives = advice.get("positives", [])
    cautions = advice.get("cautions", [])
    reason_lines = [f"+ {item}" for item in positives] + [f"! {item}" for item in cautions]
    if not reason_lines:
        reason_lines = [f"- {decision.get('reason', '확인 가능한 근거 없음')}"]

    capital_note = (
        f"총 투자금 기준: {krw(capital_krw)}"
        if capital_krw
        else "원화 금액 미설정: 비중만 표시 (선택 시 PORTFOLIO_VALUE_KRW 시크릿 사용)"
    )
    top_signal = advice.get("top_etf_signal") or top.get("ticker", "-")
    top_execution = advice.get("top_etf_execution") or top.get("execution_ticker", top_signal)
    top_price = advice.get("top_etf_last") or top.get("last")
    top_price_text = f"${float(top_price):.2f}" if top_price is not None else "-"
    top_score = top.get("score", advice.get("top_etf_score"))
    top_score_text = f"{float(top_score):.1f}" if top_score is not None else "-"
    top_rsi = top.get("rsi14")
    top_rsi_text = f"{float(top_rsi):.1f}" if top_rsi is not None else "-"

    return "\n".join(
        [
            "[Quant Guardian 지수 타이밍]",
            f"기준일: {advice.get('as_of') or decision.get('as_of', '-')} 미국장 마감",
            "",
            "[오늘 할 일]",
            f"- 판단: {advice.get('action', '보유·관찰')}",
            f"- 주식형 ETF 목표: {float(advice.get('target_equity_weight', 0)) * 100:.0f}%",
            f"- 먼저 확인: {top_execution} (신호 지수 {top_signal})",
            f"- 신호 지수 종가: {top_price_text}",
            f"- 실행 시점: {advice.get('top_etf_timing') or top.get('timing', '다음 갱신까지 대기')}",
            "",
            "[왜 이렇게 판단했나]",
            *reason_lines,
            f"- 종합점수: {top_score_text}/100",
            f"- RSI: {top_rsi_text} · 일목: {top.get('ichimoku_state', '-')} · 200일선: {'위' if top.get('above_200d') else '아래'}",
            "",
            "[목표 보유 비중]",
            capital_note,
            *plan_lines,
            "실제 주문 검토액 = 목표 평가액 - 현재 보유 평가액",
            "",
            "[보유·매도 기준]",
            "- 보유 중이면 목표 비중과 비교해 초과분만 줄입니다.",
            "- 신규매수는 표시된 분할 조건을 따르고, 종가가 50일선 아래면 다음 매수를 보류합니다.",
            "- 200일선과 일목 구름대 아래가 이어지면 비중축소 또는 매도·대기로 전환합니다.",
            "",
            "[백테스트 참고]",
            f"- 전략 CAGR/MDD: {pct(metrics.get('cagr_pct'))} / {pct(metrics.get('mdd_pct'))}",
            f"- SPY CAGR: {pct(benchmarks.get('spy_cagr_pct'))} · QQQ CAGR: {pct(benchmarks.get('qqq_cagr_pct'))}",
            "",
            "장중 실시간·자동주문이 아닙니다. 환율, 세금, 수수료와 현재 보유량은 직접 반영해야 합니다.",
            site_url,
        ]
    ).strip()


def send_message(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant Guardian 한글 매매 판단을 Telegram으로 전송")
    parser.add_argument("--data-file", default="output/daily.json")
    parser.add_argument("--data-url")
    parser.add_argument("--site-url", default=os.getenv("QUANT_GUARDIAN_URL", DEFAULT_URL))
    parser.add_argument("--soft-fail", action="store_true", help="Telegram 실패가 배포를 중단하지 않게 함")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram 시크릿이 없어 알림을 건너뜁니다.")
        return 0

    try:
        payload = load_daily_payload(args.data_file, args.data_url)
        capital = parse_capital(os.getenv("PORTFOLIO_VALUE_KRW"))
        send_message(token, chat_id, build_message(payload, args.site_url, capital))
        print("Telegram 알림을 보냈습니다.")
        return 0
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"Telegram 알림 실패: {exc}", file=sys.stderr)
        return 0 if args.soft_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
