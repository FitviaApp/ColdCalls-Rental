import { DurableObject } from 'cloudflare:workers';
import type { Env } from '../middleware';
import { providerAudioUrl } from '../services/media_token';
import { createVoiceProviderAdapter } from '../services/voice_provider_adapter';
import { TwilioService } from '../services/twilio_service';
import { getUserTwilioCredentials } from '../services/user_twilio_service';

interface CampaignDispatchRow {
  id: number;
  user_id: number;
  status: string;
  campaign_mode: string;
  voice_provider: string;
  max_concurrent_calls: number;
  press_1_to_talk_with_agent: number;
  caller_number: string;
  audio_id: number | null;
  transfer_number: string | null;
}

interface NumberRow {
  id: number;
  phone_number: string;
  call_sid: string | null;
  status: string;
  provider_status: string | null;
  transfer_status: string | null;
  transfer_connected_at: string | null;
  outcome_reason: string | null;
  provider_terminal_at: string | null;
}

const FINAL_PROVIDER_STATES = new Set([
  'completed', 'failed', 'busy', 'no-answer', 'no_answer', 'canceled', 'cancelled',
  'rejected', 'unanswered', 'timeout', 'timedout', 'cancel', 'disconnected',
  'ringtimeout', 'error', 'transferred', 'forwarded',
]);

const SUCCESS_PROVIDER_STATES = new Set(['completed', 'answered']);
export const TWILIO_START_INTERVAL_MS = 1_000;
export const SIGNALWIRE_MIN_START_INTERVAL_MS = 3_000;
export const SIGNALWIRE_MAX_START_INTERVAL_MS = 5_000;
export const SIGNALWIRE_CALLBACK_WAIT_MS = 120_000;

export interface ProviderSlotReservation {
  granted: boolean;
  retryAt: number;
}

function knownProviderRejection(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  // 429 = transient rate limit (Twilio error 112); keep the row dispatch_unknown for retry.
  if (/status=429\b/.test(message)) return false;
  return /status=4\d\d\b/.test(message) || /credentials not configured|unsupported voice provider|BASE_URL must be a public http\(s\) URL/i.test(message);
}

export class CampaignCoordinator extends DurableObject<Env> {
  async reserveProviderSlot(
    minIntervalMs = SIGNALWIRE_MIN_START_INTERVAL_MS,
    maxIntervalMs = SIGNALWIRE_MAX_START_INTERVAL_MS,
  ): Promise<ProviderSlotReservation> {
    const minimum = Math.max(1, Math.trunc(minIntervalMs));
    const maximum = Math.max(minimum, Math.trunc(maxIntervalMs));
    const now = Date.now();
    return this.ctx.storage.transaction(async (transaction) => {
      let nextAllowedAt = await transaction.get<number>('providerNextAllowedAt') ?? 0;
      // Discard cooldowns written by the superseded circuit-breaker release.
      if (nextAllowedAt > now + maximum) {
        nextAllowedAt = 0;
        await transaction.delete('providerNextAllowedAt');
      }
      if (now < nextAllowedAt) return { granted: false, retryAt: nextAllowedAt };
      const interval = minimum + Math.floor(Math.random() * (maximum - minimum + 1));
      const retryAt = now + interval;
      await transaction.put('providerNextAllowedAt', retryAt);
      return { granted: true, retryAt };
    });
  }

  async start(campaignId: number): Promise<void> {
    await this.ctx.storage.put('campaignId', campaignId);
    await this.ctx.storage.setAlarm(Date.now());
  }

  async pause(): Promise<void> {
    await this.ctx.storage.deleteAlarm();
  }

  async cancel(): Promise<void> {
    await this.ctx.storage.deleteAlarm();
  }

  async reconcile(campaignId: number): Promise<void> {
    await this.start(campaignId);
  }

  async alarm(): Promise<void> {
    const campaignId = await this.ctx.storage.get<number>('campaignId');
    if (!campaignId) return;

    const campaign = await this.env.DB.prepare(
      `SELECT c.id, c.user_id, c.status, c.campaign_mode, c.voice_provider,
              c.max_concurrent_calls, c.press_1_to_talk_with_agent,
              ci.phone_number AS caller_number, c.audio_id, u.transfer_number
         FROM campaigns c
         JOIN caller_ids ci ON ci.id = c.caller_id_id
         JOIN users u ON u.id = c.user_id
        WHERE c.id = ?`,
    ).bind(campaignId).first<CampaignDispatchRow>();
    if (!campaign) return;
    if (campaign.voice_provider === 'twilio' && campaign.campaign_mode === 'audio') {
      await this.reconcileTwilioCosts(campaign);
    }
    if (campaign.status !== 'running') {
      await this.schedulePendingCostReconciliation(campaign.id);
      return;
    }

    const now = new Date().toISOString();
    const staleBefore = new Date(Date.now() - 120_000).toISOString();
    await this.env.DB.prepare(
      `UPDATE campaign_numbers SET status='dispatch_unknown',dispatch_state='dispatch_unknown',
       error_message='Coordinator restarted after dispatch claim; outcome is ambiguous and will not be retried',
       processed_at=?,updated_at=? WHERE campaign_id=? AND status='claimed' AND claimed_at<?`,
    ).bind(now, now, campaignId, staleBefore).run();

    const adapter = await createVoiceProviderAdapter(this.env, campaign.voice_provider, campaign.user_id);
    await this.expireAwaitingCallbacks(campaign.id);
    await this.pollActive(campaign, adapter);

    const [active, pending] = await Promise.all([
      this.env.DB.prepare(
        "SELECT COUNT(*) AS count FROM campaign_numbers WHERE campaign_id = ? AND status IN ('claimed','calling')",
      ).bind(campaignId).first<{ count: number }>(),
      this.env.DB.prepare(
        "SELECT 1 AS present FROM campaign_numbers WHERE campaign_id = ? AND status = 'pending' LIMIT 1",
      ).bind(campaignId).first<{ present: number }>(),
    ]);
    const available = Math.max(0, campaign.max_concurrent_calls - Number(active?.count ?? 0));
    let providerRetryAt: number | null = null;
    if (available > 0 && pending) {
      const reservation = await this.reserveDispatchSlot(campaign);
      if (reservation.granted) await this.dispatchOne(campaign, adapter);
      else providerRetryAt = reservation.retryAt;
    }

    const remaining = await this.env.DB.prepare(
      "SELECT COUNT(*) AS count FROM campaign_numbers WHERE campaign_id = ? AND status IN ('pending','claimed','calling')",
    ).bind(campaignId).first<{ count: number }>();
    if (!Number(remaining?.count ?? 0)) {
      await this.env.DB.prepare(
        `UPDATE campaigns SET status = 'completed', completed_at = ?,
          processed_numbers = (SELECT COUNT(*) FROM campaign_numbers WHERE campaign_id = ?),
          successful_calls = (SELECT COUNT(*) FROM campaign_numbers WHERE campaign_id = ? AND status = 'completed'),
          failed_calls = (SELECT COUNT(*) FROM campaign_numbers WHERE campaign_id = ? AND status IN ('failed','dispatch_unknown'))
         WHERE id = ? AND status = 'running'`,
      ).bind(new Date().toISOString(), campaignId, campaignId, campaignId, campaignId).run();
      await this.schedulePendingCostReconciliation(campaignId);
      return;
    }
    await this.ctx.storage.setAlarm(providerRetryAt ?? Date.now() + adapter.intervalMs);
  }

  private async reserveDispatchSlot(campaign: CampaignDispatchRow): Promise<ProviderSlotReservation> {
    if (!['signalwire', 'twilio'].includes(campaign.voice_provider)) return { granted: true, retryAt: Date.now() };
    const id = this.env.CAMPAIGN_COORDINATORS.idFromName(`provider-rate:${campaign.user_id}:${campaign.voice_provider}`);
    const limiter = this.env.CAMPAIGN_COORDINATORS.get(id) as unknown as {
      reserveProviderSlot(minIntervalMs: number, maxIntervalMs: number): Promise<ProviderSlotReservation>;
    };
    return campaign.voice_provider === 'twilio'
      ? limiter.reserveProviderSlot(TWILIO_START_INTERVAL_MS, TWILIO_START_INTERVAL_MS)
      : limiter.reserveProviderSlot(SIGNALWIRE_MIN_START_INTERVAL_MS, SIGNALWIRE_MAX_START_INTERVAL_MS);
  }

  private async dispatchOne(
    campaign: CampaignDispatchRow,
    adapter: Awaited<ReturnType<typeof createVoiceProviderAdapter>>,
  ): Promise<void> {
    const next = await this.env.DB.prepare(
      `SELECT id,phone_number,call_sid,status,provider_status,transfer_status,transfer_connected_at,
              outcome_reason,provider_terminal_at
         FROM campaign_numbers WHERE campaign_id = ? AND status = 'pending' ORDER BY id LIMIT 1`,
    ).bind(campaign.id).first<NumberRow>();
    if (!next) return;

    // pollActive/adapter awaits can span a user pause or cancel; never dial afterwards.
    const live = await this.env.DB.prepare("SELECT 1 AS present FROM campaigns WHERE id=? AND status='running'")
      .bind(campaign.id).first<{ present: number }>();
    if (!live) return;

    const now = new Date().toISOString();
    const dispatchAttemptId = crypto.randomUUID();
    const claim = await this.env.DB.prepare(
      `UPDATE campaign_numbers SET status = 'claimed', dispatch_state = 'claimed',
       dispatch_attempts = dispatch_attempts + 1, dispatch_attempt_id=?, claimed_at = ?, updated_at = ?
       WHERE id = ? AND status = 'pending'`,
    ).bind(dispatchAttemptId, now, now, next.id).run();
    if (!claim.meta.changes) return;

    const audioUrl = campaign.audio_id ? await providerAudioUrl(this.env, campaign.audio_id) : null;
    const answerUrl = campaign.campaign_mode === 'ai_agent'
      ? `${(this.env.BASE_URL ?? '').replace(/\/+$/, '')}/api/ai-realtime/twiml/${next.id}`
      : null;
    try {
      const result = await adapter.makeCall({
        campaignId: campaign.id,
        campaignNumberId: next.id,
        toNumber: next.phone_number,
        fromNumber: campaign.caller_number,
        audioUrl,
        transferNumber: campaign.transfer_number ?? '',
        press1: Boolean(campaign.press_1_to_talk_with_agent),
        answerUrl,
        dispatchAttemptId,
      });
      if (result.awaitingCallback) {
        const callbackDeadline = new Date(Date.now() + SIGNALWIRE_CALLBACK_WAIT_MS).toISOString();
        await this.env.DB.prepare(
          `UPDATE campaign_numbers SET status = 'calling',
           dispatch_state = CASE WHEN call_sid IS NULL THEN 'awaiting_callback' ELSE 'dispatched' END,
           provider_status = COALESCE(provider_status,?),
           error_message = CASE WHEN call_sid IS NULL THEN ? ELSE NULL END,
           next_action_at = CASE WHEN call_sid IS NULL THEN ? ELSE ? END, updated_at = ?
           WHERE id = ? AND dispatch_attempt_id=? AND status IN ('claimed','calling')`,
        ).bind(result.status, result.diagnostic ?? 'SignalWire accepted the call without returning a call SID',
          callbackDeadline, new Date(Date.now() + 30_000).toISOString(), new Date().toISOString(),
          next.id, dispatchAttemptId).run();
        return;
      }
      if (!result.callSid) throw new Error('Voice provider returned no call SID');
      await this.env.DB.prepare(
        `UPDATE campaign_numbers SET status = 'calling', dispatch_state = 'dispatched',
         call_sid = COALESCE(call_sid,?), provider_status = COALESCE(provider_status,?), next_action_at = ?, updated_at = ?
         WHERE id = ? AND dispatch_attempt_id=? AND status IN ('claimed','calling') AND (call_sid IS NULL OR call_sid=?)`,
      ).bind(result.callSid, result.status, new Date(Date.now() + 30_000).toISOString(), now,
        next.id, dispatchAttemptId, result.callSid).run();
    } catch (error) {
      const status = knownProviderRejection(error) ? 'failed' : 'dispatch_unknown';
      await this.env.DB.prepare(
        `UPDATE campaign_numbers SET status = ?, dispatch_state = ?, error_message = ?,
         processed_at = ?, updated_at = ? WHERE id = ? AND status = 'claimed' AND dispatch_attempt_id=?`,
      ).bind(status, status, String(error).replace(/\+\d{7,15}/g, '[redacted-number]').slice(0, 500), now, now,
        next.id, dispatchAttemptId).run();
    }
  }

  private async expireAwaitingCallbacks(campaignId: number): Promise<void> {
    const now = new Date().toISOString();
    await this.env.DB.prepare(
      `UPDATE campaign_numbers SET status='dispatch_unknown',dispatch_state='dispatch_unknown',
       error_message=CASE
         WHEN error_message IS NULL OR error_message='' THEN 'SignalWire callback was not received within 120 seconds'
         ELSE error_message || '; SignalWire callback was not received within 120 seconds'
       END,
       processed_at=COALESCE(processed_at,?),updated_at=?
       WHERE campaign_id=? AND status='calling' AND dispatch_state='awaiting_callback'
         AND call_sid IS NULL AND next_action_at IS NOT NULL AND next_action_at<=?`,
    ).bind(now, now, campaignId, now).run();
  }

  private async pollActive(
    campaign: CampaignDispatchRow,
    adapter: Awaited<ReturnType<typeof createVoiceProviderAdapter>>,
  ): Promise<void> {
    const rows = await this.env.DB.prepare(
      `SELECT id,phone_number,call_sid,status,provider_status,transfer_status,transfer_connected_at,
              outcome_reason,provider_terminal_at
         FROM campaign_numbers WHERE campaign_id=? AND status='calling' AND call_sid IS NOT NULL
          AND (next_action_at IS NULL OR next_action_at<=?) ORDER BY id LIMIT 20`,
    ).bind(campaign.id, new Date().toISOString()).all<NumberRow>();
    for (const row of rows.results) {
      try {
        const result = await adapter.getCallStatus(row.call_sid!, row.id);
        if (!FINAL_PROVIDER_STATES.has(result.status)) {
          await this.env.DB.prepare(
            'UPDATE campaign_numbers SET provider_status = ?, next_action_at = ?, updated_at = ? WHERE id = ?',
          ).bind(result.status, new Date(Date.now() + 30_000).toISOString(), new Date().toISOString(), row.id).run();
          continue;
        }
        if (campaign.voice_provider === 'twilio' && campaign.campaign_mode === 'audio') {
          const connected = Boolean(row.transfer_connected_at) || ['answered', 'in-progress', 'completed'].includes(row.transfer_status ?? '');
          const transferFailed = ['failed', 'busy', 'no-answer', 'canceled', 'cancelled'].includes(row.transfer_status ?? '');
          const terminalAt = row.provider_terminal_at ?? new Date().toISOString();
          const missingExpired = Date.now() - new Date(terminalAt).valueOf() >= 120_000;
          const finalStatus = connected ? 'completed' : transferFailed || missingExpired ? 'failed' : 'calling';
          await this.env.DB.prepare(
            `UPDATE campaign_numbers SET status=?,dispatch_state=CASE WHEN ?='calling' THEN dispatch_state ELSE 'finished' END,
             provider_status=?,duration_seconds=?,answered_by=COALESCE(?,answered_by),
             provider_terminal_at=COALESCE(provider_terminal_at,?),
             outcome_reason=CASE WHEN ?='failed' AND transfer_status IS NULL THEN 'transfer_status_missing' ELSE outcome_reason END,
             error_message=CASE WHEN ?='failed' AND transfer_status IS NULL THEN 'Twilio transfer status was not received' ELSE error_message END,
             processed_at=CASE WHEN ?='calling' THEN processed_at ELSE COALESCE(processed_at,?) END,
             next_action_at=CASE WHEN ?='calling' THEN ? ELSE next_action_at END,
             cost_reconcile_status='pending',cost_next_retry_at=COALESCE(cost_next_retry_at,?),updated_at=? WHERE id=? AND status='calling'`,
          ).bind(finalStatus, finalStatus, result.status, result.duration, result.answeredBy, terminalAt,
            finalStatus, finalStatus, finalStatus, new Date().toISOString(), finalStatus,
            new Date(Date.now() + 30_000).toISOString(), new Date(Date.now() + 60_000).toISOString(),
            new Date().toISOString(), row.id).run();
          continue;
        }
        const status = SUCCESS_PROVIDER_STATES.has(result.status) ? 'completed' : 'failed';
        await this.env.DB.prepare(
          `UPDATE campaign_numbers SET status = ?, dispatch_state = 'finished', provider_status = ?,
           duration_seconds = ?, answered_by = ?, error_message = ?, processed_at = ?, updated_at = ?
           WHERE id = ? AND status = 'calling'`,
        ).bind(status, result.status, result.duration, result.answeredBy, result.errorMessage,
          new Date().toISOString(), new Date().toISOString(), row.id).run();
      } catch {
        // A failed status read is safe to retry; it never creates another call.
      }
    }
  }

  private async reconcileTwilioCosts(campaign: CampaignDispatchRow): Promise<void> {
    const rows = await this.env.DB.prepare(
      `SELECT id,call_sid,transfer_call_sid,cost_reconcile_attempts,
              COALESCE(provider_terminal_at,processed_at,updated_at) cost_base_at
         FROM campaign_numbers
        WHERE campaign_id=? AND status IN ('completed','failed') AND call_sid IS NOT NULL
          AND cost_reconcile_status='pending' AND cost_next_retry_at<=? ORDER BY id LIMIT 10`,
    ).bind(campaign.id, new Date().toISOString()).all<{
      id: number; call_sid: string; transfer_call_sid: string | null; cost_reconcile_attempts: number; cost_base_at: string;
    }>();
    if (!rows.results.length) return;
    const [accountSid, authToken] = await getUserTwilioCredentials(this.env, this.env.DB, campaign.user_id);
    const service = TwilioService(this.env, accountSid, authToken);
    const retryDelays = [5 * 60_000, 15 * 60_000, 60 * 60_000, 6 * 60 * 60_000, 24 * 60 * 60_000];
    for (const row of rows.results) {
      const parent = await service.getCallDetails(row.call_sid);
      const child = row.transfer_call_sid ? await service.getCallDetails(row.transfer_call_sid) : null;
      const pricesReady = parent?.price !== null && parent?.price !== undefined
        && (!row.transfer_call_sid || (child?.price !== null && child?.price !== undefined));
      if (pricesReady) {
        const cost = Number(parent!.price ?? 0) + Number(child?.price ?? 0);
        await this.env.DB.prepare(
          `UPDATE campaign_numbers SET cost=?,cost_reconcile_status='reconciled',cost_reconciled_at=?,
           cost_next_retry_at=NULL,updated_at=? WHERE id=?`,
        ).bind(cost, new Date().toISOString(), new Date().toISOString(), row.id).run();
      } else {
        const attempts = row.cost_reconcile_attempts + 1;
        if (attempts >= 6) {
          await this.env.DB.prepare(
            `UPDATE campaign_numbers SET cost_reconcile_status='unconfirmed',cost_reconcile_attempts=?,
             cost_next_retry_at=NULL,updated_at=? WHERE id=?`,
          ).bind(attempts, new Date().toISOString(), row.id).run();
        } else {
          const baseAt = new Date(row.cost_base_at).valueOf();
          const retryAt = Math.max(Date.now() + 1_000, baseAt + retryDelays[attempts - 1]);
          await this.env.DB.prepare(
            `UPDATE campaign_numbers SET cost_reconcile_attempts=?,cost_next_retry_at=?,updated_at=? WHERE id=?`,
          ).bind(attempts, new Date(retryAt).toISOString(),
            new Date().toISOString(), row.id).run();
        }
      }
    }
    await this.env.DB.prepare(
      'UPDATE campaigns SET total_cost=(SELECT COALESCE(SUM(cost),0) FROM campaign_numbers WHERE campaign_id=?) WHERE id=?',
    ).bind(campaign.id, campaign.id).run();
  }

  private async schedulePendingCostReconciliation(campaignId: number): Promise<void> {
    const next = await this.env.DB.prepare(
      `SELECT MIN(cost_next_retry_at) next_at FROM campaign_numbers
        WHERE campaign_id=? AND cost_reconcile_status='pending' AND cost_next_retry_at IS NOT NULL`,
    ).bind(campaignId).first<{ next_at: string | null }>();
    if (next?.next_at) await this.ctx.storage.setAlarm(Math.max(Date.now() + 1_000, new Date(next.next_at).valueOf()));
  }
}
