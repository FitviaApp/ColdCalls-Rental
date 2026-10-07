import { Environment, PrecompiledLoader, runtime } from 'nunjucks';
import { templates } from './templates_compiled';

// Python "%s"/"%.Nf" style printf filter used across templates: {{ "%.2f"|format(x) }}
function pyFormat(fmt: string, ...args: unknown[]): string {
  let i = 0;
  return String(fmt).replace(/%(\.\d+)?([sdif%])/g, (_m, prec, type) => {
    if (type === '%') return '%';
    const v = args[i++];
    if (type === 'f') return Number(v).toFixed(prec ? parseInt(prec.slice(1)) : 0);
    if (type === 'd') return String(Math.trunc(Number(v)));
    return String(v ?? '');
  });
}

// @types/nunjucks types the constructor as an array; runtime accepts a name->template map.
const env = new Environment(new PrecompiledLoader(templates as unknown as unknown[]), {
  autoescape: true,
  throwOnUndefined: false,
});
env.addFilter('format', pyFormat);
env.addFilter('tojson', (v: unknown) => new runtime.SafeString(
  JSON.stringify(v)
    .replace(/&/g, '\\u0026')
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029'),
));

const WRAPPED = Symbol('wrapped');

const proxyHandler: ProxyHandler<object> = {
  get(target, prop, receiver) {
    if (prop === WRAPPED) return true;
    // Python dict support in templates: `d.get('key', default)` (27 usages).
    if (prop === 'get') {
      return (key: string, def?: unknown) => {
        const v = (target as Record<string, unknown>)[key as string];
        return v === undefined ? wrap(def) : wrap(v);
      };
    }
    const v = Reflect.get(target, prop, receiver);
    return typeof v === 'function' ? v.bind(target) : wrap(v);
  },
};

function wrap(v: unknown): any {
  if (v === null || typeof v !== 'object') return v;
  if ((v as Record<symbol, unknown>)[WRAPPED]) return v;
  return new Proxy(v as object, proxyHandler);
}

// Templates do `request.url.path` (Starlette URL object) e às vezes recebem url string.
function normalizeRequest(
  req: { url?: string | { path?: string } } | undefined,
): { url: { path: string } } {
  const url = req?.url;
  if (url && typeof url === 'object') return { url: { path: url.path ?? '/' } };
  if (typeof url === 'string') {
    try {
      return { url: { path: new URL(url).pathname } };
    } catch {
      return { url: { path: url } };
    }
  }
  return { url: { path: '/' } };
}

export function render(
  name: string,
  ctx: Record<string, unknown> & { request?: { url?: string | { path?: string } } },
): string {
  const { request: _req, ...rest } = ctx;
  const context = { ...rest, request: normalizeRequest(ctx.request) };
  return env.render(name, wrap(context));
}
