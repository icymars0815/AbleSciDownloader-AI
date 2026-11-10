"""Fakes, fixtures and set-up shared by several test modules (refactor stage 4). Not a test module itself."""
import contextlib
import hashlib
import json
import queue
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


from ablesci.flow import intake
from ablesci.website.accounts import Accounts
from ablesci.server.app import make_server
from ablesci.website.browser_agent import BASE, BrowserAgent
from ablesci.flow.core import Store, now
from ablesci.website.download_policy import RULES_VERSION, highspeed_allowed  # noqa: F401  (RULES_VERSION is re-exported)
from ablesci.flow.pipeline import FULFILLED, PIPELINE_RULES_VERSION, WAITING, stage_of


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
URL = BASE + "/assist/detail?id=ABC123"
FILE = BASE + "/assist/download?id=FILE1"
AUTO = dict(pipeline_auto_publish=True, pipeline_auto_adopt=True, pipeline_rules_version=PIPELINE_RULES_VERSION)


def item(name="a", **fields):
    base = dict(id=name, title="Title " + name, doi="10.1234/" + name, source="website", owned=True, status="ready",
                created="2026-10-19T10:00:0" + str(len(name) % 10), account_id="default", assist_url=URL + name, website_status="")
    base.update(fields)
    return base


def queued(name="q", **fields):
    fields.setdefault("request_confirmed", True)
    return item(name, source="import", owned=False, assist_url="", **fields)


def waiting(name="w", **fields):
    return item(name, website_status=WAITING[0], **fields)


def fulfilled(name="f", **fields):
    return item(name, website_status=FULFILLED[0], **fields)


MATCH = dict(status="identity_match", sha256="x", primary_dois=["10.1234/d"], title_score=100,
             bibliography=dict(status="match"))


def deciding(name="d", **fields):
    base = dict(website_status=FULFILLED[0], status="downloaded", pdf="downloads/" + name + ".pdf",
                download_file_url=FILE, verification=dict(MATCH))
    base.update(fields)
    return item(name, **base)


class AgentHarness:
    """Shared set-up of test_pipeline.ArchiveAndActionTests (no tests of its own)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.page = MagicMock()
        self.page.url = URL
        self.agent.context, self.agent.page = MagicMock(), self.page
        self.agent.goto = MagicMock()   # no navigation; the fake page is already "there"
        self.page.is_closed.return_value = False
        self.card = MagicMock()
        link = MagicMock()
        link.count.return_value = 1
        link.last = link
        self.page.get_by_role.return_value.count.return_value = 1
        link.locator.return_value = self.card
        self.card.count.return_value = 1
        self.card.inner_text.return_value = "应助文件 FILE1"
        self.controls = self.card.get_by_role.return_value.or_.return_value
        self.controls.count.return_value = 1
        dialog = MagicMock()
        dialog.count.return_value = 0
        body = MagicMock()
        body.inner_text.return_value = "当前拥有 640 积分"
        table = MagicMock()
        table.all.return_value = []
        def locator(selector, *a, **k):
            return (link if selector.startswith("a[href") else dialog if "dialog" in selector else table if selector == "table tr" else body)
        self.page.locator.side_effect = locator
        self.link = link
        self.body = body

    def downloaded(self, **fields):
        folder = self.store.root / "downloads"
        folder.mkdir(exist_ok=True)
        data = b"%PDF-1.4 fixture"
        (folder / "one.pdf").write_bytes(data)
        item_id = self.store.add("Paper", "10.1234/ok", source="website", account_id="default", owned=True, assist_url=URL,
                                 website_status=FULFILLED[0], pdf="downloads/one.pdf", download_file_url=FILE,
                                 verification=dict(status="conflict", primary_dois=["10.9/other"], title_score=10,
                                                   sha256=hashlib.sha256(data).hexdigest()), **fields)
        return item_id


RULES = "用户积分＜500，高速下载扣 2 积分，同一文件不重复扣分；用户积分≥500，免积分。"


class DownloadHarness:
    """Shared set-up of test_download_settings.AgentFlowTests (no tests of its own)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.store.update_preferences(download_probe_enabled=False)    # line probing has its own tests (test_publish_download.py)
        self.item = self.store.add("A paper", "", "website", account_id="default", assist_url=BASE + "/assist/detail?id=ABC", owned=True,
                                   website_status="待确认")
        self.body = RULES
        self.clicked = []
        self.agent.goto = MagicMock()
        self.agent.context = MagicMock()
        self.agent.page = self.make_page()
        self.agent.line_control = MagicMock(return_value=MagicMock(**{"get_attribute.return_value": "false", "is_checked.return_value": False}))
        self.agent.save_download = MagicMock()
        self.waits = []

        def wait(item_id, label, pending, settings=None, eta=False):
            self.waits.append((label, eta))
            outcome = self.outcomes.pop(0)
            if outcome == "complete":
                pending.append(MagicMock())
            return outcome
        self.agent.wait_transfer = wait
        self.outcomes = []

    def make_page(self):
        page = MagicMock()
        page.url = BASE + "/assist/download?id=FILE1"

        def table_locator(selector, **_):
            m = MagicMock()
            if selector.startswith("a["):
                m.count.return_value = 1
                m.first.get_attribute.return_value = "/assist/download?id=FILE1"
            else:
                m.filter.return_value.count.return_value = 0
            return m

        def locator(selector, **_):
            m = MagicMock()
            if selector == "body":
                m.inner_text.side_effect = lambda *a, **k: self.body
            elif selector == "table":
                m.first.locator.side_effect = table_locator
                m.first.inner_text.return_value = "table"
            elif "file_highspeed" in selector:
                m.count.return_value = 1
                m.get_attribute.return_value = "0"
            elif selector == "#download-highspeed-direct":
                m.count.return_value = 1
                m.click.side_effect = lambda: self.clicked.append("highspeed")
            return m
        page.locator.side_effect = locator
        return page

    def run_download(self):
        with patch("ablesci.website.agent_download.highspeed_allowed", wraps=highspeed_allowed) as spy:
            try:
                self.agent.download(self.item)
                error = None
            except ValueError as exc:
                error = str(exc)
        return spy, error

    def prefs(self, **fields):
        self.store.update_preferences(**fields)


DETAIL = BASE + "/assist/detail?id=NEW123"
TITLE = "Citrus flavonoids and gut microbiota in adults"
DOI = "10.1234/ok"


class El:
    def __init__(self, text="", value="", buttons=None, on_click=None):
        self.text, self.value, self.visible, self.buttons, self.on_click, self.clicks = text, value, True, buttons or [], on_click, 0

    def inner_text(self, **_):
        return self.text

    def is_visible(self):
        return self.visible

    def fill(self, value):
        self.value = value

    def input_value(self, **_):
        return self.value

    def get_attribute(self, name):
        return self.value

    def click(self, **_):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def locator(self, selector, has_text=None):
        if "textarea" in selector:           # fake layers carry no form fields
            return Loc([])
        found = [b for b in self.buttons if has_text is None or (has_text.search(b.text) if hasattr(has_text, "search") else has_text in b.text)]
        return Loc(found)


class Loc:
    def __init__(self, items):
        self.items = items
        self.first = items[0] if items else El()
        self.last = items[-1] if items else El()

    def count(self):
        return len(self.items)

    def nth(self, n):
        return self.items[n]

    def filter(self, has_text=None):
        return Loc([i for i in self.items if has_text is None or (has_text.search(i.text) if hasattr(has_text, "search") else has_text in i.text)])

    def __getattr__(self, name):
        return getattr(self.first, name)


class FakePage:
    def __init__(self, body="您当前有 100 积分"):
        self.url = BASE + "/assist/create"
        self.el = {s: El() for s in ("#onekey", "#Assist-doi", "#Assist-title", "#Assist-url", "#Assist-note", "#Assist-point", "#Assist-close_at", "input")}
        self.el["body"] = El(body)
        self.el["#Assist-close_at"].value = "5天"
        self.layers = []
        self.response = None            # dict served for check-before-assist-create
        self.nav = None                 # url reached after a successful submit
        self.search = El(on_click=lambda: None)
        self.submit = El()
        self.el["button.onekey-search"] = self.search
        self.el["#form-submit-btn"] = self.submit
        self.el['input[name="Assist[type]"]:checked'] = El(value="1")
        self.el["#point-low"] = El("10")
        self.shots = []

    def locator(self, selector, has_text=None):
        if selector == ".layui-layer":
            return Loc(self.layers)
        return Loc([self.el[selector]])

    def screenshot(self, path, mask=None):
        with open(path, "wb") as handle:
            handle.write(b"png")
        self.shots.append(path)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_url(self, predicate, timeout=0):
        if not self.nav:
            raise TimeoutError("no navigation")
        self.url = self.nav

    @contextlib.contextmanager
    def expect_response(self, predicate, timeout=0):
        holder = MagicMock()
        yield holder
        if self.response is None:
            raise TimeoutError("no response")
        holder.value.json.return_value = self.response
        if self.nav:
            self.url = self.nav          # the real site navigates (or offers 查看求助详情) after a successful submit


def doi_lookup_text(title=TITLE, doi=DOI):
    """Exact DOI lookup layer as seen on the real page (2026-10-20): no 相似度/类型/最低悬赏, three buttons."""
    return ("标题 %s\nDOI %s\n期刊 Food Chemistry\n作者 A; B; et al\n出版日期 2025-01-20\n网址 https://doi.org/%s\n"
            "信息正确，直接发布 信息正确，还需修改 信息有误，手动填写" % (title, doi, doi))


def lookup_text(title=TITLE, doi=DOI, sim=95, kind="期刊论文", minimum=10):
    return "标题：%s\n相似度：%s%%\nDOI：%s\n期刊：J Food Sci\n类型：%s\n最低悬赏：%s" % (title, sim, doi, kind, minimum)


class PublishBase(unittest.TestCase):
    prefs = {}
    mock_reference_gate = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = BrowserAgent(self.store, Accounts(self.temp.name))
        self.addCleanup(self.agent.thread.join, 0.1)
        self.addCleanup(self.agent.commands.put, ("close", None, None))
        self.agent.throttled = lambda: contextlib.nullcontext()
        self.agent.context = MagicMock()
        self.agent.goto = MagicMock()
        self.page = FakePage()
        self.agent.page = self.page
        self.agent.notifier = MagicMock()
        self.store.update_account(account_id="default", points=100)
        self.store.update_preferences(**self.prefs)
        if self.mock_reference_gate:
            # Older site-form unit tests assume a valid independent reference. Gate tests opt out and mock only APIs.
            self.agent.require_verified_reference = MagicMock(side_effect=lambda item, check: self.agent.reference_title(item, item["doi"], ""))

    def item(self, title=TITLE, doi=DOI, **fields):
        fields.setdefault("metadata_ref", dict(title=TITLE))
        return self.store.add(title, doi, account_id="default", **fields)

    def script(self, text=None, response=None, nav=DETAIL, form_title=TITLE, form_doi=DOI, confirm="确定"):
        page = self.page
        text = text or lookup_text()

        def fill_form():
            page.el["#Assist-doi"].value, page.el["#Assist-title"].value = form_doi, form_title
        button = El(confirm, on_click=fill_form)
        layer = El(text, buttons=[button, El("取消")])
        page.search.on_click = lambda: page.layers.append(layer)
        page.response, page.nav = response, nav
        return layer

    def run_publish(self, item_id):
        try:
            self.agent.publish(item_id)
        except ValueError as exc:
            return str(exc)
        return None

    def assertStopped(self, item_id, kind, clicked=0):
        after = self.store.get(item_id)
        self.assertEqual("attention", after["status"])
        self.assertEqual(kind, after["publish_check"]["kind"])
        self.assertEqual(clicked, self.page.submit.clicks)
        self.assertEqual("attention", stage_of(after))
        self.assertTrue(after["publish_blocked"])
        return after


class FakeDownload:
    def __init__(self, name, data):
        self.suggested_filename, self.data = name, data

    def save_as(self, path):
        Path(path).write_bytes(self.data)

    def cancel(self):
        pass


class FakeAgent:
    """Stands in for the serial browser agent inside Pipeline: records what the scheduler would run."""
    busy = False

    def __init__(self):
        self.commands, self.enqueued = queue.Queue(), []

    def enqueue(self, action, item_id=None, credentials=None):
        self.enqueued.append((action, item_id))


ROOT = Path(__file__).resolve().parents[1]


def start(folder, permission=False, session=True):
    server = make_server(0, folder, start_monitor=False, auto_screen=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if permission:
        server.store.update_preferences(mcp_website_actions=True, mcp_actions_account="default")
    if session:
        server.store.update_account("default", points=500)
    return server


def stop(server):
    server.shutdown()
    server.server_close()
    server.agent.commands.put(("close", None, None))
    server.agent.thread.join(timeout=2)


def add_batch(server, count=3, batch="B1"):
    store = server.store
    ids = [store.add("Fictional title %s %d" % (batch, n), "10.1234/b%s%d" % (batch.lower(), n), account_id="default", request_confirmed=False, batch_id=batch) for n in range(count)]
    intake.create_batch(store, batch, "default", ids)
    for item_id in ids:
        store.update(item_id, oa_check=dict(status="not_oa"))
    with store.lock:
        intake.find_batch(store, batch)["screening_done_at"] = now()
        store.save()
    return ids


T = 1_800_000_000.0   # fixed "now" (epoch seconds)


class FakeAccounts:
    active_id = "default"

    def has_cookie_file(self):
        return True


class MonitorAgent:
    def __init__(self):
        self.busy = False
        self.commands = queue.Queue()
        self.calls = []
        self.context = None
        self.interactive = False
        self.now = 1000.0
        self.last_activity = 1000.0
        self.on_done = None

    def clock(self):
        return self.now

    def has_browser(self):
        return bool(self.context)

    def enqueue(self, action, item_id=None, credentials=None):
        if self.busy:
            raise ValueError("busy")
        self.calls.append(action)


class Clock:
    def __init__(self, value=T):
        self.value = value

    def __call__(self):
        return self.value
