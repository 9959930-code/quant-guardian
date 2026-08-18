# Quant Guardian BTC Fixed Six · Clock Hybrid 1.1 — TRD

- 구현 브랜치: `feature/btc-clock-hybrid-1-1`
- 런타임 버전: `btc-fixed-six-clock-hybrid-1.1`
- 상태 스키마: 5

## 1. 구성

```text
GitHub Actions 15분 예약
        │
        ├─ Telegram getUpdates
        ├─ mempool.space block height
        ├─ Blockstream block height
        ├─ Upbit KRW-BTC ticker
        └─ ISA 시세·환율 점검
                │
                ▼
 portfolio_telegram_clock_hybrid.py
        │
        ├─ btc_clock_hybrid_runtime.py
        │    - legacy 상태 마이그레이션
        │    - 블록 임계값·사전알림
        │    - 주문 재계산·취소 패치
        │
        └─ portfolio_telegram_bot.py
             - BTC·ISA 통합 입력 라우팅
             - 기존 한글 메뉴 유지
                │
                ▼
 BTC·ISA 별도 GitHub Actions cache state JSON
```

기존 `btc_fixed_advisory.py`, `btc_fixed_telegram_bot.py`, ISA 모듈은 직접 대규모 변경하지 않는다. 새 런타임 계층이 BTC 함수만 설치한 뒤 기존 통합 봇을 실행한다.

## 2. 블록 임계값

부동소수점 직접 비교 대신 epoch 내 블록 오프셋을 사용한다.

```text
HALVING_INTERVAL       = 210000
ENTRY_WATCH_OFFSET     = round(210000 × 0.625) = 131250
ENTRY_OFFSET           = round(210000 × 0.65)  = 136500
EXIT_WARNING_OFFSET    = round(210000 × 0.35)  = 73500
EXIT_OFFSETS           = (75600, 77700, 79800)
```

```python
offset = block_height % 210_000
```

매도 임계값은 `cycle_epoch + 1`에서 판정한다. 데이터 장애로 그 epoch를 넘긴 경우에는 미완료 단계를 순서대로 한 번씩 복구할 수 있으나, 한 공식 월요일에는 최대 한 단계만 만든다.

## 3. 상태파일·마이그레이션

경로:

```text
data/state/btc_fixed_state.json
```

cache prefix는 기존과 동일하다.

```text
btc-fixed-six-state-
```

### 3.1 legacy 인식

```text
schema_version = 4
strategy_version = btc-fixed-six-upbit-telegram-1.0
```

위 조합만 자동 마이그레이션한다.

### 3.2 새 상태

```text
schema_version = 5
strategy_version = btc-fixed-six-clock-hybrid-1.1
```

추가 필드:

```text
strategy.entry_watch_alerted_epochs
strategy.funding_alerts_sent
```

기존 `entry_alerted_epochs`는 65% 알림 이력, `exit_alerted_epochs`는 35% 경고 이력으로 유지한다.

### 3.3 보존 원칙

마이그레이션은 JSON 객체를 새로 초기화하지 않고 버전과 누락 필드만 보강한다. account, phase, 완료 단계, pending operation, pending sync, Telegram last update ID와 audit를 보존한다.

알 수 없는 버전은 `FixedStrategyError`로 중단한다. 1,000만원·0 BTC로 자동 초기화하지 않는다.

## 4. 자금준비 예상일

```text
remaining_blocks = ENTRY_OFFSET - current_offset
estimated_trigger = now + remaining_blocks × 10분
estimated_first_buy = estimated_trigger 이후 첫 월요일 09:17 KST
```

영업일 계산은 현재 KST 날짜 다음 날부터 예상 매수일까지의 월~금을 센다.

- `4~5영업일` 범위에 처음 진입: 5영업일 알림
- `0~3영업일` 범위에 처음 진입: 3영업일 알림
- 키: `{epoch}:{lead}`
- epoch별·lead별 중복 방지

정상 실행에서는 각각 정확히 5일·3일에 발송되지만, 블록속도 추정이 갑자기 변할 경우 4일 또는 2일에 근사 알림으로 들어올 수 있다. 메시지에 실제 추정 영업일을 함께 표시한다.

## 5. 이벤트 판정

`detect_block_events`는 다음을 반환한다.

- `ENTRY_FUNDING_PREP`
- `ENTRY_WATCH`
- `ENTRY_THRESHOLD`
- `HALVING`
- `EXIT_WARNING`

62.5%, 65%, 35% 이벤트는 epoch별 리스트로 중복을 막는다. 자금준비 이벤트는 `funding_alerts_sent`로 막는다.

## 6. 주문 상태전이

```text
WAITING_ENTRY
  offset >= 136500
  → ENTRY step 1 pending sync

ENTRY
  이전 sync 완료 후 공식 월요일마다 step 2, step 3
  → HOLD

HOLD
  cycle_epoch + 1의 offset >= 75600
  → EXIT step 1 pending sync

EXIT
  step 1 완료 + offset >= 77700
  → step 2
  step 2 완료 + offset >= 79800
  → step 3
  step 3 sync 완료
  → WAITING_ENTRY
```

`create_official_order`는 시작과 동시에 `last_official_monday`를 기록한다. 따라서 같은 월요일의 반복 실행으로 여러 단계를 만들 수 없다.

## 7. 주문 지연 안전장치

pending sync에 저장된 목표비중·종류·단계를 사용한다.

### 7.1 재계산

```text
latest_price + last_synced_balance + same_target_weight
→ 새 OrderInstruction
→ pending sync ID와 생성시각 갱신
```

목표비중에 이미 도달한 경우에는 주문 없이 해당 단계를 완료한다.

### 7.2 취소

pending sync만 제거하고 phase·완료단계를 변경하지 않는다. 조건이 계속 유효하면 다음 공식 월요일에 새 주문안을 만든다.

### 7.3 시간 표시

- 24시간: 재계산 필요
- 3일: 오래된 주문안 주의
- 7일: 기존 금액 사용 금지

자동 만료나 자동 주문은 없다.

## 8. 통합 Telegram

실행 진입점:

```text
portfolio_telegram_clock_hybrid.py
```

이 파일은 런타임 패치를 먼저 설치한 뒤 기존 `portfolio_telegram_bot.py`를 불러온다. 따라서 통합 봇이 저장해 둔 원래 BTC callback이 새 재계산·취소 callback을 포함하며, ISA callback은 기존처럼 별도로 라우팅된다.

메뉴는 변경하지 않는다.

```text
₿ BTC 상태 / 📈 ISA 상태
🔄 BTC 잔고 동기화 / 🔄 ISA 잔고 동기화
➕ BTC 추가입금 / 💰 BTC 시작예산
⏳ 대기 작업 / ❓ 도움말
```

## 9. 데이터·장애

- mempool.space와 Blockstream 높이 차이 3블록 초과 시 신규 행동 중단
- Upbit 현재가 오류 시 신규 주문·재계산 중단
- 마지막 정상 상태 유지
- 첫 오류와 복구만 Telegram 알림
- 자금준비 예상은 고정 10분/블록이며 외부 달력·예측 API를 쓰지 않음

## 10. 테스트

전용 테스트:

- schema 4→5 계좌·Telegram 상태 보존
- 알 수 없는 버전 자동 초기화 금지
- 62.5% 직전·정확 경계
- 65% 정확 경계 1차 매수
- 예상 5·3영업일 알림·중복 방지
- 35% 경고만
- 36·37·38% 순차 잠금
- 큰 임계값 점프 시 한 단계
- pending sync 차단
- 주문 재계산·취소 단계 안전
- 새 버튼 존재

전체 저장소 unittest도 함께 실행한다.
