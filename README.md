# ColdCalls Platform

Plataforma web para gerenciamento de campanhas de cold calls via Twilio.

## Stack Tecnologica

- **Backend**: Python + FastAPI
- **Database**: SQLite (SQLAlchemy ORM)
- **Frontend**: HTML + TailwindCSS + Alpine.js (Jinja2 templates)
- **Audios**: Cloudflare R2
- **Pagamentos**: USDT (ERC-20) via Etherscan API

## Estrutura do Projeto

```
coldcalls/
├── app/
│   ├── main.py              # FastAPI application
│   ├── config.py            # Pydantic settings
│   ├── database.py          # SQLAlchemy setup
│   ├── models.py            # Database models
│   ├── schemas.py           # Pydantic schemas
│   ├── auth.py              # JWT, bcrypt, encryption
│   ├── dependencies.py      # FastAPI dependencies
│   ├── routers/
│   │   ├── auth.py          # Login/registro
│   │   ├── dashboard.py     # Dashboard do usuario
│   │   ├── campaigns.py     # CRUD campanhas
│   │   ├── payments.py      # Depositos USDT
│   │   ├── admin.py         # Gerenciamento admin
│   │   └── api.py           # API JSON
│   ├── services/
│   │   ├── twilio_service.py    # Logica de chamadas
│   │   ├── payment_service.py   # Verificacao Etherscan
│   │   ├── r2_service.py        # Upload de audios
│   │   └── campaign_worker.py   # Worker de processamento
│   ├── templates/           # Templates Jinja2
│   └── static/              # CSS/JS
├── worker.py                # Entry point do worker
├── requirements.txt
└── .env.example
```

## Instalacao

```bash
# 1. Clonar repositorio
git clone <repo-url>
cd coldcalls

# 2. Criar ambiente virtual
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# ou: venv\Scripts\activate  # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variaveis de ambiente
cp .env.example .env
# Editar .env com suas configuracoes
```

## Configuracao (.env)

```env
# Aplicacao
SECRET_KEY=sua-chave-secreta-min-32-chars
DEBUG=false

# JWT
JWT_SECRET=jwt-secret-min-32-chars

# Encryption (gerar com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY=sua-chave-fernet

# Admin inicial
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=senha-segura

# Cloudflare R2 (para audios)
R2_ACCOUNT_ID=xxx
R2_ACCESS_KEY_ID=xxx
R2_SECRET_ACCESS_KEY=xxx
R2_BUCKET_NAME=coldcalls-audios
R2_PUBLIC_URL=https://seu-bucket.r2.dev

# Etherscan (para verificacao USDT)
ETHERSCAN_API_KEY=xxx
USDT_WALLET_ADDRESS=0xSuaWallet
```

## Executando

### Aplicacao Web

```bash
# Desenvolvimento
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Producao
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

### Worker de Campanhas

```bash
# Em outro terminal
python worker.py
```

O worker processa campanhas com status "running" a cada 10 segundos.

## Uso

### 1. Primeiro Acesso

- Acesse `http://localhost:8000`
- Faca login com as credenciais admin definidas no `.env`
- O admin e criado automaticamente no primeiro startup

### 2. Configuracao Admin

1. Acesse `/admin`
2. Configure credenciais **Twilio** globais
3. Adicione **Paises** com precos por minuto
4. Adicione **Caller IDs** (numeros de origem)
5. Faca upload de **Audios** para o R2

### 3. Usuarios

1. Admin cria usuarios em `/admin/users` (max 4 usuarios)
2. Configuram numero de transferencia em `/dashboard/settings`
3. Adicionam creditos via deposito USDT em `/payments/deposit`

### 4. Campanhas

1. Criar campanha em `/campaigns/create`
2. Selecionar pais, caller ID, audio
3. Upload lista de numeros (formato E.164: +5511999999999)
4. Iniciar campanha
5. Worker processa as chamadas automaticamente

## API Endpoints

### Autenticacao
- `GET/POST /auth/login` - Login
- `GET/POST /auth/register` - Desabilitado (redireciona para login)
- `GET /auth/logout` - Logout

### Dashboard
- `GET /dashboard` - Dashboard principal
- `GET/POST /dashboard/settings` - Configuracoes de transferencia (3CX)

### Campanhas
- `GET /campaigns` - Listar campanhas
- `GET/POST /campaigns/create` - Criar campanha
- `GET /campaigns/{id}` - Detalhes da campanha
- `POST /campaigns/{id}/start` - Iniciar
- `POST /campaigns/{id}/pause` - Pausar
- `POST /campaigns/{id}/cancel` - Cancelar

### Pagamentos
- `GET /payments` - Historico
- `GET /payments/deposit` - Instrucoes de deposito
- `POST /payments/verify` - Verificar TX

### API JSON
- `GET /api/stats` - Estatisticas do usuario
- `GET /api/campaigns/{id}/progress` - Progresso da campanha
- `GET /api/data/countries` - Lista de paises
- `GET /api/data/caller-ids` - Lista de caller IDs
- `GET /api/data/audios` - Lista de audios

## Deploy (VPS Linux)

```bash
# Instalar dependencias do sistema
sudo apt update
sudo apt install python3.11 python3.11-venv nginx

# Criar usuario
sudo useradd -m coldcalls
sudo su - coldcalls

# Setup aplicacao
git clone <repo> ~/app
cd ~/app
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Editar .env

# Systemd service (app)
sudo tee /etc/systemd/system/coldcalls.service << EOF
[Unit]
Description=ColdCalls Platform
After=network.target

[Service]
User=coldcalls
WorkingDirectory=/home/coldcalls/app
ExecStart=/home/coldcalls/app/venv/bin/gunicorn app.main:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Systemd service (worker)
sudo tee /etc/systemd/system/coldcalls-worker.service << EOF
[Unit]
Description=ColdCalls Worker
After=network.target

[Service]
User=coldcalls
WorkingDirectory=/home/coldcalls/app
ExecStart=/home/coldcalls/app/venv/bin/python worker.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Iniciar servicos
sudo systemctl enable coldcalls coldcalls-worker
sudo systemctl start coldcalls coldcalls-worker

# Nginx reverse proxy
sudo tee /etc/nginx/sites-available/coldcalls << EOF
server {
    listen 80;
    server_name seu-dominio.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/coldcalls /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# SSL com Let's Encrypt
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d seu-dominio.com
```

## Seguranca

- Senhas hasheadas com bcrypt
- Credenciais Twilio encriptadas com Fernet
- JWT com expiracao de 24h
- Cookies httponly
- Limite de 4 usuarios

## Licenca

MIT
