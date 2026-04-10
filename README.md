# ColdCalls Platform

Plataforma web para gerenciamento de campanhas de cold calls multiusuario com:

- FastAPI + Jinja2
- SQLAlchemy + SQLite
- Twilio, SignalWire, Telnyx, Vonage e Voximplant
- Agentes de IA reutilizaveis com Twilio/SignalWire + OpenAI Chat + ElevenLabs
- Cloudflare R2 (audios)
- Cobranca de aluguel via USDT (verificacao on-chain)

## Stack

- Backend: Python 3.11+ / FastAPI
- Frontend: Jinja2 templates + TailwindCSS + Alpine.js
- Banco: SQLite (`coldcalls.db`)
- Processamento: worker separado (`worker.py`)

## Estrutura do projeto

```text
app/
  main.py                   # App FastAPI e startup
  config.py                 # Configuracoes via .env
  database.py               # Engine, sessao e init de schema
  models.py                 # Modelos SQLAlchemy
  routers/
    auth.py                 # Login/logout
    dashboard.py            # Dashboard e settings do usuario
    campaigns.py            # CRUD e controle de campanhas
    assets.py               # Caller IDs e audios por usuario
    billing.py              # Pagamentos e aluguel
    admin.py                # Painel admin
    api.py                  # Endpoints JSON e TwiML
  services/
    campaign_worker.py      # Loop do worker
    twilio_service.py       # Integracao Twilio
    voximplant_service.py   # Runtime Voximplant
    voximplant_management_service.py  # Provisionamento Voximplant
    payment_service.py      # Verificacao da transacao USDT
    rental_service.py       # Regras de aluguel
    r2_service.py           # Upload/delete no R2
    user_twilio_service.py  # Credenciais Twilio por usuario
    user_voximplant_service.py  # Credenciais Voximplant por usuario
worker.py                   # Entry point do worker
scripts/                    # Scripts de migracao/limpeza legado
requirements.txt
README.md
```

## Requisitos

- Python 3.11+
- Pelo menos um provider de voz configurado por usuario
- Para campanhas com agente IA: Twilio ou SignalWire + credenciais OpenAI + ElevenLabs por usuario
- Bucket Cloudflare R2 (para audios)
- Chave Etherscan (verificacao de pagamento)
- `BASE_URL` publica para callbacks de providers

## Instalacao

```bash
git clone <repo-url>
cd ColdCalls-Rental

python3 -m venv .venv
source .venv/bin/activate   # Linux/Mac
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt
```

## Configuracao (.env)

Crie um arquivo `.env` na raiz do projeto.

```env
# Aplicacao
APP_NAME=ColdCalls Platform
SECRET_KEY=change-me-in-production-min-32-chars
DEBUG=false
# URL publica para callbacks Twilio, SignalWire, Telnyx e Voximplant.
# Em deploy real, nao use localhost ou IP privado.
BASE_URL=https://your-public-domain.example.com
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_DEFAULT_MODEL=gpt-4o-mini
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_REALTIME_URL=wss://api.openai.com/v1/realtime
REDIS_URL=redis://localhost:6379/0
AI_MAX_AGENT_TURNS=6
AI_GATHER_TIMEOUT_SECONDS=3
AI_GATHER_SPEECH_TIMEOUT_SECONDS=1
AI_GATHER_POST_PLAY_PAUSE_SECONDS=0

# Banco
DATABASE_URL=sqlite:///./coldcalls.db

# JWT
JWT_SECRET=jwt-secret-change-me-min-32-chars
JWT_ALGORITHM=HS256
JWT_EXPIRATION_HOURS=24

# Criptografia das credenciais dos providers do usuario (Fernet)
ENCRYPTION_KEY=<32-byte-urlsafe-base64-key>

# Admin inicial (criado automaticamente no primeiro startup)
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-me

# Cloudflare R2
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=coldcalls-audios
R2_PUBLIC_URL=

# Pagamentos (USDT ERC-20)
ETHERSCAN_API_KEY=
USDT_CONTRACT=0xdAC17F958D2ee523a2206206994597C13D831ec7
USDT_WALLET_ADDRESS=

# Limite de usuarios nao-admin
MAX_USERS=4
```

Gerar `ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Executando

### 1) Aplicacao web

```bash
bash deploy/bin/start-app.sh
```

### 2) Worker (em outro terminal)

```bash
bash deploy/bin/start-worker.sh
```

O worker verifica campanhas `running` a cada 10 segundos.
Os wrappers usam `.venv` primeiro e fazem fallback para `venv`.

## Publicando com Nginx

Se quiser expor a aplicacao pelo IP `http://18.231.196.243`, o repositorio agora inclui um template em `deploy/nginx/coldcalls.conf.template` e o `deploy.sh` publica essa configuracao automaticamente no servidor.

Variaveis relevantes:

```bash
APP_HOST=127.0.0.1
APP_PORT=8000
NGINX_SERVER_NAME=18.231.196.243
NGINX_SITE_NAME=coldcalls
CONFIGURE_NGINX=1
```

Ao executar `./deploy.sh`, ele vai:

1. atualizar o codigo
2. instalar dependencias
3. gerar `/etc/nginx/sites-available/coldcalls`
4. criar o link em `/etc/nginx/sites-enabled/coldcalls`
5. validar com `nginx -t`
6. recarregar o `nginx`

Exemplo de upstream esperado:

```nginx
server_name 18.231.196.243;
proxy_pass http://127.0.0.1:8000;
```

Se quiser pular essa etapa em algum ambiente, use `CONFIGURE_NGINX=0`.

## Rodando com systemd

O repositorio inclui templates em `deploy/systemd/` e wrappers em `deploy/bin/`:

- `coldcalls.service` para a aplicacao web
- `coldcalls-worker.service` para o worker
- `start-app.sh` e `start-worker.sh` para resolver o virtualenv de forma consistente

No servidor Ubuntu:

```bash
sudo cp deploy/systemd/coldcalls.service /etc/systemd/system/coldcalls.service
sudo cp deploy/systemd/coldcalls-worker.service /etc/systemd/system/coldcalls-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now coldcalls
sudo systemctl enable --now coldcalls-worker
```

Para acompanhar logs:

```bash
sudo journalctl -u coldcalls -f
sudo journalctl -u coldcalls-worker -f
```

## Fluxo de uso

1. Acesse `http://localhost:8000`.
2. Faca login com o admin definido no `.env`.
3. Em `/admin`, cadastre paises e planos de aluguel.
4. Crie usuarios em `/admin/users`.
5. Cada usuario configura:
   - Numero de transferencia em `/dashboard/settings`
   - Credenciais de voz em `/dashboard/settings`
   - Credenciais OpenAI em `/dashboard/settings`
   - Credenciais ElevenLabs em `/dashboard/settings`
   - Caller IDs em `/assets/caller-ids`
   - Audios em `/assets/audios`
   - Agentes IA reutilizaveis em `/ai-agents`
   - Se usar Voximplant, verifica cada Caller ID pelo fluxo de codigo em `/assets/caller-ids`
6. O usuario paga aluguel em `/billing`.
7. Crie e inicie campanhas em `/campaigns`.
   - `audio`: usa audio gravado e/ou transferencia direta
   - `ai_agent`: usa Twilio ou SignalWire + `/api/ai-runtime/*` + OpenAI Chat + ElevenLabs para conversa e transferencia

## Rotas principais

- Auth: `/auth/login`, `/auth/logout`
- Dashboard: `/dashboard`, `/dashboard/settings`
- AI Agents: `/ai-agents`, `/ai-agents/create`, `/ai-agents/{id}/edit`
- Assets: `/assets/caller-ids`, `/assets/audios`
- Campanhas: `/campaigns`, `/campaigns/create`, `/campaigns/{id}`
- Billing: `/billing`, `POST /billing/verify`
- Admin: `/admin`, `/admin/users`, `/admin/countries`, `/admin/rental-plans`
- API JSON/TwiML:
  - `/api/stats`
  - `/api/campaigns/{id}/progress`
  - `/api/campaigns/{id}/numbers`
  - `/api/data/countries`
  - `/api/data/caller-ids`
  - `/api/data/audios`
  - `/api/twiml/{campaign_id}` (Twilio/SignalWire)
  - `/api/telnyx/texml/{campaign_id}`
  - `/api/ai-runtime/twiml/{campaign_number_id}` (Twilio/SignalWire + IA)
  - `/api/ai-runtime/audio/{campaign_number_id}/{audio_token}`
  - `/api/voximplant/callback`

## Campanhas com Agente IA

Fluxo da v1:

1. O usuario cadastra Twilio ou SignalWire, OpenAI e ElevenLabs em `/dashboard/settings`.
2. O usuario cria um agente reutilizavel em `/ai-agents` com:
   - nome
   - prompt do sistema
   - `voice_id` do ElevenLabs
   - modelo OpenAI Chat (ex.: `gpt-4o-mini`)
   - regra de handoff
3. Em `/campaigns/create`, escolhe `Campaign Mode = AI agent`.
4. A campanha usa Twilio ou SignalWire para originar a chamada.
5. O provider busca `/api/ai-runtime/twiml/{campaign_number_id}` na sua `BASE_URL` publica.
6. O backend executa um loop TwiML/Gather, chama OpenAI Chat para decidir a proxima resposta e sintetiza audio com ElevenLabs.
7. Quando o modelo decide transferir, a chamada vai para o `transfer_number` do usuario.

Observacoes:

- O idioma padrao da v1 e ingles.
- O modo IA nao usa `audio_id`.
- O modo IA nao usa o fluxo `Press 1`.
- `BASE_URL` precisa estar acessivel publicamente para os callbacks `/api/ai-runtime/*`.
- Campanhas IA falham cedo se `BASE_URL` apontar para `localhost`, `.local` ou IP privado.
- O dashboard e `/health` agora mostram estado do worker e prontidao do schema de IA.

## Recuperacao rapida no servidor

Se campanhas IA com Twilio estiverem falhando logo no inicio:

```bash
sudo journalctl -u coldcalls -n 100 --no-pager
sudo journalctl -u coldcalls-worker -n 100 --no-pager
curl -s http://127.0.0.1:8000/health
```

Verifique tambem:

- `/var/www/ColdCalls-Rental/.env` existe
- `BASE_URL` e publica e alcancavel pelo provider
- app e worker subiram ao menos uma vez apos o deploy para aplicar `init_db()`
- o schema contem `campaign_mode`, `ai_agent_id` e as colunas `campaign_numbers.ai_*`

## Voximplant

Para usar a Voximplant por usuario:

1. Gere uma service account na Voximplant.
2. Copie `account_id`, `service_account_email`, `key_id` e `private_key`.
3. Configure esses dados em `/dashboard/settings#voximplant`.
4. O sistema vai tentar provisionar automaticamente:
   - application
   - scenario
   - rule
5. Verifique cada Caller ID em `/assets/caller-ids` antes de criar campanhas Voximplant.

Observacoes da Voximplant:

- `BASE_URL` precisa ser acessivel publicamente para o callback `/api/voximplant/callback`.
- O fluxo implementado usa scenario JavaScript na Voximplant para:
  - originar a chamada PSTN
  - tocar audio do R2
  - opcionalmente pedir `Press 1`
  - transferir para o numero configurado pelo usuario

## Scripts de manutencao legado

Associar assets orfaos a um usuario:

```bash
python3 scripts/assign_orphan_assets.py --email user@example.com --dry-run
python3 scripts/assign_orphan_assets.py --email user@example.com
```

Migrar campanhas legadas para assets corretos:

```bash
python3 scripts/migrate_campaign_assets_to_owners.py --dry-run
python3 scripts/migrate_campaign_assets_to_owners.py
```

Limpar schema legado:

```bash
python3 scripts/cleanup_legacy_schema.py
python3 scripts/cleanup_legacy_schema.py --apply
```

## Observacoes

- O banco e criado automaticamente no startup (`init_db()`), sem Alembic.
- O endpoint `/health` retorna status da aplicacao, do worker e do schema de IA.
- Rotas admin antigas de Caller IDs e Audios estao descontinuadas; a gestao e por usuario em `/assets`.
