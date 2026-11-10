"""CLI inventory reads and connection reminders use a fake local Zotero only."""
import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ablesci.cli import kytdl
from ablesci.cli.kytdl_client import Client
from ablesci.flow import settings
from ablesci.papers import zotero_bridge as z
from tests import test_zotero as fixtures


class ZoteroCliFlowTests(unittest.TestCase):
    setUp = fixtures.PipelineZoteroTests.setUp

    def cli(self, *args):
        client = Client("http://127.0.0.1:%d" % self.server.server_port, token_path=self.store.root / ".token")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = kytdl.main(["zotero", *args, "--json"], client)
        return code, json.loads(out.getvalue())

    def test_status_is_read_only_and_does_not_claim_save_was_verified(self):
        before = self.store.snapshot()["preferences"]
        code, data = self.cli("status")
        self.assertEqual((0, "ready", False), (code, data["state"], data["save_verified"]))
        self.assertEqual(before, self.store.snapshot()["preferences"])
        self.assertEqual([], fixtures.FakeZotero.saved)

    def test_enabling_previews_both_jobs_then_configures_them_only_after_confirmation(self):
        before = self.store.snapshot()["preferences"]
        code, data = self.cli("on")
        self.assertEqual(1, code)
        self.assertIn("查询", data["rules"])
        self.assertIn("入库", data["rules"])
        self.assertEqual(before, self.store.snapshot()["preferences"])
        code, data = self.cli("on", "--collection", "Fictional collection", "--confirmed")
        self.assertEqual(0, code)
        prefs = z.settings(self.store.snapshot()["preferences"])
        self.assertEqual((True, True, True), tuple(prefs[k] for k in ("enabled", "check", "save")))
        self.assertEqual("Fictional collection", prefs["collection"])
        self.assertEqual([], fixtures.FakeZotero.saved)

    def test_inventory_positive_and_negative_are_exact_doi_matches(self):
        fixtures.FakeZotero.library = [dict(key="FICTION1", data=dict(itemType="journalArticle", DOI=fixtures.DOI, title="Fictional library item"))]
        code, data = self.cli("find", fixtures.DOI)
        self.assertEqual((0, True, "found"), (code, data["found"], data["state"]))
        self.assertFalse(data["local_pdf_verified"])
        self.assertEqual([], fixtures.FakeZotero.saved)
        code, data = self.cli("find", "10.1234/another-fictional")
        self.assertEqual((0, False, "not_found"), (code, data["found"], data["state"]))

    def test_api_unavailable_is_unknown_not_no_inventory(self):
        fixtures.FakeZotero.local_api = False
        code, data = self.cli("find", fixtures.DOI)
        self.assertEqual((1, None, "unavailable"), (code, data["found"], data["state"]))
        self.assertIsNone(data["has_pdf"])

    def test_malformed_api_response_is_not_an_empty_library(self):
        with patch.object(z, "_call", return_value=(200, {}, b'{"unexpected":true}')):
            with self.assertRaises(z.ZoteroUnavailable):
                z.find(fixtures.DOI)
        with patch.object(z, "_call", return_value=(200, {}, b'[true]')):
            with self.assertRaises(z.ZoteroUnavailable):
                z.find(fixtures.DOI)

    def test_attachment_query_failure_is_not_a_missing_pdf(self):
        real = z._call
        def fail_children(path, *args, **kw):
            return (500, {}, b"[]") if "/children" in path else real(path, *args, **kw)
        fixtures.FakeZotero.library = [dict(key="FICTION1", data=dict(itemType="journalArticle", DOI=fixtures.DOI))]
        with patch.object(z, "_call", fail_children):
            code, data = self.cli("find", fixtures.DOI)
        self.assertEqual((1, None, "unavailable"), (code, data["found"], data["state"]))

    def test_connection_notice_is_once_per_outage_and_resolves_after_reconnection(self):
        self.store.update_preferences(**settings.smart_on({}, "default", dict(settings.DEFAULTS, auto_confirm_max=10)),
                                      zotero_enabled=True, zotero_check=True, zotero_save=True)
        self.store.add("Fictional pending paper", fixtures.DOI)
        down = dict(running=False, local_api=False, version="")
        up = dict(running=True, local_api=True, version="10.0.7")
        with patch.object(z, "status", return_value=down):
            self.pipeline.notify_zotero_readiness()
            self.pipeline.notify_zotero_readiness()
        self.assertEqual(1, len(self.server.notifier.inbox()))
        with patch.object(z, "status", return_value=up):
            self.pipeline.notify_zotero_readiness()
        self.assertEqual([], self.server.notifier.inbox())
        with patch.object(z, "status", return_value=down):
            self.pipeline.notify_zotero_readiness()
        self.assertEqual(1, len(self.server.notifier.inbox()))

    def test_failed_query_is_recorded_as_unavailable_then_can_refresh(self):
        self.store.update_preferences(zotero_enabled=True)
        iid = self.store.add("Fictional paper", fixtures.DOI)
        fixtures.FakeZotero.local_api = False
        result = self.pipeline.zotero_check(iid)
        self.assertEqual("unavailable", result["status"])
        self.assertNotIn("found", self.store.get(iid)["zotero_check"])
        self.store.update(iid, zotero_check=dict(result, time=(datetime.now().astimezone() - timedelta(minutes=2)).isoformat()))
        fixtures.FakeZotero.local_api = True
        result = self.pipeline.zotero_check(iid)
        self.assertEqual(("checked", False), (result["status"], result["found"]))


if __name__ == "__main__":
    unittest.main()
