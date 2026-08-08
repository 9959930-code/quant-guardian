# Sources — Quant Guardian Bitcoin Strategy Design

- 문서 버전: `0.3`
- 조사 기준일: `2026-08-09`
- 목적: 전략·데이터·운영 설계에 사용한 주요 공식 문서와 연구 근거 기록

> 이 목록은 인터넷상의 모든 자료를 뜻하지 않는다. 프로토콜·거래소 API·온체인 정의·반감기 연구·기술규칙 검증·운영 인프라에서 설계에 직접 영향을 주는 공식·1차 자료를 우선한 근거 목록이다.  
> 논문 결과는 서로 다르며, 특히 preprint는 동료평가가 끝나지 않았을 수 있다. 따라서 특정 논문 하나를 매수·매도 규칙으로 그대로 사용하지 않는다.

---

## 1. Bitcoin 프로토콜·반감기

### Bitcoin Core — consensus halving interval

- 출처: [Bitcoin Core `src/kernel/chainparams.cpp`](https://github.com/bitcoin/bitcoin/blob/master/src/kernel/chainparams.cpp)
- 유형: 공식 오픈소스 구현
- 확인사항: mainnet consensus의 `nSubsidyHalvingInterval = 210000`
- 설계 영향: 반감기 계산은 날짜가 아니라 블록 높이와 210,000블록 간격을 기준으로 한다.

### mempool.space REST API

- 출처: [mempool.space REST API documentation](https://mempool.space/docs/api/rest)
- 유형: 공개 Bitcoin 탐색기 API 문서
- 확인사항: tip height, block-height hash, block details 조회 가능
- 설계 영향: 운영 중 현재 블록 높이와 반감기 진행률을 계산하는 기본 공급자 후보다.

### 확인 가능한 반감기 경계 블록

- [Block 420000 — 2016-07-09 UTC](https://mempool.space/block/000000000000000002cce816c0ab2c5c269cb081896b7dcb34b8422d6b74ffa1)
- [Block 630000 — 2020-05-11 UTC](https://mempool.space/block/000000000000000000024bead8df69990852c202db0e0097c1a12ea637d7e96d)
- [Block 840000 — 2024-04-20 UTC](https://mempool.space/block/0000000000000000000320283a032748cef8227873ff4872689bf23f1cda83a5)
- 유형: 공개 블록 데이터
- 설계 영향: 경계값 단위테스트 fixture로 사용한다.

---

## 2. Upbit 원화 실행 데이터

### 일 캔들 API

- 출처: [Upbit 일(Day) 캔들 조회](https://docs.upbit.com/kr/reference/list-candles-days)
- 유형: 공식 거래소 API 문서
- 확인사항: `KRW-BTC`, `to` 페이지네이션, 요청당 최대 200개, 체결이 없으면 캔들이 생성되지 않을 수 있음, 일봉 시간경계가 UTC 기준
- 설계 영향: UTC 확정일봉, 누락검사, 과거 페이지네이션을 구현한다.

### 요청 수 제한

- 출처: [Upbit 요청 수 제한](https://docs.upbit.com/kr/reference/rate-limits)
- 유형: 공식 거래소 API 문서
- 확인사항: candle quotation 그룹 초당 최대 10회, `Remaining-Req`, 429/418 처리
- 설계 영향: 속도제한·백오프·캐시·재시도 정책을 구현한다.

### 거래수수료·최소주문

- 출처: [Upbit 고객센터 — 거래 수수료 안내](https://support.upbit.com/hc/ko/articles/4403838454809)
- 유형: 공식 거래소 운영 문서
- 설계 영향: 수수료율을 코드에 영구 고정하지 않고 설정·운영확인 대상으로 둔다. 최소주문과 반올림도 실제 실행규칙에 포함한다.

---

## 3. 온체인 데이터 정의와 API

### Coin Metrics API

- 출처: [Coin Metrics API conventions](https://docs.coinmetrics.io/api)
- 유형: 공식 데이터 공급자 문서
- 확인사항: Community API는 비상업적 용도에서 API key 없이 사용할 수 있는 데이터가 있으며 별도 rate limit가 적용됨
- 설계 영향: 구현 시 지표별 Community 가용성·라이선스·지연을 다시 확인한다. 유료지표를 무료라고 가정하지 않는다.

### Realized Cap, MVRV, MVRV Z

- 출처: [Coin Metrics Market Capitalization metrics](https://docs.coinmetrics.io/asset-metrics/market/capact1yrusd)
- 유형: 공식 지표 정의
- 설계 영향:
  - Realized Cap은 각 단위가 마지막으로 이동했을 때의 가격을 사용한 비용기준 근사치다.
  - MVRV는 Market Cap / Realized Cap이다.
  - 고정 임계값과 expanding/cycle-relative percentile을 함께 비교한다.

### SOPR와 NUPL

- 출처: [Coin Metrics Valuation metrics](https://docs.coinmetrics.io/asset-metrics/economics/rvtadj)
- 유형: 공식 지표 정의
- 설계 영향:
  - SOPR은 소비된 output의 지출가치/생성가치 비율로 손익실현을 근사한다.
  - NUPL은 시가총액 중 미실현 손익의 비중을 나타낸다.
  - 단일 날짜 극단값보다 지속·회복·다른 영역과의 합의를 사용한다.

### Hash rate와 miner revenue per hash

- 출처: [Coin Metrics Hash Rate metrics](https://docs.coinmetrics.io/asset-metrics/mining/hashrate)
- 유형: 공식 지표 정의
- 설계 영향: 반감기 전후 광부경제 변화와 네트워크 상태를 보조적으로 관찰한다.

### Exchange deposits

- 출처: [Coin Metrics Exchange Deposits](https://docs.coinmetrics.io/asset-metrics/exchange/flowinexusd)
- 유형: 공식 지표 정의
- 주의: 거래소 주소 식별·coverage와 UTXO change 처리에 의존한다.
- 설계 영향: 운영 핵심으로 바로 사용하지 않고 가용성과 아웃오브샘플 증분효과를 검증한다.

---

## 4. 반감기 가격효과 연구

### 2025 JRFM — halving별 수익·변동성 변화

- 논문: [Is Bitcoin’s Market Maturing? Cumulative Abnormal Returns and Volatility in the 2024 Halving and Past Cycles](https://www.mdpi.com/1911-8074/18/5/242)
- 유형: 동료평가 학술논문
- 주요 시사점: 2012·2016·2020·2024 반감기를 비교하며 최근 사이클의 수익과 변동성 반응이 약화되는 경향을 보고한다.
- 설계 영향: 반감기를 필수로 사용하되 과거 고정 배수·고정 임계값을 미래에 그대로 적용하지 않는다.

### 2025 synthetic-control 연구

- 논문: [Estimating the Impact of the Bitcoin Halving on Its Price Using Synthetic Control](https://arxiv.org/abs/2511.05512)
- 유형: preprint
- 주요 시사점: 2024 반감기에는 긍정적 효과 증거를 보고하지만 2020 효과는 통계적으로 강건하지 않았다고 보고한다.
- 설계 영향: 공급 사건과 가격 상승 사이의 인과를 자동 확정하지 않는다.

### Halving clock preprint

- 논문: [Bitcoin Runs on a Clock: Why Every Price Indicator Dies and the Halving Clock Doesn't](https://arxiv.org/abs/2607.26188)
- 유형: 2026 preprint
- 주요 시사점: 사이클별 지표 극단값이 약화될 수 있으며 블록·시간 구조를 강조한다.
- 주의: 회고적 turn 정의와 적은 cycle 수의 한계가 있으므로 실전 규칙으로 직접 채택하지 않는다.
- 설계 영향: block progress를 연속 변수로 사용하고 고정 임계값과 percentile 방식을 비교한다.

---

## 5. 기술적 규칙·과최적화·거래비용 연구

### 대규모 기술규칙 연구

- 논문: [Technical trading and cryptocurrencies](https://link.springer.com/article/10.1007/s10479-019-03357-1)
- 유형: 동료평가 학술논문
- 주요 시사점: 인샘플에서는 다수 기술규칙의 수익성이 나타났지만 Bitcoin의 아웃오브샘플 예측력은 확인되지 않았다.
- 설계 영향: 최고 인샘플 규칙을 사용하지 않고 walk-forward·cycle holdout을 필수화한다.

### 데이터 스누핑 reality check

- 논문: [A reality check on trading rule performance in the cryptocurrency market](https://www.sciencedirect.com/science/article/pii/S1544612320304414)
- 유형: 동료평가 학술논문
- 주요 시사점: 데이터 스누핑과 시장 마찰을 통제하면 유의한 초과수익이 드물다고 보고한다.
- 설계 영향: 다중 후보 탐색, 비교군, 비용·민감도 검증을 결과표에 공개한다.

### 현실적인 단순 기술규칙 검증

- 논문: [Are simple technical trading rules profitable in bitcoin markets?](https://www.sciencedirect.com/science/article/pii/S1059056024003010)
- 유형: 동료평가 학술논문
- 주요 시사점: 다수 규칙, 현실적 행동, 거래비용, data-mining 문제를 함께 검토한다.
- 설계 영향: 규칙 수가 많을수록 검증 기준을 더 엄격하게 한다.

### 2026 walk-forward·거래비용 연구

- 논문: [Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From Walk-Forward Forecasting](https://arxiv.org/abs/2606.00060)
- 유형: preprint
- 주요 시사점: 단순 방향예측은 비용에서 실패할 수 있고 비용을 넘는 기대변화가 있을 때만 거래하는 필터가 turnover를 줄일 수 있다고 보고한다.
- 설계 영향: hysteresis, cooldown, 최소 조정폭, 비용 0/기준/2배 stress test를 사용한다.

---

## 6. 온체인 예측력 연구

### 2026 rolling-window 연구

- 논문: [Bitcoin forecasting with machine learning and on-chain information](https://www.tandfonline.com/doi/full/10.1080/10293523.2026.2616575)
- 유형: 동료평가 학술논문
- 주요 시사점: 연구 표본에서 온체인 변수가 feature contribution의 큰 비중을 차지하고, mining·fee 관련 지표가 중요하게 나타났다고 보고한다.
- 설계 영향: 온체인을 독립 영역으로 연구하되 ML 결과를 운영 상태 머신보다 우선하지 않는다.

---

## 7. 운영 인프라

### GitHub Actions schedule 주의사항

- 출처: [GitHub Actions workflow troubleshooting](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
- 유형: 공식 플랫폼 문서
- 설계 영향: 정각 예약을 피하고 실행지연을 허용하며, 실제 실행시각과 대상 데이터시각을 분리 기록한다.

---

## 8. 연구 해석 원칙

1. 반감기는 필수 context지만 수익을 보장하는 단독 trigger가 아니다.
2. RSI·MACD·이동평균을 여러 개 넣었다고 독립증거가 늘어난 것으로 세지 않는다.
3. 온체인 지표도 정의·주소 군집화·데이터 수정·라이선스에 영향을 받는다.
4. 논문에서 유의하다고 나온 지표도 Quant Guardian의 기간·거래소·비용에서 재검증한다.
5. 실전 후보는 walk-forward, leave-one-cycle-out, 비용 stress, parameter neighborhood를 통과해야 한다.
6. 2016년 이후 주 분석과 2012년 이후 반감기 구조 분석을 분리한다.
7. Upbit 실체결 구간과 보완된 USD 장기구간을 하나의 동일 체결계열인 것처럼 합치지 않는다.
8. 연구상 최적 후보가 나와도 30개 확정 일봉 Shadow와 사용자 승인을 거친다.
