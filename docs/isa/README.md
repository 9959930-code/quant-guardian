# ISA TIGER Telegram

운용 기준:

- 기존 보유 유지: PLUS 글로벌HBM반도체 7주, KODEX 미국머니마켓액티브 145주, KODEX 미국나스닥100 70주
- 신규 상품: TIGER 미국나스닥100레버리지(합성), 종목코드 418660
- 초기 신규자금 10,000,000원
- 초기 체결을 반영한 다음 달부터 월 500,000원 적립 알림
- 기존 ETF 매도 없음
- 자동주문 없음

## Telegram 메뉴

BTC와 ISA는 같은 봇과 채팅을 사용하지만 메뉴를 명확히 분리한다.

- `₿ BTC 상태`
- `🔄 BTC 잔고 동기화`
- `📈 ISA 상태`
- `🔄 ISA 잔고 동기화`

ISA 잔고 동기화는 다음 값을 순서대로 입력한다.

1. 현재 TIGER 총수량
2. TIGER 누적투입원금
3. ISA 누적 납입원금 — 모르면 `-`를 입력해 기존 값 유지
4. 초기 1,000만원 매수가 아직 미완료라면 `초기매수 완료` 또는 `잔고만 갱신` 선택

초기매수 완료를 선택하면 다음 달부터 월 500,000원 주문 검토 알림이 활성화된다. 모든 값은 주문 증분이 아니라 현재 계좌의 절대값을 입력한다.

## 실행 구조

`BTC + ISA Portfolio Telegram` workflow가 Telegram 입력을 한 번만 읽고 BTC와 ISA 상태파일을 각각 갱신한다. 기존의 별도 ISA 발신 workflow는 제거했다.

- BTC 상태파일: `data/state/btc_fixed_state.json`
- ISA 상태파일: `data/state/isa_leverage_state.json`
- BTC와 ISA cache는 각각 기존 prefix를 유지한다.
- 자동주문 API는 사용하지 않는다.

ISA의 정기 조건은 한국 평일 09:17 KST 부근에 확인한다. 초기 주문안은 한 번, 월간 주문안은 월 1회, 환율 Shadow 상태는 구간이 바뀔 때만 보낸다.

기존 `Build and Deploy Quant Guardian` workflow는 사이트만 갱신하며 이전 지수 타이밍 Telegram은 발송하지 않는다.
