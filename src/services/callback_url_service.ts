const PRIVATE_V4: Array<[number, number]> = [
  [0x00000000, 8],
  [0x0a000000, 8],
  [0x64400000, 10],
  [0x7f000000, 8],
  [0xa9fe0000, 16],
  [0xac100000, 12],
  [0xc0000000, 24],
  [0xc0000200, 24],
  [0xc0a80000, 16],
  [0xc6120000, 16],
  [0xc6336400, 24],
  [0xcb007100, 24],
  [0xe0000000, 4],
  [0xffffffff, 32],
];

function ipv4ToInt(host: string): number | null {
  const parts = host.split('.');
  if (parts.length !== 4) return null;
  let value = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    value = value * 256 + n;
  }
  return value;
}

function isGlobalIp(host: string): boolean {
  if (host.includes(':')) {
    const lower = host.toLowerCase();
    if (lower === '::' || lower === '::1') return false;
    if (lower.startsWith('fc') || lower.startsWith('fd')) return false;
    if (/^fe[89ab]/.test(lower)) return false;
    if (lower.startsWith('ff')) return false;
    const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/.exec(lower);
    if (mapped) return isGlobalIp(mapped[1]);
    return true;
  }
  const ip = ipv4ToInt(host);
  if (ip === null) return false;
  for (const [net, bits] of PRIVATE_V4) {
    const size = 2 ** (32 - bits);
    if (Math.floor(ip / size) === Math.floor(net / size)) return false;
  }
  return true;
}

function normalizeHost(host: string): string {
  let normalized = (host || '').trim().toLowerCase();
  if (normalized.startsWith('[') && normalized.endsWith(']')) {
    normalized = normalized.slice(1, -1);
  }
  return normalized;
}

export function isPublicCallbackUrl(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(String(url ?? '').trim());
  } catch {
    return false;
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;

  const host = normalizeHost(parsed.hostname);
  if (!host || host === 'localhost' || host === '0.0.0.0' || host.endsWith('.local')) return false;

  const isIp = host.includes(':') || /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);
  if (!isIp) return true;
  return isGlobalIp(host);
}

export function validatePublicCallbackUrl(url: string, providerName = 'provider'): string {
  const normalized = String(url ?? '').trim();
  if (!isPublicCallbackUrl(normalized)) {
    throw new Error(
      `BASE_URL must be a public http(s) URL reachable by ${providerName} callbacks`,
    );
  }
  return normalized;
}

export function buildPublicCallbackUrl(
  baseUrl: string,
  path: string,
  providerName = 'provider',
): string {
  const normalizedBase = String(baseUrl ?? '').trim();
  const base = `${normalizedBase.replace(/\/+$/, '')}/`;
  const relative = String(path ?? '').replace(/^\/+/, '');
  let callbackUrl: string;
  try {
    callbackUrl = new URL(relative, base).toString();
  } catch {
    callbackUrl = `${base}${relative}`;
  }
  return validatePublicCallbackUrl(callbackUrl, providerName);
}
