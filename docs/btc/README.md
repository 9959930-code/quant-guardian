# Quant Guardian BTC Extension

현재 설계 기준은 **v0.3**, 무반감기 가격기반 기준선은 **v0.5**, 최신 연구 단계는 **반감기 필수 오버레이 v0.6**입니다.

- [v0.6 반감기 필수 오버레이 연구](./v0.6/README.md)
- [v0.5 모멘텀·변동성 연구](./v0.5/README.md)
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
- 전략 연구 및 구현: Phase 5 반감기 필수 v0.6 연구 진행 중
- 후보 임계값·목표비중: 실전 미승인
- 자동주문: 범위 제외
- ETF 기준선: `main` / `b0cecb0ef7e8e6678de9ebc34944589d9332cb54`
- 현재 연구 기준: `research/btc-halving-v0.6`

## 구현 현황

- Phase 1: 무료 데이터·반감기 모듈 구현 및 실제 API 점검 완료
- Phase 2: feature frame, 다음 일봉 시가 체결, 비용, 후보·벤치마크, walk-forward와 cycle holdout 구현
- Phase 3: 2016년 합성 원화 확장, 반감기·40주 추세 v0.4, 실제 Upbit 교차검증 구현
- Phase 4: 1·3·6·12개월 모멘텀, 63일 변동성 예산, v0.5 구현
- Phase 5: v0.5 기준선 보존, 반감기 국면 오버레이, 실제비중 리밸런싱, 고정 반감기 보유창과 사후 최적 상한 v0.6 구현
- 현재 범위: 연구 보고서만 제공, 웹·텔레그램 BTC 연결은 미구현
- 안전 상태: `shadow`, 자동주문 없음, v0.6 선택 가능 후보는 반감기 컨텍스트 없으면 의사결정 차단

연구 결과 스냅샷은 다음 문서에서 확인합니다.

- [Phase 2 연구 결과](./v0.3/BTC_PHASE2_RESEARCH_RESULT_2026-08-09.md)
- [v0.4 연구 결과](./v0.4/BTC_CYCLE_RESEARCH_RESULT_2026-08-10.md)
- [v0.5 연구 결과](./v0.5/BTC_MOMENTUM_RESEARCH_RESULT_2026-08-10.md)
- v0.6 결과는 GitHub Actions 재현이 끝난 뒤 `v0.6` 폴더에 고정합니다.
