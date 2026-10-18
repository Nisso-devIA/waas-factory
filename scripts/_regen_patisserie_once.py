#!/usr/bin/env python3
"""One-shot: regen patisserie demo with template + Grok, preserve expires_at."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

os.environ["DEMO_MODE"] = "template"
sys.path.insert(0, "/opt/waas-factory")
os.chdir("/opt/waas-factory")

import lead_sites  # noqa: E402
from trade_profiles import format_fr_phone, normalize_inputs, title_case_fr  # noqa: E402

meta_path = Path("/var/www/sites/patisserie/meta.json")
meta = json.loads(meta_path.read_text(encoding="utf-8"))
expires = meta.get("expires_at")
created = meta.get("created_at")
trade = meta.get("trade") or "Patissier"
place = {
    "id": meta.get("place_id"),
    "displayName": {"text": meta.get("name") or "Patisserie"},
    "formattedAddress": meta.get("address") or "Osny",
    "nationalPhoneNumber": meta.get("phone") or "",
}
name, city, phone = normalize_inputs(
    lead_sites.display_name(place),
    place.get("formattedAddress") or "",
    place.get("nationalPhoneNumber") or "",
)
place["displayName"] = {"text": name}
place["formattedAddress"] = title_case_fr(place.get("formattedAddress") or city) or city
if phone:
    place["nationalPhoneNumber"] = phone

cfg = lead_sites.Config()
checkout = meta.get("checkout_url") or ""
html_out = lead_sites.render_preview(
    place, checkout, cfg, trade, slug="patisserie", allow_llm=True
)
lead_sites.atomic_write_text(Path("/var/www/sites/patisserie/index.html"), html_out)
lead_sites.atomic_write_text(Path("/var/www/sites/patisserie/index.preview.html"), html_out)
metrics = lead_sites.last_render_metrics()
m = re.search(r'data-content-source="([^"]+)"', html_out)
meta["name"] = name
meta["trade"] = trade
meta["phone"] = format_fr_phone(str(phone)) or phone
meta["content_source"] = metrics.get("content_source") or (m.group(1) if m else "unknown")
meta["demo_mode"] = "template"
meta["gen_metrics"] = {
    k: metrics[k]
    for k in (
        "duration_s",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "attempts",
        "model",
        "fallback",
        "fallback_reason",
    )
    if k in metrics
}
if expires:
    meta["expires_at"] = expires
if created:
    meta["created_at"] = created
lead_sites.atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

imgs = sorted(set(re.findall(r"demo-assets/images/([A-Za-z0-9_.-]+)", html_out)))
print("SOURCE", meta["content_source"])
print("EXPIRES", meta.get("expires_at"))
print("IMAGES", imgs)
print("METRICS", meta["gen_metrics"])
assert all(i.startswith("patisserie-") for i in imgs), imgs
assert not any(i.startswith("local-") or i in ("bouche-02.jpg", "bouche-06.jpg") for i in imgs)
low = html_out.casefold()
assert "chantier" not in low
assert "cette prestation est menée simplement" not in low
assert any(w in low for w in ("gâteaux", "gateaux", "viennoiser", "entremet", "commande"))
print("COPY_OK")
for t in re.findall(r"<h3>([^<]+)</h3>", html_out)[:8]:
    print("H3", t)
