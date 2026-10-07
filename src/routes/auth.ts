import { Hono } from 'hono';
import { deleteCookie, setCookie } from 'hono/cookie';
import { render } from '../render';
import { signToken, verifyPassword } from '../auth';
import { AppEnv, getUser } from '../middleware';

export const authRoutes = new Hono<AppEnv>();

authRoutes.use('/login', async (c, next) => {
  await next();
  c.res.headers.set('Cache-Control', 'no-store, max-age=0');
  c.res.headers.set('Pragma', 'no-cache');
});

authRoutes.get('/login', async (c) => {
  const user = await getUser(c);
  if (user) return c.redirect('/dashboard', 302);
  return c.html(
    render('auth/login.html', {
      request: { url: c.req.url },
      registration_disabled: c.req.query('registration') === 'disabled',
      error: null,
    }),
  );
});

authRoutes.post('/login', async (c) => {
  const body = await c.req.parseBody();
  const email = String(body.email ?? '').trim().toLowerCase();
  const password = String(body.password ?? '');

  const fail = (error: string, status: 400 | 403) =>
    c.html(
      render('auth/login.html', {
        request: { url: c.req.url },
        registration_disabled: false,
        error,
      }),
      status,
    );

  const user = await c.env.DB.prepare('SELECT * FROM users WHERE email = ?')
    .bind(email)
    .first<{ id: number; password_hash: string; is_active: number }>();

  if (!user || !verifyPassword(password, user.password_hash)) {
    return fail('Invalid email or password', 400);
  }
  if (!user.is_active) return fail('Your account has been disabled', 403);

  const token = await signToken(c.env.JWT_SECRET, user.id);
  setCookie(c, 'access_token', token, {
    httpOnly: true,
    maxAge: 24 * 3600,
    sameSite: 'Lax',
    secure: new URL(c.req.url).protocol === 'https:',
    path: '/',
  });
  return c.redirect('/dashboard', 302);
});

authRoutes.get('/register', () => new Response(null, { status: 302, headers: { Location: '/auth/login?registration=disabled' } }));
authRoutes.post('/register', () => new Response(null, { status: 302, headers: { Location: '/auth/login?registration=disabled' } }));

// POST only: a GET logout is triggerable cross-site (cookie is SameSite=Lax).
authRoutes.post('/logout', (c) => {
  deleteCookie(c, 'access_token', { path: '/' });
  return c.redirect('/auth/login', 302);
});
