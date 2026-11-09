"""kytdl: the command line of AbleSciDownloader-AI (talks to the local background service on 127.0.0.1 only).
Read-only or local-only commands: nothing here publishes, adopts, rejects or changes 科研通 state itself.
Spending is only ever decided by the person: `kytdl ai on --confirmed` (AI takeover with its limits) and
`kytdl confirm ... --confirmed` (one batch / one raised reward). Show the rules or the preview to the user first.
AI workflow: `kytdl get <list>` -> report the results; `needs_points` -> ask the user -> `kytdl confirm`;
`needs_check` -> look at the PDF pages -> `kytdl verify`; later `kytdl inbox --ack` for results that arrived meanwhile.
Exit codes: 0 ok, 1 business error / something needs the person, 2 service not running, 3 permission denied."""
import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from kytdl_client import ApiError, Client, ServiceDown, WAIT_MAX_SECONDS, wait_until

OK, BUSINESS, DOWN, DENIED = 0, 1, 2, 3
BINARY_IMPORTS = (".docx", ".doc")


def clip(text, size):
    text = str(text or "")
    return text if len(text) <= size else text[:size - 1] + "…"


def render(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                print("%s%s:" % (pad, key))
                render(item, indent + 1)
            else:
                print("%s%s: %s" % (pad, key, "" if item is None else item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print("%s- %s" % (pad, "  ".join("%s=%s" % (k, clip(v, 60)) for k, v in item.items() if v not in (None, "", [], {}))))
            else:
                print("%s- %s" % (pad, item))
    else:
        print(pad + str(value))


def read_source(source, allow_text=False):
    """(kind, payload): ('text', str) or ('file', (name, bytes)). allow_text: a non-file argument is the text itself."""
    if source == "-":
        return "text", sys.stdin.read()
    path = Path(source)
    if allow_text and not path.is_file() and (len(source) > 200 or not path.suffix or "/" in source and source.lower().startswith(("10.", "doi", "http"))):
        return "text", source
    if not path.is_file():
        raise ValueError("找不到文件：" + source)
    binary = path.read_bytes()
    if path.suffix.lower() in BINARY_IMPORTS:
        return "file", (path.name, binary)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return "text", binary.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文本编码，请使用 UTF-8")


def needs_user(client, st):
    """Only what really needs the person before papers can arrive (login, AI takeover off, daily cap reached)."""
    out = []
    if not (st.get("settings") or {}).get("ai_takeover"):
        out.append(dict(kind="ai_takeover_off", reason="AI 接管未开启", next="kytdl ai on（把规则告诉用户，同意后加 --confirmed）"))
    try:
        reply = client.post("/api/mcp/needs-attention", {})
    except Exception:
        return out
    for row in reply.get("account") or []:
        out.append(dict(kind=row.get("kind"), reason=row.get("reason"), next="kytdl login" if "login" in (row.get("kind") or "") or row.get("kind") == "no_session" else row.get("next")))
    if out and out[0]["kind"] == "ai_takeover_off":
        return out   # `kytdl ai on` covers the remaining setup hints; only login matters before it
    for row in reply.get("setup") or []:
        if not row.get("optional") and row.get("kind") not in ("smart_mode_off", "mcp_permission", "oa_contact_email"):
            out.append(dict(kind=row.get("kind"), reason=row.get("reason"), next=row.get("next")))
    return out


def cmd_email(client, args):
    """Optional contact e-mail for the free full-text services (Unpaywall needs one; OpenAlex/Crossref treat it politely).
    Only sent to those public services. Ask the user for their own address first."""
    if args.address is None:
        prefs = client.state().get("preferences") or {}
        return dict(ok=True, email_set=bool(prefs.get("oa_contact_email"))), OK
    client.post("/api/pipeline", {"oa_contact_email": args.address})
    return dict(ok=True, email_set=bool(args.address)), OK


def cmd_status(client, args):
    st = client.state()
    m = st.get("monitor") or {}
    return dict(running=True, needs_user=needs_user(client, st), points=((st.get("account") or {}).get("points")), version=(st.get("about") or {}).get("version"), papers=len(st["items"]), busy=bool(st.get("busy") or st.get("waiting")),
                active_account_id=st.get("active_account_id"), mcp_website_actions=bool((st.get("preferences") or {}).get("mcp_website_actions")),
                monitor=dict(enabled=m.get("enabled"), next_sync_at=m.get("next_sync_at"), last_sync_at=m.get("last_sync_at"), login_required=m.get("login_required")),
                batches=[dict(id=b["id"], phase=b["phase"], total=b["total"]) for b in st.get("batches", [])[:5]]), OK


def cmd_import(client, args):
    kind, payload = read_source(args.source, getattr(args, "allow_text", False))
    if kind == "file":
        name, binary = payload
        parsed = client.post("/api/parse-file", {"filename": name, "base64": base64.b64encode(binary).decode()})
        reply = client.post("/api/import", {"records": parsed["records"]}) if parsed["records"] else {}
    else:
        reply = client.post("/api/import", {"text": payload}) if payload.strip() else {}
    if not reply.get("batch_id"):
        return dict(ok=False, error="没有可导入的新文献（空内容、无法识别或全部重复）"), BUSINESS
    return dict(ok=True, batch_id=reply["batch_id"], count=reply["count"], skipped_duplicates=reply["skipped_duplicates"],
                next="kytdl wait %s --until awaiting_confirmation,done" % reply["batch_id"]), OK


def cmd_search(client, args):
    reply = client.post("/api/literature-search", dict(query=args.query, cursor=args.cursor, year_from=args.year_from,
                         year_to=args.year_to, open_access=args.open_access, page_size=args.page_size, mode=args.mode, source=args.source))
    return dict(ok=True, **reply), OK


def cmd_oa(client, args):
    if args.download:
        reply = client.post("/api/mcp/oa-fetch", {"doi": args.doi})
        if not reply.get("started"):
            return dict(ok=False, error="未开始：" + reply.get("reason", ""), id=reply.get("id")), BUSINESS
        return dict(ok=True, started=True, id=reply["id"], next="kytdl wait %s --until oa_finished; kytdl result %s" % (reply["id"], reply["id"])), OK
    reply = client.post("/api/mcp/oa-lookup", {"doi": args.doi}, timeout=130)
    found = reply["status"] == "found"
    return dict(reply, ok=found), OK if found else BUSINESS


def cmd_batch(client, args):
    st = client.state()
    if not args.id:
        return dict(batches=[dict(id=b["id"], created=b["created"], phase=b["phase"], total=b["total"], counts=b["counts"]) for b in st.get("batches", [])]), OK
    batch = next((b for b in st.get("batches", []) if b["id"] == args.id), None)
    if not batch:
        return dict(ok=False, error="未找到批次 " + args.id), BUSINESS
    rows = [dict(id=r["id"], title=clip(r["title"], 70), doi=r["doi"], code=r["code"], advice=r["advice"], checked=r["checked"], reward=r.get("reward"),
                 version=r["version"], retracted=r["retracted"]) for r in batch["items"]]
    return dict(id=batch["id"], phase=batch["phase"], counts=batch["counts"], steps=[dict(label=s["label"], status=s["status"]) for s in batch["steps"]], rows=rows), OK


def cmd_events(client, args):
    rows = client.state().get("events", [])[-max(1, min(args.n, 150)):]
    return dict(events=[dict(time=e["time"], message=e["message"]) for e in rows]), OK


def cmd_monitor(client, args):
    if args.mode in ("on", "off"):
        client.post("/api/monitor", {"enabled": args.mode == "on"})
    m = client.state().get("monitor") or {}
    return {k: m.get(k) for k in ("enabled", "label", "login_required", "pending", "next_sync_at", "last_sync_at", "interval_seconds", "reason", "failures")}, OK


def cmd_wait(client, args):
    result = wait_until(client, args.id, args.until, min(args.timeout, WAIT_MAX_SECONDS))
    if not result["ok"]:
        return dict(result, error="未找到 " + args.id), BUSINESS
    return result, OK if result["reached"] else BUSINESS


def cmd_result(client, args):
    reply = client.post("/api/mcp/result", {"id": args.id})
    return dict(reply, ok=reply["has_pdf"]), OK if reply["has_pdf"] else BUSINESS


def cmd_open(client, args):
    """Open a downloaded PDF (or its folder) with the system viewer, for the person to read."""
    target = "folder" if args.folder else "pdf"
    client.post("/api/open", {"id": args.id, "target": target})
    return dict(ok=True, opened=target, id=args.id), OK


def doctor_checks():
    """Offline installation check (no service, no network): Python, packages, browser, writable data folder."""
    import importlib.util
    import runtime_paths
    rows = []
    ok_py = sys.version_info >= (3, 10)
    rows.append(dict(check="python", ok=ok_py, detail="%d.%d.%d" % sys.version_info[:3] + ("" if ok_py else "（需要 3.10 或更高）")))
    missing = [name for name in ("playwright", "pymupdf", "certifi", "olefile") if importlib.util.find_spec(name) is None]
    rows.append(dict(check="packages", ok=not missing, detail="缺少：" + "、".join(missing) + "（运行 install.cmd）" if missing else "齐全"))
    channel = runtime_paths.installed_channel()
    browser = "runtime/browsers" if runtime_paths.bundled_browser() else channel
    rows.append(dict(check="browser", ok=bool(browser), detail=browser or "没有 Chrome / Edge：运行 .venv\\Scripts\\python.exe -m playwright install chromium"))
    try:
        folder = ROOT / "data"
        folder.mkdir(exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rows.append(dict(check="data_folder", ok=True, detail=str(folder)))
    except OSError as exc:
        rows.append(dict(check="data_folder", ok=False, detail="无法写入：%s" % exc))
    return rows


def cmd_doctor(client, args):
    rows = doctor_checks()
    good = all(r["ok"] for r in rows)
    return dict(ok=good, checks=rows, next="kytdl start，然后 kytdl login（第一次）和 kytdl ai on" if good else "按 detail 处理后再运行 kytdl doctor"), OK if good else BUSINESS


def cmd_stop(client, args):
    try:
        client.state()
    except ServiceDown:
        return dict(ok=True, running=False, message="后台服务本来就没有运行"), OK
    client.post("/api/shutdown", {})
    return dict(ok=True, stopping=True, message="后台服务正在退出（进行中的网页操作会先收尾）"), OK


def cmd_autostart(client, args):
    """Start the background service when Windows starts (a shortcut in the user's Startup folder; no admin rights)."""
    if args.mode != "status":
        client.post("/api/autostart", {"enabled": args.mode == "on"})
    return dict(ok=True, autostart=bool(((client.state().get("autostart") or {}).get("enabled")))), OK


AI_DEFAULT_AUTO = 10   # same as settings.AI_DEFAULT_AUTO (the CLI does not import the app modules)
AI_RULES = ("AI 接管会设置：允许本程序代你在科研通操作（当前账户）；后台监控开启；只在有结果时提醒；"
            "每篇悬赏不超过 {auto} 积分的求助无需逐条确认（0 = 每篇都问）；每日花费上限 {cap} 积分；保留 {reserve} 积分；"
            "科研通要求提高悬赏时，超过已同意的金额就停下询问。登录/验证码、驳回、超出上限的花费仍需你本人决定。"
            "本程序是第三方开源工具，不是科研通官方程序；同意即表示已阅读 DISCLAIMER.md。")


def ai_view(st):
    sm = (st.get("settings") or {}).get("smart") or {}
    return dict(ai_takeover=bool((st.get("settings") or {}).get("ai_takeover")), smart_mode=bool(sm.get("enabled")),
                auto_confirm_max=sm.get("auto_confirm_max", 0), daily_cap=sm.get("daily_cap"), reserve=sm.get("reserve"),
                spent_today=sm.get("spent_today", 0), raise_max=sm.get("raise_max"))


def cmd_ai(client, args):
    st = client.state()
    view = ai_view(st)
    if args.mode == "status":
        return view, OK
    if args.mode == "off":
        client.post("/api/preferences", {"smart_mode": {"enabled": False}})
        return dict(ai_view(client.state()), message="AI 接管已关闭，相关设置已恢复为开启前的值"), OK
    auto = (view["auto_confirm_max"] or AI_DEFAULT_AUTO) if args.auto_max is None else args.auto_max
    cap = view["daily_cap"] if args.daily_cap is None else args.daily_cap
    rules = AI_RULES.format(auto=auto, cap=cap, reserve=view["reserve"])
    if not args.confirmed:
        return dict(ok=False, error="请先把以下规则告诉用户，用户同意后加 --confirmed 再执行", rules=rules), BUSINESS
    body = dict(enabled=True, confirmed=True, raise_max=view["raise_max"] or 50, reserve=view["reserve"] if view["reserve"] is not None else 50,
                daily_cap=cap or 200, auto_confirm_max=auto)
    client.post("/api/preferences", {"smart_mode": body})
    client.post("/api/preferences", {"disclaimer_ack": True})
    return dict(ai_view(client.state()), rules=rules), OK


ROOT = Path(__file__).resolve().parent
GET_WAIT_DEFAULT = 300
DECISION_GRACE = 90     # an answer that fully matches is adopted by the background flow within a pass or two


def ensure_running(client, wait=20):
    """Start the service in the background (no window) when it is not running; its output goes to data/logs."""
    try:
        return client.state()
    except ServiceDown:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with (logs / ("server-%s.stdout.log" % stamp)).open("ab") as out, (logs / ("server-%s.stderr.log" % stamp)).open("ab") as err:
        process = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=ROOT, creationflags=flags,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            return client.state()
        except ServiceDown:
            if process.poll() is not None:
                raise ServiceDown("后台服务没能启动（端口 8769 被占用或已有实例），请查看 data/logs 中最新的 server 日志") from None
            continue
    raise ServiceDown("后台服务启动超时，请查看 data/logs 中的 server 日志")


def cmd_start(client, args):
    st = ensure_running(client)
    return dict(ok=True, running=True, ai_takeover=bool((st.get("settings") or {}).get("ai_takeover"))), OK


def get_row(item, batch, first_decision, now, auto=0):
    """One paper's result for `kytdl get`: (status, done?). auto: the AI-takeover no-ask amount (0 = off)."""
    stage = item.get("stage") or "queued"
    if stage == "done":
        return "done", True
    if stage == "oa_review":
        return "needs_check", True
    if stage == "attention" or (item.get("oa_check") or {}).get("status") == "needs_human" and not item.get("request_confirmed"):
        return "needs_person", True
    if stage == "awaiting_decision":
        return ("needs_check", True) if now - first_decision.setdefault(item["id"], now) >= DECISION_GRACE else ("adopting", False)
    if stage == "queued" and not item.get("request_confirmed"):
        if batch and batch.get("phase") == "awaiting_confirmation":
            if auto and not batch.get("ai_auto") and (item.get("request_reward") or 10) <= auto:
                return "screening", False   # the background confirms it within a pass (it was reported too early before)
            return "needs_points", True
        if batch and batch.get("phase") == "done":
            return "not_requested", True
        return "screening", False
    return {"submitted": "waiting_answer", "fulfilled": "downloading"}.get(stage, stage), False


def cmd_get(client, args):
    st = ensure_running(client)
    reply, code = cmd_import(client, args)
    if code != OK:
        # Everything already in the library: report those papers instead of an empty import.
        import re
        kind, payload = read_source(args.source, True)
        text = payload if kind == "text" else ""
        dois = {d.lower().rstrip(".,;)") for d in re.findall(r"10\.\d{4,9}/[^\s,;]+", text)}
        known = [i for i in st["items"] if (i.get("doi") or "").lower() in dois]
        if not known:
            return reply, code
        rows = [dict(id=i["id"], title=clip(i.get("title") or i.get("doi"), 80), doi=i.get("doi"), status=get_row(i, None, {}, 0)[0],
                     note=clip(i.get("note"), 120)) for i in known]
        return dict(ok=True, already_in_library=True, papers=rows,
                    next=["这些文献已在库中：done 的用 kytdl result <id> 取路径；其余告诉用户当前状态"]), OK
    batch_id, wait = reply["batch_id"], max(5, min(args.wait, WAIT_MAX_SECONDS))
    ids = None
    first_decision = {}
    settings_now = st.get("settings") or {}
    auto = ((settings_now.get("smart") or {}).get("auto_confirm_max") or 0) if settings_now.get("ai_takeover") else 0
    start = time.monotonic()
    while True:
        st = client.state()
        batch = next((b for b in st.get("batches") or [] if b["id"] == batch_id), None)
        ids = ids or [r["id"] for r in (batch or {}).get("items", [])] or [i["id"] for i in st["items"] if i.get("batch_id") == batch_id]
        items = {i["id"]: i for i in st["items"] if i["id"] in ids}
        now = time.monotonic()
        rows = [(items[i], get_row(items[i], batch, first_decision, now, auto)) for i in ids if i in items]
        if rows and all(done for _, (_, done) in rows) or now - start >= wait:
            break
        time.sleep(3)
    out = []
    for item, (status, _) in rows:
        row = dict(id=item["id"], title=clip(item.get("title") or item.get("doi"), 80), doi=item.get("doi", ""), status=status,
                   reward=item.get("request_reward"), note=clip(item.get("note"), 120))
        if status == "done":
            result = client.post("/api/mcp/result", {"id": item["id"]})
            row.update(path=result.get("local_path", ""), verification=(result.get("verification") or {}).get("status"))
        out.append(row)
    need = [r for r in out if r["status"] in ("needs_points", "needs_check", "needs_person")]
    hints = []
    if any(r["status"] == "needs_points" for r in out):
        hints.append("needs_points：先运行 kytdl confirm %s 看预览，告诉用户要花多少积分；同意后加 --confirmed" % batch_id)
    if any(r["status"] == "needs_check" for r in out):
        hints.append("needs_check：kytdl pages <id> 看首页图，读出 DOI 与标题。科研通文件用 kytdl verify <id> --doi ... --title ... --confirmed；"
                     "免费全文文件把读到的内容告诉用户，用户同意后 kytdl resolve <id> keep --confirmed（保留）或 request --confirmed（改走科研通）")
    if any(r["status"] in ("waiting_answer", "downloading", "adopting", "screening", "publishing") for r in out):
        hints.append("仍在进行的条目会在后台继续；结果出来后用 kytdl inbox 查看并告诉用户")
    return dict(ok=not need, batch_id=batch_id, skipped_duplicates=reply.get("skipped_duplicates", 0), papers=out, next=hints), (BUSINESS if need else OK)


def parse_rewards(values, ids):
    rewards = {}
    for value in values or []:
        if "=" in value:
            key, number = value.split("=", 1)
            rewards[key.strip()] = int(number)
        else:
            rewards.update({i: int(value) for i in ids or []})
            rewards["*"] = int(value)
    return rewards


def cmd_confirm(client, args):
    """Preview (default) or, with --confirmed, confirm the 科研通 requests of a batch; a paper id whose publish stopped
    on a raised minimum reward is re-queued with --reward first."""
    st = client.state()
    batch_id, ids = args.target, [i for i in (args.ids or "").split(",") if i] or None
    paper = next((i for i in st["items"] if i["id"] == args.target), None)
    if paper is not None:
        if not args.reward:
            return dict(ok=False, error="请用 --reward 指定新的悬赏（例如科研通要求的最低悬赏）"), BUSINESS
        reward = parse_rewards(args.reward, [paper["id"]]).get(paper["id"])
        if not args.confirmed:
            return dict(ok=False, error="将撤回该条并以 %s 积分重新求助：告诉用户后加 --confirmed" % reward, id=paper["id"], reward=reward), BUSINESS
        if paper.get("request_confirmed") or (paper.get("stage") == "attention"):
            client.post("/api/mcp/requeue", {"id": paper["id"], "reward": reward, "confirmed": True})
            st = client.state()
            paper = next(i for i in st["items"] if i["id"] == paper["id"])
        batch_id, ids = paper.get("batch_id"), [paper["id"]]
        if not batch_id:
            return dict(ok=False, error="该条目不在任何导入清单中，无法确认"), BUSINESS
    rewards = parse_rewards(args.reward, ids) if args.reward else None
    if rewards and "*" in rewards and ids is None:
        rewards = None   # a single number without --ids applies through the default reward of each row
    preview = client.post("/api/mcp/confirm-preview", {"batch_id": batch_id, "item_ids": ids, "rewards": rewards})
    rows = [dict(id=r["id"], title=clip(r["title"] or r["doi"], 70), reward=r["reward"]) for r in preview["items"]]
    if not args.confirmed:
        return dict(ok=False, error="预览：共 %d 篇、%d 积分（现有 %s）。告诉用户，同意后加 --confirmed" % (len(rows), preview["estimated_points"], preview.get("points_now")),
                    batch_id=batch_id, papers=rows, estimated_points=preview["estimated_points"]), BUSINESS
    done = client.post("/api/mcp/confirm", {"batch_id": batch_id, "item_ids": [r["id"] for r in rows],
                                            "rewards": {r["id"]: r["reward"] for r in rows},
                                            "confirm_token": preview["confirm_token"], "confirmed": True})
    return dict(ok=True, batch_id=batch_id, confirmed=done.get("confirmed"), estimated_points=done.get("estimated_points"),
                next="kytdl wait %s --until done 或稍后 kytdl inbox" % (ids[0] if ids and len(ids) == 1 else batch_id)), OK


def cmd_inbox(client, args):
    reply = client.post("/api/mcp/inbox", {"ack": True if args.ack else None, "all": args.all})
    rows = [dict(seq=e["seq"], time=e["time"][5:16], kind=e["kind"], message=e["message"], id=e.get("item_id") or e.get("batch_id"))
            for e in reply["entries"]]
    return dict(ok=True, results=rows, unread=reply["unread"]), OK


def cmd_verify(client, args):
    pages = [int(p) for p in (args.pages or "1").split(",") if p.strip()]
    reply = client.post("/api/mcp/ai-verify", {"id": args.id, "doi": args.doi, "title": args.title, "pages": pages, "confirmed": args.confirmed})
    if reply.get("passed"):
        reply = dict(reply, next="后台会自动采纳；kytdl wait %s --until done 或稍后 kytdl inbox" % args.id)
    return dict(reply, ok=reply.get("passed", False)), OK if reply.get("passed") else BUSINESS


def cmd_pages(client, args):
    """Render PDF pages to PNG so the AI can look at them before `kytdl verify`."""
    pages = [int(p) for p in (args.pages or "1").split(",") if p.strip()]
    reply = client.post("/api/mcp/render", {"id": args.id, "pages": pages})
    return dict(ok=True, id=reply["id"], images=[dict(page=p["page"], path=p["path"]) for p in reply["pages"]]), OK


def cmd_login(client, args):
    """Open the 科研通 login window (the only window the CLI ever opens: captcha / WeChat need the person)."""
    client.post("/api/browser", {"action": "login", "use_saved": True})
    return dict(ok=True, opened=True, next="请用户在弹出的窗口中完成登录（验证码 / 微信），然后运行 kytdl sync"), OK


def cmd_sync(client, args):
    """Read-only sync of the own-requests list now (instead of waiting for the next scheduled check)."""
    client.post("/api/monitor", {"sync_now": True})
    return dict(ok=True, started=True, next="几秒后用 kytdl inbox 或 kytdl status 查看"), OK


def cmd_resolve(client, args):
    """A free full-text file under review: keep it as the result, or send the paper to 科研通 (the file stays on disk).
    Only after the user agreed (the AI showed what the first page says); request spends nothing by itself."""
    if not args.confirmed:
        return dict(ok=False, error="先把首页读到的 DOI / 标题告诉用户；用户同意后加 --confirmed", id=args.id, decision=args.decision), BUSINESS
    client.post("/api/oa-accept" if args.decision == "keep" else "/api/oa-discard", {"id": args.id, "confirmed": True})
    nxt = "kytdl result %s 取路径" % args.id if args.decision == "keep" else "回到求助队列：kytdl confirm <batch_id> 预览积分"
    return dict(ok=True, id=args.id, decision=args.decision, next=nxt), OK


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="kytdl", description="Local 科研通 literature helper CLI. Never changes 科研通 state itself; "
                                "`ai on --confirmed` lets the app's background flow spend points within the shown limits.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common]).set_defaults(run=cmd_status)
    s = sub.add_parser("import", parents=[common], help="import references (file or - for stdin); starts free-source screening only")
    s.add_argument("source")
    s.set_defaults(run=cmd_import)
    s = sub.add_parser("search", parents=[common], help="search OpenAlex (or PubMed with --source pubmed); no download or import")
    s.add_argument("query")
    s.add_argument("--cursor", default="*")
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)
    s.add_argument("--open-access", action="store_true")
    s.add_argument("--page-size", type=int, default=50)
    s.add_argument("--mode", choices=["keyword", "exact", "semantic"], default="keyword")
    s.add_argument("--source", choices=["openalex", "pubmed"], default="openalex")
    s.set_defaults(run=cmd_search)
    s = sub.add_parser("oa", parents=[common], help="look up (or with --download fetch) the open-access PDF of a DOI")
    s.add_argument("doi")
    s.add_argument("--download", action="store_true")
    s.set_defaults(run=cmd_oa)
    s = sub.add_parser("batch", parents=[common])
    s.add_argument("id", nargs="?", default="")
    s.set_defaults(run=cmd_batch)
    s = sub.add_parser("events", parents=[common])
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(run=cmd_events)
    s = sub.add_parser("monitor", parents=[common])
    s.add_argument("mode", nargs="?", choices=["on", "off", "status"], default="status")
    s.set_defaults(run=cmd_monitor)
    s = sub.add_parser("wait", parents=[common], help="long poll until a batch/paper reaches a state")
    s.add_argument("id")
    s.add_argument("--until", required=True, help="comma list, e.g. awaiting_confirmation,done or downloaded")
    s.add_argument("--timeout", type=int, default=60, help="seconds, max 600")
    s.set_defaults(run=cmd_wait)
    s = sub.add_parser("open", parents=[common], help="open a downloaded PDF (or --folder) for the person")
    s.add_argument("id")
    s.add_argument("--folder", action="store_true")
    s.set_defaults(run=cmd_open)
    s = sub.add_parser("email", parents=[common], help="optional contact e-mail for free full-text lookups (Unpaywall); '' clears it")
    s.add_argument("address", nargs="?", default=None)
    s.set_defaults(run=cmd_email)
    s = sub.add_parser("doctor", parents=[common], help="check the installation (Python, packages, browser, data folder)")
    s.set_defaults(run=cmd_doctor)
    s = sub.add_parser("stop", parents=[common], help="stop the background service")
    s.set_defaults(run=cmd_stop)
    s = sub.add_parser("autostart", parents=[common], help="start the background service with Windows (on/off/status)")
    s.add_argument("mode", nargs="?", default="status", choices=("status", "on", "off"))
    s.set_defaults(run=cmd_autostart)
    s = sub.add_parser("start", parents=[common], help="start the background service (no window) if it is not running")
    s.set_defaults(run=cmd_start)
    s = sub.add_parser("get", parents=[common], help="AI workflow: import a list (file, - for stdin, or DOIs/text) and wait for results")
    s.add_argument("source", help="file, '-' for stdin, or the references / DOIs themselves")
    s.add_argument("--wait", type=int, default=GET_WAIT_DEFAULT, help="seconds to wait for results, max 600 (the rest continues in the background)")
    s.set_defaults(run=cmd_get, allow_text=True)
    s = sub.add_parser("confirm", parents=[common], help="preview, or with --confirmed confirm, the 科研通 requests of a batch (or re-request one paper with --reward)")
    s.add_argument("target", help="batch id, or a paper id that stopped on a raised minimum reward")
    s.add_argument("--ids", help="comma list of paper ids (default: the recommended rows)")
    s.add_argument("--reward", action="append", help="N for all chosen papers, or ID=N; repeatable")
    s.add_argument("--confirmed", action="store_true", help="the user has seen the preview and agreed")
    s.set_defaults(run=cmd_confirm)
    s = sub.add_parser("inbox", parents=[common], help="results not yet told to the user (--ack marks them as told)")
    s.add_argument("--ack", action="store_true")
    s.add_argument("--all", action="store_true", help="include results already told")
    s.set_defaults(run=cmd_inbox)
    s = sub.add_parser("verify", parents=[common], help="report the DOI and title you read on the downloaded PDF (AI takeover); a pass lets the app adopt")
    s.add_argument("id")
    s.add_argument("--doi", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--pages", default="1", help="pages you looked at, e.g. 1,2")
    s.add_argument("--confirmed", action="store_true", help="you really looked at the PDF")
    s.set_defaults(run=cmd_verify)
    s = sub.add_parser("pages", parents=[common], help="render PDF pages to PNG files (look at them before kytdl verify)")
    s.add_argument("id")
    s.add_argument("--pages", default="1", help="e.g. 1,2 (first 3 pages at most)")
    s.set_defaults(run=cmd_pages)
    s = sub.add_parser("resolve", parents=[common], help="free full-text file under review: keep it, or request it on 科研通 instead")
    s.add_argument("id")
    s.add_argument("decision", choices=("keep", "request"))
    s.add_argument("--confirmed", action="store_true")
    s.set_defaults(run=cmd_resolve)
    s = sub.add_parser("login", parents=[common], help="open the 科研通 login window for the user (captcha / WeChat)")
    s.set_defaults(run=cmd_login)
    s = sub.add_parser("sync", parents=[common], help="read-only sync of the own-requests list now")
    s.set_defaults(run=cmd_sync)
    s = sub.add_parser("ai", parents=[common], help="AI takeover: status | on [--auto-max N] [--daily-cap N] --confirmed | off")
    s.add_argument("mode", nargs="?", choices=["status", "on", "off"], default="status")
    s.add_argument("--auto-max", type=int, help="papers with a reward up to N points are requested without asking (0 = always ask)")
    s.add_argument("--daily-cap", type=int, help="daily spending cap in points")
    s.add_argument("--confirmed", action="store_true", help="the user has seen and agreed to the rules")
    s.set_defaults(run=cmd_ai)
    s = sub.add_parser("result", parents=[common])
    s.add_argument("id")
    s.set_defaults(run=cmd_result)
    return p


def main(argv=None, client=None):
    args = parser().parse_args(argv)
    try:
        data, code = args.run(client or Client(), args)
    except ServiceDown as exc:
        data, code = dict(ok=False, error=str(exc), reason="service_not_running"), DOWN
    except ApiError as exc:
        data, code = dict(ok=False, error=str(exc), reason=exc.reason or "error"), DENIED if exc.permission else BUSINESS
    except (ValueError, KeyError, OSError) as exc:
        data, code = dict(ok=False, error=str(exc) or type(exc).__name__), BUSINESS
    if args.json:
        data.setdefault("ok", code == OK)
        print(json_text(dict(data, exit=code)))
    elif code != OK and data.get("error"):
        print(data["error"], file=sys.stderr)
        extra = {k: v for k, v in data.items() if k not in ("error", "ok")}
        if extra:
            render(extra)
    else:
        render({k: v for k, v in data.items() if k != "ok"})
    return code


def safe_output():
    """Plain-text output never crashes on a character the console encoding lacks (it becomes '?')."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def json_text(data):
    """JSON in the console's own encoding (callers decode with it, e.g. GBK here); a character that encoding cannot
    hold is written as a JSON \\u escape, so the output always parses. Real 2026-11-09: a title with 'ł' crashed it."""
    text = json.dumps(data, ensure_ascii=False)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    out = []
    for ch in text:
        try:
            ch.encode(encoding)
            out.append(ch)
        except (UnicodeEncodeError, LookupError):
            out.append(json.dumps(ch)[1:-1])
    return "".join(out)


if __name__ == "__main__":
    safe_output()
    sys.exit(main())
