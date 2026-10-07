import { Hono, type Context } from 'hono';
import { render } from '../render';
import { type AppEnv, requireRental } from '../middleware';
import {
  createUserAiAgent,
  DEFAULT_AI_AGENT_LANGUAGE,
  DEFAULT_AI_AGENT_MODEL,
  DEFAULT_AI_AGENT_TEMPERATURE,
  getUserAiAgent,
  listUserAiAgents,
  updateUserAiAgent,
  type AiAgentRow,
} from '../services/ai_agent_service';

export const aiAgentRoutes = new Hono<AppEnv>();
aiAgentRoutes.use('*', requireRental);

const truthy = (value: unknown): boolean =>
  ['1', 'true', 'yes', 'on'].includes(String(value ?? '').toLowerCase());

const agentId = (c: Context<AppEnv>): number => Number(c.req.param('id'));

interface AgentFormData {
  name: string;
  system_prompt: string;
  voice_id: string;
  model: string;
  temperature: number;
  language: string;
  handoff_description: string;
  is_active: boolean;
}

function parseAgentForm(body: Record<string, unknown>): AgentFormData {
  const rawTemperature = body.temperature;
  const temperature =
    rawTemperature === undefined
      ? DEFAULT_AI_AGENT_TEMPERATURE
      : String(rawTemperature).trim()
        ? Number(rawTemperature)
        : Number.NaN;
  return {
    name: String(body.name ?? '').trim(),
    system_prompt: String(body.system_prompt ?? '').trim(),
    voice_id: String(body.voice_id ?? '').trim(),
    model: String(body.model ?? DEFAULT_AI_AGENT_MODEL).trim() || DEFAULT_AI_AGENT_MODEL,
    temperature,
    language:
      String(body.language ?? DEFAULT_AI_AGENT_LANGUAGE).trim().toLowerCase() ||
      DEFAULT_AI_AGENT_LANGUAGE,
    handoff_description: String(body.handoff_description ?? '').trim(),
    is_active: truthy(body.is_active),
  };
}

function validationError(formData: AgentFormData): string | null {
  if (!formData.name) return 'Agent name cannot be empty.';
  if (!formData.system_prompt) return 'System prompt cannot be empty.';
  if (!formData.voice_id) return 'Voice ID cannot be empty.';
  if (!formData.model.startsWith('gpt-realtime')) {
    return 'AI agent model must be an OpenAI Realtime model.';
  }
  if (
    !Number.isFinite(formData.temperature) ||
    formData.temperature < 0 ||
    formData.temperature > 2
  ) {
    return 'Temperature must be between 0.0 and 2.0.';
  }
  return null;
}

function formContext(
  c: Context<AppEnv>,
  options: {
    agent?: AiAgentRow | null;
    error?: string | null;
    formData?: AgentFormData;
  } = {},
): Record<string, unknown> {
  return {
    request: { url: c.req.url },
    user: c.get('user'),
    agent: options.agent ?? null,
    error: options.error ?? null,
    form_data: options.formData ?? {},
    default_model: DEFAULT_AI_AGENT_MODEL,
    default_language: DEFAULT_AI_AGENT_LANGUAGE,
    default_temperature: DEFAULT_AI_AGENT_TEMPERATURE,
  };
}

aiAgentRoutes.get('/', async (c) => {
  const agents = await listUserAiAgents(c.env.DB, c.get('user').id);
  return c.html(
    render('ai_agents/list.html', {
      request: { url: c.req.url },
      user: c.get('user'),
      agents,
      created: truthy(c.req.query('created')),
      updated: truthy(c.req.query('updated')),
      toggled: truthy(c.req.query('toggled')),
      error: c.req.query('error') ?? null,
    }),
  );
});

aiAgentRoutes.get('/create', (c) =>
  c.html(render('ai_agents/create.html', formContext(c))),
);

aiAgentRoutes.post('/create', async (c) => {
  const formData = parseAgentForm(await c.req.parseBody());
  const error = validationError(formData);
  if (error) {
    return c.html(
      render('ai_agents/create.html', formContext(c, { error, formData })),
      400,
    );
  }

  await createUserAiAgent(
    c.env.DB,
    c.get('user').id,
    formData.name,
    formData.system_prompt,
    formData.voice_id,
    {
      model: formData.model,
      temperature: formData.temperature,
      language: formData.language,
      handoffDescription: formData.handoff_description,
      isActive: formData.is_active,
    },
  );
  return c.redirect('/ai-agents?created=true', 302);
});

aiAgentRoutes.get('/:id/edit', async (c) => {
  const agent = await getUserAiAgent(c.env.DB, c.get('user').id, agentId(c));
  if (!agent) return c.json({ detail: 'AI agent not found' }, 404);
  return c.html(render('ai_agents/edit.html', formContext(c, { agent })));
});

aiAgentRoutes.post('/:id/edit', async (c) => {
  const agent = await getUserAiAgent(c.env.DB, c.get('user').id, agentId(c));
  if (!agent) return c.json({ detail: 'AI agent not found' }, 404);

  const formData = parseAgentForm(await c.req.parseBody());
  const error = validationError(formData);
  if (error) {
    return c.html(
      render('ai_agents/edit.html', formContext(c, { agent, error, formData })),
      400,
    );
  }

  await updateUserAiAgent(c.env.DB, agent, {
    name: formData.name,
    systemPrompt: formData.system_prompt,
    voiceId: formData.voice_id,
    model: formData.model,
    temperature: formData.temperature,
    language: formData.language,
    handoffDescription: formData.handoff_description,
    isActive: formData.is_active,
  });
  return c.redirect('/ai-agents?updated=true', 302);
});

aiAgentRoutes.post('/:id/toggle', async (c) => {
  const agent = await getUserAiAgent(c.env.DB, c.get('user').id, agentId(c));
  if (!agent) return c.json({ detail: 'AI agent not found' }, 404);

  if (agent.is_active) {
    const runningCampaign = await c.env.DB.prepare(
      `SELECT 1 FROM campaigns
        WHERE ai_agent_id = ? AND campaign_mode = 'ai_agent' AND status = 'running'
        LIMIT 1`,
    )
      .bind(agent.id)
      .first();
    if (runningCampaign) {
      return c.json(
        { detail: 'Pause the running campaign before disabling this agent' },
        400,
      );
    }
  }

  await c.env.DB.prepare(
    'UPDATE ai_agents SET is_active = ?, updated_at = ? WHERE id = ?',
  )
    .bind(agent.is_active ? 0 : 1, new Date().toISOString(), agent.id)
    .run();
  return c.redirect('/ai-agents?toggled=true', 302);
});
