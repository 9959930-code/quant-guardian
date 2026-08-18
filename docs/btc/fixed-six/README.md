# Quant Guardian BTC Fixed Six

현재 실전 Telegram 기준 문서입니다.

- [PRD](./PRD.md)
- [TRD](./TRD.md)
- [운영방법](./OPERATIONS.md)
- [사용자 결정사항](./DECISIONS.md)
- [35% 주간매도와 36·37·38% 매도 비교](./BTC_CLOCK_EXIT_COMPARE_RESULT_2026-08-17.md)

## 현재 설정

```text
전략 버전: btc-fixed-six-clock-hybrid-1.1
Upbit KRW-BTC 현물
시작예산 10,000,000원
62.5% 저점 관찰 알림만
65% 도달 후 3주 분할매수
다음 반감기 후 35% 고점 위험경고만
36%·37%·38% 도달 후 단계별 분할매도
보유 중 리밸런싱 없음
공식 주문 점검: 월요일 09:17 KST
Telegram 15분 polling
자동주문 없음
```

## 체결 지연 안전장치

- 주문안 24시간 경과: 현재가 기준 주문검토액 재계산
- 3일 경과: 체결지연 주의와 재계산
- 7일 경과: 기존 주문안 만료
- 만료돼도 전략 단계를 건너뛰지 않으며, 현재가 재계산 또는 실제 잔고동기화가 필요하다.

실행 진입점은 `btc_clock_hybrid_portfolio_runner.py`이며 기존 BTC와 ISA 상태파일을 각각 유지한다.
