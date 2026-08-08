# Quant Guardian BTC Extension — Design Pack v0.3

이 폴더는 기존 Quant Guardian에 Bitcoin 반감기 중심의 매수·보유·축소·매도 보조기능을 추가하기 위한 제품·기술·연구·검수 문서 묶음이다.

## 현재 상태

```text
제품 방향: 승인
ETF 기준선: main / cd00bcf953b80cb2e900d685f504da19192f4e24
반감기 필수: 승인
사용자 선택형 예산: 승인
비중+수익률 표시: 승인
후보전략: 연구 전
최종 임계값·비중: 미승인
실전 사용: 미승인
자동주문: 금지
```

## 문서 목록

| 파일 | 역할 |
|---|---|
| `PRD_BTC_EXTENSION_v0.3.md` | 제품이 무엇을 해야 하는지 정의 |
| `TRD_BTC_EXTENSION_v0.3.md` | 데이터·계산·모듈·배포 기술구조 정의 |
| `BTC_STRATEGY_RESEARCH_DESIGN_v0.3.md` | 매수·보유·축소·매도 후보와 검증 방법 |
| `DECISIONS_BTC_EXTENSION_v0.3.md` | 승인된 사용자 결정과 미정사항 분리 |
| `DECISION_LOG_BTC_EXTENSION_v0.3.md` | 버전별 변경 이유 기록 |
| `BTC_SHADOW_MODE_PLAN_v0.3.md` | 30개 확정 일봉 모의운영 계획 |
| `ACCEPTANCE_TESTS_BTC_EXTENSION_v0.3.md` | 구현 완료를 판단할 검수 시나리오 |
| `CONFIG_BTC_CANDIDATE_v0.3.toml` | 연구·Shadow용 후보 설정, live 미승인 |
| `SOURCES_BTC_STRATEGY_v0.3.md` | 공식 문서·논문 근거와 설계 영향 |

## 핵심 구조

```text
ETF 엔진                         기존 유지
BTC 데이터·반감기 엔진           신규
BTC 연구·백테스트 엔진           신규
BTC 설명가능 상태 머신           신규
공개 대시보드                    ETF + BTC
개인 계산                        브라우저 localStorage
텔레그램                         비중 + 수익률 + 상태변경
자동주문                         없음
```

## 사용자 예산

- 기본값은 500만원이다.
- 100만·300만·500만·1,000만원 프리셋과 직접입력을 제공한다.
- 예산 변경은 목표 원화금액만 바꾸며 전략 신호·비중·수익률은 바꾸지 않는다.
- 개인 예산·수량·평균단가는 공개 사이트 파일이나 GitHub 로그에 저장하지 않는다.

## 반감기

- 모든 배포 가능한 전략의 필수 입력이다.
- 210,000블록 단위와 실제 block height로 계산한다.
- 예상 날짜는 화면 참고용이다.
- 반감기 하나만으로 전액매수·전액매도하지 않는다.
- 무반감기 모델은 연구용 ablation일 뿐 배포할 수 없다.

## 개발 순서

```text
1. 완료 — 최신 ETF 기준선 `cd00bcf` 확정
2. 완료 — 기존 ETF 회귀테스트 6개 통과
3. 검토 수정된 문서 병합
4. 데이터·반감기·feature 모듈 구현
5. 비교 백테스트와 연구 보고서 생성
6. 후보전략 사용자 승인
7. 대시보드·개인예산·Telegram 구현
8. 30개 확정 일봉 Shadow
9. 종료보고서와 사용자 승인
10. live advisory 배포
```

BTC 구현은 `agent/btc-extension-v1` 별도 브랜치에서 시작하며 기존 ETF 기능을 회귀테스트로 보호한다.
