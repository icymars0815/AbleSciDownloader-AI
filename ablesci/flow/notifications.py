"""System notifications: master + per-item toggles, quiet hours with a single summary afterwards, and de-duplication.
Under AI takeover results go to the inbox (kytdl inbox); otherwise through an injected sink; everything is also recorded in the app event list."""
import re
import threading
import time
from datetime import datetime, timedelta

KINDS = {   # kind: (label, default)
    "applied": ("新应助", True),
    "download_ok": ("下载并核对通过", True),
    "needs_action": ("需处理（采纳 / 驳回、核对未通过）", True),
    "needs_human": ("需人工验证（出版社人机验证）", True),
    "min_bounty": ("最低悬赏上调，待确认", True),
    "login_expired": ("登录失效", True),
    "closed": ("求助已关闭或到期退还积分", False),
    "oa_ok": ("免费全文已下载", False),
    "sync_failed": ("同步失败（连续 3 次以上提示）", False),
}
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
ACTIONS = {"applied": "focus", "download_ok": "focus", "needs_action": "focus", "needs_human": "oa-human", "min_bounty": "focus", "login_expired": "login"}
KEEP = 200   # remembered (item, kind) pairs
AI_RECENT_SECONDS = 30 * 60
# Under AI takeover nothing pops up (user, 2026-11-09: "放弃一切打断体验的东西"). Results -- downloaded, not obtainable,
# points needed, not enough points, login needed -- go to the inbox, which the AI reads (kytdl inbox / MCP inbox) and
# relays to the person. Progress notices (a new answer, a failed sync) are only recorded in the event list.
PROGRESS_KINDS = ("applied", "sync_failed")
FINAL_KINDS = ("download_ok", "oa_ok")   # a paper's final good result; it supersedes earlier unread problems for that paper
INBOX_KEEP = 200   # an MCP call this recent means an AI harness is driving: no pop-ups, it reads needs_attention instead


def defaults():
    return dict(master=True, items={k: v[1] for k, v in KINDS.items()}, quiet_enabled=False, quiet_start="22:00", quiet_end="08:00")


def settings(prefs):
    """Effective settings from preferences (missing or malformed values fall back to defaults)."""
    prefs = prefs or {}
    out = defaults()
    if isinstance(prefs.get("notify_master"), bool):
        out["master"] = prefs["notify_master"]
    stored = prefs.get("notify_items")
    if isinstance(stored, dict):
        for kind in KINDS:
            if isinstance(stored.get(kind), bool):
                out["items"][kind] = stored[kind]
    if isinstance(prefs.get("notify_quiet_enabled"), bool):
        out["quiet_enabled"] = prefs["notify_quiet_enabled"]
    for key in ("quiet_start", "quiet_end"):
        value = prefs.get("notify_" + key)
        if isinstance(value, str) and TIME_RE.fullmatch(value):
            out[key] = value
    return out


def validate(body, current):
    """Strict validation of a POST body; returns preference changes."""
    allowed = {"master", "items", "quiet_enabled", "quiet_start", "quiet_end"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise ValueError("通知设置无效")
    changes = {}
    for key in ("master", "quiet_enabled"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError("通知开关参数无效")
            changes["notify_" + key] = body[key]
    if "items" in body:
        items = body["items"]
        if not isinstance(items, dict) or not items or set(items) - set(KINDS) or any(not isinstance(v, bool) for v in items.values()):
            raise ValueError("单项通知开关无效")
        merged = dict(current["items"])
        merged.update(items)
        changes["notify_items"] = merged
    for key in ("quiet_start", "quiet_end"):
        if key in body:
            if not isinstance(body[key], str) or not TIME_RE.fullmatch(body[key]):
                raise ValueError("免打扰时间须为 HH:MM")
            changes["notify_" + key] = body[key]
    return changes


def in_quiet(cfg, moment):
    if not cfg["quiet_enabled"]:
        return False
    start, end = cfg["quiet_start"], cfg["quiet_end"]
    now = moment.strftime("%H:%M")
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


class Notifier:
    """notify(kind, key, title, message): at most once per (key, kind) until reset(key, kind).
    sink(title, message) shows a toast; state lives in store.data["notifications"] so a restart does not repeat toasts."""

    def __init__(self, store, sink=None, clock=lambda: datetime.now().astimezone()):
        self.store, self.sink, self.clock = store, sink, clock
        self.lock = threading.RLock()
        with store.lock:
            self.state = store.data.setdefault("notifications", {"seen": [], "pending": []})
            self.state.setdefault("seen", [])
            self.state.setdefault("pending", [])
            self.state.setdefault("inbox", [])

    def cfg(self):
        return settings(self.store.snapshot().get("preferences", {}))

    def _save(self):
        with self.store.lock:
            self.state["seen"] = self.state["seen"][-KEEP:]
            self.store.save()

    def reset(self, key, kind):
        """Allow the same (key, kind) to notify again, e.g. after a successful sync cleared a login problem."""
        with self.lock:
            pair = [key, kind]
            if pair in self.state["seen"]:
                self.state["seen"].remove(pair)
                self._save()

    def to_inbox(self, kind, key, title, message):
        inbox = self.state.setdefault("inbox", [])
        # One website stop often arrives twice (publish notice + the item's "needs action"; real 2026-11-09): an unread
        # entry from the last 10 minutes whose text contains the other is the same news, so keep only one.
        cutoff = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        for old in inbox[-10:]:
            if (not old.get("acked") and old.get("time", "") >= cutoff and min(len(old.get("message") or ""), len(message or "")) >= 10
                    and (old["message"] in message or message in old["message"])):
                if len(message) > len(old["message"]):
                    old.update(message=message, kind=kind, title=title)
                    if not key.startswith(("batch:", "account", "sync")):
                        old["item_id"] = key.split(":")[0]
                return
        seq = (inbox[-1]["seq"] + 1) if inbox else int(self.state.get("inbox_seq") or 0) + 1
        self.state["inbox_seq"] = seq
        target = key.split(":", 1)[1] if key.startswith("batch:") else ("" if key.startswith(("account", "sync")) else key.split(":")[0])
        if kind in FINAL_KINDS and target and not key.startswith("batch:"):
            # The paper has its final result: earlier unread problems for it are no longer news (marked superseded).
            for old in inbox:
                if old.get("item_id") == target and not old.get("acked"):
                    old.update(acked=True, superseded=seq)
        inbox.append(dict(seq=seq, time=datetime.now().astimezone().isoformat(timespec="seconds"), kind=kind, title=title,
                          message=message, key=key, item_id="" if key.startswith("batch:") else target,
                          batch_id=target if key.startswith("batch:") else "", acked=False))
        del inbox[:-INBOX_KEEP]

    def inbox(self, include_acked=False):
        with self.lock:
            return [dict(e) for e in self.state.get("inbox", []) if include_acked or not e.get("acked")]

    def resolve_inbox(self, prefix, keep=None):
        """Retire an obsolete limit question after approval/day rollover; retain its audit record."""
        with self.lock:
            changed = False
            for entry in self.state.get("inbox", []):
                if entry.get("key", "").startswith(prefix) and entry.get("key") != keep and not entry.get("acked"):
                    entry.update(acked=True, resolved=True)
                    changed = True
            if changed:
                self._save()

    def ack(self, seqs=None):
        """Mark inbox entries as relayed to the person (all of them when seqs is None). Returns how many changed."""
        with self.lock:
            changed = 0
            for entry in self.state.get("inbox", []):
                if not entry.get("acked") and (seqs is None or entry["seq"] in seqs):
                    entry["acked"] = True
                    changed += 1
            if changed:
                self._save()
            return changed

    def takeover(self):
        from ablesci.flow import settings as app_settings   # local import: settings imports this module
        prefs = self.store.snapshot().get("preferences", {})
        return prefs.get("mcp_silent") is not False and app_settings.ai_takeover(prefs, prefs.get("mcp_actions_account", "default"))

    def ai_driven(self, key_text):
        """Silent mode: while an AI harness drives the work (recent MCP call, or an item / batch it confirmed), nothing pops up;
        the problem is recorded and reported through MCP (needs_attention) so the AI can tell the user. prefs.mcp_silent=False opts out."""
        snap = self.store.snapshot()
        if snap.get("preferences", {}).get("mcp_silent") is False:
            return False
        last = getattr(self.store, "mcp_last_call", None)
        if last is not None and time.time() - last < AI_RECENT_SECONDS:
            return True
        if key_text.startswith("batch:"):
            batch_id = key_text.split(":", 1)[1]
            return any(b.get("id") == batch_id and b.get("origin") == "mcp" for b in snap.get("batches", []))
        item = next((i for i in snap.get("items", []) if i.get("id") == key_text.split(":")[0]), None)
        if not item:
            return False
        if item.get("request_mcp_auto_publish"):
            return True
        batch_id = item.get("request_batch_id") or item.get("batch_id")
        return bool(batch_id and any(b.get("id") == batch_id and b.get("origin") == "mcp" for b in snap.get("batches", [])))

    def notify(self, kind, key, title, message):
        """Returns 'sent' | 'queued' | 'muted' | 'ai' | 'duplicate'."""
        if kind not in KINDS:
            raise ValueError("unknown notification kind")
        with self.lock:
            pair = [str(key), kind]
            if pair in self.state["seen"]:
                return "duplicate"
            self.state["seen"].append(pair)
            cfg = self.cfg()
            now = self.clock()
            if self.takeover():
                # The inbox is how results reach the person under AI takeover (nothing pops up), so the GUI-era pop-up
                # switches do not mute it (real 2026-11-09: "free PDF downloaded" was switched off and the result was lost).
                outcome = "ai"
                if kind not in PROGRESS_KINDS:
                    self.to_inbox(kind, str(key), title, message)
            elif not cfg["master"] or not cfg["items"].get(kind):
                outcome = "muted"
            elif self.ai_driven(str(key)):
                outcome = "ai"
            elif in_quiet(cfg, now):
                self.state["pending"].append(dict(kind=kind, title=title, message=message))
                outcome = "queued"
            else:
                outcome = "sent"
            self._save()
        key_text = str(key)
        self.store.event("[通知] " + message + {"muted": "（通知已关闭，仅记录）", "queued": "（免打扰时段，稍后汇总）", "ai": "（AI 处理中，未弹窗，已交给 AI 反馈）"}.get(outcome, ""),
                         kind="notify", item_id=key_text.split(":")[0] if not key_text.startswith(("account", "sync", "batch")) else "",
                         batch_id=key_text.split(":", 1)[1] if key_text.startswith("batch:") else "",
                         action="open-batch" if key_text.startswith("batch:") else ACTIONS.get(kind, ""))
        if outcome == "sent":
            self._show(title, message)
        return outcome

    def flush(self):
        """After quiet hours: one summary toast for everything queued. Returns True if a summary was shown."""
        with self.lock:
            cfg = self.cfg()
            if not self.state["pending"] or not cfg["master"] or in_quiet(cfg, self.clock()):
                return False
            pending, self.state["pending"] = self.state["pending"], []
            self._save()
        lines = [p["message"] for p in pending if cfg["items"].get(p["kind"])]
        if not lines:
            return False
        text = "免打扰期间共 %d 条通知：" % len(lines) + "；".join(lines[:3]) + ("…" if len(lines) > 3 else "")
        self._show("文献通互助 通知汇总", text[:250])
        return True

    def pending(self):
        return len(self.state["pending"])

    def _show(self, title, message):
        if self.sink:
            try:
                self.sink(title, message[:250])
            except Exception:
                pass
