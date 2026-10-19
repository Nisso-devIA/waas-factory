#!/usr/bin/env bash
# stripe_prices_setup.sh — Crée les prix d'abonnement par palier (devis) en mode TEST.
#   À lancer sur le VPS en root APRÈS stripe_test_setup.sh.
#   Interdit : sk_live_*. N'invente aucune clé. Idempotent (saute les vars déjà posées).
#
#   Paliers : Essentiel 29€ / Visibilité 49€ / Croissance 99€ (mensuel, EUR).
#   « Visibilité » réutilise STRIPE_PRICE_ID (offre 49€ actuelle) s'il est déjà défini.
set -euo pipefail

APP_DIR="/opt/waas-factory"
ENV_FILE="${APP_DIR}/.env"
STRIPE_BIN="${STRIPE_BIN:-/usr/local/bin/stripe}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash ${APP_DIR}/stripe_prices_setup.sh" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090,SC1091
set -a
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}" || true)
set +a

KEY="${STRIPE_SECRET_KEY:-}"
if [[ -z "${KEY}" || "${KEY}" != sk_test_* ]]; then
  echo "ERROR: STRIPE_SECRET_KEY must start with sk_test_ (sk_live_ interdit)." >&2
  echo "Current prefix: ${KEY:0:20}" >&2
  exit 1
fi
if [[ ! -x "${STRIPE_BIN}" ]]; then
  echo "ERROR: Stripe CLI not found at ${STRIPE_BIN}" >&2
  exit 1
fi

set_env() {  # set_env VAR value  → écrit / met à jour dans .env
  local var="$1" val="$2"
  if grep -q "^${var}=" "${ENV_FILE}"; then
    sed -i "s|^${var}=.*|${var}=${val}|" "${ENV_FILE}"
  else
    echo "${var}=${val}" >> "${ENV_FILE}"
  fi
}

create_price() {  # create_price "Nom produit" cents  → echo price_id
  local label="$1" amount="$2" prod price
  prod="$("${STRIPE_BIN}" products create -d "name=${label}" --api-key "${KEY}" 2>/dev/null \
          | grep -oE 'prod_[A-Za-z0-9]+' | head -n1)"
  if [[ -z "${prod}" ]]; then
    echo "ERREUR: produit non créé pour « ${label} »" >&2
    return 1
  fi
  price="$("${STRIPE_BIN}" prices create \
             -d "product=${prod}" \
             -d "unit_amount=${amount}" \
             -d "currency=eur" \
             -d "recurring[interval]=month" \
             --api-key "${KEY}" 2>/dev/null \
          | grep -oE 'price_[A-Za-z0-9]+' | head -n1)"
  if [[ -z "${price}" ]]; then
    echo "ERREUR: prix non créé pour « ${label} »" >&2
    return 1
  fi
  echo "${price}"
}

TS="$(date +%Y%m%d_%H%M%S)"
cp -a "${ENV_FILE}" "${ENV_FILE}.bak.${TS}"
echo "==> Sauvegarde .env → ${ENV_FILE}.bak.${TS}"

# --- Essentiel 29€ ---------------------------------------------------------- #
if [[ -z "${STRIPE_PRICE_ESSENTIEL:-}" ]]; then
  pid="$(create_price "Abonnement Essentiel — hébergement + indexation" 2900)"
  set_env STRIPE_PRICE_ESSENTIEL "${pid}"
  echo "Essentiel  (29€) → ${pid}"
else
  echo "Essentiel  déjà défini (${STRIPE_PRICE_ESSENTIEL})"
fi

# --- Visibilité 49€ (réutilise l'offre existante si possible) --------------- #
if [[ -z "${STRIPE_PRICE_VISIBILITE:-}" ]]; then
  if [[ -n "${STRIPE_PRICE_ID:-}" ]]; then
    set_env STRIPE_PRICE_VISIBILITE "${STRIPE_PRICE_ID}"
    echo "Visibilité (49€) → réutilise STRIPE_PRICE_ID (${STRIPE_PRICE_ID})"
  else
    pid="$(create_price "Abonnement Visibilité — SEO local + Google Business" 4900)"
    set_env STRIPE_PRICE_VISIBILITE "${pid}"
    echo "Visibilité (49€) → ${pid}"
  fi
else
  echo "Visibilité déjà défini (${STRIPE_PRICE_VISIBILITE})"
fi

# --- Croissance 99€ --------------------------------------------------------- #
if [[ -z "${STRIPE_PRICE_CROISSANCE:-}" ]]; then
  pid="$(create_price "Abonnement Croissance — SEO continu + suivi" 9900)"
  set_env STRIPE_PRICE_CROISSANCE "${pid}"
  echo "Croissance (99€) → ${pid}"
else
  echo "Croissance déjà défini (${STRIPE_PRICE_CROISSANCE})"
fi

chown waas:waas "${ENV_FILE}" 2>/dev/null || true
chmod 600 "${ENV_FILE}"

echo "==> Prix écrits dans ${ENV_FILE} :"
grep -E '^STRIPE_PRICE_(ESSENTIEL|VISIBILITE|CROISSANCE)=' "${ENV_FILE}" || true
echo "==> Redémarre le service : systemctl restart waas-webhook.service"
