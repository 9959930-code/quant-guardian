# Quant Guardian BTC Extension

현재 설계 기준 문서는 **v0.3**입니다.

- [v0.3 문서 안내](./v0.3/README.md)
- [제품 요구사항 PRD](./v0.3/PRD_BTC_EXTENSION_v0.3.md)
- [기술 요구사항 TRD](./v0.3/TRD_BTC_EXTENSION_v0.3.md)
- [BTC 전략 연구설계](./v0.3/BTC_STRATEGY_RESEARCH_DESIGN_v0.3.md)
- [사용자 결정사항](./v0.3/DECISIONS_BTC_EXTENSION_v0.3.md)
- [결정 변경로그](./v0.3/DECISION_LOG_BTC_EXTENSION_v0.3.md)
- [1개월 모의운영 계획](./v0.3/BTC_SHADOW_MODE_PLAN_v0.3.md)
- [인수·검수 테스트](./v0.3/ACCEPTANCE_TESTS_BTC_EXTENSION_v0.3.md)
- [후보 설정 TOML](./v0.3/CONFIG_BTC_CANDIDATE_v0.3.toml)
- [조사 출처와 설계 근거](./v0.3/SOURCES_BTC_STRATEGY_v0.3.md)

## 상태

- 문서 업로드: 완료
- 전략 연구 및 구현: 시작 전
- 후보 임계값·목표비중: 실전 미승인
- 자동주문: 범위 제외
- ETF 기준선: `main` / `cd00bcf953b80cb2e900d685f504da19192f4e24`
- 다음 단계: Phase 2 지표·연구 엔진과 워크포워드 백테스트 구현

## 구현 현황

- 브랜치: `agent/btc-extension-v1`
- Phase 1: 무료 데이터·반감기 모듈 구현 및 실제 API 점검 완료
- 현재 범위: 데이터 품질 보고서만 제공, 매수·매도 전략과 웹·텔레그램 연결은 미구현
- 안전 상태: `shadow`, 자동주문 없음, 핵심 데이터 오류 시 신호 계산 차단
