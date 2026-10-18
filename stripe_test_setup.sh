#!/usr/bin/env bash
# stripe_test_setup.sh — run ON the VPS as root (or sudo) AFTER pasting sk_test_ into .env
# Does NOT invent keys. Forbidden: sk_live_*, ovh_domain.py --buy.
set -euo pipefail

APP_DIR="/opt/waas-factory"
ENV_FILE="${APP_DIR}/.env"
SITES_DIR="${SITES_DIR:-/var/www/sites}"
FORWARD_TO="http://127.0.0.1:8000/webhook/stripe"
# Stripe CLI ≥1.54 requires --events (or --all-snapshot / --all-thin)
STRIPE_EVENTS="checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,invoice.payment_failed,customer.subscription.deleted,customer.subscription.updated"
STRIPE_BIN="${STRIPE_BIN:-/usr/local/bin/stripe}"
UNIT="stripe-listen-test.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash ${APP_DIR}/stripe_test_setup.sh" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
# Parse KEY=VAL lines only (ignore comments/blank)
# shellcheck disable=SC1091
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}" || true)
set +a

KEY="${STRIPE_SECRET_KEY:-}"
if [[ -z "${KEY}" || "${KEY}" != sk_test_* ]]; then
  echo "ERROR: STRIPE_SECRET_KEY must start with sk_test_." >&2
  echo "Edit ${ENV_FILE}, set:" >&2
  echo "  STRIPE_SECRET_KEY=sk_test_…" >&2
  echo "Current prefix: ${KEY:0:20}" >&2
  exit 1
fi

if [[ ! -x "${STRIPE_BIN}" ]]; then
  echo "ERROR: Stripe CLI not found at ${STRIPE_BIN}" >&2
  exit 1
fi

echo "==> Fetching webhook signing secret (stripe listen --print-secret)"
# --print-secret exits after printing whsec_… (needs valid sk_test_)
WHSEC="$("${STRIPE_BIN}" listen --api-key "${KEY}" --print-secret 2>/dev/null | tr -d '\r' | tail -n1)"
if [[ -z "${WHSEC}" || "${WHSEC}" != whsec_* ]]; then
  echo "ERROR: could not obtain whsec_ (got: ${WHSEC:-empty})" >&2
  exit 1
fi
echo "    Got ${WHSEC:0:10}…"

echo "==> Writing STRIPE_WEBHOOK_SECRET into ${ENV_FILE}"
TS="$(date +%Y%m%d_%H%M%S)"
cp -a "${ENV_FILE}" "${ENV_FILE}.bak.${TS}"
if grep -q '^STRIPE_WEBHOOK_SECRET=' "${ENV_FILE}"; then
  sed -i "s|^STRIPE_WEBHOOK_SECRET=.*|STRIPE_WEBHOOK_SECRET=${WHSEC}|" "${ENV_FILE}"
else
  echo "STRIPE_WEBHOOK_SECRET=${WHSEC}" >> "${ENV_FILE}"
fi
chown waas:waas "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

echo "==> Enable/start ${UNIT} + restart waas-webhook"
systemctl daemon-reload
systemctl enable "${UNIT}"
systemctl restart waas-webhook.service
systemctl restart "${UNIT}"
sleep 2
systemctl --no-pager -l status "${UNIT}" | head -20 || true
systemctl --no-pager -l status waas-webhook.service | head -15 || true

echo "==> stripe trigger (TEST events)"
for ev in checkout.session.completed invoice.payment_failed customer.subscription.deleted; do
  echo "--- trigger ${ev} ---"
  "${STRIPE_BIN}" trigger "${ev}" --api-key "${KEY}" || echo "WARN: trigger ${ev} failed"
done

echo "==> Recent stripe-listen / waas-webhook logs"
journalctl -u "${UNIT}" -u waas-webhook.service -n 80 --no-pager || true

echo "==> Site activation state under ${SITES_DIR}"
if [[ -d "${SITES_DIR}" ]]; then
  find "${SITES_DIR}" -maxdepth 2 -name '.activated' -printf '%p\n' 2>/dev/null || true
  echo "--- .activated contents (if any) ---"
  find "${SITES_DIR}" -maxdepth 2 -name '.activated' -exec echo "### {}" \; -exec cat {} \; 2>/dev/null || true
  echo "(Note: generic stripe trigger fixtures may not map to a real slug — check logs above.)"
else
  echo "No ${SITES_DIR}"
fi

echo "==> DONE. Forward target: ${FORWARD_TO}"
echo "    Events: ${STRIPE_EVENTS}"
echo "    Listen: systemctl status ${UNIT}"
echo "    Follow: journalctl -u ${UNIT} -f"
