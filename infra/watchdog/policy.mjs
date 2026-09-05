// External monitor only. It never reads, changes, or initializes BTC/ISA account state.
export const REPO = '9959930-code/quant-guardian';
export const WORKFLOW = 'btc-fixed-six-telegram.yml';
const API = `https://api.github.com/repos/${REPO}/actions`;
const MINUTE = 60_000;
const ALLOWED_EVENTS = new Set(['schedule', 'push', 'workflow_dispatch']);
const ACTIVE = new Set(['queued', 'in_progress', 'waiting', 'requested', 'pending']);
const PATH = `.github/workflows/${WORKFLOW}`;
const millis = value => value ? Date.parse(value) : NaN;
const elapsed = (now, value) => Number.isFinite(millis(value)) ? now - millis(value) : Infinity;
const iso = value => new Date(value).toISOString();

function allowed(run) {
  return run.head_branch === 'main' && ALLOWED_EVENTS.has(run.event) && run.path === PATH;
}

async function request(fetcher, url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10_000);
  try {
    return await fetcher(url, { ...options, signal: controller.signal, redirect: 'error' });
  } catch {
    // In particular, never print Telegram's secret-bearing request URL.
    throw new Error('External request failed or timed out');
  } finally { clearTimeout(timer); }
}

async function githubJSON(env, fetcher, url) {
  const response = await request(fetcher, url, {headers: {
    Authorization: `Bearer ${env.GH_ACTIONS_TOKEN}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'quant-guardian-external-watchdog',
  }});
  if (!response.ok) throw new Error(`GitHub read HTTP ${response.status}`);
  return response.json();
}

export async function inspect(env, now, fetcher) {
  let active = false;
  for (let page = 1; page <= 3; page++) {
    const body = await githubJSON(env, fetcher,
      `${API}/workflows/${WORKFLOW}/runs?branch=main&per_page=100&page=${page}`);
    if (!Array.isArray(body.workflow_runs)) throw new Error('Invalid workflow list');
    const runs = body.workflow_runs.filter(allowed);
    active ||= runs.some(run => ACTIVE.has(run.status) && elapsed(now, run.created_at) < 45 * MINUTE);
    // Only real, completed production runs count. PR and unrelated CI successes do not.
    for (const run of runs.filter(r => r.status === 'completed' && r.conclusion === 'success').slice(0, 3)) {
      const jobs = await githubJSON(env, fetcher, `${API}/runs/${run.id}/jobs?per_page=100`);
      const service = (jobs.jobs || []).find(j => j.name === 'service' && j.status === 'completed' && j.conclusion === 'success');
      const proof = service?.steps?.find(s => s.name === 'Check data health after state persistence' && s.conclusion === 'success');
      const finished = proof?.completed_at || service?.completed_at;
      // Old pre-hardening or skipped jobs are not a health attestation.
      if (proof && Number.isFinite(millis(finished)) && millis(finished) <= now) {
        return { lastSuccessAt: finished, lastSuccessRun: run.id, active };
      }
    }
    if (body.workflow_runs.length < 100) break;
  }
  return {lastSuccessAt: null, lastSuccessRun: null, active};
}

async function telegram(env, fetcher, text) {
  const response = await request(fetcher,
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true}),
    });
  if (!response.ok) throw new Error(`Telegram HTTP ${response.status}`);
  const body = await response.json();
  if (body.ok !== true) throw new Error('Telegram rejected message');
}

export async function tick(env, storage, now = Date.now(), fetcher = fetch) {
  if (env.ENABLED !== 'true') return {enabled:false};
  for (const key of ['GH_ACTIONS_TOKEN','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID']) {
    if (!env[key]) throw new Error(`Missing secret: ${key}`);
  }
  const state = await storage.get('monitor') || {schema:1};
  if (state.schema !== 1) throw new Error('Unknown watchdog state version; refusing reset');
  if (elapsed(now, state.lastPollAt) < MINUTE) return {enabled:true, duplicateTick:true};
  state.lastPollAt = iso(now);
  await storage.put('monitor', state);
  const result = {enabled:true, dispatchAccepted:false, alertSent:false, recoverySent:false};

  async function notify(text, recovery = false) {
    if (elapsed(now, state.lastAlertAttemptAt) < 15 * MINUTE) return false;
    state.lastAlertAttemptAt = iso(now);
    await storage.put('monitor', state);
    await telegram(env, fetcher, text);
    state.lastAlertAt = iso(now);
    state.incidentOpen = !recovery;
    await storage.put('monitor', state);
    return true;
  }

  let health;
  try { health = await inspect(env, now, fetcher); }
  catch (error) {
    result.githubStatus = 'unknown';
    state.lastReadErrorAt = iso(now);
    if (!state.incidentOpen || elapsed(now, state.lastAlertAt) >= 12 * 60 * MINUTE) {
      result.alertSent = await notify('[Quant Guardian 외부 감시 · 조회 실패]\nGitHub 실행 상태를 확인할 수 없습니다. 복구 성공으로 간주하지 않습니다.\n권한·네트워크를 확인하세요. 자동주문과 상태 초기화는 없습니다.');
    }
    await storage.put('monitor', state);
    return result; // Never dispatch blindly while run/queue status is unknown.
  }
  if (!health.lastSuccessAt) {
    result.githubStatus = 'unverified';
    result.ageMinutes = null;
    if (!state.incidentOpen || elapsed(now, state.lastAlertAt) >= 12 * 60 * MINUTE) {
      result.alertSent = await notify('[Quant Guardian 외부 감시 · 검증 대기]\n안전성 강화 버전의 정상 main 운영 실행을 찾지 못했습니다. 배포·권한·실행 기록을 확인하세요.\n최초 정상 실행을 검증하기 전에는 복구 호출을 자동으로 만들지 않습니다.');
    }
    await storage.put('monitor', state);
    return result;
  }
  state.lastSuccessAt = health.lastSuccessAt;
  state.lastSuccessRun = health.lastSuccessRun;
  const age = elapsed(now, health.lastSuccessAt);
  result.ageMinutes = Number.isFinite(age) ? Math.floor(age / MINUTE) : null;
  result.activeRun = health.active;
  if (age <= 30 * MINUTE) {
    result.githubStatus = 'healthy';
    if (state.incidentOpen) {
      result.recoverySent = await notify(
        `[Quant Guardian 외부 감시 · 복구 확인]\n실제 main 운영 run ${health.lastSuccessRun}의 상태 저장·데이터 점검 성공을 확인했습니다.\n확인시각: ${health.lastSuccessAt}\n자동주문 없음.`, true);
    }
  } else {
    result.githubStatus = 'stale';
    if (env.ENABLE_RECOVERY === 'true' && !health.active && elapsed(now, state.lastDispatchAttemptAt) >= 30 * MINUTE) {
      // Persist the attempt before the request: a timeout must not cause a dispatch storm.
      state.lastDispatchAttemptAt = iso(now);
      await storage.put('monitor', state);
      try {
        const response = await request(fetcher, `${API}/workflows/${WORKFLOW}/dispatches`, {
          method:'POST', headers: {
            Authorization:`Bearer ${env.GH_ACTIONS_TOKEN}`,
            Accept:'application/vnd.github+json', 'Content-Type':'application/json',
            'X-GitHub-Api-Version':'2022-11-28', 'User-Agent':'quant-guardian-external-watchdog',
          },
          body:JSON.stringify({ref:'main', inputs: {
            service_only:true, trigger_source:'cloudflare_watchdog',
            force_status:false, force_isa_status:false, reset_state:false, reset_isa_state:false,
          }}),
        });
        result.dispatchAccepted = response.ok;
        state.lastDispatchStatus = response.status;
      } catch { state.lastDispatchStatus = 'unknown'; }
      // HTTP acceptance is NOT proof that a workflow actually ran or succeeded.
    }
    if (age >= 90 * MINUTE && (!state.incidentOpen || elapsed(now, state.lastAlertAt) >= 12 * 60 * MINUTE)) {
      result.alertSent = await notify(
        `[Quant Guardian 외부 감시 · 실행 지연]\n마지막 검증된 정상 실행 후: ${result.ageMinutes ?? '확인 불가'}분\n복구 실행 접수: ${result.dispatchAccepted ? '이번 점검에서 접수됨(실행 성공 아님)' : '기존 요청·대기 작업 또는 설정 확인 필요'}\n외부 감시에서 직접 보낸 경고입니다. 투자 주문·계좌 초기화는 없습니다.`);
    }
  }
  await storage.put('monitor', state);
  return result;
}
