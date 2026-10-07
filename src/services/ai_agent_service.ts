
export interface AiAgentRow {
  id: number;
  user_id: number;
  name: string;
  system_prompt: string;
  is_active: number;
  language: string;
  voice_id: string;
  model: string;
  temperature: number;
  handoff_description: string | null;
  created_at: string;
  updated_at: string;
}

export const DEFAULT_AI_AGENT_MODEL = 'gpt-realtime-2.1';
export const DEFAULT_AI_AGENT_LANGUAGE = 'en';
export const DEFAULT_AI_AGENT_TEMPERATURE = 0.7;

export function normalizeAiAgentRealtimeConfig(model: string, voiceId: string): [string, string] {
  let normalizedModel = String(model ?? DEFAULT_AI_AGENT_MODEL).trim();
  const normalizedVoice = String(voiceId ?? '').trim();
  if (!normalizedModel) normalizedModel = DEFAULT_AI_AGENT_MODEL;
  if (!normalizedModel.startsWith('gpt-realtime')) {
    throw new Error('AI agent model must be an OpenAI Realtime model');
  }
  if (!normalizedVoice) throw new Error('Voice cannot be empty');
  return [normalizedModel, normalizedVoice];
}

export async function listUserAiAgents(db: D1Database, userId: number): Promise<AiAgentRow[]> {
  const result = await db
    .prepare('SELECT * FROM ai_agents WHERE user_id = ? ORDER BY is_active DESC, name ASC')
    .bind(userId)
    .all<AiAgentRow>();
  return result.results ?? [];
}

export async function getUserAiAgent(
  db: D1Database,
  userId: number,
  agentId: number,
): Promise<AiAgentRow | null> {
  return db
    .prepare('SELECT * FROM ai_agents WHERE user_id = ? AND id = ?')
    .bind(userId, agentId)
    .first<AiAgentRow>();
}

export interface CreateAiAgentOptions {
  model?: string;
  temperature?: number;
  language?: string;
  handoffDescription?: string;
  isActive?: boolean;
}

export async function createUserAiAgent(
  db: D1Database,
  userId: number,
  name: string,
  systemPrompt: string,
  voiceId: string,
  options: CreateAiAgentOptions = {},
): Promise<AiAgentRow> {
  const [normalizedModel, normalizedVoice] = normalizeAiAgentRealtimeConfig(
    options.model ?? DEFAULT_AI_AGENT_MODEL,
    voiceId,
  );
  const language =
    String(options.language ?? '').trim().toLowerCase() || DEFAULT_AI_AGENT_LANGUAGE;
  const handoff = String(options.handoffDescription ?? '').trim();
  const isActive = options.isActive === undefined || options.isActive ? 1 : 0;
  const row = await db
    .prepare(
      'INSERT INTO ai_agents (user_id, name, system_prompt, voice_id, model, temperature, language, handoff_description, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *',
    )
    .bind(
      userId,
      String(name).trim(),
      String(systemPrompt).trim(),
      normalizedVoice,
      normalizedModel,
      Number(options.temperature ?? DEFAULT_AI_AGENT_TEMPERATURE),
      language,
      handoff || null,
      isActive,
    )
    .first<AiAgentRow>();
  if (!row) throw new Error('Failed to create AI agent');
  return row;
}

export interface UpdateAiAgentChanges {
  name: string;
  systemPrompt: string;
  voiceId: string;
  model: string;
  temperature: number;
  language: string;
  handoffDescription: string;
  isActive: boolean;
}

export async function updateUserAiAgent(
  db: D1Database,
  agent: AiAgentRow,
  changes: UpdateAiAgentChanges,
): Promise<AiAgentRow> {
  const [normalizedModel, normalizedVoice] = normalizeAiAgentRealtimeConfig(
    changes.model,
    changes.voiceId,
  );
  const language =
    String(changes.language ?? '').trim().toLowerCase() || DEFAULT_AI_AGENT_LANGUAGE;
  const handoff = String(changes.handoffDescription ?? '').trim();
  const isActive = Boolean(changes.isActive);
  const temperature = Number(changes.temperature);
  const now = new Date().toISOString();

  await db
    .prepare(
      'UPDATE ai_agents SET name = ?, system_prompt = ?, voice_id = ?, model = ?, temperature = ?, language = ?, handoff_description = ?, is_active = ?, updated_at = ? WHERE id = ?',
    )
    .bind(
      String(changes.name).trim(),
      String(changes.systemPrompt).trim(),
      normalizedVoice,
      normalizedModel,
      temperature,
      language,
      handoff || null,
      isActive ? 1 : 0,
      now,
      agent.id,
    )
    .run();

  return {
    ...agent,
    name: String(changes.name).trim(),
    system_prompt: String(changes.systemPrompt).trim(),
    voice_id: normalizedVoice,
    model: normalizedModel,
    temperature,
    language,
    handoff_description: handoff || null,
    is_active: isActive ? 1 : 0,
    updated_at: now,
  };
}
