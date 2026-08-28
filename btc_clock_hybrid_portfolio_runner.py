from __future__ import annotations

import btc_clock_hybrid_runtime as hybrid

hybrid.install()

import portfolio_telegram_bot as app  # noqa: E402
import portfolio_operational_alerts as operational  # noqa: E402
import portfolio_operational_delivery as delivery  # noqa: E402

_original_help = app.unified_help_message


def hybrid_help() -> str:
    return (
        _original_help()
        + "\n\n[BTC 반감기 시계 1.1]"
        + "\n- 62.5%: 저점 관찰 알림"
        + "\n- 65%: 3주 분할매수 시작"
        + "\n- 예상 첫 매수 5·3영업일 전: 자금준비 알림"
        + "\n- 다음 반감기 후 35%: 고점 위험경고"
        + "\n- 36%·37%·38%: 단계별 분할매도"
        + "\n- 주문안: 24시간 재계산, 3일 주의, 7일 만료"
        + "\n\n[운영 확인]"
        + "\n- 주 1회 BTC·ISA 통합 heartbeat"
        + "\n- ISA 초기매수 완료 전 주간 재알림"
        + "\n- 예약 실행 90분 이상 공백 시 복구 후 지연 알림"
    )


app.unified_help_message = hybrid_help
delivery.install()
operational.install()


if __name__ == "__main__":
    raise SystemExit(app.main())
