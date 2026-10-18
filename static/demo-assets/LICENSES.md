# Licences — banque d’images & polices (démos ArtisanVisible)

Toutes les ressources sont **servies localement** sous `/demo-assets/` (pas de hotlink CDN). Compatible CSP `img-src 'self'` / `font-src 'self'`.

## Images (Unsplash License)

Licence : [Unsplash License](https://unsplash.com/license) — libre d’usage commercial, pas de redistribution en tant que banque d’images.

### Règle familles (anti cross-métier)

Banques métier (8–10 fichiers byte-uniques quand possible, préfixe = métier) :

Si un métier n’a pas de banque dédiée → pool de sa **famille** uniquement, sinon **visuel neutre** (CSS, aucune photo). Jamais d’image d’une autre famille.

| Famille | Métiers (ex.) | Préfixe / pool |
| --- | --- | --- |
| `alimentation` | pâtisserie, boulangerie, traiteur, boucherie, restaurant | `bouche-01.jpg` … `bouche-08.jpg` |
| `batiment` | plombier, électricien, peintre, menuisier, couvreur, … | banques dédiées ; fallback `local-*` |
| `beaute` | coiffeur, esthéticienne | `coiffeur-*` |
| `sport` | coach sportif | `coach-*` |
| `services` | photographe | `photographe-*` |

`local-*.jpg` = chantier / plans — **bâtiment uniquement**, plus jamais un fallback global.

### Banques métier

| Préfixe | Métier / famille | Exemples |
| --- | --- | --- |
| `bouche-` | Pool famille alimentation | `bouche-01.jpg` … `bouche-08.jpg` |
| `patisserie-` | Sous-banque pâtisserie (gâteaux / viennoiseries — pas de pain) | `patisserie-01…14.jpg` + `patisserie-tarte.jpg` (Unsplash, 2026-10-18) |
| `coach-` | Coach sportif / fitness | `coach-01.jpg` … `coach-08.jpg` |
| `plombier-` | Plombier / sanitaire | `plombier-01.jpg` … |
| `electricien-` | Électricien | `electricien-01.jpg` … |
| `coiffeur-` | Coiffeur | `coiffeur-01.jpg` … |
| `paysagiste-` | Paysagiste | `paysagiste-01.jpg` … |
| `peintre-` | Peintre | `peintre-01.jpg` … |
| `menuisier-` | Menuisier | `menuisier-01.jpg` … |
| `photographe-` | Photographe | `photographe-01.jpg` … |
| `local-` | Pool famille bâtiment | `local-01.jpg` … |

Alias legacy `hero-*.jpg` pointent vers le `-01` du métier correspondant.

**Galerie** : emplacements « Photo à venir » (CSS / photos métier) — jamais d’images hors famille. Les photos client remplacent le bloc via upload.

Téléchargées / rafraîchies le 2026-10-17 (JPEG ~1400px). Banque `bouche-*` ajoutée 2026-10-17.

## Polices

| Fichier | Famille | Licence |
| --- | --- | --- |
| `fonts/ibm-plex-sans-latin-wght-normal.woff2` | IBM Plex Sans (variable) | OFL-1.1 (IBM / Fontsource) |
| `fonts/fraunces-latin-wght-normal.woff2` | Fraunces (variable) | OFL-1.1 (Undercase Type / Fontsource) |
