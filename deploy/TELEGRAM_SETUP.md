# Dedicated Telegram bot for ArtisanVisible

Do **not** reuse tokens from other VPS bots. Create a new bot for this product.

## Steps

1. Open Telegram → talk to **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. On the VPS, edit env:
   ```bash
   sudo nano /opt/waas-factory/.env
   ```
   Set:
   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC…   # your new bot token
   TELEGRAM_CHAT_ID=                  # leave empty until step 4
   ```
3. Open a chat with **your new bot** and send `/start`.
4. Resolve `chat_id`:
   ```bash
   cd /opt/waas-factory
   sudo -u waas -H bash -lc 'set -a; source <(grep -E "^[A-Za-z_][A-Za-z0-9_]*=" .env); set +a; ./venv/bin/python scripts/telegram_get_chat_id.py'
   ```
   Or: `TELEGRAM_BOT_TOKEN=… python3 scripts/telegram_get_chat_id.py`
5. Paste the printed id into `.env` as `TELEGRAM_CHAT_ID=…`
6. Generate webhook secret and set `TELEGRAM_WEBHOOK_SECRET=…` (see `deploy/DEMO_REQUEST.md`).
7. Restart:
   ```bash
   sudo systemctl restart waas-webhook
   ```
8. Register Telegram webhook (preferred over polling — Caddy already proxies `/webhook/*`):
   ```bash
   curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -H 'Content-Type: application/json' \
     -d "{\"url\":\"https://visibleartisan.fr/webhook/telegram\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\",\"allowed_updates\":[\"callback_query\"]}"
   ```
9. Optional test (after token + chat id are set):
   ```bash
   sudo -u waas -H bash -lc 'set -a; source <(grep -E "^[A-Za-z_][A-Za-z0-9_]*=" /opt/waas-factory/.env); set +a; cd /opt/waas-factory; ./venv/bin/python -c "import webhook_server as w; print(w.send_telegram_message(\"Test alerte ArtisanVisible\"))"'
   ```
   Expect `True` (`ok: true` from Telegram).
