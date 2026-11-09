"""Downloading a received file: free lines, high speed, waiting, saving and verifying (BrowserAgent mixin, moved unchanged from browser_agent.py in refactor stage 3)."""
import re
import time
from urllib.parse import urljoin
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from download_policy import (make_watch, read_progress, highspeed_allowed, download_settings, resolve_highspeed,
                             takeover_reason, backoff_minutes, dynamic_cap_for, longer_minutes)
import deletion
from download_lines import (LINES, SpeedHistory, SwitchWatch, select_line, order_after, eta_seconds, plan_text, fmt_size,
                            fmt_duration, explain_failure, safe_filename, HIGHSPEED)
import settings as app_settings
from core import doi_value, site_url, now
from metadata import verify_with_metadata
from rejection_policy import rejection_decision
from agent_common import BASE, _count, SessionExpired, PreconditionError, RetryScheduled, is_transient


class DownloadMixin:
    BALANCE_FRESH_SECONDS = 15 * 60

    def recent_balance_at_least(self, minimum):
        """The stored balance (from a sync) is at least `minimum` and that sync is under 15 minutes old.
        Used only for the daily-spend bookkeeping of a high-speed click when the live balance could not be read."""
        account = self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {})
        points, synced = account.get("points"), account.get("last_sync_time")
        if not isinstance(points, int) or isinstance(points, bool) or points < minimum or not synced:
            return False
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(synced)).total_seconds()
        except (TypeError, ValueError):
            return False
        return 0 <= age < self.BALANCE_FRESH_SECONDS

    def hand_over_download(self, item_id):
        """O1: the download runs on the dedicated worker so this agent stays free for sync/publish/adopt/reject.
        Cheap preconditions and the session export happen here, on this agent's own thread."""
        item = self.store.get(item_id)
        if not item or not item.get("owned") or item["source"] != "website":
            raise PreconditionError("只下载从本人求助列表同步的文件")
        self.assert_account(item)
        if item.get("verification") or item.get("pdf"):
            raise PreconditionError("该条目已有本地 PDF，不会重复下载（避免重复扣分）；请先在本地复核")
        state = self.export_state()
        scope = getattr(self, "_mcp_scope", None)
        if scope:
            self.worker.submit(item_id, state, authority={"mcp_scope": scope})
        else:
            self.worker.submit(item_id, state)

    def download(self, item_id):
        item = self.store.get(item_id)
        if not item or not item.get("owned") or item["source"] != "website":
            raise PreconditionError("仅可下载本人求助列表中同步的文件")
        self.assert_account(item)
        if item.get("verification") or item.get("pdf"):
            raise PreconditionError("该条目已有本地 PDF，不会重复下载（避免重复扣分）。请先在本地复核")
        self.assert_mcp_authority()
        self.store.update(item_id, download_mcp_scope=getattr(self, "_mcp_scope", None))
        self.goto(item["assist_url"])
        table = self.page.locator("table").first
        table.wait_for()
        text = table.inner_text()
        title_row = table.locator("tr").filter(has=self.page.locator("td", has_text=re.compile(r"^标题$")))
        actual_title = title_row.locator("td").last.inner_text().strip().splitlines()[0].strip() if title_row.count() else item["title"]
        doi_row = table.locator("tr").filter(has=self.page.locator("td", has_text=re.compile(r"^DOI$")))
        actual_doi = doi_value(doi_row.inner_text()) if doi_row.count() else ""
        if item["doi"] and actual_doi != item["doi"]:
            raise ValueError("求助详情 DOI 与任务不符，已停止")
        link = table.locator('a[href*="/assist/download?id="]')
        if not link.count():
            link = self.page.locator('a[href*="/assist/download?id="]')
        if not link.count():
            raise ValueError("尚无可下载的文件，请待有人应助后重试")
        url = urljoin(BASE, link.first.get_attribute("href"))
        if not site_url(url, "/assist/download"):
            raise ValueError("下载入口格式变化，已停止")
        # The download page starts automatically. Check paid auto-routing BEFORE opening it.
        self.goto("/my/assist-my-set")
        selected = self.page.locator('input[name="file_highspeed"]:checked')
        if selected.count() != 1 or selected.get_attribute("value") != "0":
            raise ValueError("科研通的“智能高速下载”未确认关闭。为避免额外扣分，请自行核对科研通的设置；软件不会修改该设置。")
        preferences = self.store.snapshot().get("preferences", {})
        settings = download_settings(preferences)
        try:
            fresh = self.read_points()
        except Exception:
            fresh = None
        if fresh is not None:
            self.store.update_account(account_id=self.accounts.active_id, points=fresh)
        points = fresh if fresh is not None else self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}).get("points")
        mode, _source, changes, message = resolve_highspeed(preferences, points)
        if changes:
            self.store.update_preferences(**changes)
        if message:
            self.store.event(message)
        file_id = (parse_qs(urlparse(url).query).get("id") or [""])[0]
        extra = {}
        if actual_doi and not item.get("doi"):
            extra["doi_origin"] = "site"      # filled in by the site's smart extraction, not by the person
        self.store.update(item_id, status="downloading", doi=actual_doi, title=actual_title, download_progress=None, download_failure=None,
                          download_file_url=url, download_attempts=[], note="正在连接科研通普通下载", **extra)
        self._record_ctx = None
        pending, attached, failures = [], [], []
        ctx = dict(transient=False, hs_allowed=None, fee_mismatch=False, mode=mode)

        def bump(field):
            value = int((self.store.get(item_id) or {}).get(field) or 0) + 1
            self.store.update(item_id, **{field: value})
            return value
        def download_event(download):
            pending.append(download)
        observed = []
        def observe(response):
            # D1: passive, read-only record of how the file server answers (resume support); query strings dropped
            try:
                headers = response.headers
                kind = headers.get("content-type", "")
                size = int(headers.get("content-length") or 0)
                ranged = "content-range" in headers or response.status == 206 or "range" in (response.request.headers or {})
                filelike = "pdf" in kind or "octet-stream" in kind or size > 1_000_000
                if not (ranged or filelike) or re.search(r"javascript|css|image/|font/|text/html", kind) and not ranged:
                    return
                if len(observed) < 40:
                    parts = urlparse(response.url)
                    observed.append(dict(url=parts.netloc + parts.path, status=response.status, accept_ranges=headers.get("accept-ranges"),
                                         content_range=headers.get("content-range"), length=size or None, type=kind[:60],
                                         request_range=(response.request.headers or {}).get("range"), time=now()))
            except Exception:
                pass
        def attach(page):
            page.on("download", download_event)
            try:
                page.on("response", observe)
            except Exception:
                pass
            attached.append(page)
        for page in self.context.pages:
            attach(page)
        self.context.on("page", attach)
        try:
            self.goto(url)
            self.page.get_by_role("progressbar", name="下载进度").wait_for(timeout=20000)
            def active_line(n):
                control = self.line_control(n)
                if control.get_attribute("aria-pressed") == "true" or control.get_attribute("aria-checked") == "true":
                    return True
                try:
                    return control.is_checked()
                except Exception:
                    return False
            attempts, reasons = [], []
            # The ">= 500 points, no click-through" waiver may only rely on points read live for THIS
            # download; a stored balance can be stale and the user may now be charged.
            live_points = fresh
            paid_before = file_id and any(p.get("file_id") == file_id for p in (self.store.get(item_id).get("highspeed_paid") or []))
            history = SpeedHistory(self.store)
            hist_now = history.all() if settings["probe_enabled"] else {}
            self.report_plan(item_id, hist_now, settings, active_line)
            force_ok = False
            if mode == "force":
                force_ok, force_reason = highspeed_allowed([], preferences, self.page.locator("body").inner_text(), mode="force", points=live_points)
                ctx["hs_allowed"] = force_ok
                ctx["fee_mismatch"] = (not force_ok) and "费用规则" in force_reason
                if force_ok:
                    self.store.event("强制高速：费用规则核对通过，跳过普通线路。")
                else:
                    self.store.event("强制高速未执行（" + force_reason + "），改走普通线路，未扣分。")
            def run_normal_lines():
                current = next((n for n in LINES if active_line(n)), 1)
                order, active = [current] + [n for n in LINES if n != current], current
                if settings["probe_enabled"]:
                    order, active = self.choose_line(item_id, current, settings, pending, history)
                queue = list(order)
                if mode == "smart" and not pending and self.hs_early(item_id, settings, history, hist_now):
                    ctx["early_hs"] = True
                    queue = []
                while queue and not pending:
                    line = queue.pop(0)
                    if self.stop.is_set():
                        raise ValueError("下载已停止，未执行采纳或驳回")
                    if line != active:
                        self.line_control(line).click()
                        active = line
                    self.store.event(f"正在尝试普通线路 {line}；有持续进度时继续等待，停止接收 {settings['stall_seconds']} 秒后切换。", item_id=item_id, kind="download")
                    self._line_ctx = dict(line=line, hist=hist_now, history=history, probe=settings["probe_enabled"])
                    self._switch_target = None
                    outcome = self.wait_transfer(item_id, f"普通线路 {line}", pending, settings, eta=mode == "smart")
                    attempts.append(dict(line=line, outcome=outcome, time=now(), mode=mode, reason="" if outcome == "complete" else outcome))
                    self.store.update(item_id, download_attempts=attempts)
                    if outcome == "complete":
                        break
                    failures.append(line)
                    reasons.append(outcome)
                    bump("normal_failures")
                    explanation = {"stalled": f"{settings['stall_seconds']} 秒未继续接收", "error": "页面提示失败", "timeout": "等待时间过长",
                                   "eta": "按当前速度预计超出单线路上限", "switch": "明显比另一条线路慢"}.get(outcome, "未完成")
                    target = self._switch_target if outcome == "switch" else None
                    if target in queue:
                        queue.remove(target)
                        queue.insert(0, target)
                    self.store.event(f"普通线路 {line} 未完成（{explanation}），将检查下一条线路。", item_id=item_id, kind="download")
            def patient_best_line():
                """Every free line was dropped only for being slow ("eta"): waiting on the fastest beats giving up on all."""
                if pending or not reasons or any(r != "eta" for r in reasons) or self.stop.is_set():
                    return
                if mode == "smart" and self.hs_early(item_id, settings, history, hist_now, after_lines=True):
                    ctx["early_hs"] = True
                    return
                measured = ((self.store.get(item_id) or {}).get("line_probe") or {}).get("measured") or {}
                speeds = {int(k): v for k, v in measured.items() if str(k).isdigit()}
                best = max(speeds, key=speeds.get) if speeds else LINES[0]
                self.store.event(f"三条普通线路都很慢；改为耐心等最快的线路 {best} 下完（只按停滞和时间上限判断）。", item_id=item_id, kind="download")
                self.line_control(best).click()
                self._line_ctx = None            # patient mode: no mid-download switching
                # Last free option: give it the same longer limit as a paid high-speed attempt (real 2026-10-20: 10 min was not enough).
                patient = dict(settings, line_max_minutes=longer_minutes(settings))
                outcome = self.wait_transfer(item_id, f"普通线路 {best}", pending, patient, eta=False)
                attempts.append(dict(line=best, outcome=outcome, time=now(), mode=mode, reason="patient"))
                self.store.update(item_id, download_attempts=attempts)
                if outcome != "complete":
                    bump("normal_failures")

            if not force_ok:
                run_normal_lines()
                patient_best_line()
            if not pending:
                # Smart reaches this only after all 3 normal lines failed; force reaches it directly.
                # Both modes go through the same fee-text / consent gate.
                if force_ok:
                    allowed, explanation = True, ""
                elif mode == "force":
                    allowed, explanation = False, "强制高速未通过费用核对，已改用普通线路，但均未完成。"
                else:
                    allowed, explanation = highspeed_allowed(list(LINES) if ctx.get("early_hs") else failures, preferences, self.page.locator("body").inner_text(), mode=mode, points=live_points)
                    ctx["hs_allowed"] = allowed
                    ctx["fee_mismatch"] = (not allowed) and "费用规则" in explanation
                if not allowed:
                    ctx["transient"] = not ctx["fee_mismatch"]
                    raise ValueError("3 条普通线路均未完成。" + explanation)
                clicks = int((self.store.get(item_id) or {}).get("highspeed_clicks") or 0)
                if clicks >= settings["highspeed_max_tries"]:
                    raise ValueError("该文件的高速通道已尝试 %d 次（上限 %d 次），不再尝试。请在科研通查看；已扣分但未下载成功的，可提交工单" % (clicks, settings["highspeed_max_tries"]))
                if paid_before:
                    self.store.event("该文件此前已支付过高速费用。按科研通规则，同一文件只扣一次，本次再次使用高速不会重复扣分。", item_id=item_id, kind="download")
                if self.stop.is_set():
                    raise ValueError("下载已停止，未启用高速通道")
                tries = "本次点击高速通道一次（该文件最多 %d 次，当前为第 %d 次）。" % (settings["highspeed_max_tries"], clicks + 1)
                self.store.event(("准备按确认的费用规则使用高速通道；" if force_ok else "3 条普通线路全部失败，准备按确认的费用规则切换高速通道；") + tries)
                speed_button = self.page.locator("#download-highspeed-direct")
                if speed_button.count() != 1:
                    raise ValueError("高速通道入口已变化，未点击，请手动检查")
                # Recorded BEFORE the click: whatever happens next, this file is never paid for a second time.
                charged = not paid_before and not (isinstance(live_points, int) and not isinstance(live_points, bool) and live_points >= 500)
                if charged and not paid_before and live_points is None and self.recent_balance_at_least(510):
                    charged = False   # bookkeeping only: a fresh synced balance well above 500 means this click is free
                over = app_settings.cap_block(preferences, self.store.snapshot().get("account_metrics", {}).get(self.accounts.active_id, {}), 2) if charged else ""
                if over:
                    raise ValueError("未使用付费高速通道：" + over)
                self.store.update(item_id, highspeed_paid=list(self.store.get(item_id).get("highspeed_paid") or [])
                                  + [dict(file_id=file_id, time=now(), charged=charged, mode=mode)], highspeed_clicks=clicks + 1)
                self._record_ctx = dict(line=HIGHSPEED, history=history)   # speed history only; the switch rule never applies
                self._line_ctx = None                # the free-line switch rule never applies to high speed
                speed_button.click()
                if charged:
                    app_settings.record_spend(self.store, self.accounts.active_id, 2)
                self.confirm_highspeed(item_id)
                # Paid attempt: don't cut it off with the free-line cap (real 2026-10-20: high speed ran ~20 KB/s and restarted once).
                hs_settings = dict(settings, line_max_minutes=longer_minutes(settings))
                outcome = self.wait_transfer(item_id, "高速通道", pending, hs_settings)
                attempts.append(dict(line="highspeed", outcome=outcome, time=now(), mode=mode,
                                     reason="forced" if force_ok else ("early_eta" if ctx.get("early_hs") else (reasons[-1] if reasons else ""))))
                self.store.update(item_id, download_attempts=attempts)
                if outcome != "complete":
                    bump("highspeed_failures")
                if outcome != "complete" and force_ok and not failures and not self.stop.is_set():
                    # Forced high speed failed before any free line was tried: fall back to the free lines (no second high-speed click).
                    self.store.event("强制高速未完成，改用免费的普通线路继续下载（不会再次点击高速，不会重复扣分）。", item_id=item_id, kind="download")
                    self.goto(url)
                    self.page.get_by_role("progressbar", name="下载进度").wait_for(timeout=20000)
                    run_normal_lines()
                    outcome = "complete" if pending else outcome
                if outcome != "complete":
                    ctx["transient"] = True
                    raise ValueError("高速通道未完成，请检查页面（同一文件只扣一次分，后续重试不会重复扣分）")
            self.save_download(item_id, pending[0], actual_title, actual_doi)
            self.store.update(item_id, download_retry=None)
        except Exception as exc:
            screenshot_folder = self.store.root / "screenshots"
            screenshot_folder.mkdir(exist_ok=True)
            try:
                self.page.screenshot(path=str(screenshot_folder / f"download-{item_id}-{datetime.now():%H%M%S}.png"), mask=[self.page.locator("input")])
            except Exception:
                pass
            # D8: say what kind of failure this is and what to do next.
            message = str(exc) if isinstance(exc, ValueError) else "下载页面未正常打开，或页面结构已变化"
            failure = explain_failure(message, exc)
            record = dict(kind=failure["kind"], label=failure["label"], advice=failure["advice"], time=now())
            if failure["kind"] == "stopped":
                self.store.update(item_id, status="attention", download_failure=record)
                raise
            full = f"{message}（{failure['label']}。建议：{failure['advice']}）"
            transient = (ctx["transient"] or is_transient(exc)) and not isinstance(exc, (SessionExpired, PreconditionError))
            retry, text = self.retry_or_takeover(item_id, transient, ctx, settings, record, full)
            if retry:
                raise RetryScheduled(text) from exc
            raise (type(exc)(text) if isinstance(exc, ValueError) else ValueError(text)) from exc
        finally:
            self.context.remove_listener("page", attach)
            for page in attached:
                try:
                    page.remove_listener("download", download_event)
                    page.remove_listener("response", observe)
                except Exception:
                    pass
            if observed:
                try:
                    self.store.update(item_id, range_observations=observed)
                except Exception:
                    pass
            try:
                with self.throttled():
                    self.page.goto(item["assist_url"], wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

    def retry_or_takeover(self, item_id, transient, ctx, settings, record, full):
        """Download did not finish. Returns (True, text) when an automatic retry was scheduled (item NOT in attention),
        or (False, text) after handing the item to the person (attention + needs_action notification)."""
        item = self.store.get(item_id) or {}
        normal, fast = int(item.get("normal_failures") or 0), int(item.get("highspeed_failures") or 0)
        done_rounds = int((item.get("download_retry") or {}).get("count") or 0)
        hs_possible = ctx["hs_allowed"] if ctx["hs_allowed"] is not None else ctx["mode"] in ("smart", "force")
        if not transient:
            reason = "遇到需手动处理的问题（非网络卡顿），不自动重试（普通线路失败 %d 次，高速通道失败 %d 次）" % (normal, fast)
        else:
            reason = takeover_reason(normal, fast, done_rounds, settings, hs_possible, deletion.seconds_left(item))
        if reason:
            text = reason + "。" + full
            self.store.update(item_id, status="attention", download_failure=record, download_retry=None, note=text)
            self.notify_user("needs_action", item_id, "需处理", "《%s》下载需处理：%s" % ((item.get("title") or item.get("doi") or "?")[:40], reason))
            return False, text
        count = done_rounds + 1
        at = datetime.now().astimezone() + timedelta(minutes=backoff_minutes(count))
        status_text = "自动重试中（第 %d 次，预计 %s 重试）" % (count, at.strftime("%H:%M"))
        self.store.update(item_id, status="retrying", download_failure=record, note=status_text,
                          download_retry=dict(count=count, next_at=at.isoformat(timespec="seconds"), last_reason=record["label"]))
        return True, "%s：%s（普通线路失败 %d 次，高速通道失败 %d 次）" % (status_text, full, normal, fast)

    def line_control(self, number):
        name = re.compile(rf"^线路{number}(?:\s|$)")
        return self.page.get_by_role("button", name=name).or_(self.page.get_by_role("checkbox", name=name)).first

    def line_speed_sample(self, pending, seconds):
        """Average bytes/s the page reports over `seconds` (None if the file finished meanwhile)."""
        self.page.wait_for_timeout(350)
        first = read_progress(self.page.locator("body").inner_text(timeout=5000))["received"]
        start, last = time.monotonic(), first
        while time.monotonic() - start < seconds:
            if self.stop.is_set():
                raise ValueError("下载已停止，未执行采纳或驳回")
            self.page.wait_for_timeout(350)
            if pending:
                return None
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            last = metrics["received"]
            if metrics["error"]:
                return 0.0
        gained = last - first if last >= first else last      # a line switch may reset the page's byte counter
        return max(0, gained) / max(time.monotonic() - start, 0.001)

    def choose_line(self, item_id, current, settings, pending, history):
        """D3: probe the normal lines (history first when there is recent history) and settle on the fastest."""
        state = dict(active=current)
        hist = history.all()

        def probe(line, seconds):
            if pending:
                return None
            if line != state["active"]:
                self.line_control(line).click()
                state["active"] = line
            return self.line_speed_sample(pending, seconds)
        order = [current] + [n for n in LINES if n != current]
        self.store.event("正在测试普通线路速度（每条约 %d 秒），用于选择最快的一条。" % settings["probe_seconds"], item_id=item_id, kind="download")
        result = select_line(order, probe, hist, settings["probe_seconds"], record=history.record)
        measured = result["measured"]
        if not result["done"] and result["chosen"] != state["active"]:
            self.line_control(result["chosen"]).click()
            state["active"] = result["chosen"]
        speeds = "、".join("线路%d %.1f KB/s" % (l, v / 1024) for l, v in sorted(measured.items()))
        how = {"history": "已按近期同时段的历史速度优先测试最快的一条", "history_then_full": "历史最快的线路本次变慢，已改为逐条测速", "full": "逐条测速"}[result["mode"]]
        self.store.update(item_id, line_probe=dict(measured={str(k): int(v) for k, v in measured.items()}, chosen=result["chosen"],
                                                   mode=result["mode"], seconds=result["secs"], time=now()))
        self.store.event(f"{how}：{speeds or '无数据'}；选用线路 {result['chosen']}。", item_id=item_id, kind="download")
        return order_after(result["chosen"], measured), state["active"]

    def hs_early(self, item_id, settings, history, hist_now, after_lines=False):
        """O6: every free line is projected to need longer than the dynamic cap AND the high-speed history for this hour is
        at least twice the best free line. Only decides to skip waiting on free lines; the fee gate, max tries and the
        charged-once rule still apply afterwards."""
        try:
            total = read_progress(self.page.locator("body").inner_text(timeout=5000))["total"]
            hs_speed = history.expected(HIGHSPEED)
            if not total or not hs_speed:
                return False
            measured = ((self.store.get(item_id) or {}).get("line_probe") or {}).get("measured") or {}
            speeds = {int(k): v for k, v in measured.items() if str(k).isdigit() and isinstance(v, (int, float))}
            for line in LINES:
                if line not in speeds and hist_now.get(line):
                    speeds[line] = hist_now[line]
            if not after_lines and set(speeds) != set(LINES):
                return False
            if not speeds:
                return False
            cap = dynamic_cap_for(settings, total)
            if any(v > 0 and total / v <= cap for v in speeds.values()) or hs_speed < 2 * max(speeds.values()):
                return False
            self.store.event("三条普通线路按速度预计都超过 %s，而高速通道同时段历史速度明显更快（至少 2 倍）；直接评估高速通道（仍需通过费用核对）。" % fmt_duration(cap),
                             item_id=item_id, kind="download")
            return True
        except Exception:
            return False

    def report_plan(self, item_id, hist, settings, active_line):
        """D4: size and expected time before the transfer starts."""
        try:
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            line = next((n for n in LINES if active_line(n)), 1)
            text = plan_text(metrics["total"], hist.get(line) if hist else None, int(dynamic_cap_for(settings, metrics["total"]) // 60))
            self.store.update(item_id, download_progress=dict(line="", speed_bps=0, slow=False, eta_seconds=None, plan=text, **metrics))
            self.store.event(text, item_id=item_id, kind="download")
        except Exception:
            pass

    def wait_transfer(self, item_id, label, pending, settings=None, eta=False):
        start = time.monotonic()
        watch = make_watch(start, settings or download_settings({}), eta)
        ctx = getattr(self, "_line_ctx", None) or getattr(self, "_record_ctx", None) or {}
        line, history = ctx.get("line"), ctx.get("history")
        switcher = SwitchWatch(line, ctx["hist"]) if ctx.get("probe") and line in LINES and ctx.get("hist") else None
        last_saved = 0
        slow_reported = False
        size_reported = False
        milestones = set()
        last = dict(received=0, total=0)

        def finish(outcome):
            # D3: remember how fast this line really was at this hour.
            elapsed = time.monotonic() - start
            if history and (line in LINES or line == HIGHSPEED) and last["received"] > 0 and elapsed >= 10:
                history.record(line, last["received"] / elapsed)
            return outcome
        while not pending:
            if self.stop.is_set():
                raise ValueError("下载已停止，未执行采纳或驳回")
            self.page.wait_for_timeout(350)
            if pending:
                return finish("complete")
            metrics = read_progress(self.page.locator("body").inner_text(timeout=5000))
            last = metrics
            tick = time.monotonic()
            outcome = watch.observe(tick, metrics["received"], metrics["error"], metrics["total"])
            if metrics["total"] and not size_reported:
                size_reported = True
                self.store.event(f"{label}：文件大小 {fmt_size(metrics['total'])}。", item_id=item_id, kind="download")
            remaining = eta_seconds(metrics["received"], metrics["total"], watch.speed_bps)
            if outcome == "slow" and not slow_reported:
                self.store.event(f"{label}速度偏慢但仍在接收，继续等待，不会因此切换收费通道。", item_id=item_id, kind="download")
                slow_reported = True
            if tick - last_saved >= 5:
                suffix = "，速度偏慢，继续等待" if outcome == "slow" else ""
                total_text = f" / {fmt_size(metrics['total'])}" if metrics["total"] else ""
                eta_text = f"，预计还需 {fmt_duration(remaining)}" if remaining is not None else ""
                self.store.update(item_id, download_progress=dict(line=label, speed_bps=watch.speed_bps, slow=outcome == "slow",
                                                                  eta_seconds=None if remaining is None else int(remaining), **metrics),
                                  note=f"{label}：{metrics['percent']}%，已接收 {fmt_size(metrics['received'])}{total_text}，约 {watch.speed_bps / 1024:.1f} KB/s{eta_text}{suffix}。")
                last_saved = tick
                for mark in (25, 50, 75):
                    if metrics["percent"] >= mark and mark not in milestones:
                        milestones.add(mark)
                        self.store.event(f"{label}：已下载 {mark}%，约 {watch.speed_bps / 1024:.1f} KB/s" + (f"，预计还需 {fmt_duration(remaining)}" if remaining is not None else "") + "。",
                                         item_id=item_id, kind="download")
            if outcome in {"error", "stalled", "timeout", "eta"}:
                return finish(outcome)
            if switcher and tick - start >= watch.speed_window:
                target = switcher.observe(tick, watch.speed_bps)
                if target:
                    self._switch_target = target
                    return finish("switch")
        return finish("complete")

    def save_download(self, item_id, download, title, doi):
        if self.stop.is_set():
            download.cancel()
            raise ValueError("下载已停止")
        # D7: keep the site's original file name (only characters Windows cannot store are replaced):
        # downloads/{item_id}/{suggested_filename}. A rejected earlier file stays where it is; a later answer gets answer-N/.
        suggested = getattr(download, "suggested_filename", "")
        original = suggested if isinstance(suggested, str) else ""
        name = safe_filename(original) or (item_id + ".pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"      # whatever the site calls it, it is saved as a document and never as something executable
        earlier = len(self.store.get(item_id).get("rejected_files", []))
        folder = self.store.root / "downloads" / item_id / (f"answer-{earlier + 1}" if earlier else "")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if path.exists():
            raise ValueError("目标文件已存在，未覆盖")
        download.save_as(str(path))
        try:
            with path.open("rb") as handle:
                head = handle.read(1024)
        except OSError:
            head = b""
        if b"%PDF-" not in head:
            quarantine = self.store.root / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / f"{item_id}-{datetime.now():%Y%m%d%H%M%S}-{path.name}.quarantined"
            try:
                path.replace(target)
            except OSError:
                target = path
            note = "收到的文件不是 PDF（缺少 PDF 标记），已隔离到 quarantine 文件夹，未作为应助文件处理。请在科研通查看该应助是否有问题。"
            self.store.update(item_id, status="attention", note=note, original_filename=original)
            self.store.event(note, item_id=item_id, kind="error", action="focus")
            raise ValueError("收到的文件不是 PDF，已隔离，未标记下载成功")
        email = self.store.snapshot().get("preferences", {}).get("oa_contact_email", "")
        result = verify_with_metadata(path, title, doi, self.store.root, email=email)
        names = dict(original_filename=original, local_filename=path.name)
        if result["status"] == "invalid":
            self.store.update(item_id, status="attention", pdf=str(path.relative_to(self.store.root)), verification=result, **names,
                              note="已保存浏览器返回的文件，但 PDF 解析失败。文件已保留，供检查。")
            raise ValueError("收到文件但 PDF 解析失败，未标记下载成功")
        self.store.update(item_id, status="downloaded", pdf=str(path.relative_to(self.store.root)), verification=result, **names,
                          download_progress=None, note="文件已保存并完成初步核对。请核对版本和完整性后自行采纳。")
        self.store.event("文件已保存（保留科研通原文件名 " + path.name + "）并完成 PDF 核对，未自动采纳或驳回。", item_id=item_id, kind="download", action="focus")
        decision = rejection_decision(self.store.get(item_id), self.store.snapshot().get("preferences", {}))
        self.store.update(item_id, rejection=decision)
        if decision["action"] == "auto_reject":
            self.reject(item_id, automatic=True)

    def confirm_highspeed(self, item_id, wait_ms=6000):
        """The site asks "高速通道提示 / 确认使用高速通道" after the high-speed click unless the browser has snoozed it
        (localStorage, 30 days). Without this click high speed never starts (real 2026-10-20: every earlier
        "high-speed" attempt sat behind this dialog while the auto-started free line 1 crawled).
        The dialog's own tariff text must match the consented rule; otherwise press 取消 and stop."""
        waited = 0
        while waited <= wait_ms:
            self.assert_mcp_authority()
            for layer, text in self.visible_layers():
                compact = re.sub(r"\s+", "", text)
                if "确认使用高速通道" not in compact:
                    continue
                rule = compact.replace("＜", "<").replace("≥", ">=")
                if not (re.search(r"积分<500.*?扣(?:除)?2积分", rule) and re.search(r"积分>=500.*?(?:免积分|不扣积分)", rule)):
                    cancel = layer.locator(".layui-layer-btn a").filter(has_text="取消")
                    if _count(cancel) == 1:
                        cancel.first.click()
                    self.store.update(item_id, highspeed_confirm=dict(status="fee_mismatch", time=now()))
                    raise PreconditionError("科研通高速通道提示中的费用规则与已确认的规则不一致，已取消，未使用高速通道")
                ok = layer.locator(".layui-layer-btn a").filter(has_text="确认使用高速通道")
                if _count(ok) != 1 or self.stop.is_set():
                    raise ValueError("未能唯一确认高速通道提示的确认按钮，或已停止")
                ok.first.click()
                self.store.update(item_id, highspeed_confirm=dict(status="confirmed", time=now()))
                self.store.event("已在科研通的“高速通道提示”中确认使用高速通道（费用规则与已确认的一致）。", item_id=item_id, kind="download")
                return "confirmed"
            self.page.wait_for_timeout(500)
            waited += 500
        self.store.update(item_id, highspeed_confirm=dict(status="no_dialog", time=now()))
        return "no_dialog"     # snoozed in this browser: the site starts high speed directly

