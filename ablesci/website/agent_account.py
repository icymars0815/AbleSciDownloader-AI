"""Log-in, account sync and points, matching published requests, prepare/review pages (BrowserAgent mixin, moved unchanged from browser_agent.py in refactor stage 3)."""
import re
from urllib.parse import urljoin
from ablesci.flow.core import normalize, site_url, checkin_result, now
from ablesci.flow.pipeline import WAITING, FULFILLED, DONE
from ablesci.website.agent_common import BASE, PreconditionError


class AccountMixin:
    def login(self, _, credentials=None):
        self.ensure(interactive=True)
        with self.throttled():
            self.open_page(BASE + "/site/login")
        if not credentials:
            self.store.event("已打开独立浏览器，可自行登录，也可在软件中输入账号密码自动登录。")
            return
        if "/site/login" not in self.page.url:
            self.store.update_account(account_id=self.accounts.active_id, last_login_time=now())
            self.store.event("独立浏览器已有登录状态，可同步本人求助。")
            return
        self.page.get_by_role("combobox", name="登录邮箱（必填）").fill(credentials["email"])
        self.page.get_by_role("textbox", name="登录密码（必填）").fill(credentials["password"])
        self.page.get_by_role("button", name="登录", exact=True).click()
        try:
            self.page.wait_for_url(lambda url: "/site/login" not in str(url), timeout=45000)
        except Exception:
            body = self.page.locator("body").inner_text()
            if "邮箱或密码错误" in body:
                raise ValueError("登录失败：邮箱或密码错误。请核对后重试。")
            raise ValueError("登录未完成，请查看独立浏览器中的验证码、验证提示或网络状态。软件不会自动重试密码。")
        if "/site/" in self.page.url:
            raise ValueError("登录需要额外确认，请在独立浏览器中完成后再同步")
        self.store.update_account(account_id=self.accounts.active_id, last_login_time=now())
        self.store.event("账号密码登录成功，登录状态仅保留在本机独立浏览器中。可同步本人求助。")

    def signin(self, _):
        self.goto("/")
        with self.throttled():
            response = self.context.request.get(BASE + "/user/sign", timeout=30000)
        if not response.ok:
            raise ValueError("签到未正常返回，请查看科研通页面。未自动重试")
        try:
            result = response.json()
        except Exception:
            raise ValueError("签到未返回有效结果，请检查登录状态或验证提示")
        status = checkin_result(result)
        self.goto("/")
        balance = re.search(r"当前拥有\s*(\d+)\s*积分", self.page.locator("body").inner_text())
        fields = {"last_checkin_result": status, "last_checkin_time": now()}
        if balance:
            fields["points"] = int(balance.group(1))
        self.store.update_account(account_id=self.accounts.active_id, **fields)
        self.store.event(status + (f"，当前 {fields['points']} 积分。" if balance else "。"))

    def read_points(self):
        match = re.search(r"(?:当前拥有|您当前有|当前有|我的积分)[\s:：]*(\d+)\s*(?:积分)?", self.page.locator("body").inner_text(timeout=5000))
        return int(match.group(1)) if match else None

    def sync(self, _):
        # Record the attempt first so a failing sync is not retried on every scheduler tick.
        self.store.update_account(account_id=self.accounts.active_id, last_sync_time=now())
        self.goto("/my/assist-my")
        self.page.locator("table").wait_for()
        try:
            points = self.read_points()
        except Exception:
            points = None
        if points is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=points)
        count = 0
        for row in self.page.locator("table tr").all():
            link = row.locator('a[href*="/assist/detail?id="]')
            if not link.count():
                continue
            title = link.first.inner_text().strip()
            url = urljoin(BASE, link.first.get_attribute("href"))
            if not site_url(url, "/assist/detail"):
                continue
            state = row.locator("td").last.inner_text().strip()
            # A request this app published (possibly with an unconfirmed result) is adopted by title, not duplicated.
            mine = self.match_published(title, url)
            if mine:
                self.store.update(mine, source="website", owned=True, assist_url=url, status="published",
                                  publish_action=dict(self.store.get(mine).get("publish_action") or {}, status="confirmed"))
            item_id = mine or self.store.add(title, "", "website", account_id=self.accounts.active_id, assist_url=url, owned=True, website_status=state)
            self.store.update(item_id, website_status=state, website_status_time=now())
            # The answer's arrival time is stamped once (deadline base for auto-adopt / 2-hour takeover); a new answer after
            # a rejection (status back to waiting) gets a fresh stamp.
            current = self.store.get(item_id) or {}
            if state in FULFILLED and not current.get("fulfilled_at"):
                self.store.update(item_id, fulfilled_at=now())
            elif state in WAITING and current.get("fulfilled_at"):
                self.store.update(item_id, fulfilled_at=None)
            self.resolve_handover(item_id, state)
            count += 1
        if points is None:
            # "我的求助" does not show the balance (real 2026-11-02): without this a top-up was never noticed, the stale
            # balance kept the <500 gate shut, and the only step that reads it (publish) never ran. Opening the form reads only.
            try:
                self.goto("/assist/create", interactive=False)
                points = self.read_points()
            except Exception:
                points = None
            if points is not None:
                self.store.update_account(account_id=self.accounts.active_id, points=points)
        self.store.event(f"已同步本页 {count} 条求助，未打开登录窗口。" + (f"当前积分 {points}。" if points is not None else ""))

    def resolve_handover(self, item_id, state):
        """After the human handled a blocked adopt/reject on the site, the synced status tells what they did."""
        item = self.store.get(item_id)
        if not item or not item.get("handover") or item.get("status") != "attention" or state in FULFILLED:
            return
        if state in DONE:
            self.store.update(item_id, status="adopted", handover=dict(item["handover"], resolved=now(), outcome="adopted_on_site"))
            self.store.event("已同步：《%s》已在科研通处理（已完结）。" % (item.get("title") or "")[:40], item_id=item_id, kind="adopt")
        elif state in WAITING:
            archived = dict(pdf=item.get("pdf", ""), verification=item.get("verification"), download_file_url=item.get("download_file_url", ""),
                            rejected_at=now(), by="human_on_site")
            self.store.update(item_id, status="rejected", pdf="", verification=None, download_file_url="", rejected_at=archived["rejected_at"],
                              rejected_files=list(item.get("rejected_files", [])) + [archived],
                              handover=dict(item["handover"], resolved=now(), outcome="rejected_on_site"))
            self.store.event("已同步：《%s》已在科研通驳回，求助重新等待应助，本地文件已归档保留。" % (item.get("title") or "")[:40], item_id=item_id, kind="reject")

    def match_published(self, title, url):
        """Local import item this app tried to publish whose title matches a synced row (and has no site URL yet)."""
        key = normalize(title)
        for item in self.store.snapshot()["items"]:
            if (item.get("source") == "import" and item.get("account_id", "default") == self.accounts.active_id
                    and not item.get("assist_url") and (item.get("publish_action") or {}).get("status") in ("attempted", "uncertain", "confirmed")
                    and key and normalize(item.get("title", "")) == key):
                return item["id"]
        return None

    def wait_published(self, timeout_s=25):
        """After a code-0 submit the site shows a success layer with 查看求助详情 instead of always navigating."""
        page = self.page
        for _ in range(timeout_s * 2):
            if site_url(str(page.url), "/assist/detail"):
                return True
            for layer, text in self.visible_layers():
                if "查看求助详情" in text:
                    button = layer.locator("a, button, span").filter(has_text=re.compile(r"^\s*查看求助详情\s*$"))
                    if button.count() >= 1:
                        try:
                            button.first.click()
                        except Exception:
                            pass
            page.wait_for_timeout(500)
        return site_url(str(page.url), "/assist/detail")

    def prepare(self, item_id):
        item = self.store.get(item_id)
        if not item or item["source"] == "demo":
            raise PreconditionError("请先导入真实 DOI 或标题。演示数据不能提交到科研通")
        self.assert_account(item)
        self.goto("/assist/create", interactive=True)
        if item["doi"]:
            self.page.locator('[id="Assist-doi"]').fill(item["doi"])
        if item["title"]:
            self.page.locator('[id="Assist-title"]').fill(item["title"])
        self.store.update(item_id, status="prepared", note="已填入现有信息。请在求助页面提取并核对文献信息后自行发布，软件未提交。")
        self.store.event("已填写求助表单，尚未发布。")

    def review(self, item_id):
        item = self.store.get(item_id)
        if not item or not site_url(item.get("assist_url", ""), "/assist/detail"):
            raise PreconditionError("该条尚无已同步的求助链接，请先同步本人记录")
        self.assert_account(item)
        self.goto(item["assist_url"], interactive=True)
        self.store.event("已打开求助详情。请结合核对结果自行采纳或驳回，软件未执行这两类操作。")

