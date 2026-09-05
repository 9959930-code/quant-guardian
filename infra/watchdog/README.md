# Quant Guardian external watchdog

**상태: 배포 준비 코드. 기본 비활성. 실제 Cloudflare 계정·Cron·비밀값을 생성한 것이 아니다.**

## 재검토된 구조

JavaScript Worker가 15분마다 단일 SQLite Durable Object를 호출한다. 감시 상태·재호출 시각만 저장하며 BTC/ISA 자산 기록을 읽거나 수정하지 않는다. 계좌 원장을 Workers KV로 옮기지 않는다. public HTTP endpoint는 404이며 Telegram 입력 polling도 하지 않는다.

GitHub의 해당 workflow/main만 확인한다. 최근 성공 service의 `Check data health after state persistence` 단계까지 성공한 기록이 필요하다. 이전 버전/PR/다른 CI가 성공해도 정상 서비스로 간주하지 않는다. 최초 검증 성공을 찾지 못하면 자동 재호출 없이 설치 확인 경고만 보낸다.

- 마지막 검증 성공 <=30분: 대기
- >30분, 새 실행/대기 작업 없음: 최대 30분에 한 번 안전한 경량 dispatch
- >=90분: 외부에서 Telegram 경고, 반복은 12시간 제한
- 실제 정상 운영 성공: 복구 확인
- GitHub API 오류: 확인 불가 경고, 맹목적 dispatch 금지
- Telegram 실패/timeout: 성공했다고 기록하지 않음, 시도 냉각시간 적용

Cloudflare나 GitHub 모두 절대적인 정시 실행 또는 무중단을 보장하는 것으로 해석하면 안 된다. 이 구성은 서로 다른 감시 경로를 제공하는 것이다. GitHub 전체 실행 장애는 dispatch로 해결할 수 없지만 외부 경고는 별도 경로로 시도할 수 있다.

## 안전한 설치

1. 검토한 main을 checkout한다. 기존 production workflow에 새 건강상태 검사가 포함된 **실제 main 정상 실행**을 먼저 확인한다.
2. Cloudflare 본인 계정의 비용·사용량 한도를 확인한 후 Wrangler로 로그인한다. Worker/Durable Object 생성 권한만 필요한 계정으로 작업한다.
3. fine-grained GitHub token을 새로 만들고 `9959930-code/quant-guardian` 단일 저장소의 **Actions: read/write**로 제한한다. Contents write·Administration·Secrets 권한은 부여하지 않는다. 토큰 만료·교체일을 별도 관리한다.
4. 다음 명령의 프롬프트에서 각 비밀값을 입력한다. 비밀값을 채팅, 소스, PR, CLI 인자, 로그에 붙여넣지 않는다. 기존 GitHub secret은 읽어 추출하지 않는다. 별도로 보관한 Telegram Bot Token/Chat ID를 사용한다.

```sh
cd infra/watchdog
npx wrangler login
npx wrangler deploy                    # ENABLED=false 유지, 아직 감시하지 않음
npx wrangler secret put GH_ACTIONS_TOKEN
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
```

5. `wrangler.jsonc`에서 `ENABLED`만 문자열 `true`로 바꾸고 deploy한다. `ENABLE_RECOVERY=false`를 유지해 조회·경고만 관찰한다. 최근 main 운영 run이 건강한 것으로 판단되는지 확인한다.
6. 복구 호출의 실제 검증을 할 준비가 됐을 때 `ENABLE_RECOVERY=true`로 바꾸고 deploy한다. 강제로 계좌 상태를 리셋하거나 잘못된 가격을 넣어 테스트하지 않는다.
7. 실제 stale 발생 시 GitHub run에 `event=workflow_dispatch`, 경량 Python 설정, no pip, `trigger_source=cloudflare_watchdog`, 두 reset=false가 기록되는지 확인한다. 완료된 service 건강상태 검사와 Telegram 직접 경고/복구 확인까지 각각 확인한다.

## 테스트와 승인 기준

```sh
node --test worker.test.mjs
```

이 테스트는 모의 HTTP/저장소 단위 테스트다. Cloudflare 원격 배포, 실제 자격증명, Cron 발동, Telegram 수신 검증을 대신하지 않는다.

운영 승인에는 단위 테스트 외에 실제 조회 성공, 검증된 성공 run 식별, 경량 복구 접수 및 **실행 완료**, 정상 상태 보존, 경고/복구 메시지 수신 증거가 필요하다. 단순 HTTP 204로 배포 완료/복구 완료라고 보고하지 않는다.

## 중단·문제 해결

`ENABLED=false`로 변경해 deploy하면 외부 호출을 중단한다. GitHub 원래 cron과 BTC/ISA 상태는 그대로 남는다. 필요하면 GitHub token만 폐기한다. 상태파일 reset은 하지 않는다.

cache 미스·서로 다른 BTC/ISA cache revision으로 운영이 중단되면 반복 dispatch로 고칠 수 없다. 같은 정상 완료 run의 양쪽 상태를 복원하고 잔고를 사용자와 대조해야 한다. 토큰 401/403이면 정상으로 표시하지 않는다. 장시간 전체 실행 장애가 반복되면 전략 runner를 상시 호스팅으로 옮기는 별도 검토가 필요하다.

## 근거

- GitHub schedule: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- Workflow dispatch: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- Durable Object storage: https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/
- Secrets: https://developers.cloudflare.com/workers/configuration/secrets/
