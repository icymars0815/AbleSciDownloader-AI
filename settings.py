"""One place for effective settings (refactor stage 2) and the smart mode preset (docs/SMART_MODE_AND_EDITION_ASSESSMENT_20261102.md).
Smart mode is a single switch the person turns on once: it sets the handful of existing preferences an AI-driven run needs
(MCP website permission, auto-accepting a raised minimum reward up to a cap, reserve points, default reward, background
monitor, silent mode) and adds a daily spending cap. Turning it off restores the values it replaced. It never removes a
human decision: login/captcha, confirming each batch, rejections and anything above the caps still stop and ask."""
from datetime import datetime

import notifications
import publish_policy as pp
import zotero_bridge
from download_policy import download_settings, chosen_mode

SMART_RULES = "smart-mode-v1"
RAISE_CHOICES = (30, 50, 100, 150)
RESERVE_RANGE = (0, 1000)
CAP_RANGE = (10, 2000)
DEFAULTS = dict(raise_max=50, reserve=50, daily_cap=200)
# AI takeover: papers whose reward is at most this many points are requested without asking (0 = always ask).
# Stored only when the person sets it explicitly (CLI `kytdl ai on --auto-max N` or the settings page); never a silent default.
AUTO_CONFIRM_RANGE = (0, 50)
# Preferences smart mode writes; their previous values are kept in smart_snapshot and put back when it is turned off.
MANAGED = ("mcp_website_actions", "mcp_actions_account", "publish_raise_mode", "publish_raise_rules", "publish_raise_max",
           "publish_reserve_points", "default_reward", "monitor_enabled", "mcp_silent")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def smart(prefs):
    """Effective smart mode: on only with the stored rules confirmation; damaged numbers fall back to the defaults."""
    prefs = prefs or {}
    cfg = prefs.get("smart_config") if isinstance(prefs.get("smart_config"), dict) else {}
    out = dict(enabled=prefs.get("smart_mode") is True and prefs.get("smart_rules") == SMART_RULES, **DEFAULTS)
    if cfg.get("raise_max") in RAISE_CHOICES:
        out["raise_max"] = cfg["raise_max"]
    if _is_int(cfg.get("reserve")) and RESERVE_RANGE[0] <= cfg["reserve"] <= RESERVE_RANGE[1]:
        out["reserve"] = cfg["reserve"]
    if _is_int(cfg.get("daily_cap")) and CAP_RANGE[0] <= cfg["daily_cap"] <= CAP_RANGE[1]:
        out["daily_cap"] = cfg["daily_cap"]
    auto = cfg.get("auto_confirm_max")
    out["auto_confirm_max"] = auto if _is_int(auto) and AUTO_CONFIRM_RANGE[0] <= auto <= AUTO_CONFIRM_RANGE[1] else 0
    out["auto_confirm_since"] = cfg.get("auto_confirm_since") if out["auto_confirm_max"] and isinstance(cfg.get("auto_confirm_since"), str) else None
    return out


# ---- AI takeover (docs/AI_TAKEOVER_20261109.md) ----
def ai_takeover(prefs, account_id):
    """The person handed downloads to an AI: smart mode is on (its rules confirmed) and the MCP website permission is on
    for this account. Then confirmed papers run publish -> wait -> download -> verify -> adopt-if-matching on their own,
    across restarts, without the separate pipeline / auto-publish switches. Every spending guard still applies."""
    prefs = prefs or {}
    return (smart(prefs)["enabled"] and prefs.get("mcp_website_actions") is True
            and prefs.get("mcp_actions_account", "default") == (account_id or "default"))


# Under AI takeover nothing that only tunes the GUI-era flow is asked of the person ("they just want the paper"):
AI_DEFAULT_AUTO = 10            # `kytdl ai on` proposes this no-ask amount when none is stored (user, 2026-11-09: "≤10 分不用问")
AI_HIDDEN_SETUP = ("smart_mode_off", "raise_mode_ask", "zotero_available", "mcp_permission")


def oa_challenge_mode(prefs, account_id):
    """Publisher page needs a human check: under AI takeover never stop to ask or open a window (prompt/popup -> skip);
    the paper then goes the usual way (科研通 request within what the person agreed to)."""
    mode = (prefs or {}).get("oa_challenge_mode", "prompt")
    return "skip" if ai_takeover(prefs, account_id) else mode


def raise_settings_for(prefs, account_id, base):
    """Reward-raise setting for one paper. Under AI takeover a raised minimum is accepted only up to what the person
    already agreed to for this paper (its confirmed reward, or the per-paper no-ask amount): above that it stops and asks.
    Within that amount it is accepted even if the GUI-era raise setting says "ask" (real 2026-11-09: a stored "ask"
    stopped a paper although it was set to accept within the agreed amount); the reserve check stays."""
    out = pp.raise_settings(prefs)
    if ai_takeover(prefs, account_id):
        agreed = max(base if _is_int(base) else 0, smart(prefs)["auto_confirm_max"])
        out = dict(out, mode="auto", max=min(out["max"], agreed))
    return out


def validate_smart(body, current=None):
    """{"enabled": bool, "confirmed": true when turning on, optional raise_max / reserve / daily_cap / auto_confirm_max}.
    current: the stored smart_config; an auto_confirm_max that is not sent keeps its stored value (never a silent default)."""
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool) or set(body) - {"enabled", "confirmed", "raise_max", "reserve", "daily_cap", "auto_confirm_max"}:
        raise ValueError("智能模式参数无效")
    if body["enabled"] and body.get("confirmed") is not True:
        raise ValueError("请先阅读并确认智能模式会设置的内容")
    cfg = dict(DEFAULTS)
    if "raise_max" in body:
        if body["raise_max"] not in RAISE_CHOICES:
            raise ValueError("自动接受上限只能选 %s 积分" % "、".join(map(str, RAISE_CHOICES)))
        cfg["raise_max"] = body["raise_max"]
    for key, (low, high), label in (("reserve", RESERVE_RANGE, "保留积分"), ("daily_cap", CAP_RANGE, "每日花费上限")):
        if key in body:
            if not _is_int(body[key]) or not low <= body[key] <= high:
                raise ValueError("%s须为 %d–%d 的整数" % (label, low, high))
            cfg[key] = body[key]
    current = current if isinstance(current, dict) else {}
    if "auto_confirm_max" in body:
        value = body["auto_confirm_max"]
        if not _is_int(value) or not AUTO_CONFIRM_RANGE[0] <= value <= AUTO_CONFIRM_RANGE[1]:
            raise ValueError("免确认额度须为 %d–%d 的整数（0 表示每篇都问）" % AUTO_CONFIRM_RANGE)
        cfg["auto_confirm_max"] = value
        if value:   # only batches screened after this moment are confirmed without asking
            same = current.get("auto_confirm_max") == value and isinstance(current.get("auto_confirm_since"), str)
            cfg["auto_confirm_since"] = current["auto_confirm_since"] if same else datetime.now().astimezone().isoformat(timespec="seconds")
    elif _is_int(current.get("auto_confirm_max")):
        cfg["auto_confirm_max"] = current["auto_confirm_max"]
        if isinstance(current.get("auto_confirm_since"), str):
            cfg["auto_confirm_since"] = current["auto_confirm_since"]
    return cfg


def smart_on(prefs, account_id, cfg):
    """Preference changes that turn smart mode on (snapshot taken only when it was off, so re-saving keeps the original)."""
    prefs = prefs or {}
    changes = dict(smart_mode=True, smart_rules=SMART_RULES, smart_config=dict(cfg),
                   mcp_website_actions=True, mcp_actions_account=account_id,
                   publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=cfg["raise_max"],
                   publish_reserve_points=cfg["reserve"], default_reward=10, monitor_enabled=True, mcp_silent=True)
    if not smart(prefs)["enabled"]:
        changes["smart_snapshot"] = {k: prefs.get(k) for k in MANAGED}
    return changes


def smart_off(prefs):
    """Preference changes that turn smart mode off and put back what it replaced (unknown earlier values become unset)."""
    snapshot = (prefs or {}).get("smart_snapshot")
    restored = {k: snapshot.get(k) for k in MANAGED} if isinstance(snapshot, dict) else {}
    return dict(restored, smart_mode=False, smart_rules="", smart_snapshot=None)


# ---- daily spending (rewards and paid high speed), per account ----
def _day(when=None):
    return (when or datetime.now().astimezone()).strftime("%Y-%m-%d")


def spent_today(account, when=None):
    log = (account or {}).get("spend_log")
    value = log.get(_day(when)) if isinstance(log, dict) else 0
    return value if _is_int(value) else 0


def record_spend(store, account_id, amount, when=None):
    """Add points spent today (publish reward, or a charged high-speed click). Keeps the last 14 days."""
    if not _is_int(amount) or amount <= 0:
        return
    account = store.snapshot().get("account_metrics", {}).get(account_id, {})
    log = dict(account.get("spend_log") or {}) if isinstance(account.get("spend_log"), dict) else {}
    day = _day(when)
    log[day] = (log.get(day) if _is_int(log.get(day)) else 0) + amount
    store.update_account(account_id=account_id, spend_log={k: log[k] for k in sorted(log)[-14:]})


def cap_block(prefs, account, amount, when=None):
    """Plain-Chinese reason when spending `amount` now would pass smart mode's daily cap, else ''. Only smart mode has a cap."""
    cfg = smart(prefs)
    if not cfg["enabled"]:
        return ""
    spent = spent_today(account, when)
    if spent + (amount if _is_int(amount) else 0) > cfg["daily_cap"]:
        return "今日已花费 %d 积分，再花 %d 将超过智能模式每日上限 %d 积分；明天自动继续，或在设置中调高上限" % (spent, amount, cfg["daily_cap"])
    return ""


# ---- the one place to read every effective setting ----
def effective(prefs, account=None, account_id=None):
    """Effective values of all setting groups, as the rest of the app interprets them (read-only view for the UI, MCP and docs)."""
    prefs = prefs or {}
    mode, chosen = chosen_mode(prefs)
    return dict(smart=dict(smart(prefs), spent_today=spent_today(account)),
                ai_takeover=ai_takeover(prefs, account_id or "default"),
                publish_raise=pp.raise_settings(prefs), default_reward=prefs.get("default_reward"),
                mcp=dict(website_actions=bool(prefs.get("mcp_website_actions")), account=prefs.get("mcp_actions_account", "default"),
                         silent=prefs.get("mcp_silent") is not False),
                download=download_settings(prefs), highspeed=dict(mode=mode, chosen_by_user=chosen),
                notifications=notifications.settings(prefs), zotero=zotero_bridge.settings(prefs),
                oa=dict(first=prefs.get("oa_first", True) is not False, contact_email=bool(prefs.get("oa_contact_email")),
                        challenge_mode=prefs.get("oa_challenge_mode", "prompt")),
                monitor_enabled=bool(prefs.get("monitor_enabled")))
