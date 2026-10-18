#!/usr/bin/env bash
# Server-side bootstrap for LeadHunter (run on VPS as root)
set -euo pipefail

APP_DIR=/opt/lead-hunter
REPO_URL="${REPO_URL:-https://github.com/machinekill64-commits/tgkras.git}"

echo "==> LeadHunter deploy"

mkdir -p "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git fetch --all
  git reset --hard origin/main || git reset --hard origin/master
else
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# OmniRoute (global)
if ! command -v omniroute >/dev/null 2>&1; then
  npm install -g omniroute
fi

# Headless OmniRoute setup (idempotent-ish)
export INITIAL_PASSWORD="${OMNIROUTE_PASSWORD:-LeadHunterOmni2026}"
omniroute setup --non-interactive --password "$INITIAL_PASSWORD" || true
# Free Claude via Kiro (no API key) + Pollinations fallback
omniroute setup --non-interactive --add-provider --provider kiro || true
omniroute setup --non-interactive --add-provider --provider pollinations || true

mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

# systemd units
cp "$APP_DIR/deploy/omniroute.service" /etc/systemd/system/omniroute.service
cp "$APP_DIR/deploy/lead-hunter.service" /etc/systemd/system/lead-hunter.service
cp "$APP_DIR/deploy/lead-hunter-acc2.service" /etc/systemd/system/lead-hunter-acc2.service
systemctl daemon-reload
systemctl enable omniroute.service lead-hunter.service lead-hunter-acc2.service
systemctl restart omniroute.service
sleep 3
systemctl restart lead-hunter.service
# acc2 запускаем только если его env с прокси заполнен — иначе юнит
# уйдёт в restart-loop без session/прокси
if [ -f "$APP_DIR/acc2.env" ]; then
  chmod 600 "$APP_DIR/acc2.env"
  systemctl restart lead-hunter-acc2.service
else
  echo "==> Пропуск acc2: нет $APP_DIR/acc2.env (скопируйте acc2.env.example и заполните)"
fi

systemctl --no-pager --full status omniroute.service || true
systemctl --no-pager --full status lead-hunter.service || true
systemctl --no-pager --full status lead-hunter-acc2.service || true

echo "==> Done. Ensure $APP_DIR/.env and work_account.session exist."
