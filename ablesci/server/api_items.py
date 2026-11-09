"""Item, browser-action and MCP routes (POST /api/...), moved unchanged from app.do_POST in refactor stage 1.
Each route(c, h, body, path, origin): c holds the server objects, h is the request handler; a route that does not
respond itself gets {"ok": true} (see api_routes.dispatch)."""
import time

from ablesci.server.app_common import now_stamp
from ablesci.flow.pipeline import stage_of
from ablesci.website.rejection_policy import rejection_decision, manual_reason
from ablesci.flow import intake


def route_mcp(c, h, body, path, origin):
    store = c.store
    mcp_api = c.mcp_api
    store.mcp_last_call = time.time()   # silent mode: an AI harness is driving (notifications.Notifier.ai_driven)
    return h.respond(200, mcp_api.handle(path[len("/api/mcp/"):], body))


def route_pipeline_step(c, h, body, path, origin):
    pipeline = c.pipeline
    planned = pipeline.plan()[3]
    if planned["requires_user"] or planned["action"] in pipeline.PASSIVE:
        planned = pipeline.local_plan() or planned   # local OA work never waits on 科研通-side steps
    if planned["requires_user"]:
        raise ValueError(planned["reason"] or "该步骤需手动处理")
    if not pipeline.execute(planned):
        return h.respond(200, {"ok": True, "executed": False, "step": planned})
    return h.respond(200, {"ok": True, "executed": True, "step": planned})


def route_browser(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    agent = c.agent
    pipeline = c.pipeline
    mcp_api = c.mcp_api
    action = body.get("action")
    if body.get("mcp_call") is True:
        store.mcp_last_call = time.time()
        prefs = store.snapshot().get("preferences", {})
        mcp_api.need_permission()
    if action not in {"login", "sync", "signin", "prepare", "review", "download", "reject", "publish", "adopt"}:
        raise ValueError("演示数据不支持该操作")
    if action in {"prepare", "review", "download", "reject", "publish", "adopt"} and not store.get(body.get("id")):
        raise ValueError("未找到任务")
    if action == "download" and (store.get(body["id"]) or {}).get("site_file_deleted") and not (store.get(body["id"]) or {}).get("pdf"):
        raise ValueError("科研通文件已删除，无法下载")
    if action == "adopt" and (body.get("confirmed") is not True or stage_of(store.get(body["id"])) != "awaiting_decision"):
        raise ValueError("仅支持已下载核对、待处理的本人应助，请确认后采纳")
    if action == "publish" and (body.get("confirmed") is not True or stage_of(store.get(body["id"])) != "queued"):
        raise ValueError("仅支持尚未发布的本地求助。发布将扣除积分，请确认后再发布")
    if action == "reject":
        item = store.get(body.get("id"))
        decision = rejection_decision(item, store.snapshot().get("preferences", {}))
        category, reason = manual_reason(body.get("category"), body.get("detail"))
        if not decision["eligible"] or body.get("confirmed") is not True:
            raise ValueError("仅支持本人未完结、已下载核对的应助，请确认后驳回")
        store.update(item["id"], manual_reject_reason=reason, manual_reject_category=category)
    credentials = None
    if action == "login" and body.get("use_saved"):
        credentials = accounts.vault().load()
    if action == "login" and (body.get("email") or body.get("password")):
        if not body.get("email") or not body.get("password"):
            raise ValueError("自动登录需同时填写邮箱和密码，或留空后在浏览器中自行登录")
        if len(body["email"]) > 320 or len(body["password"]) > 1000:
            raise ValueError("邮箱或密码过长")
        credentials = {"email": body["email"], "password": body["password"]}
        if body.get("save_credentials") is True:
            accounts.vault().save(credentials["email"], credentials["password"])
            store.event("邮箱和密码已使用 Windows 用户加密保存在本机；未保存明文。")
    if action == "download":
        if agent.busy or not agent.commands.empty() or agent.download_busy():
            raise ValueError("已有任务运行，请等待完成或停止后再操作")
        # A person pressing 下载/重试 starts fresh: failure counters and the retry schedule are cleared.
        store.update(body["id"], normal_failures=0, highspeed_failures=0, highspeed_clicks=0, download_retry=None,
                     **({"status": "ready"} if (store.get(body["id"]) or {}).get("status") == "retrying" else {}))
    if body.get("mcp_call") is True:
        credentials = {"mcp_scope": {"account_id": accounts.active_id,
                                      "item_id": body.get("id"), "auto": False,
                                      "generation": store.snapshot().get("preferences", {}).get("mcp_action_generation", 0)}}
    agent.enqueue(action, body.get("id"), credentials)
    if action == "publish":
        pipeline.publish_limiter.record()   # manual publishes count towards the hourly cap


def route_item_ack(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    item = store.get(body.get("id"))
    if not item or item.get("status") != "attention" or not item.get("publish_check"):
        raise ValueError("该条目不处于“发布已停止”状态")
    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
        raise ValueError("该条目属于另一个账户")
    publish_state = (item.get("publish_action") or {}).get("status")
    if publish_state in ("attempted", "uncertain") or (publish_state == "confirmed" and not item.get("assist_url")):
        raise ValueError("发布结果不确定，请先在科研通核实，软件不会自动清除")
    if publish_state == "confirmed":
        store.update(item["id"], status="published", note="已发布，等待应助。")
        store.event("已清除提示，该求助已在科研通发布：" + (item.get("title") or "")[:40])
    else:
        store.update(item["id"], status="ready", note="已由用户接手，不再自动发布")
        store.event("已标记为用户接手，不再自动发布：" + (item.get("title") or "")[:40])


def route_item_requeue(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    item = store.get(body.get("id"))
    if not item or (item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id):
        raise ValueError("未找到该条目（或属于另一个账户）")
    if body.get("confirmed") is not True:
        raise ValueError("请确认撤回该条的求助确认并重新排队")
    points = store.snapshot().get("account_metrics", {}).get(accounts.active_id, {}).get("points")
    return h.respond(200, intake.requeue_item(store, item, body.get("reward"), points))


def route_item_reset_publish(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    agent = c.agent
    pipeline = c.pipeline
    monitor = c.monitor
    item = store.get(body.get("id"))
    if not item or (item.get("publish_action") or {}).get("status") not in ("attempted", "uncertain"):
        raise ValueError("该条目没有“结果不确定”的发布记录")
    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
        raise ValueError("该条目属于另一个账户")
    if body.get("confirmed") is not True:
        raise ValueError("请先在科研通“我的求助”核实，再确认")
    title = (item.get("title") or "")[:40]
    if path == "/api/item/reset-publish":
        store.update(item["id"], publish_action=None, publish_blocked=False, status="ready", note="已核实科研通上无此求助，已重置，可重新发布")
        store.event("已重置发布记录（核实科研通上无此求助）：" + title, item_id=item["id"], kind="publish")
    else:
        store.update(item["id"], publish_action=dict(item["publish_action"], status="uncertain", user_verified=now_stamp()),
                     note="已核实科研通上已发出，将在下次同步时按标题对应")
        store.event("已记录：《%s》经核实已在科研通发出，将在下次同步时自动对应。" % title, item_id=item["id"], kind="publish")
        try:
            agent.enqueue("sync")
        except ValueError:
            pass    # busy: the regular monitor/pipeline sync will pick it up


def route_item_handover_done(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    agent = c.agent
    item = store.get(body.get("id"))
    if not item or not item.get("handover") or item.get("status") != "attention":
        raise ValueError("该条目不处于“科研通弹窗遮挡、待手动处理”状态")
    if item.get("source") != "demo" and item.get("account_id", "default") != accounts.active_id:
        raise ValueError("该条目属于另一个账户")
    title = (item.get("title") or "")[:40]
    if path == "/api/item/handover-done":
        store.update(item["id"], handover=dict(item["handover"], waiting_sync=True, done_claimed=now_stamp()),
                     note="已在科研通处理，等待同步确认结果")
        store.event("已记录：《%s》已在科研通处理，正在同步确认。" % title, item_id=item["id"], kind="sync")
        try:
            agent.enqueue("sync")
        except ValueError:
            pass
    else:
        store.update(item["id"], status="downloaded", handover=None, handover_last=dict(item["handover"], retried=now_stamp()),
                     note="已恢复为待处理，可重新采纳或驳回")
        store.event("已恢复为待处理，可重新尝试：" + title, item_id=item["id"], kind="adopt")


ROUTES = {
    "/api/pipeline/step": route_pipeline_step,
    "/api/browser": route_browser,
    "/api/item/ack": route_item_ack,
    "/api/item/requeue": route_item_requeue,
    "/api/item/reset-publish": route_item_reset_publish,
    "/api/item/confirm-published": route_item_reset_publish,
    "/api/item/handover-done": route_item_handover_done,
    "/api/item/handover-retry": route_item_handover_done,
}
PREFIX_ROUTES = {
    "/api/mcp/": route_mcp,
}
