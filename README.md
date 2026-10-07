# ColdCalls Rental

Aplicação nativa para Cloudflare Workers, escrita em Hono e TypeScript. O Worker mantém os contratos HTTP da aplicação FastAPI anterior e usa serviços gerenciados da Cloudflare:

- D1 como banco autoritativo;
- R2 para áudios privados;
- Durable Objects para coordenação de campanhas e sessões realtime;
- Workers Static Assets para CSS;
- Cron Trigger para reconciliação de campanhas.

Não há Container, Redis, Queue, Workflow ou processo Python na arquitetura ativa. O código Python, Docker e systemd permanece temporariamente no repositório apenas como referência de paridade e será retirado depois do checkpoint de aceitação.

## Desenvolvimento local

Requisitos: Node.js 20+ e uma sessão autenticada do Wrangler.

```sh
npm install
cp .env.example .dev.vars
npm run types:generate
npx wrangler d1 migrations apply coldcalls --local
npm run dev
```

Use uma URL pública de túnel em `BASE_URL` para exercitar callbacks de providers. `.dev.vars` é ignorado pelo Git. Credenciais reais nunca devem ser colocadas em `.env`, código-fonte, logs ou `vars`.

## Validação

```sh
npm run check
```

O comando verifica os tipos gerados pelo Wrangler, pré-compila os templates Nunjucks, executa TypeScript, roda Vitest no runtime Cloudflare com D1/R2/Assets/Durable Objects locais, faz o dry-run do bundle e verifica whitespace do diff.

A suíte cobre o manifesto das 98 rotas legadas, autenticação e compatibilidade Fernet, isolamento entre tenants, R2 privado, bindings e saúde do Worker, callbacks assinados e deduplicados, além de dispatch conhecido/ambíguo, recuperação e alarmes do coordenador.

## Persistência e deploy

As migrations ficam em `migrations/`. O deploy de produção sempre aplica as migrations e faz readback antes de publicar:

```sh
npm run deploy:production
```

O bootstrap inicial exige `ADMIN_EMAIL` e solicita a senha sem eco:

```sh
ADMIN_EMAIL='admin@example.com' node scripts/bootstrap_d1.mjs --local
```

Para a recriação remota, rotação de segredos, primeiro deploy em `workers.dev` e configuração do Workers Builds, siga [docs/CLOUDFLARE_RELEASE.md](docs/CLOUDFLARE_RELEASE.md). Essas operações são deliberadamente separadas do desenvolvimento local porque alteram GitHub e recursos persistentes da Cloudflare.

## Campanhas

`CampaignCoordinator` mantém um objeto por campanha, aplica concorrência e intervalo por provider e usa alarmes idempotentes. O claim acontece primeiro no D1. Falhas explicitamente rejeitadas terminam em `failed`; resultado externo ambíguo termina em `dispatch_unknown` e nunca é rediscado automaticamente.

Twilio, SignalWire, Telnyx, Vonage e Voximplant implementam o contrato `VoiceProviderAdapter` para campanhas de áudio. Campanhas de IA permanecem restritas à Twilio.

## Realtime

`RealtimeCallSession` mantém um objeto por `campaign_number_id`, autentica o Media Stream da Twilio, conecta ao OpenAI Realtime, sintetiza a transcrição com ElevenLabs, executa handoff pela atualização da chamada Twilio e grava observabilidade no D1. Como existe um WebSocket de saída durante a chamada, a sessão não depende de hibernação.

Testes locais e smoke HTTP não comprovam chamadas telefônicas reais, aceitação dos providers, DTMF, mídia telefônica ou qualidade da voz realtime.
