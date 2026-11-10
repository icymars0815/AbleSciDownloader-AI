"""Server-side half of the harness (MCP / CLI) integration: /api/mcp/* endpoints.
Anything that must not be forgeable by an external harness lives here: confirm tokens, permission checks, path confinement.
Nothing here changes download/highspeed mode, AI settings, MCP permission, or reveals credentials. Confirmed MCP requests are processed by the bounded background pipeline."""
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from ablesci.system import applog
from ablesci.flow import intake
from ablesci.website import publish_policy as pp
from ablesci.papers import oa_fetch
from ablesci.flow import settings as app_settings
from ablesci.papers import zotero_bridge
from ablesci.flow.core import doi_value, now
from ablesci.flow.pipeline import stage_of, acquisition_state, gate, next_step, ai_review_task, PIPELINE_RULES_VERSION

TOKEN_TTL = 300
RENDER_MAX_PAGE = 3
ASSIST_KEEP = 5
FAIL_WORDS = ("失败", "错误", "停下", "无法", "超时", "未完成", "过期", "不符", "异常", "拒绝", "登录失效")


class PermissionDenied(ValueError):
    """Needs the App-side MCP 科研通 permission for the active account (served as HTTP 400 with reason permission_denied)."""
    reason = "permission_denied"


class ConfirmTokens:
    """Short-lived, single-use confirm tokens bound to batch + items + rewards + account. Memory only."""
    def __init__(self, ttl=TOKEN_TTL, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self.lock = threading.Lock()
        self.items = {}

    @staticmethod
    def binding(batch_id, account_id, chosen):
        return (batch_id, account_id, tuple(sorted((iid, int(reward)) for iid, reward in chosen)))

    def issue(self, binding):
        with self.lock:
            now_ = self.clock()
            self.items = {t: v for t, v in self.items.items() if v[1] > now_}
            token = secrets.token_urlsafe(24)
            self.items[token] = (binding, now_ + self.ttl)
            return token

    def consume(self, token, batch_id, account_id):
        """Return the bound (item, reward) pairs; the token is spent either way once it was found."""
        with self.lock:
            entry = self.items.pop(token, None) if isinstance(token, str) else None
        if entry is None:
            raise ValueError("确认令牌无效或已使用；请重新预览")
        binding, expires = entry
        if self.clock() > expires:
            raise ValueError("确认令牌已过期；请重新预览")
        if binding[0] != batch_id or binding[1] != account_id:
            raise ValueError("确认令牌与导入记录或账户不符；请重新预览")
        return binding[2]


def redact(text, root=None):
    """Central redaction (applog.redact) plus tighter MCP limits: raw paths, link queries and long token-like strings go too."""
    text = applog.redact(text, root)
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"'，。；]*", "<path>", text)
    text = re.sub(r"(https?://[^\s/?#]+)[^\s]*", r"\1/…", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{28,}\b", "<redacted>", text)
    return text[:300]


class McpApi:
    def __init__(self, store, accounts, pipeline, monitor, session, tokens=None):
        self.store, self.accounts, self.pipeline, self.monitor = store, accounts, pipeline, monitor
        self.session = session
        self.tokens = tokens or ConfirmTokens()

    # ---- shared ----
    def allowed(self):
        prefs = self.store.snapshot().get("preferences", {})
        return bool(prefs.get("mcp_website_actions") and prefs.get("mcp_actions_account", "default") == self.accounts.active_id)

    def need_permission(self):
        if not self.allowed():
            raise PermissionDenied("当前账户未开启 MCP 科研通操作权限，请在设置的 MCP 页中开启")

    def item(self, item_id):
        item = self.store.get(item_id) if isinstance(item_id, str) else None
        if not item or (item.get("source") != "demo" and item.get("account_id", "default") != self.accounts.active_id):
            raise ValueError("未找到该条目（或属于另一个账户）")
        return item

    def points(self):
        return self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")

    def pdf_path(self, item):
        if not item.get("pdf"):
            return None
        target = (self.store.root / item["pdf"]).resolve()
        return target if target.is_relative_to(self.store.root.resolve()) and target.is_file() else None

    def handle(self, name, body):
        method = getattr(self, "do_" + name.replace("-", "_"), None)
        if method is None or not isinstance(body, dict):
            raise ValueError("不支持该操作")
        return method(body)

    # ---- two-step confirmation ----
    def do_confirm_preview(self, body):
        chosen = intake.prepare_confirm(self.store, body.get("batch_id"), body.get("item_ids"), body.get("rewards"), self.points(), self.accounts.active_id)
        binding = ConfirmTokens.binding(body.get("batch_id"), self.accounts.active_id, [(i["id"], r) for i, r in chosen])
        token = self.tokens.issue(binding)
        rows = [dict(id=i["id"], title=i.get("title", ""), doi=i.get("doi", ""), reward=r, version=(i.get("oa_check") or {}).get("version") or "",
                     retracted=bool(intake.classify(i)["retracted"])) for i, r in chosen]
        return dict(batch_id=body["batch_id"], items=rows, estimated_points=sum(r for _, r in chosen), points_now=self.points(),
                    confirm_token=token, expires_in=self.tokens.ttl, permission=self.allowed(), session=bool(self.session()))

    def do_confirm(self, body):
        self.need_permission()
        if body.get("confirmed") is not True:
            raise ValueError("请先向用户展示预览，并在确认后带 confirmed=true")
        bound = self.tokens.consume(body.get("confirm_token"), body.get("batch_id"), self.accounts.active_id)
        ids = [iid for iid, _ in bound]
        if body.get("item_ids") is not None and sorted(body["item_ids"]) != sorted(ids):
            raise ValueError("条目与预览不一致；请重新预览")
        if isinstance(body.get("rewards"), dict) and any(dict(bound).get(k) != v for k, v in body["rewards"].items()):
            raise ValueError("悬赏与预览不一致；请重新预览")
        count = intake.confirm(self.store, body["batch_id"], ids, dict(bound), self.points(), self.session(), self.accounts.active_id)
        for iid in ids:
            self.store.update(iid, request_mcp_auto_publish=True)
        self.kick()
        return dict(ok=True, confirmed=count, estimated_points=sum(r for _, r in bound))

    def do_auto_continue(self, body):
        """Resume only an already explicitly confirmed request; no other queue item is authorised."""
        self.need_permission()
        item = self.item(body.get("id"))
        # A pre-submit interruption can be retried safely; a recorded or uncertain submission cannot.
        if (body.get("retry_unsubmitted") is True and stage_of(item) == "attention"
                and item.get("request_confirmed") is True and item.get("source") == "import"
                and not item.get("publish_action") and not item.get("publish_check")
                and not item.get("assist_url") and not item.get("publish_blocked")):
            self.store.update(item["id"], status="ready", note="已恢复尚未提交的后台求助")
            item = self.store.get(item["id"])
        stage = stage_of(item)
        if item.get("request_confirmed") is not True or stage not in ("queued", "submitted", "fulfilled", "awaiting_decision") or item.get("publish_blocked"):
            raise ValueError("仅已确认且无结果不确定或发布阻塞的求助可继续后台处理")
        self.store.update(item["id"], request_mcp_auto_publish=True)
        items = [i for i in self.store.snapshot().get("items", []) if i.get("account_id", "default") == self.accounts.active_id]
        blocked = gate(items, self.points(), item.get("request_reward"))
        self.kick()
        return dict(ok=True, id=item["id"], queued=stage == "queued", continuing=True, stage=stage, gate=blocked,
                    plan=self.plan_for(item["id"]))

    def kick(self):
        kick = getattr(self.pipeline, "kick", None)
        if callable(kick):
            kick()

    def plan_for(self, item_id):
        """What the background pipeline will do next for MCP-authorised work (same settings as tick_mcp_authorized), so the
        caller can see why an item is still waiting instead of assuming it is being published."""
        try:
            items, account, prefs, session = self.pipeline._view()
            settings = dict(prefs, pipeline_auto_publish=True, pipeline_rules_version=PIPELINE_RULES_VERSION,
                            pipeline_auto_adopt=True, rejection_mode="manual")
            planned = next_step(items, account, settings, datetime.now().astimezone(), session=session, local=False)
        except Exception:
            return None
        wait = self.pipeline.publish_limiter.wait_seconds() if planned.get("action") == "publish" else 0
        return dict(action=planned["action"], item_id=planned.get("item_id"), label=planned.get("label"), reason=planned.get("reason"),
                    requires_user=planned.get("requires_user"), for_this_item=planned.get("item_id") == item_id,
                    publish_wait_seconds=int(wait or 0), agent_busy=bool(getattr(self.pipeline.agent, "busy", False)))

    # ---- silent mode feedback: what needs the user or the AI, instead of pop-ups ----
    def do_needs_attention(self, body):
        snap = self.store.snapshot()
        prefs = snap.get("preferences", {})
        active = self.accounts.active_id
        mine = [i for i in snap["items"] if i.get("source") != "demo" and i.get("account_id", "default") == active]
        blocked = gate(mine, self.points())
        rows = []
        for i in mine:
            stage = stage_of(i)
            note = redact(i.get("note") or i.get("website_status") or "", self.store.root)
            row = None
            task = ai_review_task(i) if app_settings.ai_takeover(prefs, active) else None
            if task:
                row = dict(kind="pending_ai_review", reason=task["hint"], next=task["next"], since=task["since"],
                           minutes=task["minutes"], escalate=task["escalate"], preprint_doi=task["preprint_doi"] or None)
            elif stage == "awaiting_decision":
                v = (i.get("verification") or {}).get("status")
                row = dict(kind="decide_answer", reason="已下载应助文件，核对结果：%s" % v,
                           next="get_result(id) and show the user; then adopt_paper(id, confirmed=true) or reject_paper(...)")
            elif stage == "fulfilled":
                row = dict(kind="answer_ready", reason="已有应助文件待下载", next="download_paper(id)")
            elif stage == "oa_review":
                row = dict(kind="oa_review", reason="免费全文核对未完全匹配", next="get_result(id), then resolve_oa_review(id, keep|site_fallback, confirmed=true)")
            elif stage == "attention" and not (i.get("source") == "import" and i.get("request_confirmed") is not True and not i.get("publish_check")):
                kind = (i.get("publish_check") or {}).get("kind") or ""
                row = dict(kind="stopped:" + kind if kind else "stopped", reason=note,
                           next="requeue_request(id, reward?, confirmed=true) then confirm_batch" if i.get("source") == "import" and not i.get("publish_action") and not i.get("assist_url")
                           else "tell the user to check 科研通 我的求助; nothing is retried automatically")
            elif (i.get("oa_check") or {}).get("status") == "needs_human" and stage == "queued" and i.get("request_confirmed") is not True:
                row = dict(kind="oa_needs_human", reason="出版社页面需人工验证", next="ask the user, or let it go to 科研通 via the confirm table")
            elif i.get("publish_retry") and stage == "queued":
                row = dict(kind="retry_scheduled", reason=note, next="nothing: retried automatically at %s" % i["publish_retry"].get("next_at"))
            if row:
                rows.append(dict(row, id=i["id"], title=(i.get("title") or i.get("doi") or "")[:100], stage=stage,
                                 blocks_new_requests=blocked.get("item_id") == i["id"]))
        account = []
        monitor = self.monitor.public() if self.monitor else {}
        if monitor.get("login_required"):
            account.append(dict(kind="login_required", reason="科研通登录已失效", next="website_action('login') opens a login window for the user"))
        if not self.session():
            account.append(dict(kind="no_session", reason="未检测到科研通登录", next="website_action('login')"))
        setup = []
        if not prefs.get("oa_contact_email"):
            setup.append(dict(kind="oa_contact_email", reason="未设置联系邮箱：Unpaywall 免费全文来源不会被查询，OpenAlex/Crossref 也不在礼貌池",
                              next="ask the user to enter an e-mail in the App settings (free; only sent to OpenAlex/Crossref/Unpaywall)"))
        if not prefs.get("mcp_website_actions"):
            setup.append(dict(kind="mcp_permission", reason="MCP 科研通操作权限未开启", next="the user enables it in the App settings (MCP page)"))
        smart = app_settings.smart(prefs)
        account_now = snap.get("account_metrics", {}).get(active, {})
        if not smart["enabled"]:
            setup.append(dict(kind="smart_mode_off", reason="智能模式未开启：中途可能因权限、最低悬赏上调或同步设置而停下",
                              next="suggest the user turns on 智能模式 in the App settings (one switch; it shows exactly what it sets)"))
        else:
            over = app_settings.cap_block(prefs, account_now, 1)
            if over:
                account.append(dict(kind="daily_cap_reached", reason=over, next="tell the user; publishing resumes tomorrow or after they raise the cap"))
        zcfg = zotero_bridge.settings(prefs)
        if not zcfg["enabled"] and not zcfg["prompted"] and zotero_bridge.status()["running"]:
            setup.append(dict(kind="zotero_available", reason="检测到 Zotero 正在运行，Zotero 联动未开启（可选）：求助前按 DOI 查重，核对通过的全文自动存入 Zotero",
                              next="ask the user once whether to enable it; then set_zotero_integration(enabled=true|false, confirmed=true)"))
        elif zcfg["check"] and zotero_bridge.status()["running"] and not zotero_bridge.status()["local_api"]:
            setup.append(dict(kind="zotero_local_api_off", reason="Zotero 联动已开启，但 Zotero 本地接口未开启，无法查重（存入不受影响）",
                              next="tell the user: Zotero 设置 > 高级 > 勾选“允许其他应用程序与 Zotero 通信”"))
        if (prefs.get("publish_raise_mode") or "ask") != "auto":
            setup.append(dict(kind="raise_mode_ask", reason="最低悬赏上调设为“询问”：每次上调都会停下", next="optional: the user may switch to auto-accept with a cap in settings"))
        takeover = app_settings.ai_takeover(prefs, active)
        if takeover:   # the AI flow already covers these; only real blockers and optional extras remain
            setup = [dict(s, optional=True) if s["kind"] == "oa_contact_email" else s for s in setup if s["kind"] not in app_settings.AI_HIDDEN_SETUP]
        return dict(items=rows[:40], account=account, setup=setup, points=self.points(), gate=blocked, ai_takeover=takeover,
                    note="静默模式：AI 驱动期间不弹窗；以上内容请转告用户或按 next 处理")

    def do_zotero_settings(self, body):
        """Optional Zotero integration, on or off as the user answered (local only: no points, no website action)."""
        if body.get("confirmed") is not True or not isinstance(body.get("enabled"), bool):
            raise ValueError("请先问用户是否开启 Zotero 联动，按用户回答带 enabled 和 confirmed=true")
        self.store.update_preferences(**zotero_bridge.validate(dict(enabled=body["enabled"])))
        self.store.event("Zotero 联动已" + ("开启" if body["enabled"] else "保持关闭") + "（外部工具按用户回答设置）", kind="zotero")
        return dict(ok=True, zotero=dict(zotero_bridge.settings(self.store.snapshot().get("preferences", {})), **zotero_bridge.status(fresh=True)))

    # ---- adopt (guarded like reject: verified file + explicit confirmation) ----
    def do_adopt(self, body):
        self.need_permission()
        item = self.item(body.get("id"))
        if body.get("confirmed") is not True:
            raise ValueError("采纳会完结该求助并把积分给应助者：请先向用户说明核对结果，用户同意后带 confirmed=true")
        if (item.get("adopt_action") or {}).get("status") == "confirmed":
            # Requests confirmed through confirm_batch are adopted by the App itself when the file fully matches.
            return dict(ok=True, already_adopted=True, time=item["adopt_action"].get("time"),
                        note="已采纳：经 confirm_batch 确认的求助，文件完全匹配时由 App 自动采纳，无需再操作")
        if item.get("source") != "website" or not item.get("owned") or stage_of(item) != "awaiting_decision" or item.get("adopt_action"):
            raise ValueError("仅可采纳本人求助中已下载、等待决定且未处理过的应助")
        v = item.get("verification") or {}
        path = self.pdf_path(item)
        if v.get("status") != "identity_match" or not v.get("sha256") or path is None:
            raise ValueError("该文件未通过身份核对（DOI/标题），不能通过 MCP 采纳；请在面板中人工判断")
        if (v.get("bibliography") or {}).get("status") == "mismatch":
            raise ValueError("书目核对显示不一致，不能通过 MCP 采纳；请在面板中人工判断")
        import hashlib
        if hashlib.sha256(path.read_bytes()).hexdigest() != v["sha256"]:
            raise ValueError("PDF 在核对后已变化，请重新核对后再采纳")
        scope = {"mcp_scope": {"account_id": self.accounts.active_id, "item_id": item["id"], "auto": False,
                               "generation": self.store.snapshot().get("preferences", {}).get("mcp_action_generation", 0)}}
        self.pipeline.agent.enqueue("adopt", item["id"], scope)
        self.store.event("外部工具(MCP)经用户确认后请求采纳：" + (item.get("title") or "")[:40], item_id=item["id"], kind="adopt")
        return dict(ok=True, queued=True, id=item["id"], next="wait_for(id, until='done')")

    # ---- AI takeover: AI-read verification and the results inbox (docs/AI_TAKEOVER_20261109.md) ----
    def do_ai_verify(self, body):
        """The AI opened the downloaded PDF (render_pages / the file) and reports the DOI and title printed on it.
        App-side checks: AI takeover on, the same file (sha256), exact DOI, title >= publish_policy.TITLE_MIN against the
        public record, bibliographic check not 'mismatch'. A pass lets the background flow adopt the answer."""
        self.need_permission()
        prefs = self.store.snapshot().get("preferences", {})
        if not app_settings.ai_takeover(prefs, self.accounts.active_id):
            raise ValueError("仅在 AI 接管（智能模式 + MCP 网站操作）开启时可用")
        if body.get("confirmed") is not True:
            raise ValueError("请先亲自查看 PDF 首页，读出其中印的 DOI 与标题后带 confirmed=true 提交")
        item = self.item(body.get("id"))
        if item.get("source") != "website" or not item.get("owned") or stage_of(item) != "awaiting_decision" or item.get("adopt_action"):
            raise ValueError("仅可核对本人求助中已下载、等待决定且未处理过的应助")
        v = item.get("verification") or {}
        path = self.pdf_path(item)
        if path is None or not v.get("sha256"):
            raise ValueError("该条目没有已核对的 PDF")
        import hashlib
        if hashlib.sha256(path.read_bytes()).hexdigest() != v["sha256"]:
            raise ValueError("PDF 在核对后已变化，请重新核对")
        target = doi_value(item.get("doi") or "")
        if not target:
            raise ValueError("该条目没有 DOI，无法用 AI 核对代替人工判断")
        seen_doi = doi_value(str(body.get("doi") or ""))
        seen_title = str(body.get("title") or "")[:500]
        reference = (item.get("metadata_ref") or {}).get("title") or item.get("title") or ""
        score = pp.title_similarity(seen_title, reference)
        problems = []
        if seen_doi != target:
            problems.append("PDF 上的 DOI（%s）与目标 DOI 不一致" % (seen_doi or "未读到"))
        if score < pp.TITLE_MIN:
            problems.append("PDF 上的标题与公开记录相似度 %.0f%%，低于 %.0f%%" % (score * 100, pp.TITLE_MIN * 100))
        if (v.get("bibliography") or {}).get("status") == "mismatch":
            problems.append("本地书目核对显示是另一篇文献")
        pages = [p for p in (body.get("pages") or []) if isinstance(p, int) and not isinstance(p, bool)][:5]
        entry = dict(time=now(), status="failed" if problems else "passed", doi=seen_doi, title=seen_title, title_score=round(score, 3),
                     sha256=v["sha256"], pages=pages, problems=problems, source="external_harness")
        self.store.update(item["id"], ai_verification=entry)
        self.store.event(("AI 读取 PDF 核对通过，将自动采纳：" if not problems else "AI 读取 PDF 核对未通过（" + "；".join(problems) + "）：")
                         + (item.get("title") or "")[:40], item_id=item["id"], kind="adopt" if not problems else "needs_action")
        if not problems:
            self.kick()
        return dict(ok=True, id=item["id"], passed=not problems, problems=problems,
                    next="wait_for(id, until='done')" if not problems else "向用户说明问题，由用户决定采纳或驳回")

    def notifier(self):
        found = getattr(self.pipeline, "notifier", None)
        if found is None:
            raise ValueError("通知模块不可用")
        return found

    def do_inbox(self, body):
        """Results waiting to be told to the person. ack: list of seq numbers, or true for everything returned."""
        notifier = self.notifier()
        entries = notifier.inbox(include_acked=body.get("all") is True)
        ack = body.get("ack")
        if ack is True:
            notifier.ack([e["seq"] for e in entries])
        elif isinstance(ack, list):
            notifier.ack([s for s in ack if isinstance(s, int) and not isinstance(s, bool)])
        return dict(entries=entries[-100:], unread=len(notifier.inbox()))

    # ---- re-queue / change reward of a confirmed but unpublished request ----
    def do_requeue(self, body):
        """Undo the confirmation of a request that never reached 科研通 (optionally keeping the new reward as its default),
        so it goes through the normal two-step confirm_batch again. Never touches a submitted or uncertain request."""
        self.need_permission()
        if body.get("confirmed") is not True:
            raise ValueError("请先向用户说明将撤回该条的确认并重新排队，用户同意后带 confirmed=true")
        item = self.item(body.get("id"))
        reward = body.get("reward")
        return intake.requeue_item(self.store, item, reward, self.points(), "外部工具(MCP)经用户确认后")

    # ---- results ----
    def do_result(self, body):
        item = self.item(body.get("id"))
        path = self.pdf_path(item)
        v = item.get("verification") or {}
        b = v.get("bibliography") or {}
        retraction = v.get("retraction") or (item.get("metadata") or {}).get("retraction") or {}
        check = item.get("oa_check") or {}
        mine = [i for i in self.store.snapshot().get("items", []) if i.get("account_id", "default") == self.accounts.active_id]
        return dict(
            id=item["id"], title=item.get("title", ""), doi=item.get("doi", ""), stage=stage_of(item), has_pdf=path is not None,
            acquisition=acquisition_state(item, gate(mine, self.points(), item.get("request_reward"))),
            local_path=str(path) if path else "", original_filename=item.get("original_filename", ""), local_filename=item.get("local_filename", "") or (path.name if path else ""),
            size_bytes=path.stat().st_size if path else 0,
            verification=dict({k: v.get(k) for k in ("status", "reason", "title_score", "primary_dois", "pages")},
                              bibliography={k: b.get(k) for k in ("status", "sources", "title_status", "author_status", "abstract_status", "abstract_score", "matched_authors", "missing_authors")}),
            retraction=dict(retracted=retraction.get("retracted"), updates=(retraction.get("updates") or [])[:5], source=retraction.get("source")),
            version=check.get("version") or "", via="oa" if item.get("done_via") == "oa" else ("website" if item.get("source") == "website" else ""),
            external_assist=item.get("external_assist") or [])

    def do_render(self, body):
        item = self.item(body.get("id"))
        path = self.pdf_path(item)
        if path is None:
            raise ValueError("该条目没有 App 保存的 PDF")
        pages = body.get("pages") or [1]
        if not isinstance(pages, list) or not 0 < len(pages) <= RENDER_MAX_PAGE or any(isinstance(p, bool) or not isinstance(p, int) for p in pages):
            raise ValueError("pages 须为 1–%d 页的整数列表" % RENDER_MAX_PAGE)
        import pymupdf
        out_dir = self.store.root / "renders" / item["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        files = []
        try:
            with pymupdf.open(path) as doc:
                for number in dict.fromkeys(pages):
                    if not 1 <= number <= min(RENDER_MAX_PAGE, doc.page_count):
                        raise ValueError("只能渲染前 %d 页（该 PDF 共 %d 页）" % (RENDER_MAX_PAGE, doc.page_count))
                    rect = doc[number - 1].rect
                    zoom = min(1.6, 1400 / max(rect.width, rect.height, 1))
                    pix = doc[number - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                    target = out_dir / ("page-%d.png" % number)
                    pix.save(target)
                    files.append(dict(page=number, path=str(target), width=pix.width, height=pix.height))
        except ValueError:
            raise
        except Exception:
            raise ValueError("此 PDF 暂无法渲染，请先人工检查文件") from None
        return dict(id=item["id"], pages=files, page_count_limit=RENDER_MAX_PAGE)

    def do_assist(self, body):
        item = self.item(body.get("id"))
        fields, evidence = body.get("fields"), body.get("evidence", "")
        if not isinstance(fields, dict) or not fields or len(fields) > 30:
            raise ValueError("fields 须为 1–30 项的对象")
        clean = {}
        for key, value in fields.items():
            if not isinstance(key, str) or not 0 < len(key) <= 60:
                raise ValueError("字段名无效")
            if isinstance(value, str):
                value = value[:1000]
            elif isinstance(value, list) and len(value) <= 50 and all(isinstance(v, str) for v in value):
                value = [v[:200] for v in value]
            elif not (value is None or isinstance(value, (bool, int, float))):
                raise ValueError("字段 %s 的值类型不支持（可用文本、数字、布尔或文本列表）" % key[:20])
            clean[key] = value
        if not isinstance(evidence, str) or len(evidence) > 4000:
            raise ValueError("evidence 须为 4000 字以内文本")
        entry = dict(time=now(), source="external_harness", status="unverified_assist", fields=clean, evidence=evidence,
                     doi_valid=bool(doi_value(str(clean.get("doi", "")))) if "doi" in clean else None)
        history = (item.get("external_assist") or [])[-(ASSIST_KEEP - 1):] + [entry]
        self.store.update(item["id"], external_assist=history)   # only this key: verification / rejection / status stay untouched
        self.store.event("已保存外部工具辅助结果（仅供参考，未更改核对结论或任何决定）：" + (item.get("title") or "")[:40], item_id=item["id"], kind="assist")
        return dict(ok=True, stored=True, count=len(history), note="外部辅助结果，不影响核对结论、拒绝、采纳或发布")

    # ---- open access ----
    def do_oa_lookup(self, body):
        doi = doi_value(body.get("doi", ""))
        if not doi:
            raise ValueError("DOI 格式不正确")
        prefs = self.store.snapshot().get("preferences", {})
        res = oa_fetch.lookup_only(doi, prefs.get("oa_contact_email", ""), prefs.get("core_api_key", ""), self.store.root)
        links = [dict(url=l.get("url"), source=l.get("source"), version=l.get("version"), license=l.get("license")) for l in res.get("links") or []]
        return dict(doi=doi, status=res["status"], oa_status=res.get("oa_status"), is_oa=res.get("is_oa"), license=res.get("license"),
                    version=res.get("version"), links=links, landing_url=res.get("landing_url"), cached=res.get("cached"),
                    tried=[str(a)[:120] if not isinstance(a, dict) else {k: str(v)[:80] for k, v in a.items()} for a in (res.get("attempts") or [])[-12:]])

    def do_oa_fetch(self, body):
        doi = doi_value(body.get("doi", ""))
        if not doi:
            raise ValueError("DOI 格式不正确")
        active = self.accounts.active_id
        item = next((i for i in self.store.snapshot()["items"] if i.get("source") == "import" and i.get("doi") == doi and i.get("account_id", "default") == active), None)
        if item and item.get("pdf"):
            return dict(started=False, reason="already_downloaded", id=item["id"])
        if item and (item.get("oa_check") or {}).get("status") == "downloaded":
            return dict(started=False, reason="already_downloaded", id=item["id"])
        if item and stage_of(item) != "queued":
            return dict(started=False, reason="already_in_request_flow", id=item["id"])
        item_id = item["id"] if item else self.store.add("", doi, account_id=active, import_format="oa", request_confirmed=False)
        if (self.store.root / "oa" / (item_id + ".pdf")).exists():
            return dict(started=False, reason="target_exists", id=item_id)
        self.pipeline.oa_start(item_id)
        return dict(started=True, id=item_id, next="wait_for(id, until='oa_finished') then get_result(id)")

    # ---- error report ----
    def do_error_report(self, body):
        snap = self.store.snapshot()
        root = self.store.root
        events = [e for e in snap.get("events") or [] if any(w in e.get("message", "") for w in FAIL_WORDS)][-15:]
        active = self.accounts.active_id
        problems = []
        for i in snap["items"]:
            if i.get("source") == "demo" or i.get("account_id", "default") != active:
                continue
            if stage_of(i) == "attention" or i.get("download_failure"):
                problems.append(dict(id=i["id"], stage=stage_of(i), title=redact(i.get("title"), root)[:80], note=redact(i.get("note"), root),
                                     download_failure=redact(i.get("download_failure"), root)))
        pub = self.monitor.public()
        return dict(redacted=True, generated=now(), report_markdown=applog.build_report(self.store, extra=[
                        "来自外部工具(MCP)的请求；以下为本机最近的问题条目"] + ["%s：%s" % (p["stage"], p["title"]) for p in problems[:15]]),
                    recent_errors=[dict(time=e.get("time"), module=e.get("module"), action=e.get("action"), error_type=e.get("error_type"), message=e.get("message"))
                                   for e in self.store.log.recent_errors(10)],
                    recent_problem_events=[dict(time=e.get("time"), message=redact(e.get("message"), root)) for e in events],
                    problem_items=problems[:15],
                    monitor=dict(enabled=pub["enabled"], failures=pub["failures"], login_required=pub["login_required"], last_error=redact(getattr(self.monitor, "last_error", ""), root)),
                    note="已去除路径、邮箱、链接参数和疑似密钥；不含凭据、Cookie 或 PDF 内容")
