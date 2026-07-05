# Quant Guardian QG-Core

무료 가격 데이터로 매일 갱신되는 개인용 퀀트 투자 대시보드입니다. 자동주문은 하지 않습니다. 핵심 목적은 “지금 성장주 노출을 키울 때인지, 줄일 때인지”를 확인하고, SPY/QQQ 단순 보유와 비교하며 전략을 검증하는 것입니다.

## 핵심 전략

QG-Core는 세 단계로 판단합니다.

1. 시장국면: SPY/QQQ 200일선, 6개월 수익률, VIX로 공격/중립/방어를 판정합니다.
2. 성장 ETF 코어: QQQ, SPYG, XLK, SMH를 12-1개월/6개월/3개월 모멘텀과 추세로 비교합니다.
3. 대형주 위성: 대형주 후보 중 상위 5개 내외만 전체 비중 25% 이하로 사용합니다.

기본 비중은 다음과 같습니다.

```text
공격: 성장 ETF 60%, 대형주 위성 25%, SGOV 15%
중립: 성장 ETF 35%, 대형주 위성 10%, SGOV/GLD/TLT 55%
방어: SGOV 80%, GLD 10%, TLT 10%
```

QQQ 신호가 나올 때 실제 장기 매수 후보로는 QQQM도 함께 봅니다. 백테스트는 긴 역사를 위해 QQQ와 SHY를 사용하고, 실제 대기자금 후보는 SGOV를 우선 표시합니다.

## 웹에서 보는 법

GitHub Pages 주소:

```text
https://9959930-code.github.io/quant-guardian/
```

상단의 `오늘의 행동`을 먼저 봅니다.

- 성장 노출 유지 / 분할편입 검토: 공격 국면입니다.
- 비중 조절 / 신규매수 신중: 중립 국면입니다.
- 위험 축소 / 현금성 자산 우선: 방어 국면입니다.

그 다음 순서로 보면 됩니다.

1. `시장 모드`: 지금 공격/중립/방어 중 어디인지 확인합니다.
2. `ETF 1순위`: QG-Core가 가장 강하게 보는 성장 ETF 후보입니다.
3. `QG-Core 비중`: ETF, 개별주, SGOV/GLD/TLT 비중 제안입니다.
4. `종목 스캐너`: 개별주 후보를 점수순으로 봅니다.
5. `백테스트`: QG-Core를 SPY, QQQ와 비교합니다.

## 로컬에서 실행

캐시된 데이터로 빠르게 생성:

```bash
python launch_dashboard.py --no-open
```

최신 무료 가격 데이터를 다시 받은 뒤 생성:

```bash
python launch_dashboard.py --refresh --no-open
```

브라우저까지 열기:

```bash
python launch_dashboard.py --refresh
```

명령어로 확인:

```bash
python quant_guardian.py signal
python quant_guardian.py portfolio
python quant_guardian.py backtest
python quant_guardian.py scan --limit 20
```

## GitHub Pages 자동 갱신

`.github/workflows/deploy-pages.yml`이 미국장 거래일 다음 한국 오전에 자동 실행됩니다.

작업 내용:

1. Python 설치
2. 무료 Yahoo 가격 데이터 갱신
3. `output/dashboard.html` 생성
4. GitHub Pages에 배포
5. 텔레그램 시크릿이 있으면 요약 알림 발송

수동 갱신은 GitHub 저장소의 `Actions` 탭에서 `Build and Deploy Quant Guardian` 워크플로를 실행하면 됩니다.

## 텔레그램 알림

GitHub Secrets에 아래 두 값을 넣으면 배포 후 알림이 전송됩니다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

알림에는 시장모드, ETF 1순위, QG-Core 비중, 상위 대형주 후보, SPY/QQQ 대비 백테스트 요약이 들어갑니다.

## 주요 파일

- `quant_guardian.py`: 전략 계산 엔진
- `build_dashboard.py`: HTML 대시보드 생성
- `launch_dashboard.py`: 로컬 실행 진입점
- `telegram_notify.py`: 텔레그램 알림
- `config.toml`: 전략 설정, ETF 후보, 비중 설정
- `.github/workflows/deploy-pages.yml`: GitHub Pages 자동 배포

## 주의

이 프로그램은 투자 추천기가 아니라 의사결정 보조 도구입니다. 백테스트가 좋아도 미래 수익을 보장하지 않습니다. 실제 매매 전에는 계좌 비중, 환율, 세금, 수수료, 실적 발표, 보유 종목 중복을 직접 확인해야 합니다.
