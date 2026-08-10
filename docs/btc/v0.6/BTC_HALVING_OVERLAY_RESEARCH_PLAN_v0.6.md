# BTC 반감기 필수 오버레이 연구계획 v0.6

- 문서 버전: `0.6`
- 작성일: `2026-08-10`
- 상태: **PREDECLARED RESEARCH PLAN / 실전 미승인**
- 기준 브랜치: `research/btc-cycle-v0.4`
- 기준 커밋: `11702d82d102892e73edf5ebd81418c2005e6b40`
- 신규 브랜치: `research/btc-halving-v0.6`

## 1. 연구 목적

v0.5는 1·3·6·12개월 모멘텀과 63일 변동성으로 BTC 비중을 조절해
2016년 이후 과거 MDD를 약 `-44%`로 낮췄지만 반감기를 매매식에 사용하지 않았다.
사용자 요구사항은 반감기 변수를 모든 배포 가능한 BTC 전략의 필수 입력으로 두는 것이다.

v0.6은 다음 원칙을 따른다.

```text
v0.5 무반감기 전략은 삭제하지 않고 고정 벤치마크로 보존
v0.6 선택 가능 후보는 모두 실제 block-height 반감기 입력 필수
반감기 하나만으로 전액매수·전액매도 금지
가격 모멘텀·변동성·가치·과열 증거와 결합
실제 계좌비중과 이전 목표비중 리밸런싱을 분리 비교
전체기간 최고수익 한 점을 자동 채택하지 않음
```

## 2. 공통 데이터와 체결

### 가격

- 2016년부터: Yahoo `BTC-USD` × 해당 시점에 이미 알 수 있었던 전일 USD/KRW 합성 원화
- 실제 검증: Upbit `KRW-BTC` 가용 중첩구간
- 현재 미완성 UTC 일봉 제외

### 반감기

- Coin Metrics `BlkCnt` 누적 블록 높이
- 반감기 간격 `210,000` 블록
- `cycle_progress = (block_height mod 210000) / 210000`
- 날짜 추정값이 아니라 실제 과거 블록 진행률 사용

### 체결

```text
판단: 매주 일요일 UTC 확정 종가
체결: 다음 일봉 시가
수수료: 5bps
슬리피지: 10bps
공매도·레버리지: 없음
남는 비중: 현금
세금: 제외
```

## 3. v0.5 고정 기준선

```text
30·90·180·365일 수익률의 양수 개수
0개 0%, 1개 25%, 2개 50%, 3개 75%, 4개 100%

변동성 상한
= 목표 연변동성 / 최근 63일 연환산 변동성

기본 목표
= min(모멘텀 비중, 변동성 상한, 100%)
```

비교 기준선은 두 개다.

1. `v05_vol45_target_change`
   - 이전 목표비중과 새 목표비중 차이가 10%p 이상일 때만 거래
2. `v05_vol45_actual_weight`
   - 실제 평가비중과 새 목표비중 차이가 10%p 이상일 때 거래

두 기준선은 반감기를 쓰지 않으며 최종 v0.6 선택 대상에서 제외한다.

## 4. 반감기 국면

기존 feature frame의 실제 블록 진행률을 사용한다.

| 국면 | 진행률 |
|---|---:|
| `HALVING_TRANSITION` | 0.00–0.08 |
| `POST_HALVING_EXPANSION` | 0.08–0.32 |
| `LATE_EXPANSION_DISTRIBUTION` | 0.32–0.50 |
| `CONTRACTION_RECOVERY` | 0.50–0.75 |
| `PRE_HALVING_ACCUMULATION` | 0.75–1.00 |

국면은 단독 주문신호가 아니라 비중 상한·위험예산·확인조건을 조정한다.

## 5. 사전 고정 후보

### A. 반감기 국면 비중상한

```text
HALVING_TRANSITION             100%
POST_HALVING_EXPANSION         100%
LATE_EXPANSION_DISTRIBUTION     75%
CONTRACTION_RECOVERY            50%
PRE_HALVING_ACCUMULATION        75%
```

후보:

- `v06_phase_cap_vol45_actual`
- `v06_phase_cap_vol40_actual`

### B. 국면별 위험예산

```text
HALVING_TRANSITION              40%
POST_HALVING_EXPANSION          45%
LATE_EXPANSION_DISTRIBUTION     35%
CONTRACTION_RECOVERY            30%
PRE_HALVING_ACCUMULATION        40%
```

후보:

- `v06_phase_risk_vol40_actual`

### C. 후기 사이클 확인조건

후기 사이클에서 50%를 넘는 비중은 30·90·180·365일 모멘텀이 모두 양수일 때만 허용한다.

후보:

- `v06_confirmation_vol40_actual`

### D. 반감기·가치·과열 결합

매집국면에서 다음을 모두 충족하면 25% 최소비중을 허용한다.

```text
국면 = CONTRACTION_RECOVERY 또는 PRE_HALVING_ACCUMULATION
365일 고점 대비 낙폭 <= -45%
MVRV expanding percentile <= 30%
또는 가격/실현가격 <= 1.15
```

후기 사이클에서는 독립 과열영역을 계산한다.

```text
MVRV 상위 90% 이상
주봉 RSI 75 이상
가격/200일선 1.8 이상 또는 180일 수익률 120% 이상

과열 1개: 최대 75%
과열 2개 이상: 최대 50%
```

후보:

- `v06_cycle_value_vol40_actual`
- `v06_cycle_value_vol45_actual`
- `v06_cycle_value_vol40_target_change`

마지막 후보는 실제비중 리밸런싱 효과를 분리하기 위한 민감도 비교다.

## 6. 실제비중 리밸런싱

### 이전 목표 기준

```text
|새 목표 - 이전 실행목표| >= 10%p
```

가격이 크게 변해 실제 BTC 비중이 목표에서 벗어나도 목표값이 같으면 거래하지 않는다.

### 실제 계좌비중 기준

```text
|새 목표 - 다음 시가의 실제 BTC 평가비중| >= 10%p
```

v0.6의 기본 후보는 실제 계좌비중 기준을 사용한다.
두 정책은 동일 수수료·슬리피지로 비교한다.

## 7. 반감기 전후 고정 보유창

“반감기 전에 사고 반감기 후 판다”는 아이디어를 별도 단순 비교군으로 구현한다.

```text
매수 시작: 이전 epoch 진행률 70%·75%·80%·85%
매도 종료: 새 epoch 진행률 25%·30%·35%·40%·45%
비중: 100% 또는 40% 변동성 상한
리밸런싱: 실제비중, 10%p 데드밴드
```

전체기간 최상위 규칙을 그대로 채택하지 않는다.

검증:

- 완료 epoch 하나를 제외하고 나머지 완료 epoch에서 규칙 선택
- 제외한 epoch에 적용
- 완료 epoch에서 선택한 규칙을 현재 진행 epoch에 적용
- 학습·검증 결과가 크게 달라지면 고정 날짜 타이밍은 불안정 판정

## 8. 신의 타이밍 상한

실전 불가능한 상한을 계산한다.

```text
매수 허용창: 반감기 전 epoch 진행률 60% 이후
매도 허용창: 반감기 후 epoch 진행률 50%까지
매수일: 허용창의 사후 최저 종가
매도일: 허용창의 사후 최고 종가
```

이 결과는 미래정보를 사용하므로 전략 후보·성과표·순위에 포함하지 않는다.
목적은 AI와 규칙전략이 놓치는 수익구간의 크기를 확인하는 것이다.

## 9. 후보 선택 기준

v0.6 선택 가능 후보는 다음을 모두 만족해야 한다.

```text
반감기 필수 입력
전체기간 MDD >= -50%
Upbit 중첩구간 MDD >= -50%
다음 시가 체결
비용 반영
자동주문 없음
```

통과 후보는 다음 순서로 정렬한다.

1. 완료 반감기 사이클의 최저 Calmar
2. 전체·Upbit 중 더 낮은 Calmar
3. 전체·Upbit 중 더 낮은 CAGR
4. 회전율이 낮은 후보

이는 연구상 추천 순위일 뿐 실전 승인이 아니다.

## 10. 강건성 검사

연구 추천 후보에 다음을 적용한다.

| 조건 | 변경 |
|---|---|
| 기본 | 수수료 5bps + 슬리피지 10bps |
| 비용 2배 | 수수료·슬리피지 각각 2배 |
| 1일 지연 | 기본 다음 시가보다 1일 추가 지연 |
| 2일 지연 | 기본 다음 시가보다 2일 추가 지연 |
| 데드밴드 5%p | 더 잦은 조정 |
| 데드밴드 15%p | 더 적은 조정 |
| 대기현금 연 3% | 현금 0% 가정 민감도 |

## 11. 산출물

```text
output/btc_halving_v06_candidates.csv
output/btc_halving_v06_ranking.csv
output/btc_halving_v06_signals.csv
output/btc_halving_v06_cycle_metrics.csv
output/btc_halving_v06_data_modes.csv
output/btc_halving_v06_window_grid.csv
output/btc_halving_v06_window_walkforward.csv
output/btc_halving_v06_oracle.csv
output/btc_halving_v06_robustness.csv
output/btc_halving_v06_equity.csv
output/btc_halving_v06_manifest.json
output/btc_halving_v06_report.md
```

GitHub Actions에서는 위 파일을 artifact로 보존한다.

## 12. 해석 제한

- v0.6은 v0.3~v0.5 결과를 본 뒤 설계했으므로 순수 표본외 결과가 아니다.
- 완료된 독립 반감기 사이클 수가 매우 적다.
- 2016년 이전에는 이번 실행가격 기준을 동일하게 적용하지 않는다.
- 현재 epoch는 완료되지 않았다.
- 고정 보유창 최고값과 연구 추천 후보는 미래 성과를 보장하지 않는다.
- 30개 확정 일봉 Shadow는 운영 정확성만 검증한다.
- 사용자 승인 전 `main`, 공개 웹, Telegram, 자동주문에 연결하지 않는다.
