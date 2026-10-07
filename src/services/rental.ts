
export interface RentalPlanRow {
  id: number;
  code: string;
  name: string;
  duration_days: number;
  price_usdt: number;
  is_active: number;
  created_at: string;
  updated_at: string;
}

export interface UserRentalRow {
  id: number;
  user_id: number;
  plan_id: number;
  starts_at: string;
  expires_at: string;
  status: string;
  created_at: string;
}

export const DEFAULT_RENTAL_PLANS = [
  { code: 'daily', name: 'Daily Rental', duration_days: 1, price_usdt: 15.0 },
  { code: 'weekly', name: 'Weekly Rental', duration_days: 7, price_usdt: 75.0 },
];

export async function ensureDefaultRentalPlans(db: D1Database): Promise<void> {
  for (const plan of DEFAULT_RENTAL_PLANS) {
    await db
      .prepare(
        'INSERT OR IGNORE INTO rental_plans (code, name, duration_days, price_usdt, is_active) VALUES (?, ?, ?, ?, 1)',
      )
      .bind(plan.code, plan.name, plan.duration_days, plan.price_usdt)
      .run();
  }
}

export async function getActiveRental(
  db: D1Database,
  userId: number,
): Promise<UserRentalRow | null> {
  const now = new Date().toISOString();
  await db
    .prepare(
      "UPDATE user_rentals SET status = 'expired' WHERE user_id = ? AND status = 'active' AND expires_at <= ?",
    )
    .bind(userId, now)
    .run();
  return await db
    .prepare(
      "SELECT * FROM user_rentals WHERE user_id = ? AND status = 'active' AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
    )
    .bind(userId, now)
    .first<UserRentalRow>();
}

export async function hasActiveRental(db: D1Database, userId: number): Promise<boolean> {
  return (await getActiveRental(db, userId)) !== null;
}

function plusDays(fromIso: string | null, days: number): { starts: string; expires: string } {
  const now = new Date();
  const starts = fromIso ? new Date(fromIso) : now;
  const expires = new Date(starts.getTime() + days * 86400000);
  return { starts: starts.toISOString(), expires: expires.toISOString() };
}

export async function activateOrExtendRental(
  db: D1Database,
  userId: number,
  plan: RentalPlanRow,
): Promise<void> {
  const current = await getActiveRental(db, userId);
  const active = current && current.expires_at > new Date().toISOString() ? current : null;
  const { starts, expires } = plusDays(active ? active.expires_at : null, plan.duration_days);
  await db
    .prepare(
      "INSERT INTO user_rentals (user_id, plan_id, starts_at, expires_at, status) VALUES (?, ?, ?, ?, 'active')",
    )
    .bind(userId, plan.id, starts, expires)
    .run();
}

export async function addPaidDays(
  db: D1Database,
  userId: number,
  days: number,
): Promise<void> {
  if (days <= 0) throw new Error('Days must be greater than zero');
  const plan =
    (await db
      .prepare('SELECT * FROM rental_plans WHERE is_active = 1 ORDER BY duration_days ASC LIMIT 1')
      .first<RentalPlanRow>()) ??
    (await db
      .prepare('SELECT * FROM rental_plans ORDER BY duration_days ASC LIMIT 1')
      .first<RentalPlanRow>());
  if (!plan) throw new Error('No rental plan exists');
  const current = await getActiveRental(db, userId);
  const active = current && current.expires_at > new Date().toISOString() ? current : null;
  const { starts, expires } = plusDays(active ? active.expires_at : null, days);
  await db
    .prepare(
      "INSERT INTO user_rentals (user_id, plan_id, starts_at, expires_at, status) VALUES (?, ?, ?, ?, 'active')",
    )
    .bind(userId, plan.id, starts, expires)
    .run();
}
