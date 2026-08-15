# ISA TIGER Telegram

운용 기준:

- 기존 보유 유지: PLUS 글로벌HBM반도체 7주, KODEX 미국머니마켓액티브 145주, KODEX 미국나스닥100 70주
- 신규 상품: TIGER 미국나스닥100레버리지(합성), 종목코드 418660
- 초기 신규자금 10,000,000원
- 초기 체결을 반영한 다음 달부터 월 500,000원 적립 알림
- 기존 ETF 매도 없음
- 자동주문 없음

알림은 한국 평일 09:17 KST에 조건을 확인한다. 초기 주문안은 한 번, 월간 주문안은 월 1회, 환율 Shadow 상태는 구간이 바뀔 때만 보낸다.

초기 주문을 직접 체결한 뒤 `ISA TIGER Leverage Telegram` workflow를 수동 실행해 실제 TIGER 수량과 누적투입원금을 반영한다. 이후 월 적립 체결 뒤에도 두 값을 절대값으로 갱신한다.

BTC 고정 6회 Telegram은 별도 상태파일을 계속 사용한다. ISA 서비스는 Telegram 메시지만 발송하고 명령이나 버튼 입력을 읽지 않는다.

기존 `Build and Deploy Quant Guardian` workflow는 사이트만 갱신하며 이전 지수 타이밍 Telegram은 더 이상 발송하지 않는다.
