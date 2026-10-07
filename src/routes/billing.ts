import { Hono, type Context } from 'hono';
import { render } from '../render';
import { AppEnv, requireUser } from '../middleware';
import { getActiveRental, activateOrExtendRental, type RentalPlanRow } from '../services/rental';
import { paymentService } from '../services/payment_service';

export const billingRoutes = new Hono<AppEnv>();

billingRoutes.use('*', requireUser);

interface PaymentListRow {
  tx_hash: string;
  amount_usdt: number | null;
  status: string;
  created_at: string;
  plan_name: string | null;
}

function fmtDate(iso: string | null | undefined) {
  return {
    strftime(fmt: string) {
      if (!iso) return '';
      const d = new Date(iso);
      const p = (n: number) => String(n).padStart(2, '0');
      return fmt
        .replace(/%Y/g, String(d.getUTCFullYear()))
        .replace(/%m/g, p(d.getUTCMonth() + 1))
        .replace(/%d/g, p(d.getUTCDate()))
        .replace(/%H/g, p(d.getUTCHours()))
        .replace(/%M/g, p(d.getUTCMinutes()))
        .replace(/%S/g, p(d.getUTCSeconds()));
    },
  };
}

async function loadBillingContext(c: Context<AppEnv>, error: string | null, success: boolean) {
  const user = c.get('user');
  const plans = await c.env.DB.prepare(
    'SELECT * FROM rental_plans WHERE is_active = 1 ORDER BY duration_days ASC',
  ).all<RentalPlanRow>();
  const active_rental = await getActiveRental(c.env.DB, user.id);
  const payments = await c.env.DB.prepare(
    `SELECT rp.tx_hash, rp.amount_usdt, rp.status, rp.created_at, p.name AS plan_name
     FROM rental_payments rp
     LEFT JOIN rental_plans p ON p.id = rp.plan_id
     WHERE rp.user_id = ?
     ORDER BY rp.created_at DESC
     LIMIT 10`,
  )
    .bind(user.id)
    .all<PaymentListRow>();

  return {
    request: { url: c.req.url },
    user,
    plans: plans.results,
    active_rental: active_rental
      ? { ...active_rental, expires_at: fmtDate(active_rental.expires_at) }
      : null,
    wallet_address: c.env.USDT_WALLET_ADDRESS ?? '',
    success,
    payments: payments.results.map((row) => ({
      tx_hash: row.tx_hash,
      amount_usdt: row.amount_usdt,
      status: { value: row.status },
      created_at: fmtDate(row.created_at),
      plan: row.plan_name ? { name: row.plan_name } : null,
    })),
    error,
  };
}

billingRoutes.get('/', async (c) => {
  const success = c.req.query('success') === 'true';
  const ctx = await loadBillingContext(c, null, success);
  return c.html(render('billing/index.html', ctx));
});

billingRoutes.post('/verify', async (c) => {
  const user = c.get('user');
  const body = await c.req.parseBody();
  const planIdRaw = body.plan_id;
  const txHashRaw = body.tx_hash;
  if (typeof planIdRaw !== 'string' || typeof txHashRaw !== 'string') {
    return c.json({ detail: 'field required' }, 422);
  }
  const plan_id = Number(planIdRaw);
  if (!Number.isInteger(plan_id)) {
    return c.json({ detail: 'plan_id must be an integer' }, 422);
  }

  const plan = await c.env.DB.prepare('SELECT * FROM rental_plans WHERE id = ? AND is_active = 1')
    .bind(plan_id)
    .first<RentalPlanRow>();
  if (!plan) {
    return c.json({ detail: 'Invalid rental plan' }, 400);
  }

  const tx_hash = txHashRaw.trim().toLowerCase();
  if (!tx_hash.startsWith('0x') || tx_hash.length !== 66) {
    const ctx = await loadBillingContext(c, 'Invalid transaction hash format.', false);
    return c.html(render('billing/index.html', ctx), 400);
  }

  const existing = await c.env.DB.prepare("SELECT id FROM rental_payments WHERE tx_hash = ? AND status != 'failed'")
    .bind(tx_hash)
    .first<{ id: number }>();
  if (existing) {
    const ctx = await loadBillingContext(
      c,
      'This transaction hash has already been processed.',
      false,
    );
    return c.html(render('billing/index.html', ctx), 400);
  }

  await c.env.DB.batch([
    c.env.DB.prepare("DELETE FROM rental_payments WHERE tx_hash = ? AND status = 'failed'").bind(tx_hash),
    c.env.DB.prepare(
      "INSERT INTO rental_payments (user_id, plan_id, tx_hash, status) VALUES (?, ?, ?, 'pending')",
    ).bind(user.id, plan.id, tx_hash),
  ]);

  const result = await paymentService.verify_usdt_transaction(c.env, tx_hash);
  if (!result.valid) {
    await c.env.DB.prepare(
      "UPDATE rental_payments SET status = 'failed', error_message = ? WHERE tx_hash = ?",
    )
      .bind(result.error, tx_hash)
      .run();
    const ctx = await loadBillingContext(
      c,
      `Transaction verification failed: ${result.error}`,
      false,
    );
    return c.html(render('billing/index.html', ctx), 400);
  }

  const amount = result.amount;
  if (amount < plan.price_usdt) {
    const errMsg = `Paid ${amount.toFixed(2)} USDT, but selected plan requires at least ${plan.price_usdt.toFixed(2)} USDT.`;
    await c.env.DB.prepare(
      "UPDATE rental_payments SET status = 'failed', amount_usdt = ?, error_message = ? WHERE tx_hash = ?",
    )
      .bind(amount, errMsg, tx_hash)
      .run();
    const ctx = await loadBillingContext(c, errMsg, false);
    return c.html(render('billing/index.html', ctx), 400);
  }

  await activateOrExtendRental(c.env.DB, user.id, plan);

  const rental = await c.env.DB.prepare(
    'SELECT id FROM user_rentals WHERE user_id = ? ORDER BY id DESC LIMIT 1',
  )
    .bind(user.id)
    .first<{ id: number }>();

  await c.env.DB.prepare(
    "UPDATE rental_payments SET status = 'confirmed', amount_usdt = ?, verified_at = ?, rental_id = ? WHERE tx_hash = ?",
  )
    .bind(amount, new Date().toISOString(), rental?.id ?? null, tx_hash)
    .run();

  return c.redirect('/billing?success=true', 302);
});
