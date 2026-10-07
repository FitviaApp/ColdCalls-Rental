import type { Env } from '../middleware';
import { validatePublicCallbackUrl } from './callback_url_service';
import { hasUserElevenlabsCredentials } from './user_elevenlabs_service';
import { hasUserOpenaiCredentials } from './user_openai_service';
import { hasUserTwilioCredentials } from './user_twilio_service';

export interface AiSchemaHealth {
  ready: boolean;
  missing_items: string[];
}

export interface AiCampaignReadiness {
  ok: boolean;
  error: string | null;
  schema_ready: boolean;
  missing_schema_items: string[];
}

const PROVIDER_LABELS: Record<string, string> = { twilio: 'Twilio' };

// ponytail: schema vem de migrations versionadas no repo — não há drift como no init_db() do container.
export async function getAiSchemaHealth(_env?: Env): Promise<AiSchemaHealth> {
  return { ready: true, missing_items: [] };
}

export async function getAiCampaignReadiness(
  env: Env,
  opts: {
    userId: number;
    voiceProvider: string;
    aiAgent?: { is_active?: number | boolean } | null;
    requireSchema?: boolean;
    requireActiveAgent?: boolean;
  },
): Promise<AiCampaignReadiness> {
  const requireSchema = opts.requireSchema !== false;
  const requireActiveAgent = opts.requireActiveAgent !== false;
  const provider = (opts.voiceProvider ?? '').trim().toLowerCase();
  const providerLabel =
    PROVIDER_LABELS[provider] ?? (provider.charAt(0).toUpperCase() + provider.slice(1) || 'provider');

  const schemaHealth = await getAiSchemaHealth(env);
  const fail = (error: string): AiCampaignReadiness => ({
    ok: false,
    error,
    schema_ready: schemaHealth.ready,
    missing_schema_items: schemaHealth.missing_items,
  });

  if (requireSchema && !schemaHealth.ready) {
    return {
      ok: false,
      error:
        'Database schema is outdated for AI campaigns. Restart the app and worker so ' +
        'init_db() can apply the latest AI schema updates.',
      schema_ready: false,
      missing_schema_items: schemaHealth.missing_items,
    };
  }

  try {
    validatePublicCallbackUrl(env.BASE_URL ?? '', providerLabel);
  } catch (e) {
    return fail(String(e instanceof Error ? e.message : e));
  }

  if (provider !== 'twilio') {
    return fail('AI agent campaigns currently require Twilio as the voice provider.');
  }

  if (!(await hasUserTwilioCredentials(env, env.DB, opts.userId))) {
    return fail('Please configure Twilio credentials in Settings first');
  }
  if (!(await hasUserOpenaiCredentials(env, env.DB, opts.userId))) {
    return fail('Please configure OpenAI credentials in Settings first');
  }
  if (!(await hasUserElevenlabsCredentials(env, env.DB, opts.userId))) {
    return fail('Please configure ElevenLabs credentials in Settings first');
  }

  if (requireActiveAgent && !opts.aiAgent?.is_active) {
    return fail('Selected AI agent is inactive');
  }

  return {
    ok: true,
    error: null,
    schema_ready: schemaHealth.ready,
    missing_schema_items: schemaHealth.missing_items,
  };
}
