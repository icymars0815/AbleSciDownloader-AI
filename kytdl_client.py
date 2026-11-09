"""Small client for the local background service API (127.0.0.1 only), used by kytdl.py.
No business logic lives here: it reads the per-run token from data/.token, posts JSON, and offers one long-poll helper."""
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_BASE = "http://127.0.0.1:8769"
POLL_SECONDS = 2
WAIT_MAX_SECONDS = 600
NOT_RUNNING = "后台服务没有运行：先执行 kytdl start 启动"
TOKEN_PATH = Path(__file__).resolve().parent / "data" / ".token"


class ServiceDown(Exception):
    """The workbench is not running (or not reachable on loopback)."""


class ApiError(Exception):
    """The workbench answered with an error. reason is a short machine code when the server gave one."""
    def __init__(self, message, status=400, reason=""):
        super().__init__(message)
        self.status, self.reason = status, reason

    @property
    def permission(self):
        return self.reason == "permission_denied"


def check_base(base):
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Only the local workbench (http://127.0.0.1:PORT) is allowed")
    return base.rstrip("/")


class Client:
    def __init__(self, base=None, token_path=None):
        self.base = check_base((base or os.environ.get("ABLESCI_LOCAL_URL") or DEFAULT_BASE).rstrip("/"))
        self.token_path = Path(token_path or os.environ.get("ABLESCI_TOKEN_FILE") or TOKEN_PATH)
        self._token = None

    def token(self, fresh=False):
        if self._token is None or fresh:
            try:
                self._token = self.token_path.read_text(encoding="utf-8").strip()
            except OSError:
                raise ServiceDown(NOT_RUNNING) from None
            if not self._token:
                raise ServiceDown(NOT_RUNNING)
        return self._token

    def _send(self, path, body=None, timeout=30):
        headers = {}
        data = None
        if body is not None:
            headers = {"Content-Type": "application/json", "Origin": self.base, "X-Demo-Token": self.token()}
            data = json.dumps(body).encode()
        try:
            with urlopen(Request(self.base + path, data, headers=headers), timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                info = json.loads(exc.read())
            except ValueError:
                info = {}
            raise ApiError(info.get("error", "Local request failed"), exc.code, info.get("reason", "")) from None
        except (URLError, OSError):
            raise ServiceDown(NOT_RUNNING) from None

    def get(self, path, timeout=30):
        return self._send(path, None, timeout)

    def post(self, path, body=None, timeout=30):
        try:
            return self._send(path, {} if body is None else body, timeout)
        except ApiError as exc:
            if exc.status == 403 and not exc.reason:   # token changed after a service restart: refresh once
                self.token(fresh=True)
                return self._send(path, {} if body is None else body, timeout)
            raise

    def state(self):
        return self.get("/api/state")


# ---------- state helpers shared by wait_for and the CLI ----------
def item_states(item, state):
    """Names an item can be waited on: its stage plus a few derived ones (downloaded, oa_finished, needs_human)."""
    names = {item.get("stage") or "queued"}
    # The submit click is recorded as "attempted" first (no double charge after a crash), which reads as attention.
    # While the agent is still busy with that click the paper is "publishing", not waiting for a person.
    if ("attention" in names and (item.get("publish_action") or {}).get("status") == "attempted"
            and (item.get("publish_check") or {}).get("kind") == "submitting" and state.get("busy")):
        names = {"publishing"}
    if item.get("pdf"):
        names.add("downloaded")
    check = item.get("oa_check") or {}
    running = (state.get("pipeline") or {}).get("oa_running") == item.get("id")
    if check.get("status") == "needs_human" and not running and not item.get("request_confirmed"):
        names.add("needs_human")
    if check and not running:
        names.add("oa_finished")
    return names


def find_target(state, target_id):
    """('batch', row) | ('paper', row) | (None, None)."""
    batch = next((b for b in state.get("batches") or [] if b["id"] == target_id), None)
    if batch:
        return "batch", batch
    paper = next((i for i in state.get("items") or [] if i["id"] == target_id), None)
    return ("paper", paper) if paper else (None, None)


def snapshot_target(state, target_id):
    kind, row = find_target(state, target_id)
    if kind == "batch":
        return kind, {row["phase"]}, row
    if kind == "paper":
        return kind, item_states(row, state), row
    return None, set(), None


def wait_until(client, target_id, until, timeout=60, poll=None, sleep=time.sleep, clock=time.monotonic):
    """Long poll the local API every POLL_SECONDS (never longer than 600 s). Returns what happened and the events seen meanwhile.
    A paper that needs a person (stage attention / needs_human) ends the wait early with reached=false and reason."""
    wanted = {u.strip() for u in (until.split(",") if isinstance(until, str) else until) if u and u.strip()}
    if not wanted:
        raise ValueError("until 不能为空")
    timeout = max(1, min(int(timeout), WAIT_MAX_SECONDS))
    poll = POLL_SECONDS if poll is None else poll
    start = clock()
    state = client.state()
    seen = {(e.get("time"), e.get("message")) for e in state.get("events") or []}
    events = []
    while True:
        kind, names, row = snapshot_target(state, target_id)
        if kind is None:
            return dict(ok=False, reason="not_found", target=target_id, events=events)
        waited = round(clock() - start, 1)
        hit = sorted(wanted & names)
        stop = sorted({"attention", "needs_human"} & names - wanted) if kind == "paper" else []
        if hit or stop or waited >= timeout:
            return dict(ok=True, kind=kind, reached=bool(hit), states=sorted(names), matched=hit,
                        reason="" if hit else ("needs_person" if stop else "timeout"), waited_seconds=waited, events=events)
        sleep(min(poll, max(0.0, timeout - waited)))
        state = client.state()
        for event in state.get("events") or []:
            key = (event.get("time"), event.get("message"))
            if key not in seen:
                seen.add(key)
                events.append(event)
