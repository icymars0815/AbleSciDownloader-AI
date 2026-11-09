"""The command-line-only edition: no window, the CLI reads the per-run token from data/.token, Edge as browser fallback."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

import kytdl
import runtime_paths
from app import make_server
from kytdl_client import Client, ServiceDown


class TokenFileTests(unittest.TestCase):
    @staticmethod
    def write(folder, text):
        path = Path(folder) / "other.token"
        path.write_text(text, encoding="utf-8")
        return path

    def test_server_writes_the_token_and_the_client_uses_it(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(0, folder, start_monitor=False, auto_screen=False)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = "http://127.0.0.1:%d" % server.server_port
                token = (Path(folder) / ".token").read_text(encoding="utf-8")
                self.assertGreater(len(token), 30)
                page = urlopen(base).read().decode()
                self.assertNotIn(token, page)            # the root page no longer hands out the token
                self.assertIn("kytdl", page)
                client = Client(base, token_path=Path(folder) / ".token")
                client.post("/api/monitor", {"enabled": False})   # accepted with the file token (a wrong token is refused)
                with self.assertRaises(Exception):
                    Client(base, token_path=self.write(folder, "wrong")).post("/api/monitor", {"enabled": False})
            finally:
                server.shutdown()
                server.server_close()
                server.agent.commands.put(("close", None, None))
                server.agent.thread.join(timeout=2)

    def test_missing_token_file_means_service_down(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client("http://127.0.0.1:1", token_path=Path(folder) / ".token")
            with self.assertRaises(ServiceDown):
                client.token()


class BrowserChoiceTests(unittest.TestCase):
    def test_chrome_then_edge_then_playwright_chromium(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = {"PROGRAMFILES": str(root / "pf"), "LOCALAPPDATA": str(root / "la")}
            self.assertIsNone(runtime_paths.installed_channel(env))
            edge = root / "pf" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            edge.parent.mkdir(parents=True)
            edge.write_bytes(b"")
            self.assertEqual("msedge", runtime_paths.installed_channel(env))
            chrome = root / "la" / "Google" / "Chrome" / "Application" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.write_bytes(b"")
            self.assertEqual("chrome", runtime_paths.installed_channel(env))


class CommandTests(unittest.TestCase):
    def run_cli(self, argv, client=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kytdl.main(argv + ["--json"], client)
        return code, json.loads(out.getvalue())

    def test_doctor_reports_each_check(self):
        code, data = self.run_cli(["doctor"], client=object())
        self.assertEqual({"python", "packages", "browser", "data_folder"}, {r["check"] for r in data["checks"]})
        self.assertEqual(0 if data["ok"] else 1, code)

    def test_stop_when_not_running_is_fine(self):
        with tempfile.TemporaryDirectory() as folder:
            code, data = self.run_cli(["stop"], client=Client("http://127.0.0.1:1", token_path=Path(folder) / ".token"))
            self.assertEqual((0, False), (code, data["running"]))

    def test_email_sets_only_the_contact_address(self):
        class FakeClient:
            def __init__(self):
                self.posts = []

            def post(self, path, body=None):
                self.posts.append((path, body))
                return dict(ok=True)

            def state(self):
                return dict(preferences=dict(oa_contact_email="x@example.org"))

        client = FakeClient()
        code, data = self.run_cli(["email", "x@example.org"], client=client)
        self.assertEqual((0, True), (code, data["email_set"]))
        self.assertEqual([("/api/pipeline", {"oa_contact_email": "x@example.org"})], client.posts)
        code, data = self.run_cli(["email"], client=client)
        self.assertEqual((0, True), (code, data["email_set"]))

    def test_before_ai_on_only_login_and_ai_on_are_listed(self):
        class FakeClient:
            def post(self, path, body=None):
                return dict(account=[dict(kind="no_session", reason="未检测到科研通登录")],
                            setup=[dict(kind="raise_mode_ask", reason="x"), dict(kind="oa_contact_email", reason="y")])

        rows = kytdl.needs_user(FakeClient(), dict(settings={}))
        self.assertEqual(["ai_takeover_off", "no_session"], [r["kind"] for r in rows])
        self.assertEqual("kytdl login", rows[1]["next"])

    def test_removed_parts_are_gone(self):
        root = Path(kytdl.__file__).resolve().parent
        for name in ("mcp_server.py", "tray.py", "launcher.py", "app.js", "index.html"):
            self.assertFalse((root / name).exists(), name)
        requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("mcp", "pystray", "pillow"):
            self.assertNotIn(package + ">", requirements)
            self.assertNotIn(package + "=", requirements)


if __name__ == "__main__":
    unittest.main()
