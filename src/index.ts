import { Hono } from 'hono';
import { type AppEnv, csrfProtection, loadUser } from './middleware';
import { authRoutes } from './routes/auth';
import { billingRoutes } from './routes/billing';
import { adminRoutes } from './routes/admin';
import { dashboardRoutes } from './routes/dashboard';
import { assetsRoutes } from './routes/assets';
import { aiAgentRoutes } from './routes/ai_agents';
import { campaignRoutes } from './routes/campaigns';
import { apiRoutes } from './routes/api';
export { CampaignCoordinator } from './durable/campaign_coordinator';
export { RealtimeCallSession } from './durable/realtime_call_session';

export const app = new Hono<AppEnv>();
// Fail closed: without JWT_SECRET, TextEncoder().encode(undefined) would sign/verify every
// token with the literal key "undefined".
app.use('*', async (c, next) => {
  if (!c.env.JWT_SECRET || c.env.JWT_SECRET === 'undefined') {
    return c.json({ detail: 'JWT_SECRET is not configured' }, 500);
  }
  return next();
});
app.use('*', csrfProtection);
app.use('*', loadUser);

// Preserve the FastAPI `/static/*` contract while the binding publishes app/static at its root.
app.get('/static/*', (c) => {
  const source = new URL(c.req.url);
  source.pathname = source.pathname.replace(/^\/static/, '') || '/';
  return c.env.ASSETS.fetch(new Request(source, c.req.raw));
});

app.get('/health', async (c) => {
  const checks: Record<string, boolean> = { worker: true, d1: false, r2: false, campaign_coordinator: false, realtime_call_session: false };
  try { await c.env.DB.prepare('SELECT 1').first(); checks.d1 = true; } catch { /* surfaced below */ }
  try { await c.env.AUDIO_R2.list({ limit: 1 }); checks.r2 = true; } catch { /* surfaced below */ }
  try { c.env.CAMPAIGN_COORDINATORS.idFromName('health'); checks.campaign_coordinator = true; } catch { /* surfaced below */ }
  try { c.env.REALTIME_CALLS.idFromName('health'); checks.realtime_call_session = true; } catch { /* surfaced below */ }
  const ok = Object.values(checks).every(Boolean);
  return c.json({ status: ok ? 'ok' : 'degraded', runtime: 'cloudflare-workers', checks }, ok ? 200 : 503);
});

app.get('/', (c) => c.redirect(c.get('user') ? '/dashboard' : '/auth/login', 302));

app.route('/auth', authRoutes);
app.route('/billing', billingRoutes);
app.route('/admin', adminRoutes);
app.route('/dashboard', dashboardRoutes);
app.route('/assets', assetsRoutes);
app.route('/ai-agents', aiAgentRoutes);
app.route('/campaigns', campaignRoutes);
app.route('/api', apiRoutes);

app.notFound((c) => c.text('Not Found', 404));
app.onError((error, c) => {
  console.error('request_failed', { method: c.req.method, path: new URL(c.req.url).pathname, error: String(error) });
  return c.json({ detail: 'Internal Server Error' }, 500);
});

async function reconcileRunningCampaigns(env: AppEnv['Bindings']): Promise<void> {
  const campaigns = await env.DB.prepare(
    `SELECT DISTINCT c.id FROM campaigns c LEFT JOIN campaign_numbers cn ON cn.campaign_id=c.id
      WHERE c.status='running' OR (cn.cost_reconcile_status='pending' AND cn.cost_next_retry_at<=?)
      ORDER BY c.id LIMIT 100`,
  ).bind(new Date().toISOString()).all<{ id: number }>();
  await Promise.allSettled(campaigns.results.map(async ({ id }) => {
    const stub = env.CAMPAIGN_COORDINATORS.get(env.CAMPAIGN_COORDINATORS.idFromName(String(id))) as unknown as { reconcile(id: number): Promise<void> };
    await stub.reconcile(id);
  }));
}

export default {
  fetch: app.fetch,
  async scheduled(_controller: ScheduledController, env: AppEnv['Bindings'], ctx: ExecutionContext) {
    ctx.waitUntil(reconcileRunningCampaigns(env));
  },
} satisfies ExportedHandler<AppEnv['Bindings']>;
