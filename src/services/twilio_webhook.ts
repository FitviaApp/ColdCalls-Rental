import { validateRequest } from 'twilio/lib/webhooks/webhooks.js';

export interface ParsedTwilioWebhook {
  params: Record<string, string | string[]>;
  body: URLSearchParams;
}

export async function parseTwilioWebhook(request: Request): Promise<ParsedTwilioWebhook> {
  const raw = new TextDecoder().decode(await request.clone().arrayBuffer());
  const body = new URLSearchParams(raw);
  const params: Record<string, string | string[]> = {};
  for (const key of new Set(body.keys())) {
    const values = body.getAll(key);
    params[key] = values.length === 1 ? values[0] : values;
  }
  return { params, body };
}

export async function validateTwilioWebhook(
  request: Request,
  authToken: string,
): Promise<ParsedTwilioWebhook | null> {
  const signature = request.headers.get('X-Twilio-Signature') ?? '';
  if (!signature || !authToken) return null;
  const parsed = await parseTwilioWebhook(request);
  return validateRequest(authToken, signature, request.url, parsed.params) ? parsed : null;
}
