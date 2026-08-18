# Quant Guardian

미국 ETF 연구 대시보드와 BTC·ISA 수동매매 Telegram 보조도구를 함께 관리하는 저장소입니다. 자동주문과 출금 기능은 없습니다.

웹 주소: https://9959930-code.github.io/quant-guardian/

## 현재 운영 범위

### 1. BTC 고정 6회 clock-hybrid

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

Telegram에서 BTC와 ISA 상태·잔고동기화를 구분합니다.

### 3. ETF 연구 웹사이트

기존 미국 ETF 점수·백테스트 웹사이트는 계속 갱신하지만, 이전 XLK·SPYM·SGOV 주식 Telegram은 중단했습니다. 직투 TQQQ는 별도 주문알림 없이 사용자가 장기보유합니다.

## Telegram 메뉴

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

운영 workflow는 `.github/workflows/btc-fixed-six-telegram.yml`이며 매시 `02·17·32·47분`에 상태를 확인합니다. 실제 주문은 사용자가 거래소·증권사에서 직접 실행합니다.

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

BTC·ISA 통합 Telegram은 GitHub Actions 운영을 기본으로 하며, 로컬에서 실행하려면 Telegram 시크릿 환경변수와 상태파일 경로가 필요합니다.

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
