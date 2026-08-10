# BTC 반감기 필수 오버레이 연구 v0.6

- 기준일: 2026-08-10 KST
- 상태: **과거 연구 완료 / 실전 미승인**
- 실행시장: Upbit `KRW-BTC` 현물 롱온리
- 자동주문: 없음
- 웹·Telegram: 미연결
- 사용자 위험한도: BTC 전용예산 기준 연구 MDD 약 `-50%`
- 연구 추천 후보: `v06_confirmation_vol40_actual`

## 목적

v0.5의 다중기간 모멘텀·변동성 전략을 무반감기 기준선으로 보존하고,
실제 block-height 기반 반감기 국면을 필수 입력으로 사용하는 v0.6 후보를 비교했다.

검증한 질문은 다음과 같다.

1. 반감기 전후 비중 상한이 v0.5보다 위험조정 성과를 높이는가?
2. 이전 목표비중이 아니라 실제 계좌비중을 기준으로 리밸런싱하면 결과가 어떻게 달라지는가?
3. 반감기 전 고정 매수·반감기 후 고정 매도 규칙은 사이클을 바꿔도 유지되는가?
4. 반감기 매집국면의 가치조건과 후기 사이클 과열축소가 도움이 되는가?
5. 사후 최적 매수·매도의 도달 불가능 상한과 실전 규칙 사이의 차이는 얼마나 큰가?

## 핵심 결과

```text
v0.6 연구 추천: v06_confirmation_vol40_actual
전체 CAGR / MDD: 46.74% / -41.95%
Upbit CAGR / MDD: 35.11% / -41.71%
최종자산: 10,000,000원 → 585,195,141원
최근 연구 목표비중: 0%
```

반감기를 필수로 넣은 v0.6은 v0.5보다 낙폭을 조금 줄였지만 수익과 위험조정 성과는 낮았다. 반감기 전후 고정 보유창은 전체기간 결과가 강했으나 완료 사이클별 최적 구간이 달라 실전 승인하지 않았다.

## 문서와 코드

- [사전 연구계획](./BTC_HALVING_OVERLAY_RESEARCH_PLAN_v0.6.md)
- [고정 연구 결과](./BTC_HALVING_RESEARCH_RESULT_2026-08-10.md)
- 실행 코드: 저장소 루트 [`btc_halving_research.py`](../../../btc_halving_research.py)
- 테스트: [`tests/test_btc_halving_research.py`](../../../tests/test_btc_halving_research.py)
- 동적 보고서: GitHub Actions artifact의 `btc_halving_v06_report.md`

## 재현

```powershell
python btc_halving_research.py --refresh --initial-capital-krw 10000000
python -m unittest discover -s tests -v
```

GitHub Actions에서 전체 55개 테스트, 최신 무료 데이터 새로고침, v0.6 연구 실행, 산출물 검증이 통과했다. `output/`의 CSV·JSON·Markdown 12개는 Actions artifact로 30일간 보존한다.

## 안전 상태

```text
반감기 입력: v0.6 배포 후보에 필수
v0.5 무반감기: 비교 기준선으로만 허용
v0.6 연구 후보 고정: 예
실전 전략 승인: 아니오
웹·Telegram 연결: 아니오
자동주문: 금지
30개 확정 일봉 Shadow: 아직 시작하지 않음
```
