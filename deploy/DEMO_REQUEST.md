# Demande de démo gratuite — setup

## Architecture

| Élément | Rôle |
| --- | --- |
| Vitrine Astro `#demo` | Formulaire → `POST /api/demo-request` |
| FastAPI | Validation, honeypot, rate-limit IP, JSON `pending` |
| Telegram | Alerte + boutons **Générer la démo** / **Refuser** |
| Callbacks | Toast `answerCallbackQuery` immédiat → `editMessageText` « en cours (≈4–7 min) » → succès / échec / timeout (async) |
| `lead_sites.generate_demo_site` | Places si clé OK, sinon données formulaire |

**Pas** d’e-mail / SMS automatique vers l’artisan : l’admin envoie le lien manuellement.

Statuts demande : `pending` → `generating` → `generated` | `failed` ; ou `refused`.  
Idempotence : second clic « Générer » si statut ≠ `pending` → toast « Déjà en cours » / ignoré.

### Timeout Telegram « délai dépassé » (aligné scratch)

| Variable | Rôle |
| --- | --- |
| `DEMO_GENERATE_TIMEOUT_S` / `DEMO_GEN_TIMEOUT_SEC` | Override explicite (secondes) |
| `XAI_SCRATCH_PHASE2_TIMEOUT_S` / `XAI_SCRATCH_TIMEOUT_S` | Si pas d’override : `max(phases) + DEMO_GENERATE_TIMEOUT_MARGIN_S` (défaut marge **180**) |
| Défaut absolu | **600 s (10 min)** si aucune des vars ci-dessus |

Exemple VPS actuel : `XAI_SCRATCH_PHASE2_TIMEOUT_S=420` → watchdog Telegram **600 s**.  
Message « en cours » : `⏳ Génération en cours… (≈4–7 min, démarrée à HH:MM)` (`DEMO_GENERATE_ETA_HINT`).

## Telegram callbacks : webhook (préféré)

Caddy TEST (`config/Caddyfile`) proxy déjà `/webhook/*` et `/api/*` vers FastAPI `:8000`.  
Nginx `visibleartisan.fr` → Caddy — **aucun changement d’autres vhosts**.

Pourquoi webhook plutôt que polling :

- Pas de process long-running / `getUpdates` concurrent
- Secret header `X-Telegram-Bot-Api-Secret-Token` = `TELEGRAM_WEBHOOK_SECRET`
- Déployable sur le FastAPI existant (`POST /webhook/telegram`)

Polling reste possible en TEST local sans URL publique ; non documenté comme chemin principal.

### Enregistrer le webhook

1. Générer un secret : `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Dans `/opt/waas-factory/.env` :
   ```bash
   TELEGRAM_BOT_TOKEN=…
   TELEGRAM_CHAT_ID=…
   TELEGRAM_WEBHOOK_SECRET=…   # même valeur que secret_token ci-dessous
   ```
3. `sudo systemctl restart waas-webhook`
4. Enregistrer (variables déjà exportées, **ne pas** coller le token en clair dans l’historique) :
   ```bash
   curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -H 'Content-Type: application/json' \
     -d "{\"url\":\"https://visibleartisan.fr/webhook/telegram\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\",\"allowed_updates\":[\"callback_query\"]}"
   ```
5. Vérifier : `getWebhookInfo` → `url` + `pending_update_count`.

## Stockage

- Répertoire : `$DEMO_REQUESTS_DIR` ou `$SITES_DIR/_demo_requests`
- Fichiers JSON `0600`, dossier `0700`
- Statuts : `pending` → `generating` → `generated` | `failed` ; ou `refused`

## Rate limit

Par IP (`CF-Connecting-IP` prioritaire, sinon 1er hop `X-Forwarded-For`) : 3 requêtes / 15 min (configurable).
