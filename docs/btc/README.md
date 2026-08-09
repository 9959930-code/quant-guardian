# Quant Guardian BTC Extension

현재 설계 기준은 **v0.3**, 최신 장기 연구 결과는 **v0.4**입니다.

- [v0.4 반감기·40주 추세 연구](./v0.4/README.md)

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
- 전략 연구 및 구현: Phase 3 장기 반감기 v0.4 연구 완료
- 후보 임계값·목표비중: 실전 미승인
- 자동주문: 범위 제외
- ETF 기준선: `main` / `b0cecb0ef7e8e6678de9ebc34944589d9332cb54`
- 다음 단계: v0.4R1 전향 기록과 30개 확정 일봉 Shadow

## 구현 현황

- 작업 브랜치: `research/btc-cycle-v0.4`
- Phase 1: 무료 데이터·반감기 모듈 구현 및 실제 API 점검 완료
- Phase 2: feature frame, 다음 일봉 시가 체결, 비용, 고정 후보 9개, 필수 벤치마크, anchored walk-forward, cycle holdout, 비용·지연·인접값 보고서 구현
- Phase 3: 2016년 합성 원화 확장, 반감기·40주 추세 v0.4, 실제 Upbit 교차검증, 비용·지연 진단 구현
- 연구 판정: 고정 기준안은 MDD 경계 실패, 사후 진단 후보 1개만 경계 통과, 실전 승인 보류
- 현재 범위: 연구 보고서만 제공, 웹·텔레그램 BTC 연결은 미구현
- 안전 상태: `shadow`, 자동주문 없음, 핵심 데이터 오류 시 신호 계산 차단

연구 결과 스냅샷은 [Phase 2 연구 결과](./v0.3/BTC_PHASE2_RESEARCH_RESULT_2026-08-09.md)에서 확인합니다.
장기 반감기 연구는 [v0.4 연구 결과](./v0.4/BTC_CYCLE_RESEARCH_RESULT_2026-08-10.md)에서 확인합니다.
