# Quant Guardian BTC Fixed Six — TRD

- clock-hybrid 기준일: 2026-08-19
- 전략 버전: `btc-fixed-six-clock-hybrid-1.1`
- 상태 스키마: 5

## 1. 구성

```text
GitHub Actions 15분 예약
        │
        ├─ Telegram getUpdates
        ├─ mempool.space / Blockstream 블록 높이
        ├─ Upbit KRW-BTC 현재가
        └─ ISA 시세·환율 Shadow
                │
                ▼
btc_clock_hybrid_portfolio_runner.py
        │
        ├─ btc_clock_hybrid_core.py
        │    상태 migration·블록임계값·주문 단계
        ├─ btc_clock_hybrid_telegram.py
        │    경고문구·현재가 재계산·지연 안전장치
        └─ portfolio_telegram_bot.py
             BTC·ISA 통합 입력 라우팅
                │
                ▼
BTC와 ISA별 GitHub Actions cache state JSON
```

기존 `btc_fixed_advisory.py`와 `btc_fixed_telegram_bot.py`의 검증된 예산·잔고·추가입금·Telegram 흐름을 유지하고, runtime 계층에서 clock-hybrid 규칙을 설치한다.

## 2. 데이터와 블록 판정

```text
epoch = block_height // 210000
offset = block_height % 210000
```

임계 오프셋:

```text
62.5% 관찰  = round(210000 × 0.625) = 131250
65% 매수    = round(210000 × 0.65)  = 136500
35% 경고    = round(210000 × 0.35)  = 73500
36% 매도    = round(210000 × 0.36)  = 75600
37% 매도    = round(210000 × 0.37)  = 77700
38% 매도    = round(210000 × 0.38)  = 79800
```

mempool.space와 Blockstream 높이 차이가 3블록을 넘으면 주문 판단을 중단한다. 실제 조건 비교는 `cycle_progress` 부동소수점보다 정수 offset을 사용한다.

## 3. 상태파일

BTC 경로:

```text
data/state/btc_fixed_state.json
cache prefix: btc-fixed-six-state-
```

추가된 strategy 필드:

```text
entry_watch_alerted_epochs
entry_alerted_epochs
halving_alerted_epochs
exit_warning_alerted_epochs
last_block_offset
```

pending sync 추가 필드:

```text
original_created_at_utc
age_anchor_at_utc
last_recalculated_at_utc
notice_24h_sent
notice_72h_sent
notice_7d_sent
plan_expired
policy_version
```

## 4. schema 4 → 5 migration

기존 `btc-fixed-six-upbit-telegram-1.0`, schema 4만 자동 변환한다.

보존 대상:

- account 전체
- phase·cycle_epoch·완료 단계
- correction buy와 completed cycle
- pending sync·pending operation·conversation
- Telegram update offset와 통합 메뉴 버전
- history·peak·MDD·감사로그

기존 `exit_alerted_epochs`는 `exit_warning_alerted_epochs` 초기값으로 복사한다. 기존 pending sync에는 주문안 경과시간 필드를 보충한다. 알 수 없는 버전은 새 상태로 초기화하지 않고 오류를 발생시킨다.

## 5. 상태전이

```text
WAITING_ENTRY
  offset 131250 통과 → ENTRY_WATCH 알림만
  공식 월요일 + offset >= 136500 → ENTRY 1차 주문

ENTRY
  잔고동기화 완료 시 단계 증가
  3차 완료 → HOLD

HOLD
  다음 epoch offset 73500 통과 → EXIT_WARNING 알림만
  공식 월요일 + offset >= 75600 → EXIT 1차 주문

EXIT
  1차 완료 후 offset >= 77700 → 2차 주문
  2차 완료 후 offset >= 79800 → 3차 주문
  3차 동기화 완료 → WAITING_ENTRY
```

임계값을 여러 개 넘어도 한 번의 공식 점검에서 한 주문만 만든다. pending sync가 있으면 모든 다음 단계가 차단된다.

## 6. 주문금액 계산

```text
btc_value = btc_quantity × current_price
active_equity = cash - reserve_next + btc_value
target_btc_value = active_equity × target_weight
adjustment = target_btc_value - btc_value
```

목표비중:

```text
매수: 1/3 → 2/3 → 1
매도: 2/3 → 1/3 → 0
```

## 7. 체결 지연 상태기계

주문 생성 시 `pending_sync`에 age anchor를 기록한다.

- 24시간: 현재 저장잔고와 최신 Upbit 가격으로 동일 목표비중 주문안을 재계산한다.
- 72시간: 지연 주의 메시지와 재계산 주문안을 보낸다.
- 7일: `plan_expired = true`로 표시한다.
- 만료 상태에서도 pending sync를 제거하지 않는다. 이미 주문했다면 늦은 잔고동기화로 해당 단계를 완료할 수 있다.
- 아직 주문하지 않았다면 `sync:refresh` callback으로 age anchor를 다시 시작한다.

이 방식은 만료 후 같은 단계를 중복 생성하거나, 실제 체결했는데 단계를 잃는 문제를 막는다.

## 8. Telegram 통합

BTC와 ISA는 하나의 `getUpdates` 소비자가 처리한다. BTC 주문 버튼:

```text
주문 완료 · 잔고 동기화
현재가로 주문안 재계산
30분 뒤 다시 알림
```

ISA 상태파일과 BTC 상태파일은 서로 독립적이며 자동주문은 없다.

## 9. 스케줄과 동시성

```text
cron: 2,17,32,47 * * * *
concurrency group: quant-guardian-telegram-state
cancel-in-progress: false
```

공식 주문 판단은 월요일 09:17 KST 이후 첫 실행에서만 한다. 블록 임계값이 월요일 점검 이후에 도달하면 다음 월요일까지 기다린다.

## 10. 테스트

- v4 상태의 자산·단계·pending 보존
- 알 수 없는 버전의 자동 초기화 금지
- 62.5% 직전·정확히 도달
- 65% 직전·정확히 도달
- 35% 경고만 생성
- 36·37·38% 단계별 주문
- 큰 임계값 점프에서도 한 단계만 생성
- pending sync 차단
- 24시간 재계산·7일 만료
- 수동 재계산의 age reset
- 전체 BTC·ISA·대시보드 회귀테스트
