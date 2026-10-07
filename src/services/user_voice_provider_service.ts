import type { Env } from '../middleware';
import { hasUserSignalwireCredentials } from './user_signalwire_service';
import { hasUserTelnyxCredentials } from './user_telnyx_service';
import { hasUserTwilioCredentials } from './user_twilio_service';
import { hasUserVonageCredentials } from './user_vonage_service';
import { hasUserVoximplantCredentials } from './user_voximplant_service';
import { hasUserOpenaiCredentials } from './user_openai_service';
import { hasUserElevenlabsCredentials } from './user_elevenlabs_service';

export const PROVIDER_LABELS: Record<string, string> = {
  twilio: 'Twilio',
  signalwire: 'SignalWire',
  telnyx: 'Telnyx',
  vonage: 'Vonage',
  voximplant: 'Voximplant',
};

export interface VoiceProviderStatus {
  value: string;
  label: string;
  configured: boolean;
  supports_press_1: boolean;
}

export function supportedVoiceProviders(): string[] {
  return ['twilio', 'signalwire', 'telnyx', 'vonage', 'voximplant'];
}

function titleCase(value: string): string {
  return value
    .split('_')
    .map((part) => (part ? part.charAt(0).toUpperCase() + part.slice(1).toLowerCase() : part))
    .join('_');
}

export function providerSupportsPress1(provider: string): boolean {
  const normalized = String(provider ?? '').trim().toLowerCase();
  return ['twilio', 'signalwire', 'voximplant'].includes(normalized);
}

export async function hasUserVoiceProviderCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  provider: string,
): Promise<boolean> {
  const normalized = String(provider ?? '').trim().toLowerCase();
  if (normalized === 'twilio') return hasUserTwilioCredentials(env, db, userId);
  if (normalized === 'signalwire') return hasUserSignalwireCredentials(env, db, userId);
  if (normalized === 'telnyx') return hasUserTelnyxCredentials(env, db, userId);
  if (normalized === 'vonage') return hasUserVonageCredentials(env, db, userId);
  if (normalized === 'voximplant') return hasUserVoximplantCredentials(env, db, userId);
  return false;
}

export async function hasAnyUserVoiceProviderCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  for (const provider of supportedVoiceProviders()) {
    if (await hasUserVoiceProviderCredentials(env, db, userId, provider)) return true;
  }
  return false;
}

export async function getUserVoiceProviderStatus(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<VoiceProviderStatus[]> {
  const providers: VoiceProviderStatus[] = [];
  for (const provider of supportedVoiceProviders()) {
    providers.push({
      value: provider,
      label: PROVIDER_LABELS[provider] ?? titleCase(provider),
      configured: await hasUserVoiceProviderCredentials(env, db, userId, provider),
      supports_press_1: providerSupportsPress1(provider),
    });
  }
  return providers;
}

export async function hasUserAiRuntimeCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  return (
    (await hasUserTwilioCredentials(env, db, userId)) &&
    (await hasUserOpenaiCredentials(env, db, userId)) &&
    (await hasUserElevenlabsCredentials(env, db, userId))
  );
}
