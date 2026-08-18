# Quant Guardian BTC Fixed Six · Clock Hybrid 1.1

현재 실전 Telegram 기준 문서입니다.

- [PRD](./PRD.md)
- [TRD](./TRD.md)
- [운영방법](./OPERATIONS.md)
- [사용자 결정사항](./DECISIONS.md)
- [Clock Hybrid 1.1 변경사항](./CLOCK_HYBRID_1_1.md)

## 현재 설정

```text
거래소: Upbit KRW-BTC 현물
시작예산: 10,000,000원
62.5%: 저점 관찰 알림만
65%: 공식 월요일부터 3주 분할매수
다음 epoch 35%: 고점 위험경고만
36%: BTC 2/3 목표
37%: BTC 1/3 목표
38%: BTC 0% 목표
첫 매수 예상 5·3영업일 전: 자금준비 알림
보유 중 리밸런싱 없음
Telegram 15분 polling
자동주문 없음
```

첫 매수일은 현재 블록 높이에서 10분/블록으로 65% 도달시점을 추정한 뒤, 그 이후 첫 월요일 09:17 KST로 계산한다. 예상일은 블록 생성속도에 따라 바뀔 수 있다.

주문안이 24시간 이상 지나면 최신 가격·잔고로 다시 계산한다. 3일 이후에는 주의, 7일 이후에는 기존 금액 사용 금지로 안내한다.
