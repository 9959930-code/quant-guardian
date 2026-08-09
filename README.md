# Quant Guardian 지수 타이밍

미국 지수·섹터 ETF의 일봉 데이터를 바탕으로 `신규매수`, `분할매수`, `보유·관찰`, `비중축소`, `매도·대기` 시점을 정리하는 개인용 퀀트 대시보드입니다. 자동주문과 장중 실시간 매매는 하지 않습니다.

웹 주소: https://9959930-code.github.io/quant-guardian/

## 무엇을 계산하나

대상은 개별주가 아니라 아래 지수·섹터 ETF입니다.

- 핵심 지수: SPY(S&P 500), QQQ(나스닥100), SPYG(S&P 500 성장주)
- 섹터 보조: XLK(미국 기술), SMH(반도체)
- 대기·분산: SGOV(초단기 미국 국채), GLD(금), TLT(장기 미국 국채)
- 실제 매수 표시: SPY 신호는 SPYM, QQQ 신호는 QQQM으로 표시

각 ETF를 네 묶음으로 평가합니다.

1. 추세 40점: 20/50/100/200일선, EMA 12/26, MACD, 일목균형표, ADX
2. 모멘텀 25점: 1/3/6/12개월 수익률, 12-1개월 모멘텀, SPY 대비 상대강도
3. 진입 타이밍 20점: RSI, 스토캐스틱, 볼린저밴드, ATR 이격, 20일 돌파, 거래량, OBV
4. 위험 15점: 63일 변동성, 1년 낙폭, ATR 비율, VIX

지표 대부분은 같은 가격에서 파생됩니다. 그래서 지표 개수를 그대로 투표 수로 사용하지 않고 네 묶음마다 점수 상한을 둡니다. 종합점수 80점은 수익 확률 80%라는 뜻이 아닙니다.

## 판단과 목표 비중

SPY와 QQQ의 종합 상태를 이용해 주식형 ETF 목표 비중을 정합니다.

```text
신규매수 검토: 주식형 100%
분할매수: 주식형 90%
보유·관찰: 주식형 75%
비중축소: 주식형 50%
매도·대기: 주식형 0%
```

남는 비중은 SGOV에 두고, GLD와 TLT는 각 ETF 자체가 200일선 위일 때만 일부 사용합니다. 주식형은 상위 두 ETF로 나누며 적어도 하나는 핵심 지수를 포함합니다. 섹터 ETF는 전체 자금의 50%를 넘지 못합니다.

## 웹에서 보는 순서

1. `오늘 할 일`: 매수·보유·축소 중 무엇을 검토할지 확인합니다.
2. `주식형 목표 비중`: 전체 투자금 중 지수 ETF 목표 비중을 봅니다.
3. `ETF별 매수·보유 판단`: 각 ETF의 행동과 실행 조건을 확인합니다.
4. `차트·보조지표`: 이동평균선과 모든 계산 지표를 직접 확인합니다.
5. `목표 비중`: 총 투자 가능 금액을 입력해 목표 평가액으로 바꿉니다.
6. `백테스트`: SPY·QQQ 단순보유와 수익률·낙폭을 함께 비교합니다.

총 투자금은 브라우저의 로컬 저장소에만 보관됩니다. 사이트나 GitHub로 전송되지 않습니다. 현재 보유량은 알 수 없으므로 실제 주문 검토액은 `목표 평가액 - 현재 보유 평가액`으로 직접 계산해야 합니다.

## 데이터와 갱신 시점

- 무료 Yahoo 일봉 데이터 사용
- 미국 정규장 마감 뒤 한국 오전에 GitHub Actions 자동 실행
- 마감 종가 기반이므로 장중 실시간 가격은 반영하지 않음
- 신호는 매일 계산하지만 잦은 매매를 줄이기 위해 주문 판단은 주 1회 이내로 모으는 방식

## BTC 확장 진행 상태

BTC 기능은 **2단계 연구 모드**입니다. 업비트 `KRW-BTC` 확정 일봉, Yahoo `BTC-USD`·USD/KRW, mempool.space·Blockstream 블록 높이, Coin Metrics Community 무료 온체인 이력으로 반감기·가치·추세 상태 머신과 비교 백테스트를 계산합니다. 자동주문은 없고 기존 ETF 웹 화면과 텔레그램에는 아직 연결하지 않았습니다.

최신 무료 데이터를 점검하려면 다음 명령을 사용합니다.

```powershell
python btc_guardian.py data-report --refresh
```

결과는 `output/btc_data_report.json`에 저장됩니다. 매수·보유·축소·매도 규칙은 워크포워드 검증과 모의운영을 통과한 뒤 별도 승인해야 합니다.

BTC 연구 보고서를 최신 데이터로 다시 만들려면 다음 명령을 사용합니다.

```powershell
python btc_research.py run --refresh
```

캐시된 데이터로 재현할 때는 `--refresh`를 빼면 됩니다. 보고서는 `output/btc_research_report.md`에 생성되며 후보·벤치마크·워크포워드·사이클·비용/지연·인접 파라미터 CSV도 함께 저장됩니다.

현재 고정 기준안은 MDD -50% 연구 경계를 통과하지 못했습니다. 약세장 재진입 비중을 제한한 후보는 경계를 통과했지만, 2021~2022 하락을 본 뒤 만든 사후 진단 후보이므로 실전 선택에서 제외되어 있습니다. 웹과 텔레그램에 BTC 매매문구를 추가하기 전에 extended USD 검증, 새 데이터 전향검증, 사용자 승인과 30개 확정 일봉 Shadow가 필요합니다.

## 텔레그램 알림

GitHub 저장소의 `Settings -> Secrets and variables -> Actions`에 아래 두 시크릿이 있으면 알림을 보냅니다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

원화 목표금액도 받고 싶을 때만 아래 시크릿을 추가합니다.

```text
PORTFOLIO_VALUE_KRW
```

값에는 쉼표 없이 총 투자 가능 금액을 입력합니다. 예: `10000000`. 이 값이 없으면 텔레그램에는 목표 비중만 표시됩니다.

알림은 `오늘 할 일 -> 우선 확인 ETF -> 실행 시점 -> 판단 근거 -> 목표 비중 -> 보유·매도 기준` 순서로 전부 한글로 표시됩니다.

## 로컬 실행

최신 데이터를 받은 뒤 대시보드 생성:

```powershell
python launch_dashboard.py --refresh --no-open
```

브라우저까지 열기:

```powershell
python launch_dashboard.py --refresh
```

캐시된 데이터로 다시 생성:

```powershell
python launch_dashboard.py --no-open
```

명령줄 확인:

```powershell
python quant_guardian.py signal
python quant_guardian.py portfolio
python quant_guardian.py backtest
python quant_guardian.py report
```

테스트:

```powershell
python -m unittest discover -s tests
```

## 백테스트 해석

백테스트는 월말까지 확인된 데이터로 신호를 만들고 다음 달 수익률에 적용합니다. 매매 회전율에 설정된 거래비용도 차감합니다. 현재 ETF 목록을 과거에도 알고 있었다고 가정하는 상품 선택 편향, SHY와 SGOV의 차이, 세금, 환율, 실제 체결 가격은 완전히 반영하지 못합니다.

과거 결과가 좋더라도 미래 수익을 보장하지 않습니다. 이 프로그램의 목적은 최고점과 최저점을 맞히는 것이 아니라 큰 하락 추세에서 노출을 줄이고, 상승 추세에서 규칙적으로 참여하도록 돕는 것입니다.

## 주요 파일

- `quant_guardian.py`: 데이터, 보조지표, 판단, 목표 비중, 백테스트
- `btc_guardian.py`: BTC 무료 데이터 품질과 반감기 진행률 검증
- `btc_research.py`: BTC feature, 상태 머신, 가상체결, 후보·강건성 연구 보고서
- `build_dashboard.py`: 한글 웹 대시보드와 `daily.json` 생성
- `telegram_notify.py`: 한글 텔레그램 알림
- `config.toml`: 대상 ETF, 비중, 위험 제한
- `.github/workflows/deploy-pages.yml`: 자동 갱신, GitHub Pages 배포, 알림
