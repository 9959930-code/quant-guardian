# TRD — Quant Guardian Bitcoin Cycle Signal Extension

- 문서 버전: `0.3`
- 작성 기준일: `2026-08-09`
- 상태: **TECHNICAL BASELINE — 반감기 필수 / 최신 로컬 GitHub 기준선 확인 전**
- 대상 저장소: `9959930-code/quant-guardian`
- 관련 문서: `PRD_BTC_EXTENSION_v0.3.md`, `BTC_STRATEGY_RESEARCH_DESIGN_v0.3.md`, `ACCEPTANCE_TESTS_BTC_EXTENSION_v0.3.md`, `BTC_SHADOW_MODE_PLAN_v0.3.md`, `SOURCES_BTC_STRATEGY_v0.3.md`

> 목표는 기존 미국 ETF 기능을 유지하면서 Bitcoin 전용 데이터·반감기·전략·개인 계산·차트·텔레그램 기능을 독립 모듈로 추가하는 것이다.  
> 이 문서의 임계값과 비중은 연구 후보이며, 워크포워드 백테스트와 30개 확정 일봉의 모의운영 전에는 실전 승인값이 아니다.

---

## 1. 구현 전제와 기준선

### 1.1 GitHub 기준선

현재 연결된 원격 `main`은 사용자가 설명한 최신 로컬 완성본보다 오래된 상태다. 구현 전 다음 순서를 지킨다.

```text
1. 최신 로컬 코드를 안전한 동기화 브랜치에 push
2. 기존 ETF 화면·텔레그램·테스트 결과를 기준선으로 저장
3. 기준 commit SHA를 DECISIONS 문서에 기록
4. feature/btc-extension-v1 브랜치 생성
5. 데이터 → 연구 → 신호 → 화면 → 알림 순으로 구현
6. shadow 모드 배포
7. 사용자 승인 전 live 모드 금지
```

강제 push나 최신 로컬 코드의 덮어쓰기를 금지한다.

### 1.2 호환 원칙

- 기존 ETF 결과와 BTC 결과의 실패 도메인을 분리한다.
- BTC 데이터 장애 때문에 ETF 대시보드 전체가 생성 실패해서는 안 된다.
- ETF 회귀테스트를 먼저 고정한 뒤 BTC 기능을 추가한다.
- 기존 단일 파일 구조가 안정적이면 첫 단계에서 무리한 전면 리팩터링을 하지 않는다.
- 연구 코드와 운영 신호 코드는 동일한 계산 함수를 호출해야 한다.

---

## 2. 목표 아키텍처

```text
┌──────────────────────────────────────────────────────────────┐
│ Entry Points                                                  │
│ CLI | local launcher | GitHub Actions                         │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ BTC Data Orchestrator                                        │
│ Upbit | USD price | FX | block height | on-chain | optional  │
│ caching | availability timestamp | revision hash | QC         │
└───────────────────────────┬──────────────────────────────────┘
                            │ canonical feature frame
┌───────────────────────────▼──────────────────────────────────┐
│ BTC Cycle & Feature Engine                                   │
│ halving epoch/progress | daily/weekly indicators             │
│ expanding percentiles | cycle-relative transforms            │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Strategy Engine                                               │
│ evidence domains | state machine | hysteresis | target weight│
│ core + tactical | explanation | signal_id                    │
└───────────────┬──────────────────────────┬───────────────────┘
                │                          │
┌───────────────▼─────────────┐  ┌────────▼───────────────────┐
│ Research / Backtest         │  │ Live/Shadow Signal         │
│ walk-forward | cycle holdout│  │ next-action | data status  │
│ costs | sensitivity         │  │ output schema              │
└───────────────┬─────────────┘  └────────┬───────────────────┘
                │                          │
┌───────────────▼──────────────────────────▼───────────────────┐
│ Outputs                                                       │
│ static dashboard | JSON/CSV | Telegram | audit/shadow logs    │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 설계 원칙

1. **반감기 필수**: 모든 배포 가능한 전략은 유효한 반감기 컨텍스트를 받아야 한다.
2. **반감기 비단독성**: 반감기만으로 목표비중을 올리거나 내리지 않는다.
3. **독립 증거영역**: 상관성이 높은 가격 가공지표를 여러 표로 중복 계산하지 않는다.
4. **시간 가용성 보존**: 각 데이터가 실제로 사용 가능해진 시각 이후에만 신호에 사용한다.
5. **명시적 상태 전이**: 점수 하나가 곧바로 주문 문구가 되지 않는다.
6. **개인정보 분리**: 시장 신호는 공개, 개인 예산·보유량·거래내역은 브라우저 로컬이다.
7. **연구·운영 분리**: 후보 파라미터와 승인 파라미터를 다른 설정 블록으로 둔다.

---

## 3. 권장 저장소 구조

최신 로컬 구조 확인 후 경로는 조정할 수 있으나 책임 분리는 유지한다.

```text
quant-guardian/
├─ quant_guardian.py                   # 기존 CLI 호환 진입점
├─ launch_dashboard.py
├─ build_dashboard.py
├─ telegram_notify.py
├─ config.toml
├─ src/quant_guardian/
│  ├─ common/
│  │  ├─ time.py
│  │  ├─ schemas.py
│  │  ├─ hashing.py
│  │  └─ logging.py
│  ├─ data/
│  │  ├─ cache.py
│  │  ├─ quality.py
│  │  ├─ availability.py
│  │  └─ providers/
│  │     ├─ base.py
│  │     ├─ yahoo.py
│  │     ├─ upbit.py
│  │     ├─ usd_spot.py
│  │     ├─ fx.py
│  │     ├─ mempool.py
│  │     ├─ bitcoin_core.py
│  │     └─ coinmetrics.py
│  ├─ btc/
│  │  ├─ models.py
│  │  ├─ halving.py
│  │  ├─ indicators_price.py
│  │  ├─ indicators_onchain.py
│  │  ├─ features.py
│  │  ├─ evidence.py
│  │  ├─ state_machine.py
│  │  ├─ allocation.py
│  │  ├─ portfolio.py
│  │  ├─ explanations.py
│  │  └─ serializers.py
│  ├─ research/
│  │  ├─ backtest.py
│  │  ├─ execution.py
│  │  ├─ walk_forward.py
│  │  ├─ cycle_holdout.py
│  │  ├─ bootstrap.py
│  │  ├─ sensitivity.py
│  │  └─ reports.py
│  └─ notifications/
│     └─ btc_telegram.py
├─ web/
│  ├─ btc-personal-state.js
│  ├─ btc-charts.js
│  └─ vendor/                           # 승인된 경우 JS 라이브러리 고정본
├─ data/
│  ├─ cache/
│  ├─ snapshots/
│  └─ reference/
│     └─ bitcoin_halving_blocks.json
├─ output/
│  ├─ daily.json
│  ├─ btc_daily.json
│  ├─ btc_chart_data.json
│  ├─ btc_signal_history.csv
│  ├─ btc_shadow_ledger.csv
│  ├─ btc_research_summary.json
│  └─ btc_data_quality.json
├─ tests/
│  ├─ fixtures/btc/
│  ├─ test_btc_halving.py
│  ├─ test_btc_data_quality.py
│  ├─ test_btc_indicators.py
│  ├─ test_btc_state_machine.py
│  ├─ test_btc_portfolio.py
│  ├─ test_btc_backtest_no_lookahead.py
│  ├─ test_btc_outputs.py
│  ├─ test_btc_telegram.py
│  ├─ test_btc_privacy.py
│  └─ test_etf_regression.py
└─ docs/
   ├─ PRD.md
   ├─ TRD.md
   ├─ OPERATIONS.md
   ├─ BTC_STRATEGY_RESEARCH.md
   └─ BTC_SHADOW_MODE.md
```

---

## 4. 데이터 모델

### 4.1 확정 일봉 표준 스키마

```python
@dataclass(frozen=True)
class DailyCandle:
    provider: str
    symbol: str
    quote_currency: str
    open_time_utc: datetime
    close_time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume_base: float | None
    volume_quote: float | None
    is_final: bool
    fetched_at_utc: datetime
    available_at_utc: datetime
    source_revision: str
```

필수 제약:

```text
open_time_utc < close_time_utc
low <= min(open, close) <= max(open, close) <= high
close > 0
동일 provider+symbol+open_time 중복 금지
신호 시각 >= available_at_utc
```

### 4.2 온체인 표준 스키마

```python
@dataclass(frozen=True)
class DailyMetric:
    source: str
    asset: str
    metric_id: str
    observation_time_utc: datetime
    available_at_utc: datetime
    value: float | None
    unit: str
    status: str              # ok | stale | missing | revised
    source_revision: str
```

온체인 데이터는 관측일과 실제 입수시각을 분리한다. 백테스트는 `observation_time`이 아니라 `available_at`을 기준으로 병합한다.

### 4.3 블록·반감기 스키마

```python
@dataclass(frozen=True)
class HalvingContext:
    tip_height: int
    tip_time_utc: datetime
    epoch: int
    epoch_start_height: int
    next_halving_height: int
    blocks_since_halving: int
    blocks_to_halving: int
    cycle_progress: float
    days_since_halving: float
    block_subsidy_btc: float
    estimated_days_to_halving: float
    estimated_next_halving_utc: datetime
    blocks_per_day_30: float
    blocks_per_day_90: float
    annualized_new_supply_pct: float | None
    phase_label: str
    source_primary: str
    source_backup: str | None
    verified: bool
```

---

## 5. 공급자와 데이터 우선순위

### 5.1 가격

| 역할 | 1차 | 2차/교차검증 | 사용 목적 |
|---|---|---|---|
| 실제 실행가격 | Upbit `KRW-BTC` | Upbit ticker | 목표금액·가상체결 |
| 장기 USD 기준 | Yahoo `BTC-USD` | 독립 USD 현물거래소 | 2016 이후 기준연구 |
| USD 현물 교차검증 | Coinbase/Kraken/Bitstamp 후보 | 다른 거래소 | 공급자 이상·괴리 탐지 |
| 환율 | 공식/신뢰 가능한 USDKRW 계열 | 대체 공급자 | 합성 KRW 가격·김치프리미엄 |

`Yahoo + Upbit`, `Upbit 중심`, `USD 단일 거래소`, `USD/KRW 합의형`을 각각 독립 전략으로 평가하고 결과를 섞지 않는다.

### 5.2 블록 높이

```text
1차: mempool.space block tip API
2차: Bitcoin Core RPC getblockcount 또는 승인된 독립 공급자
```

두 소스가 모두 있으면 높이 차이가 허용범위를 넘는지 검사한다. 한 소스만 정상일 때는 경고와 함께 계산할 수 있으나, 연속 실패 또는 비정상 역행 시 `DATA_ERROR`로 전환한다.

### 5.3 온체인

초기 후보는 Coin Metrics Community API에서 실제 무료 가용성을 확인한 항목만 운영판에 넣는다.

```text
우선 후보: realized cap/price, MVRV, NUPL, SOPR, miner revenue,
           hashrate, fees, transfer activity
조건부 후보: exchange flows, holder-segment metrics, open interest
```

유료 지표가 필요하면 무료 대체식이 정확히 재현 가능한지 검토하고, 불가능하면 `optional_paid`로 분류한다. 무단 스크래핑으로 대체하지 않는다.

### 5.4 데이터 등급

```text
CRITICAL
- Upbit 확정 일봉
- USD 기준 가격 최소 1개
- 블록 높이/반감기 컨텍스트

CORE
- 장기 추세·모멘텀 계산 가능 OHLCV
- 승인된 핵심 온체인 지표 최소 개수

OPTIONAL
- 파생, 거시, ETF 흐름, 뉴스·감성
```

CRITICAL 실패 시 신규 행동을 금지한다. CORE 일부 누락은 신뢰도와 허용 최대비중을 낮추며, OPTIONAL 실패는 설명에만 경고한다.

---

## 6. UTC 일봉과 실행시각

### 6.1 일봉 기준

```text
공통 기준: UTC 00:00 ~ 다음 날 UTC 00:00
KST 마감: 오전 09:00
수집·검증 시작 후보: KST 09:12
신호·배포 예약 후보: KST 09:17
```

미완성 당일 캔들은 차트 미리보기에는 표시할 수 있지만 신호 계산에서는 제외한다.

### 6.2 GitHub Actions

권장 구조:

```text
ETF workflow: 기존 평일 스케줄 유지 또는 최신 로컬 구조에 맞춤
BTC workflow: 매일 UTC 00:17 (KST 09:17), 주말 포함
Pages deploy: 전체 사이트를 원자적으로 재생성
```

BTC 주말 실행 시 ETF는 마지막 정상 캐시를 사용한다. 두 워크플로가 Pages를 동시에 배포하지 않도록 동일 concurrency group과 적절한 시간간격을 둔다.

### 6.3 재시도

```text
수집 1차 실패 → 2분 뒤 재시도
2차 실패 → 백업 공급자
백업도 실패 → stale 허용시간 검토
CRITICAL stale 초과 → DATA_ERROR + 오류 알림
```

---

## 7. 반감기 엔진

### 7.1 프로토콜 계산

```python
HALVING_INTERVAL = 210_000
INITIAL_SUBSIDY_BTC = 50.0

epoch = tip_height // HALVING_INTERVAL
epoch_start_height = epoch * HALVING_INTERVAL
next_halving_height = (epoch + 1) * HALVING_INTERVAL
blocks_since = tip_height - epoch_start_height
blocks_to = next_halving_height - tip_height
cycle_progress = blocks_since / HALVING_INTERVAL
subsidy = INITIAL_SUBSIDY_BTC / (2 ** epoch)
```

현재 예상 반감기 날짜는 최근 30일·90일 블록생성속도의 강건한 결합으로 계산하되, 화면 참고값일 뿐 매매입력으로 사용하지 않는다.

```python
blocks_per_day = weighted_median(blocks_per_day_30, blocks_per_day_90)
estimated_days = blocks_to / blocks_per_day
```

### 7.2 연속형 사이클 특성

고정 월수만 사용하지 않고 블록 진행률을 우선한다.

```text
cycle_progress                  0~1
cycle_sin = sin(2π·progress)
cycle_cos = cos(2π·progress)
log_blocks_since_halving
log_blocks_to_halving
issuance_ratio_vs_prev_epoch
annualized_new_supply_pct
```

### 7.3 화면용 국면 후보

아래 경계는 설명용 후보이며 연구를 통해 조정한다.

| 국면 | 진행률 후보 | 의미 |
|---|---:|---|
| `HALVING_TRANSITION` | 0.00–0.08 | 반감기 직후 공급전환·변동성 적응 |
| `POST_HALVING_EXPANSION` | 0.08–0.32 | 과거 상승확장 후보 구간 |
| `LATE_EXPANSION_DISTRIBUTION` | 0.32–0.50 | 과열·분배 민감도 상승 |
| `CONTRACTION_RECOVERY` | 0.50–0.75 | 하락·바닥형성·회복 후보 |
| `PRE_HALVING_ACCUMULATION` | 0.75–1.00 | 다음 반감기 전 매집·재평가 후보 |

국면명은 목표비중을 직접 결정하지 않는다. 국면은 요구 확인수, 최대 허용비중, 과열 민감도를 조정한다.

### 7.4 정확성 검증

테스트 픽스처는 최소 다음 경계를 포함한다.

```text
209,999 → epoch 0, subsidy 50
210,000 → epoch 1, subsidy 25
419,999 → epoch 1
420,000 → epoch 2, subsidy 12.5
630,000 → epoch 3, subsidy 6.25
840,000 → epoch 4, subsidy 3.125
1,050,000 → epoch 5, subsidy 1.5625
```

---

## 8. 특성 계산

### 8.1 가격·추세 영역

```text
SMA/EMA: 20, 50, 100, 200일
주봉: 20, 40, 104, 200주 후보
이격: log(close / MA)
기울기: 20·60일 정규화 slope
돌파/재지지: close가 밴드를 넘은 뒤 N일 유지
ADX/DI: 추세 강도 보조
```

단순 `close > SMA200`보다 1~3% 비영 밴드와 지속일을 비교해 횡보구간 잦은 왕복매매를 줄인다.

### 8.2 가치·투매 영역

```text
ATH 및 365일 고점 대비 낙폭
200일·200주선 이격
실현가격 이격
MVRV / MVRV Z 또는 expanding percentile
NUPL
Puell 계열 또는 miner revenue percentile
30·90일 급락률
손실실현(SOPR < 1)과 회복
```

고정 임계값만 사용하지 않고 다음을 병행한다.

```text
full-history expanding percentile
current-halving-epoch percentile
rolling 4y percentile
```

현재까지 알려진 값만 사용하도록 expanding/rolling 계산을 한 칸 지연한다.

### 8.3 모멘텀·진입 영역

```text
RSI 14일·주봉
PPO 또는 MACD 중 하나를 주 지표로 선택
30·90·180일 수익률
볼린저 밴드 위치·폭
ATR/NATR
거래량 z-score, OBV
가격-모멘텀 다이버전스 후보
```

MACD·PPO·여러 EMA는 같은 정보를 중복하므로 한 영역 내 점수상한을 둔다.

### 8.4 과열·분배 영역

```text
MVRV/NUPL/실현이익의 expanding 상위 분위
주봉 RSI 상위 분위
close/SMA200 및 close/200WMA의 상위 분위
90·180일 포물선 수익률
변동성 급증과 추세 둔화
SOPR·거래량의 분배 패턴
김치프리미엄 상위 분위
```

고정된 과거 최고 임계값이 다음 사이클에서 사라질 수 있으므로 절대값과 분위값을 함께 평가한다.

### 8.5 원화시장 영역

```text
synthetic_krw = usd_btc_close × usdkrw_close
kimchi_premium = upbit_close / synthetic_krw - 1
usd_state
krw_state
state_disagreement
provider_price_spread
```

김치프리미엄은 매수·축소를 단독 결정하지 않고 실행지연 또는 한 단계 보수화에 사용한다.

### 8.6 거시·파생 영역

DXY, VIX, 실질금리, 유동성, 펀딩, 미결제약정, 현물 ETF 흐름은 연구용으로 시작한다. 워크포워드에서 증분효과가 반복 확인되기 전에는 핵심 상태를 뒤집지 못한다.

---

## 9. 증거영역과 상태 머신

### 9.1 증거 객체

```python
@dataclass(frozen=True)
class EvidenceDomain:
    name: str
    direction: str          # accumulation | neutral | distribution
    strength: float         # 0..1, 설명/정렬용
    confidence: float       # 데이터 품질 포함
    triggered_rules: tuple[str, ...]
    missing_inputs: tuple[str, ...]
```

영역 예시:

```text
cycle_supply
long_term_trend
valuation_capitulation
momentum_timing
overheat_distribution
krw_crosscheck
optional_macro_derivatives
```

### 9.2 행동 결정 원칙

- 반감기 영역은 항상 존재해야 하지만 그것 하나만으로 전이할 수 없다.
- 매수는 최소 3개 독립영역의 확인을 요구하는 후보를 우선한다.
- 축소는 과열영역과 추세영역을 분리해 저점 뒤늦은 매도를 방지한다.
- 데이터 신뢰도가 낮으면 목표비중 상한을 낮춘다.
- 상태 전이는 목표비중을 만들고, 표시점수는 설명용이다.

### 9.3 후보 전이

```text
WAIT
 └─ 가치/사이클 접근 → WATCH
WATCH
 └─ 반감기 유효 + 가치·투매 다중확인 → ACCUMULATE_1
ACCUMULATE_1
 └─ 신호 지속·재시험·양의 다이버전스 → ACCUMULATE_2
ACCUMULATE_2
 └─ 50일선/PPO/주봉 안정 회복 → CONFIRM_BUY
CONFIRM_BUY
 └─ 200일선 또는 40주선 회복·재지지 → TREND_HOLD
TREND_HOLD
 └─ 과열 낮고 사이클·추세 허용 → FULL_HOLD
FULL_HOLD
 └─ 과열 독립영역 2개 이상 지속 → REDUCE_1
REDUCE_1
 └─ 과열 지속 + 주봉 추세약화 → REDUCE_2
REDUCE_2
 └─ 구조적 약세 지속 → CORE_ONLY 또는 EXIT
```

### 9.4 지속·히스테리시스

연구 후보:

```text
일봉 조건 지속: 2~5개 확정 일봉
주봉 조건: 주봉 마감 1~2회
상태변경 냉각기간: 5~20일
재진입 밴드: 진입 임계값과 이탈 임계값 분리
하루 최대 전이 단계: 1단계
```

심각한 데이터 오류는 상태를 `DATA_ERROR`로 표시하되 실제 가상 보유비중을 자동 청산하지 않는다.

---

## 10. 핵심물량·전술물량

### 10.1 회계 분리

```text
total_target_weight = core_target_weight + tactical_target_weight
0 <= core <= approved_core_cap
0 <= tactical <= 1 - core
```

후보 비교:

```text
core 0% / tactical 100%
core 25% / tactical 75%
core 50% / tactical 50%
core 75% / tactical 25%
core 100% buy-and-hold benchmark
```

### 10.2 후보 운영 철학

- 핵심물량: 반감기·장기추세·가치 국면을 따라 낮은 회전율로 유지한다.
- 전술물량: 투매매집·추세회복·과열축소에 따라 단계적으로 조절한다.
- 핵심 25%는 현재 선호 후보일 뿐 백테스트 후 확정한다.
- `all_out`, `tiered`, `core_tactical`을 같은 비용조건으로 비교한다.

---

## 11. 개인 예산과 브라우저 상태

### 11.1 사용자 선택 UI

```text
예산 모드:
- 고정 전략예산
- 현재 전략자산 기준
- 정기 납입 계획

빠른 선택:
- 100만원
- 300만원
- 500만원
- 1,000만원
- 직접입력
```

### 11.2 localStorage 스키마

키: `quant_guardian_btc_personal_v1`

```json
{
  "schemaVersion": 1,
  "budgetMode": "fixed",
  "fixedBudgetKrw": 5000000,
  "cashKrw": 5000000,
  "btcQuantity": 0,
  "averageCostKrw": null,
  "monthlyContributionKrw": 0,
  "transactions": [],
  "cashFlows": [],
  "updatedAt": "2026-08-09T00:00:00Z"
}
```

### 11.3 계산식

#### 고정예산

```text
target_value = fixed_budget × target_weight
current_btc_value = btc_quantity × upbit_reference_price
adjustment = target_value - current_btc_value
```

#### 현재 NAV

```text
nav = cash + current_btc_value
target_value = nav × target_weight
adjustment = target_value - current_btc_value
```

#### 정기납입

```text
committed_capital = initial_budget + cumulative_contributions - withdrawals
target_value = committed_capital × target_weight
```

### 11.4 개인 수익률

```text
미실현 손익률 = current_price / average_cost - 1
단순 계좌수익률 = (current_nav - net_contributions) / net_contributions
TWR = 외부 현금흐름 구간별 수익률의 기하연결
XIRR = 일자별 현금흐름의 연환산 내부수익률
```

MVP에서 거래내역이 없으면 미실현·단순 계좌수익률만 표시한다. 거래내역이 충분하면 TWR과 XIRR을 추가한다. 서로 다른 수익률을 같은 명칭으로 표시하지 않는다.

### 11.5 개인정보 방지

- 개인 상태는 서버로 전송하지 않는다.
- 공개 HTML 소스에는 기본예산 예시만 들어가며 실제 입력값은 들어가지 않는다.
- 내보내기 JSON은 사용자가 명시적으로 다운로드할 때만 생성한다.
- 콘솔로그·텔레메트리·GitHub artifact에 개인 값을 기록하지 않는다.
- 공용 PC 경고와 전체 초기화 버튼을 제공한다.

---

## 12. 텔레그램 개인화

### 12.1 기본 메시지

텔레그램은 예산과 무관한 정보를 기본으로 전송한다.

```text
현재 상태 / 행동
목표비중
반감기 국면·진행률
전략 수익률
동일기간 BTC 단순보유 수익률
현재 상태 이후 수익률
핵심 근거와 다음 조건
데이터 상태
```

### 12.2 선택 예산 금액

정적 GitHub Pages의 `localStorage`는 GitHub Actions가 읽을 수 없다. 따라서 텔레그램 금액표시는 별도 설정이다.

```text
Repository Variable 또는 Secret:
BTC_BUDGET_KRW=5000000
BTC_TELEGRAM_SHOW_AMOUNT=true
```

사용자가 사이트 예산을 바꿔도 텔레그램 예산은 자동으로 따라가지 않는다. 메시지에 `텔레그램 설정 예산`이라고 명시해 오해를 막는다.

### 12.3 미래 확장

봇 명령으로 예산을 바꾸는 방식은 비공개 저장소·인증·상태 DB가 필요하므로 v1 범위 밖이다.

---

## 13. 공개 출력 스키마

### 13.1 `btc_daily.json`

```json
{
  "schema_version": 1,
  "generated_at_utc": "...",
  "signal_as_of_utc": "...",
  "mode": "shadow",
  "data_status": "ok",
  "market": {
    "upbit_krw_close": 0,
    "usd_close": 0,
    "usdkrw": 0,
    "kimchi_premium_pct": 0
  },
  "halving": {
    "tip_height": 0,
    "epoch": 0,
    "cycle_progress": 0,
    "phase": "...",
    "blocks_to_halving": 0,
    "subsidy_btc": 0
  },
  "signal": {
    "state": "WATCH",
    "previous_state": "WAIT",
    "target_weight": 0,
    "core_weight": 0,
    "tactical_weight": 0,
    "signal_id": "sha256:...",
    "reasons": [],
    "next_buy_conditions": [],
    "next_reduce_conditions": []
  },
  "returns": {
    "strategy": {},
    "buy_hold": {},
    "excess": {},
    "current_state": {}
  },
  "data_quality": {
    "critical_missing": [],
    "stale_metrics": [],
    "provider_disagreements": []
  }
}
```

개인 예산·수량·평균단가·개인 거래내역은 금지한다.

### 13.2 `signal_id`

아래 정규화 값을 해시한다.

```text
signal_as_of_utc
strategy_version
state
core_weight
tactical_weight
critical_input_revision_hash
```

동일 `signal_id`의 중복 텔레그램 발송을 막는다.

---

## 14. 차트 구현

### 14.1 데이터와 렌더링 분리

Python은 차트 데이터 JSON을 생성하고 브라우저가 렌더링한다. 차트 라이브러리는 최신 로컬 UI를 확인한 뒤 확정하되 다음 조건을 만족해야 한다.

- 모바일 터치·확대 지원
- 일봉 캔들과 여러 선·마커 표시
- 정적 GitHub Pages에서 동작
- 외부 사용자 추적 없음
- 가능하면 버전 고정·로컬 vendor

### 14.2 필수 패널

1. 가격·거래량·50/100/200일선·주봉 장기선
2. 반감기 수직선·사이클 음영·매수/축소/매도 마커
3. RSI와 PPO/MACD
4. 낙폭·ATR·실현변동성
5. MVRV·NUPL·SOPR·광부수익 계열 중 승인 지표
6. 김치프리미엄과 공급자 괴리
7. 전략 자산곡선·BTC 단순보유·두 전략의 낙폭

### 14.3 차트 안전성

- 오늘 미완성 캔들은 별도 점선 또는 `진행 중` 표시
- 백테스트 매매마커는 다음 실행가능 시점에 표시
- 데이터 결측구간은 선으로 연결하지 않는다
- 서로 다른 축을 같은 축인 것처럼 겹치지 않는다
- 차트의 모든 임계값은 현재 전략버전과 연결한다

---

## 15. 백테스트 실행 엔진

### 15.1 체결

```text
신호 계산: UTC 일봉 확정 후
기본 체결: 다음 Upbit 실행가능 일봉 시가
수수료: 실행 당시 설정값
슬리피지: 고정 bps + 변동성/주문크기 스트레스 후보
최소주문: Upbit KRW 최소주문 규칙 반영
현금 부족·BTC 음수 금지
```

수수료율은 코드 상수가 아니라 버전된 설정값이며, 실제 운영 전 최신 Upbit 조건을 재확인한다.

### 15.2 미래참조 방지

- indicator는 당일까지의 데이터만 사용한다.
- `available_at_utc`보다 먼저 온체인 값을 사용하지 않는다.
- expanding percentile은 현재값을 포함하기 전 과거분포를 사용하거나 명시적으로 한 칸 지연한다.
- 최적화 구간과 평가 구간을 분리한다.
- 종가 신호를 같은 종가에 체결하지 않는다.

### 15.3 검증 모드

```text
anchored walk-forward
yearly expanding window
halving-epoch holdout
leave-one-cycle-out
parameter neighborhood
stationary/block bootstrap
fee/slippage/delay stress
provider substitution stress
```

### 15.4 연구 결과 선택

단일 최고 CAGR을 자동 채택하지 않는다. 다음 축의 Pareto frontier를 만든다.

```text
CAGR 최대
MDD 최소
Calmar 최대
회전율 최소
단순보유 대비 개선
사이클별 일관성
임계값 주변 안정성
```

사용자가 최종 전략버전과 비중을 승인해야 `approved_strategy`가 생성된다.

---

## 16. 설정 구조

`CONFIG_BTC_CANDIDATE_v0.3.toml`을 연구 시작점으로 사용한다.

```toml
[btc]
enabled = true
mode = "shadow"
halving_required = true
execution_market = "KRW-BTC"
candle_timezone = "UTC"

[btc.personal]
default_budget_krw = 5000000
budget_presets_krw = [1000000, 3000000, 5000000, 10000000]
storage = "browser_local"

[btc.research]
primary_report_start = "2016-01-01"
extended_cycle_start = "2012-01-01"
risk_mdd_hard_limit = -0.50
preferred_mdd_range = [-0.45, -0.35]
```

운영판에는 후보 그리드가 아니라 승인된 한 전략버전만 들어간다.

---

## 17. CLI 후보

```text
python quant_guardian.py btc-data --refresh
python quant_guardian.py btc-halving
python quant_guardian.py btc-signal --mode shadow
python quant_guardian.py btc-backtest --experiment baseline
python quant_guardian.py btc-research --walk-forward
python quant_guardian.py btc-report
python quant_guardian.py build --include-btc
python telegram_notify.py --section btc
```

모든 명령은 비정상 종료 시 원인과 사용한 캐시 시점을 출력한다.

---

## 18. 오류 처리와 관측성

### 18.1 오류 등급

```text
FATAL: CRITICAL 데이터 부족, 반감기 검증 실패, 미래 일봉 감지
ERROR: 운영 신호 생성 실패
WARN: 온체인 일부 stale, 공급자 괴리, optional 데이터 실패
INFO: 정상 신호·상태 유지·배포 완료
```

### 18.2 로그

로그에는 다음을 남긴다.

```text
run_id
strategy_version
input snapshot hashes
latest final candle
block height sources
metric freshness
state transition
output hashes
notification result
```

토큰·chat ID·개인 예산·개인 보유량은 로그에 남기지 않는다.

### 18.3 데이터 스냅샷

각 연구 실행은 재현을 위해 manifest를 저장한다.

```json
{
  "run_id": "...",
  "code_sha": "...",
  "config_sha": "...",
  "data_sources": [],
  "source_revisions": {},
  "date_range": {},
  "generated_at": "..."
}
```

---

## 19. 보안

- v1에는 Upbit 개인 API 키가 필요 없다.
- 주문·잔고 API를 호출하지 않는다.
- Telegram token과 chat ID는 GitHub Secrets만 사용한다.
- 공개 JSON에 Secret 또는 개인 상태가 포함되는지 자동 검사한다.
- 외부 JS를 사용하는 경우 무결성·버전 고정 또는 로컬 vendor를 사용한다.
- Content Security Policy 적용 가능성을 검토한다.

---

## 20. 테스트 전략

상세 시나리오는 `ACCEPTANCE_TESTS_BTC_EXTENSION_v0.3.md`에 정의한다.

필수 범주:

```text
반감기 경계 계산
UTC 확정 일봉 필터
데이터 가용시각과 미래참조
기술·온체인 지표 수치
상태 전이·지속·히스테리시스
핵심+전술 합계 및 0~100% 범위
예산 모드별 원화 계산
개인정보 비공개 검사
비용·슬리피지·다음 시가 체결
텔레그램 중복 방지
차트 JSON 스키마
ETF 회귀
30일 shadow ledger
```

---

## 21. 단계별 구현

### Phase 0 — 기준선

- 최신 로컬 GitHub push 및 commit SHA 확정
- ETF 회귀 픽스처 확보
- 문서 병합 위치 확정

### Phase 1 — 데이터·반감기

- Upbit 일봉
- USD 기준가격·FX
- 블록 높이 1차/2차
- 온체인 catalog·가용성 검사
- 데이터 품질 보고

### Phase 2 — 연구 엔진

- 지표·feature frame
- 기존 투매매수 seed 재현
- 반감기·온체인·추세 추가
- 매도정책 3종
- 워크포워드·사이클 홀드아웃

### Phase 3 — 승인 후보

- Pareto 후보 보고
- 임계값 민감도
- 사용자 전략·비중 승인
- `strategy_version` 고정

### Phase 4 — 화면·개인 계산

- BTC 탭·차트
- 예산 모드·localStorage
- 수익률 카드
- 모바일 검증

### Phase 5 — 알림·배포

- 매일 09:17 KST
- 상태변경·오류 알림
- Pages 원자적 배포
- 중복 방지

### Phase 6 — 30일 모의운영

- 가상체결
- 운영·데이터·알림 보고
- 사용자 승인 후에만 live 표시 검토

---

## 22. 완료 정의

다음 조건을 모두 충족해야 BTC v1 구현 완료로 본다.

1. 최신 로컬 ETF 기능 회귀 없음.
2. 모든 배포 가능한 신호에 검증된 반감기 컨텍스트 포함.
3. 반감기 단독으로 매매상태가 변하지 않음.
4. 가격·온체인·원화시장 중 어떤 증거가 사용됐는지 설명 가능.
5. 사용자 예산을 바꿔도 시장 신호와 수익률이 바뀌지 않음.
6. 개인 상태가 공개 산출물에 포함되지 않음.
7. 백테스트에 다음 실행가격·비용·가용시각 반영.
8. 워크포워드·사이클 홀드아웃·민감도 보고 생성.
9. 30개 확정 일봉 shadow 운영 통과.
10. 사용자 최종 전략 승인 기록 존재.
