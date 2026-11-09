"""Account, AI, report, demo, panel and service routes (POST /api/...), moved unchanged from app.do_POST in refactor stage 1.
Each route(c, h, body, path, origin): c holds the server objects, h is the request handler; a route that does not
respond itself gets {"ok": true} (see api_routes.dispatch)."""
import hashlib

from ablesci.flow.core import seed_demo
from ablesci.server import app_info
from ablesci.system import applog


def route_forget_credentials(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    accounts.vault().forget()
    store.event("已删除保存的邮箱和密码，浏览器登录状态仍保留。")


def route_accounts_add(c, h, body, path, origin):
    accounts = c.accounts
    agent = c.agent
    if agent.busy or not agent.commands.empty(): raise ValueError("请等待当前操作完成后再切换账户")
    account_id = accounts.add(body.get("label", ""))
    agent.enqueue("switch_account", account_id)


def route_accounts_switch(c, h, body, path, origin):
    accounts = c.accounts
    agent = c.agent
    account_id = body.get("account_id", "")
    accounts.account_root(account_id)
    agent.enqueue("switch_account", account_id)


def route_ai_settings(c, h, body, path, origin):
    store = c.store
    ai = c.ai
    if not isinstance(body.get("enabled"), bool): raise ValueError("请选择是否开启 AI 辅助")
    ai.configure(body["enabled"], str(body.get("api_key", "")), str(body.get("model", "")), body.get("consent") is True,
                 provider=body.get("provider") or None, base_url=str(body.get("base_url", "")), storage=body.get("storage") or None)
    store.update_preferences(ai_settings=ai.settings_dict())
    store.event("AI 辅助已" + ("开启，每次发送前需预览并确认。" if body["enabled"] else "关闭。"))


def route_ai_test(c, h, body, path, origin):
    ai = c.ai
    result = ai.test_connection(str(body.get("api_key", "")), body.get("provider") or None, str(body.get("base_url", "")), str(body.get("model", "")))
    return h.respond(200, dict(result, note="仅查询模型列表，未发送文献内容"))


def route_ai_forget_key(c, h, body, path, origin):
    store = c.store
    ai = c.ai
    ai.forget_key()
    store.update_preferences(ai_settings=ai.settings_dict())
    store.event("已删除保存的 AI 密钥并关闭 AI。")


def route_ai_organize(c, h, body, path, origin):
    store = c.store
    ai = c.ai
    result = ai.organize_text(body.get("segments"), body.get("consent"))
    store.event("AI 整理结果已生成，确认后才会导入，未发送其他内容。")
    return h.respond(200, result)


def route_logs_recent(c, h, body, path, origin):
    store = c.store
    return h.respond(200, {"errors": store.log.recent_errors(20)})


def route_report_build(c, h, body, path, origin):
    store = c.store
    report_cache = c.report_cache
    ids = body.get("error_ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(i, str) for i in ids)):
        raise ValueError("所选错误无效")
    text = applog.build_report(store, ids or None)
    report_cache.clear()
    report_cache.update(id=hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], text=text)
    return h.respond(200, {"report_id": report_cache["id"], "text": text})


def route_report_analyze(c, h, body, path, origin):
    store = c.store
    ai = c.ai
    report_cache = c.report_cache
    if not report_cache or body.get("report_id") != report_cache.get("id"):
        raise ValueError("请先生成并预览报告")
    result = ai.analyze_report(report_cache["text"], body.get("consent"))
    store.event("AI 已分析预览的脱敏报告，结果仅供参考，未执行任何操作。")
    return h.respond(200, result)


def route_report_issue(c, h, body, path, origin):
    url = applog.issue_url(app_info.REPO_URL, "科研通下载器 问题反馈", "请描述遇到的问题，并附上已保存的错误报告（.md）。提交前请确认报告中不含个人信息。")
    if not url: raise ValueError("仓库地址未设置，无法打开 GitHub Issue")
    return h.respond(200, {"url": url})


def route_ai_recognize(c, h, body, path, origin):
    store = c.store
    ai = c.ai
    item = store.get(body.get("id"))
    if not item or not item.get("pdf"): raise ValueError("请先保存本地 PDF")
    target = (store.root / item["pdf"]).resolve()
    if not target.is_relative_to(store.root.resolve()): raise ValueError("文件路径无效")
    result = ai.recognize(target, body.get("pages"), body.get("consent"))
    store.update(item["id"], ai_assistance=result)
    store.event("AI 识别结果已保存在本机，需人工复核，未改变本地核对结果。")


def route_demo(c, h, body, path, origin):
    store = c.store
    seed_demo(store)


def route_open(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    item = store.get(body.get("id"))
    if not item or not item.get("pdf") or body.get("target") not in ("pdf", "folder"):
        raise ValueError("没有可打开的本地 PDF")
    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
        raise ValueError("该条目属于另一个账户")
    target = (store.root / item["pdf"]).resolve()
    if not target.is_relative_to(store.root.resolve()) or not target.is_file():
        raise ValueError("文件不存在或不在数据目录内，无法打开")
    if body["target"] == "pdf" and target.suffix.lower() != ".pdf":
        raise ValueError("仅可直接打开 .pdf 文件。该文件不是 PDF，请使用“打开所在文件夹”查看")
    h.server.open_path(str(target.parent if body["target"] == "folder" else target))


def route_stop(c, h, body, path, origin):
    store = c.store
    agent = c.agent
    pipeline = c.pipeline
    store.update_preferences(pipeline_enabled=False)
    pipeline.pause_mcp()
    agent.request_stop()
    store.event("已停止自动流程及 MCP 后台续办，并请求停止当前操作。已完成的文件和确认记录保留；继续求助须主动恢复。")


def route_shutdown(c, h, body, path, origin):
    shutdown_service = c.shutdown_service
    shutdown_service(h.server)


ROUTES = {
    "/api/forget-credentials": route_forget_credentials,
    "/api/accounts/add": route_accounts_add,
    "/api/accounts/switch": route_accounts_switch,
    "/api/ai/settings": route_ai_settings,
    "/api/ai/test": route_ai_test,
    "/api/ai/forget-key": route_ai_forget_key,
    "/api/ai/organize": route_ai_organize,
    "/api/logs/recent": route_logs_recent,
    "/api/report/build": route_report_build,
    "/api/report/analyze": route_report_analyze,
    "/api/report/issue": route_report_issue,
    "/api/ai/recognize": route_ai_recognize,
    "/api/demo": route_demo,
    "/api/open": route_open,
    "/api/stop": route_stop,
    "/api/shutdown": route_shutdown,
}
PREFIX_ROUTES = {
}
