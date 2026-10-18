#!/usr/bin/env bash
# Install Stripe CLI to /usr/local/bin/stripe (Linux amd64). Idempotent.
set -euo pipefail
VER="${STRIPE_CLI_VERSION:-1.54.0}"
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64) ARCH=linux_x86_64 ;;
  aarch64|arm64) ARCH=linux_arm64 ;;
  *) echo "Unsupported arch: ${ARCH}" >&2; exit 1 ;;
esac
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
URL="https://github.com/stripe/stripe-cli/releases/download/v${VER}/stripe_${VER}_${ARCH}.tar.gz"
curl -fsSL "${URL}" -o "${TMP}/stripe.tgz"
tar -xzf "${TMP}/stripe.tgz" -C "${TMP}"
install -m 0755 "${TMP}/stripe" /usr/local/bin/stripe
stripe version
