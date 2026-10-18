# TTL démos (4 jours) + limites formulaire

## TTL
- `DEMO_TTL_DAYS=4` (env)
- `meta.json` : `created_at`, `expires_at` ; activation Stripe → `never_expires` / `expires_at=null`
- Archive : `scripts/archive_expired_demos.py` + timer systemd `waas-archive-demos.timer` (03:15)
- Archive dir : `/var/lib/waas/demo-archive` (hors web)
- Stub `.expired` → Caddy → `GET /api/demo-expired` **HTTP 410**
- Prolongation : `python scripts/extend_demo.py SLUG --days 4` ou bouton Telegram « Prolonger 4 jours »

## Limites POST /api/demo-request
- E-mail normalisé + jetables bloqués ; 1 demande si pending / generated active / refused < 30j
- IP : `CF-Connecting-IP` seulement si peer Cloudflare/local ; 2/24h et 3/30j
- IP stockée en HMAC (`DEMO_IP_HASH_SALT`) ; purge 30j
- Reset admin : `python scripts/demo_limits.py --reset email@x.fr`

## Migration
```bash
sudo -u waas /opt/waas-factory/venv/bin/python /opt/waas-factory/scripts/migrate_demo_ttl.py
```
