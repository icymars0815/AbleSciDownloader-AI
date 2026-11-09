"""Preference, monitor, notification, autostart, limit and pipeline-setting routes (POST /api/...), moved unchanged from app.do_POST in refactor stage 1.
Each route(c, h, body, path, origin): c holds the server objects, h is the request handler; a route that does not
respond itself gets {"ok": true} (see api_routes.dispatch)."""
import re

from download_policy import RULES_VERSION, HIGHSPEED_MODES, FORCE_DEFAULT_POINTS, validate_download_settings, download_defaults_as_preferences
from monitor import validate_monitor
from pipeline import PIPELINE_RULES_VERSION, rules_ok
from ratelimit import validate_limits
from rejection_policy import REJECTION_RULES_VERSION
import app_info
import intake
import notifications
import publish_policy as pp
import settings as app_settings
import zotero_bridge


def route_preferences(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    pipeline = c.pipeline
    monitor = c.monitor
    if "default_reward" in body:
        points_now = store.snapshot().get("account_metrics", {}).get(accounts.active_id, {}).get("points")
        store.update_preferences(default_reward=intake.validate_reward(body["default_reward"], points_now))
        return h.respond(200, {"ok": True})
    if "disclaimer_ack" in body:
        if body["disclaimer_ack"] is not True:
            raise ValueError("参数无效")
        store.update_preferences(disclaimer_ack=app_info.DISCLAIMER_VERSION)
        return h.respond(200, {"ok": True})
    if "smart_mode" in body:
        prefs_now = store.snapshot().get("preferences", {})
        cfg = app_settings.validate_smart(body["smart_mode"], prefs_now.get("smart_config"))
        if body["smart_mode"]["enabled"]:
            store.update_preferences(**app_settings.smart_on(prefs_now, accounts.active_id, cfg))
            monitor.set_enabled(True)
            store.event("智能模式已开启：MCP 网站操作、最低悬赏自动接受（≤%d）、保留 %d 积分、每日花费上限 %d 积分、后台监控、静默模式。"
                        % (cfg["raise_max"], cfg["reserve"], cfg["daily_cap"])
                        + ("每篇不超过 %d 积分的求助无需逐条确认。" % cfg["auto_confirm_max"] if cfg.get("auto_confirm_max") else ""))
        else:
            changes = app_settings.smart_off(prefs_now)
            store.update_preferences(**changes)
            monitor.set_enabled(bool(changes.get("monitor_enabled")))
            if not changes.get("mcp_website_actions"):
                pipeline.pause_mcp()
            store.event("智能模式已关闭，相关设置已恢复为开启前的值。")
        return h.respond(200, {"ok": True, "smart": app_settings.smart(store.snapshot().get("preferences", {}))})
    if "zotero" in body:
        store.update_preferences(**zotero_bridge.validate(body["zotero"]))
        return h.respond(200, {"ok": True, "zotero": zotero_bridge.settings(store.snapshot().get("preferences", {}))})
    if "setup_email_hint_hidden" in body:
        if body["setup_email_hint_hidden"] is not True:
            raise ValueError("参数无效")
        store.update_preferences(setup_email_hint_hidden=True)
        return h.respond(200, {"ok": True})
    if "home_notice_hidden" in body:
        if not isinstance(body["home_notice_hidden"], bool):
            raise ValueError("首页提示参数无效")
        store.update_preferences(home_notice_hidden=body["home_notice_hidden"])
        return h.respond(200, {"ok": True})
    if "mcp_website_actions" in body:
        enabled = body["mcp_website_actions"]
        if not isinstance(enabled, bool):
            raise ValueError("请选择 MCP 的科研通操作权限")
        store.update_preferences(mcp_website_actions=enabled, mcp_actions_account=accounts.active_id if enabled else "")
        if not enabled:
            pipeline.pause_mcp()
        store.event("MCP 科研通操作权限已" + ("开启" if enabled else "关闭"))
        return h.respond(200, {"ok": True})
    if "rejection_mode" in body:
        mode = body["rejection_mode"]
        if mode not in {"manual", "auto"}:
            raise ValueError("请选择手动或自动驳回模式")
        if mode == "auto" and body.get("rejection_rules_version") != REJECTION_RULES_VERSION:
            raise ValueError("请先确认自动驳回规则")
        store.update_preferences(rejection_mode=mode, rejection_rules_version=REJECTION_RULES_VERSION if mode == "auto" else "")
        store.event("驳回模式已设为" + ("自动，仅当 DOI 与标题均明确不符时才提交驳回。" if mode == "auto" else "手动，异常情况由用户判断。"))
        return h.respond(200, {"ok": True})
    if "download_settings" in body or body.get("download_reset") is not None:
        if body.get("download_reset") is not None:
            if body["download_reset"] is not True:
                raise ValueError("恢复默认参数无效")
            store.update_preferences(**download_defaults_as_preferences())
            store.event("下载参数已恢复默认。")
        else:
            store.update_preferences(**validate_download_settings(body["download_settings"]))
            store.event("下载参数已更新。")
        return h.respond(200, {"ok": True})
    if "download_highspeed" in body:
        mode = body["download_highspeed"]
        if mode not in HIGHSPEED_MODES:
            raise ValueError("请选择关闭、智能或强制")
        account_points = store.snapshot().get("account_metrics", {}).get(accounts.active_id, {}).get("points")
        free = isinstance(account_points, int) and not isinstance(account_points, bool) and account_points >= FORCE_DEFAULT_POINTS
        if mode != "off" and not free and body.get("rules_version") != RULES_VERSION:
            raise ValueError("积分低于 500 或未知时，请先阅读并确认高速通道规则")
        keep = store.snapshot().get("preferences", {}).get("rules_version", "")
        store.update_preferences(download_highspeed=mode, download_highspeed_chosen=True, download_default_force_applied=False,
                                 allow_paid_fallback=mode != "off",
                                 rules_version=RULES_VERSION if body.get("rules_version") == RULES_VERSION and mode != "off" else ("" if mode == "off" else keep))
        store.event({"off": "高速通道已设为关闭，仅使用 3 条普通线路。",
                     "smart": "高速通道已设为智能，普通线路失败（含预估超时）后切换一次。",
                     "force": "高速通道已设为强制，跳过普通线路；积分低于 500 时，每个文件首次使用高速扣 2 积分。"}[mode])
        return h.respond(200, {"ok": True})
    enabled = body.get("allow_paid_fallback")
    if not isinstance(enabled, bool):
        raise ValueError("请选择是否启用高速通道后备")
    if enabled and body.get("rules_version") != RULES_VERSION:
        raise ValueError("请先阅读并确认高速通道规则")
    store.update_preferences(allow_paid_fallback=enabled, rules_version=RULES_VERSION if enabled else "",
                             download_highspeed="smart" if enabled else "off", download_highspeed_chosen=True,
                             download_default_force_applied=False)
    store.event("已开启高速通道后备：3 条普通线路均失败后切换；积分低于 500 时，每个文件首次最多扣 2 积分。" if enabled else "已关闭高速通道后备。")


def route_monitor(c, h, body, path, origin):
    store = c.store
    monitor = c.monitor
    changes = validate_monitor(body)
    if body.get("sync_now") is True:
        if len(body) > 1: raise ValueError("立即同步需单独提交")
        monitor.sync_now()
        return h.respond(200, {"ok": True})
    if "sync_now" in body: raise ValueError("立即同步参数无效")
    if "monitor_enabled" in changes:
        monitor.set_enabled(changes.pop("monitor_enabled"))
    if changes:
        store.update_preferences(**changes)
        monitor.reschedule()
        store.event("后台监控间隔已更新。")


def route_notifications(c, h, body, path, origin):
    store = c.store
    changes = notifications.validate(body, notifications.settings(store.snapshot().get("preferences", {})))
    store.update_preferences(**changes)
    store.event("通知设置已更新。")


def route_autostart(c, h, body, path, origin):
    store = c.store
    autostart = c.autostart
    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        raise ValueError("开机自启参数无效")
    if body["enabled"]:
        kind = autostart.enable()
        store.event("已开启开机自启（" + ("“启动”文件夹快捷方式" if kind == "lnk" else "“启动”文件夹 .cmd 备用方式") + "）：仅在后台启动服务，不弹出窗口。")
    else:
        autostart.disable()
        store.event("已关闭开机自启，并删除“启动”文件夹中的项目。")


def route_limits(c, h, body, path, origin):
    store = c.store
    store.update_preferences(**validate_limits(body))
    store.event("请求间隔设置已更新。")


def route_pipeline(c, h, body, path, origin):
    store = c.store
    allowed = {"enabled", "auto_publish", "auto_adopt", "sync_minutes", "rules_version", "oa_first", "oa_contact_email", "oa_challenge_mode",
               "raise_mode", "raise_max", "reserve_points", "raise_confirmed", "core_api_key"}
    if not isinstance(body, dict) or not body or set(body) - allowed or not set(body) & (allowed - {"rules_version"}):
        raise ValueError("自动流程设置无效")
    prefs_now = store.snapshot().get("preferences", {})
    changes = {}
    for key in ("enabled", "auto_publish", "auto_adopt"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError("自动流程开关参数无效")
            changes["pipeline_" + key] = body[key]
    if "sync_minutes" in body:
        minutes = body["sync_minutes"]
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 120:
            raise ValueError("同步间隔须为 5–120 的整数（分钟）")
        changes["pipeline_sync_minutes"] = minutes
    if "oa_first" in body:
        if not isinstance(body["oa_first"], bool):
            raise ValueError("免费全文优先开关参数无效")
        changes["oa_first"] = body["oa_first"]
    if "oa_challenge_mode" in body:
        if body["oa_challenge_mode"] not in ("prompt", "popup", "skip"):
            raise ValueError("人机验证处理方式无效")
        changes["oa_challenge_mode"] = body["oa_challenge_mode"]
    if "oa_contact_email" in body:
        email = body["oa_contact_email"]
        if not isinstance(email, str) or (email.strip() and (len(email.strip()) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}", email.strip()))):
            raise ValueError("联系邮箱格式不正确（可留空）")
        changes["oa_contact_email"] = email.strip()
    if set(body) & {"raise_mode", "raise_max", "reserve_points"}:
        changes.update(pp.validate_raise(body, pp.raise_settings(prefs_now)))
    if "core_api_key" in body:
        key = body["core_api_key"]
        if not isinstance(key, str) or len(key.strip()) > 200 or re.search(r"\s", key.strip()):
            raise ValueError("CORE 密钥格式不正确（可留空）")
        changes["core_api_key"] = key.strip()
    final = {k: changes.get("pipeline_" + k, rules_ok(prefs_now, "pipeline_" + k)) for k in ("auto_publish", "auto_adopt")}
    newly = [k for k in final if final[k] and not rules_ok(prefs_now, "pipeline_" + k)]
    if newly and body.get("rules_version") != PIPELINE_RULES_VERSION:
        raise ValueError("请先确认自动流程规则，再开启自动求助或自动采纳")
    changes["pipeline_rules_version"] = PIPELINE_RULES_VERSION if any(final.values()) else ""
    if "pipeline_enabled" in changes:
        changes["pipeline_restart_notice"] = False     # the person has decided; stop reminding
    store.update_preferences(**changes)
    store.event("自动流程设置已更新：自动流程" + ("已开启" if changes.get("pipeline_enabled", prefs_now.get("pipeline_enabled")) else "未开启") + "；自动求助" + ("已开启" if final["auto_publish"] else "已关闭") + "，自动采纳" + ("已开启" if final["auto_adopt"] else "已关闭") + "。")


ROUTES = {
    "/api/preferences": route_preferences,
    "/api/monitor": route_monitor,
    "/api/notifications": route_notifications,
    "/api/autostart": route_autostart,
    "/api/limits": route_limits,
    "/api/pipeline": route_pipeline,
}
PREFIX_ROUTES = {
}
