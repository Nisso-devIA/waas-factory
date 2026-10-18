# Cloudflare — domaines clients (post-OVH)

Après `ovh_domain.py --buy` ou `--dns-only`, le pipeline appelle
`cloudflare_domain.try_move_client_domain_to_cloudflare` :

1. Crée (ou réutilise) une zone Cloudflare sur le compte `CLOUDFLARE_ACCOUNT_ID`
2. A proxied `@` + `www` → `VPS_PUBLIC_IP` (défaut `57.128.177.132`)
3. Copie MX + TXT SPF OVH en DNS-only
4. SSL zone → **Full (strict)**
5. Désactive DNSSEC OVH (attend `dnssecState=disabled` + DS vide)
6. Bascule les NS OVH en mode externe vers les NS Cloudflare

## Skip sans token

Si `CLOUDFLARE_API_TOKEN` est vide : **avertissement stderr**, étape ignorée,
le pipeline OVH/Caddy **ne échoue pas**.

## Variables (`.env` / `/opt/waas-factory/.env`)

```bash
CLOUDFLARE_API_TOKEN=          # laisser vide jusqu'à création du token
CLOUDFLARE_ACCOUNT_ID=2dc6d88394ebd5dfb147ddf563d7e2eb
VPS_PUBLIC_IP=57.128.177.132
```

## Permissions token Cloudflare (minimum)

Custom API Token → **Account** `2dc6d88394ebd5dfb147ddf563d7e2eb` :

| Dashboard (Zone) | Permission group (API) | Pourquoi |
| --- | --- | --- |
| **Zone → Zone → Edit** | Zone Edit / Zone Write | Créer / lister les zones |
| **Zone → DNS → Edit** | DNS Write | A / MX / TXT |
| **Zone → Zone Settings → Edit** | Zone Settings Edit / Write | SSL Full (strict) |

- **Account resources** : Include → ce compte
- **Zone resources** : **All zones** (obligatoire pour créer de nouvelles zones clients)

Ne pas utiliser un token scoped à une seule zone existante.

## SSL Full (strict) + ACME

Hypothèse : l’origine (Caddy on-demand TLS et/ou Nginx+certbot) présente un
certificat valide pour le hostname. Cloudflare parle en HTTPS à l’origine.

Pour HTTP-01 (certbot / Caddy) derrière le proxy orange :

- Sur le vhost **HTTP :80**, servir `/.well-known/acme-challenge/` **avant** tout
  redirect 301 vers HTTPS (voir `nginx/client-domain-cloudflare.example.conf`).
- Ne pas modifier `visibleartisan.fr` ni les autres vhosts hors périmètre client.

## Zones protégées (écriture refusée)

Toute mutation (create zone, DNS, SSL, DNSSEC OVH, NS) est **refusée** si la
cible matche (casse / IDNA) :

- `miina-pâtisserie.fr`
- `miina-patisserie.fr` (ASCII)
- `xn--miina-ptisserie-9jb.fr` (punycode)

Voir `PROTECTED_ZONES` / `assert_domain_writable` dans `cloudflare_domain.py`.

## Interdits

- Pas de `ovh_domain.py --buy` en test destructif
- **Ne jamais toucher** `miina-pâtisserie.fr` (CF / DNS / Nginx)
- Pas de bascule NS live sur `visibleartisan.fr` depuis ce module hors pipeline client
- Pas de secrets dans git
