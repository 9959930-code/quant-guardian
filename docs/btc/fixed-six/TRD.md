# Quant Guardian BTC Fixed Six — TRD

- 기준일: 2026-08-11
- 구현 브랜치: `feature/btc-fixed-six-telegram-bot`

## 1. 구성

```text
GitHub Actions 15분 예약
        │
        ├─ Telegram getUpdates
        ├─ mempool.space block height
        ├─ Blockstream block height
        └─ Upbit KRW-BTC ticker
                │
                ▼
       btc_fixed_advisory.py
       - 상태·반감기·6회 전략
       - 예산·추가입금·동기화
       - 주문 검토액 계산
                │
                ▼
     btc_fixed_telegram_bot.py
       - 한글 메뉴·버튼
       - 확인 절차·재알림
       - Telegram 발송
                │
                ▼
 GitHub Actions cache state JSON
```

## 2. 런타임

- Python 3.12
- 표준 라이브러리만 사용
- 예약 실행 시 외부 패키지 설치 없음
- PR·push 검증 시 저장소 전체 테스트를 위해 `requirements.txt` 설치

## 3. 데이터

### 3.1 블록 높이

- `mempool.space/api/blocks/tip/height`
- `blockstream.info/api/blocks/tip/height`
- 두 값의 차이가 3블록을 넘으면 데이터 오류
- 합의 높이는 더 낮은 값을 사용

```text
epoch = block_height // 210000
cycle_progress = (block_height % 210000) / 210000
```

### 3.2 Upbit

- 공개 `ticker` API로 `KRW-BTC` 현재가 조회
- API 키 불필요
- 주문·잔고 API는 사용하지 않음

## 4. 상태파일

경로:

```text
data/state/btc_fixed_state.json
```

GitHub Actions cache prefix:

```text
btc-fixed-six-state-
```

핵심 영역:

```text
account
- initial_budget_krw
- total_contributions_krw
- cash_krw
- btc_quantity
- reserve_next_krw
- peak_equity_krw
- max_drawdown
- history

strategy
- phase: WAITING_ENTRY / ENTRY / HOLD / EXIT
- cycle_epoch
- entry_steps_completed
- exit_steps_completed
- correction_buy_pending
- block threshold alert history

telegram
- last_update_id
- conversation
- pending_operation
- pending_sync
```

전략 버전 또는 상태 스키마가 다르면 10,000,000원·0 BTC로 새 상태를 생성한다.

## 5. 전략 상태전이

```text
WAITING_ENTRY
  progress >= 65%
  → ENTRY 1차 주문 대기

ENTRY
  sync 확인 후 단계 증가
  3차 sync 완료
  → HOLD

HOLD
  다음 epoch progress > 35%
  → EXIT 1차 주문 대기

EXIT
  sync 확인 후 단계 증가
  3차 sync 완료
  → WAITING_ENTRY
```

주문 안내만으로 단계를 완료하지 않는다. 실제 BTC·KRW 잔고 동기화가 확정돼야 단계가 증가한다.

## 6. 목표금액 계산

다음 사이클 대기자금은 현재 사이클 투자금에서 제외한다.

```text
btc_value = btc_quantity × price
active_equity = cash - reserve_next + btc_value
target_btc_value = active_equity × target_weight
adjustment = target_btc_value - btc_value
```

- adjustment > 0: 매수 검토액
- adjustment < 0: 매도 검토액
- 목표비중은 33.3/66.7/100 또는 66.7/33.3/0

## 7. 추가입금

- 입금 확정 시 `cash_krw`, `total_contributions_krw` 증가
- MDD 왜곡 방지를 위해 `peak_equity_krw`도 같은 금액만큼 조정
- `next` 선택 시 `reserve_next_krw` 증가
- HOLD의 `current` 선택 시 `correction_buy_pending = true`

## 8. Telegram polling

- `getUpdates` 사용
- `last_update_id + 1`을 offset으로 사용
- 최초 실행에서는 이전 대기 업데이트를 소비만 하고 처리하지 않음
- 허용된 `TELEGRAM_CHAT_ID`의 message/callback만 처리
- webhook 사용 없음

## 9. 대화·확인

- 숫자 입력 흐름은 `conversation`에 저장
- 상태 변경은 `pending_operation`에 저장
- 확인 유효기간: 24시간
- 최초 재알림: 30분
- 이후 일일 재알림: 09:17 KST
- 주문 후 동기화는 `pending_sync`로 별도 관리

## 10. 스케줄

```text
cron: 2,17,32,47 * * * *
```

- 15분 간격 준실시간 polling
- 월요일 09:17 이후 첫 실행에서 공식 단계 판단
- 첫 월요일 월간보고
- 동일 월요일·동일 월간보고 중복 방지 키 저장

## 11. 동시성

모든 실행은 공통 concurrency group을 사용한다.

```text
btc-fixed-six-state
cancel-in-progress: false
```

한 번에 하나의 실행만 상태파일을 수정한다.

## 12. 보안

- Bot token과 chat ID는 GitHub Secrets
- 상태파일에 API token 저장 금지
- 주문·출금 권한 API 키 미사용
- 허용 chat ID 외 메시지 무시
- 공개 저장소에 실제 상태 JSON 커밋 금지
- Telegram 버튼 callback은 현재 pending operation ID와 일치할 때만 처리

## 13. 장애 동작

- Upbit 또는 블록 공급자 오류: 새 주문 안내 중단
- 첫 오류: Telegram 오류 알림
- 동일 오류 지속: 반복 폭주 방지
- 복구: 정상화 알림
- 마지막 정상 상태와 계좌정보 유지

## 14. 테스트

- 10,000,000원 초기화
- 3단계 진입·3단계 청산
- HOLD 중 무리밸런싱
- 다음 epoch 35% 조건
- 추가입금 current/next
- 보정매수 1회
- budget 잠금
- sync 완료 전 다음 단계 차단
- Telegram 숫자 입력·버튼 확인
- 30분 재알림
- 기존 전체 unittest 회귀
