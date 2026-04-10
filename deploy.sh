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

echo "📁 Projeto: $PROJECT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo -e "${RED}❌ Este diretório não é um repositório Git.${NC}"
    exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-$CURRENT_BRANCH}"
if [ -z "$DEPLOY_BRANCH" ] || [ "$DEPLOY_BRANCH" = "HEAD" ]; then
    DEPLOY_BRANCH="main"
fi

DB_FILE="coldcalls.db"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${DB_FILE}.backup.${TIMESTAMP}"
DB_TMP=""
APP_SERVICE="${APP_SERVICE:-coldcalls}"
WORKER_SERVICE="${WORKER_SERVICE:-}"
HAS_WORKER_SERVICE=1

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
echo "🌿 Branch de deploy: ${DEPLOY_BRANCH}"
git fetch origin "$DEPLOY_BRANCH"
git reset --hard "origin/${DEPLOY_BRANCH}"

# Restaura DB local preservado após reset duro.
if [ -n "$DB_TMP" ] && [ -f "$DB_TMP" ]; then
    cp "$DB_TMP" "$DB_FILE"
    rm -f "$DB_TMP"
fi

# Cria virtualenv se ele nao existir.
if [ ! -f ".venv/bin/activate" ] && [ ! -f "venv/bin/activate" ]; then
    echo "🐍 Virtualenv não encontrado; criando .venv..."
    python3 -m venv .venv
fi

# Ativa virtualenv (.venv preferido; fallback para venv).
if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
else
    echo -e "${RED}❌ Virtualenv não encontrado (.venv ou venv).${NC}"
    exit 1
fi

echo "📦 Atualizando dependências..."
python -m pip install -r requirements.txt --quiet

if [ -z "$WORKER_SERVICE" ]; then
    for candidate in coldcalls-worker coldcalls_worker worker coldcallsworker; do
        if systemctl list-unit-files "${candidate}.service" --no-legend 2>/dev/null | grep -q "${candidate}.service"; then
            WORKER_SERVICE="$candidate"
            break
        fi
    done
fi

if [ -z "$WORKER_SERVICE" ]; then
    HAS_WORKER_SERVICE=0
    echo -e "${YELLOW}⚠️  Serviço do worker não detectado. Deploy seguirá apenas com app.${NC}"
fi

echo "🔄 Reiniciando serviços..."
sudo -n systemctl restart "$APP_SERVICE"
if [ "$HAS_WORKER_SERVICE" -eq 1 ]; then
    sudo -n systemctl restart "$WORKER_SERVICE"
fi

sleep 3

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
