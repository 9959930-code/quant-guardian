from __future__ import annotations

import btc_clock_hybrid_runtime as clock_hybrid

clock_hybrid.install()

import portfolio_telegram_bot as portfolio  # noqa: E402


_ORIGINAL_UNIFIED_HELP = portfolio.unified_help_message


def clock_hybrid_help_message() -> str:
    return (
        _ORIGINAL_UNIFIED_HELP()
        + "\n\n[BTC 시계형 1.1]"
        + "\n62.5% 저점 관찰 · 65% 3주 분할매수"
        + "\n다음 epoch 35% 고점 경고 · 36/37/38% 분할매도"
        + "\n첫 매수 예상 5·3영업일 전 자금준비 알림"
        + "\n24시간 지난 주문안은 현재 금액 다시 계산"
    )


portfolio.unified_help_message = clock_hybrid_help_message


if __name__ == "__main__":
    raise SystemExit(portfolio.main())
