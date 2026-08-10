# Quant Guardian BTC Extension

현재 Telegram 실전 기준은 **Fixed Six**이며, 기존 v0.3 문서는 초기 설계·연구 이력으로 보존합니다.

## 현재 실전 Telegram 기준

- [Fixed Six 문서 안내](./fixed-six/README.md)
- [제품 요구사항 PRD](./fixed-six/PRD.md)
- [기술 요구사항 TRD](./fixed-six/TRD.md)
- [운영방법](./fixed-six/OPERATIONS.md)
- [사용자 결정사항](./fixed-six/DECISIONS.md)

```text
Upbit KRW-BTC 현물
시작예산 10,000,000원
반감기 진행률 65%부터 3주 분할매수
다음 반감기 후 진행률 35%부터 3주 분할매도
보유 중 중간 리밸런싱 없음
Telegram 15분 polling
자동주문 없음
```

## 초기 설계·연구 문서

- [v0.3 문서 안내](./v0.3/README.md)
- [v0.3 제품 요구사항](./v0.3/PRD_BTC_EXTENSION_v0.3.md)
- [v0.3 기술 요구사항](./v0.3/TRD_BTC_EXTENSION_v0.3.md)
- [BTC 전략 연구설계](./v0.3/BTC_STRATEGY_RESEARCH_DESIGN_v0.3.md)
- [결정 변경로그](./v0.3/DECISION_LOG_BTC_EXTENSION_v0.3.md)

## 안전 상태

- 거래소: Upbit `KRW-BTC`
- 주문 실행: 사용자 수동
- Upbit 주문 API: 사용하지 않음
- 레버리지·공매도: 사용하지 않음
- AI API: 사용하지 않음
- 핵심 데이터 오류 시: 새 매수·매도 단계 차단
