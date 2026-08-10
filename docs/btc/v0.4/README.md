# BTC 반감기·40주 추세 연구 v0.4

- 기준일: 2026-08-10 KST
- 연구기간: 2016-01-01~2026-08-08
- 초기자금 예시: 10,000,000원 일시납
- 상태: 과거 연구 완료 / 실전 미승인
- 자동주문: 없음

기존 v0.3 기준안을 규칙 변경 없이 2016년까지 연장한 직접 비교와 새 v0.4
후보를 함께 기록한다.

## 문서

- [연구 결과](./BTC_CYCLE_RESEARCH_RESULT_2026-08-10.md)
- 실행 코드: `btc_cycle_research.py`
- 동적 보고서: `output/btc_cycle_v04_report.md`

## 재현

```powershell
python btc_cycle_research.py --refresh
python -m unittest discover -s tests -v
```

`--refresh`를 빼면 최근 정상 캐시로 재현한다. 블록 높이 안전 게이트가
오래된 캐시를 거부하면 최신 데이터 갱신이 필요하다.
