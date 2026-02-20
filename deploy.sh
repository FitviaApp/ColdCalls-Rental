#!/bin/bash
set -e

echo "🚀 Iniciando deploy..."

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

cd /home/ubuntu/coldcalls || exit 1

echo "📥 Puxando mudanças do GitHub..."
git fetch origin main
git reset --hard origin/main

source venv/bin/activate

echo "📦 Atualizando dependências..."
pip install -r requirements.txt --quiet

if [ -f coldcalls.db ]; then
    echo "💾 Backup do banco..."
    cp coldcalls.db coldcalls.db.backup.$(date +%Y%m%d_%H%M%S)
fi

echo "🔄 Reiniciando serviços..."
sudo systemctl restart coldcalls
sudo systemctl restart coldcalls-worker

sleep 3

if systemctl is-active --quiet coldcalls; then
    echo -e "${GREEN}✅ App: RODANDO${NC}"
else
    echo -e "${RED}❌ App: ERRO${NC}"
    sudo journalctl -u coldcalls -n 20 --no-pager
    exit 1
fi

if systemctl is-active --quiet coldcalls-worker; then
    echo -e "${GREEN}✅ Worker: RODANDO${NC}"
else
    echo -e "${RED}❌ Worker: ERRO${NC}"
    sudo journalctl -u coldcalls-worker -n 20 --no-pager
    exit 1
fi

echo ""
echo -e "${GREEN}Deploy concluído.${NC}"
