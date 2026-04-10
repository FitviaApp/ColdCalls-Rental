#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Iniciando deploy..."

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
YELLOW='\033[1;33m'

# Resolve diretório do projeto com fallback para variavel de ambiente.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${DEPLOY_DIR:-$SCRIPT_DIR}"
cd "$PROJECT_DIR"
# shellcheck source=deploy/bin/common.sh
source "${PROJECT_DIR}/deploy/bin/common.sh"
resolve_runtime_binaries "${PROJECT_DIR}"

echo "📁 Projeto: $PROJECT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo -e "${RED}❌ Este diretório não é um repositório Git.${NC}"
    exit 1
fi

DB_FILE="coldcalls.db"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${DB_FILE}.backup.${TIMESTAMP}"
DB_TMP=""
APP_SERVICE="${APP_SERVICE:-coldcalls}"
WORKER_SERVICE="${WORKER_SERVICE:-}"
HAS_WORKER_SERVICE=1
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"
NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-18.231.196.243}"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-coldcalls}"
CONFIGURE_NGINX="${CONFIGURE_NGINX:-1}"
ENSURE_REDIS="${ENSURE_REDIS:-1}"
REDIS_SERVICE="${REDIS_SERVICE:-}"
REDIS_URL_EFFECTIVE=""
HAS_REDIS_SERVICE=1
NGINX_AVAILABLE_PATH="/etc/nginx/sites-available/${NGINX_SITE_NAME}"
NGINX_ENABLED_PATH="/etc/nginx/sites-enabled/${NGINX_SITE_NAME}"
NGINX_TEMPLATE_PATH="${PROJECT_DIR}/deploy/nginx/coldcalls.conf.template"
HAS_NGINX_CONFIG=0

read_env_value() {
    local env_file="$1"
    local key="$2"
    if [ ! -f "$env_file" ]; then
        return
    fi
    grep -E "^${key}=" "$env_file" | tail -n 1 | cut -d'=' -f2-
}

is_local_redis_url() {
    local value="$1"
    if [ -z "$value" ]; then
        return 0
    fi
    case "$value" in
        redis://localhost*|redis://127.0.0.1*|redis://[::1]*|unix://*|redis+socket://*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

detect_systemd_service() {
    local service_name
    for service_name in "$@"; do
        if [ -n "$service_name" ] && systemctl list-unit-files "${service_name}.service" --no-legend 2>/dev/null | grep -q "${service_name}.service"; then
            printf '%s\n' "$service_name"
            return 0
        fi
    done
    return 1
}

install_redis_server_if_needed() {
    if [ "$ENSURE_REDIS" != "1" ]; then
        return 1
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        return 1
    fi
    echo "🧠 Redis não encontrado; instalando redis-server..."
    sudo -n apt-get update -y
    sudo -n apt-get install -y redis-server
    return 0
}

# Backup antes de atualizar código para evitar perda em reset.
if [ -f "$DB_FILE" ]; then
    echo "💾 Backup do banco..."
    cp "$DB_FILE" "$BACKUP_FILE"
fi

# Se o DB estiver versionado no git, preservar cópia para restaurar após sync.
if git ls-files --error-unmatch "$DB_FILE" >/dev/null 2>&1 && [ -f "$DB_FILE" ]; then
    DB_TMP="$(mktemp)"
    cp "$DB_FILE" "$DB_TMP"
    echo -e "${YELLOW}⚠️  ${DB_FILE} está versionado; preservando conteúdo local.${NC}"
fi

echo "📥 Puxando mudanças do GitHub..."
git fetch origin main
git reset --hard origin/main

# Restaura DB local preservado após reset duro.
if [ -n "$DB_TMP" ] && [ -f "$DB_TMP" ]; then
    cp "$DB_TMP" "$DB_FILE"
    rm -f "$DB_TMP"
fi

echo "📦 Atualizando dependências..."
"${PYTHON_BIN}" -m pip install -r requirements.txt --quiet

if [ "$CONFIGURE_NGINX" = "1" ] && [ -f "$NGINX_TEMPLATE_PATH" ] && command -v nginx >/dev/null 2>&1; then
    HAS_NGINX_CONFIG=1
elif [ "$CONFIGURE_NGINX" = "1" ] && [ ! -f "$NGINX_TEMPLATE_PATH" ]; then
    echo -e "${YELLOW}⚠️  Template do nginx não encontrado em ${NGINX_TEMPLATE_PATH}.${NC}"
elif [ "$CONFIGURE_NGINX" = "1" ] && ! command -v nginx >/dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  nginx não detectado; pulando configuração web.${NC}"
fi

if [ -z "$WORKER_SERVICE" ]; then
    for candidate in coldcalls-worker coldcalls_worker worker coldcallsworker; do
        if systemctl list-unit-files "${candidate}.service" --no-legend 2>/dev/null | grep -q "${candidate}.service"; then
            WORKER_SERVICE="$candidate"
            break
        fi
    done
fi

REDIS_URL_EFFECTIVE="$(read_env_value "${PROJECT_DIR}/.env" "REDIS_URL" || true)"
if [ -z "$REDIS_URL_EFFECTIVE" ]; then
    REDIS_URL_EFFECTIVE="redis://localhost:6379/0"
fi

if is_local_redis_url "$REDIS_URL_EFFECTIVE"; then
    if [ -z "$REDIS_SERVICE" ]; then
        REDIS_SERVICE="$(detect_systemd_service redis-server redis || true)"
    fi

    if [ -z "$REDIS_SERVICE" ]; then
        install_redis_server_if_needed || true
        REDIS_SERVICE="$(detect_systemd_service redis-server redis || true)"
    fi

    if [ -z "$REDIS_SERVICE" ]; then
        HAS_REDIS_SERVICE=0
        echo -e "${RED}❌ Redis é obrigatório para campanhas IA e não foi encontrado neste servidor.${NC}"
        echo -e "${YELLOW}   Configure REDIS_URL para um Redis remoto ou instale redis-server localmente.${NC}"
        exit 1
    fi
fi

if [ -z "$WORKER_SERVICE" ]; then
    HAS_WORKER_SERVICE=0
    echo -e "${YELLOW}⚠️  Serviço do worker não detectado. Deploy seguirá apenas com app.${NC}"
fi

if [ "$HAS_NGINX_CONFIG" -eq 1 ]; then
    echo "🌐 Atualizando configuração do nginx..."
    TMP_NGINX_CONF="$(mktemp)"
    sed \
        -e "s|__SERVER_NAME__|${NGINX_SERVER_NAME}|g" \
        -e "s|__APP_HOST__|${APP_HOST}|g" \
        -e "s|__APP_PORT__|${APP_PORT}|g" \
        -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        "$NGINX_TEMPLATE_PATH" > "$TMP_NGINX_CONF"

    sudo -n install -m 644 "$TMP_NGINX_CONF" "$NGINX_AVAILABLE_PATH"
    sudo -n ln -sfn "$NGINX_AVAILABLE_PATH" "$NGINX_ENABLED_PATH"
    sudo -n nginx -t
    sudo -n systemctl reload nginx
    rm -f "$TMP_NGINX_CONF"
fi

echo "🔄 Reiniciando serviços..."
if [ "$HAS_REDIS_SERVICE" -eq 1 ] && [ -n "$REDIS_SERVICE" ]; then
    sudo -n systemctl enable --now "$REDIS_SERVICE"
fi
sudo -n systemctl restart "$APP_SERVICE"
if [ "$HAS_WORKER_SERVICE" -eq 1 ]; then
    sudo -n systemctl restart "$WORKER_SERVICE"
fi

sleep 3

if [ "$HAS_REDIS_SERVICE" -eq 1 ] && [ -n "$REDIS_SERVICE" ]; then
    if systemctl is-active --quiet "$REDIS_SERVICE"; then
        echo -e "${GREEN}✅ Redis: RODANDO${NC}"
    else
        echo -e "${RED}❌ Redis: ERRO${NC}"
        sudo journalctl -u "$REDIS_SERVICE" -n 20 --no-pager
        exit 1
    fi
fi

if systemctl is-active --quiet "$APP_SERVICE"; then
    echo -e "${GREEN}✅ App: RODANDO${NC}"
else
    echo -e "${RED}❌ App: ERRO${NC}"
    sudo journalctl -u "$APP_SERVICE" -n 20 --no-pager
    exit 1
fi

if [ "$HAS_WORKER_SERVICE" -eq 0 ]; then
    echo -e "${YELLOW}⚠️  Worker: não configurado neste servidor.${NC}"
elif systemctl is-active --quiet "$WORKER_SERVICE"; then
    echo -e "${GREEN}✅ Worker: RODANDO${NC}"
else
    echo -e "${RED}❌ Worker: ERRO${NC}"
    sudo journalctl -u "$WORKER_SERVICE" -n 20 --no-pager
    exit 1
fi

echo ""
echo -e "${GREEN}Deploy concluído.${NC}"
if [ -f "$BACKUP_FILE" ]; then
    echo "🗂️  Backup do banco: $BACKUP_FILE"
fi
if [ "$HAS_NGINX_CONFIG" -eq 1 ]; then
    echo "🌍 App publicada em: http://${NGINX_SERVER_NAME}"
fi
