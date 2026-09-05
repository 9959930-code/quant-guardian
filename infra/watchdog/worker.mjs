import { DurableObject } from 'cloudflare:workers';
import { tick } from './policy.mjs';

export class WatchdogState extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
    this.tail = Promise.resolve();
  }
  async fetch(request) {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/tick') {
      return new Response('Not found', {status:404});
    }
    // Serialize overlapping cron requests in one strongly consistent Durable Object.
    const task = this.tail.then(() => tick(this.env, this.ctx.storage));
    this.tail = task.catch(() => {});
    return Response.json(await task);
  }
}

export default {
  async scheduled(controller, env, ctx) {
    if (env.ENABLED !== 'true') return;
    const monitor = env.WATCHDOG_STATE.get(env.WATCHDOG_STATE.idFromName('quant-guardian-main'));
    ctx.waitUntil(monitor.fetch('https://watchdog.internal/tick', {method:'POST'}).then(response => {
      if (!response.ok) throw new Error('Watchdog tick failed');
    }));
  },
  fetch() {
    // No unauthenticated public endpoint can trigger GitHub or read monitor state.
    return new Response('Not found', {status:404});
  },
};
