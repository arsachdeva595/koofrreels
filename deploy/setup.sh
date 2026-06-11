#!/bin/bash
# KoofrReels — Vultr Ubuntu 24.04 setup script
# Run as root on a fresh server: bash setup.sh
set -e

APP_DIR=/opt/koofrreels
APP_USER=koofrreels

echo "==> Installing system dependencies"
apt-get update -q
apt-get install -y python3.12 python3.12-venv python3-pip ffmpeg git nginx

echo "==> Creating app user"
id -u $APP_USER &>/dev/null || useradd --system --shell /bin/bash --home $APP_DIR $APP_USER

echo "==> Cloning / updating repo"
if [ -d "$APP_DIR/.git" ]; then
    git -C $APP_DIR pull
else
    git clone https://github.com/arsachdeva595/koofrreels.git $APP_DIR
fi
chown -R $APP_USER:$APP_USER $APP_DIR

echo "==> Setting up Python venv"
sudo -u $APP_USER python3.12 -m venv $APP_DIR/.venv
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install --quiet --upgrade pip
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install --quiet -r $APP_DIR/requirements.txt

echo "==> Creating data directories"
sudo -u $APP_USER mkdir -p $APP_DIR/{projects,uploads,music}

echo "==> Installing systemd service"
cp $APP_DIR/deploy/koofrreels.service /etc/systemd/system/koofrreels.service
systemctl daemon-reload
systemctl enable koofrreels
systemctl restart koofrreels

echo "==> Configuring nginx"
cp $APP_DIR/deploy/nginx.conf /etc/nginx/sites-available/koofrreels
ln -sf /etc/nginx/sites-available/koofrreels /etc/nginx/sites-enabled/koofrreels
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "==> Done. Next steps:"
echo "    1. Copy your .env file to $APP_DIR/.env"
echo "    2. Run: systemctl restart koofrreels"
echo "    3. Check logs: journalctl -u koofrreels -f"
