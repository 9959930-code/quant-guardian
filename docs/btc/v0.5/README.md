# BTC 모멘텀·변동성 연구 v0.5

- 기준일: 2026-08-10 KST
- 연구기간: 2016-01-01~2026-08-08
- 초기자금 예시: 10,000,000원 일시납
- 상태: 과거 연구 완료 / 실전 미승인
- 자동주문: 없음

## 문서

- [전략 규칙 설명](./BTC_MOMENTUM_VOLATILITY_STRATEGY_v0.5.md)
- [1,000만 원 연구 결과](./BTC_MOMENTUM_RESEARCH_RESULT_2026-08-10.md)
- 실행 코드: `btc_momentum_research.py`
- 동적 보고서: `output/btc_momentum_v05_report.md`

## 재현

```powershell
python btc_momentum_research.py --refresh
python -m unittest discover -s tests -v
```

`--refresh`를 빼면 최근 정상 캐시로 재현한다. 이 연구는 웹·텔레그램·자동주문과
분리되어 있으며 결과를 실전 매매지시로 사용하지 않는다.
