# Quant Guardian

미국 ETF 연구 대시보드와 BTC·ISA 전략의 과거 Telegram 실행 기록을 보존하는 저장소입니다. 자동주문과 출금 기능은 없습니다.

> **2026-11-22 운영 변경:** Telegram 그룹을 사용자가 삭제했고, BTC 알림의 주 운영 채널을 ChatGPT Scheduled로 전환했습니다. 새로운 정책은 **5회 누적 목표비중 매수(20/40/60/80/100%) + 다음 epoch 35% 경고·37% 전량 매도 검토**입니다. [결정 및 안전 운영 기록](docs/btc/GPT_PRIMARY_POLICY_2026-11-22.md)과 독립 계산 모듈 `btc_gpt_primary_policy.py`를 참고하세요. 기존 Telegram 운영 workflow는 실행·예약이 제거된 수동 no-op으로 변경되었습니다. 기존 `btc_clock_hybrid_*` 및 `btc_fixed_*` 코드는 *이전 3+3회 규칙의 역사적 구현*이며 활성 거래 신호 엔진이 아닙니다. ChatGPT 작업과 GitHub 원장 사이의 자동 동기화는 구현되지 않았습니다.

웹 주소: https://9959930-code.github.io/quant-guardian/

## 현재 운영 범위

### 1. BTC GPT-primary (현재 정책) / 구 고정 6회 clock-hybrid (참고용)

아래는 **2026-11-22 이전 Telegram 방식의 역사적 설정**이며 현재 매매 지침이 아닙니다.

```text
전략 버전: btc-fixed-six-clock-hybrid-1.1
거래소: Upbit KRW-BTC 현물
시작예산: 10,000,000원
62.5%: 저점 관찰 알림만
65%: 3주 분할매수, 목표 1/3 → 2/3 → 1
다음 epoch 35%: 고점 위험경고만
36%·37%·38%: 목표 2/3 → 1/3 → 0 분할매도
공식 주문 판단: 월요일 09:17 KST
잔고동기화: Telegram 수동 입력
자동주문: 없음
```

주문안이 체결되지 않은 채 남으면 24시간에 현재가 재계산, 3일에 지연주의, 7일에 주문안 만료를 표시합니다. 만료돼도 전략 단계를 건너뛰지 않으며 현재가 재계산 또는 실제 잔고동기화가 필요합니다.

상세 문서: [`docs/btc/fixed-six`](docs/btc/fixed-six/README.md)

### 2. ISA TIGER 레버리지 적립

```text
기존 보유 유지
- PLUS 글로벌HBM반도체 7주
- KODEX 미국머니마켓액티브 145주
- KODEX 미국나스닥100 70주

신규 전략
- TIGER 미국나스닥100레버리지(합성) 418660
- 초기 신규자금 10,000,000원
- 초기매수 완료 다음 달부터 월 500,000원
- 환율 z점수는 Shadow 경고만
- 자동주문 없음
```

과거에는 Telegram에서 BTC와 ISA 상태·잔고동기화를 구분했습니다. 현재 텔레그램 통합 서비스는 비활성화되었고, ISA까지 ChatGPT로 이전된 것은 아닙니다.

### 3. ETF 연구 웹사이트

기존 미국 ETF 점수·백테스트 웹사이트는 계속 갱신하지만, 이전 XLK·SPYM·SGOV 주식 Telegram은 중단했습니다. 직투 TQQQ는 별도 주문알림 없이 사용자가 장기보유합니다.

## 과거 Telegram 메뉴 (비활성)

```text
BTC 상태              ISA 상태
BTC 잔고 동기화       ISA 잔고 동기화
BTC 추가입금          BTC 시작예산
대기 작업             도움말
```

GitHub Actions Secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

과거 운영 workflow는 `.github/workflows/btc-fixed-six-telegram.yml`이었으며, 2026-11-22에 자동 스케줄과 Telegram 전송을 제거했습니다. 현재 BTC 알림은 ChatGPT Scheduled 작업(매일 09:17 KST)을 사용합니다. 실제 주문은 사용자가 거래소에서 직접 실행하며, GitHub에 BTC 개인 계좌 잔고를 기록하지 않습니다.

## 주요 실행 파일

- `btc_clock_hybrid_portfolio_runner.py`: BTC clock-hybrid와 ISA 통합 Telegram 진입점
- `btc_clock_hybrid_core.py`: BTC 상태 migration, 블록 임계값, 6회 주문 단계
- `btc_clock_hybrid_telegram.py`: BTC 이벤트 문구, 주문안 재계산·만료
- `btc_fixed_advisory.py`: BTC 계좌·추가입금·잔고동기화 기반 기능
- `btc_fixed_telegram_bot.py`: Telegram 입력·확인·재알림 기반 기능
- `portfolio_telegram_bot.py`: BTC·ISA 통합 메뉴와 입력 라우팅
- `isa_leverage_core.py`: ISA 상태·시세·환율 Shadow
- `isa_leverage_advisory.py`: ISA 초기·월간 주문 검토 알림
- `quant_guardian.py`: 미국 ETF 데이터·보조지표·백테스트
- `build_dashboard.py`: 웹 대시보드 생성

## 상태 보존

BTC 상태:

```text
data/state/btc_fixed_state.json
cache prefix: btc-fixed-six-state-
```

ISA 상태:

```text
data/state/isa_leverage_state.json
cache prefix: isa-tiger-leverage-state-
```

기존 BTC schema 4 상태는 자산·단계·pending 작업을 보존해 schema 5로 변환합니다. 알 수 없는 상태 버전은 자산 기록 보호를 위해 자동 초기화하지 않습니다.

## 데이터

BTC:

- Upbit 공개 `KRW-BTC` 현재가
- mempool.space와 Blockstream 블록 높이 교차검증
- 두 공급자 차이가 3블록을 넘으면 새 주문 판단 중단

ISA:

- 국내 ETF 공개 시세
- USD/KRW 52주 로그 z점수 Shadow

ETF 웹:

- Yahoo 일봉 데이터
- 미국 정규장 마감 뒤 한국 오전 자동 갱신

## 로컬 실행

웹 대시보드:

```powershell
python launch_dashboard.py --refresh --no-open
```

아래 실행 명령은 **역사적 Telegram 서비스 로컬 실행 예시**이며 현재 운영용이 아닙니다. 기존 Telegram 시크릿을 재사용하거나 실거래 상태를 무단으로 초기화하지 마세요.

```powershell
python btc_clock_hybrid_portfolio_runner.py
```

테스트:

```powershell
python -m unittest discover -s tests -v
```

## 주의

- 모든 주문은 수동입니다.
- 알림의 주문금액은 참고가격과 저장된 잔고를 이용한 검토값입니다.
- 실제 체결 후 반드시 잔고를 동기화해야 합니다.
- 레버리지 ETF와 BTC는 큰 손실이 가능하며 과거 백테스트는 미래 수익을 보장하지 않습니다.
