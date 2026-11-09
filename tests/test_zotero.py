"""Optional Zotero integration against a fake Zotero 7.0 (connector + read-only local API). Temp data only."""
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from ablesci.flow import intake
from ablesci.papers import zotero_bridge
from tests import helpers as hp

DOI = "10.1234/zot.1"
PDF = b"%PDF-1.4 fictional zotero test"


class FakeZotero(BaseHTTPRequestHandler):
    """Mimics the parts of Zotero that zotero_bridge uses: version 10 (byte upload) by default, version 7 with upload=False."""
    upload = True         # Zotero 10: ping advertises supportsAttachmentUpload, saveItems ignores attachment URLs
    attach_code = 201
    library = []          # local API rows
    children = {}
    local_api = True
    saved = []            # saveItems bodies
    moved = []            # updateSession calls
    fetched = []          # attachment bytes Zotero downloaded

    def log_message(self, *args):
        pass

    def reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Zotero-Version", "10.0.7" if self.upload else "7.0.15")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/connector/ping":
            return self.reply(200, b"<html><body>Zotero is running</body></html>", "text/html")
        if not self.local_api:
            return self.reply(403, b"Local API is not enabled", "text/plain")
        if url.path == "/api/":
            return self.reply(200, b"Nothing to see here.", "text/plain")
        if url.path == "/api/users/0/items":
            q = parse_qs(url.query)
            assert q["qmode"] == ["everything"]
            return self.reply(200, self.library)
        if url.path.startswith("/api/users/0/items/") and url.path.endswith("/children"):
            return self.reply(200, self.children.get(url.path.split("/")[5], []))
        return self.reply(404, b"")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        url = urlparse(self.path)
        if url.path == "/connector/ping":
            return self.reply(200, {"prefs": {"supportsAttachmentUpload": True}} if self.upload else {"prefs": {}})
        if url.path == "/connector/saveAttachment" and self.upload:
            meta = json.loads(self.headers["X-Metadata"])
            session = self.sessions.get(meta["sessionID"])
            if session is None or meta["parentItemID"] not in session:
                return self.reply(400, {"error": "SESSION_NOT_FOUND"})
            assert self.headers["Content-Type"] == "application/pdf"
            self.fetched.append(raw)
            return self.reply(self.attach_code, b"")
        body = json.loads(raw)
        if url.path == "/connector/getSelectedCollection":
            return self.reply(200, {"name": "Selected", "targets": [{"id": "L1", "name": "我的文库"}, {"id": "C3", "name": "KYTDL 测试"}]})
        if url.path == "/connector/updateSession":
            self.moved.append((body["sessionID"], body["target"], body["tags"]))
            return self.reply(200 if body["sessionID"] in self.sessions else 400, {})
        if url.path == "/connector/saveItems":
            assert self.headers["Host"].startswith("127.0.0.1")
            self.saved.append(body)
            self.sessions[body["sessionID"]] = {i["id"] for i in body["items"]}
            if not self.upload:
                for attachment in body["items"][0]["attachments"]:   # Zotero 7.0 downloads attachments itself
                    with urlopen(attachment["url"], timeout=5) as r:
                        self.fetched.append(r.read())
            return self.reply(201, b"")
        return self.reply(404, b"")


def start_zotero():
    FakeZotero.library, FakeZotero.children, FakeZotero.saved, FakeZotero.fetched, FakeZotero.local_api = [], {}, [], [], True
    FakeZotero.upload, FakeZotero.attach_code, FakeZotero.sessions, FakeZotero.moved = True, 201, {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeZotero)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.zot = start_zotero()
        self.addCleanup(self.zot.server_close)
        self.addCleanup(self.zot.shutdown)
        patcher = patch.object(zotero_bridge, "ZOTERO", "http://127.0.0.1:%d" % self.zot.server_port)
        patcher.start()
        self.addCleanup(patcher.stop)
        zotero_bridge._status_cache.clear()

    def test_off_by_default(self):
        self.assertEqual(dict(enabled=False, check=False, save=False, prompted=False, collection=""), zotero_bridge.settings({}))
        self.assertTrue(zotero_bridge.settings(dict(zotero_enabled=True))["save"])

    def test_status_and_local_api_off(self):
        self.assertEqual((True, True, "10.0.7"), tuple(zotero_bridge.status(fresh=True)[k] for k in ("running", "local_api", "version")))
        FakeZotero.local_api = False
        self.assertFalse(zotero_bridge.status(fresh=True)["local_api"])
        with self.assertRaises(zotero_bridge.ZoteroUnavailable):
            zotero_bridge.find(DOI)

    def test_not_running(self):
        with patch.object(zotero_bridge, "ZOTERO", "http://127.0.0.1:9"):
            self.assertFalse(zotero_bridge.status(fresh=True)["running"])
            with self.assertRaises(zotero_bridge.ZoteroUnavailable):
                zotero_bridge.find(DOI)

    def test_find_needs_exact_doi_and_reads_pdf_children(self):
        FakeZotero.library = [
            {"key": "NOISE1", "data": {"itemType": "journalArticle", "title": "Mentions 10.1234/zot.1 in abstract", "DOI": "10.1234/zot.10"}},
            {"key": "NOTE01", "data": {"itemType": "note", "DOI": DOI}},
            {"key": "HIT001", "data": {"itemType": "journalArticle", "title": "The paper", "DOI": "https://doi.org/10.1234/ZOT.1"}}]
        FakeZotero.children = {"HIT001": [{"data": {"itemType": "attachment", "contentType": "application/pdf"}}]}
        self.assertEqual(dict(found=True, key="HIT001", title="The paper", has_pdf=True), zotero_bridge.find(DOI))
        FakeZotero.library = [{"key": "BOOK01", "data": {"itemType": "bookSection", "title": "Chapter", "extra": "DOI: 10.1234/zot.1"}}]
        self.assertEqual((True, False), (zotero_bridge.find(DOI)["found"], zotero_bridge.find(DOI)["has_pdf"]))
        FakeZotero.library = []
        self.assertFalse(zotero_bridge.find(DOI)["found"])

    def test_file_offers_are_single_use_and_expire(self):
        clock = [0.0]
        offers = zotero_bridge.FileOffers(ttl=10, clock=lambda: clock[0])
        token = offers.offer("p")
        self.assertEqual("p", offers.take(token))
        self.assertIsNone(offers.take(token))
        late = offers.offer("q")
        clock[0] = 11
        self.assertIsNone(offers.take(late))

    def test_item_json(self):
        data = zotero_bridge.zotero_item(dict(id="x", doi=DOI, title="T", metadata=dict(title="Real title", authors=["Ada Lovelace", "Curie, Marie", "Plato"], year=2020)))
        self.assertEqual(("journalArticle", "Real title", DOI, "2020", [{"tag": "KYTDL"}]), (data["itemType"], data["title"], data["DOI"], data["date"], data["tags"]))
        self.assertEqual([dict(firstName="Ada", lastName="Lovelace", creatorType="author"), dict(firstName="Marie", lastName="Curie", creatorType="author"),
                          dict(lastName="Plato", creatorType="author", fieldMode=1)], data["creators"])


class PipelineZoteroTests(unittest.TestCase):
    def setUp(self):
        self.zot = start_zotero()
        self.addCleanup(self.zot.server_close)
        self.addCleanup(self.zot.shutdown)
        patcher = patch.object(zotero_bridge, "ZOTERO", "http://127.0.0.1:%d" % self.zot.server_port)
        patcher.start()
        self.addCleanup(patcher.stop)
        zotero_bridge._status_cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = hp.start(self.temp.name, permission=True)
        self.addCleanup(hp.stop, self.server)
        self.store, self.pipeline = self.server.store, self.server.pipeline

    def verified(self):
        pdf = self.store.root / "downloads" / "z.pdf"
        pdf.parent.mkdir(exist_ok=True)
        pdf.write_bytes(PDF)
        return self.store.add("Zotero test paper", DOI, account_id="default", pdf="downloads/z.pdf", status="checked", done_via="oa",
                              verification=dict(status="identity_match", sha256=hashlib.sha256(PDF).hexdigest()),
                              metadata=dict(title="Zotero test paper", authors=["Ada Lovelace"], year=2024))

    def run_step(self):
        started = self.pipeline.zotero_step()
        if started:
            self.pipeline.zotero_thread.join(10)
        return started

    def test_nothing_happens_while_off(self):
        self.verified()
        self.assertIsNone(self.run_step())
        self.assertEqual([], FakeZotero.saved)

    def test_zotero10_uploads_pdf_bytes_to_the_created_item(self):
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True)
        self.assertEqual(item_id, self.run_step())
        sent = FakeZotero.saved[0]["items"][0]
        self.assertEqual((DOI, "Zotero test paper", [{"tag": "KYTDL"}], []), (sent["DOI"], sent["title"], sent["tags"], sent["attachments"]))
        self.assertEqual([PDF], FakeZotero.fetched)
        self.assertEqual(("sent", "upload"), (self.store.get(item_id)["zotero"]["status"], self.store.get(item_id)["zotero"]["mode"]))
        self.assertIsNone(self.run_step())

    def test_moves_into_named_collection_and_falls_back_when_missing(self):
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True, zotero_collection="KYTDL 测试")
        self.run_step()
        self.assertEqual("C3", FakeZotero.moved[0][1])
        self.assertEqual("KYTDL 测试", self.store.get(item_id)["zotero"]["collection"])
        other = self.store.add("Second", "10.1234/zot.9", account_id="default", pdf="downloads/z.pdf", status="checked",
                               verification=dict(status="identity_match"), metadata=dict(title="Second", authors=[]))
        self.store.update_preferences(zotero_collection="不存在的分类")
        self.run_step()
        self.assertEqual(1, len(FakeZotero.moved))                         # unknown name: left where Zotero put it
        self.assertEqual(("sent", ""), (self.store.get(other)["zotero"]["status"], self.store.get(other)["zotero"]["collection"]))

    def test_failed_upload_after_item_created_is_not_retried(self):
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True)
        FakeZotero.attach_code = 500
        self.run_step()
        self.assertEqual("partial", self.store.get(item_id)["zotero"]["status"])
        self.assertIsNone(self.run_step())                                 # a retry would duplicate the item
        self.assertEqual(1, len(FakeZotero.saved))

    def test_zotero7_downloads_through_single_use_url(self):
        FakeZotero.upload = False
        zotero_bridge._status_cache.clear()
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True)
        self.assertEqual(item_id, self.run_step())
        self.assertEqual([PDF], FakeZotero.fetched)                       # fetched through the single-use loopback URL
        sent = FakeZotero.saved[0]["items"][0]
        self.assertEqual(("sent", "url"), (self.store.get(item_id)["zotero"]["status"], self.store.get(item_id)["zotero"]["mode"]))
        with self.assertRaises(Exception):
            urlopen(sent["attachments"][0]["url"], timeout=5)               # the link is spent
        self.assertIsNone(self.run_step())                                 # never saved twice
        self.assertEqual(1, len(FakeZotero.saved))

    def test_existing_item_with_pdf_is_not_duplicated(self):
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True)
        FakeZotero.library = [{"key": "HAVE01", "data": {"itemType": "journalArticle", "title": "x", "DOI": DOI}}]
        FakeZotero.children = {"HAVE01": [{"data": {"contentType": "application/pdf"}}]}
        self.run_step()
        self.assertEqual([], FakeZotero.saved)
        self.assertEqual(("exists", "HAVE01"), (self.store.get(item_id)["zotero"]["status"], self.store.get(item_id)["zotero"]["key"]))

    def test_unverified_files_are_never_saved(self):
        item_id = self.verified()
        self.store.update(item_id, verification=dict(status="title_mismatch"))
        self.store.update_preferences(zotero_enabled=True)
        self.assertIsNone(self.run_step())
        self.assertEqual([], FakeZotero.saved)

    def test_zotero_closed_waits_without_counting_attempts(self):
        item_id = self.verified()
        self.store.update_preferences(zotero_enabled=True)
        with patch.object(zotero_bridge, "ZOTERO", "http://127.0.0.1:9"):   # nothing listens there: Zotero closed
            zotero_bridge._status_cache.clear()
            self.assertIsNone(self.run_step())
            self.assertIsNone(self.store.get(item_id).get("zotero"))   # no attempt counted
        zotero_bridge._status_cache.clear()
        self.assertEqual(item_id, self.run_step())                     # Zotero opened again: saved then
        self.assertEqual("sent", self.store.get(item_id)["zotero"]["status"])

    def test_check_marks_the_confirm_table(self):
        self.store.update_preferences(zotero_enabled=True)
        FakeZotero.library = [{"key": "HAVE01", "data": {"itemType": "journalArticle", "title": "x", "DOI": DOI}}]
        FakeZotero.children = {"HAVE01": [{"data": {"contentType": "application/pdf"}}]}
        item_id = self.store.add("Queued", DOI, account_id="default", request_confirmed=False, oa_check=dict(status="not_oa"))
        self.pipeline.zotero_check(item_id)
        row = intake.classify(self.store.get(item_id))
        self.assertEqual(("in_zotero", False, True), (row["code"], row["checked"], row["selectable"]))
        FakeZotero.children = {}
        other = self.store.add("Queued 2", "10.1234/zot.2", account_id="default", request_confirmed=False, oa_check=dict(status="not_oa"))
        FakeZotero.library = [{"key": "BARE01", "data": {"itemType": "journalArticle", "title": "x", "DOI": "10.1234/zot.2"}}]
        self.pipeline.zotero_check(other)
        self.assertEqual(("in_zotero_no_pdf", True), (lambda r: (r["code"], r["checked"]))(intake.classify(self.store.get(other))))

    def test_opt_in_is_asked_once_and_mcp_needs_confirmation(self):
        feed = self.server.mcp_api.do_needs_attention({})
        self.assertIn("zotero_available", {h["kind"] for h in feed["setup"]})
        with self.assertRaises(ValueError):
            self.server.mcp_api.do_zotero_settings({"enabled": True})
        out = self.server.mcp_api.do_zotero_settings({"enabled": False, "confirmed": True})
        self.assertEqual((False, True), (out["zotero"]["enabled"], out["zotero"]["prompted"]))
        self.assertNotIn("zotero_available", {h["kind"] for h in self.server.mcp_api.do_needs_attention({})["setup"]})

    def test_preferences_endpoint_validates(self):
        with self.assertRaises(ValueError):
            zotero_bridge.validate({"enabled": "yes"})
        with self.assertRaises(ValueError):
            zotero_bridge.validate({"other": True})
        self.assertEqual(dict(zotero_prompted=True, zotero_enabled=True), zotero_bridge.validate({"enabled": True}))
        self.assertEqual(dict(zotero_prompted=True, zotero_collection="KYTDL 测试"), zotero_bridge.validate({"collection": "  KYTDL   测试 "}))
        with self.assertRaises(ValueError):
            zotero_bridge.validate({"collection": 3})


if __name__ == "__main__":
    unittest.main()
