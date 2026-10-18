# WaaS factory — TEST deploy checklist

Run on the VPS after `sudo bash deploy/deploy_test.sh`. **Test phase only.**

## Forbidden

- Live Stripe keys (`sk_live_*`) or production webhook secrets
- `ovh_domain.py --buy` (or any real domain purchase)
- Writing real secrets into git

## Missing until you fill them (leave placeholders)

- `STRIPE_SECRET_KEY` — paste real `sk_test_…` in `/opt/waas-factory/.env` (placeholder is `COLLER_ICI_sk_test_`)
- `STRIPE_PRICE_ID` — already set to ArtisanVisible test price `price_1URXV5E7ndCMwd4gKHQkksKw` on VPS
- `STRIPE_WEBHOOK_SECRET` — filled by `stripe_test_setup.sh` via `stripe listen --print-secret`
- `GOOGLE_PLACES_API_KEY` — only if calling live Places (mock path does not need it)
- OVH keys — leave empty for TEST
- `CLOUDFLARE_API_TOKEN` — leave empty for TEST (pipeline skips CF with warning). Permissions when ready: see `deploy/CLOUDFLARE_CLIENT_DOMAINS.md` (Zone:Zone:Edit, Zone:DNS:Edit, Zone:Zone Settings:Edit, account `2dc6d88394ebd5dfb147ddf563d7e2eb`, All zones)

## 1. Unit tests as user `waas`

```bash
sudo -u waas -H bash -lc 'cd /opt/waas-factory && ./venv/bin/python -m unittest test_waas.py -v'
```

Expect: `Ran 9 tests` → `OK`

## 2. Service status

```bash
systemctl status waas-webhook --no-pager
```

Expect: `active (running)`

## 3. Caddy :8080 (ArtisanVisible TEST — Nginx keeps :80/:443)

```bash
systemctl status caddy --no-pager
curl -sS -o /tmp/merci.out -w '%{http_code}\n' 'http://127.0.0.1:8080/merci/'
curl -sS -o /tmp/check-domain.out -w '%{http_code}\n' \
  'http://127.0.0.1:8080/api/check-domain?domain=example-test.invalid'
# Public IP:
# http://57.128.177.132:8080/merci/
# http://57.128.177.132:8080/api/check-domain?domain=example-test.invalid
```

Expect: merci **200**; check-domain often **403** for unknown domains (still proves Caddy→uvicorn).

Direct uvicorn (optional): `http://127.0.0.1:8000/api/check-domain?...`

## 4. Mock lead generation

```bash
sudo -u waas -H bash -lc 'cd /opt/waas-factory && ./venv/bin/python lead_sites.py --mock mock.json --out /tmp/waas-mock-out --queries "plombier Osny"'
```

Expect: exit 0, sites / CSV under `/tmp/waas-mock-out`.

## 5. Stripe TEST (CLI + systemd) — after you paste `sk_test_`

Forward: `http://127.0.0.1:8000/webhook/stripe`.  
CLI ≥1.54 requires `--events` (unit includes checkout/invoice/subscription events).

```bash
sudo nano /opt/waas-factory/.env
# Set: STRIPE_SECRET_KEY=sk_test_…

sudo bash /opt/waas-factory/stripe_test_setup.sh
systemctl status stripe-listen-test --no-pager
journalctl -u stripe-listen-test -u waas-webhook -f
```

Unit **refuses to start** unless the key starts with `sk_test_`. Never use `sk_live_*`.

## Re-run deploy

```bash
cd /path/to/repo/waas-factory && sudo bash deploy/deploy_test.sh
```

Idempotent: safe to re-run; existing `.env` is not overwritten.
