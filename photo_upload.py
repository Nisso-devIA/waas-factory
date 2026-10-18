#!/usr/bin/env python3
"""
photo_upload.py — Private photo upload for activated artisan sites.

- Long unguessable token stored in .activated + SITES_DIR/upload_registry.json
- Accept jpeg/png/webp (+ heic if pillow-heif available)
- Resize/compress, strip EXIF/GPS, soft-delete → media/trash/
- Inject « Nos réalisations » into index.html without full regen
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("photo_upload")

SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
SAFE_PHOTO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
REALISATIONS_RE = re.compile(
    r"<!-- REALISATIONS_START -->.*?<!-- REALISATIONS_END -->\s*",
    re.DOTALL,
)

MAX_BYTES = int(os.getenv("PHOTO_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
MAX_PER_BATCH = int(os.getenv("PHOTO_UPLOAD_MAX_BATCH", "8"))
MAX_PER_SITE = int(os.getenv("PHOTO_UPLOAD_MAX_SITE", "40"))
MAX_EDGE = int(os.getenv("PHOTO_UPLOAD_MAX_EDGE", "1600"))
WEBP_QUALITY = int(os.getenv("PHOTO_UPLOAD_QUALITY", "82"))
RATE_LIMIT_WINDOW_S = float(os.getenv("PHOTO_UPLOAD_RATE_WINDOW", "60"))
RATE_LIMIT_MAX = int(os.getenv("PHOTO_UPLOAD_RATE_MAX", "12"))

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
ALLOWED_EXT_HINTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("PREVIEW_BASE_DOMAIN")
    or "https://visibleartisan.fr"
).rstrip("/")

_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}

_heif_ready: bool | None = None


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def ensure_heif() -> bool:
    global _heif_ready
    if _heif_ready is not None:
        return _heif_ready
    try:
        from pillow_heif import register_heif_opener  # type: ignore

        register_heif_opener()
        _heif_ready = True
    except Exception:  # noqa: BLE001
        _heif_ready = False
    return _heif_ready


def media_dir(site_dir: Path) -> Path:
    return site_dir / "media"


def photos_dir(site_dir: Path) -> Path:
    return media_dir(site_dir) / "photos"


def trash_dir(site_dir: Path) -> Path:
    return media_dir(site_dir) / "trash"


def manifest_path(site_dir: Path) -> Path:
    return media_dir(site_dir) / "photos.json"


def registry_path(sites_dir: Path) -> Path:
    return sites_dir / "upload_registry.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_manifest(site_dir: Path) -> dict:
    data = load_json(manifest_path(site_dir), {"photos": []})
    if not isinstance(data, dict):
        return {"photos": []}
    photos = data.get("photos")
    if not isinstance(photos, list):
        data["photos"] = []
    return data


def save_manifest(site_dir: Path, data: dict) -> None:
    save_json(manifest_path(site_dir), data)


def load_registry(sites_dir: Path) -> dict:
    data = load_json(registry_path(sites_dir), {})
    return data if isinstance(data, dict) else {}


def save_registry(sites_dir: Path, data: dict) -> None:
    save_json(registry_path(sites_dir), data)


def upload_url(slug: str, token: str) -> str:
    return f"{PUBLIC_BASE_URL}/upload/{slug}/{token}"


def site_public_url(slug: str) -> str:
    return f"{PUBLIC_BASE_URL}/{slug}/"


def generate_upload_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_upload_token(
    site_dir: Path,
    sites_dir: Path,
    *,
    rotate: bool = False,
    business_name: str | None = None,
) -> str:
    """Create or return upload token; persist in .activated + upload_registry.json."""
    act_file = site_dir / ".activated"
    if not act_file.exists():
        raise FileNotFoundError("site not activated")

    try:
        act = json.loads(act_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        act = {}

    token = act.get("upload_token") if not rotate else None
    if not token or not isinstance(token, str) or len(token) < 20:
        token = generate_upload_token()
        act["upload_token"] = token
        act["upload_token_created_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_text(act_file, json.dumps(act, ensure_ascii=False, indent=2))

    name = business_name or (act.get("customer_details") or {}).get("name") or site_dir.name
    meta = load_json(site_dir / "meta.json", {})
    if isinstance(meta, dict) and meta.get("name"):
        name = meta["name"]

    reg = load_registry(sites_dir)
    reg[site_dir.name] = {
        "token": token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "upload_url": upload_url(site_dir.name, token),
    }
    save_registry(sites_dir, reg)
    return token


def validate_slug_token(site_dir: Path, slug: str, token: str) -> dict:
    if not SAFE_SLUG_RE.match(slug):
        raise PermissionError("invalid slug")
    if site_dir.name != slug or not site_dir.is_dir():
        raise PermissionError("unknown site")
    # Reject path tricks: site_dir must resolve under expected parent
    act_file = site_dir / ".activated"
    if not act_file.exists():
        raise PermissionError("not activated")
    try:
        act = json.loads(act_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PermissionError("invalid activation") from e
    expected = act.get("upload_token")
    if not expected or not secrets.compare_digest(str(expected), str(token)):
        raise PermissionError("bad token")
    return act


def check_rate_limit(key: str) -> bool:
    """Return True if allowed, False if rate-limited."""
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW_S]
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def _open_image(raw: bytes, content_type: str | None, filename: str | None) -> Image.Image:
    ct = (content_type or "").lower().split(";")[0].strip()
    name = (filename or "").lower()
    ext = Path(name).suffix if name else ""

    if ct in ("image/heic", "image/heif") or ext in (".heic", ".heif"):
        if not ensure_heif():
            raise ValueError("HEIC non supporté sur ce serveur")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except UnidentifiedImageError as e:
        raise ValueError("Fichier image invalide") from e
    except OSError as e:
        raise ValueError("Impossible de lire l'image") from e

    # Normalize orientation from EXIF then drop all EXIF/GPS
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    return img


def process_image_bytes(raw: bytes, content_type: str | None = None, filename: str | None = None) -> bytes:
    if len(raw) > MAX_BYTES:
        raise ValueError(f"Fichier trop volumineux (max {MAX_BYTES // (1024 * 1024)} Mo)")
    if len(raw) < 32:
        raise ValueError("Fichier vide ou trop petit")

    ct = (content_type or "").lower().split(";")[0].strip()
    ext = Path((filename or "").lower()).suffix
    if ct and ct not in ALLOWED_CONTENT_TYPES and ext not in ALLOWED_EXT_HINTS:
        raise ValueError("Type de fichier non autorisé (jpeg, png, webp, heic)")
    if not ct and ext and ext not in ALLOWED_EXT_HINTS:
        raise ValueError("Extension non autorisée")

    img = _open_image(raw, content_type, filename)
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)

    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Re-encode without EXIF
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()


def build_realisations_html(site_dir: Path, photos: list[dict]) -> str:
    if not photos:
        # Keep markers only — caller leaves existing placeholder block when possible
        return (
            "<!-- REALISATIONS_START -->\n"
            "<!-- REALISATIONS_END -->\n"
        )

    cards = []
    for p in photos:
        pid = html.escape(p["id"], quote=True)
        cap = html.escape((p.get("caption") or "").strip(), quote=True)
        src = html.escape(f"media/photos/{pid}.webp", quote=True)
        cap_html = (
            f"<figcaption>{cap}</figcaption>"
            if cap
            else ""
        )
        cards.append(
            f'<figure class="demo-gallery-slot demo-gallery-slot-live">'
            f'<img src="{src}" alt="{cap or "Réalisation"}" '
            f'loading="lazy" decoding="async" width="800" height="600">'
            f"{cap_html}</figure>"
        )
    grid = "\n      ".join(cards)
    return (
        "<!-- REALISATIONS_START -->\n"
        '<section class="demo-section demo-section-alt" id="realisations">\n'
        '  <div class="demo-wrap">\n'
        '    <header class="demo-section-head"><h2>Nos réalisations</h2>'
        "<p>Photos de vos chantiers et prestations.</p></header>\n"
        '    <p class="demo-gallery-cue">Galerie client — photos ajoutées via l’espace upload.</p>\n'
        f'    <div class="demo-gallery">\n      {grid}\n    </div>\n'
        "  </div>\n"
        "</section>\n"
        "<!-- REALISATIONS_END -->\n"
    )


def inject_realisations_section(site_dir: Path) -> None:
    index_path = site_dir / "index.html"
    if not index_path.exists():
        return
    manifest = load_manifest(site_dir)
    photos = [p for p in manifest.get("photos", []) if not p.get("deleted")]
    html_text = index_path.read_text(encoding="utf-8")

    # No client photos: keep elegant empty cue / placeholder (do not wipe markers)
    if not photos and (
        "demo-gallery-placeholder" in html_text or "av-realisations-empty" in html_text
    ):
        return

    block = build_realisations_html(site_dir, photos)

    if "<!-- REALISATIONS_START -->" in html_text:
        html_text = REALISATIONS_RE.sub(block, html_text)
    else:
        # Insert before contact section or before footer
        inserted = False
        for marker in (
            'id="contact"',
            '<section class="demo-section" id="contact">',
            '<section class="max-w-5xl mx-auto px-5 py-10 text-center">',
            "<footer",
        ):
            idx = html_text.find(marker)
            if idx != -1:
                html_text = html_text[:idx] + block + "\n  " + html_text[idx:]
                inserted = True
                break
        if not inserted:
            html_text = html_text.replace("</body>", block + "</body>")

    atomic_write_text(index_path, html_text)


def soft_delete_photo(site_dir: Path, photo_id: str) -> bool:
    if not SAFE_PHOTO_ID_RE.match(photo_id):
        raise ValueError("invalid photo id")
    manifest = load_manifest(site_dir)
    found = None
    for p in manifest.get("photos", []):
        if p.get("id") == photo_id and not p.get("deleted"):
            found = p
            break
    if not found:
        return False

    src = photos_dir(site_dir) / f"{photo_id}.webp"
    trash_dir(site_dir).mkdir(parents=True, exist_ok=True)
    if src.exists():
        dest = trash_dir(site_dir) / f"{photo_id}_{int(time.time())}.webp"
        src.replace(dest)

    found["deleted"] = True
    found["deleted_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(site_dir, manifest)
    inject_realisations_section(site_dir)
    return True


def save_uploaded_photos(
    site_dir: Path,
    items: list[tuple[bytes, str | None, str | None, str]],
) -> list[dict]:
    """
    items: list of (raw_bytes, content_type, filename, caption)
    Returns list of new photo meta dicts.
    """
    if not items:
        raise ValueError("Aucune photo envoyée")
    if len(items) > MAX_PER_BATCH:
        raise ValueError(f"Maximum {MAX_PER_BATCH} photos par envoi")

    photos_dir(site_dir).mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(site_dir)
    active = [p for p in manifest.get("photos", []) if not p.get("deleted")]
    if len(active) + len(items) > MAX_PER_SITE:
        raise ValueError(f"Limite atteinte ({MAX_PER_SITE} photos max sur le site)")

    created: list[dict] = []
    for raw, content_type, filename, caption in items:
        webp = process_image_bytes(raw, content_type, filename)
        photo_id = secrets.token_urlsafe(12)
        dest = photos_dir(site_dir) / f"{photo_id}.webp"
        atomic_write_bytes(dest, webp)
        meta = {
            "id": photo_id,
            "caption": (caption or "").strip()[:200],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bytes": len(webp),
        }
        manifest.setdefault("photos", []).append(meta)
        created.append(meta)

    save_manifest(site_dir, manifest)
    inject_realisations_section(site_dir)
    return created


def format_photo_upload_alert(slug: str, name: str, count: int) -> str:
    return (
        f"Nouvelles photos — {name} ({slug})\n"
        f"• {count} photo(s) ajoutée(s)\n"
        f"• Site: {site_public_url(slug)}"
    )


UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="robots" content="noindex, nofollow">
  <title>Envoyer des photos — {name}</title>
  <link rel="stylesheet" href="/demo-assets/demo.css?v=20261017u">
</head>
<body class="bg-slate-50 text-slate-900 font-sans antialiased min-h-screen">
  <main class="max-w-lg mx-auto px-4 py-8">
    <h1 class="text-2xl font-extrabold mb-1">Vos photos</h1>
    <p class="text-slate-600 text-sm mb-6">{name} — elles apparaîtront dans « Nos réalisations ».</p>

    <form id="form" class="space-y-4" data-max-batch="{max_batch}">
      <label class="block">
        <span class="text-sm font-semibold">Choisir des photos</span>
        <input id="files" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif,image/*"
               capture="environment" multiple
               class="mt-2 block w-full text-sm file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0
                      file:bg-slate-900 file:text-white file:font-semibold">
      </label>
      <div id="captions" class="space-y-3"></div>
      <p id="hint" class="text-xs text-slate-500">JPEG, PNG, WebP (HEIC si supporté) — max {max_batch} par envoi, {max_mb} Mo chacune.</p>
      <button type="submit" id="submit"
              class="w-full bg-slate-900 text-white py-3.5 rounded-xl font-bold text-base hover:bg-slate-800 disabled:opacity-50">
        Envoyer
      </button>
    </form>
    <p id="status" class="mt-4 text-sm text-center text-slate-600" role="status"></p>
    <p class="mt-8 text-center text-xs text-slate-400">
      <a href="{site_url}" class="underline">Voir mon site</a>
    </p>
  </main>
  <script src="/demo-assets/upload.js" defer></script>
</body>
</html>
"""


def render_upload_page(name: str, site_url: str) -> str:
    return UPLOAD_PAGE_HTML.format(
        name=html.escape(name),
        site_url=html.escape(site_url, quote=True),
        max_batch=MAX_PER_BATCH,
        max_mb=MAX_BYTES // (1024 * 1024),
    )
