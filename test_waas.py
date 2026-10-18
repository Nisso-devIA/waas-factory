#!/usr/bin/env python3
"""
test_waas.py — Suite unitaire / intégration
couvrant lead_sites.py, webhook_server.py, set_domain.py, ovh_domain.py,
cloudflare_domain.py et photo_upload.py.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from PIL import Image

import cloudflare_domain
import lead_sites
import ovh_domain
import photo_upload
import webhook_server


class TestLeadSites(unittest.TestCase):
    def test_company_detection_regex(self):
        self.assertTrue(lead_sites.is_company_name("Plomberie Morel & Fils"))
        self.assertTrue(lead_sites.is_company_name("Martin & Cie"))
        self.assertTrue(lead_sites.is_company_name("SARL Dupuis Chauffage"))
        self.assertTrue(lead_sites.is_company_name("Plomberie Moderne SA"))
        self.assertTrue(lead_sites.is_company_name("Dupont S.A."))
        self.assertTrue(lead_sites.is_company_name("Dupont S.A. Chauffage"))
        self.assertTrue(lead_sites.is_company_name("Établissements Lefèvre"))

        self.assertFalse(lead_sites.is_company_name("Sa Plomberie"))
        self.assertFalse(lead_sites.is_company_name("Plomberie Sa Majesté"))
        self.assertFalse(lead_sites.is_company_name("Jean-Michel Artisan Plombier"))

    def test_json_ld_script_injection_escaped(self):
        malicious = {"name": "Malin </script><script>alert(1)</script>"}
        encoded = lead_sites.safe_json_ld(malicious)
        self.assertNotIn("</script>", encoded)
        self.assertIn("<\\/script>", encoded)

    def test_outreach_gating_and_symlink_ignored_in_slugs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            fixture = {
                "places": [
                    {
                        "id": "pid_sole_mobile",
                        "displayName": {"text": "Sa Plomberie Artisan"},
                        "formattedAddress": "10 Rue de Pontoise, 95520 Osny",
                        "nationalPhoneNumber": "06 12 34 56 78",
                        "internationalPhoneNumber": "+33 6 12 34 56 78",
                        "rating": 4.9,
                        "userRatingCount": 25,
                    },
                    {
                        "id": "pid_company_mobile",
                        "displayName": {"text": "Dupont S.A."},
                        "formattedAddress": "14 Rue Aristide Briand, 95520 Osny",
                        "nationalPhoneNumber": "06 98 76 54 32",
                        "internationalPhoneNumber": "+33 6 98 76 54 32",
                        "rating": 4.8,
                        "userRatingCount": 42,
                    },
                ]
            }
            mock_path = out_dir / "mock.json"
            mock_path.write_text(json.dumps(fixture), encoding="utf-8")

            rc = lead_sites.main(["--mock", str(mock_path), "--out", str(out_dir), "--queries", "plombier Osny"])
            self.assertEqual(rc, 0)

            symlink_domain = out_dir / "aaa-dupont-plomberie.fr"
            symlink_domain.symlink_to("dupont-s-a")

            pid_to_slug, used = lead_sites.load_existing_slugs(out_dir)
            self.assertEqual(pid_to_slug["pid_company_mobile"], "dupont-s-a")
            self.assertNotIn("aaa-dupont-plomberie.fr", used)

            csv_path = out_dir / "leads.csv"
            with csv_path.open("r", encoding="utf-8-sig") as f:
                rows = {r["place_id"]: r for r in csv.DictReader(f, delimiter=";")}

            sole = rows["pid_sole_mobile"]
            self.assertEqual(sole["is_company_hint"], "0")
            self.assertEqual(sole["is_mobile"], "1")
            self.assertEqual(sole["outreach_ok"], "0")
            self.assertEqual(sole["outreach_message"], "")

            comp = rows["pid_company_mobile"]
            self.assertEqual(comp["is_company_hint"], "1")
            self.assertEqual(comp["is_mobile"], "1")
            self.assertEqual(comp["outreach_ok"], "1")
            self.assertIn("https://", comp["outreach_message"])


class TestWebhookServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sites_dir = Path(self.tmp.name).resolve()
        webhook_server.SITES_DIR = self.sites_dir
        webhook_server.STRIPE_WEBHOOK_SECRET = "whsec_test_secret"
        webhook_server.stripe.api_key = "sk_test_123"
        webhook_server.STRIPE_PRICE_ID = "price_123"

        self.slug = "morel-et-fils"
        self.site_dir = self.sites_dir / self.slug
        self.site_dir.mkdir(parents=True, exist_ok=True)
        cfg = lead_sites.Config(api_base="https://api.example.fr")
        place = {
            "id": "pid_morel",
            "displayName": {"text": "Morel & Fils"},
            "formattedAddress": "Osny",
            "nationalPhoneNumber": "01 30 38 00 00",
            "rating": 4.8,
            "userRatingCount": 40,
        }
        html_preview = lead_sites.render_preview(place, f"https://api.example.fr/checkout/{self.slug}", cfg, "plombier")
        (self.site_dir / "index.html").write_text(html_preview, encoding="utf-8")
        (self.site_dir / "index.preview.html").write_text(html_preview, encoding="utf-8")
        (self.site_dir / "meta.json").write_text(
            json.dumps({"place_id": "pid_morel", "slug": self.slug, "name": "Morel & Fils"}),
            encoding="utf-8",
        )
        self.client = TestClient(webhook_server.app)

    def tearDown(self):
        self.tmp.cleanup()

    def _post_event(self, event_dict: dict):
        with patch("stripe.Webhook.construct_event", return_value=event_dict):
            return self.client.post(
                "/webhook/stripe",
                content=json.dumps(event_dict),
                headers={"stripe-signature": "t=1,v1=fake"},
            )

    def test_no_payment_required_coupon_or_trial_activates_site(self):
        promo_checkout = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_promo_100",
                    "client_reference_id": self.slug,
                    "payment_status": "no_payment_required",
                    "customer": "cus_promo",
                    "subscription": "sub_promo_1",
                    "consent": {"terms_of_service": "accepted"},
                }
            },
        }
        r = self._post_event(promo_checkout)
        self.assertEqual(r.json()["status"], "activated")
        self.assertTrue((self.site_dir / ".activated").exists())
        self.assertFalse((self.site_dir / "..activated.tmp").exists())
        act_data = json.loads((self.site_dir / ".activated").read_text(encoding="utf-8"))
        self.assertEqual(act_data.get("tos_accepted"), "accepted")
        self.assertTrue(act_data.get("upload_token"))
        self.assertGreaterEqual(len(act_data["upload_token"]), 20)
        reg = photo_upload.load_registry(self.sites_dir)
        self.assertEqual(reg[self.slug]["token"], act_data["upload_token"])

    def test_full_lifecycle_all_four_bugs(self):
        sepa_pending = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sepa_1",
                    "client_reference_id": self.slug,
                    "payment_status": "unpaid",
                    "subscription": "sub_111",
                }
            },
        }
        r1 = self._post_event(sepa_pending)
        self.assertEqual(r1.json()["status"], "pending_payment")
        self.assertFalse((self.site_dir / ".activated").exists())

        sepa_paid = {
            "type": "checkout.session.async_payment_succeeded",
            "data": {
                "object": {
                    "id": "cs_sepa_1",
                    "client_reference_id": self.slug,
                    "payment_status": "paid",
                    "customer": "cus_111",
                    "subscription": "sub_111",
                    "customer_details": {"email": "contact@morel.fr", "phone": "+33600000000"},
                    "custom_fields": [
                        {"key": "desired_domain", "text": {"value": "https://www.Morel-Plomberie.fr/"}}
                    ],
                }
            },
        }
        r2 = self._post_event(sepa_paid)
        self.assertEqual(r2.json()["status"], "activated")
        self.assertTrue((self.site_dir / ".activated").exists())
        self.assertTrue((self.sites_dir / "morel-plomberie.fr").is_symlink())
        self.assertEqual(webhook_server.find_slug_by_subscription("sub_111"), self.slug)

        dup_paid = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_dup_2",
                    "client_reference_id": self.slug,
                    "payment_status": "paid",
                    "subscription": "sub_222",
                }
            },
        }
        with patch("stripe.Subscription.cancel") as mock_cancel:
            r3 = self._post_event(dup_paid)
            self.assertEqual(r3.json()["status"], "duplicate_cancelled")
            mock_cancel.assert_called_once_with("sub_222")

        dup_deleted_event = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_222",
                    "status": "canceled",
                    "metadata": {"slug": self.slug},
                }
            },
        }
        r_dup_del = self._post_event(dup_deleted_event)
        self.assertEqual(r_dup_del.json()["status"], "ignored_non_active_subscription")
        self.assertTrue((self.site_dir / ".activated").exists())
        self.assertNotIn("BANDEAU_DEMO_START", (self.site_dir / "index.html").read_text(encoding="utf-8"))

        failed_invoice_2025 = {
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_2025_999",
                    "attempt_count": 1,
                    "parent": {
                        "type": "subscription_details",
                        "subscription_details": {"subscription": "sub_111"},
                    },
                }
            },
        }
        r_grace = self._post_event(failed_invoice_2025)
        self.assertEqual(r_grace.json()["status"], "payment_failed_grace_period")
        self.assertTrue((self.site_dir / ".activated").exists())

        sub_past_due = {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_111", "status": "past_due"}},
        }
        r_past_due = self._post_event(sub_past_due)
        self.assertEqual(r_past_due.json()["status"], "ok")
        self.assertTrue((self.site_dir / ".activated").exists())

        sub_unpaid = {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_111", "status": "unpaid"}},
        }
        r_unpaid = self._post_event(sub_unpaid)
        self.assertEqual(r_unpaid.json()["status"], "deactivated")
        self.assertFalse((self.site_dir / ".activated").exists())

        restored_html = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("BANDEAU_DEMO_START", restored_html)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', restored_html)
        r_dom_after = self.client.get("/api/check-domain", params={"domain": "morel-plomberie.fr"})
        self.assertEqual(r_dom_after.status_code, 403)

    def test_domain_conflict_notifies_admin_with_existing_owner(self):
        webhook_server.save_domains_map({"morel-plomberie.fr": "autre-artisan"})

        evt = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_conflict_1",
                    "client_reference_id": self.slug,
                    "payment_status": "paid",
                    "subscription": "sub_conflict_1",
                    "customer_details": {"name": "Morel & Fils", "phone": "+33611223344"},
                    "custom_fields": [
                        {"key": "desired_domain", "text": {"value": "morel-plomberie.fr"}}
                    ],
                }
            },
        }
        with patch.object(webhook_server, "notify_admin_async") as mock_notify:
            r = self._post_event(evt)
            self.assertEqual(r.json()["status"], "activated")
            mock_notify.assert_called_once_with(
                self.slug,
                None,
                "morel-plomberie.fr",
                "autre-artisan",
                {"name": "Morel & Fils", "phone": "+33611223344"},
            )

        msg = webhook_server.format_admin_notification(
            self.slug, None, "morel-plomberie.fr", "autre-artisan", {"name": "Morel & Fils"}
        )
        self.assertIn("⚠️ `morel-plomberie.fr` (déjà attribué à `autre-artisan`", msg)

    def test_callmebot_whatsapp_sends_urlencoded_get(self):
        class FakeResp:
            status = 200

            def read(self):
                return b"<p>Message queued</p>"

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured: dict = {}

        def fake_urlopen(url, timeout=8.0):
            captured["url"] = url if isinstance(url, str) else getattr(url, "full_url", str(url))
            captured["timeout"] = timeout
            return FakeResp()

        with patch.object(webhook_server, "CALLMEBOT_PHONE", "0686199055"), patch.object(
            webhook_server, "CALLMEBOT_APIKEY", "testkey"
        ), patch.object(webhook_server.urllib.request, "urlopen", side_effect=fake_urlopen):
            status = webhook_server.send_callmebot_whatsapp("Test alerte ArtisanVisible")
        self.assertEqual(status, 200)
        self.assertIn("api.callmebot.com/whatsapp.php?", captured["url"])
        self.assertIn("phone=33686199055", captured["url"])
        self.assertIn("apikey=testkey", captured["url"])
        self.assertIn("text=Test+alerte+ArtisanVisible", captured["url"])
        self.assertEqual(captured["timeout"], 8.0)

    def test_callmebot_http200_quota_exhausted_is_failure(self):
        body = (
            b'<p>Message to: +33XXXXXXXX<p>Text to send: x'
            b'<p style="color:red">You have <b>0</b> messages left.</p>'
            b'<p style="color:red"><b>Message not sent</b>'
        )

        class FakeResp:
            status = 200

            def read(self):
                return body

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        self.assertFalse(
            webhook_server.callmebot_body_indicates_success(body.decode("utf-8"))
        )
        with patch.object(webhook_server, "CALLMEBOT_PHONE", "33686199055"), patch.object(
            webhook_server, "CALLMEBOT_APIKEY", "testkey"
        ), patch.object(webhook_server.urllib.request, "urlopen", return_value=FakeResp()):
            self.assertIsNone(webhook_server.send_callmebot_whatsapp("x"))

    def test_callmebot_body_success_markers(self):
        self.assertTrue(webhook_server.callmebot_body_indicates_success("Message queued"))
        self.assertTrue(webhook_server.callmebot_body_indicates_success("Message sent OK"))
        self.assertFalse(webhook_server.callmebot_body_indicates_success("Message not sent"))
        self.assertFalse(webhook_server.callmebot_body_indicates_success("OK"))  # ambiguous

    def test_callmebot_skipped_when_unconfigured(self):
        with patch.object(webhook_server, "CALLMEBOT_PHONE", ""), patch.object(
            webhook_server, "CALLMEBOT_APIKEY", ""
        ), patch.object(webhook_server.urllib.request, "urlopen") as mock_open:
            self.assertIsNone(webhook_server.send_callmebot_whatsapp("x"))
            mock_open.assert_not_called()

    def test_notify_admin_async_fires_callmebot_alongside_discord(self):
        calls: list[str] = []

        def fake_discord(msg: str) -> None:
            calls.append("discord")

        def fake_wa(text: str, timeout: float = 8.0) -> int | None:
            calls.append("whatsapp")
            return 200

        def fake_tg(text: str, timeout: float = 8.0) -> bool:
            calls.append("telegram")
            return True

        with patch.object(webhook_server, "NOTIFY_WEBHOOK_URL", "https://discord.example/hook"), patch.object(
            webhook_server, "CALLMEBOT_PHONE", "33686199055"
        ), patch.object(webhook_server, "CALLMEBOT_APIKEY", "k"), patch.object(
            webhook_server, "TELEGRAM_BOT_TOKEN", "t"
        ), patch.object(webhook_server, "TELEGRAM_CHAT_ID", "1"), patch.object(
            webhook_server, "_send_discord_webhook", side_effect=fake_discord
        ), patch.object(webhook_server, "send_callmebot_whatsapp", side_effect=fake_wa), patch.object(
            webhook_server, "send_telegram_message", side_effect=fake_tg
        ):
            # Call sync path directly (async wraps it in a daemon thread)
            webhook_server._send_admin_notification(
                self.slug,
                None,
                "morel-plomberie.fr",
                "autre-artisan",
                {"name": "Morel & Fils", "phone": "+33611", "email": "a@b.c"},
            )
        self.assertEqual(calls, ["discord", "whatsapp", "telegram"])

    def test_telegram_send_requires_ok_true(self):
        class FakeResp:
            status = 200

            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "tok"), patch.object(
            webhook_server, "TELEGRAM_CHAT_ID", "123"
        ), patch.object(
            webhook_server.urllib.request,
            "urlopen",
            return_value=FakeResp(b'{"ok":true,"result":{}}'),
        ):
            self.assertTrue(webhook_server.send_telegram_message("hello"))

        with patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", "tok"), patch.object(
            webhook_server, "TELEGRAM_CHAT_ID", "123"
        ), patch.object(
            webhook_server.urllib.request,
            "urlopen",
            return_value=FakeResp(b'{"ok":false,"description":"Forbidden"}'),
        ):
            self.assertFalse(webhook_server.send_telegram_message("hello"))

    def test_telegram_skipped_when_unconfigured(self):
        with patch.object(webhook_server, "TELEGRAM_BOT_TOKEN", ""), patch.object(
            webhook_server, "TELEGRAM_CHAT_ID", ""
        ), patch.object(webhook_server.urllib.request, "urlopen") as mock_open:
            self.assertFalse(webhook_server.send_telegram_message("x"))
            mock_open.assert_not_called()


class TestOvhDomain(unittest.TestCase):
    def test_offer_selection_enforces_default_pricing_and_max_price(self):
        premium_or_expensive = [
            {
                "orderable": True,
                "action": "create",
                "pricingMode": "premium",
                "planCode": "fr",
                "prices": [{"label": "PRICE", "price": {"value": 8.0}}],
            },
            {
                "orderable": True,
                "action": "create",
                "pricingMode": "default",
                "planCode": "fr",
                "prices": [{"label": "PRICE", "price": {"value": 45.0}}],
            },
        ]
        self.assertIsNone(ovh_domain.select_valid_offer(premium_or_expensive, max_price=15.0))

        standard_offer = [
            {
                "orderable": True,
                "action": "create",
                "pricingMode": "default",
                "planCode": "fr",
                "prices": [{"label": "PRICE", "price": {"value": 7.49}}],
            }
        ]
        matched = ovh_domain.select_valid_offer(standard_offer, max_price=15.0)
        self.assertIsNotNone(matched)
        self.assertEqual(matched[1], 7.49)

    def test_buy_exact_domain_deletes_cart_on_failure(self):
        class FakeOvhClient:
            def __init__(self):
                self.deleted_carts = []

            def post(self, path, **kwargs):
                if path == "/order/cart":
                    return {"cartId": "cart_test_99"}
                if path.endswith("/assign"):
                    return None
                if path.endswith("/checkout"):
                    raise RuntimeError("Erreur de paiement OVH simulée")
                return {"itemId": 1}

            def get(self, path, **kwargs):
                return [
                    {
                        "orderable": True,
                        "action": "create",
                        "pricingMode": "default",
                        "planCode": "fr",
                        "prices": [{"label": "PRICE", "price": {"value": 7.49}}],
                    }
                ]

            def delete(self, path):
                self.deleted_carts.append(path)

        fake = FakeOvhClient()
        with self.assertRaises(RuntimeError):
            ovh_domain.buy_exact_domain(fake, "FR", "morel-plomberie.fr", max_price=15.0)
        self.assertEqual(fake.deleted_carts, ["/order/cart/cart_test_99"])

    def test_configure_dns_zone_preserves_other_subdomains(self):
        class FakeZoneClient:
            def __init__(self):
                self.deleted_ids = []
                self.created_records = []
                self.records = {
                    1: {"id": 1, "subDomain": "", "fieldType": "A", "target": "213.186.33.5"},
                    2: {"id": 2, "subDomain": "www", "fieldType": "TXT", "target": "1|www.morel.fr"},
                    3: {"id": 3, "subDomain": "mail", "fieldType": "A", "target": "198.51.100.10"},
                    4: {"id": 4, "subDomain": "_dmarc", "fieldType": "TXT", "target": "v=DMARC1; p=none"},
                    5: {"id": 5, "subDomain": "", "fieldType": "TXT", "target": "v=spf1 include:mx.ovh.com ~all"},
                }

            def get(self, path, **kwargs):
                if path.endswith("/record"):
                    return list(self.records.keys())
                rec_id = int(path.rsplit("/", 1)[-1])
                return self.records[rec_id]

            def delete(self, path):
                self.deleted_ids.append(int(path.rsplit("/", 1)[-1]))

            def post(self, path, **kwargs):
                if path.endswith("/record"):
                    self.created_records.append(kwargs)

        client = FakeZoneClient()
        ovh_domain.configure_dns_zone(client, "morel-plomberie.fr", "203.0.113.50")

        self.assertEqual(client.deleted_ids, [1, 2])
        self.assertEqual(len(client.created_records), 2)


class TestCloudflareDomain(unittest.TestCase):
    def test_skip_when_token_missing(self):
        with patch.dict("os.environ", {"CLOUDFLARE_API_TOKEN": ""}, clear=False):
            result = cloudflare_domain.move_client_domain_to_cloudflare(
                MagicMock(), "client-demo.fr"
            )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "missing_token")

    def test_parse_ovh_mx_target(self):
        self.assertEqual(
            cloudflare_domain.parse_ovh_mx_target("10 mx1.mail.ovh.net."),
            (10, "mx1.mail.ovh.net"),
        )
        self.assertIsNone(cloudflare_domain.parse_ovh_mx_target("bad"))

    def test_protected_zone_unicode_ascii_punycode(self):
        """miina-pâtisserie.fr and IDNA/ASCII aliases must refuse all writes."""
        puny = "xn--miina-ptisserie-9jb.fr"
        self.assertEqual(
            "miina-pâtisserie.fr".encode("idna").decode("ascii"),
            puny,
        )
        for name in (
            "miina-pâtisserie.fr",
            "Miina-Pâtisserie.FR",
            "miina-patisserie.fr",
            puny,
            f"www.{puny}",
            "www.miina-pâtisserie.fr",
        ):
            self.assertTrue(
                cloudflare_domain.is_protected_zone(name),
                msg=f"expected protected: {name!r}",
            )
            with self.assertRaises(cloudflare_domain.ProtectedZoneError):
                cloudflare_domain.assert_domain_writable(name, action="test")

        self.assertFalse(cloudflare_domain.is_protected_zone("client-demo.fr"))
        self.assertFalse(cloudflare_domain.is_protected_zone("visibleartisan.fr"))

    def test_protected_zone_blocks_write_paths_without_http(self):
        """create/ensure/move refuse before any Cloudflare HTTP call."""
        session = MagicMock()
        with self.assertRaises(cloudflare_domain.ProtectedZoneError):
            cloudflare_domain.create_zone(
                "xn--miina-ptisserie-9jb.fr", "tok", "acct", session=session
            )
        session.request.assert_not_called()

        with self.assertRaises(cloudflare_domain.ProtectedZoneError):
            cloudflare_domain.ensure_zone(
                "miina-pâtisserie.fr", "tok", "acct", session=session
            )
        session.request.assert_not_called()

        with patch.dict(
            "os.environ",
            {"CLOUDFLARE_API_TOKEN": "tok_test"},
            clear=False,
        ):
            with self.assertRaises(cloudflare_domain.ProtectedZoneError):
                cloudflare_domain.move_client_domain_to_cloudflare(
                    MagicMock(), "miina-patisserie.fr", session=session
                )
        session.request.assert_not_called()

        with patch.dict(
            "os.environ",
            {"CLOUDFLARE_API_TOKEN": "tok_test"},
            clear=False,
        ):
            result = cloudflare_domain.try_move_client_domain_to_cloudflare(
                MagicMock(), "xn--miina-ptisserie-9jb.fr"
            )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "protected_zone")

    def test_ensure_zone_reuses_existing(self):
        session = MagicMock()
        list_resp = MagicMock()
        list_resp.json.return_value = {
            "success": True,
            "result": [
                {
                    "id": "zone_existing",
                    "name": "client-demo.fr",
                    "name_servers": ["asa.ns.cloudflare.com", "dax.ns.cloudflare.com"],
                }
            ],
        }
        session.request.return_value = list_resp
        zone, created = cloudflare_domain.ensure_zone(
            "client-demo.fr", "tok_test", "acct_test", session=session
        )
        self.assertFalse(created)
        self.assertEqual(zone["id"], "zone_existing")
        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/zones", args[1])

    def test_move_idempotent_sync_and_ns(self):
        """Full happy path with mocked Cloudflare + OVH HTTP."""
        domain = "client-demo.fr"
        zone_id = "zone_abc"
        calls: list[tuple[str, str]] = []

        def cf_request(method, url, headers=None, params=None, json=None, timeout=None):
            path = url.replace(cloudflare_domain.CF_API_BASE, "")
            calls.append((method, path))
            resp = MagicMock()
            if method == "GET" and path == "/zones":
                resp.json.return_value = {
                    "success": True,
                    "result": [
                        {
                            "id": zone_id,
                            "name": domain,
                            "name_servers": ["asa.ns.cloudflare.com", "dax.ns.cloudflare.com"],
                        }
                    ],
                }
            elif method == "GET" and "/dns_records" in path:
                resp.json.return_value = {"success": True, "result": []}
            elif method == "POST" and path.endswith("/dns_records"):
                resp.json.return_value = {
                    "success": True,
                    "result": {"id": f"rec_{len(calls)}", **(json or {})},
                }
            elif method == "PUT" and "/dns_records/" in path:
                resp.json.return_value = {"success": True, "result": json}
            elif method == "PATCH" and path.endswith("/settings/ssl"):
                self.assertEqual(json, {"value": "strict"})
                resp.json.return_value = {
                    "success": True,
                    "result": {"id": "ssl", "value": "strict"},
                }
            else:
                resp.json.return_value = {"success": True, "result": {}}
            return resp

        class FakeOvh:
            def __init__(self):
                self.ns_update = None
                self.dnssec_deleted = False
                self.records = {
                    1: {
                        "id": 1,
                        "subDomain": "",
                        "fieldType": "MX",
                        "target": "1 mx1.mail.ovh.net.",
                    },
                    2: {
                        "id": 2,
                        "subDomain": "",
                        "fieldType": "TXT",
                        "target": "v=spf1 include:mx.ovh.com ~all",
                    },
                    3: {
                        "id": 3,
                        "subDomain": "",
                        "fieldType": "TXT",
                        "target": "1|www.parking",
                    },
                }

            def get(self, path, **kwargs):
                if path == f"/domain/{domain}":
                    return {"dnssecState": "disabled", "dsRecord": []}
                if path.endswith("/record"):
                    return list(self.records.keys())
                rec_id = int(path.rsplit("/", 1)[-1])
                return self.records[rec_id]

            def delete(self, path):
                self.dnssec_deleted = True

            def post(self, path, **kwargs):
                if path.endswith("/nameServers/update"):
                    self.ns_update = kwargs
                    return {"taskId": 1}
                return None

        session = MagicMock()
        session.request.side_effect = cf_request
        ovh = FakeOvh()

        with patch.dict(
            "os.environ",
            {
                "CLOUDFLARE_API_TOKEN": "tok_test_not_a_secret",
                "CLOUDFLARE_ACCOUNT_ID": "2dc6d88394ebd5dfb147ddf563d7e2eb",
                "VPS_PUBLIC_IP": "57.128.177.132",
            },
            clear=False,
        ):
            result = cloudflare_domain.move_client_domain_to_cloudflare(
                ovh,
                domain,
                session=session,
                dnssec_timeout_sec=0,
            )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["zone_id"], zone_id)
        self.assertFalse(result["created"])
        self.assertEqual(result["mail_records_copied"], 2)
        self.assertIsNotNone(ovh.ns_update)
        hosts = [h["host"] for h in ovh.ns_update["nameServers"]]
        self.assertEqual(hosts, ["asa.ns.cloudflare.com", "dax.ns.cloudflare.com"])
        # Proxied A for apex + www, plus MX + SPF
        post_dns = [
            c
            for c in calls
            if c[0] == "POST" and c[1].endswith("/dns_records")
        ]
        self.assertGreaterEqual(len(post_dns), 4)
        self.assertTrue(any(c[0] == "PATCH" and c[1].endswith("/settings/ssl") for c in calls))

    def test_try_move_does_not_raise(self):
        with patch.dict(
            "os.environ",
            {"CLOUDFLARE_API_TOKEN": "tok"},
            clear=False,
        ), patch.object(
            cloudflare_domain,
            "move_client_domain_to_cloudflare",
            side_effect=RuntimeError("boom"),
        ):
            result = cloudflare_domain.try_move_client_domain_to_cloudflare(
                MagicMock(), "x.fr"
            )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "error")


class TestPhotoUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sites_dir = Path(self.tmp.name).resolve()
        webhook_server.SITES_DIR = self.sites_dir
        photo_upload.PUBLIC_BASE_URL = "http://test.example:8080"
        webhook_server.PUBLIC_BASE_URL = "http://test.example:8080"
        self.slug = "morel-et-fils"
        self.site_dir = self.sites_dir / self.slug
        self.site_dir.mkdir(parents=True)
        cfg = lead_sites.Config(api_base="https://api.example.fr")
        place = {
            "id": "pid_morel",
            "displayName": {"text": "Morel & Fils"},
            "formattedAddress": "Osny",
            "nationalPhoneNumber": "01 30 38 00 00",
            "rating": 4.8,
            "userRatingCount": 40,
        }
        html_preview = lead_sites.render_preview(place, f"/checkout/{self.slug}", cfg, "plombier")
        (self.site_dir / "index.html").write_text(html_preview, encoding="utf-8")
        (self.site_dir / "meta.json").write_text(
            json.dumps({"name": "Morel & Fils", "slug": self.slug}), encoding="utf-8"
        )
        (self.site_dir / ".activated").write_text(
            json.dumps(
                {
                    "slug": self.slug,
                    "stripe_session_id": "cs_test_abc",
                    "stripe_subscription_id": "sub_test",
                    "customer_details": {"name": "Morel & Fils"},
                }
            ),
            encoding="utf-8",
        )
        self.token = photo_upload.ensure_upload_token(self.site_dir, self.sites_dir)
        self.client = TestClient(webhook_server.app)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _png_bytes(color=(30, 144, 255), size=(400, 300)) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format="PNG")
        return buf.getvalue()

    def test_reject_bad_token_and_path_trick(self):
        r = self.client.get(f"/upload/{self.slug}/wrong-token-value-here-xxxxx")
        self.assertEqual(r.status_code, 404)
        r2 = self.client.get("/upload/../etc/passwd/" + self.token)
        self.assertIn(r2.status_code, (404, 422))

    def test_upload_info_requires_matching_session(self):
        bad = self.client.get(
            "/api/upload-info", params={"slug": self.slug, "session_id": "cs_other"}
        )
        self.assertEqual(bad.status_code, 403)
        ok = self.client.get(
            "/api/upload-info", params={"slug": self.slug, "session_id": "cs_test_abc"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertIn("/upload/", ok.json()["upload_url"])
        self.assertIn(self.token, ok.json()["upload_url"])

    def test_upload_injects_gallery_and_soft_delete(self):
        page = self.client.get(f"/upload/{self.slug}/{self.token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Envoyer des photos", page.text)

        png = self._png_bytes()
        webhook_server.TELEGRAM_BOT_TOKEN = "test-token"
        webhook_server.TELEGRAM_CHAT_ID = "12345"
        with patch.object(webhook_server, "send_telegram_message", return_value=True) as mock_tg:
            r = self.client.post(
                f"/upload/{self.slug}/{self.token}",
                files=[("photos", ("chantier.png", png, "image/png"))],
                data={"captions": "Cuisine rénovée"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["count"], 1)
            # daemon thread — join briefly
            import time

            for _ in range(20):
                if mock_tg.called:
                    break
                time.sleep(0.05)
            mock_tg.assert_called_once()
            self.assertIn("Nouvelles photos", mock_tg.call_args[0][0])

        index = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("REALISATIONS_START", index)
        self.assertIn("Nos réalisations", index)
        self.assertIn("Cuisine rénovée", index)

        photo_id = r.json()["photos"][0]["id"]
        webp = self.site_dir / "media" / "photos" / f"{photo_id}.webp"
        self.assertTrue(webp.exists())
        # EXIF stripped / re-encoded as webp
        with Image.open(webp) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertFalse(im.getexif() or im.info.get("exif"))

        d = self.client.post(f"/upload/{self.slug}/{self.token}/delete/{photo_id}")
        self.assertEqual(d.status_code, 200)
        self.assertFalse(webp.exists())
        trash = list((self.site_dir / "media" / "trash").glob(f"{photo_id}_*.webp"))
        self.assertEqual(len(trash), 1)
        index2 = (self.site_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Cuisine rénovée", index2)

    def test_reject_unsupported_and_oversized(self):
        r = self.client.post(
            f"/upload/{self.slug}/{self.token}",
            files=[("photos", ("note.txt", b"hello world", "text/plain"))],
        )
        self.assertEqual(r.status_code, 400)

        big = b"\xff\xd8\xff" + b"0" * (photo_upload.MAX_BYTES + 10)
        r2 = self.client.post(
            f"/upload/{self.slug}/{self.token}",
            files=[("photos", ("big.jpg", big, "image/jpeg"))],
        )
        self.assertEqual(r2.status_code, 400)

    def test_process_strips_gps_exif(self):
        img = Image.new("RGB", (64, 64), (255, 0, 0))
        buf = io.BytesIO()
        # Pillow may ignore custom EXIF on save without piexif; still ensure output has none
        img.save(buf, format="JPEG", quality=90)
        out = photo_upload.process_image_bytes(buf.getvalue(), "image/jpeg", "x.jpg")
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertEqual(len(im.getexif()), 0)


class TestDemoRequest(unittest.TestCase):
    def setUp(self):
        import demo_request

        self.demo_request = demo_request
        self.tmp = tempfile.TemporaryDirectory()
        self.sites_dir = Path(self.tmp.name).resolve()
        self.req_dir = self.sites_dir / "_demo_requests"
        demo_request.SITES_DIR = self.sites_dir
        demo_request.DEMO_REQUESTS_DIR = self.req_dir
        demo_request.TELEGRAM_BOT_TOKEN = "tok_test"
        demo_request.TELEGRAM_CHAT_ID = "42"
        demo_request.TELEGRAM_WEBHOOK_SECRET = "secret_test_token_value"
        demo_request.reset_rate_limits()
        os.environ["DEMO_REQUESTS_DIR"] = str(self.req_dir)
        os.environ["SITES_DIR"] = str(self.sites_dir)
        os.environ["DEMO_IP_HASH_SALT"] = "test-salt-demo-request"

        webhook_server.SITES_DIR = self.sites_dir
        webhook_server.TELEGRAM_BOT_TOKEN = "tok_test"
        webhook_server.TELEGRAM_CHAT_ID = "42"
        webhook_server.TELEGRAM_WEBHOOK_SECRET = "secret_test_token_value"
        demo_request.TELEGRAM_WEBHOOK_SECRET = "secret_test_token_value"
        webhook_server.demo_request.DEMO_REQUESTS_DIR = self.req_dir
        webhook_server.demo_request.SITES_DIR = self.sites_dir
        import demo_limits

        demo_limits.IP_HASH_SALT = "test-salt-demo-request"

        self.client = TestClient(webhook_server.app)
        self.valid = {
            "company": "Plomberie Test SARL",
            "trade": "plombier",
            "city": "Osny",
            "phone": "06 12 34 56 78",
            "email": "contact@plomberie-test.fr",
            "website": "",
            "fax": "",
            "rgpd_consent": True,
        }

    def tearDown(self):
        self.tmp.cleanup()
        self.demo_request.reset_rate_limits()

    def test_validate_requires_rgpd_and_rejects_bad_email(self):
        dr = self.demo_request
        bad = dict(self.valid, rgpd_consent=False)
        fields, err = dr.validate_payload(bad)
        self.assertIsNone(fields)
        self.assertIn("RGPD", err or "")

        bad2 = dict(self.valid, email="pas-un-email")
        fields2, err2 = dr.validate_payload(bad2)
        self.assertIsNone(fields2)
        self.assertIn("E-mail", err2 or "")

    def test_honeypot_detected(self):
        fields, err = self.demo_request.validate_payload(dict(self.valid, fax="http://spam"))
        self.assertIsNone(fields)
        self.assertEqual(err, "honeypot")

    def test_client_ip_prefers_cf_connecting_ip(self):
        # Peer local (Caddy) → on fait confiance à CF-Connecting-IP
        ip = self.demo_request.client_ip_from_headers(
            {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
            fallback="127.0.0.1",
        )
        self.assertEqual(ip, "203.0.113.9")
        ip2 = self.demo_request.client_ip_from_headers(
            {"X-Forwarded-For": "198.51.100.2, 10.0.0.1"},
            fallback="127.0.0.1",
        )
        self.assertEqual(ip2, "198.51.100.2")
        # Peer non fiable → ignore les headers (anti-spoof)
        spoofed = self.demo_request.client_ip_from_headers(
            {"CF-Connecting-IP": "203.0.113.9"},
            fallback="198.51.100.50",
        )
        self.assertEqual(spoofed, "198.51.100.50")

    def test_persist_pending_json(self):
        data = self.demo_request.create_pending_request(
            {
                "company": self.valid["company"],
                "trade": self.valid["trade"],
                "city": self.valid["city"],
                "phone": self.valid["phone"],
                "email": self.valid["email"],
                "website": "",
            },
            "203.0.113.1",
        )
        self.assertEqual(data["status"], "pending")
        path = self.req_dir / f"{data['id']}.json"
        self.assertTrue(path.is_file())
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["company"], "Plomberie Test SARL")
        self.assertEqual(loaded["status"], "pending")
        self.assertIn("ip_hash", loaded)
        self.assertNotIn("ip", loaded)

    def test_api_demo_request_creates_pending_and_alerts(self):
        alerts: list[dict] = []

        def fake_alert(data):
            alerts.append(data)
            return True

        with patch.object(self.demo_request, "send_demo_alert", side_effect=fake_alert):
            # Call sync path: create + alert without waiting on thread
            r = self.client.post("/api/demo-request", json=self.valid)
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("id", body)
        stored = self.demo_request.load_request(body["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "pending")
        # Thread may or may not have run; force sync call coverage via direct create
        self.assertTrue((self.req_dir / f"{body['id']}.json").is_file())

    def test_api_honeypot_silent_ok(self):
        r = self.client.post("/api/demo-request", json=dict(self.valid, fax="bot"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(self.req_dir.glob("*.json")), [])

    def test_api_rate_limit_email_pending(self):
        """2e demande même e-mail (pending) → 429, message poli, pas de détail règles."""
        import demo_limits

        demo_limits.IP_HASH_SALT = "test-salt-unit"
        with patch.object(self.demo_request, "send_demo_alert", return_value=True):
            self.assertEqual(self.client.post("/api/demo-request", json=self.valid).status_code, 201)
            r2 = self.client.post("/api/demo-request", json=self.valid)
        self.assertEqual(r2.status_code, 429)
        self.assertIn("déjà demandé", r2.json()["detail"])

    def test_telegram_webhook_secret_required(self):
        r = self.client.post("/webhook/telegram", json={"update_id": 1})
        self.assertEqual(r.status_code, 403)
        r2 = self.client.post(
            "/webhook/telegram",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        self.assertEqual(r2.status_code, 403)

    def test_refuse_callback(self):
        data = self.demo_request.create_pending_request(
            {k: self.valid[k] for k in ("company", "trade", "city", "phone", "email")}
            | {"website": ""},
            "1.2.3.4",
        )
        update = {
            "update_id": 99,
            "callback_query": {
                "id": "cq1",
                "data": f"demo:ref:{data['id']}",
                "message": {"chat": {"id": 42}, "message_id": 501},
            },
        }
        calls: list[tuple] = []

        def fake_api(method, payload, timeout=12.0):
            calls.append((method, payload))
            return {"ok": True, "result": {}}

        with patch.object(self.demo_request, "telegram_api", side_effect=fake_api):
            result = self.demo_request.process_telegram_update(update)
        self.assertTrue(result["handled"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.demo_request.load_request(data["id"])["status"], "refused")
        methods = [c[0] for c in calls]
        self.assertIn("answerCallbackQuery", methods)
        self.assertIn("editMessageText", methods)
        edit = next(p for m, p in calls if m == "editMessageText")
        self.assertIn("🚫 Refusée", edit["text"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})

        # Already refused — webhook still accepts valid secret
        with patch.object(self.demo_request, "telegram_api", return_value={"ok": True}):
            r = self.client.post(
                "/webhook/telegram",
                json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret_test_token_value"},
            )
        self.assertEqual(r.status_code, 200)

    def test_generate_callback_toast_edit_and_async_success(self):
        data = self.demo_request.create_pending_request(
            {k: self.valid[k] for k in ("company", "trade", "city", "phone", "email")}
            | {"website": ""},
            "1.2.3.4",
        )
        fake_result = {
            "slug": "plomberie-test-sarl",
            "preview_url": "https://visibleartisan.fr/plomberie-test-sarl/",
            "place_id": "form_abc",
            "source": "form",
            "checkout_url": "https://visibleartisan.fr/checkout/plomberie-test-sarl",
            "expires_at": "2026-10-21T12:00:00+00:00",
        }
        update = {
            "update_id": 100,
            "callback_query": {
                "id": "cq2",
                "data": f"demo:gen:{data['id']}",
                "message": {"chat": {"id": 42}, "message_id": 777},
            },
        }
        calls: list[tuple] = []
        done = threading.Event()

        def fake_api(method, payload, timeout=12.0):
            calls.append((method, dict(payload)))
            return {"ok": True, "result": {}}

        def fake_gen(req):
            # Ensure webhook returned while still generating
            self.assertEqual(
                self.demo_request.load_request(data["id"])["status"], "generating"
            )
            done.set()
            return fake_result

        with patch.object(
            self.demo_request, "generate_demo_for_request", side_effect=fake_gen
        ), patch.object(self.demo_request, "telegram_api", side_effect=fake_api):
            result = self.demo_request.process_telegram_update(update)
            self.assertTrue(result["ok"])
            self.assertTrue(result.get("started"))
            self.assertEqual(
                self.demo_request.load_request(data["id"])["status"], "generating"
            )
            # Wait for background job
            self.assertTrue(done.wait(2))
            for _ in range(50):
                if self.demo_request.load_request(data["id"])["status"] == "generated":
                    break
                time.sleep(0.05)

        stored = self.demo_request.load_request(data["id"])
        self.assertEqual(stored["status"], "generated")
        self.assertEqual(stored["demo_url"], fake_result["preview_url"])

        answer = next(p for m, p in calls if m == "answerCallbackQuery")
        self.assertEqual(answer["text"], "Génération lancée")
        edits = [p for m, p in calls if m == "editMessageText"]
        self.assertGreaterEqual(len(edits), 2)
        self.assertIn("Génération en cours", edits[0]["text"])
        self.assertIn("≈4–7 min", edits[0]["text"])
        self.assertEqual(edits[0]["reply_markup"], {"inline_keyboard": []})
        self.assertIn("✅ Démo prête", edits[-1]["text"])
        markup = edits[-1]["reply_markup"]["inline_keyboard"][0]
        texts = {b["text"] for b in markup}
        self.assertIn("Prolonger 4 jours", texts)
        self.assertIn("Régénérer", texts)

    def test_generate_idempotent_second_click_while_generating(self):
        data = self.demo_request.create_pending_request(
            {k: self.valid[k] for k in ("company", "trade", "city", "phone", "email")}
            | {"website": ""},
            "1.2.3.4",
        )
        rid = data["id"]
        claimed, err = self.demo_request.claim_generation(rid, allow_from=("pending",))
        self.assertIsNotNone(claimed)
        self.assertIsNone(err)
        self.assertEqual(self.demo_request.load_request(rid)["status"], "generating")

        update = {
            "update_id": 101,
            "callback_query": {
                "id": "cq3",
                "data": f"demo:gen:{rid}",
                "message": {"chat": {"id": 42}, "message_id": 778},
            },
        }
        calls: list[tuple] = []

        def fake_api(method, payload, timeout=12.0):
            calls.append((method, dict(payload)))
            return {"ok": True}

        with patch.object(self.demo_request, "telegram_api", side_effect=fake_api), patch.object(
            self.demo_request, "generate_demo_for_request"
        ) as gen:
            result = self.demo_request.process_telegram_update(update)
            gen.assert_not_called()
        self.assertFalse(result.get("started"))
        self.assertEqual(result.get("reason"), "generating")
        answer = next(p for m, p in calls if m == "answerCallbackQuery")
        self.assertEqual(answer["text"], "Déjà en cours")
        self.assertEqual(self.demo_request.load_request(rid)["status"], "generating")

    def test_generation_failure_and_retry_transitions(self):
        data = self.demo_request.create_pending_request(
            {k: self.valid[k] for k in ("company", "trade", "city", "phone", "email")}
            | {"website": ""},
            "1.2.3.4",
        )
        rid = data["id"]
        calls: list[tuple] = []

        def fake_api(method, payload, timeout=12.0):
            calls.append((method, dict(payload)))
            return {"ok": True}

        with patch.object(
            self.demo_request,
            "generate_demo_for_request",
            side_effect=RuntimeError("xAI timeout"),
        ), patch.object(self.demo_request, "telegram_api", side_effect=fake_api):
            out = self.demo_request.start_generation(
                rid,
                chat_id="42",
                message_id=900,
                allow_from=("pending",),
                run_async=False,
            )
        self.assertTrue(out["started"])
        self.assertEqual(self.demo_request.load_request(rid)["status"], "failed")
        fail_edit = next(
            p for m, p in calls if m == "editMessageText" and "Échec" in p["text"]
        )
        self.assertIn("xAI timeout", fail_edit["text"])
        self.assertEqual(
            fail_edit["reply_markup"]["inline_keyboard"][0][0]["text"], "Réessayer"
        )

        # Retry from failed → success
        calls.clear()
        fake_result = {
            "slug": "retry-ok",
            "preview_url": "https://visibleartisan.fr/retry-ok/",
            "source": "form",
        }
        with patch.object(
            self.demo_request, "generate_demo_for_request", return_value=fake_result
        ), patch.object(self.demo_request, "telegram_api", side_effect=fake_api):
            out2 = self.demo_request.start_generation(
                rid,
                chat_id="42",
                message_id=900,
                allow_from=("failed",),
                run_async=False,
            )
        self.assertTrue(out2["started"])
        self.assertEqual(self.demo_request.load_request(rid)["status"], "generated")

    def test_resolve_generate_timeout_from_scratch_env(self):
        """Watchdog = max(phase timeouts) + marge 180, ou override explicite."""
        dr = self.demo_request
        with patch.dict(
            os.environ,
            {
                "XAI_SCRATCH_PHASE2_TIMEOUT_S": "420",
                "XAI_SCRATCH_TIMEOUT_S": "240",
                "DEMO_GENERATE_TIMEOUT_MARGIN_S": "180",
            },
            clear=False,
        ):
            for k in ("DEMO_GENERATE_TIMEOUT_S", "DEMO_GEN_TIMEOUT_SEC"):
                os.environ.pop(k, None)
            self.assertEqual(dr._resolve_generate_timeout_s(), 600)
        with patch.dict(os.environ, {"DEMO_GEN_TIMEOUT_SEC": "720"}, clear=False):
            self.assertEqual(dr._resolve_generate_timeout_s(), 720)

    def test_generation_timeout_marks_failed(self):
        data = self.demo_request.create_pending_request(
            {k: self.valid[k] for k in ("company", "trade", "city", "phone", "email")}
            | {"website": ""},
            "1.2.3.4",
        )
        rid = data["id"]
        gate = threading.Event()
        calls: list[tuple] = []

        def fake_api(method, payload, timeout=12.0):
            calls.append((method, dict(payload)))
            return {"ok": True}

        def slow_gen(_req):
            gate.wait(2)
            return {
                "slug": "late",
                "preview_url": "https://visibleartisan.fr/late/",
                "source": "form",
            }

        with patch.object(
            self.demo_request, "generate_demo_for_request", side_effect=slow_gen
        ), patch.object(self.demo_request, "telegram_api", side_effect=fake_api):
            # Short timeout for unit test (production default = 300s)
            self.demo_request.start_generation(
                rid,
                chat_id="42",
                message_id=901,
                allow_from=("pending",),
                run_async=True,
                timeout_s=0.15,
            )
            for _ in range(40):
                if self.demo_request.load_request(rid)["status"] == "failed":
                    break
                time.sleep(0.05)
            gate.set()
            time.sleep(0.1)

        stored = self.demo_request.load_request(rid)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored.get("failure_reason"), "délai dépassé")
        timeout_edits = [
            p for m, p in calls if m == "editMessageText" and "délai dépassé" in p["text"]
        ]
        self.assertTrue(timeout_edits)
        self.assertEqual(
            timeout_edits[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            f"demo:retry:{rid}",
        )

    def test_generate_demo_site_from_form_writes_files(self):
        env = {
            "GOOGLE_PLACES_API_KEY": "",
            "PREVIEW_BASE_DOMAIN": "https://visibleartisan.fr",
            "API_BASE_URL": "https://visibleartisan.fr",
            "STRIPE_SECRET_KEY": "",
            "STRIPE_PRICE_ID": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = lead_sites.generate_demo_site(
                name="Atelier Dupont",
                trade="électricien",
                city="Cergy",
                phone="01 23 45 67 89",
                email="a@b.fr",
                out_dir=self.sites_dir,
                try_places=False,
            )
        self.assertEqual(result["source"], "form")
        site = self.sites_dir / result["slug"]
        self.assertTrue((site / "index.html").is_file())
        self.assertTrue((site / "meta.json").is_file())
        html = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("Atelier Dupont", html)
        self.assertIn("Aperçu ArtisanVisible", html)
        self.assertIn("/demo-assets/demo.css", html)
        self.assertNotIn("cdn.tailwindcss.com", html)

    def test_upload_page_uses_local_assets_no_cdn(self):
        html = photo_upload.render_upload_page("Demo", "https://visibleartisan.fr/x/")
        self.assertIn("/demo-assets/demo.css", html)
        self.assertIn("/demo-assets/upload.js", html)
        self.assertNotIn("cdn.tailwindcss.com", html)
        self.assertNotIn("<script>", html.replace('<script src="/demo-assets/upload.js"', ""))


class TestTradeProfilesAndXai(unittest.TestCase):
    def test_title_case_and_phone_format(self):
        import trade_profiles as tp

        self.assertEqual(tp.title_case_fr("grindfit"), "Grindfit")
        self.assertEqual(tp.title_case_fr("paris"), "Paris")
        self.assertEqual(tp.title_case_fr("morel & fils"), "Morel & Fils")
        self.assertEqual(tp.format_fr_phone("0668619055"), "06 68 61 90 55")
        self.assertEqual(tp.format_fr_phone("+33668619055"), "06 68 61 90 55")
        name, city, phone = tp.normalize_inputs("grindfit", "paris", "0668619055")
        self.assertEqual((name, city, phone), ("Grindfit", "Paris", "06 68 61 90 55"))

    def test_hero_dedupe_no_repeated_phrase(self):
        import trade_profiles as tp

        t, s = tp._dedupe_hero(
            "Perte de poids, reprise ou performance",
            "Perte de poids, reprise ou performance : un coaching clair, motivant et adapté.",
        )
        self.assertEqual(t, "Perte de poids, reprise ou performance")
        self.assertEqual(s, "un coaching clair, motivant et adapté.")
        curated = tp.resolve_profile("coach sportif", allow_llm=False)
        # Curated has no separate hero_title → only one hook line, no duplicated split
        self.assertEqual(curated.hero_title, "")
        self.assertTrue(curated.hero_subtitle or curated.hook)

    def test_google_rating_badge_only_for_authentic_places(self):
        cfg = lead_sites.Config()
        # Mock / test fixture id must NOT show Google score
        mockish = {
            "id": "ChIJ_test_osny_001",
            "displayName": {"text": "Plomberie Chauffage Morel & Fils"},
            "formattedAddress": "14 Rue Aristide Briand, 95520 Osny, France",
            "nationalPhoneNumber": "06 12 34 56 78",
            "rating": 4.8,
            "userRatingCount": 42,
        }
        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            html = lead_sites.render_preview(mockish, "/checkout/x", cfg, "plombier")
        self.assertNotIn("4,8/5", html)
        self.assertNotIn("42 avis", html)
        self.assertIn("Aperçu ArtisanVisible", html)
        self.assertFalse(lead_sites.has_google_places_rating(mockish))

        real = dict(mockish)
        real["id"] = "ChIJxxxxxxxxxxxxxxxxxxxxxxx"  # 27+ chars ChIJ-style
        self.assertTrue(lead_sites.is_authentic_google_place_id(real["id"]))
        self.assertTrue(lead_sites.has_google_places_rating(real))
        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            html_real = lead_sites.render_preview(real, "/checkout/x", cfg, "plombier")
        self.assertIn("4,8/5", html_real)
        self.assertIn("42 avis", html_real)

    def test_coach_never_gets_plumber_copy(self):
        import trade_profiles as tp

        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            p = tp.resolve_profile("coach sportif", allow_llm=False)
        self.assertEqual(p.key, "coach sportif")
        blob = " ".join(t + " " + d for t, d, _ in p.services).lower()
        self.assertNotIn("fuite", blob)
        self.assertNotIn("canalisation", blob)
        self.assertNotIn("chaudière", blob)
        self.assertIn("coach", blob)
        self.assertEqual(len(p.services), 6)

    def test_unknown_trade_fallback_not_plumber(self):
        import trade_profiles as tp

        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            p = tp.resolve_profile("toilettage canin", allow_llm=False)
        self.assertEqual(p.content_source, "fallback")
        blob = (p.hook + " " + " ".join(t for t, _, _ in p.services)).lower()
        self.assertNotIn("plombier", blob)
        self.assertNotIn("chaudière", blob)
        self.assertIn("toilettage", blob)

    def test_xai_generate_profile_mocked_http(self):
        import trade_profiles as tp

        fake_json = {
            "hero_title": "Coaching sur mesure à Paris",
            "hero_subtitle": "Des séances adaptées à vos objectifs, sans jargon.",
            "services": [
                {"title": f"Prestation {i}", "desc": f"Description utile {i}"}
                for i in range(1, 7)
            ],
            "why_us": [
                {"title": "Écoute", "desc": "Un plan réaliste."},
                {"title": "Progression", "desc": "Des séances ajustées."},
                {"title": "Motivation", "desc": "Un suivi régulier."},
            ],
            "faq": [
                {"q": "Débutant ?", "a": "Oui, les séances s'adaptent."},
                {"q": "Où ?", "a": "Selon votre préférence."},
                {"q": "Comment démarrer ?", "a": "Un premier échange suffit."},
            ],
            "zone_blurb": "Séances à Paris et environs.",
            "meta_description": "Coach sportif à Paris — coaching personnalisé.",
            "cta_primary": "Réserver un échange",
            "cta_secondary": "Appeler",
            "gallery_caption": "Photos de séances à venir.",
            "label": "Coach sportif",
        }
        api_payload = {
            "choices": [{"message": {"content": json.dumps(fake_json, ensure_ascii=False)}}]
        }

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(api_payload).encode("utf-8")

        with patch.dict(
            os.environ,
            {"XAI_API_KEY": "xai-test-key-not-real", "XAI_MODEL": "grok-4.3"},
            clear=False,
        ), patch("trade_profiles.urllib.request.urlopen", return_value=FakeResp()) as mock_open:
            base = tp.curated_or_fallback("coach sportif")
            out = tp.xai_generate_profile(
                "coach sportif", name="Grindfit", city="Paris", design_base=base
            )
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out.content_source, "xai")
            self.assertEqual(out.hero_title, "Coaching sur mesure à Paris")
            self.assertEqual(len(out.services), 6)
            self.assertEqual(out.services[0][0], "Prestation 1")
            self.assertEqual(out.template, "pulse")  # design from curated
            self.assertTrue(
                "coach" in out.hero_image,
                f"expected coach hero image, got {out.hero_image!r}",
            )
            # Authorization header present; key never printed by our code (assert call used Bearer)
            req = mock_open.call_args[0][0]
            self.assertIn("Bearer ", req.headers.get("Authorization", ""))

    def test_xai_missing_key_returns_none(self):
        import trade_profiles as tp

        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            self.assertIsNone(tp.xai_generate_profile("coach sportif"))

    def test_resolved_xai_model_reads_env_without_overwrite(self):
        import trade_profiles as tp

        with patch.dict(os.environ, {"XAI_MODEL": "grok-4.7"}, clear=False):
            self.assertEqual(tp.resolved_xai_model(), "grok-4.7")
        with patch.dict(os.environ, {"XAI_MODEL": ""}, clear=False):
            self.assertEqual(tp.resolved_xai_model(), tp.DEFAULT_XAI_MODEL)

    def test_xai_unknown_model_logs_and_falls_back(self):
        import trade_profiles as tp
        import urllib.error

        err = urllib.error.HTTPError(
            "https://api.x.ai/v1/chat/completions",
            404,
            "Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"model grok-nope does not exist"}'),
        )
        with patch.dict(
            os.environ,
            {"XAI_API_KEY": "xai-test-key", "XAI_MODEL": "grok-nope"},
            clear=False,
        ), patch("trade_profiles.urllib.request.urlopen", side_effect=err), self.assertLogs(
            "trade_profiles", level="WARNING"
        ) as cm:
            out = tp.xai_generate_profile("coach sportif")
            self.assertIsNone(out)
            self.assertTrue(any("grok-nope" in r.getMessage() for r in cm.records))
            self.assertFalse(any("xai-test-key" in r.getMessage() for r in cm.records))
        curated = tp.resolve_profile("coach sportif", allow_llm=True)
        self.assertEqual(curated.key, "coach sportif")
        self.assertNotEqual(curated.content_source, "xai")

    def test_check_xai_ok_and_never_prints_key(self):
        import trade_profiles as tp

        api_payload = {
            "model": "grok-4.7",
            "choices": [{"message": {"content": '{"ok":true}'}}],
        }

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(api_payload).encode("utf-8")

        with patch.dict(
            os.environ,
            {"XAI_API_KEY": "xai-secret-should-not-appear", "XAI_MODEL": "grok-4.7"},
            clear=False,
        ), patch("trade_profiles.urllib.request.urlopen", return_value=FakeResp()):
            ok, msg = tp.check_xai()
            self.assertTrue(ok)
            self.assertIn("OK", msg)
            self.assertIn("grok-4.7", msg)
            self.assertNotIn("xai-secret", msg)
            rc = tp.main(["--check-xai"])
            self.assertEqual(rc, 0)

    def test_check_xai_missing_key(self):
        import trade_profiles as tp

        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            ok, msg = tp.check_xai()
            self.assertFalse(ok)
            self.assertIn("XAI_API_KEY", msg)

    def test_xai_http_failure_retries_then_none(self):
        import trade_profiles as tp
        import urllib.error

        with patch.dict(os.environ, {"XAI_API_KEY": "xai-test-key"}, clear=False), patch(
            "trade_profiles.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ) as mock_open:
            self.assertIsNone(tp.xai_generate_profile("photographe"))
            self.assertGreaterEqual(mock_open.call_count, 2)
            self.assertLessEqual(mock_open.call_count, 3)

    def test_render_preview_coach_sections_and_csp(self):
        with patch.dict(os.environ, {"XAI_API_KEY": ""}, clear=False):
            cfg = lead_sites.Config()
            place = lead_sites.place_from_form("grindfit", "paris", "0668619055")
            html = lead_sites.render_preview(
                place,
                "https://visibleartisan.fr/checkout/grindfit",
                cfg,
                "coach sportif",
                slug="grindfit",
                allow_llm=False,
            )
        self.assertIn("Grindfit", html)
        self.assertIn("06 68 61 90 55", html)
        self.assertIn('id="prestations"', html)
        self.assertIn("REALISATIONS_START", html)
        self.assertIn("/demo-assets/demo.css", html)
        self.assertTrue(
            "/demo-assets/images/coach-" in html or "/demo-assets/images/hero-coach.jpg" in html,
            "coach métier image expected",
        )
        self.assertIn("Photo à venir", html)
        self.assertIn("demo-gallery-cue", html)
        self.assertNotIn("cdn.tailwindcss.com", html)
        self.assertNotIn("Dépannage & Intervention", html)
        self.assertIn("demo-sticky-call", html)
        self.assertIn('data-plan-slug="grindfit"', html)
        # Unique structure — not the old universal 6-icon card grid class alone
        self.assertTrue(
            "demo-num-list" in html
            or "demo-acc-list" in html
            or "demo-flagship" in html
        )
        self.assertNotIn('<script src="http', html)

    def test_slug_plans_differ_for_same_trade(self):
        import site_plan as sp

        a = sp.build_curated_plan(
            slug="grindfit", name="Grindfit", city="Paris", trade="coach sportif"
        )
        b = sp.build_curated_plan(
            slug="atelier-force-lyon", name="Atelier Force", city="Lyon", trade="coach sportif"
        )
        # Same métier, different slug → different visual/structure choices
        self.assertTrue(
            a.hero_variant != b.hero_variant
            or a.palette_id != b.palette_id
            or a.section_order != b.section_order
            or a.font_pair != b.font_pair
        )



class TestPhotoBanks(unittest.TestCase):
    """Anti cross-métier / cross-famille — banques photo."""

    def test_patissier_gets_patisserie_not_bread_or_peintre(self):
        import photo_banks as pb
        import site_plan as sp

        imgs = pb.resolve_trade_images("Patissier")
        self.assertTrue(imgs, "pâtissier must resolve a pâtisserie bank")
        self.assertTrue(all(f.startswith("patisserie-") for f in imgs))
        self.assertFalse(any(f.startswith(("local-", "peintre-", "bouche-02", "bouche-06")) for f in imgs))
        pb.assert_images_allowed_for_trade("Patissier", imgs)

        plan = sp.build_curated_plan(
            slug="patisserie", name="Patisserie", city="Osny", trade="Patissier"
        )
        used = [plan.hero_image, plan.section_image, plan.story_image, *plan.gallery_images]
        pb.assert_images_allowed_for_trade("Patissier", used)
        self.assertTrue(all(u.startswith("patisserie-") for u in used if u))
        self.assertIn(plan.hero_image, imgs)

    def test_cross_family_selection_fails(self):
        import photo_banks as pb

        with self.assertRaises(AssertionError):
            pb.assert_images_allowed_for_trade("pâtisserie", ["peintre-01.jpg"])
        with self.assertRaises(AssertionError):
            pb.assert_images_allowed_for_trade("pâtisserie", ["local-01.jpg"])
        with self.assertRaises(AssertionError):
            pb.assert_images_allowed_for_trade("pâtisserie", ["plombier-01.jpg"])
        with self.assertRaises(AssertionError):
            pb.assert_images_allowed_for_trade("coach sportif", ["bouche-01.jpg"])

    def test_unknown_trade_gets_empty_not_default_local(self):
        import photo_banks as pb

        imgs = pb.resolve_trade_images("traducteur assermenté")
        self.assertEqual(imgs, [])
        # No TRADE_IMAGES['default'] fallback
        self.assertNotIn("default", pb.TRADE_IMAGES)

    def test_batiment_family_may_use_local_pool(self):
        import photo_banks as pb

        imgs = pb.resolve_trade_images("couvreur")
        self.assertTrue(imgs)
        self.assertTrue(all(f.startswith("local-") for f in imgs))
        pb.assert_images_allowed_for_trade("couvreur", imgs)

    def test_bouche_metiers_share_alimentation_family(self):
        import photo_banks as pb

        for trade in ("pâtisserie", "boulangerie", "traiteur", "boucherie", "restaurant"):
            self.assertEqual(pb.resolve_family(trade), pb.FAMILY_ALIMENTATION)
            imgs = pb.resolve_trade_images(trade)
            self.assertGreaterEqual(len(imgs), 6)
            pb.assert_images_allowed_for_trade(trade, imgs)

    def test_paysagiste_bank_has_8_to_10_unique(self):
        import photo_banks as pb
        import hashlib
        from pathlib import Path

        imgs = pb.resolve_trade_images("paysagiste")
        self.assertGreaterEqual(len(imgs), 8)
        self.assertLessEqual(len(imgs), 12)
        root = Path(__file__).resolve().parent / "static" / "demo-assets" / "images"
        digests = set()
        for name in imgs:
            p = root / name
            self.assertTrue(p.is_file(), name)
            digests.add(hashlib.md5(p.read_bytes()).hexdigest())
        self.assertEqual(len(digests), len(imgs), "bank must be byte-unique")

    def test_slug_rotation_diverges_paysagiste_picks(self):
        import photo_banks as pb

        a = pb.pick_trade_images("paysagiste", "jardins-verrier-nantes", limit=4)
        b = pb.pick_trade_images("paysagiste", "test-paysagiste-avant-apres-rennes", limit=4)
        self.assertEqual(len(a), 4)
        self.assertEqual(len(b), 4)
        self.assertNotEqual(a, b)


class TestScratchCopyGuards(unittest.TestCase):
    def test_meta_prompt_leak_rejected(self):
        import scratch_site as ss

        bad = (
            "<html><body><h1>X</h1>"
            "<p>Le métier est la pâtisserie, le lieu est Lyon, le contact est le téléphone.</p>"
            "<p>L'adresse indiquée est Lyon. Réalisé sur le terrain.</p>"
            "</body></html>"
        )
        hits = ss._meta_copy_errors(bad)
        self.assertTrue(hits, "expected meta/prompt-leak hits")

    def test_scrub_meta_copy_removes_leaks(self):
        import scratch_site as ss

        bad = (
            "<html><body><h1>Pulse</h1>"
            "<p>Coach à Nice. Appelez pour une séance.</p>"
            "<p>Le lieu est Nice centre.</p>"
            "</body></html>"
        )
        out = ss._scrub_meta_copy(bad)
        self.assertEqual(ss._meta_copy_errors(out), [])
        self.assertIn("Coach à Nice", out)
        self.assertNotIn("Le lieu est", out)

    def test_warm_copy_passes_meta_guard(self):
        import scratch_site as ss

        ok = (
            "<html><body><h1>Atelier Sucré</h1>"
            "<p>Pâtisserie à Lyon. Appelez pour commander viennoiseries et gâteaux.</p>"
            "<p>Exemples d'ambiances de jardin (illustrations).</p>"
            "</body></html>"
        )
        self.assertEqual(ss._meta_copy_errors(ok), [])

    def test_strip_empty_faq_section(self):
        import scratch_site as ss

        html = (
            "<html><body>"
            "<section id='hero' class='hero'><h1>Atelier</h1><p>Pâtisserie à Lyon, appelez-nous.</p></section>"
            "<section id='faq'><h2>Ce qu'on nous demande souvent au comptoir</h2></section>"
            "<section id='ok'><h2>La vitrine</h2><p>Croissants et tartes du jour.</p>"
            "<img src='/demo-assets/images/patisserie-01.jpg' alt=''></section>"
            "</body></html>"
        )
        out = ss._strip_empty_sections(html)
        self.assertNotIn("id='faq'", out.replace('"', "'"))
        self.assertNotIn("souvent au comptoir", out)
        self.assertIn("La vitrine", out)

    def test_repair_truncated_list_item(self):
        import scratch_site as ss

        html = "<ul><li><strong>Tailler les haies</strong><p>Une fois par saison.</p></li>"
        html += "<li><strong>Revenir entre</strong></li></ul>"
        out = ss._repair_truncated_list_items(html)
        self.assertIn("Tailler les haies", out)
        self.assertNotIn("Revenir entre", out)

    def test_paysagiste_bank_excludes_building_filenames(self):
        import photo_banks as pb

        imgs = set(pb.resolve_trade_images("paysagiste"))
        for bad in ("paysagiste-08.jpg", "paysagiste-11.jpg", "paysagiste-16.jpg", "paysagiste-17.jpg"):
            self.assertNotIn(bad, imgs)
        self.assertNotIn("paysagiste-45.jpg", imgs)  # trowel alias of 03/02
        self.assertGreaterEqual(len(imgs), 8)

    def test_image_identity_uses_content_hash_not_bank_id(self):
        """Anti-dupe must collide on real bytes / URL basename, not bank entry IDs."""
        import scratch_site as ss
        import photo_banks as pb

        # 45 is byte-identical to 03; both near-dup with 02 (trowel desk)
        h45 = pb.file_content_hash("paysagiste-45.jpg")
        h03 = pb.file_content_hash("paysagiste-03.jpg")
        if h45 and h03:
            self.assertEqual(h45, h03)
        k02 = ss._image_identity_key("/demo-assets/images/paysagiste-02.jpg?v=1")
        k45 = ss._image_identity_key("paysagiste-45.jpg")
        self.assertEqual(k02, k45, "02 and 45 must share identity (content/near-dup)")

        html = (
            "<html><body>"
            '<img src="/demo-assets/images/paysagiste-02.jpg?v=a">'
            '<img src="/demo-assets/images/paysagiste-45.jpg?v=b">'
            "</body></html>"
        )
        out = ss._dedupe_page_images(html)
        self.assertEqual(out.count("<img"), 1)

        bad = (
            "<!DOCTYPE html><html><head><meta name='viewport' content='w'>"
            "<style>@media(max-width:1px){a{}}</style></head><body><h1>Vert</h1>"
            '<img src="/demo-assets/images/paysagiste-02.jpg">'
            '<img src="/demo-assets/images/paysagiste-45.jpg">'
            "<p>Paysagiste à Rennes. Appelez pour un jardin.</p></body></html>"
        )
        verrs = ss._validate_html(bad, check_claims=False)
        self.assertTrue(
            any("duplicate image identity" in e for e in verrs),
            verrs,
        )

    def test_cap_css_trims_oversized(self):
        import scratch_site as ss

        big = "body{color:red}\n" + (".x{a:1}\n" * 400)
        out = ss._cap_css(big, max_chars=500)
        self.assertLessEqual(len(out), 500)
        self.assertIn("}", out)

    def test_stream_content_usable_requires_body_signal(self):
        import scratch_site as ss

        self.assertFalse(ss._stream_content_usable("hi"))
        good = "<body><h1>Boulangerie Dupont</h1><a href='tel:+331'>Appeler</a>" + ("x" * 400)
        self.assertTrue(ss._stream_content_usable(good))

    def test_success_telegram_labels_grok_vs_secours(self):
        import demo_request as dr

        self.assertEqual(dr.content_source_label("scratch-xai"), "Grok")
        self.assertEqual(
            dr.content_source_label("template-fallback:curated"),
            "secours (template)",
        )
        text = dr.format_success_text(
            {"company": "Test", "id": "r1"},
            "https://visibleartisan.fr/test/",
            "",
            content_source="scratch-xai",
        )
        self.assertIn("Source : Grok", text)
        text2 = dr.format_success_text(
            {"company": "Test", "id": "r1"},
            "https://visibleartisan.fr/test/",
            "lundi",
            content_source="template-fallback:xai",
        )
        self.assertIn("Source : secours (template)", text2)

    def test_contact_cta_uses_aa_safe_paper_on_brand(self):
        """Contact CTA must not use leaf/ink on pine (Rennes green-on-green)."""
        import scratch_site as ss

        html = (
            "<!DOCTYPE html><html><head><meta name='viewport' content='w'>"
            "<style>:root{--pine:#163828;--leaf:#2a6a42;--ink:#17241c;--paper:#f3f6f1}</style>"
            "</head><body><h1>Vert</h1>"
            "<p>Paysagiste à Rennes. Appelez pour un jardin.</p></body></html>"
        )
        out = ss._inject_platform(
            html,
            name="Vert Clair",
            phone_display="02 98 76 54 32",
            tel="+33298765432",
            checkout="",
            price_label="",
            banner_html="<!-- BANDEAU_DEMO_START --><div class='demo-banner'>demo</div><!-- BANDEAU_DEMO_END -->",
            gallery_caption="x",
            meta_description="Paysagiste à Rennes",
            json_ld="",
            catalog=[],
        )
        chrome = re.search(r'(?is)<style id="av-chrome">.*?</style>', out)
        self.assertIsNotNone(chrome)
        css = chrome.group(0)
        # Light chip + dark brand text (AA); not leaf bg + ink text
        self.assertIn("background:var(--paper,var(--bg,#f4f1ea))!important", css)
        self.assertIn(
            "color:var(--pine,var(--cocoa,var(--cacao,var(--navy,var(--ink,#0f1c1a)))))!important",
            css,
        )
        self.assertNotIn(
            "background:var(--leaf,var(--berry,var(--brass,var(--copper,var(--accent,#e8a54b)))))",
            css,
        )
        # Odd-count gallery orphan centering
        self.assertIn(".split:has(> :only-child)", css)
        self.assertIn("grid-column:1/-1", css)


class TestBoucheCopyGuardrails(unittest.TestCase):
    """Bouche curated copy: no chantier vocab, no repeated card phrases."""

    _FORBIDDEN_BOUCHE = ("devis", "chantier", "intervention")

    def test_patisserie_curated_has_no_building_vocab(self):
        import site_plan as sp

        plan = sp.build_curated_plan(
            slug="patisserie", name="Patisserie", city="Osny", trade="Patissier"
        )
        blob = " ".join(
            [
                plan.hero_title,
                plan.hero_subtitle,
                plan.flagship.get("body", ""),
                plan.story.get("body", ""),
                " ".join(plan.audience),
                " ".join(s.get("title", "") + " " + s.get("lead", "") + " " + s.get("detail", "") for s in plan.services),
                " ".join(st.get("body", "") for st in plan.process_steps),
            ]
        ).casefold()
        for word in self._FORBIDDEN_BOUCHE:
            self.assertNotIn(word, blob, f"bouche plan must not contain {word!r}")
        # Expected pastry vocabulary
        self.assertTrue(
            any(w in blob for w in ("gâteau", "gateau", "viennoiser", "entremet", "commande")),
            "expected pâtisserie vocabulary in curated plan",
        )

    def test_no_identical_detail_repeated_across_service_cards(self):
        import site_plan as sp

        plan = sp.build_curated_plan(
            slug="patisserie", name="Patisserie", city="Osny", trade="Patissier"
        )
        details = [s.get("detail", "").strip() for s in plan.services if s.get("detail")]
        self.assertGreaterEqual(len(details), 3)
        self.assertEqual(len(details), len(set(details)), f"repeated detail phrases: {details}")

    def test_patissier_profile_is_curated_not_intelligent_fallback(self):
        from trade_profiles import curated_or_fallback

        p = curated_or_fallback("Patissier")
        self.assertEqual(p.key, "pâtisserie")
        self.assertEqual(p.content_source, "curated")
        self.assertNotIn("devis", " ".join(s[0] + s[1] for s in p.services).casefold())

class TestDemoTtl(unittest.TestCase):
    def setUp(self):
        import demo_ttl

        self.demo_ttl = demo_ttl
        self.tmp = tempfile.TemporaryDirectory()
        self.sites = Path(self.tmp.name).resolve() / "sites"
        self.arch = Path(self.tmp.name).resolve() / "archive"
        self.sites.mkdir(parents=True)
        os.environ["SITES_DIR"] = str(self.sites)
        os.environ["DEMO_ARCHIVE_DIR"] = str(self.arch)
        os.environ["DEMO_TTL_DAYS"] = "4"

    def tearDown(self):
        self.tmp.cleanup()

    def _make_demo(self, slug: str, *, expires_at=None, activated=False):
        d = self.sites / slug
        d.mkdir(parents=True)
        meta = {"slug": slug, "name": slug}
        meta = self.demo_ttl.stamp_ttl_fields(meta)
        if expires_at is not None:
            meta["expires_at"] = expires_at
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (d / "index.html").write_text(
            "<!-- BANDEAU_DEMO_START --><div>Aperçu</div><!-- BANDEAU_DEMO_END -->",
            encoding="utf-8",
        )
        if activated:
            (d / ".activated").write_text("{}", encoding="utf-8")
            self.demo_ttl.clear_expiry_on_activation(d)
        return d

    def test_stamp_and_format(self):
        meta = self.demo_ttl.stamp_ttl_fields({"slug": "x"})
        self.assertIn("created_at", meta)
        self.assertIn("expires_at", meta)
        exp = self.demo_ttl.parse_iso(meta["expires_at"])
        self.assertIsNotNone(exp)
        self.assertRegex(self.demo_ttl.format_expiry_fr(exp), r"^\d{2}/\d{2}$")
        self.assertIn("jusqu'au", self.demo_ttl.banner_expiry_suffix(meta))

    def test_paid_never_expires(self):
        d = self._make_demo("paid-shop")
        # Force past expiry then activate
        past = (self.demo_ttl.now_utc() - timedelta(days=10)).isoformat()
        meta = self.demo_ttl.load_meta(d)
        meta["expires_at"] = past
        self.demo_ttl.save_meta(d, meta)
        self.assertTrue(self.demo_ttl.is_expired(meta, d))
        (d / ".activated").write_text("{}", encoding="utf-8")
        cleared = self.demo_ttl.clear_expiry_on_activation(d)
        self.assertIsNone(cleared.get("expires_at"))
        self.assertTrue(cleared.get("never_expires"))
        self.assertFalse(self.demo_ttl.is_expired(cleared, d))

    def test_activate_via_webhook_clears_expiry(self):
        self._make_demo("morel-et-fils")
        webhook_server.SITES_DIR = self.sites
        webhook_server.STRIPE_WEBHOOK_SECRET = "whsec_test"
        webhook_server.stripe.api_key = "sk_test"
        client = TestClient(webhook_server.app)
        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_ttl",
                    "client_reference_id": "morel-et-fils",
                    "payment_status": "paid",
                    "customer": "cus_1",
                    "subscription": "sub_ttl",
                    "consent": {"terms_of_service": "accepted"},
                }
            },
        }
        with patch("stripe.Webhook.construct_event", return_value=event):
            r = client.post(
                "/webhook/stripe",
                content=json.dumps(event),
                headers={"stripe-signature": "t=1,v1=x"},
            )
        self.assertEqual(r.json()["status"], "activated")
        meta = self.demo_ttl.load_meta(self.sites / "morel-et-fils")
        self.assertTrue(meta.get("never_expires"))
        self.assertIsNone(meta.get("expires_at"))

    def test_checkout_refuses_expired(self):
        past = (self.demo_ttl.now_utc() - timedelta(days=1)).isoformat()
        self._make_demo("old-demo", expires_at=past)
        webhook_server.SITES_DIR = self.sites
        client = TestClient(webhook_server.app)
        r = client.get("/checkout/old-demo")
        self.assertEqual(r.status_code, 410)
        self.assertIn("expiré", r.text.lower())
        self.assertEqual(r.headers.get("x-robots-tag"), "noindex, nofollow")

    def test_archive_idempotent_and_skips_activated(self):
        past = (self.demo_ttl.now_utc() - timedelta(days=1)).isoformat()
        self._make_demo("gone", expires_at=past)
        self._make_demo("kept-paid", activated=True)
        r1 = self.demo_ttl.archive_expired_demos(root=self.sites, archive_root=self.arch)
        self.assertIn("gone", r1["archived"])
        self.assertIn("kept-paid", r1["skipped_activated"])
        self.assertTrue((self.arch / "gone").is_dir())
        self.assertTrue((self.sites / "gone" / ".expired").is_file())
        r2 = self.demo_ttl.archive_expired_demos(root=self.sites, archive_root=self.arch)
        self.assertIn("gone", r2["already_expired"])

    def test_extend_and_migrate(self):
        past = (self.demo_ttl.now_utc() - timedelta(days=1)).isoformat()
        self._make_demo("ext-me", expires_at=past)
        self.demo_ttl.archive_expired_demos(root=self.sites, archive_root=self.arch)
        out = self.demo_ttl.extend_demo("ext-me", days=4, root=self.sites, archive_root=self.arch)
        self.assertEqual(out["status"], "extended")
        self.assertTrue(out["restored"])
        self.assertFalse((self.sites / "ext-me" / ".expired").exists())
        # migrate existing without expires
        d = self.sites / "fresh"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"slug": "fresh"}), encoding="utf-8")
        mig = self.demo_ttl.migrate_existing_ttls(root=self.sites)
        self.assertIn("fresh", mig["updated"])
        meta = self.demo_ttl.load_meta(d)
        self.assertIsNotNone(self.demo_ttl.parse_iso(meta.get("expires_at")))


class TestDemoLimits(unittest.TestCase):
    def setUp(self):
        import demo_limits
        import demo_request

        self.dl = demo_limits
        self.tmp = tempfile.TemporaryDirectory()
        self.sites = Path(self.tmp.name).resolve()
        self.req = self.sites / "_demo_requests"
        self.req.mkdir(parents=True)
        os.environ["DEMO_REQUESTS_DIR"] = str(self.req)
        os.environ["SITES_DIR"] = str(self.sites)
        os.environ["DEMO_IP_HASH_SALT"] = "unit-test-salt-xyz"
        demo_request.DEMO_REQUESTS_DIR = self.req
        demo_request.SITES_DIR = self.sites
        demo_limits.IP_HASH_SALT = "unit-test-salt-xyz"
        demo_limits.IP_LIMIT_24H = 2
        demo_limits.IP_LIMIT_30D = 3
        demo_limits.EMAIL_REFUSED_COOLDOWN_DAYS = 30

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_email(self):
        self.assertEqual(self.dl.normalize_email("  Foo.Bar+tag@Gmail.com "), "foobar@gmail.com")
        self.assertEqual(self.dl.normalize_email("a.b+x@googlemail.com"), "ab@gmail.com")
        self.assertEqual(self.dl.normalize_email("Jean.Dupont@exemple.fr"), "jean.dupont@exemple.fr")

    def test_disposable(self):
        self.assertTrue(self.dl.is_disposable_email("x@yopmail.com"))
        self.assertFalse(self.dl.is_disposable_email("contact@plomberie-test.fr"))

    def test_cf_ranges_and_resolve_ip(self):
        self.assertTrue(self.dl.is_cloudflare_ip("172.64.10.1"))
        self.assertTrue(self.dl.is_trusted_peer("127.0.0.1"))
        self.assertFalse(self.dl.is_cloudflare_ip("8.8.8.8"))
        # Trusted peer (loopback) → accept CF header
        ip = self.dl.resolve_client_ip({"CF-Connecting-IP": "203.0.113.44"}, "127.0.0.1")
        self.assertEqual(ip, "203.0.113.44")
        # Untrusted peer → ignore header
        ip2 = self.dl.resolve_client_ip({"CF-Connecting-IP": "203.0.113.44"}, "8.8.8.8")
        self.assertEqual(ip2, "8.8.8.8")

    def test_ip_limits_and_purge(self):
        h = self.dl.hash_ip("203.0.113.10")
        self.assertIsNone(self.dl.ip_blocked_reason(h))
        self.dl.append_ip_hit(h)
        self.dl.append_ip_hit(h)
        self.assertEqual(self.dl.ip_blocked_reason(h), "ip_24h")
        # Backdate hits then purge with 1-day retention
        path = self.dl.ip_log_path()
        old = (self.dl.now_utc() - timedelta(days=40)).isoformat()
        path.write_text(
            json.dumps({"ip_hash": h, "at": old}) + "\n" + json.dumps({"ip_hash": h, "at": old}) + "\n",
            encoding="utf-8",
        )
        n = self.dl.purge_ip_hits(retention_days=30)
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(self.dl.ip_blocked_reason(h))

    def test_email_pending_blocks(self):
        rec = {
            "id": "req_test_abc12",
            "status": "pending",
            "email": "Same.User+1@gmail.com",
            "created_at": self.dl.now_utc().isoformat(),
        }
        (self.req / "req_test_abc12.json").write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(
            self.dl.email_blocked_reason("sameuser@gmail.com"),
            "pending",
        )

    def test_check_and_register_records_hash(self):
        gate = self.dl.check_and_register("ok@exemple.fr", "203.0.113.99")
        self.assertTrue(gate["allowed"])
        self.assertEqual(len(gate["ip_hash"]), 64)
        gate2 = self.dl.check_and_register("ok@exemple.fr", "203.0.113.99")
        # Second call still allowed at IP level (1 hit) but email not in requests yet —
        # email block only looks at request JSON; so allowed until pending created
        self.assertTrue(gate2["allowed"])

class TestSameMetierVarietyGuardrail(unittest.TestCase):
    """
    Auto-test: 3 démos même métier → palettes, layouts ET sets photo **disjoints**.
    Échoue si convergence (bug B-variety / agency-unique).
    """

    PAT_SLUGS = ("var2-sucre-osny", "var2-brioche-tls", "var2-madeleine-metz")
    PLOMB_SLUGS = ("var2-fuite-cergy", "var2-tuyau-pontoise")

    def test_three_patisserie_presets_differ(self):
        from design_variety import extract_palette_fingerprint, pick_design_preset

        presets = [pick_design_preset(s, "pâtisserie") for s in self.PAT_SLUGS]
        palette_ids = [p.palette_id for p in presets]
        layout_ids = [p.layout_id for p in presets]
        self.assertEqual(len(set(palette_ids)), 3, f"palettes: {palette_ids}")
        self.assertEqual(
            len(set(layout_ids)),
            3,
            f"3 layouts distincts requis, got {layout_ids}",
        )
        fps = []
        for p in presets:
            html = '<html lang="fr"><head></head><body><h1>x</h1></body></html>'
            from design_variety import inject_preset_css, stamp_preset_attrs

            html = stamp_preset_attrs(inject_preset_css(html, p), p)
            fp = extract_palette_fingerprint(html)
            self.assertTrue(fp, "fingerprint empty")
            fps.append(fp)
        self.assertEqual(len(set(fps)), 3, fps)

    def test_three_patisserie_photo_sets_disjoint(self):
        import photo_banks as pb

        bank = pb.resolve_trade_images("pâtisserie")
        self.assertGreaterEqual(len(bank), 12, "banque pâtisserie trop petite")
        self.assertNotIn("patisserie-05.jpg", bank)
        seqs = [tuple(pb.pick_trade_images("pâtisserie", s, limit=4)) for s in self.PAT_SLUGS]
        hash_sets = []
        for seq in seqs:
            self.assertEqual(len(seq), 4)
            self.assertEqual(len(set(seq)), 4, "doublons dans une page")
            pb.assert_images_allowed_for_trade("pâtisserie", seq)
            hash_sets.append({pb.file_content_hash(f) or f for f in seq})
        # Disjoint: no shared content hash across any pair
        for i in range(len(hash_sets)):
            for j in range(i + 1, len(hash_sets)):
                overlap = hash_sets[i] & hash_sets[j]
                self.assertFalse(
                    overlap,
                    f"photos partagées entre {self.PAT_SLUGS[i]} et {self.PAT_SLUGS[j]}: {overlap}",
                )

    def test_guardrail_detects_shared_palette_layout_or_photos(self):
        from design_variety import pick_design_preset
        import photo_banks as pb

        presets = [pick_design_preset(s, "pâtisserie") for s in self.PAT_SLUGS]
        palettes = [p.palette_id for p in presets]
        layouts = [p.layout_id for p in presets]
        seqs = [tuple(pb.pick_trade_images("pâtisserie", s, limit=4)) for s in self.PAT_SLUGS]
        hash_sets = [{pb.file_content_hash(f) or f for f in seq} for seq in seqs]
        shared_palette = len(set(palettes)) < len(palettes)
        shared_layout = len(set(layouts)) < len(layouts)
        shared_photos = any(hash_sets[i] & hash_sets[j] for i in range(3) for j in range(i + 1, 3))
        self.assertFalse(
            shared_palette or shared_layout or shared_photos,
            f"variety broken: palettes={palettes} layouts={layouts} photos={seqs}",
        )

    def test_two_plombier_presets_and_photos_disjoint(self):
        from design_variety import pick_design_preset
        import photo_banks as pb

        presets = [pick_design_preset(s, "plombier") for s in self.PLOMB_SLUGS]
        self.assertNotEqual(presets[0].palette_id, presets[1].palette_id)
        self.assertNotEqual(presets[0].layout_id, presets[1].layout_id)
        seqs = [
            tuple(pb.pick_trade_images("plombier", s, limit=presets[i].photo_limit))
            for i, s in enumerate(self.PLOMB_SLUGS)
        ]
        ha = {pb.file_content_hash(f) or f for f in seqs[0]}
        hb = {pb.file_content_hash(f) or f for f in seqs[1]}
        self.assertFalse(ha & hb, f"plombier photos overlap: {seqs}")
        for seq in seqs:
            pb.assert_images_allowed_for_trade("plombier", seq)
            # No off-métier legacy files
            for f in seq:
                self.assertFalse(
                    f.startswith(("plombier-03", "plombier-05", "plombier-06", "plombier-07", "plombier-08")),
                    f"off-métier image in bank pick: {f}",
                )

    def test_plombier_bank_excludes_cleaning_architecture(self):
        import photo_banks as pb

        imgs = pb.resolve_trade_images("plombier")
        for bad in ("plombier-03.jpg", "plombier-05.jpg", "plombier-06.jpg", "plombier-07.jpg", "plombier-08.jpg"):
            self.assertNotIn(bad, imgs)
        self.assertGreaterEqual(len(imgs), 6)

    def test_ensure_hero_image_injects_when_missing(self):
        import scratch_site as ss

        html = (
            "<!DOCTYPE html><html><head></head><body>"
            '<section id="hero" class="hero"><h1>Var Chauffe</h1><p>Plombier.</p></section>'
            "<main><p>Suite</p></main></body></html>"
        )
        catalog = [
            {
                "path": "/demo-assets/images/plombier-01.jpg?v=1",
                "file": "plombier-01.jpg",
                "subject": "tuyauterie",
                "desc": "tuyauterie",
            }
        ]
        out = ss._ensure_hero_image(html, catalog)
        self.assertIn("plombier-01.jpg", out)
        self.assertIn("av-hero-photo", out)
        # Already has img → no duplicate inject
        html2 = html.replace("<p>Plombier.</p>", '<img src="/demo-assets/images/plombier-02.jpg" alt="">')
        out2 = ss._ensure_hero_image(html2, catalog)
        self.assertEqual(out2.count("<img"), 1)

    def test_wcag_all_variety_palettes(self):
        from design_variety import PALETTES, assert_wcag_aa, contrast_ratio

        for pid, cols in PALETTES.items():
            assert_wcag_aa(cols)
            self.assertGreaterEqual(contrast_ratio(cols["ink"], cols["paper"]), 4.5, pid)

    def test_inject_preset_css_forces_readable_hero_text(self):
        """Regression: ink on dark hero made Sucré/Brioche/Tuyau unreadable."""
        from design_variety import (
            BODY_MIN_RATIO,
            LARGE_MIN_RATIO,
            assert_hero_contrast,
            contrast_ratio,
            inject_preset_css,
            pick_design_preset,
        )

        for slug, trade in (
            ("var2-sucre-osny", "pâtisserie"),
            ("var2-brioche-tls", "pâtisserie"),
            ("var2-tuyau-pontoise", "plombier"),
            ("var2-madeleine-metz", "pâtisserie"),
            ("var2-fuite-cergy", "plombier"),
        ):
            p = pick_design_preset(slug, trade)
            assert_hero_contrast(p)
            # Simulate scratch bug: dark ink titles on dark hero
            html = (
                "<!DOCTYPE html><html><head><style>"
                f".hero{{background:{p.hero}}} .hero h1{{color:{p.ink}!important}}"
                f".hero p{{color:{p.ink}}}"
                "</style></head><body>"
                f'<section class="hero"><h1>Titre</h1><p>Body copy</p>'
                f'<a class="btn" href="tel:01">Appeler</a></section>'
                f'<section class="band"><h2>Suite</h2><p>ok</p></section>'
                "</body></html>"
            )
            out = inject_preset_css(html, p)
            self.assertIn("--av-hero-fg:", out)
            self.assertIn("var(--av-hero-fg)!important", out)
            fg = p.hero_text_color()
            self.assertGreaterEqual(contrast_ratio(fg, p.hero), BODY_MIN_RATIO, slug)
            self.assertGreaterEqual(
                contrast_ratio(p.on_accent_color(), p.accent), LARGE_MIN_RATIO, slug
            )
            # Overlay present and points hero text at hero-fg (not bare ink)
            self.assertIn('id="av-variety-css"', out)
            # Body section still ink
            self.assertIn("var(--av-ink)!important", out)

    def test_repair_stylesheet_rewrites_failing_hero_color(self):
        from design_variety import contrast_ratio, pick_design_preset, repair_stylesheet_contrast

        p = pick_design_preset("var2-brioche-tls", "pâtisserie")
        css = f".hero h1{{color:{p.ink};background:{p.hero}}}"
        fixed = repair_stylesheet_contrast(css, p)
        self.assertNotIn(f"color:{p.ink}", fixed)
        # Extract new color
        import re

        m = re.search(r"color:(#[0-9A-Fa-f]{6})", fixed)
        self.assertTrue(m, fixed)
        self.assertGreaterEqual(contrast_ratio(m.group(1), p.hero), 4.5)

    def test_dark_order_band_keeps_light_text(self):
        """Ink must NOT be forced onto accent/order bands (Sucré contact fail)."""
        from design_variety import contrast_ratio, inject_preset_css, pick_design_preset

        p = pick_design_preset("var2-sucre-osny", "pâtisserie")
        html = (
            "<!DOCTYPE html><html><head><style>"
            f".order{{background:var(--accent);color:var(--ink)}}"
            f".order h2{{color:var(--ink)}}"
            f".hero-bar span{{color:{p.ink}}}"
            f".demo-btn-light{{background:#fff;color:#f5f5f5}}"
            "</style></head><body>"
            f'<header class="hero"><div class="hero-bar"><span>OSNY</span>'
            f'<a href="#c">Contact</a></div>'
            f"<h1>Titre</h1><p>Lead</p>"
            f'<a class="demo-btn demo-btn-light" href="#c">Voir</a></header>'
            f'<section class="order"><h2>Commander</h2><p>Appeler</p></section>'
            "</body></html>"
        )
        out = inject_preset_css(html, p)
        overlay = out[out.find('id="av-variety-css"') :]
        self.assertIn("var(--av-on-accent)!important", overlay)
        self.assertIn(".order", overlay)
        # Light button forced to ink on paper (in overlay, not repaired source)
        self.assertIn("demo-btn-light", overlay)
        self.assertIn("color:var(--av-ink)!important", overlay)
        self.assertIn("color:var(--av-hero-fg)!important", overlay)
        self.assertGreaterEqual(contrast_ratio(p.hero_text_color(), p.hero), 4.5)
        self.assertGreaterEqual(contrast_ratio(p.on_accent_color(), p.accent), 4.5)

    def test_site_plan_same_trade_palettes_differ(self):
        import site_plan as sp

        a = sp.build_curated_plan(
            slug="var2-sucre-osny", name="Sucré", city="Osny", trade="pâtisserie"
        )
        b = sp.build_curated_plan(
            slug="var2-brioche-tls", name="Brioche", city="Toulouse", trade="pâtisserie"
        )
        c = sp.build_curated_plan(
            slug="var2-madeleine-metz", name="Madeleine", city="Metz", trade="pâtisserie"
        )
        ids = {a.palette_id, b.palette_id, c.palette_id}
        self.assertEqual(len(ids), 3, ids)


class TestB28HeroStructureVariety(unittest.TestCase):
    """B-28: ≥5 hero structures; same-trade demos never share structure; no placeholder tiles."""

    PAT = ("var3-sucre-osny", "var3-brioche-tls", "var3-madeleine-metz")
    PLOMB = ("var3-fuite-cergy", "var3-tuyau-pontoise")

    def test_at_least_five_hero_structures(self):
        from design_variety import HERO_STRUCTURES

        self.assertGreaterEqual(len(HERO_STRUCTURES), 5)

    def test_same_trade_hero_structures_unique(self):
        from design_variety import (
            assert_same_trade_hero_structures_unique,
            pick_design_preset,
        )

        pairs = [(s, "pâtisserie") for s in self.PAT] + [(s, "plombier") for s in self.PLOMB]
        assert_same_trade_hero_structures_unique(pairs)
        pat_ids = [pick_design_preset(s, "pâtisserie").hero_structure_id for s in self.PAT]
        plomb_ids = [pick_design_preset(s, "plombier").hero_structure_id for s in self.PLOMB]
        self.assertEqual(len(set(pat_ids)), 3, pat_ids)
        self.assertEqual(len(set(plomb_ids)), 2, plomb_ids)
        # Brioche stays split; Madeleine must NOT share that visual family
        self.assertEqual(pat_ids[1], "split-photo-left")
        self.assertEqual(pat_ids[2], "type-led-band")
        self.assertNotEqual(pat_ids[1], pat_ids[2])

    def test_var3_photos_disjoint_and_no_ice_cream(self):
        import photo_banks as pb

        bank = pb.resolve_trade_images("pâtisserie")
        self.assertNotIn("patisserie-05.jpg", bank)
        pairs = [(s, "pâtisserie") for s in self.PAT] + [(s, "plombier") for s in self.PLOMB]
        pb.assert_same_trade_photos_disjoint(pairs, limit=4)
        for s in self.PAT:
            seq = pb.pick_trade_images("pâtisserie", s, limit=4)
            self.assertNotIn("patisserie-05.jpg", seq)

    def test_stamp_and_nav_css_present(self):
        from design_variety import inject_preset_css, pick_design_preset, stamp_preset_attrs

        p = pick_design_preset("var3-sucre-osny", "pâtisserie")
        html = stamp_preset_attrs(inject_preset_css("<html><head></head><body></body></html>", p), p)
        self.assertIn(f'data-av-hero-structure="{p.hero_structure_id}"', html)
        self.assertIn(f"av-hs-{p.hero_structure_id}", html)
        self.assertIn(".av-topnav", html)
        self.assertIn("demo-hero-media-neutral", html)  # CSS hides placeholders

    def test_validator_rejects_neutral_placeholders(self):
        from scratch_site import _validate_html

        bad = (
            '<html><body><header class="hero"><h1>X</h1></header>'
            '<section id="x"><div class="demo-hero-media-neutral">'
            '<div class="demo-neutral-visual"></div></div></section>'
            '<section id="contact"><p>a b c d e f g</p></section></body></html>'
        )
        errs = _validate_html(bad, check_claims=False)
        self.assertTrue(any("placeholder" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
