import { Hono } from 'hono';
import type { Context } from 'hono';
import { hashPassword } from '../auth';
import { render } from '../render';
import { requireAdmin } from '../middleware';
import type { AppEnv, UserRow } from '../middleware';
import { addPaidDays, getActiveRental } from '../services/rental';
import type { RentalPlanRow } from '../services/rental';

export const adminRoutes = new Hono<AppEnv>();

adminRoutes.use('*', requireAdmin);

type RenderCtx = Parameters<typeof render>[1];

interface CountryRow {
  id: number;
  code: string;
  name: string;
  price_per_minute: number;
  is_active: number;
}

const page = (path: string, extra: Record<string, unknown>): RenderCtx =>
  ({ request: { url: { path } }, ...extra }) as unknown as RenderCtx;

async function count(db: D1Database, sql: string): Promise<number> {
  const row = await db.prepare(sql).first<{ c: number }>();
  return row ? row.c : 0;
}

function pathId(c: Context<AppEnv>): number | Response {
  const id = Number(c.req.param('id'));
  return Number.isInteger(id) ? id : c.json({ detail: 'Invalid id' }, 422);
}

function truthy(v: unknown): boolean {
  const s = String(v ?? '').toLowerCase();
  return s === 'true' || s === '1' || s === 'yes';
}

function pyDate(iso: string) {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  const fields: Record<string, string> = {
    Y: String(d.getUTCFullYear()),
    m: p(d.getUTCMonth() + 1),
    d: p(d.getUTCDate()),
    H: p(d.getUTCHours()),
    M: p(d.getUTCMinutes()),
    S: p(d.getUTCSeconds()),
  };
  return {
    strftime: (fmt: string) => fmt.replace(/%(.)/g, (m: string, ch: string) => fields[ch] ?? m),
  };
}

function noticeUrl(base: string, key: string, msg: string): string {
  return `${base}?${key}=${encodeURIComponent(msg)}`;
}

adminRoutes.get('/', async (c) => {
  const db = c.env.DB;
  const [totalUsers, totalCampaigns, totalCountries, pendingRentals, activePlans, orphanCallerIds, orphanAudios] =
    await Promise.all([
      count(db, 'SELECT COUNT(*) AS c FROM users WHERE is_admin = 0'),
      count(db, 'SELECT COUNT(*) AS c FROM campaigns'),
      count(db, 'SELECT COUNT(*) AS c FROM countries WHERE is_active = 1'),
      count(db, "SELECT COUNT(*) AS c FROM rental_payments WHERE status = 'pending'"),
      count(db, 'SELECT COUNT(*) AS c FROM rental_plans WHERE is_active = 1'),
      count(db, 'SELECT COUNT(*) AS c FROM caller_ids WHERE user_id IS NULL'),
      count(db, 'SELECT COUNT(*) AS c FROM audios WHERE user_id IS NULL'),
    ]);
  return c.html(
    render('admin/dashboard.html', page(c.req.path, {
      user: c.get('user'),
      stats: {
        total_users: totalUsers,
        total_campaigns: totalCampaigns,
        total_countries: totalCountries,
        pending_rental_payments: pendingRentals,
        active_rental_plans: activePlans,
        orphan_caller_ids: orphanCallerIds,
        orphan_audios: orphanAudios,
      },
      error: c.req.query('error') ?? null,
    })),
  );
});

const callerIdsTarget = noticeUrl(
  '/admin',
  'error',
  'Caller IDs are user-owned now. Ask users to manage them in Assets.',
);
for (const p of ['/caller-ids', '/caller-ids/create', '/caller-ids/:id/edit']) {
  adminRoutes.get(p, (c) => c.redirect(callerIdsTarget, 302));
}
for (const p of ['/caller-ids/create', '/caller-ids/:id/edit', '/caller-ids/:id/delete']) {
  adminRoutes.post(p, (c) => c.redirect(callerIdsTarget, 302));
}

const audiosTarget = noticeUrl(
  '/admin',
  'error',
  'Audios are user-owned now. Ask users to manage them in Assets.',
);
for (const p of ['/audios', '/audios/upload', '/audios/:id/edit']) {
  adminRoutes.get(p, (c) => c.redirect(audiosTarget, 302));
}
for (const p of ['/audios/upload', '/audios/:id/edit', '/audios/:id/delete']) {
  adminRoutes.post(p, (c) => c.redirect(audiosTarget, 302));
}

adminRoutes.get('/countries', async (c) => {
  const result = await c.env.DB.prepare('SELECT * FROM countries ORDER BY code').all<CountryRow>();
  return c.html(
    render('admin/countries/list.html', page(c.req.path, {
      user: c.get('user'),
      countries: result.results.length > 0 ? result.results : null,
    })),
  );
});

adminRoutes.get('/countries/create', (c) =>
  c.html(
    render('admin/countries/create.html', page(c.req.path, {
      user: c.get('user'),
      error: null,
    })),
  ),
);

adminRoutes.post('/countries/create', async (c) => {
  const body = await c.req.parseBody();
  const code = body.code;
  const name = body.name;
  const price = Number(body.price_per_minute);
  if (code === undefined || name === undefined || !Number.isFinite(price)) {
    return c.json({ detail: 'Invalid form data' }, 422);
  }
  const upper = String(code).toUpperCase();
  const existing = await c.env.DB.prepare('SELECT id FROM countries WHERE code = ?').bind(upper).first();
  if (existing) return c.json({ detail: 'Country code already exists' }, 400);
  await c.env.DB.prepare('INSERT INTO countries (code, name, price_per_minute) VALUES (?, ?, ?)')
    .bind(upper, String(name), price)
    .run();
  return c.redirect('/admin/countries', 302);
});

adminRoutes.get('/countries/:id/edit', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const country = await c.env.DB.prepare('SELECT * FROM countries WHERE id = ?').bind(id).first<CountryRow>();
  if (!country) return c.json({ detail: 'Country not found' }, 404);
  return c.html(
    render('admin/countries/edit.html', page(c.req.path, {
      user: c.get('user'),
      country,
      error: null,
    })),
  );
});

adminRoutes.post('/countries/:id/edit', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const country = await c.env.DB.prepare('SELECT * FROM countries WHERE id = ?').bind(id).first<CountryRow>();
  if (!country) return c.json({ detail: 'Country not found' }, 404);
  const body = await c.req.parseBody();
  const code = body.code;
  const name = body.name;
  const price = Number(body.price_per_minute);
  if (code === undefined || name === undefined || !Number.isFinite(price)) {
    return c.json({ detail: 'Invalid form data' }, 422);
  }
  const upper = String(code).toUpperCase();
  const clash = await c.env.DB.prepare('SELECT id FROM countries WHERE code = ? AND id <> ?').bind(upper, id).first();
  if (clash) return c.json({ detail: 'Country code already exists' }, 400);
  await c.env.DB.prepare('UPDATE countries SET code = ?, name = ?, price_per_minute = ?, is_active = ? WHERE id = ?')
    .bind(upper, String(name), price, truthy(body.is_active) ? 1 : 0, id)
    .run();
  return c.redirect('/admin/countries', 302);
});

adminRoutes.get('/users', async (c) => {
  const db = c.env.DB;
  const result = await db.prepare('SELECT * FROM users ORDER BY created_at DESC').all<UserRow>();
  const rental_status: Record<number, { expires_at: ReturnType<typeof pyDate> } | null> = {};
  for (const u of result.results) {
    const rental = await getActiveRental(db, u.id);
    rental_status[u.id] = rental ? { expires_at: pyDate(rental.expires_at) } : null;
  }
  const users = result.results.map((u) => ({ ...u, created_at: pyDate(u.created_at) }));
  return c.html(
    render('admin/users.html', page(c.req.path, {
      user: c.get('user'),
      users,
      rental_status,
      created: truthy(c.req.query('created')),
      deleted: truthy(c.req.query('deleted')),
      notice: c.req.query('notice') ?? null,
      error: c.req.query('error') ?? null,
    })),
  );
});

adminRoutes.post('/users/create', async (c) => {
  const body = await c.req.parseBody();
  const email = String(body.email ?? '').toLowerCase().trim();
  const password = String(body.password ?? '');
  const passwordConfirm = String(body.password_confirm ?? '');
  const maxUsers = Number(c.env.MAX_USERS ?? 4);
  const err = (msg: string) => c.redirect(noticeUrl('/admin/users', 'error', msg), 302);

  const userCount = await count(c.env.DB, 'SELECT COUNT(*) AS c FROM users WHERE is_admin = 0');
  if (userCount >= maxUsers) return err('Maximum users reached');
  if (password !== passwordConfirm) return err('Passwords do not match');
  if (password.length < 6) return err('Password must be at least 6 characters');

  const existing = await c.env.DB.prepare('SELECT id FROM users WHERE email = ?').bind(email).first();
  if (existing) return err('Email already registered');

  await c.env.DB.prepare('INSERT INTO users (email, password_hash, is_admin, is_active) VALUES (?, ?, 0, 1)')
    .bind(email, hashPassword(password))
    .run();
  return c.redirect('/admin/users?created=true', 302);
});

adminRoutes.post('/users/:id/toggle', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const admin = c.get('user');
  const user = await c.env.DB.prepare('SELECT * FROM users WHERE id = ?').bind(id).first<UserRow>();
  if (!user) return c.json({ detail: 'User not found' }, 404);
  if (user.id === admin.id) return c.json({ detail: 'Cannot disable yourself' }, 400);
  await c.env.DB.prepare('UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?')
    .bind(user.is_active ? 0 : 1, new Date().toISOString(), user.id)
    .run();
  return c.redirect('/admin/users', 302);
});

adminRoutes.post('/users/:id/assign-orphan-assets', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const user = await c.env.DB.prepare('SELECT * FROM users WHERE id = ? AND is_admin = 0')
    .bind(id)
    .first<UserRow>();
  if (!user) return c.json({ detail: 'User not found' }, 404);
  await c.env.DB.batch([
    c.env.DB.prepare('UPDATE caller_ids SET user_id = ? WHERE user_id IS NULL AND phone_number NOT IN (SELECT phone_number FROM caller_ids WHERE user_id = ?)')
      .bind(user.id, user.id),
    c.env.DB.prepare('UPDATE audios SET user_id = ? WHERE user_id IS NULL').bind(user.id),
  ]);
  return c.redirect(noticeUrl('/admin/users', 'notice', `Assigned orphan assets to ${user.email}`), 302);
});

adminRoutes.post('/users/:id/add-paid-days', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const body = await c.req.parseBody();
  const paidDays = Number(body.paid_days);
  if (!Number.isInteger(paidDays)) return c.json({ detail: 'Invalid paid_days' }, 422);
  const err = (msg: string) => c.redirect(noticeUrl('/admin/users', 'error', msg), 302);
  if (paidDays <= 0) return err('Paid days must be greater than zero');

  const db = c.env.DB;
  const user = await db.prepare('SELECT * FROM users WHERE id = ? AND is_admin = 0').bind(id).first<UserRow>();
  if (!user) return c.json({ detail: 'User not found' }, 404);

  try {
    await addPaidDays(db, user.id, paidDays);
  } catch (e) {
    if (!(e instanceof Error) || !e.message.includes('rental plan')) throw e;
    return err('No rental plans available. Create a rental plan first.');
  }

  const rental = await getActiveRental(db, user.id);
  const until = rental ? rental.expires_at.slice(0, 10) : '';
  return c.redirect(
    noticeUrl(
      '/admin/users',
      'notice',
      `Added ${paidDays} paid day(s) to ${user.email}. Access active until ${until}.`,
    ),
    302,
  );
});

adminRoutes.get('/rental-plans', async (c) => {
  const result = await c.env.DB.prepare('SELECT * FROM rental_plans ORDER BY duration_days ASC').all<RentalPlanRow>();
  return c.html(
    render('admin/rental_plans/list.html', page(c.req.path, {
      user: c.get('user'),
      plans: result.results.length > 0 ? result.results : null,
    })),
  );
});

adminRoutes.post('/rental-plans/create', async (c) => {
  const body = await c.req.parseBody();
  const code = String(body.code ?? '').trim().toLowerCase();
  const durationDays = Number(body.duration_days);
  const priceUsdt = Number(body.price_usdt);
  if (!Number.isInteger(durationDays) || !Number.isFinite(priceUsdt)) {
    return c.json({ detail: 'Invalid form data' }, 422);
  }
  if (!code) return c.json({ detail: 'Code is required' }, 400);
  if (durationDays <= 0 || priceUsdt <= 0) {
    return c.json({ detail: 'Duration and price must be greater than zero' }, 400);
  }
  const existing = await c.env.DB.prepare('SELECT id FROM rental_plans WHERE code = ?').bind(code).first();
  if (existing) return c.json({ detail: 'Plan code already exists' }, 400);
  await c.env.DB.prepare(
    'INSERT INTO rental_plans (code, name, duration_days, price_usdt, is_active) VALUES (?, ?, ?, ?, 1)',
  )
    .bind(code, String(body.name ?? '').trim(), durationDays, priceUsdt)
    .run();
  return c.redirect('/admin/rental-plans', 302);
});

adminRoutes.post('/rental-plans/:id/edit', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const body = await c.req.parseBody();
  const durationDays = Number(body.duration_days);
  const priceUsdt = Number(body.price_usdt);
  if (!Number.isInteger(durationDays) || !Number.isFinite(priceUsdt)) {
    return c.json({ detail: 'Invalid form data' }, 422);
  }
  const plan = await c.env.DB.prepare('SELECT * FROM rental_plans WHERE id = ?')
    .bind(id)
    .first<RentalPlanRow>();
  if (!plan) return c.json({ detail: 'Plan not found' }, 404);
  if (durationDays <= 0 || priceUsdt <= 0) {
    return c.json({ detail: 'Duration and price must be greater than zero' }, 400);
  }
  await c.env.DB.prepare(
    'UPDATE rental_plans SET name = ?, duration_days = ?, price_usdt = ?, is_active = ?, updated_at = ? WHERE id = ?',
  )
    .bind(
      String(body.name ?? '').trim(),
      durationDays,
      priceUsdt,
      truthy(body.is_active) ? 1 : 0,
      new Date().toISOString(),
      id,
    )
    .run();
  return c.redirect('/admin/rental-plans', 302);
});

adminRoutes.post('/users/:id/delete', async (c) => {
  const id = pathId(c);
  if (typeof id !== 'number') return id;
  const admin = c.get('user');
  const db = c.env.DB;
  const user = await db.prepare('SELECT * FROM users WHERE id = ?').bind(id).first<UserRow>();
  if (!user) return c.json({ detail: 'User not found' }, 404);
  if (user.id === admin.id) return c.redirect(noticeUrl('/admin/users', 'error', 'Cannot delete yourself'), 302);
  if (user.is_admin) return c.redirect(noticeUrl('/admin/users', 'error', 'Cannot delete admin users'), 302);

  const cascade = [
    'DELETE FROM provider_events WHERE campaign_number_id IN (SELECT cn.id FROM campaign_numbers cn JOIN campaigns c ON c.id=cn.campaign_id WHERE c.user_id = ?)',
    'DELETE FROM campaign_numbers WHERE campaign_id IN (SELECT id FROM campaigns WHERE user_id = ?)',
    'DELETE FROM campaigns WHERE user_id = ?',
    'DELETE FROM rental_payments WHERE user_id = ?',
    'DELETE FROM user_rentals WHERE user_id = ?',
    'DELETE FROM caller_id_provider_status WHERE user_id = ?',
    'DELETE FROM caller_ids WHERE user_id = ?',
    'DELETE FROM audios WHERE user_id = ?',
    'DELETE FROM ai_agents WHERE user_id = ?',
    'DELETE FROM user_twilio_credentials WHERE user_id = ?',
    'DELETE FROM user_signalwire_credentials WHERE user_id = ?',
    'DELETE FROM user_telnyx_credentials WHERE user_id = ?',
    'DELETE FROM user_vonage_credentials WHERE user_id = ?',
    'DELETE FROM user_voximplant_credentials WHERE user_id = ?',
    'DELETE FROM user_openai_credentials WHERE user_id = ?',
    'DELETE FROM user_elevenlabs_credentials WHERE user_id = ?',
    'DELETE FROM users WHERE id = ?',
  ];
  await db.batch(cascade.map((sql) => db.prepare(sql).bind(user.id)));
  return c.redirect('/admin/users?deleted=true', 302);
});
