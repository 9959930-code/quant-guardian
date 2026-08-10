# BTC 수익률 우선 연구 v0.7

이 폴더는 v0.6보다 높은 수익률을 목표로 하되 사용자 위험한도인 MDD -50%를 전체기간·Upbit 중첩기간·완료 반감기 사이클에 모두 적용하는 연구를 기록한다.

- [연구계획](./BTC_RETURN_FIRST_RESEARCH_PLAN_v0.7.md)
- 연구 실행 코드: `btc_return_research.py`
- 후보·신호 모듈: `btc_return_models.py`
- 평가 모듈: `btc_return_eval.py`
- 테스트: `tests/test_btc_return_research.py`
- 동적 결과: `output/btc_return_v07_report.md`

```powershell
python btc_return_research.py --refresh --initial-capital-krw 10000000
python -m unittest discover -s tests -v
```

현재 결과는 연구·Shadow 전용이며 웹·텔레그램·자동주문과 연결하지 않는다.
