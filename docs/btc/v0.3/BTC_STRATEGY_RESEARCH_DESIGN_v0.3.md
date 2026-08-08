# Bitcoin Strategy Research Design — Quant Guardian

- 문서 버전: `0.3`
- 작성 기준일: `2026-08-09`
- 상태: **RESEARCH BASELINE — 후보전략 / 실전 미승인**
- 근거 문서: `SOURCES_BTC_STRATEGY_v0.3.md`
- 전략 작업명: `QG-BTC Cycle·Value·Trend Guard`
- 대상: 업비트 `KRW-BTC` 현물 롱온리
- 핵심 제약: 반감기 변수 필수, 자동주문 금지, BTC 전용예산 최대 100%, 연구상 MDD 상한 약 -50%

> 목적은 과거 최고점과 최저점을 사후적으로 맞추는 것이 아니라, 반감기 사이클의 구조적 위치를 항상 인식하면서 장기 저평가·투매에서 단계적으로 진입하고, 추세 회복에는 참여하며, 중후반 과열과 구조적 약세에서는 노출을 줄이는 규칙을 찾는 것이다.

---

## 1. 연구 결론의 출발점

### 1.1 반감기는 필수이지만 단독 신호가 아니다

Bitcoin 보조금은 블록 높이에 따라 210,000블록 간격으로 줄어든다. 공급변화는 확정적이지만 가격반응은 수요·유동성·레버리지·거시환경에 따라 달라진다. 과거 반감기 뒤 상승이 반복됐어도 사건 수가 네 번뿐이고 최근 사이클의 평균수익·변동성 반응이 약해졌다는 연구가 있다.

따라서 실전 후보는 다음을 동시에 만족해야 한다.

```text
반감기 컨텍스트 존재
AND 가격/추세 확인
AND 가치·온체인 또는 위험 확인
AND 데이터 품질 통과
```

무반감기 전략은 기여도 검증용 비교군으로만 실행한다.

### 1.2 지표가 많다고 예측력이 자동 증가하지 않는다

RSI, MACD, PPO, 이동평균, 볼린저밴드는 대부분 가격을 다른 방식으로 변환한 값이다. 같은 정보를 여러 번 점수화하면 가짜 확신이 생긴다. 연구에서는 지표를 독립된 경제적 의미의 영역으로 묶고 영역별 상한을 둔다.

### 1.3 Bitcoin에서는 아웃오브샘플과 비용이 핵심이다

기술규칙은 인샘플에서 좋아 보여도 Bitcoin 아웃오브샘플에서 약해졌다는 연구와, 거래비용을 반영하면 단순 방향예측 전략이 무너질 수 있다는 최근 연구가 함께 존재한다. 따라서 최고 백테스트 한 개보다 다음을 우선한다.

```text
walk-forward
cycle holdout
cost/slippage stress
parameter neighborhood
turnover control
hysteresis
```

### 1.4 온체인은 보조가 아니라 Bitcoin 고유 영역이지만 만능은 아니다

MVRV·실현가격·NUPL·SOPR·광부수익은 보유자의 비용기준·미실현손익·실현손익·발행자 경제를 보여준다. 다만 주소 군집화, 지표 정의, 데이터 개정, 무료 가용성에 영향을 받으므로 출처와 가용시각을 엄격히 기록한다.

---

## 2. 연구 질문

| ID | 질문 |
|---|---|
| RQ-01 | 반감기 진행률을 포함하면 기술·온체인 전략의 사이클별 일관성이 좋아지는가? |
| RQ-02 | 반감기 전후의 저평가·투매 매집이 단순 적립식과 200일선 전략보다 나은가? |
| RQ-03 | 투매 즉시 진입과 50일선·주봉 회복 확인 중 어느 쪽이 수익·낙폭 균형이 좋은가? |
| RQ-04 | MVRV·실현가격·NUPL·SOPR·광부수익이 가격지표에 추가적인 아웃오브샘플 효익을 주는가? |
| RQ-05 | USD와 KRW 신호의 합의, 김치프리미엄이 업비트 실행위험을 줄이는가? |
| RQ-06 | 전량매도·단계축소·핵심+전술 중 어느 정책이 사용자 MDD 한도에 가장 적합한가? |
| RQ-07 | 반감기 중후반 과열축소와 이후 추세붕괴 축소를 분리하면 저점 매도를 줄일 수 있는가? |
| RQ-08 | 고정 임계값보다 expanding/cycle-relative 분위값이 최근 사이클의 지표 약화를 더 잘 처리하는가? |
| RQ-09 | 거시·파생 데이터가 기술·온체인 이후에도 재현 가능한 증분효과를 보이는가? |
| RQ-10 | 예산과 무관한 목표비중 전략이 500만원 외의 예산에서도 같은 비율 성과를 재현하는가? |

---

## 3. 연구 가설

### H1 — 반감기 국면 조건부 효과

반감기 진행률은 단독 수익예측 변수가 아니라 동일한 가치·추세 신호의 의미를 바꾸는 국면변수일 것이다.

### H2 — 투매매집의 장기 우위

대형 낙폭·장기선 하방이격·과매도·온체인 저평가가 독립적으로 겹친 구간의 1~2년 기대수익은 일반 시점보다 높을 것이다.

### H3 — 확인매수의 낙폭 감소

투매 뒤 50일선 또는 주봉 추세 회복을 확인하면 즉시 매수보다 진입가는 높아지지만 최대 불리진행폭과 실패율이 낮아질 것이다.

### H4 — 단계축소의 강건성

고점 한 날짜를 맞히려는 전량매도보다 과열→추세약화 순서의 단계축소가 사이클별 편차와 재진입 오류를 줄일 것이다.

### H5 — 핵심+전술의 균형

낮은 회전율의 핵심물량과 상태 기반 전술물량을 결합하면 단순보유보다 MDD가 작고 전량매도 전략보다 상승장 참여율이 높을 것이다.

### H6 — 상대 분위값의 필요성

MVRV·Puell·장기선 이격의 절대 극단값이 사이클마다 약해질 수 있으므로 expanding percentile과 cycle-relative percentile을 병행한 전략이 고정 임계값보다 안정적일 것이다.

---

## 4. 데이터 계층

### 4.1 가격·실행 데이터

| 데이터 | 역할 | 핵심 사용 |
|---|---|---|
| Upbit `KRW-BTC` 일봉 | 실제 실행 기준 | 가상체결, 목표금액, KRW 수익률 |
| Yahoo `BTC-USD` | 장기 기준계열 후보 | 2016 이후 연구, 기존 코드 재사용 |
| Coinbase/Kraken/Bitstamp 후보 | 독립 USD 현물 | 가격·신호 교차검증 |
| USD/KRW | 합성 KRW 가격 | 김치프리미엄·USD/KRW 합의 |

### 4.2 반감기·공급 데이터

```text
block tip height
historical halving block timestamps
block subsidy
blocks since/to halving
30/90일 blocks per day
annualized issuance
supply inflation
fees-to-subsidy ratio
miner revenue
hashrate/difficulty
```

`예상 다음 반감기 날짜`는 표시용이고, 매매입력은 블록 진행률을 우선한다.

### 4.3 온체인 데이터

최초 후보:

```text
Realized Cap / Realized Price
MVRV, MVRV Z 또는 expanding percentile
NUPL
SOPR 및 가능한 holder segment
Miner revenue / Puell-equivalent
Hashrate / difficulty / miner revenue per hash
Fees / transfer value / active supply 후보
Exchange flows — 가용성·방법론 확인 후
```

### 4.4 거시·파생 데이터

연구용 challenger:

```text
DXY
VIX
미국 실질금리
유동성 또는 통화량
현물 ETF 순유입
perpetual funding
open interest
futures basis
stablecoin supply/flows
```

이 영역은 무료·재현 가능한 가용성이 확보되고, 워크포워드 증분효과가 반복될 때만 운영 핵심으로 승격한다.

### 4.5 뉴스·감성

뉴스·소셜 감성은 구조변경과 데이터 재현 문제가 크므로 v1 매매 규칙에서는 제외한다. 대시보드의 참고 이벤트 타임라인으로만 검토한다.

---

## 5. 반감기 특성 설계

### 5.1 기본 계산

```text
epoch = floor(block_height / 210000)
progress = (block_height mod 210000) / 210000
subsidy = 50 / 2^epoch
```

### 5.2 연속형 특성

```text
progress
sin(2π progress)
cos(2π progress)
blocks_since_halving
blocks_to_halving
days_since_halving
issuance_per_day
annualized_supply_inflation
fees/subsidy
miner_revenue/365d_average
```

연속형 표현을 기본으로 하여 경계 하루 차이로 상태가 급변하지 않게 한다.

### 5.3 국면 후보

| 국면 | 진행률 후보 | 연구상 역할 |
|---|---:|---|
| 전환 | 0.00–0.08 | 공급충격 직후, 추세확인 요구 |
| 상승확장 | 0.08–0.32 | 추세 참여, 과열 감시 시작 |
| 중후반 분배 | 0.32–0.50 | 과열 민감도 강화 |
| 수축·회복 | 0.50–0.75 | 대형 낙폭·바닥 탐색 |
| 차기 반감기 전 매집 | 0.75–1.00 | 가치·회복 신호에 우호적 prior |

이 경계는 설명과 그리드의 시작점일 뿐 확정 규칙이 아니다.

### 5.4 반감기가 조절하는 항목

```text
entry_required_domains
entry_persistence_days
max_target_weight
core_weight_cap
risk_reduction_sensitivity
overheat_required_domains
reentry_cooldown
```

예를 들어 중후반 분배 후보 국면에서는 `가격이 올랐으므로 매도`가 아니라 과열 증거가 2개가 아닌 3개 필요할지, 혹은 같은 증거에서 한 단계 빨리 축소할지를 비교한다.

---

## 6. 지표 영역과 중복관리

### 6.1 반감기·공급

```text
cycle_progress
issuance inflation
fees/subsidy
miner revenue percentile
hashrate trend
```

### 6.2 장기추세

```text
close/SMA50
close/SMA200
SMA200 slope
close/40WMA
40WMA slope
20WMA vs 40WMA
breakout persistence
ADX trend strength
```

### 6.3 가치·투매

```text
365d drawdown
ATH drawdown
close/SMA200
close/200WMA
close/realized_price
MVRV percentile
NUPL percentile
miner revenue percentile
30d/90d crash
```

### 6.4 모멘텀·진입

```text
RSI14 daily/weekly
PPO histogram
30/90/180d momentum
Bollinger bandwidth/position
ATR/NATR
volume z-score
OBV slope
positive divergence candidate
```

### 6.5 과열·분배

```text
MVRV/NUPL upper percentile
weekly RSI upper percentile
close/SMA200 upper percentile
close/200WMA upper percentile
90/180d parabolic return
SOPR profit realization
volatility expansion with momentum loss
kimchi premium upper percentile
```

### 6.6 원화시장·교차검증

```text
kimchi premium
USD/KRW state agreement
provider spread
upbit volume regime
```

### 6.7 영역 상한

동일 영역의 지표 10개가 모두 참이라고 독립 증거 10개로 세지 않는다.

```text
price trend domain       최대 1표
valuation domain         최대 1표
momentum domain          최대 1표
on-chain valuation       최대 1표
cycle/supply             필수 1표
local market             보정 1표
```

---

## 7. 후보 전략 `QG-BTC-CVT`

### 7.1 전략 개요

```text
C: Cycle        반감기·발행 사이클을 필수 국면으로 사용
V: Value        대형 낙폭·실현가격·온체인 저평가에서 매집
T: Trend        회복 추세 확인 후 비중 확대, 약화 시 전술 축소
Guard: Risk     과열과 추세붕괴를 분리해 단계 축소
```

### 7.2 포트폴리오 구조

연구 기본 후보:

```text
Core      0~25% 후보 — 장기 저회전 물량
Tactical  0~75% 후보 — 상태에 따라 조절
Total     0~100%
```

핵심 25%는 사용자 선호를 반영한 시작점이며, 0/25/50/75/100% core 후보를 모두 비교한다.

### 7.3 후보 상태와 비중

| 상태 | 총비중 후보 | Core | Tactical | 목적 |
|---|---:|---:|---:|---|
| `WAIT` | 0% | 0 | 0 | 신규진입 근거 부족 |
| `WATCH` | 0% | 0 | 0 | 저가구간 접근 |
| `ACCUMULATE_1` | 20% | 0~20 | 나머지 | 첫 투매매집 |
| `ACCUMULATE_2` | 40% | 20~25 | 나머지 | 지속·재시험 확인 |
| `CONFIRM_BUY` | 60% | 25 | 35 | 단기 회복 확인 |
| `TREND_HOLD` | 80% | 25 | 55 | 장기추세 참여 |
| `FULL_HOLD` | 100% | 25 | 75 | 추세·사이클·위험 허용 |
| `REDUCE_1` | 75% | 25 | 50 | 과열 1차 축소 |
| `REDUCE_2` | 50% | 25 | 25 | 과열+추세약화 |
| `CORE_ONLY` | 25% | 25 | 0 | 전술 제거 |
| `EXIT` | 0% | 0 | 0 | 전량매도 정책에서만 |

최종 비중단계는 `10/25/50/75/100`, `20/40/60/80/100`, 변동성 타깃 방식도 비교한다.

---

## 8. 매수 경로

### 8.1 경로 A — Deep Value / Capitulation

#### Seed 조건

가격 기반 기존 연구 시작점:

```text
365일 고점 대비 하락 <= -50%
close / SMA200 <= 0.80
RSI14 <= 30
30일 수익률 <= -15%
```

이를 실전 후보로 확장한다.

#### 독립 증거

다음 6개 영역 중 반감기를 포함해 최소 3~4개를 요구하는 그리드를 비교한다.

1. `cycle`: 수축·회복 또는 차기 반감기 전 매집 prior
2. `drawdown`: 365일/ATH 낙폭 하위 극단
3. `trend_discount`: SMA200/200WMA 하방이격
4. `momentum_capitulation`: RSI·30/90일 급락
5. `onchain_value`: realized price·MVRV·NUPL 저평가
6. `loss_realization`: SOPR/거래량/광부수익의 투매·회복

#### 파라미터 그리드

```text
drawdown_365: -45%, -50%, -55%, -60%
close/SMA200: 0.75, 0.80, 0.85, 0.90
RSI14: 20, 25, 30, 35
return_30d: -10%, -15%, -20%, -25%
MVRV percentile: 10, 20, 30%
price/realized: 0.85, 1.00, 1.15
NUPL percentile: 10, 20, 30%
required_domains: 3, 4
persistence: 1, 3, 5일
```

#### 행동

첫 충족에서 전액매수하지 않고 `ACCUMULATE_1` 후보 20%로 시작한다.

### 8.2 경로 B — Persistence / Retest

`ACCUMULATE_1 → ACCUMULATE_2` 후보 조건:

```text
A. 저평가 신호가 3~10일 지속
OR
B. 가격은 저점 재시험/신저가이나 RSI·PPO·SOPR 중 2개 개선
OR
C. realized price 아래/근처에서 손실실현 후 SOPR 회복
```

가격하락 그 자체는 추가매수 이유가 아니다.

### 8.3 경로 C — Short-term Recovery

`ACCUMULATE_2 → CONFIRM_BUY` 후보:

```text
최근 투매 신호가 유효기간 안에 존재
AND close가 SMA50 비영 밴드를 상향 돌파
AND RSI가 중립영역으로 회복
AND PPO/MACD 또는 주봉 모멘텀 악화 중단
```

후보 그리드:

```text
capitulation lookback: 30, 60, 90, 120일
SMA50 band: 0%, 1%, 2%
persistence: 1, 3, 5일
RSI recovery: 40, 45, 50
```

### 8.4 경로 D — Structural Trend Participation

`CONFIRM_BUY → TREND_HOLD/FULL_HOLD` 후보:

```text
close > SMA200 × (1 + band)
SMA200 slope >= threshold
주봉 close > 40WMA 또는 20WMA > 40WMA
돌파 후 재지지 또는 N일 지속
과열증거가 상한 미만
반감기 컨텍스트가 목표비중을 허용
```

바닥매집 신호를 놓쳤더라도 장기 상승추세에 참여하는 경로다.

---

## 9. 보유 경로

`HOLD`는 아무것도 하지 않는 상태가 아니라 다음을 확인한 결과다.

```text
현재 상태의 진입근거가 아직 유효
축소증거가 독립적으로 부족
단기 변동은 상태변경 밴드 안
데이터 품질 정상
목표비중과 현재 가상비중 차이가 rebalance band 이내
```

후보 리밸런싱 밴드:

```text
절대 비중차 3%, 5%, 10%
또는 목표금액 차이가 최소주문+비용 기준을 넘을 때
```

보유 알림에는 다음 조건을 명시한다.

```text
현재 보유 이유
추가매수로 전환되는 조건
1차 축소로 전환되는 조건
현재 상태 시작 이후 수익률
```

---

## 10. 매도·축소 경로

### 10.1 원칙

고점 예측과 하락추세 대응을 분리한다.

```text
1단계: 과열·분배 → 이익 일부 보호
2단계: 중기 추세약화 → 전술 추가 축소
3단계: 구조적 약세 → core only 또는 exit
```

급락 뒤 과매도 상태에서 처음으로 장기선이 깨졌다는 이유만으로 전량매도하지 않는다.

### 10.2 과열 기반 `REDUCE_1`

반감기 중후반 위험 prior가 활성화되고 아래 독립영역 중 최소 2~3개가 지속되는 후보를 비교한다.

```text
온체인 가치 상위극단: MVRV/NUPL expanding percentile
가격 과대이격: close/SMA200, close/200WMA
모멘텀 포물선: 90/180일 수익률, 주봉 RSI
실현이익/분배: SOPR, 거래량, realized profit 후보
원화시장 과열: 김치프리미엄 상위 분위
레버리지 과열: funding/open interest — 가용 시
```

파라미터 그리드:

```text
upper percentile: 85, 90, 95, 97.5%
weekly RSI: 70, 75, 80, 85
close/SMA200: 1.6, 1.8, 2.0, 2.4
180d return: 80%, 120%, 160%, 200%
required domains: 2, 3
persistence: 1주 또는 3~10일
```

### 10.3 추세약화 기반 `REDUCE_2`

`REDUCE_1` 이후 다음이 겹치면 추가축소 후보다.

```text
close < SMA50 하방 밴드 N일
PPO histogram 음전환/하락 지속
주봉 RSI 하락 및 주봉 저점 이탈
SOPR·온체인 이익실현 약화
```

### 10.4 구조적 약세 `CORE_ONLY / EXIT`

후보 조건:

```text
주봉 close가 40WMA 또는 일봉 SMA200 아래에서 지속
AND 장기선 기울기 음수
AND 중기 모멘텀 음수
AND deep-value counter-signal이 아직 없음
```

정책별 행동:

```text
all_out       → 0%
tiered        → 25% 후 추가확인 시 0%
core_tactical → core 후보 25% 유지, tactical 0%
```

### 10.5 재진입

축소 뒤 바로 반등할 수 있으므로 다음을 비교한다.

```text
고정 cooldown 10/20/30일
SMA50 재돌파
과열 분위 하락 후 추세 재회복
주봉 구조 회복
```

---

## 11. 반감기와 매수·매도 결합 방식

### 11.1 금지 방식

```text
반감기 1년 전 무조건 매수
반감기 18개월 후 무조건 매도
다음 반감기 예상 날짜를 고정해 매매
과거 평균 고점 날짜 하나를 미래 고점으로 확정
```

### 11.2 권장 방식

반감기를 `prior`와 `gate`로 사용한다.

#### 매집 prior

수축·회복/차기 반감기 전 구간에서는 동일한 저평가 증거가 있을 때 진입 문턱을 낮추는 후보를 검증한다.

#### 분배 prior

반감기 후 중후반에서는 동일한 과열 증거가 있을 때 축소 민감도를 높이는 후보를 검증한다.

#### 전환 보호

반감기 직후에는 공급변화와 선반영 여부가 불확실하므로 가격·추세 확인 없는 전액매수를 금지한다.

### 11.3 연구용 수식 예시

```text
entry_required = base_required - accumulation_prior_adjustment
exit_required  = base_required - distribution_prior_adjustment
max_weight     = phase_cap × data_quality_cap × volatility_cap
```

조정폭은 0 또는 1개 증거 정도의 작은 범위부터 검증한다. 반감기가 다른 모든 영역을 압도하지 못하게 한다.

---

## 12. 고정 임계값과 분위값 비교

### 12.1 세 가지 표현

각 핵심 지표는 가능한 경우 다음 세 가지를 모두 만든다.

```text
raw_value
expanding_percentile
halving_epoch_percentile
```

### 12.2 채택 기준

- 절대값 전략이 모든 사이클에서 비슷하게 작동하면 단순성을 우선한다.
- 절대값이 최근 사이클에서 신호를 잃고 분위값이 안정적이면 분위값을 채택한다.
- cycle percentile은 현재 epoch 초기 데이터가 부족할 수 있으므로 최소 관측수와 fallback을 둔다.
- 분위값도 미래분포를 사용하지 않는다.

---

## 13. 데이터 모드 실험

| Mode | 신호 | 실행 | 장점 | 위험 |
|---|---|---|---|---|
| A | Yahoo BTC-USD | Upbit KRW-BTC | 기존 코드 재사용·장기성 | 지표와 실행통화 차이 |
| B | Upbit KRW-BTC | Upbit KRW-BTC | 신호·체결 일치 | 초기 역사 짧음·환율 포함 |
| C | 독립 USD 현물 | Upbit KRW-BTC | 실제 거래소 가격 | 공급자 선택 민감도 |
| D | USD와 KRW 합의 | Upbit KRW-BTC | 환율·프리미엄 방어 | 신호 지연·복잡성 |

Mode D 합의 후보:

```text
둘 중 더 보수적인 목표비중
둘 다 같은 방향일 때만 전이
USD 주도, KRW 반대가 강하면 한 단계 낮춤
```

---

## 14. 비교 전략

### 14.1 필수 벤치마크

```text
BTC buy-and-hold
월 정액 DCA
현금 100%
SMA200 단순 추세
40WMA 단순 추세
```

### 14.2 진입 비교

```text
반감기 only — 연구용, 배포 금지
기술 only
온체인 only
기존 4조건 투매 즉시매수
투매 후 SMA50 회복
통합 Cycle·Value·Trend
```

### 14.3 매도 비교

```text
no sell / buy-and-hold
all_out
tiered
core_tactical
trailing/volatility challenger
```

### 14.4 ML challenger

해석 가능한 규칙전략이 baseline이다. ML은 다음과 같이 제한한다.

```text
목표: 90/180/365일 forward return 또는 drawdown class
모델: logistic, random forest, gradient boosting 후보
CV: purged/embargoed walk-forward
특성: 동일 feature store
거래: cost-aware threshold
역할: 비교·보조, 자동 실전채택 금지
```

Bitcoin의 사이클 수가 적으므로 고차원 딥러닝을 핵심 의사결정기로 사용하지 않는다.

---

## 15. 백테스트 기간과 검증

### 15.1 세 개의 보고서

#### Report A — Extended Cycle

```text
기간: 신뢰 가능한 데이터가 있는 2012년 이후
목적: 2012·2016·2020·2024 반감기 구조 검증
통화: USD 중심
```

#### Report B — User Primary

```text
기간: 2016년 이후
목적: 사용자가 요청한 핵심 성과
통화: USD 및 KRW 별도
```

#### Report C — Upbit Execution

```text
기간: Upbit KRW-BTC 실제 가용기간 이후
목적: 실제 체결·김치프리미엄·원화수익률
```

서로 다른 데이터 구간을 조용히 이어붙이지 않는다.

### 15.2 Anchored walk-forward 후보

```text
Train 2012~2016 → Test 다음 epoch/연도
Train 2012~2020 → Test 2021~2024
Train 2012~2024 → Test 2025~현재/Shadow
```

실제 절단점은 데이터 가용성과 반감기 epoch 기준으로 확정한다.

### 15.3 Leave-one-cycle-out

각 반감기 epoch를 한 번씩 완전히 제외하고 나머지에서 선택한 규칙을 제외 epoch에 적용한다. 이벤트 수가 적어 결과 불확실성이 크므로 수치보다 실패 양상을 중점 해석한다.

### 15.4 비용과 지연

```text
Upbit 거래수수료 최신값
slippage 0, 5, 10, 20, 50 bps
signal delay 0, 1, 2일
market gap/price shock
최소주문 5,000원
```

### 15.5 통계 검증

```text
stationary/block bootstrap
confidence interval
Deflated Sharpe 또는 multiple-testing 조정 후보
parameter neighborhood heatmap
cycle-by-cycle return/MDD
```

---

## 16. 평가 기준

### 16.1 수익

```text
terminal wealth
total return
CAGR
1/2/4년 rolling return
BTC buy-and-hold excess
```

### 16.2 위험

```text
MDD
Ulcer index
Calmar
Sharpe
Sortino
maximum adverse excursion
recovery time
worst rolling year
```

### 16.3 운용성

```text
exposure
turnover
number of trades
average holding period
false transition count
0↔100 rapid reversal count
data dependency count
```

### 16.4 채택 우선순위

1. 미래참조·데이터 오류 없는 전략
2. 연구상 MDD -50% 상한 통과
3. 사이클별 치명적 실패가 적은 전략
4. 인접 파라미터에서도 유사한 전략
5. 비용·지연에도 논리가 유지되는 전략
6. 단순보유 대비 사용자가 원하는 개선이 있는 전략
7. 설명과 운영이 가능한 전략

CAGR이 가장 높은 한 전략을 자동 선택하지 않는다.

---

## 17. 후보 성공·실패 정의

### 17.1 성공 후보

```text
MDD <= 약 -50%
preferred MDD -35~-45% 후보 존재
2개 이상 독립 검증구간에서 Calmar 개선
단순보유 대비 상승참여율이 과도하게 낮지 않음
매도정책이 저점 전량매도 빈도를 줄임
반감기 변수가 실제로 요구확인·비중·위험민감도에 사용됨
```

### 17.2 실패 후보

```text
한 사이클에서만 높은 성과
임계값 1포인트 변경으로 성과 붕괴
거래비용 후 우위 소멸
반감기 날짜를 사후 고점에 맞춤
온체인 개정 데이터로 미래정보 사용
신호가 지나치게 적어 통계적 판단 불가
0↔100% 전환이 빈번
```

실패한 전략을 억지로 채택하지 않는다. 단순보유나 DCA가 더 낫다는 결론도 허용한다.

---

## 18. 대시보드에서 보여줄 전략 설명

### 18.1 핵심 카드

```text
현재 행동
목표비중
Core / Tactical 비중
반감기 epoch·진행률·국면
전략 수익률 / BTC 단순보유
현재 상태 이후 수익률
데이터 신뢰도
```

### 18.2 근거 설명 예시

```text
매수 근거
- 반감기 사이클 진행률 78%, 차기 반감기 전 매집 후보 국면
- 365일 고점 대비 -52%
- 가격이 SMA200보다 22% 낮음
- MVRV expanding percentile 14%
- RSI 과매도 후 회복

제한 요인
- 주봉 추세는 아직 하락
- USD와 KRW 신호가 완전히 일치하지 않음

따라서
- 전액매수가 아니라 1차 20% 후보
```

### 18.3 다음 조건

현재 상태에서 위·아래 전이 임계값을 수치로 보여준다. 사용자는 “왜 아직 추가매수하지 않는지”와 “언제 축소하는지”를 알 수 있어야 한다.

---

## 19. 텔레그램 전략 표시

기본 메시지는 예산에 독립적이다.

```text
[Quant Guardian | Bitcoin | SHADOW]
기준: UTC 확정 일봉
현재: ACCUMULATE_1 / 20%
Core/Tactical: 0% / 20%
반감기: epoch 4, 진행률 ...%
전략 수익률: ...%
BTC 단순보유: ...%
현재 상태 이후: ...%
근거: cycle + drawdown + on-chain value
다음 확대: SMA50 회복 및 주봉 안정
다음 축소: 과열 다중확인 또는 구조적 약세
```

선택적 GitHub 예산이 켜져 있으면 목표금액을 추가하지만, 사이트 브라우저 예산과 자동 동기화되지 않는다고 표시한다.

---

## 20. 연구 산출물

각 실험 실행은 다음을 생성한다.

```text
experiment_manifest.json
strategy_candidates.csv
walk_forward_metrics.csv
cycle_metrics.csv
parameter_sensitivity.csv
trade_ledger.csv
equity_curves.csv
drawdowns.csv
signal_history.csv
data_quality_report.json
research_report.md
```

보고서에는 실패 전략과 제외 이유도 포함한다.

---

## 21. 현재 권장 baseline

실제 통합 백테스트 전 임시 baseline은 다음이다.

```text
1. 반감기 block progress를 필수 입력으로 계산
2. 기존 투매 4조건을 seed로 사용하되 가격조건 4/4 고정 대신
   cycle + valuation + capitulation의 독립영역 확인으로 확장
3. 첫 진입 20%, 지속/재시험 40%
4. SMA50·모멘텀 회복 60%
5. SMA200/40WMA 회복 80%
6. 추세 유지·과열 부재 100%
7. 반감기 중후반 + 과열영역 2개 이상에서 75%
8. 과열 지속 + 추세약화에서 50%
9. 구조적 약세에서 core 25% 또는 정책별 0%
10. 모든 전이는 지속·히스테리시스·다음 실행가격 적용
```

이 baseline은 설계 시작점이지 “최적 전략”이 아니다. 최종 전략은 연구행렬과 30일 모의운영 뒤 사용자 승인을 받아야 한다.

---

## 22. 연구 승인 게이트

다음 결과가 나온 뒤 사용자에게 한 번에 제시한다.

1. 데이터 모드 A~D 비교
2. 반감기 포함/제외 ablation
3. 기술 only / 온체인 only / 통합 비교
4. 매도정책 3종 비교
5. core 비중 후보 비교
6. MDD -50% 상한과 preferred 범위 통과 여부
7. 파라미터 주변 안정성
8. 사이클별 거래내역과 실패 사례
9. 현재 최신 신호를 각 후보전략이 어떻게 해석하는지
10. 최종 추천 1안, 보수적 대안 1안, 단순보유/DCA 기준안

사용자 승인 없이는 실전판 설정을 만들지 않는다.
