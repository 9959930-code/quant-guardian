from __future__ import annotations

from typing import Any, Mapping

from isa_leverage_core import (
    INITIAL_INVESTMENT_KRW,
    MONTHLY_CONTRIBUTION_KRW,
    TIGER_CODE,
    TIGER_NAME,
    FxSnapshot,
    QuoteSnapshot,
    calculate_purchase_plan,
    remaining_initial_budget,
    krw,
    number,
    pct,
    portfolio_values,
)


def fx_status_text(fx: FxSnapshot | None) -> str:
    if fx is None:
        return "환율 Shadow: 계산 불가"
    label = {"NORMAL": "정상", "WATCH": "주의", "HIGH": "과열경고"}[fx.zone]
    return f"환율 Shadow: {label} · z {fx.z_52w:+.2f} · USD/KRW {fx.usdkrw:,.2f}"


def initial_plan_message(
    state: Mapping[str, Any],
    quotes: Mapping[str, QuoteSnapshot],
    fx: FxSnapshot | None,
) -> str:
    tiger = quotes[TIGER_CODE]
    budget = remaining_initial_budget(state)
    if budget <= 0:
        return "[ISA 초기매수 확인]\n초기예산의 누적 매수원금이 모두 기록되어 있습니다. 추가 주문 대신 초기매수 완료 상태를 확인하세요."
    plan = calculate_purchase_plan(budget, tiger.close)
    portfolio = portfolio_values(state, quotes, proposed_tiger_budget_krw=plan.expected_order_krw)
    lines = [
        "[Quant Guardian ISA · 초기매수]",
        "",
        f"- 상품: {TIGER_NAME} ({TIGER_CODE})",
        f"- 초기 신규자금: {krw(INITIAL_INVESTMENT_KRW)}",
        f"- 기존 투입원금 차감 후 잔여예산: {krw(budget)}",
        f"- 기준가격: {krw(tiger.close)} · {tiger.date}",
        f"- 주문 검토수량: {plan.shares:,}주",
        f"- 예상 주문금액: {krw(plan.expected_order_krw)}",
        f"- 예상 잔여현금: {krw(plan.expected_remainder_krw)}",
        "",
        "[기존 ISA 보유 · 매도 없음]",
    ]
    for holding in state["account"]["existing_holdings"]:
        code = str(holding["code"])
        value = float(holding["quantity"]) * quotes[code].close
        lines.append(f"- {holding['name']}: {number(holding['quantity'], 0)}주 · {krw(value)}")
    lines.extend(
        [
            "",
            "[매수 후 예상 구조]",
            f"- ISA 평가액 근사: {krw(portfolio['total_after'])}",
            f"- TIGER 비중 근사: {pct(portfolio['tiger_weight_after'])}",
            f"- 나스닥 명목노출 근사: {portfolio['nasdaq_multiple_after']:.2f}배",
            f"- {fx_status_text(fx)}",
            "",
            "시장가 자동주문이 아닙니다.",
            "호가·iNAV를 확인해 직접 주문하고 실제 수량·사용금액을 동기화해야 합니다.",
        ]
    )
    return "\n".join(lines)


def monthly_plan_message(
    state: Mapping[str, Any],
    quotes: Mapping[str, QuoteSnapshot],
    fx: FxSnapshot | None,
    period: str,
) -> str:
    tiger = quotes[TIGER_CODE]
    plan = calculate_purchase_plan(MONTHLY_CONTRIBUTION_KRW, tiger.close)
    portfolio = portfolio_values(state, quotes, proposed_tiger_budget_krw=plan.expected_order_krw)
    account = state["account"]
    return "\n".join(
        [
            f"[Quant Guardian ISA · {period} 월간매수]",
            "",
            f"- 월 신규자금: {krw(MONTHLY_CONTRIBUTION_KRW)}",
            f"- 상품: {TIGER_NAME} ({TIGER_CODE})",
            f"- 기준가격: {krw(tiger.close)} · {tiger.date}",
            f"- 주문 검토수량: {plan.shares:,}주",
            f"- 예상 주문금액: {krw(plan.expected_order_krw)}",
            f"- 예상 잔여현금: {krw(plan.expected_remainder_krw)}",
            "",
            f"- 현재 TIGER 수량: {number(account.get('tiger_quantity'))}주",
            f"- 누적 TIGER 투입원금: {krw(account.get('tiger_invested_krw'))}",
            f"- 매수 후 TIGER 비중 근사: {pct(portfolio['tiger_weight_after'])}",
            f"- 매수 후 나스닥 명목노출 근사: {portfolio['nasdaq_multiple_after']:.2f}배",
            f"- {fx_status_text(fx)}",
            "",
            "기존 3개 ETF는 매도하지 않습니다.",
            "자동주문은 없으며 체결 후 실제 잔고를 동기화해야 합니다.",
        ]
    )


def status_message(
    state: Mapping[str, Any],
    quotes: Mapping[str, QuoteSnapshot],
    fx: FxSnapshot | None,
    *,
    title: str = "상태보고",
) -> str:
    account, strategy = state["account"], state["strategy"]
    portfolio = portfolio_values(state, quotes)
    tiger_value = portfolio[f"value_{TIGER_CODE}"]
    tiger_weight = tiger_value / portfolio["total_before"] if portfolio["total_before"] > 0 else 0
    return "\n".join(
        [
            "[Quant Guardian ISA · 레버리지 적립]",
            f"[{title}]",
            "",
            f"- 초기 1,000만원 매수 완료: {'예' if strategy.get('initial_completed') else '아니오'}",
            f"- 월 신규자금: {krw(MONTHLY_CONTRIBUTION_KRW)}",
            f"- TIGER 수량: {number(account.get('tiger_quantity'))}주",
            f"- TIGER 평가액: {krw(tiger_value)}",
            f"- TIGER 누적투입원금: {krw(account.get('tiger_invested_krw'))}",
            f"- TIGER 기준가격: {krw(quotes[TIGER_CODE].close)} · {quotes[TIGER_CODE].date}",
            f"- ISA 전체 평가액 근사: {krw(portfolio['total_before'])}",
            f"- ISA 내 TIGER 비중: {pct(tiger_weight)}",
            f"- 나스닥 명목노출 근사: {portfolio['nasdaq_multiple_after']:.2f}배",
            f"- ISA 누적 납입원금: {krw(account.get('isa_total_contributions_krw'))}",
            f"- {fx_status_text(fx)}",
            "",
            "기존 ETF 매도 없음 · 자동주문 없음",
        ]
    )


def fx_zone_change_message(previous: str, current: FxSnapshot) -> str:
    labels = {"NORMAL": "정상", "WATCH": "주의", "HIGH": "과열경고"}
    lines = [
        "[ISA TIGER · 환율 Shadow 변경]",
        f"- 이전: {labels.get(previous, previous)}",
        f"- 현재: {labels[current.zone]}",
        f"- 기준일: {current.date}",
        f"- USD/KRW: {current.usdkrw:,.2f}",
        f"- 52주 로그환율 z: {current.z_52w:+.2f}",
        "",
    ]
    if current.zone == "HIGH":
        lines.extend(
            [
                "연구상 축소 검토구간이지만 자동매도하지 않습니다.",
                "월간 적립도 자동으로 중단하지 않고 경고만 기록합니다.",
            ]
        )
    else:
        lines.append("Shadow 신호만 변경됐으며 자동주문은 없습니다.")
    return "\n".join(lines)
