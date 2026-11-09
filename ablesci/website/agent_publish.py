"""Publishing a request (single paper): lookup layer, reward, confirm prompts, stop/retry (BrowserAgent mixin, moved unchanged from browser_agent.py in refactor stage 3)."""
import re
import time
from datetime import datetime, timedelta
from ablesci.flow import intake
from ablesci.website import publish_policy as pp
from ablesci.flow import settings as app_settings
from ablesci.flow.core import doi_value, now
from ablesci.papers.metadata import fetch_metadata
from ablesci.flow.pipeline import stage_of, gate, tier, item_reward
from ablesci.website.agent_common import PreconditionError


class PublishMixin:
    def publish_stop(self, item_id, kind, reason, check=None, pause=False, notify_kind="needs_action"):
        """Nothing is (or no longer can be) submitted: item -> attention with what was read and a screenshot, then stop."""
        if kind == "gate":
            # The site wants an answered request handled first: not an error. Nothing was submitted, the item stays queued;
            # the stored balance is treated as stale and the pipeline waits until the next sync (pipeline.next_step "site_gate").
            self.store.update_account(account_id=self.accounts.active_id, site_gate=now(), points=None)
            self.store.event("求助未提交：" + reason, item_id=item_id, kind="publish", action="focus")
            self.notify_user(notify_kind, f"publish:{item_id}:{kind}", "需先处理应助", reason)
            raise PreconditionError(reason)
        shot = ""
        page = getattr(self, "page", None)
        if page is not None:
            try:
                folder = self.store.root / "screenshots"
                folder.mkdir(exist_ok=True)
                target = folder / f"publish-{item_id}-{datetime.now():%H%M%S}.png"
                page.screenshot(path=str(target), mask=[page.locator("input")])
                shot = str(target.relative_to(self.store.root))
            except Exception:
                shot = ""
        self.store.update(item_id, status="attention", note=reason, publish_blocked=True,
                          publish_check=dict(check or {}, kind=kind, reason=reason, time=now(), screenshot=shot))
        self.store.event("求助未提交：" + reason, item_id=item_id, kind="publish", action="focus")
        if pause:
            self.store.update_preferences(pipeline_enabled=False)
            self.store.event("已暂停自动流程：科研通提示账号受限或次数已达上限。请先在科研通确认原因。", item_id=item_id, kind="publish", action="focus")
        self.notify_user(notify_kind, f"publish:{item_id}:{kind}", "求助需处理" if notify_kind == "needs_action" else "最低悬赏上调，待确认", reason)
        raise ValueError(reason)

    LOOKUP_RETRY_MINUTES = (1, 5, 10)

    def publish_retry_later(self, item_id, check):
        """The lookup layer came back empty ('undefined'): nothing was filled or submitted. Keep the item queued and let the
        pipeline try again later (1 / 5 / 10 min); only after the last retry does it stop in attention."""
        item = self.store.get(item_id) or {}
        count = int((item.get("publish_retry") or {}).get("count") or 0) + 1
        if count > len(self.LOOKUP_RETRY_MINUTES):
            self.store.update(item_id, publish_retry=dict(item.get("publish_retry") or {}, exhausted=True))
            self.publish_stop(item_id, "lookup_undefined", "科研通智能提取连续 %d 次返回空结果（undefined），未提交。可稍后用“重新排队”再试，或在科研通手动求助"
                              % (count - 1), check)
        delay = self.LOOKUP_RETRY_MINUTES[count - 1]
        next_at = (datetime.now().astimezone() + timedelta(minutes=delay)).isoformat(timespec="seconds")
        self.store.update(item_id, status="ready", publish_blocked=False,
                          publish_retry=dict(count=count, next_at=next_at, kind="lookup_undefined", time=now()),
                          publish_check=None, note="科研通智能提取返回空结果（undefined），未提交；%d 分钟后自动重试（第 %d 次）" % (delay, count))
        raise PreconditionError("科研通智能提取返回空结果（undefined），未提交；%d 分钟后自动重试" % delay)

    def reference_title(self, item, doi, email):
        """The public (OpenAlex/Crossref) title this item is compared against; '' when none can be had."""
        for key in ("metadata_ref", "metadata"):
            title = (item.get(key) or {}).get("title")
            if title:
                return str(title)
        try:
            record = fetch_metadata(doi, self.store.root, email)
            if record.get("status") == "available" and record.get("title"):
                return str(record["title"])
        except Exception:
            pass
        return ""

    def wait_lookup_layer(self, item_id, check):
        """Wait (<= 50 s) for the 智能提取 result layer; known failure wording stops immediately."""
        deadline = time.monotonic() + 50
        for _ in range(50):
            for layer, text in self.visible_layers():
                failure = pp.classify_failure(text)
                if failure:
                    self.publish_stop(item_id, failure["kind"], failure["message"], dict(check, layer_text=text[:500]), pause=failure["pause"])
                # Title queries show 相似度; an exact DOI query shows only 标题/DOI/期刊/作者 + three buttons (real page, 2026-10-20).
                if "相似度" in text or ("DOI" in text and "标题" in text and "信息正确" in text):
                    return layer, text
                if re.search(r"未(找到|查到|检索到)|没有找到|查询失败|识别失败|无法识别", text):
                    self.publish_stop(item_id, "lookup_failed", "科研通未能识别该 DOI：" + " ".join(text.split())[:80], dict(check, layer_text=text[:500]))
            self.page.wait_for_timeout(1000)
            if time.monotonic() > deadline:
                break
        self.publish_stop(item_id, "lookup_timeout", "科研通在 50 秒内未返回识别结果，未提交。请打开求助页面查看", check)

    def confirm_quick_tip(self):
        """「快捷提示」layer.confirm: press 直接提交 (only reached after every check passed). Returns an error text or ''."""
        for _ in range(4):
            for layer, text in self.visible_layers():
                if "快捷提示" in text:
                    button = layer.locator(".layui-layer-btn a", has_text="直接提交")
                    if button.count() != 1:
                        return "出现“快捷提示”，但未找到唯一的“直接提交”按钮，未提交"
                    button.first.click()
                    return ""
            self.page.wait_for_timeout(500)
        return ""

    def publish(self, item_id):
        item = self.store.get(item_id)
        if not item or item["source"] == "demo":
            raise PreconditionError("请先导入真实 DOI 或标题。演示数据不能提交到科研通")
        self.assert_account(item)
        if stage_of(item) != "queued" or item.get("publish_action"):
            raise PreconditionError("仅可发布尚未发布、未尝试过的本地求助")
        prefs = self.store.snapshot().get("preferences", {})
        email = prefs.get("oa_contact_email", "")
        doi = doi_value(item.get("doi", ""))
        check = dict(expected_doi=doi, item_title=item.get("title", ""))
        if not doi:
            self.publish_stop(item_id, "no_doi", "无 DOI 的文献不支持自动求助（科研通的智能提取需要 DOI）。请补充 DOI，或在科研通页面自行求助。", check)
        ref_title = self.reference_title(item, doi, email)
        check["reference_title"] = ref_title
        block = pp.auto_publish_block([item.get("title", ""), ref_title], doi)
        if block:
            self.publish_stop(item_id, "not_auto", block, check)

        def recheck():
            snapshot = self.store.snapshot()
            active = self.accounts.active_id
            mine = [i for i in snapshot["items"] if i.get("source") != "demo" and i.get("account_id", "default") == active]
            stored = snapshot.get("account_metrics", {}).get(active, {}).get("points")
            blocked = gate(mine, stored, item_reward(item, prefs, stored))
            if blocked["blocked"]:
                raise PreconditionError(blocked["reason"])
            return stored
        stored_before = recheck()
        self.goto("/assist/create", interactive=False)
        try:
            points = self.read_points()
        except Exception:
            points = None
        if points is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=points)
        else:
            points = self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")
        recheck()
        if tier(stored_before, item_reward(item, prefs, stored_before)) == "batch" and tier(points, item_reward(item, prefs, points)) == "single":
            raise PreconditionError("科研通页面显示当前积分 %s，发布本条（悬赏 %d）后将低于 500，与之前记录的积分不符；未提交。已更新积分，请按逐条求助规则重新规划"
                                    % (points, item_reward(item, prefs, points)))
        page = self.page
        if pp.page_restricted(page.locator("body").inner_text(timeout=5000)):
            failure = pp.classify_failure("受限")
            self.publish_stop(item_id, failure["kind"], failure["message"], check, pause=True)
        base = item.get("request_reward") or intake.default_reward(prefs, points)
        reserve = pp.raise_settings(prefs)["reserve"]
        if isinstance(points, int) and not isinstance(points, bool) and base > points - reserve:
            self.publish_stop(item_id, "reserve_points", "悬赏 %d 积分将使剩余积分低于保留值（现有 %d，保留 %d），未提交。请调整悬赏或保留积分" % (base, points, reserve), check)

        # 1) 智能提取：只用 DOI
        page.locator("#onekey").fill(doi)
        with self.throttled():
            page.locator("button.onekey-search").click()
        layer, text = self.wait_lookup_layer(item_id, check)
        scraped = pp.parse_lookup_text(text)
        check.update(doi=scraped["doi"], title=scraped["title"], similarity=scraped["similarity"], journal=scraped["journal"],
                     type=scraped["type"], min_reward=scraped["min_reward"], layer_text=scraped["text"][:600])
        seen = list(self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("min_reward_seen") or [])
        if isinstance(scraped["min_reward"], int):
            self.store.update_account(account_id=self.accounts.active_id, min_reward_seen=(seen + [scraped["min_reward"]])[-10:])
        if pp.min_reward_jump(scraped["min_reward"], seen):
            # A jump alone is ambiguous (a 10-point journal followed by a 50-point publisher is normal), so warn only;
            # the reward-raise setting still decides whether to pay, and explicit "受限" page text still pauses.
            self.store.event("提示：页面要求的最低悬赏变为 %d 积分（此前约 %d）。可能是该出版社要求较高，也可能是账号受限，建议在科研通查看。"
                             % (scraped["min_reward"], min(seen[-5:])), kind="needs_action", item_id=item_id)
            check["min_reward_jump"] = True

        if pp.lookup_undefined(scraped):
            self.publish_retry_later(item_id, check)

        # 2) 二次核对：DOI 精确一致、标题与公开记录 >= 0.90、相似度 >= 50、类型为期刊论文
        problems = pp.evaluate_lookup(doi, ref_title, scraped)
        if problems:
            self.publish_stop(item_id, "lookup_mismatch", "弹窗中的识别结果与该文献不符，已停止：" + "；".join(problems), dict(check, problems=problems))
        block = pp.auto_publish_block([scraped["title"]], scraped["doi"])
        if block:
            self.publish_stop(item_id, "not_auto", block, check)

        # 3) 填入表单：只点「信息正确，还需修改」（只填表、不发布）；绝不点「信息正确，直接发布」（会跳过本地核对直接提交）
        buttons = layer.locator("a, button, span").filter(has_text=re.compile(r"^\s*信息正确[，,]\s*还需修改\s*$"))
        if buttons.count() != 1:
            buttons = layer.locator(".layui-layer-btn a", has_text=re.compile(r"^\s*(?:确定|确认|使用|填入|填充|采用)"))
        if buttons.count() != 1:
            self.publish_stop(item_id, "lookup_confirm", "未找到唯一的“信息正确，还需修改”按钮，未提交。请在页面中手动操作", check)
        buttons.first.click()
        page.wait_for_timeout(800)
        got_doi = doi_value(page.locator("#Assist-doi").input_value())
        got_title = page.locator("#Assist-title").input_value()
        if got_doi != doi or pp.title_similarity(got_title, ref_title) < pp.TITLE_MIN:
            self.publish_stop(item_id, "form_mismatch", "表单中的 DOI 或标题与该文献不一致，未提交", dict(check, form_doi=got_doi, form_title=got_title))
        # DOI lookups don't show type / minimum in the popup: read them from the filled form instead.
        try:
            type_value = page.locator('input[name="Assist[type]"]:checked').get_attribute("value")
        except Exception:
            type_value = None
        if type_value != "1" and not scraped.get("type"):
            self.publish_stop(item_id, "not_auto", "表单中的文献类型不是“期刊论文”，不自动求助", dict(check, form_type=type_value))
        if scraped["min_reward"] is None:
            try:
                low = re.search(r"\d+", page.locator("#point-low").first.inner_text(timeout=3000))
                scraped["min_reward"] = int(low.group()) if low else None
            except Exception:
                pass
        if scraped["min_reward"] is None:
            self.publish_stop(item_id, "lookup_mismatch", "未读取到页面要求的最低悬赏，未提交", check)
        check["min_reward"] = scraped["min_reward"]

        # 4) 悬赏 = max(用户悬赏, 页面最低值)，被提高时按设置处理
        reward, minimum = base, scraped["min_reward"]
        if minimum > base:
            ok, why = pp.raise_decision(minimum, points, app_settings.raise_settings_for(prefs, self.accounts.active_id, base))
            if not ok:
                self.publish_stop(item_id, "min_reward_raised", why + "（当前设置的悬赏为 %d 积分）" % base, dict(check, needed=minimum, base=base), notify_kind="min_bounty")
            reward = minimum
        if isinstance(points, int) and reward > points:
            self.publish_stop(item_id, "points_short", "积分不足：需要悬赏 %d 积分，现有 %d" % (reward, points), dict(check, needed=reward))

        over = app_settings.cap_block(prefs, self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}), reward)
        if over:
            raise PreconditionError("未提交：" + over)
        page.locator("#Assist-point").fill(str(reward))
        if str(page.locator("#Assist-point").input_value()).strip() != str(reward):
            self.publish_stop(item_id, "form_mismatch", "悬赏积分未能填入表单，未提交", check)
        try:
            close_at = str(page.locator("#Assist-close_at").input_value())[:40]
        except Exception:
            close_at = ""
        check.update(reward=reward, close_at=close_at)

        # 5) 提交
        submit = page.locator("#form-submit-btn")
        if submit.count() != 1:
            self.publish_stop(item_id, "no_submit", "无法唯一确认发布入口，未提交。请在已填写的表单中手动发布", check)
        if self.stop.is_set():
            raise ValueError("已停止，未发布")
        self.assert_mcp_authority()

        def action(status):
            return dict(status=status, point=reward, close_at=close_at, time=now())
        self.store.update(item_id, publish_action=action("attempted"), publish_check=dict(check, kind="submitting", time=now()))
        try:
            with self.throttled():
                with page.expect_response(lambda r: "check-before-assist-create" in r.url, timeout=30000) as info:
                    submit.click()
                    tip_error = self.confirm_quick_tip()
                    if tip_error:
                        raise _TipError(tip_error)
            data = info.value.json()
        except _TipError as exc:
            self.store.update(item_id, publish_action=None)
            self.publish_stop(item_id, "quick_tip", str(exc), check)
        except Exception:
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认。请在科研通“我的求助”查看，不自动重复提交")
        data = data if isinstance(data, dict) else {}
        code, message = data.get("code"), str(data.get("msg") or "")
        if code == 1:
            self.store.update(item_id, publish_action=None)
            failure = pp.classify_failure(message)
            if failure:
                self.publish_stop(item_id, failure["kind"], failure["message"], dict(check, site_message=message[:200]), pause=failure["pause"])
            self.publish_stop(item_id, "site_refused", "科研通未接受本次提交：" + (message[:120] or "未说明原因") + "（未重试）", dict(check, site_message=message[:200]))
        if code == 2:
            reward = self.handle_confirm_prompt(item_id, message, reward, points, prefs, check)
        elif code != 0:
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认。请在科研通“我的求助”查看，不自动重复提交")
        if not self.wait_published():
            self.store.update(item_id, publish_action=action("uncertain"))
            raise ValueError("发布结果未确认，下次同步将按标题自动对应，不自动重复提交")
        # Adopt the new request as a website-owned record so later syncs match it by URL.
        check["reward"] = reward
        self.store.update(item_id, source="website", owned=True, assist_url=page.url, status="published", request_reward=reward,
                          publish_action=action("confirmed"), publish_check=dict(check, kind="published", time=now()), publish_blocked=False,
                          publish_retry=None, note="已发布，等待应助。")
        self.store.event("已发布求助，悬赏 %d 积分，等待同步状态。" % reward, item_id=item_id, kind="publish")
        app_settings.record_spend(self.store, self.accounts.active_id, reward)
        if isinstance(points, int) and not isinstance(points, bool):
            # The reward is deducted at publish time: keep the stored balance current for the next gate/tier decision.
            self.store.update_account(account_id=self.accounts.active_id, points=points - reward)

    def handle_confirm_prompt(self, item_id, message, reward, points, prefs, check):
        """code 2 「确认提示」. Only a pure “最低悬赏被提高” within the person's setting may be accepted; anything else stops."""
        page = self.page
        layer, text = None, ""
        for _ in range(8):
            for candidate, content in self.visible_layers():
                if "确认提示" in content or "仍然提交" in content:
                    layer, text = candidate, content
            if layer is not None:
                break
            page.wait_for_timeout(500)
        full = (message + "\n" + text).strip()
        self.store.update(item_id, publish_action=None)
        check = dict(check, confirm_text=full[:400])
        if layer is None:
            self.publish_stop(item_id, "code2_unknown", "科研通要求再次确认，但未出现确认窗口，未提交：" + " ".join(message.split())[:100], check)
        failure = pp.classify_failure(full)
        if failure:
            self.publish_stop(item_id, failure["kind"], failure["message"], check, pause=failure["pause"])
        needed = pp.parse_raise(full)
        if needed is None or needed <= reward:
            self.publish_stop(item_id, "code2_other", "科研通弹出了需用户亲自查看的确认提示（并非单纯的“最低悬赏上调”），未提交：" + " ".join(full.split())[:140], check)
        agreed = (self.store.get(item_id) or {}).get("request_reward") or reward
        ok, why = pp.raise_decision(needed, points, app_settings.raise_settings_for(prefs, self.accounts.active_id, agreed))
        if not ok:
            self.publish_stop(item_id, "min_reward_raised", why + "（当前设置的悬赏为 %d 积分）" % reward, dict(check, needed=needed, base=reward), notify_kind="min_bounty")
        over = app_settings.cap_block(prefs, self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}), needed)
        if over:
            raise PreconditionError("未提交：" + over)
        button = layer.locator(".layui-layer-btn a", has_text="仍然提交")
        if button.count() != 1:
            self.publish_stop(item_id, "code2_other", "未找到唯一的“仍然提交”按钮，未提交", check)
        if self.stop.is_set():
            raise ValueError("已停止，未发布")
        self.store.update(item_id, publish_action=dict(status="attempted", point=needed, time=now()))
        with self.throttled():
            button.first.click()
        self.store.event("科研通将最低悬赏上调至 %d 积分，已按设置自动接受。" % needed, item_id=item_id, kind="publish")
        return needed



class _TipError(Exception):
    pass
