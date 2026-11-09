"""Visible, isolated browser. Website publication and accept/reject are manual."""
import contextlib
import queue
import threading
from ablesci.system import applog
from ablesci.system.runtime_paths import browser_options
import time
from urllib.parse import urljoin

from ablesci.website.accounts import Accounts
from ablesci.flow.ratelimit import RateLimiter, limit_settings
from ablesci.website.agent_account import AccountMixin
from ablesci.website.agent_download import DownloadMixin
from ablesci.website.agent_decide import DecisionMixin
from ablesci.website.agent_layers import LayersMixin
from ablesci.website.agent_publish import PublishMixin
from ablesci.website.agent_common import BASE, SessionExpired, PreconditionError, TransientError, RetryScheduled, OPEN_TIMEOUT_TEXT, is_transient  # noqa: F401  (re-exported)


class BrowserAgent(AccountMixin, DownloadMixin, DecisionMixin, LayersMixin, PublishMixin):
    def __init__(self, store, accounts=None, limiter=None, on_done=None, clock=time.monotonic, use_worker=False):
        self.store = store
        # One limiter for every 科研通 navigation/request: >= 3 s apart (3–30 s, preference request_gap_seconds).
        self.limiter = limiter or RateLimiter(lambda: limit_settings(store.snapshot().get("preferences", {}))["request_gap_seconds"])
        self.on_done = on_done          # callback(action, item_id, error) after each command; used by the monitor
        self.clock = clock
        self.last_activity = clock()
        self.accounts = accounts or Accounts(store.root)
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self._enqueue_lock = threading.Lock()
        self._closed = False
        self.context = None
        self.page = None
        self.worker = None
        self._state_waiter = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if use_worker:
            from ablesci.website.download_worker import DownloadWorker
            self.worker = DownloadWorker(self)

    def request_stop(self):
        """Stop the current command and any download running in the dedicated worker."""
        self.stop.set()
        worker = getattr(self, "worker", None)
        if worker:
            worker.stop.set()

    def download_busy(self):
        worker = getattr(self, "worker", None)
        return bool(worker and worker.busy)

    def worker_status(self):
        worker = getattr(self, "worker", None)
        if not worker:
            return dict(enabled=False, status="idle", item_id="")
        return dict(enabled=True, status="downloading" if worker.busy else "idle", item_id=worker.current_item if worker.busy else "")

    def revive(self):
        """Watchdog: restart the worker thread if it died unexpectedly (not after a deliberate close). True if restarted."""
        thread = getattr(self, "thread", None)
        if thread is None or thread.is_alive() or getattr(self, "_closed", False):
            return False
        if self.commands.empty():
            self.busy = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        applog.record(self.store, "browser", "revive", result="warn", message="browser agent thread was not alive; restarted")
        try:
            self.store.event("后台浏览器线程意外停止，已自动重启。如反复出现，请生成错误报告。", kind="error")
        except Exception:
            pass
        return True

    def assert_mcp_authority(self):
        """Recheck queued/in-flight MCP work at the point of a site action."""
        scope = getattr(self, "_mcp_scope", None)
        if not scope:
            return
        prefs = self.store.snapshot().get("preferences", {})
        active = self.accounts.active_id
        if (self.stop.is_set() or active != scope["account_id"] or not prefs.get("mcp_website_actions")
                or prefs.get("mcp_actions_account", "default") != active
                or scope.get("generation") != prefs.get("mcp_action_generation", 0)):
            raise PreconditionError("MCP 后台操作已停止或授权已撤销，未继续操作")
        if scope.get("auto") and scope.get("item_id"):
            item = self.store.get(scope["item_id"]) or {}
            if (item.get("account_id", "default") != active or item.get("request_confirmed") is not True
                    or item.get("request_mcp_auto_publish") is not True):
                raise PreconditionError("该文献的 MCP 后台续办已暂停，请主动恢复后继续")

    @contextlib.contextmanager
    def throttled(self):
        """Context manager for one 科研通 navigation/request (agents built without __init__ in tests skip it)."""
        limiter = getattr(self, "limiter", None)
        with limiter.slot(self.stop) if limiter else contextlib.nullcontext():
            self.assert_mcp_authority()
            yield

    def has_browser(self):
        return bool(self.context)

    def enqueue(self, action, item_id=None, credentials=None):
        # Check-and-put is atomic and the agent counts as busy from this moment (run() resets it when the command ends).
        lock = self.__dict__.setdefault("_enqueue_lock", threading.Lock())
        if getattr(self, "worker", None) and self.worker.busy:
            if action == "download":
                raise ValueError("已有下载在进行，同一时间只下载一篇；请等它完成或停止")
            if action in ("login", "switch_account", "close_idle"):
                raise ValueError("下载进行中，暂不能登录或切换账户；请等下载完成或先停止")
        with lock:
            if self.busy or not self.commands.empty():
                raise ValueError("已有任务运行，请等待完成或停止后再操作")
            self.stop.clear()
            self.busy = True
            self.commands.put((action, item_id, credentials))

    def run(self):
        while True:
            action, item_id, credentials = self.commands.get()
            self.busy = True
            error = None
            self._mcp_scope = credentials.get("mcp_scope") if isinstance(credentials, dict) else None
            try:
                if action != "close":
                    self.assert_mcp_authority()
                if action == "close":
                    self._closed = True
                    if getattr(self, "worker", None):
                        self.worker.shutdown()
                    try:
                        if self.context:
                            self.context.close()
                    except Exception:
                        pass
                    finally:
                        if hasattr(self, "playwright"):
                            self.playwright.stop()
                    return
                elif action == "login":
                    self.login(item_id, credentials)
                elif action == "download" and getattr(self, "worker", None):
                    self.hand_over_download(item_id)
                else:
                    getattr(self, action)(item_id)
            except Exception as exc:
                error = exc
                try:
                    self.handle_failure(action, item_id, exc)
                except Exception as inner:   # a failing store write must never kill the worker thread
                    try:
                        applog.record(self.store, "browser", "handle_failure", inner, item_id=item_id or "")
                    except Exception:
                        pass
            finally:
                credentials = None
                self._mcp_scope = None
                self.busy = False
                try:
                    if action != "close_idle":
                        self.last_activity = self.clock()
                    self.commands.task_done()
                except Exception:
                    pass
                if self.on_done and action != "close":
                    try:
                        self.on_done(action, item_id, error)
                    except Exception:
                        pass

    def handle_failure(self, action, item_id, exc):
        applog.record(self.store, "browser", action, exc, item_id=item_id or "")
        known = bool(item_id and self.store.get(item_id))
        if isinstance(exc, PreconditionError):
            # Refused before doing anything: say so, but leave the item's status exactly as it was.
            self.store.event(str(exc), item_id=item_id if known else None, kind="error", action="focus" if known else "")
            return
        if isinstance(exc, RetryScheduled):
            # download() already stored status "retrying" + download_retry; just tell the person.
            self.store.event(str(exc), item_id=item_id if known else None, kind="download", action="focus" if known else "")
            return
        if self.page:
            try:
                screenshot_folder = self.store.root / "screenshots"
                screenshot_folder.mkdir(exist_ok=True)
                self.page.screenshot(path=str(screenshot_folder / "browser-last.png"), mask=[self.page.locator("input")])
            except Exception:
                pass
        # Do not log third-party response bodies, URLs with tokens or credentials.
        message = str(exc) if isinstance(exc, ValueError) else "浏览器操作未完成（登录失效、页面变化或网络超时），请查看独立浏览器窗口后重试"
        self.store.event(message, item_id=item_id if known else None, kind="error", action="focus" if item_id else "")
        if known:
            self.store.update(item_id, status="attention", note=message)
        elif is_transient(exc):
            self.store.event("网络或科研通暂时无法访问，已记录。自动流程保持开启，稍后由后台监控自动重试。")
        elif self.store.snapshot().get("preferences", {}).get("pipeline_enabled"):
            self.store.update_preferences(pipeline_enabled=False)
            self.store.event("自动流程步骤失败，已自动暂停。请处理后重新开启。")

    def close_context(self):
        # Closing an already-closed browser (idle close, crash) must never fail (real 2026-10-20: a scheduled retry was
        # handed to the human because of TargetClosedError here).
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None

    def close_idle(self, _=None):
        """Release the headless browser after a quiet period; the next sync starts it again. Never closes a login window."""
        if self.context and getattr(self, "interactive", False):
            return
        self.close_context()
        if hasattr(self, "playwright"):
            try:
                self.playwright.stop()
            except Exception:
                pass
            del self.playwright

    def switch_account(self, account_id):
        self.close_context()
        self.accounts.switch(account_id)
        self.store.update_preferences(pipeline_enabled=False)
        self.store.event("已切换账户。各账户的登录状态和求助记录相互独立，未打开登录窗口。")

    def assert_account(self, item):
        if item.get("account_id", "default") != self.accounts.active_id:
            raise PreconditionError("该任务属于另一个账户，请先切换账户")

    def ensure(self, interactive=False):
        if self.context and getattr(self, "interactive", False) != interactive:
            if not interactive and "/site/login" in self.page.url:
                raise ValueError("请先在登录窗口完成扫码或验证码，再回到软件同步")
            self.close_context()
        if self.context:
            try:
                if self.page.is_closed():
                    self.page = self.context.new_page()
                return
            except Exception:
                self.context = None
        from playwright.sync_api import sync_playwright
        if hasattr(self, "playwright"):
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = sync_playwright().start()
        self.interactive = interactive
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.accounts.account_root() / "browser-profile"), **browser_options(), headless=not interactive,
            args=["--app=" + BASE + "/site/login", "--window-size=1080,850"] if interactive else [],
            accept_downloads=True, viewport={"width": 1080, "height": 850})
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(12000)

    def open_page(self, url, timeout=45000):
        """page.goto with a plain-language message when the site does not answer in time."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            if any(c.__name__ == "TimeoutError" for c in type(exc).__mro__):
                raise TransientError(OPEN_TIMEOUT_TEXT) from exc
            raise

    def goto(self, path, interactive=False):
        self.ensure(interactive=interactive)
        with self.throttled():
            self.open_page(urljoin(BASE, path))
        if "/site/login" in self.page.url:
            raise SessionExpired("账户登录状态已失效，请重新登录。后台操作不会自动弹出窗口")

    def export_state(self, _=None):
        """Runs on THIS agent's thread: the session's cookies/storage as an in-memory dict (never written to disk)."""
        self.ensure()
        return self.context.storage_state()

    def serve_state_request(self, _=None):
        waiter = self._state_waiter
        if waiter is None:
            return
        try:
            waiter["state"] = self.export_state()
        except Exception as exc:
            waiter["error"] = exc
        waiter["event"].set()

    def fetch_state(self, stop, timeout=120):
        """Called from the worker thread: ask the main agent (on its own thread) for a fresh storage_state.
        Returns None when the main agent stays busy, fails, or a stop is requested."""
        waiter = dict(event=threading.Event(), state=None, error=None)
        deadline = time.monotonic() + timeout
        while True:
            if stop.is_set() or time.monotonic() > deadline:
                return None
            try:
                self._state_waiter = waiter
                self.enqueue("serve_state_request")
                break
            except ValueError:
                stop.wait(0.5)
        while not waiter["event"].wait(0.5):
            if stop.is_set() or time.monotonic() > deadline:
                return None
        return waiter["state"]

    # ------------------------------------------------------------------------------------------------
    # 发布求助（单篇）。流程与二次核对见 docs/PUBLISH_FLOW_DESIGN.md。页面选择器里确认来自侦察的：
    # #onekey、button.onekey-search、layer 弹窗、#Assist-doi/#Assist-title/#Assist-point/#Assist-close_at、#form-submit-btn、
    # check-before-assist-create 的 code 0/1/2；弹窗里各字段的具体文字、弹窗确认按钮的文字属推测，已做成容错解析。
    # ------------------------------------------------------------------------------------------------
    def notify_user(self, kind, key, title, message):
        notifier = getattr(self, "notifier", None)
        if notifier:
            try:
                notifier.notify(kind, key, title, message)
            except Exception:
                pass


