#!/bin/bash
# setup.sh — Run once on fresh Aliyun Singapore Ubuntu 22.04 VM
# Usage: bash setup.sh <dashboard-password>
set -euo pipefail

DASHBOARD_PASS="${1:?Usage: bash setup.sh <dashboard-password>}"
REPO_DIR="/opt/quant_trade_competition"
VENV_DIR="/opt/quant_venv"
LOG_DIR="/var/log/trading"
SERVICE_USER="trader"

echo "==> [1/8] System packages"
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv git nginx apache2-utils curl

echo "==> [2/8] Create service user"
id -u $SERVICE_USER &>/dev/null || useradd -r -m -s /bin/bash $SERVICE_USER

echo "==> [3/8] Clone / pull repo"
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull --ff-only
else
    # Replace with your actual repo URL
    git clone https://github.com/YOUR_USERNAME/quant_trade_competition.git "$REPO_DIR"
fi
chown -R $SERVICE_USER:$SERVICE_USER "$REPO_DIR"

echo "==> [4/8] Python virtualenv + dependencies"
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/engine/requirements.txt"
chown -R $SERVICE_USER:$SERVICE_USER "$VENV_DIR"

echo "==> [5/8] OKX credentials"
if [ ! -f /home/$SERVICE_USER/.okx/config.toml ]; then
    mkdir -p /home/$SERVICE_USER/.okx
    echo "  !! Copy your ~/.okx/config.toml to /home/$SERVICE_USER/.okx/config.toml"
    echo "  !! Run: scp ~/.okx/config.toml trader@<server-ip>:/home/$SERVICE_USER/.okx/config.toml"
fi
chown -R $SERVICE_USER:$SERVICE_USER /home/$SERVICE_USER/.okx 2>/dev/null || true

echo "==> [6/8] Log directory"
mkdir -p "$LOG_DIR"
chown $SERVICE_USER:$SERVICE_USER "$LOG_DIR"

echo "==> [7/8] systemd services"
cp "$REPO_DIR/deploy/trading-engine.service"    /etc/systemd/system/
cp "$REPO_DIR/deploy/trading-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable trading-engine trading-dashboard
systemctl restart trading-engine trading-dashboard

echo "==> [8/8] nginx + basic auth"
htpasswd -bc /etc/nginx/.htpasswd trader "$DASHBOARD_PASS"
cp "$REPO_DIR/deploy/nginx.conf" /etc/nginx/sites-available/trading-dashboard
ln -sf /etc/nginx/sites-available/trading-dashboard /etc/nginx/sites-enabled/trading-dashboard
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "=============================="
echo " Setup complete!"
echo " Dashboard: http://$(curl -s ifconfig.me)"
echo " Login:     trader / $DASHBOARD_PASS"
echo ""
echo " Check engine:    systemctl status trading-engine"
echo " Check dashboard: systemctl status trading-dashboard"
echo " Engine logs:     tail -f /var/log/trading/engine.log"
echo "=============================="
