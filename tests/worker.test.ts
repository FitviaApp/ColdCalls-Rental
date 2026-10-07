import { describe, expect, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { app } from '../src/index';

describe('Worker runtime', () => {
  it('reports D1, R2, and both Durable Object bindings', async () => {
    const response = await exports.default.fetch(new Request('https://example.test/health'));
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      status: 'ok', runtime: 'cloudflare-workers',
      checks: { worker: true, d1: true, r2: true, campaign_coordinator: true, realtime_call_session: true },
    });
  });

  it('serves static assets through the ASSETS binding', async () => {
    const response = await env.ASSETS.fetch('https://example.test/css/custom.css');
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('text/css');
    const legacy = await exports.default.fetch(new Request('https://example.test/static/css/custom.css'));
    expect(legacy.status).toBe(200);
    expect(legacy.headers.get('content-type')).toContain('text/css');
  });

  it('allows login without CSRF while protecting application forms', async () => {
    const get = await exports.default.fetch(new Request('https://example.test/auth/login'));
    expect(get.status).toBe(200);
    expect(get.headers.get('cache-control')).toContain('no-store');
    expect(await get.text()).toContain('name="csrf_token"');
    const login = await exports.default.fetch(new Request('https://example.test/auth/login', {
      method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'email=a%40b.com&password=x',
    }));
    expect(login.status).toBe(400);
    const protectedForm = await exports.default.fetch(new Request('https://example.test/dashboard/settings/transfer', {
      method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'transfer_number=%2B15550000001',
    }));
    expect(protectedForm.status).toBe(403);
  });

  it('registers every legacy application method/path plus Worker-only media routes', () => {
    const routes = new Set(app.routes.filter((route) => route.method !== 'ALL').map((route) => `${route.method} ${route.path}`));
    const legacy = [
      'GET /', 'GET /health',
      'GET /auth/login', 'POST /auth/login', 'GET /auth/register', 'POST /auth/register', 'POST /auth/logout',
      'GET /dashboard', 'GET /dashboard/settings', 'POST /dashboard/settings/password', 'POST /dashboard/settings/transfer',
      'POST /dashboard/settings/twilio', 'POST /dashboard/settings/signalwire', 'POST /dashboard/settings/telnyx',
      'POST /dashboard/settings/vonage', 'POST /dashboard/settings/voximplant', 'POST /dashboard/settings/voximplant/reprovision',
      'POST /dashboard/settings/openai', 'POST /dashboard/settings/elevenlabs',
      'GET /campaigns', 'GET /campaigns/create', 'POST /campaigns/create', 'GET /campaigns/:id',
      'POST /campaigns/:id/start', 'POST /campaigns/:id/pause', 'POST /campaigns/:id/cancel',
      'GET /ai-agents', 'GET /ai-agents/create', 'POST /ai-agents/create', 'GET /ai-agents/:id/edit',
      'POST /ai-agents/:id/edit', 'POST /ai-agents/:id/toggle', 'GET /billing', 'POST /billing/verify',
      'GET /assets/caller-ids', 'GET /assets/caller-ids/create', 'POST /assets/caller-ids/create',
      'GET /assets/caller-ids/:id/edit', 'POST /assets/caller-ids/:id/edit', 'POST /assets/caller-ids/:id/voximplant/verify',
      'POST /assets/caller-ids/:id/voximplant/activate', 'POST /assets/caller-ids/:id/delete',
      'GET /assets/audios', 'GET /assets/audios/upload', 'POST /assets/audios/upload',
      'GET /assets/audios/:id/edit', 'POST /assets/audios/:id/edit', 'POST /assets/audios/:id/delete',
      'GET /admin', 'GET /admin/caller-ids', 'GET /admin/caller-ids/create', 'GET /admin/caller-ids/:id/edit',
      'POST /admin/caller-ids/create', 'POST /admin/caller-ids/:id/edit', 'POST /admin/caller-ids/:id/delete',
      'GET /admin/countries', 'GET /admin/countries/create', 'POST /admin/countries/create',
      'GET /admin/countries/:id/edit', 'POST /admin/countries/:id/edit',
      'GET /admin/audios', 'GET /admin/audios/upload', 'GET /admin/audios/:id/edit',
      'POST /admin/audios/upload', 'POST /admin/audios/:id/edit', 'POST /admin/audios/:id/delete',
      'GET /admin/users', 'POST /admin/users/create', 'POST /admin/users/:id/toggle',
      'POST /admin/users/:id/assign-orphan-assets', 'POST /admin/users/:id/add-paid-days',
      'GET /admin/rental-plans', 'POST /admin/rental-plans/create', 'POST /admin/rental-plans/:id/edit',
      'POST /admin/users/:id/delete',
      'GET /api/twiml/:campaignId', 'POST /api/twiml/:campaignId', 'POST /api/twiml/:campaignId/gather',
      'POST /api/twilio/calls/:numberId/answer', 'POST /api/twilio/calls/:numberId/gather',
      'POST /api/twilio/calls/:numberId/status', 'POST /api/twilio/calls/:numberId/transfer-status',
      'GET /api/telnyx/texml/:campaignId', 'POST /api/telnyx/texml/:campaignId', 'POST /api/voximplant/callback',
      'GET /api/ai-runtime/twiml/:numberId', 'POST /api/ai-runtime/twiml/:numberId',
      'POST /api/ai-runtime/twiml/:numberId/gather', 'GET /api/ai-runtime/audio/:numberId/:token',
      'GET /api/ai-realtime/twiml/:numberId', 'POST /api/ai-realtime/twiml/:numberId',
      'GET /api/ai-realtime/session/:numberId/health', 'POST /api/ai-realtime/session/:numberId/start',
      'POST /api/ai-realtime/session/:numberId/stop', 'GET /api/ai-realtime/ws/:numberId',
      'GET /api/ai-realtime/ws/:numberId/:token', 'GET /api/stats', 'GET /api/campaigns/:id/progress',
      'GET /api/campaigns/:id/numbers', 'GET /api/data/caller-ids', 'GET /api/data/countries', 'GET /api/data/audios',
    ];
    expect(legacy).toHaveLength(102);
    for (const route of legacy) expect(routes.has(route), route).toBe(true);
    expect(routes.has('GET /api/media/audio/:audioId')).toBe(true);
    expect(routes.has('GET /assets/audios/:id/content')).toBe(true);
  });
});
