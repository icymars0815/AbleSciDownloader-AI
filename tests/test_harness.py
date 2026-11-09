"""Harness integration: MCP tools over a real stdio session, the /api/mcp/* endpoints, and the kytdl CLI. Temp data roots only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch


from ablesci.flow import intake
from ablesci.cli import kytdl
from ablesci.flow.core import seed_demo
from ablesci.cli.kytdl_client import Client

EXPECTED_TOOLS = {
    "workbench_status", "list_accounts", "switch_account", "list_papers", "preview_references", "import_references", "query_public_metadata", "search_literature",
    "verify_paper", "website_action", "reject_paper", "stop_website_task", "unparsed_segments", "batch_status", "review_table", "confirm_batch",
    "download_paper", "download_progress", "wait_for", "get_result", "render_pages", "submit_assist_result", "error_report", "monitor_status",
    "events", "download_settings_get", "oa_lookup", "oa_fetch", "open_panel", "continue_authorized_request", "resolve_oa_review", "adopt_paper", "requeue_request", "needs_attention", "set_zotero_integration",
    "report_pdf_identity", "inbox"}
from tests.helpers import ROOT, start, stop


class WarningTests(unittest.TestCase):
    def test_structured_import_rejects_invalid_batch_without_partial_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                client = Client("http://127.0.0.1:%d" % server.server_port, token_path=server.store.root / ".token")
                from ablesci.cli.kytdl_client import ApiError
                for bad in ({"title": None, "doi": None}, {"title": "x", "authors": "not a list"}, "not an object"):
                    with self.assertRaises(ApiError):
                        client.post("/api/import", {"records": [{"title": "valid"}, bad]})
                    self.assertEqual([], server.store.snapshot()["items"])
            finally:
                stop(server)
    def test_resume_pre_submit_only(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder, permission=True)
            try:
                iid = server.store.add("Test", "10.1234/test", account_id="default")
                server.store.update(iid, request_confirmed=True, status="attention")
                result = server.mcp_api.do_auto_continue({"id": iid, "retry_unsubmitted": True})
                self.assertTrue(result["queued"])
                server.store.update(iid, status="attention", publish_action={"status": "uncertain"})
                with self.assertRaises(ValueError):
                    server.mcp_api.do_auto_continue({"id": iid, "retry_unsubmitted": True})
            finally:
                stop(server)


# ---------- CLI ----------
class FakeService(BaseHTTPRequestHandler):
    state = {"items": [{"id": "p1", "stage": "fulfilled", "pdf": ""}], "events": [], "batches": [], "monitor": {"enabled": False}, "about": {"version": "x"},
             "preferences": {}, "pipeline": {}, "active_account_id": "default"}
    posts = []

    def log_message(self, *_):
        pass

    def send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, b'<script>const TOKEN="fake-token";let state={}</script>', "text/html")
        if self.path == "/api/state":
            return self.send(200, self.state)
        self.send(404, {"error": "nope"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeService.posts.append((self.path, body, self.headers.get("X-Demo-Token")))
        if self.path == "/api/mcp/result":
            return self.send(403, {"error": "当前账户未开启 MCP 科研通操作权限", "reason": "permission_denied"})
        if self.path == "/api/import":
            return self.send(200, {"ok": True, "batch_id": "bb1", "count": 1, "item_ids": ["p1"], "skipped_duplicates": 0})
        if self.path == "/api/monitor":
            return self.send(200, {"ok": True})
        self.send(400, {"error": "business problem"})


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeService.posts = []
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.fake.server_port
        self.token_dir = tempfile.TemporaryDirectory()
        token = Path(self.token_dir.name) / ".token"
        token.write_text("fake-token", encoding="utf-8")
        self.client = Client(self.base, token_path=token)

    def tearDown(self):
        self.token_dir.cleanup()
        self.fake.shutdown()
        self.fake.server_close()

    def run_cli(self, *argv, client=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kytdl.main(list(argv), client or self.client)
        return code, out.getvalue(), err.getvalue()

    def test_status_ok_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(0, code)
        data = json.loads(out)
        self.assertTrue(data["ok"] and data["running"])
        self.assertEqual(0, data["exit"])

    def test_service_not_running_is_exit_2(self):
        dead = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        port = dead.server_port
        dead.server_close()
        code, out, _ = self.run_cli("status", "--json", client=Client("http://127.0.0.1:%d" % port))
        self.assertEqual(2, code)
        self.assertEqual("service_not_running", json.loads(out)["reason"])
        code, _, err = self.run_cli("events", client=Client("http://127.0.0.1:%d" % port))
        self.assertEqual(2, code)
        self.assertIn("启动", err)

    def test_permission_denied_is_exit_3_and_business_error_is_1(self):
        code, out, _ = self.run_cli("result", "p1", "--json")
        self.assertEqual(3, code)
        self.assertEqual("permission_denied", json.loads(out)["reason"])
        code, out, _ = self.run_cli("oa", "10.1/x", "--json")   # fake answers 400
        self.assertEqual(1, code)
        code, out, _ = self.run_cli("batch", "missing", "--json")
        self.assertEqual(1, code)

    def test_import_file_and_token_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "refs.txt"
            path.write_text("10.1234/x\tFictional\n", "utf-8")
            code, out, _ = self.run_cli("import", str(path), "--json")
            self.assertEqual(0, code)
            self.assertEqual("bb1", json.loads(out)["batch_id"])
            self.assertEqual("fake-token", FakeService.posts[-1][2])
            code, out, _ = self.run_cli("import", str(Path(folder) / "none.txt"), "--json")
            self.assertEqual(1, code)

    def test_wait_exit_codes(self):
        code, out, _ = self.run_cli("wait", "p1", "--until", "fulfilled", "--timeout", "5", "--json")
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["reached"])
        code, out, _ = self.run_cli("wait", "p1", "--until", "done", "--timeout", "1", "--json")
        self.assertEqual(1, code)
        self.assertEqual("timeout", json.loads(out)["reason"])
        code, out, _ = self.run_cli("wait", "zzz", "--until", "done", "--json")
        self.assertEqual(1, code)

    def test_monitor_toggle_posts_only_monitor_setting(self):
        code, _, _ = self.run_cli("monitor", "on", "--json")
        self.assertEqual(0, code)
        self.assertEqual(("/api/monitor", {"enabled": True}), FakeService.posts[-1][:2])

    def test_no_spending_commands_exist(self):
        # `confirm` exists since 2026-11-09 (AI takeover) but only previews unless --confirmed: see test_ai_takeover.CliTests
        for word in ("publish", "adopt", "reject", "download", "highspeed"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    kytdl.main([word])

    def test_cli_script_runs_as_subprocess(self):
        env = dict(os.environ, ABLESCI_LOCAL_URL=self.base)
        done = subprocess.run([sys.executable, "-m", "ablesci", "status", "--json"], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertTrue(json.loads(done.stdout)["running"])
        env["ABLESCI_LOCAL_URL"] = "http://127.0.0.1:1"
        done = subprocess.run([sys.executable, "-m", "ablesci", "status"], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(2, done.returncode)


class CliAgainstRealServer(unittest.TestCase):
    def test_import_batch_events_result(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            try:
                client = Client("http://127.0.0.1:%d" % server.server_port, token_path=server.store.root / ".token")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    with patch("sys.stdin", io.StringIO("10.1234/cli.1\tFictional CLI paper\n")):
                        self.assertEqual(0, kytdl.main(["import", "-", "--json"], client))
                batch_id = json.loads(out.getvalue())["batch_id"]
                for argv in (["batch", batch_id], ["batch"], ["events", "-n", "5"], ["monitor", "status"], ["status"]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0, kytdl.main(argv + ["--json"], client), argv)
                item_id = server.store.snapshot()["items"][0]["id"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(1, kytdl.main(["result", item_id, "--json"], client))   # no PDF yet: business result, not an error crash
            finally:
                stop(server)


if __name__ == "__main__":
    unittest.main()
