import type { Env } from '../middleware';
import { providerAudioUrl } from './media_token';
import { createTwilioAttemptToken, verifyTwilioAttemptToken, type TwilioCallbackPurpose } from './provider_callback_token';
import { getUserTwilioCredentials } from './user_twilio_service';
import { validateTwilioWebhook, type ParsedTwilioWebhook } from './twilio_webhook';

export interface TwilioBasicCallRow {
  id: number;
  campaign_id: number;
  user_id: number;
  dispatch_attempt_id: string | null;
  call_sid: string | null;
  provider_sequence: number;
  transfer_call_sid: string | null;
  transfer_status: string | null;
  transfer_sequence: number;
  transfer_connected_at: string | null;
  status: string;
  outcome_reason: string | null;
  caller_number: string;
  transfer_number: string | null;
  audio_id: number | null;
  press_1_to_talk_with_agent: number;
}

const PARENT_TERMINAL = new Set(['completed', 'failed', 'busy', 'no-answer', 'canceled', 'cancelled']);
const TRANSFER_FAILED = new Set(['failed', 'busy', 'no-answer', 'canceled', 'cancelled']);
const TRANSFER_CONNECTED = new Set(['answered', 'in-progress', 'completed']);

const xml = (body: string) => new Response(body, {
  headers: { 'content-type': 'application/xml; charset=utf-8', 'cache-control': 'no-store' },
});
const escapeXml = (value: unknown) => String(value ?? '').replace(/[<>&"']/g, (char) => ({
  '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;',
})[char]!);

async function rowForCall(env: Env, numberId: number): Promise<TwilioBasicCallRow | null> {
  return env.DB.prepare(
    `SELECT cn.id,cn.campaign_id,c.user_id,cn.dispatch_attempt_id,cn.call_sid,cn.provider_sequence,
            cn.transfer_call_sid,cn.transfer_status,cn.transfer_sequence,cn.transfer_connected_at,
            cn.status,cn.outcome_reason,ci.phone_number caller_number,u.transfer_number,c.audio_id,
            c.press_1_to_talk_with_agent
       FROM campaign_numbers cn
       JOIN campaigns c ON c.id=cn.campaign_id
       JOIN caller_ids ci ON ci.id=c.caller_id_id
       JOIN users u ON u.id=c.user_id
      WHERE cn.id=? AND c.voice_provider='twilio' AND c.campaign_mode='audio'`,
  ).bind(numberId).first<TwilioBasicCallRow>();
}

async function authenticate(
  env: Env,
  request: Request,
  row: TwilioBasicCallRow,
  purpose: TwilioCallbackPurpose,
): Promise<ParsedTwilioWebhook | null> {
  if (!row.dispatch_attempt_id) return null;
  const token = new URL(request.url).searchParams.get('token') ?? '';
  if (!(await verifyTwilioAttemptToken(env, token, {
    numberId: row.id,
    attemptId: row.dispatch_attempt_id,
    purpose,
  }))) return null;
  const [, authToken] = await getUserTwilioCredentials(env, env.DB, row.user_id);
  return validateTwilioWebhook(request, authToken);
}

async function bindParentCallSid(
  env: Env,
  row: TwilioBasicCallRow,
  callSid: string,
): Promise<boolean> {
  if (!callSid || !row.dispatch_attempt_id) return false;
  const result = await env.DB.prepare(
    `UPDATE campaign_numbers SET call_sid=COALESCE(call_sid,?),updated_at=?
      WHERE id=? AND dispatch_attempt_id=? AND (call_sid IS NULL OR call_sid=?)`,
  ).bind(callSid, new Date().toISOString(), row.id, row.dispatch_attempt_id, callSid).run();
  return Boolean(result.meta.changes);
}

async function recordEvent(
  env: Env,
  row: TwilioBasicCallRow,
  leg: 'parent' | 'transfer',
  callSid: string,
  status: string,
  sequence: number,
): Promise<boolean> {
  const eventId = `twilio:${row.dispatch_attempt_id}:${leg}:${callSid}:${status}:${sequence}`;
  const inserted = await env.DB.prepare(
    "INSERT OR IGNORE INTO provider_events(id,provider,campaign_number_id) VALUES(?,'twilio',?)",
  ).bind(eventId, row.id).run();
  return Boolean(inserted.meta.changes);
}

async function transferTwiml(env: Env, row: TwilioBasicCallRow): Promise<string> {
  if (!row.dispatch_attempt_id || !row.transfer_number) return '<Response><Hangup/></Response>';
  const token = await createTwilioAttemptToken(env, {
    numberId: row.id,
    attemptId: row.dispatch_attempt_id,
    purpose: 'transfer-status',
  });
  const callback = `${(env.BASE_URL ?? '').replace(/\/+$/, '')}/api/twilio/calls/${row.id}/transfer-status?token=${encodeURIComponent(token)}`;
  return `<Response><Dial callerId="${escapeXml(row.caller_number)}" timeout="30"><Number statusCallback="${escapeXml(callback)}" statusCallbackMethod="POST" statusCallbackEvent="initiated ringing answered completed">${escapeXml(row.transfer_number)}</Number></Dial><Hangup/></Response>`;
}

export async function handleTwilioAnswer(env: Env, request: Request, numberId: number): Promise<Response> {
  const row = await rowForCall(env, numberId);
  if (!row) return xml('<Response><Hangup/></Response>');
  const parsed = await authenticate(env, request, row, 'answer');
  if (!parsed) return Response.json({ detail: 'Invalid Twilio signature or attempt token' }, { status: 403 });
  const callSid = parsed.body.get('CallSid') ?? '';
  if (!(await bindParentCallSid(env, row, callSid))) {
    return Response.json({ detail: 'Stale or mismatched Twilio call attempt' }, { status: 409 });
  }
  const answeredBy = String(parsed.body.get('AnsweredBy') ?? 'unknown').toLowerCase();
  const now = new Date().toISOString();
  if (answeredBy !== 'human') {
    const reason = answeredBy === 'fax' ? 'fax' : answeredBy.startsWith('machine') ? 'machine' : 'amd_unknown';
    await env.DB.prepare(
      `UPDATE campaign_numbers SET status='failed',dispatch_state='finished',answered_by=?,outcome_reason=?,
       error_message=?,processed_at=COALESCE(processed_at,?),updated_at=?
       WHERE id=? AND dispatch_attempt_id=?`,
    ).bind(answeredBy, reason, `Twilio AMD result: ${reason}`, now, now, row.id, row.dispatch_attempt_id).run();
    return xml('<Response><Hangup/></Response>');
  }

  await env.DB.prepare(
    `UPDATE campaign_numbers SET status='calling',dispatch_state='dispatched',answered_by='human',
     outcome_reason='human_answered',next_action_at=?,updated_at=? WHERE id=? AND dispatch_attempt_id=?`,
  ).bind(new Date(Date.now() + 30_000).toISOString(), now, row.id, row.dispatch_attempt_id).run();
  const play = row.audio_id ? `<Play>${escapeXml(await providerAudioUrl(env, row.audio_id))}</Play>` : '';
  if (!row.press_1_to_talk_with_agent) return xml(await transferTwiml(env, row).then((value) => value.replace('<Response>', `<Response>${play}`)));

  const gatherToken = await createTwilioAttemptToken(env, {
    numberId: row.id,
    attemptId: row.dispatch_attempt_id!,
    purpose: 'gather',
  });
  const gatherAction = `${(env.BASE_URL ?? '').replace(/\/+$/, '')}/api/twilio/calls/${row.id}/gather?token=${encodeURIComponent(gatherToken)}`;
  return xml(`<Response><Gather numDigits="1" timeout="10" actionOnEmptyResult="true" action="${escapeXml(gatherAction)}" method="POST">${play}<Say>Press 1 to talk with an agent.</Say></Gather><Hangup/></Response>`);
}

export async function handleTwilioGather(env: Env, request: Request, numberId: number): Promise<Response> {
  const row = await rowForCall(env, numberId);
  if (!row) return xml('<Response><Hangup/></Response>');
  const parsed = await authenticate(env, request, row, 'gather');
  if (!parsed) return Response.json({ detail: 'Invalid Twilio signature or attempt token' }, { status: 403 });
  const callSid = parsed.body.get('CallSid') ?? '';
  if (!(await bindParentCallSid(env, row, callSid))) {
    return Response.json({ detail: 'Stale or mismatched Twilio call attempt' }, { status: 409 });
  }
  const digits = String(parsed.body.get('Digits') ?? '');
  const now = new Date().toISOString();
  if (digits === '1') {
    await env.DB.prepare(
      `UPDATE campaign_numbers SET pressed_1_at=?,outcome_reason='pressed_1',updated_at=?
       WHERE id=? AND dispatch_attempt_id=?`,
    ).bind(now, now, row.id, row.dispatch_attempt_id).run();
    return xml(await transferTwiml(env, row));
  }
  const reason = digits ? 'invalid_digit' : 'no_input';
  await env.DB.prepare(
    `UPDATE campaign_numbers SET status='failed',dispatch_state='finished',outcome_reason=?,error_message=?,
     processed_at=COALESCE(processed_at,?),updated_at=? WHERE id=? AND dispatch_attempt_id=?`,
  ).bind(reason, digits ? 'Twilio gather received an invalid digit' : 'Twilio gather timed out without input', now, now, row.id, row.dispatch_attempt_id).run();
  return xml('<Response><Hangup/></Response>');
}

export async function handleTwilioParentStatus(env: Env, request: Request, numberId: number): Promise<Response> {
  const row = await rowForCall(env, numberId);
  if (!row) return Response.json({ detail: 'Twilio call not found' }, { status: 404 });
  const parsed = await authenticate(env, request, row, 'parent-status');
  if (!parsed) return Response.json({ detail: 'Invalid Twilio signature or attempt token' }, { status: 403 });
  const callSid = parsed.body.get('CallSid') ?? '';
  if (!(await bindParentCallSid(env, row, callSid))) {
    return Response.json({ detail: 'Stale or mismatched Twilio call attempt' }, { status: 409 });
  }
  const status = String(parsed.body.get('CallStatus') ?? 'unknown').toLowerCase();
  const sequence = Number(parsed.body.get('SequenceNumber') ?? -1);
  if (!(await recordEvent(env, row, 'parent', callSid, status, sequence))) {
    return Response.json({ ok: true, duplicate: true });
  }
  const now = new Date().toISOString();
  const terminal = PARENT_TERMINAL.has(status);
  const fresh = await rowForCall(env, numberId);
  if (!fresh || sequence <= fresh.provider_sequence) return Response.json({ ok: true, stale: true });
  const duration = Number(parsed.body.get('CallDuration') ?? 0);
  const answeredBy = parsed.body.get('AnsweredBy');
  const transferConnected = Boolean(fresh.transfer_connected_at) || TRANSFER_CONNECTED.has(fresh.transfer_status ?? '');
  const transferFailed = TRANSFER_FAILED.has(fresh.transfer_status ?? '');
  const preserveFailure = ['machine', 'fax', 'amd_unknown', 'no_input', 'invalid_digit'].includes(fresh.outcome_reason ?? '');
  const resolvedStatus = transferConnected ? 'completed' : (transferFailed || preserveFailure) ? 'failed' : 'calling';
  const outcome = terminal && resolvedStatus === 'calling' ? 'awaiting_transfer_status' : fresh.outcome_reason;
  await env.DB.prepare(
    `UPDATE campaign_numbers SET provider_status=?,provider_sequence=?,duration_seconds=?,
     answered_by=COALESCE(?,answered_by),provider_terminal_at=CASE WHEN ? THEN COALESCE(provider_terminal_at,?) ELSE provider_terminal_at END,
     status=CASE WHEN ? THEN ? ELSE status END,outcome_reason=?,
     dispatch_state=CASE WHEN ? AND ?!='calling' THEN 'finished' ELSE dispatch_state END,
     processed_at=CASE WHEN ? AND ?!='calling' THEN COALESCE(processed_at,?) ELSE processed_at END,
     next_action_at=CASE WHEN ? AND ?='calling' THEN ? ELSE next_action_at END,
     cost_reconcile_status=CASE WHEN ? THEN 'pending' ELSE cost_reconcile_status END,
     cost_next_retry_at=CASE WHEN ? THEN COALESCE(cost_next_retry_at,?) ELSE cost_next_retry_at END,updated_at=?
     WHERE id=? AND dispatch_attempt_id=? AND provider_sequence<?`,
  ).bind(status, sequence, duration, answeredBy, terminal ? 1 : 0, now,
    terminal ? 1 : 0, resolvedStatus, outcome, terminal ? 1 : 0, resolvedStatus,
    terminal ? 1 : 0, resolvedStatus, now, terminal ? 1 : 0, resolvedStatus,
    new Date(Date.now() + 30_000).toISOString(), terminal ? 1 : 0, terminal ? 1 : 0,
    new Date(Date.now() + 60_000).toISOString(), now, row.id, row.dispatch_attempt_id, sequence).run();
  return Response.json({ ok: true });
}

export async function handleTwilioTransferStatus(env: Env, request: Request, numberId: number): Promise<Response> {
  const row = await rowForCall(env, numberId);
  if (!row) return Response.json({ detail: 'Twilio call not found' }, { status: 404 });
  const parsed = await authenticate(env, request, row, 'transfer-status');
  if (!parsed) return Response.json({ detail: 'Invalid Twilio signature or attempt token' }, { status: 403 });
  const parentSid = parsed.body.get('ParentCallSid') ?? '';
  if (!(await bindParentCallSid(env, row, parentSid))) {
    return Response.json({ detail: 'Stale or mismatched Twilio parent call' }, { status: 409 });
  }
  const callSid = parsed.body.get('CallSid') ?? '';
  const status = String(parsed.body.get('CallStatus') ?? 'unknown').toLowerCase();
  const sequence = Number(parsed.body.get('SequenceNumber') ?? -1);
  if (!callSid) return Response.json({ detail: 'Missing Twilio transfer CallSid' }, { status: 400 });
  if (!(await recordEvent(env, row, 'transfer', callSid, status, sequence))) {
    return Response.json({ ok: true, duplicate: true });
  }
  const fresh = await rowForCall(env, numberId);
  if (!fresh || sequence <= fresh.transfer_sequence || (fresh.transfer_call_sid && fresh.transfer_call_sid !== callSid)) {
    return Response.json({ ok: true, stale: true });
  }
  const now = new Date().toISOString();
  const connected = Boolean(fresh.transfer_connected_at) || TRANSFER_CONNECTED.has(status);
  const failed = !connected && TRANSFER_FAILED.has(status);
  const duration = Number(parsed.body.get('CallDuration') ?? 0);
  await env.DB.prepare(
    `UPDATE campaign_numbers SET transfer_call_sid=COALESCE(transfer_call_sid,?),transfer_status=?,transfer_sequence=?,
     transfer_duration_seconds=?,transfer_connected_at=CASE WHEN ? THEN COALESCE(transfer_connected_at,?) ELSE transfer_connected_at END,
     status=CASE WHEN ? THEN 'completed' WHEN ? THEN 'failed' ELSE status END,
     outcome_reason=CASE WHEN ? THEN 'transfer_connected' WHEN ? THEN 'transfer_failed' ELSE outcome_reason END,
     error_message=CASE WHEN ? THEN 'Twilio transfer did not connect' WHEN ? THEN NULL ELSE error_message END,
     dispatch_state=CASE WHEN ? OR ? THEN 'finished' ELSE dispatch_state END,
     processed_at=CASE WHEN ? OR ? THEN COALESCE(processed_at,?) ELSE processed_at END,
     cost_reconcile_status=CASE WHEN ? OR ? THEN 'pending' ELSE cost_reconcile_status END,
     cost_next_retry_at=CASE WHEN ? OR ? THEN COALESCE(cost_next_retry_at,?) ELSE cost_next_retry_at END,updated_at=?
     WHERE id=? AND dispatch_attempt_id=? AND transfer_sequence<? AND (transfer_call_sid IS NULL OR transfer_call_sid=?)`,
  ).bind(callSid, status, sequence, duration, connected ? 1 : 0, now,
    connected ? 1 : 0, failed ? 1 : 0, connected ? 1 : 0, failed ? 1 : 0,
    failed ? 1 : 0, connected ? 1 : 0, connected ? 1 : 0, failed ? 1 : 0,
    connected ? 1 : 0, failed ? 1 : 0, now, connected ? 1 : 0, failed ? 1 : 0,
    connected ? 1 : 0, failed ? 1 : 0, new Date(Date.now() + 60_000).toISOString(), now,
    row.id, row.dispatch_attempt_id, sequence, callSid).run();
  return Response.json({ ok: true });
}
