"""Import, parsing, open-access, batch, upload and verification routes (POST /api/...), moved unchanged from app.do_POST in refactor stage 1.
Each route(c, h, body, path, origin): c holds the server objects, h is the request handler; a route that does not
respond itself gets {"ok": true} (see api_routes.dispatch)."""
from datetime import datetime
import base64

from ablesci.papers.import_files import import_file
from ablesci.papers.metadata import verify_with_metadata, fetch_metadata
from ablesci.papers.paste_parser import parse_references, make_record, unparsed_segments
from ablesci.flow.pipeline import stage_of, has_session
from ablesci.flow import intake
from ablesci.papers import oa_fetch


def route_import(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    pipeline = c.pipeline
    if "records" in body:
        if not isinstance(body["records"], list) or len(body["records"]) > 200:
            raise ValueError("每次最多导入 200 篇")
        if any(not isinstance(r, dict) or not isinstance(r.get("authors", []), list)
               or any(r.get(k) is not None and not isinstance(r.get(k), str) for k in ("title", "doi"))
               for r in body["records"]):
            raise ValueError("文献记录须为对象，作者须为列表")
        source = "AI整理" if body.get("ai_assisted") is True else "text"
        records = [make_record(r.get("title") or "", [str(a)[:200] for a in r.get("authors", [])][:300], r.get("doi") or "", source) for r in body["records"]]
        if any(not r["title"] and not r["doi"] for r in records):
            raise ValueError("每条文献至少需要有效标题或 DOI")
    else:
        records = parse_references(body.get("text", ""))
    batch_id = intake.new_batch_id()
    for record in records:
        store.add(record["title"], record["doi"], account_id=accounts.active_id, authors=record["authors"], import_format=record["format"],
                  batch_id=batch_id, request_confirmed=False,
                  submitted_reference=dict(title=record["title"], doi=record["doi"], authors=list(record["authors"])))
    new_ids = [i["id"] for i in store.snapshot()["items"] if i.get("batch_id") == batch_id]
    count = len(new_ids)
    if new_ids:
        intake.create_batch(store, batch_id, accounts.active_id, new_ids,
                            origin="mcp" if body.get("mcp_call") is True else "app")
    store.event(f"已导入 {count} 条新文献，重复项已跳过。尚未发布求助。")
    if new_ids and pipeline.auto_screen:
        pipeline.screen_step()
    return h.respond(200, {"ok": True, "batch_id": batch_id if new_ids else None, "count": count, "item_ids": new_ids,
                              "skipped_duplicates": max(0, len(records) - count)})


def route_parse(c, h, body, path, origin):
    text = body.get("text", "")
    return h.respond(200, {"records": parse_references(text), "ai_used": False, "unparsed": unparsed_segments(text)[:20]})


def route_parse_file(c, h, body, path, origin):
    binary = base64.b64decode(body.get("base64", ""), validate=True)
    return h.respond(200, {"records": import_file(str(body.get("filename", "")), binary), "ai_used": False})


def route_oa_fetch(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    pipeline = c.pipeline
    if ("lookup_only" in body and not isinstance(body["lookup_only"], bool)) or ("url" in body and not isinstance(body["url"], str)):
        raise ValueError("参数无效")
    item = store.get(body.get("id"))
    if not item or item.get("source") != "import" or item.get("account_id", "default") != accounts.active_id:
        raise ValueError("仅支持当前账户中本地导入的文献")
    if path == "/api/oa-accept":
        if body.get("confirmed") is not True:
            raise ValueError("请确认保留该免费全文文件为完成")
        pipeline.oa_accept(item["id"])
    elif path == "/api/oa-discard":
        pipeline.oa_discard(item["id"])
    elif path == "/api/oa-skip":
        pipeline.oa_skip(item["id"])
    elif path == "/api/oa-human":
        check = item.get("oa_check") or {}
        if check.get("status") != "needs_human" or not str(check.get("manual_url", "")).startswith(("http://", "https://")):
            raise ValueError("该条目没有待人工验证的免费全文页面")
        pipeline.oa_start(item["id"], human=True)
    else:
        if not item.get("doi") or item.get("pdf") or stage_of(item) != "queued":
            raise ValueError("仅支持尚未发布、没有 PDF 且带 DOI 的本地条目")
        old = item.get("oa_check")
        if old and old.get("status") == "downloaded":
            raise ValueError("已有免费全文文件，不会覆盖")
        if (store.root / "oa" / (item["id"] + ".pdf")).exists():
            raise ValueError("免费全文目标文件已存在，不会覆盖")
        options = {}
        if body.get("lookup_only"):
            options["lookup_only"] = True
        if body.get("url"):
            if not any(l.get("url") == body["url"] for l in (old or {}).get("links") or []):
                raise ValueError("该链接不在已找到的免费全文来源中")
            options["only_url"] = body["url"]
        pipeline.oa_start(item["id"], **options)


def route_batch_confirm(c, h, body, path, origin):
    store = c.store
    accounts = c.accounts
    if not isinstance(body.get("batch_id"), str):
        raise ValueError("导入记录参数无效")
    snap_now = store.snapshot()
    account_now = snap_now.get("account_metrics", {}).get(accounts.active_id, {})
    if path == "/api/batch/confirm":
        mine = [i for i in snap_now["items"] if i.get("account_id", "default") == accounts.active_id]
        count = intake.confirm(store, body["batch_id"], body.get("item_ids"), body.get("rewards"), account_now.get("points"),
                               has_session(account_now, mine, accounts.has_cookie_file()), accounts.active_id)
        return h.respond(200, {"ok": True, "confirmed": count})
    if path == "/api/batch/defer":
        intake.mark(store, body["batch_id"], accounts.active_id, prompted=True, deferred_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        store.event("已选择稍后确认求助清单，可从右下角消息面板重新打开。", kind="intake", batch_id=body["batch_id"], action="open-batch")
    else:
        intake.mark(store, body["batch_id"], accounts.active_id, prompted=True)


def route_verify(c, h, body, path, origin):
    store = c.store
    handle_verification = c.handle_verification
    item = store.get(body.get("id"))
    if not item or not item.get("pdf"):
        raise ValueError("请先为该条选择本地 PDF 或下载文件")
    result = verify_with_metadata(store.root / item["pdf"], item.get("expected_title", item["title"]), item.get("expected_doi", item["doi"]), store.root, lookup=item["source"] != "demo",
                                  email=store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
    store.update(item["id"], verification=result, status="oa_review" if item.get("status") == "oa_review" else "checked")
    handle_verification(item["id"], allow_website=body.get("allow_website_actions") is not False)
    store.event("已完成本地 PDF 核对，未操作科研通。")


def route_metadata(c, h, body, path, origin):
    store = c.store
    item = store.get(body.get("id"))
    if not item or item["source"] == "demo" or not item.get("doi"):
        raise ValueError("请先提供真实 DOI；演示数据不查询在线数据库")
    record = fetch_metadata(item["doi"], store.root, store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
    store.update(item["id"], metadata=record)
    store.event("已按 DOI 查询公开书目并保存在本机，未发送 PDF 或账号信息。")


def route_upload(c, h, body, path, origin):
    store = c.store
    handle_verification = c.handle_verification
    item = store.get(body.get("id"))
    if not item or item.get("pdf"):
        raise ValueError("任务不存在或已有文件，不会覆盖已保存的 PDF")
    binary = base64.b64decode(body["base64"], validate=True)
    if len(binary) > 25_000_000 or not binary.startswith(b"%PDF-"):
        raise ValueError("仅支持 25 MB 以内的 PDF 文件")
    folder = store.root / "uploads"
    folder.mkdir(exist_ok=True)
    target = folder / (item["id"] + ".pdf")
    # A leftover from an earlier upload that no item references is simply replaced (the item has no PDF).
    referenced = any(i.get("pdf") and (store.root / i["pdf"]).resolve() == target.resolve() for i in store.snapshot()["items"])
    if target.exists() and referenced:
        raise ValueError("该文件已被其他记录使用，不会覆盖")
    with target.open("wb") as output:
        output.write(binary)
    result = verify_with_metadata(target, item["title"], item["doi"], store.root, lookup=item["source"] != "demo",
                                  email=store.snapshot().get("preferences", {}).get("oa_contact_email", ""))
    store.update(item["id"], pdf=str(target.relative_to(store.root)), verification=result, status="checked")
    handle_verification(item["id"])
    store.event("已保存本地 PDF 并完成核对，文件未上传至任何网站。")


def route_oa_cache_clear(c, h, body, path, origin):
    store = c.store
    removed = oa_fetch.clear_oa_cache(store.root)
    store.event("已清除免费全文查找缓存（%d 条），下次将重新查询。" % removed)


ROUTES = {
    "/api/import": route_import,
    "/api/parse": route_parse,
    "/api/parse-file": route_parse_file,
    "/api/oa-fetch": route_oa_fetch,
    "/api/oa-human": route_oa_fetch,
    "/api/oa-skip": route_oa_fetch,
    "/api/oa-accept": route_oa_fetch,
    "/api/oa-discard": route_oa_fetch,
    "/api/batch/confirm": route_batch_confirm,
    "/api/batch/defer": route_batch_confirm,
    "/api/batch/seen": route_batch_confirm,
    "/api/verify": route_verify,
    "/api/metadata": route_metadata,
    "/api/upload": route_upload,
    "/api/oa-cache-clear": route_oa_cache_clear,
}
PREFIX_ROUTES = {
}
