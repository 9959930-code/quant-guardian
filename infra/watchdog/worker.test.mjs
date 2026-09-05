import test from 'node:test';
import assert from 'node:assert/strict';
import {tick, WORKFLOW, REPO} from './policy.mjs';

const now = Date.parse('2026-09-05T03:00:00Z');
const ago = n => new Date(now - n*60_000).toISOString();
const env = {ENABLED:'true', ENABLE_RECOVERY:'true', GH_ACTIONS_TOKEN:'test-only', TELEGRAM_BOT_TOKEN:'test-only', TELEGRAM_CHAT_ID:'test-only'};
const memory = () => ({value:null, async get(){return structuredClone(this.value);}, async put(k,v){this.value=structuredClone(v);}});
const goodRun = (id=1, event='schedule') => ({id,event, head_branch:'main',path:`.github/workflows/${WORKFLOW}`,status:'completed',conclusion:'success',created_at:ago(180)});
function fixtures({age=151, active=false, failRead=false, failDispatch=false, runs=null, proof=true}={}) {
  const calls=[];
  const fetcher=async (url, options={}) => {
    calls.push({url,options});
    if(url.includes('api.telegram.org')) return Response.json({ok:true});
    if(url.endsWith('/dispatches')) return new Response(null,{status:failDispatch?403:204});
    if(failRead) return new Response(null,{status:403});
    if(url.includes('/jobs?')) return Response.json({jobs:[{name:'service',status:'completed',conclusion:'success',completed_at:ago(age),steps:proof?[{name:'Check data health after state persistence',conclusion:'success',completed_at:ago(age)}]:[]}]});
    const list = runs || [goodRun()];
    if(active) list.unshift({...goodRun(2),status:'queued',conclusion:null,created_at:ago(5)});
    return Response.json({workflow_runs:list});
  };
  return {calls,fetcher};
}

test('disabled default has no external effects', async()=>{
  const m=memory();const {calls,fetcher}=fixtures();
  assert.equal((await tick({...env,ENABLED:'false'},m,now,fetcher)).enabled,false);
  assert.equal(calls.length,0);assert.equal(m.value,null);
});
test('recent validated success does not dispatch',async()=>{
  const {calls,fetcher}=fixtures({age:10});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.githubStatus,'healthy');assert.equal(r.dispatchAccepted,false);
  assert(!calls.some(c=>c.url.includes('api.telegram.org')));
});
test('stale main run requests lightweight non-resetting recovery and alerts',async()=>{
  const {calls,fetcher}=fixtures();const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.dispatchAccepted,true);assert.equal(r.alertSent,true);
  const request=calls.find(c=>c.url.endsWith('/dispatches'));
  assert(request.url.includes(REPO));
  const body=JSON.parse(request.options.body);
  assert.deepEqual(body,{ref:'main',inputs:{service_only:true,trigger_source:'cloudflare_watchdog',force_status:false,force_isa_status:false,reset_state:false,reset_isa_state:false}});
});
test('young queued production job prevents dispatch, not an overdue warning',async()=>{
  const {fetcher}=fixtures({active:true});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.activeRun,true);assert.equal(r.dispatchAccepted,false);assert.equal(r.alertSent,true);
});
test('PR successes cannot hide a stalled production service',async()=>{
  const {fetcher}=fixtures({runs:[goodRun(9,'pull_request')]});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.githubStatus,'unverified');assert.equal(r.ageMinutes,null);assert.equal(r.dispatchAccepted,false);
});
test('unrelated workflow successes cannot hide a stalled service',async()=>{
  const {fetcher}=fixtures({runs:[{...goodRun(9),path:'.github/workflows/pull-request-ci.yml'}]});
  assert.equal((await tick(env,memory(),now,fetcher)).ageMinutes,null);
});
test('legacy success without health proof is not claimed healthy',async()=>{
  const {fetcher}=fixtures({age:5,proof:false});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.githubStatus,'unverified');assert.equal(r.dispatchAccepted,false);
});
test('same tick and 15-minute repeats cannot cause a dispatch storm',async()=>{
  const m=memory();const {calls,fetcher}=fixtures();await tick(env,m,now,fetcher);
  assert.equal((await tick(env,m,now+1000,fetcher)).duplicateTick,true);
  const later=await tick(env,m,now+15*60_000,fetcher);
  assert.equal(later.dispatchAccepted,false);assert.equal(later.alertSent,false);
  assert.equal(calls.filter(c=>c.url.endsWith('/dispatches')).length,1);
});
test('API error reports unknown and does not dispatch blindly',async()=>{
  const {calls,fetcher}=fixtures({failRead:true});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.githubStatus,'unknown');assert.equal(r.alertSent,true);
  assert(!calls.some(c=>c.url.endsWith('/dispatches')));
});
test('dispatch rejection is not reported as successful execution',async()=>{
  const {fetcher}=fixtures({failDispatch:true});const r=await tick(env,memory(),now,fetcher);
  assert.equal(r.dispatchAccepted,false);assert.equal(r.githubStatus,'stale');
});
test('observe-only mode does not request a workflow',async()=>{
  const {fetcher}=fixtures();const r=await tick({...env,ENABLE_RECOVERY:'false'},memory(),now,fetcher);
  assert.equal(r.dispatchAccepted,false);assert.equal(r.alertSent,true);
});
test('recovery requires a later actually validated successful job',async()=>{
  const m=memory();await tick(env,m,now,fixtures().fetcher);
  const r=await tick(env,m,now+20*60_000,fixtures({age:1}).fetcher);
  assert.equal(r.recoverySent,true);assert.equal(m.value.incidentOpen,false);
});
test('unknown durable state version is rejected without reset',async()=>{
  const m=memory();m.value={schema:99};
  await assert.rejects(tick(env,m,now,fixtures().fetcher),/Unknown watchdog/);
  assert.equal(m.value.schema,99);
});
