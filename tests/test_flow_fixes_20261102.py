"""Regression tests for the real failures of 2026-11-02 (docs/FLOW_FIXES_20261102.md). Temp data only; nothing contacts ablesci.com."""
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import intake
import publish_policy as pp
from accounts import Accounts
from core import Store
from pipeline import Pipeline, PIPELINE_RULES_VERSION, stage_of
from tests import helpers as hp
from tests.helpers import FakeAgent
from tests.helpers import DETAIL, DOI, TITLE, El, PublishBase

# Exact text of the popup that stopped Rice 2003 (data/queue.json event 2026-11-02T12:57:02).
REAL_CODE2 = ("检验有风险 确认提示 积分不符合最低要求提醒： 您所求助的文献来源于University of Chicago Press，拥有该库权限的用户非常少 根据 "
              "疑难数据库求助规则 ，最低需要 50 积分 处理方法：点击【仍然提交】系统自动修改为最低积分 点击【返回修改】自行修改积分")
UNDEFINED_LAYER = "标题 undefined\nDOI undefined\n期刊 undefined\n作者 undefined\n信息正确，直接发布 信息正确，还需修改 信息有误，手动填写"
AUTO_RAISE = dict(publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=50)


class Code2MinimumRaiseTests(PublishBase):
    """Issue 2: the 「积分不符合最低要求」 popup is a plain minimum raise and must follow the auto-accept setting."""

    def test_real_popup_text_is_a_plain_raise(self):
        self.assertEqual(50, pp.parse_raise(REAL_CODE2))
        self.assertIsNone(pp.classify_failure(REAL_CODE2))
        self.assertIsNone(pp.parse_raise("你可能已经提交过重复求助，最低需要 50 积分"))   # duplicates still never count

    def prompt(self):
        again = El("仍然提交", on_click=lambda: None)
        self.page.submit.on_click = lambda: self.page.layers.append(El(REAL_CODE2, buttons=[again, El("返回修改")]))
        self.page.response = dict(code=2, msg="检验有风险")
        return again

    def test_auto_setting_accepts_the_real_popup(self):
        self.store.update_preferences(**AUTO_RAISE)
        item_id = self.item()
        self.script(response=None)
        again = self.prompt()
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(1, again.clicks)
        after = self.store.get(item_id)
        self.assertEqual(("confirmed", 50), (after["publish_action"]["status"], after["request_reward"]))

    def test_ask_setting_still_stops_as_min_bounty(self):
        item_id = self.item()
        self.script(response=None)
        again = self.prompt()
        self.run_publish(item_id)
        self.assertEqual(0, again.clicks)
        self.assertStopped(item_id, "min_reward_raised", clicked=1)


class UndefinedLookupTests(PublishBase):
    """Issue 3: an all-'undefined' lookup layer is never submitted, and is retried instead of stopping for good."""

    def undefined(self):
        layer = El(UNDEFINED_LAYER, buttons=[El("信息正确，还需修改")])
        self.page.search.on_click = lambda: self.page.layers.append(layer)

    def test_retries_then_stops_after_last_attempt(self):
        item_id = self.item()
        for count, minutes in enumerate(pp_minutes(), start=1):
            self.page.layers = []
            self.undefined()
            self.assertIn("自动重试", self.run_publish(item_id))
            after = self.store.get(item_id)
            self.assertEqual(("queued", "ready", count), (stage_of(after), after["status"], after["publish_retry"]["count"]))
            self.assertFalse(after.get("publish_blocked"))
            self.assertGreater(datetime.fromisoformat(after["publish_retry"]["next_at"]), datetime.now().astimezone() + timedelta(minutes=minutes - 1))
        self.page.layers = []
        self.undefined()
        self.assertIn("undefined", self.run_publish(item_id))
        self.assertStopped(item_id, "lookup_undefined")
        self.assertEqual(0, self.page.submit.clicks)

    def test_success_after_retry_clears_the_retry(self):
        item_id = self.item(publish_retry=dict(count=1, next_at="2000-01-01T00:00:00+00:00"))
        self.script(response=dict(code=0), nav=DETAIL)
        self.assertIsNone(self.run_publish(item_id))
        self.assertIsNone(self.store.get(item_id).get("publish_retry"))


def pp_minutes():
    from browser_agent import BrowserAgent
    return BrowserAgent.LOOKUP_RETRY_MINUTES


class McpAuthorizedPipelineTests(unittest.TestCase):
    """Issues 1 + 5: MCP-confirmed requests publish with the App's auto flow off, past a per-paper attention item."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.fake = FakeAgent()
        self.pipeline = Pipeline(self.store, Accounts(self.temp.name), self.fake, start=False)
        self.store.update_preferences(mcp_website_actions=True, mcp_actions_account="default", oa_first=False)   # auto flow stays off
        self.store.update_account("default", points=500, last_sync_time=datetime.now().astimezone().isoformat())

    def confirmed(self, title, **fields):
        return self.store.add(title, "10.1234/" + title.replace(" ", ""), account_id="default", request_confirmed=True,
                              request_mcp_auto_publish=True, request_reward=10, oa_check=dict(status="not_oa"), **fields)

    def test_stopped_item_no_longer_holds_the_others(self):
        rice = self.confirmed("rice", status="attention", publish_blocked=True, note="弹窗识别结果不符", publish_check=dict(kind="lookup_mismatch"))
        optra = self.confirmed("optra")
        self.assertFalse(self.pipeline.enabled())
        planned = self.pipeline.tick_mcp_authorized()
        self.assertEqual(("publish", optra), (planned["action"], planned["item_id"]))
        self.assertEqual([("publish", optra)], self.fake.enqueued)
        self.assertNotIn(("publish", rice), self.fake.enqueued)

    def test_account_wide_stop_still_holds(self):
        self.confirmed("acct", status="attention", publish_blocked=True, publish_check=dict(kind="restricted"))
        self.confirmed("optra")
        self.assertIsNone(self.pipeline.tick_mcp_authorized())
        self.assertEqual([], self.fake.enqueued)

    def test_kick_only_when_enabled(self):
        self.pipeline.kick()
        self.assertFalse(self.pipeline.woken.is_set())
        self.pipeline.kick_enabled = True
        self.pipeline.kick()
        self.assertTrue(self.pipeline.woken.is_set())


class McpAdoptAndRequeueTests(unittest.TestCase):
    """Issues 4 + 6 through the real /api/mcp handlers (browser agent mocked)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = hp.start(self.temp.name, permission=True)
        self.addCleanup(hp.stop, self.server)
        self.api = self.server.mcp_api
        self.store = self.server.store

    def answered(self, **verification):
        pdf = self.store.root / "downloads" / "answer.pdf"
        pdf.parent.mkdir(exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 fictional")
        v = dict(status="identity_match", sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), bibliography=dict(status="match"))
        v.update(verification)
        return self.store.add("Answered", "10.1234/ans", account_id="default", source="website", owned=True, assist_url="https://www.ablesci.com/assist/detail?id=X",
                              website_status="待确认", status="downloaded", pdf="downloads/answer.pdf", download_file_url="f", verification=v)

    def test_adopt_needs_confirmation_and_a_verified_unchanged_file(self):
        good = self.answered()
        with patch.object(self.server.pipeline, "agent", MagicMock()) as agent:
            with self.assertRaises(ValueError):
                self.api.do_adopt({"id": good})                       # no confirmed=true
            agent.enqueue.assert_not_called()
            self.assertTrue(self.api.do_adopt({"id": good, "confirmed": True})["queued"])
            agent.enqueue.assert_called_once()
            action, item_id, scope = agent.enqueue.call_args.args
            self.assertEqual(("adopt", good, False), (action, item_id, scope["mcp_scope"]["auto"]))   # scoped like other MCP site actions
        self.store.update(good, verification=dict(self.store.get(good)["verification"], status="title_mismatch"))
        with patch.object(self.server.pipeline, "agent", MagicMock()) as agent:
            with self.assertRaises(ValueError):
                self.api.do_adopt({"id": good, "confirmed": True})
            agent.enqueue.assert_not_called()

    def test_adopt_after_app_adopted_says_so(self):
        # Real 2026-11-03: the App adopted a fully matching MCP-confirmed answer itself; adopt_paper then only errored.
        good = self.answered()
        self.store.update(good, status="adopted", adopt_action=dict(status="confirmed", time="2026-11-03T00:07:58+08:00"))
        with patch.object(self.server.pipeline, "agent", MagicMock()) as agent:
            out = self.api.do_adopt({"id": good, "confirmed": True})
            agent.enqueue.assert_not_called()
        self.assertTrue(out["already_adopted"])

    def test_adopt_refused_without_permission(self):
        good = self.answered()
        self.store.update_preferences(mcp_website_actions=False)
        with self.assertRaises(Exception):
            self.api.do_adopt({"id": good, "confirmed": True})

    def test_adopt_refused_when_file_changed(self):
        good = self.answered()
        (self.store.root / "downloads" / "answer.pdf").write_bytes(b"%PDF-1.4 changed")
        with patch.object(self.server.pipeline, "agent", MagicMock()) as agent:
            with self.assertRaises(ValueError):
                self.api.do_adopt({"id": good, "confirmed": True})
            agent.enqueue.assert_not_called()

    def test_requeue_with_new_reward_then_reconfirm(self):
        ids = hp.add_batch(self.server, count=2, batch="RQ")
        preview = self.api.do_confirm_preview({"batch_id": "RQ", "item_ids": [ids[0]], "rewards": {ids[0]: 10}})
        self.api.do_confirm({"batch_id": "RQ", "confirm_token": preview["confirm_token"], "confirmed": True})
        self.store.update(ids[0], status="attention", publish_blocked=True, publish_check=dict(kind="code2_other"))
        with self.assertRaises(ValueError):
            self.api.do_requeue({"id": ids[0], "reward": 50})          # confirmed=true required
        out = self.api.do_requeue({"id": ids[0], "reward": 50, "confirmed": True})
        after = self.store.get(ids[0])
        self.assertEqual(("queued", False, 50, "RQ"), (stage_of(after), after["request_confirmed"], after["request_reward"], out["batch_id"]))
        self.assertEqual("code2_other", after["publish_history"][-1]["kind"])
        self.assertTrue(intake.classify(after)["selectable"])
        again = self.api.do_confirm_preview({"batch_id": "RQ", "item_ids": [ids[0]]})
        self.assertEqual(50, again["items"][0]["reward"])

    def test_requeue_refused_for_submitted_or_uncertain(self):
        ids = hp.add_batch(self.server, count=1, batch="RU")
        for fields in (dict(publish_action=dict(status="uncertain")), dict(publish_action=dict(status="attempted")),
                       dict(assist_url="https://www.ablesci.com/assist/detail?id=Y")):
            self.store.update(ids[0], request_confirmed=True, status="attention", **fields)
            with self.assertRaises(ValueError):
                self.api.do_requeue({"id": ids[0], "confirmed": True})
            self.store.update(ids[0], publish_action=None, assist_url="")

    def test_auto_continue_reports_the_plan(self):
        ids = hp.add_batch(self.server, count=1, batch="AC")
        self.store.update(ids[0], request_confirmed=True, request_reward=10)
        out = self.api.do_auto_continue({"id": ids[0]})
        self.assertEqual(("publish", ids[0], True), (out["plan"]["action"], out["plan"]["item_id"], out["plan"]["for_this_item"]))


class SyncReadsPointsTests(unittest.TestCase):
    """A top-up must be noticed: "我的求助" shows no balance, so sync falls back to reading it from the request form."""

    def test_sync_reads_points_from_form_when_list_page_has_none(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from browser_agent import BrowserAgent
        store = Store(temp.name)
        agent = BrowserAgent(store, Accounts(temp.name))
        self.addCleanup(agent.thread.join, 0.1)
        self.addCleanup(agent.commands.put, ("close", None, None))
        store.update_account("default", points=186)
        agent.page, agent.goto = MagicMock(), MagicMock()
        agent.page.locator.return_value.all.return_value = []
        agent.read_points = MagicMock(side_effect=[None, 640])
        agent.sync(None)
        self.assertEqual(640, store.snapshot()["account_metrics"]["default"]["points"])
        self.assertEqual("/assist/create", agent.goto.call_args_list[-1].args[0])


class SilentModeTests(unittest.TestCase):
    """AI-driven work shows no pop-ups; problems are recorded and reported through needs_attention."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.toasts = []
        from notifications import Notifier
        self.notifier = Notifier(self.store, sink=lambda t, m: self.toasts.append(t))

    def test_mcp_item_is_silent_but_recorded(self):
        iid = self.store.add("AI paper", "10.1234/ai", account_id="default", request_mcp_auto_publish=True)
        self.assertEqual("ai", self.notifier.notify("needs_action", iid, "需处理", "AI paper 需处理"))
        self.assertEqual([], self.toasts)
        self.assertTrue(any("未弹窗" in e["message"] for e in self.store.snapshot()["events"]))

    def test_recent_mcp_call_silences_account_toasts_and_user_items_still_pop(self):
        user_item = self.store.add("User paper", "10.1234/u", account_id="default")
        self.assertEqual("sent", self.notifier.notify("needs_action", user_item, "需处理", "x"))
        import time
        self.store.mcp_last_call = time.time()
        self.assertEqual("ai", self.notifier.notify("login_expired", "account:default", "需登录", "y"))
        self.assertEqual(1, len(self.toasts))

    def test_opt_out(self):
        iid = self.store.add("AI paper", "10.1234/ai", account_id="default", request_mcp_auto_publish=True)
        self.store.update_preferences(mcp_silent=False)
        self.assertEqual("sent", self.notifier.notify("needs_action", iid, "需处理", "z"))


class NeedsAttentionTests(unittest.TestCase):
    def test_feed_lists_decisions_stops_and_setup_hints(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = hp.start(temp.name, permission=True)
        self.addCleanup(hp.stop, server)
        store = server.store
        answered = store.add("Answered", "10.1234/a", account_id="default", source="website", owned=True, assist_url="https://www.ablesci.com/assist/detail?id=A",
                             website_status="待确认", status="downloaded", pdf="downloads/a.pdf", download_file_url="f", verification=dict(status="identity_match"))
        stopped = store.add("Stopped", "10.1234/s", account_id="default", request_confirmed=True, status="attention",
                            publish_blocked=True, publish_check=dict(kind="lookup_mismatch"), note="弹窗识别不符")
        feed = server.mcp_api.do_needs_attention({})
        kinds = {r["id"]: r["kind"] for r in feed["items"]}
        self.assertEqual("decide_answer", kinds[answered])
        self.assertEqual("stopped:lookup_mismatch", kinds[stopped])
        self.assertIn("requeue_request", next(r for r in feed["items"] if r["id"] == stopped)["next"])
        self.assertIn("oa_contact_email", {h["kind"] for h in feed["setup"]})


if __name__ == "__main__":
    unittest.main()
