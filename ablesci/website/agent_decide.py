"""Adopting or rejecting a received file on the website (BrowserAgent mixin, moved unchanged from browser_agent.py in refactor stage 3)."""
import re
from ablesci.flow.core import now
from ablesci.website.rejection_policy import rejection_decision, CATEGORY_KEYS
from ablesci.flow.pipeline import stage_of
import hashlib
from ablesci.website.agent_common import BASE, _count, PreconditionError


class DecisionMixin:
    def reject_auto(self, item_id):
        self.reject(item_id, automatic=True)

    def reject(self, item_id, automatic=False):
        item = self.store.get(item_id)
        if not item:
            raise PreconditionError("未找到应助")
        self.assert_account(item)
        decision = rejection_decision(item, self.store.snapshot().get("preferences", {}))
        if not decision["eligible"] or item.get("reject_action"):
            raise PreconditionError("该条不满足驳回条件，或已提交过驳回。请在科研通查看，不自动重试")
        if automatic and decision["action"] != "auto_reject":
            raise PreconditionError("当前规则不允许自动驳回")
        if automatic and decision.get("category") != "file_mismatch":
            raise PreconditionError("自动驳回仅限“文件不一致”（首页 DOI 明确为另一篇文献）")
        category = "file_mismatch" if automatic else item.get("manual_reject_category", "")
        reason = decision["reason"] if automatic else item.get("manual_reject_reason", "")
        if category not in CATEGORY_KEYS:
            raise PreconditionError("请先选择科研通规定的 4 类驳回理由之一")
        if not reason:
            raise PreconditionError("请填写具体说明")
        path = (self.store.root / item["pdf"]).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != item["verification"]["sha256"]:
            raise PreconditionError("PDF 已变化，请重新核对后再驳回")
        self.goto(item["assist_url"])
        self.dismiss_info_layers()
        # Work only inside the timeline entry for the exact downloaded file.
        relative = item["download_file_url"].replace(BASE, "", 1)
        link = self.page.locator('a[href="' + relative + '"]').last
        if not link.count():
            raise ValueError("科研通上的应助文件已变化，未驳回")
        card = link.locator("xpath=ancestor::li[1]")
        if card.count() != 1 or any(s in card.inner_text() for s in ("已采纳", "已驳回", "已拒绝")):
            raise ValueError("应助已处理或科研通页面结构已变化，未驳回")
        controls = card.locator('button.btn-handle-file[data-type="reject"]')
        if controls.count() != 1:
            controls = card.get_by_role("button", name=re.compile(r"^\W*(?:驳回|拒绝)(?:应助|文件|文献)?\W*$")).or_(card.get_by_role("link", name=re.compile(r"^\W*(?:驳回|拒绝)(?:应助|文件|文献)?\W*$")))
        if controls.count() != 1:
            raise ValueError("无法唯一确认该文件的驳回入口，未提交。请打开求助详情手动处理")
        if self.stop.is_set():
            raise ValueError("已停止，未驳回")
        self.assert_mcp_authority()
        # From here on, a timeout means unknown outcome; never click again automatically.
        self.clear_blocking_layers()
        self.click_or_handover(item_id, controls, "驳回")
        self.store.update(item_id, reject_action=dict(status="attempted", automatic=automatic, time=now(), reason=reason, category=category))
        self.page.wait_for_timeout(800)
        dialog = self.page.locator('dialog[open], [role="dialog"]:visible, .layui-layer:visible')
        if dialog.count() == 1:
            reason_box = dialog.locator("textarea")
            if reason_box.count() != 1:
                raise ValueError("未识别科研通的驳回表单，已停止。请查看求助详情，不自动重试")
            reason_box.fill(reason)
            # layui buttons are <a> without href (no link role): match by text inside .layui-layer-btn.
            submit = dialog.locator(".layui-layer-btn a").filter(has_text=re.compile(r"^\s*(?:确定|确认|确认驳回|确认拒绝|提交)\s*$"))
            if submit.count() != 1:
                submit = dialog.get_by_role("button", name=re.compile(r"^(?:确定|确认|确认驳回|确认拒绝|提交)$")).or_(dialog.get_by_role("link", name=re.compile(r"^(?:确定|确认|确认驳回|确认拒绝|提交)$")))
            if submit.count() != 1 or self.stop.is_set():
                raise ValueError("未确认驳回提交按钮，或已停止，请手动查看")
            if automatic and rejection_decision(self.store.get(item_id), self.store.snapshot().get("preferences", {}))["mode"] != "auto":
                raise ValueError("自动驳回已关闭，未提交")
            self.assert_mcp_authority()
            submit.click()
        # Confirmation must belong to this exact file entry, never the live site feed.
        try:
            card.get_by_text(re.compile(r"已(?:驳回|拒绝)")).first.wait_for(timeout=15000)
        except Exception:
            self.store.update(item_id, reject_action=dict(status="uncertain", automatic=automatic, time=now(), reason=reason, category=category))
            raise ValueError("驳回结果未确认。请在科研通查看，不自动重复提交")
        done = dict(status="confirmed", automatic=automatic, time=now(), reason=reason, category=category)
        # Keep the rejected file on disk and in the record; free the item for the next answer.
        archived = dict(pdf=item["pdf"], verification=item["verification"], download_file_url=item["download_file_url"],
                        reject_action=done, rejected_at=done["time"])
        self.store.update(item_id, status="rejected", reject_action=None, rejected_at=done["time"], pdf="", verification=None,
                          download_file_url="", rejection=None, rejected_files=list(item.get("rejected_files", [])) + [archived])
        self.store.event("已确认驳回该应助文件，本地 PDF 与核对记录已归档保留。", item_id=item_id, kind="reject", action="focus")

    def card_for(self, item):
        self.goto(item["assist_url"])
        self.dismiss_info_layers()
        relative = item["download_file_url"].replace(BASE, "", 1)
        link = self.page.locator('a[href="' + relative + '"]').last
        if not link.count():
            raise ValueError("科研通上的应助文件已变化，未操作")
        card = link.locator("xpath=ancestor::li[1]")
        if card.count() != 1 or any(s in card.inner_text() for s in ("已采纳", "已驳回", "已拒绝")):
            raise ValueError("应助已处理或科研通页面结构已变化，未操作")
        # Real card text (2026-10-20): "超过24小时未处理，系统将默认采纳" -> keep it as this answer's handling deadline.
        hours = re.search(r"超过\s*(\d+)\s*小时未处理[，,]?\s*系统将默认采纳", card.inner_text())
        if hours and not item.get("auto_adopt_hours"):
            self.store.update(item["id"], auto_adopt_hours=int(hours.group(1)))
        return card

    def confirm_dialog(self, pattern, max_dialogs=2, log=None):
        """Confirmation layer(s) after a click (adopt may show two in a row). layui buttons are <a> without href,
        so match by text inside .layui-layer-btn; press only one unambiguous non-cancel button per layer."""
        handled = 0
        for _ in range(max_dialogs * 6):
            self.assert_mcp_authority()
            if handled >= max_dialogs:
                return handled
            layers = [(l, t) for l, t in self.visible_layers() if _count(l.locator(".layui-layer-btn a"))]
            if not layers:
                if handled:
                    return handled
                self.page.wait_for_timeout(500)
                continue
            layer, text = layers[-1]
            if log is not None:
                log.append(" ".join(text.split())[:200])
            ok = layer.locator(".layui-layer-btn a").filter(has_text=pattern)
            if ok.count() != 1 or self.stop.is_set():
                raise ValueError("未能唯一确认科研通的确认按钮，或已停止。请查看求助详情，不自动重试")
            ok.first.click()
            handled += 1
            self.page.wait_for_timeout(800)
        return handled

    def adopt(self, item_id):
        item = self.store.get(item_id)
        if not item or stage_of(item) != "awaiting_decision" or item.get("adopt_action"):
            raise PreconditionError("仅可采纳已下载核对、等待决定且未处理过的本人应助")
        self.assert_account(item)
        path = (self.store.root / item["pdf"]).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != (item.get("verification") or {}).get("sha256"):
            raise PreconditionError("PDF 已变化，请重新核对后再采纳")
        card = self.card_for(item)
        self.clear_blocking_layers()
        controls = card.locator('button.btn-handle-file[data-type="accept"]')
        if controls.count() != 1:
            name = re.compile(r"^\W*采纳(?:应助|文件|文献)?\W*$")
            controls = card.get_by_role("button", name=name).or_(card.get_by_role("link", name=name))
        if controls.count() != 1:
            raise ValueError("无法唯一确认该文件的采纳入口，未提交。请打开求助详情手动处理")
        if self.stop.is_set():
            raise ValueError("已停止，未采纳")
        self.assert_mcp_authority()
        # From here on a timeout means unknown outcome; never click again automatically.
        try:
            self.click_or_handover(item_id, controls, "采纳")
        except ValueError:
            self.store.update(item_id, adopt_action=None)
            raise
        self.store.update(item_id, adopt_action=dict(status="attempted", time=now()))
        try:
            seen = []
            self.confirm_dialog(re.compile(r"^\s*(?:确定|确认|确认采纳|采纳|提交)\s*$"), log=seen)
            card.get_by_text(re.compile(r"已采纳")).first.wait_for(timeout=15000)
        except Exception:
            self.store.update(item_id, adopt_action=dict(status="uncertain", time=now(), dialogs=locals().get("seen", [])))
            raise ValueError("采纳结果未确认。请在科研通查看，不自动重复提交")
        auto = bool((getattr(self, "_mcp_scope", None) or {}).get("auto"))   # adopted by the background flow, not a click
        self.store.update(item_id, status="adopted", adopt_action=dict(status="confirmed", time=now(), auto=auto),
                          note="已采纳（文件已核对通过）。" if not auto else (
                              "已自动采纳（DOI、标题、作者、摘要与书目均吻合）。" if (item.get("verification") or {}).get("status") == "identity_match"
                              else "已自动采纳（AI 读取 PDF 后确认 DOI 与标题一致）。"))
        self.store.event("已确认采纳该应助文件。", item_id=item_id, kind="adopt", action="focus")

