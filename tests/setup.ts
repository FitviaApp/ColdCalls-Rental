import { beforeEach, inject } from 'vitest';
import { env } from 'cloudflare:workers';
import { applyD1Migrations, type D1Migration } from 'cloudflare:test';

declare module 'vitest' {
  export interface ProvidedContext { D1_MIGRATIONS: D1Migration[] }
}

beforeEach(async () => {
  await applyD1Migrations(env.DB, inject('D1_MIGRATIONS'));
  for (const table of [
    'provider_events', 'campaign_numbers', 'campaigns', 'ai_agents', 'audios', 'caller_id_provider_status', 'caller_ids',
    'user_twilio_credentials', 'user_signalwire_credentials', 'user_telnyx_credentials',
    'user_vonage_credentials', 'user_voximplant_credentials', 'user_openai_credentials',
    'user_elevenlabs_credentials', 'rental_payments', 'user_rentals', 'rental_plans', 'countries', 'users',
  ]) {
    await env.DB.prepare(`DELETE FROM ${table}`).run();
  }
});
