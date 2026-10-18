"""
demo_ttl.py — Durée de vie des démos (4 jours par défaut).

- created_at / expires_at dans meta.json
- Sites .activated : jamais d'expiration (expires_at effacé à l'activation)
- Archive hors web (pas de hard-delete) + marqueur .expired → HTTP 410
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("demo_ttl")

SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

# Répertoires spéciaux sous SITES_DIR — jamais traités comme démos
SKIP_DIR_NAMES = frozenset(
    {
        "_vitrine",
        "_demo_requests",
        "_expired",
        "merci",
        "static",
    }
)


def ttl_days() -> int:
    raw = (os.getenv("DEMO_TTL_DAYS") or "4").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4
    return max(1, min(n, 365))


def sites_dir() -> Path:
    return Path(os.getenv("SITES_DIR", "/var/www/sites")).resolve()


def archive_dir() -> Path:
    return Path(
        os.getenv("DEMO_ARCHIVE_DIR") or "/var/lib/waas/demo-archive"
    ).resolve()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_expires_at(created: datetime | None = None, days: int | None = None) -> datetime:
    base = created or now_utc()
    return base + timedelta(days=days if days is not None else ttl_days())


def format_expiry_fr(dt: datetime | None) -> str:
    """JJ/MM (fuseau Europe/Paris si disponible, sinon UTC)."""
    if dt is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo("Europe/Paris"))
    except Exception:  # noqa: BLE001
        local = dt.astimezone(timezone.utc)
    return local.strftime("%d/%m")


def format_expiry_long_fr(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        local = dt.astimezone(ZoneInfo("Europe/Paris"))
    except Exception:  # noqa: BLE001
        local = dt.astimezone(timezone.utc)
    return local.strftime("%d/%m/%Y")


def stamp_ttl_fields(meta: dict[str, Any], *, created: datetime | None = None) -> dict[str, Any]:
    """Ajoute created_at / expires_at (idempotent si déjà présents et non activé)."""
    out = dict(meta)
    if out.get("never_expires") or out.get("activated"):
        out["expires_at"] = None
        return out
    created_dt = parse_iso(out.get("created_at")) if out.get("created_at") else None
    if created_dt is None:
        created_dt = created or now_utc()
        out["created_at"] = created_dt.isoformat()
    if not out.get("expires_at"):
        out["expires_at"] = compute_expires_at(created_dt).isoformat()
    return out


def clear_expiry(meta: dict[str, Any]) -> dict[str, Any]:
    """Activation payante : plus d'expiration."""
    out = dict(meta)
    out["expires_at"] = None
    out["never_expires"] = True
    out["activated"] = True
    return out


def is_activated(site_dir: Path) -> bool:
    return (site_dir / ".activated").is_file()


def is_expired_marker(site_dir: Path) -> bool:
    return (site_dir / ".expired").is_file()


def is_expired(
    meta: dict[str, Any] | None,
    site_dir: Path | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True si la démo ne doit plus être activable / visible (hors sites payés)."""
    if site_dir is not None and is_activated(site_dir):
        return False
    if site_dir is not None and is_expired_marker(site_dir):
        return True
    if not meta:
        return False
    if meta.get("never_expires") or meta.get("activated"):
        return False
    exp = parse_iso(meta.get("expires_at"))
    if exp is None:
        return False
    return (now or now_utc()) >= exp


def load_meta(site_dir: Path) -> dict[str, Any]:
    path = site_dir / "meta.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_meta(site_dir: Path, meta: dict[str, Any]) -> None:
    path = site_dir / "meta.json"
    tmp = path.with_name(".meta.json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def clear_expiry_on_activation(site_dir: Path) -> dict[str, Any]:
    meta = clear_expiry(load_meta(site_dir))
    save_meta(site_dir, meta)
    # Si un marqueur d'archive était présent (cas rare), le retirer
    marker = site_dir / ".expired"
    if marker.exists():
        marker.unlink(missing_ok=True)
    return meta


def banner_expiry_suffix(meta: dict[str, Any] | None = None, expires_at: datetime | None = None) -> str:
    """Texte discret pour le bandeau : « Aperçu disponible jusqu'au JJ/MM »."""
    dt = expires_at
    if dt is None and meta:
        dt = parse_iso(meta.get("expires_at"))
    if dt is None:
        dt = compute_expires_at()
    label = format_expiry_fr(dt)
    if not label:
        return ""
    return f" · Aperçu disponible jusqu'au {label}"


def expired_page_html(*, request_demo_url: str | None = None) -> str:
    base = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("PREVIEW_BASE_DOMAIN")
        or "https://visibleartisan.fr"
    ).rstrip("/")
    cta = request_demo_url or f"{base}/#demo"
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>Cette démo a expiré — ArtisanVisible</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      font-family: "Segoe UI", system-ui, sans-serif; background: #0f1419; color: #e8e6e3;
      padding: 1.5rem;
    }}
    main {{ max-width: 28rem; text-align: center; }}
    h1 {{ font-size: 1.75rem; margin: 0 0 0.75rem; font-weight: 700; }}
    p {{ margin: 0 0 1.5rem; line-height: 1.55; color: #b8b4ae; }}
    a.cta {{
      display: inline-block; background: #c4a574; color: #0f1419; font-weight: 700;
      text-decoration: none; padding: 0.85rem 1.35rem; border-radius: 0.75rem;
    }}
    a.cta:hover {{ filter: brightness(1.05); }}
  </style>
</head>
<body>
  <main>
    <h1>Cette démo a expiré</h1>
    <p>L’aperçu n’est plus disponible. Vous pouvez demander une nouvelle démo gratuite — sans engagement.</p>
    <a class="cta" href="{cta}">Demander une nouvelle démo</a>
  </main>
</body>
</html>
"""


def write_expired_stub(site_dir: Path, meta: dict[str, Any] | None = None) -> None:
    """Laisse un stub servi avec marqueur .expired (Caddy → 410)."""
    site_dir.mkdir(parents=True, exist_ok=True)
    meta_out = dict(meta or load_meta(site_dir))
    meta_out["expired"] = True
    meta_out["archived_at"] = now_utc().isoformat()
    save_meta(site_dir, meta_out)
    (site_dir / ".expired").write_text(
        json.dumps({"archived_at": meta_out["archived_at"]}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (site_dir / "index.html").write_text(expired_page_html(), encoding="utf-8")


def iter_demo_dirs(root: Path | None = None) -> list[Path]:
    base = root or sites_dir()
    if not base.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if child.is_symlink():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if child.name in SKIP_DIR_NAMES:
            continue
        if not SAFE_SLUG_RE.match(child.name):
            continue
        # Domaine client (.fr symlink cible) : ignorer si pas de meta typique démo
        if "." in child.name and not (child / "meta.json").is_file():
            continue
        out.append(child)
    return out


def migrate_existing_ttls(
    root: Path | None = None,
    *,
    days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Démos existantes sans expires_at : expires_at = now + TTL (pas d'expiry immédiate).
    Sites activés : never_expires, expires_at null.
    """
    base = root or sites_dir()
    n_days = days if days is not None else ttl_days()
    created = now_utc()
    expires = compute_expires_at(created, days=n_days)
    updated: list[str] = []
    activated: list[str] = []
    skipped: list[str] = []

    for site_dir in iter_demo_dirs(base):
        meta = load_meta(site_dir)
        if is_activated(site_dir):
            if meta.get("never_expires") and meta.get("expires_at") in (None, ""):
                skipped.append(site_dir.name)
                continue
            meta = clear_expiry(meta)
            if not meta.get("created_at"):
                meta["created_at"] = created.isoformat()
            if not dry_run:
                save_meta(site_dir, meta)
            activated.append(site_dir.name)
            continue

        if meta.get("expires_at") and not meta.get("never_expires"):
            skipped.append(site_dir.name)
            continue

        if not meta.get("created_at"):
            # mtime du dossier comme created_at approximatif, mais expires = now+TTL
            meta["created_at"] = created.isoformat()
        meta["expires_at"] = expires.isoformat()
        meta.pop("never_expires", None)
        if not dry_run:
            save_meta(site_dir, meta)
        updated.append(site_dir.name)

    return {
        "updated": updated,
        "activated_cleared": activated,
        "skipped": skipped,
        "expires_at": expires.isoformat(),
        "ttl_days": n_days,
    }


def extend_demo(
    slug: str,
    *,
    days: int | None = None,
    root: Path | None = None,
    archive_root: Path | None = None,
) -> dict[str, Any]:
    """Prolonge expires_at de N jours (depuis max(now, expires_at actuel)). Restaure depuis archive si besoin."""
    if not SAFE_SLUG_RE.match(slug or ""):
        raise ValueError("slug invalide")
    n_days = days if days is not None else ttl_days()
    base = root or sites_dir()
    arch = archive_root or archive_dir()
    site_dir = base / slug
    archived = arch / slug

    restored = False
    if is_expired_marker(site_dir) and archived.is_dir():
        # Restaurer le contenu archivé
        stub_meta = load_meta(site_dir)
        shutil.rmtree(site_dir)
        shutil.move(str(archived), str(site_dir))
        # Re-appliquer TTL fields from stub if useful
        meta = load_meta(site_dir)
        if stub_meta.get("name") and not meta.get("name"):
            meta.update({k: stub_meta[k] for k in ("name", "trade", "preview_url") if stub_meta.get(k)})
        restored = True
    elif not site_dir.is_dir():
        if archived.is_dir():
            arch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived), str(site_dir))
            restored = True
        else:
            raise FileNotFoundError(f"Démo introuvable : {slug}")

    if is_activated(site_dir):
        meta = clear_expiry_on_activation(site_dir)
        return {
            "slug": slug,
            "status": "activated_no_expiry",
            "expires_at": None,
            "restored": restored,
            "meta": meta,
        }

    meta = load_meta(site_dir)
    now = now_utc()
    current_exp = parse_iso(meta.get("expires_at"))
    base_dt = current_exp if current_exp and current_exp > now else now
    new_exp = base_dt + timedelta(days=n_days)
    if not meta.get("created_at"):
        meta["created_at"] = now.isoformat()
    meta["expires_at"] = new_exp.isoformat()
    meta.pop("expired", None)
    meta.pop("archived_at", None)
    meta.pop("never_expires", None)
    save_meta(site_dir, meta)
    marker = site_dir / ".expired"
    if marker.exists():
        marker.unlink(missing_ok=True)
    # Rafraîchir bandeau dans index si possible (best-effort)
    _refresh_banner_expiry(site_dir, meta)

    return {
        "slug": slug,
        "status": "extended",
        "expires_at": new_exp.isoformat(),
        "expires_fr": format_expiry_long_fr(new_exp),
        "days": n_days,
        "restored": restored,
    }


def _refresh_banner_expiry(site_dir: Path, meta: dict[str, Any]) -> None:
    """Met à jour la mention « jusqu'au JJ/MM » dans le bandeau si présent."""
    index = site_dir / "index.html"
    if not index.is_file():
        return
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        return
    if "BANDEAU_DEMO_START" not in html:
        return
    suffix = banner_expiry_suffix(meta)
    html2 = re.sub(
        r"\s*·\s*Aperçu disponible jusqu'au\s+\d{2}/\d{2}",
        "",
        html,
    )
    if suffix and suffix not in html2:
        html2 = re.sub(
            r"(<!-- BANDEAU_DEMO_START -->.*?)(</div>\s*<!-- BANDEAU_DEMO_END -->)",
            r"\1" + suffix + r"\2",
            html2,
            count=1,
            flags=re.DOTALL,
        )
    if html2 != html:
        tmp = index.with_name(".index.html.tmp")
        tmp.write_text(html2, encoding="utf-8")
        tmp.replace(index)


def archive_one(site_dir: Path, arch_root: Path) -> str:
    """Déplace le contenu vers l'archive et laisse un stub .expired. Retourne 'archived'|'skipped'|…"""
    slug = site_dir.name
    if is_activated(site_dir):
        return "skipped_activated"
    if is_expired_marker(site_dir):
        return "already_expired"
    meta = load_meta(site_dir)
    if not is_expired(meta, site_dir):
        return "not_expired"

    arch_root.mkdir(parents=True, exist_ok=True)
    dest = arch_root / slug
    if dest.exists():
        # Idempotent : garder l'archive existante, remplacer le stub
        shutil.rmtree(site_dir)
        write_expired_stub(site_dir, meta)
        return "stub_refreshed"

    # Déplacer puis stub
    parent = site_dir.parent
    staging = parent / f".archive-staging-{slug}"
    if staging.exists():
        shutil.rmtree(staging)
    site_dir.rename(staging)
    try:
        shutil.move(str(staging), str(dest))
    except Exception:
        # Rollback best-effort
        if staging.exists() and not site_dir.exists():
            staging.rename(site_dir)
        raise
    write_expired_stub(site_dir, meta)
    log.info("Démo archivée : %s → %s", slug, dest)
    return "archived"


def archive_expired_demos(
    root: Path | None = None,
    archive_root: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotent : archive les démos expirées non activées."""
    base = root or sites_dir()
    arch = archive_root or archive_dir()
    results: dict[str, list[str]] = {
        "archived": [],
        "already_expired": [],
        "skipped_activated": [],
        "not_expired": [],
        "stub_refreshed": [],
        "errors": [],
    }
    for site_dir in iter_demo_dirs(base):
        meta = load_meta(site_dir)
        if is_activated(site_dir):
            results["skipped_activated"].append(site_dir.name)
            continue
        if not is_expired(meta, site_dir) and not is_expired_marker(site_dir):
            results["not_expired"].append(site_dir.name)
            continue
        if dry_run:
            key = "already_expired" if is_expired_marker(site_dir) else "archived"
            results[key].append(site_dir.name)
            continue
        try:
            status = archive_one(site_dir, arch)
            results.setdefault(status, []).append(site_dir.name)
        except Exception as e:  # noqa: BLE001
            log.exception("Archive échouée pour %s", site_dir.name)
            results["errors"].append(f"{site_dir.name}: {e}")
    return results
