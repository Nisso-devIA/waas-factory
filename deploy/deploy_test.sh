#!/usr/bin/env bash
# deploy_test.sh — idempotent TEST deploy for ArtisanVisible WaaS factory.
# Scope: /opt/waas-factory, /var/www/sites (merci), Caddyfile, waas-webhook.service
# Forbidden: live Stripe keys, ovh_domain.py --buy, unrelated services.
set -euo pipefail

APP_USER="waas"
APP_DIR="/opt/waas-factory"
SITES_DIR="/var/www/sites"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CADDY_DEST="/etc/caddy/Caddyfile"
UNIT_SRC="${SRC_DIR}/config/waas-webhook.service"
UNIT_DEST="/etc/systemd/system/waas-webhook.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

echo "==> Ensure user ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> ${APP_DIR} (0700, owned by ${APP_USER})"
mkdir -p "${APP_DIR}"
chmod 0700 "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> Sync project files into ${APP_DIR}"
# Copy app sources only (no venv, no .env with secrets, no __pycache__)
rsync -a --delete \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude 'deploy/' \
  "${SRC_DIR}/" "${APP_DIR}/"

# Keep deploy scripts on the server for re-runs / checklist
mkdir -p "${APP_DIR}/deploy"
cp -a "${SCRIPT_DIR}/." "${APP_DIR}/deploy/"

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 0700 "${APP_DIR}"

echo "==> Python venv + requirements (+ ovh)"
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/venv"
fi
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" ovh

echo "==> Test .env (placeholders only — never overwrite a real .env)"
ENV_EXAMPLE="${APP_DIR}/.env.test.example"
if [[ ! -f "${ENV_EXAMPLE}" ]]; then
  ENV_EXAMPLE="${APP_DIR}/.env.example"
fi
if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${ENV_EXAMPLE}" "${APP_DIR}/.env"
  # Force test posture
  if grep -q '^STRIPE_REQUIRE_TOS=' "${APP_DIR}/.env"; then
    sed -i 's/^STRIPE_REQUIRE_TOS=.*/STRIPE_REQUIRE_TOS=0/' "${APP_DIR}/.env"
  else
    echo 'STRIPE_REQUIRE_TOS=0' >> "${APP_DIR}/.env"
  fi
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 0600 "${APP_DIR}/.env"
  echo "    Created ${APP_DIR}/.env from test example (placeholders)."
else
  echo "    ${APP_DIR}/.env already exists — left untouched."
fi

echo "==> /var/www/sites/merci"
mkdir -p "${SITES_DIR}/merci"
rsync -a "${SRC_DIR}/sites/merci/" "${SITES_DIR}/merci/"
chown -R "${APP_USER}:${APP_USER}" "${SITES_DIR}"
# Sites tree must be readable by Caddy (typically www-data / caddy)
chmod -R u=rwX,go=rX "${SITES_DIR}"

echo "==> systemd waas-webhook.service (uvicorn --workers 1)"
cp "${UNIT_SRC}" "${UNIT_DEST}"
# Ensure workers=1 even if unit drifts
if ! grep -q -- '--workers 1' "${UNIT_DEST}"; then
  sed -i 's|--workers [0-9]*|--workers 1|' "${UNIT_DEST}" || true
fi
systemctl daemon-reload
systemctl enable waas-webhook.service
systemctl restart waas-webhook.service

echo "==> Caddyfile TEST (HTTP :8080 beside Nginx — config/Caddyfile; prod is Caddyfile.prod)"
mkdir -p "$(dirname "${CADDY_DEST}")"
# Backup existing Caddyfile before overwrite
if [[ -f "${CADDY_DEST}" ]]; then
  cp -a "${CADDY_DEST}" "${CADDY_DEST}.bak.$(date +%Y%m%d_%H%M%S)"
fi
cp "${SRC_DIR}/config/Caddyfile" "${CADDY_DEST}"
# Keep prod template on disk for later (Nginx may later relay unknown domains → Caddy)
if [[ -f "${SRC_DIR}/config/Caddyfile.prod" ]]; then
  cp "${SRC_DIR}/config/Caddyfile.prod" "$(dirname "${CADDY_DEST}")/Caddyfile.prod"
fi
if command -v caddy >/dev/null 2>&1; then
  caddy validate --config "${CADDY_DEST}"
  systemctl enable caddy.service 2>/dev/null || true
  if systemctl list-unit-files | grep -q '^caddy.service'; then
    systemctl reload caddy.service 2>/dev/null || systemctl restart caddy.service
  fi
  # Ensure ufw allows 8080 (do not touch SSH/22)
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
    ufw allow 8080/tcp comment "caddy-artisanvisible-test" || true
  fi
else
  echo "WARN: caddy not installed — Caddyfile copied to ${CADDY_DEST} only."
fi

echo "==> DONE (test deploy). Do NOT run ovh_domain.py --buy. Do NOT put live Stripe keys in .env."
systemctl --no-pager --full status waas-webhook.service || true
