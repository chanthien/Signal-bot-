#!/bin/bash
# deploy.sh — Chạy 1 lần trên VPS Ubuntu để setup toàn bộ
set -e

echo "=== [1/4] Install Python deps ==="
apt-get update -qq
apt-get install -y python3.11 python3.11-pip python3.11-venv

echo "=== [2/4] Setup virtualenv ==="
cd /opt/signal-bot
python3.11 -m venv venv
source venv/bin/activate
pip install --quiet -r requirements.txt

echo "=== [3/4] Setup .env ==="
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  Điền credentials vào /opt/signal-bot/.env trước khi start!"
    echo "    nano /opt/signal-bot/.env"
fi

echo "=== [4/4] Install systemd service ==="
cat > /etc/systemd/system/signal-bot.service << 'SERVICE'
[Unit]
Description=TF Grid Pyramid Signal Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/signal-bot
ExecStart=/opt/signal-bot/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable signal-bot

echo ""
echo "═══════════════════════════════════════════"
echo "✅ Deploy xong!"
echo ""
echo "Bước tiếp theo:"
echo "  1. nano /opt/signal-bot/.env    ← điền credentials"
echo "  2. systemctl start signal-bot   ← start bot"
echo "  3. systemctl status signal-bot  ← check status"
echo "  4. curl localhost:8000/health   ← verify"
echo "═══════════════════════════════════════════"
