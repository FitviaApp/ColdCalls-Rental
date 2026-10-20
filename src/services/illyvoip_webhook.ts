import type { Env } from '../middleware';

export interface IllyvoipWebhookEvent {
  id: string;
  type: string;
  created_at?: string;
  data?: Record<string, any>;
}

export class IllyvoipWebhookError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function constantTimeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let i = 0; i < left.length; i += 1) mismatch |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return mismatch === 0;
}

async function hexHmacSha256(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const digest = new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message)));
  return Array.from(digest)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export async function verifyIllyvoipWebhook(
  rawBody: string,
  signatureHeader: string | undefined,
  secret: string,
  nowSeconds: number,
): Promise<IllyvoipWebhookEvent> {
  if (!secret) throw new IllyvoipWebhookError('Webhook secret not configured', 503);
  const header = signatureHeader?.trim() ?? '';
  const match = header.match(/^t=(\d{1,12}),v1=([a-f0-9]{64})$/i);
  if (!match) throw new IllyvoipWebhookError('Invalid signature header', 401);
  const [, tsStr, signature] = match;
  const timestamp = Number(tsStr);
  if (timestamp < nowSeconds - 300 || timestamp > nowSeconds + 60) {
    throw new IllyvoipWebhookError('Signature timestamp outside tolerance', 401);
  }
  const expected = await hexHmacSha256(secret, `${tsStr}.${rawBody}`);
  if (!constantTimeEqual(expected, signature.toLowerCase())) {
    throw new IllyvoipWebhookError('Invalid signature', 401);
  }
  const event = JSON.parse(rawBody) as IllyvoipWebhookEvent;
  if (!event.id || typeof event.id !== 'string') throw new IllyvoipWebhookError('Missing event ID', 401);
  return event;
}

export function illyvoipEventType(event: IllyvoipWebhookEvent): string {
  return String(event.type ?? '').toLowerCase();
}

export function illyvoipCallControlId(event: IllyvoipWebhookEvent): string | null {
  const block = (event.data?.call ?? event.data?.payload ?? {}) as Record<string, unknown>;
  const id = String(block.call_control_id ?? block.call_id ?? '');
  return id || null;
}
