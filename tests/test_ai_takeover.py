"""AI takeover (2026-11-09, docs/AI_TAKEOVER_20261109.md) and the instance-lock PID-reuse fix.
Temp data and fakes only; nothing contacts ablesci.com, publishes, spends points or changes the person's settings."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import intake
import monitor as mon
import publish_policy as pp
import settings
from accounts import Accounts
from core import Store
from instance_lock import AlreadyRunning, InstanceLock
from notifications import Notifier
from pipeline import Pipeline
from tests.helpers import FakeAgent, PublishBase, lookup_text

SMART = dict(smart_mode=True, smart_rules=settings.SMART_RULES, mcp_website_actions=True, mcp_actions_account="default",
             publish_raise_mode="auto", publish_raise_rules=pp.RAISE_RULES_TEXT, publish_raise_max=50, publish_reserve_points=0)


def takeover(auto=0, since=None, **cfg):
    config = dict(raise_max=50, reserve=0, daily_cap=200, **cfg)
    if auto:
        config.update(auto_confirm_max=auto, auto_confirm_since=since or "2000-01-01T00:00:00+08:00")
    return dict(SMART, smart_config=config)


class SettingsTests(unittest.TestCase):
    def test_takeover_needs_smart_mode_and_the_website_permission_for_this_account(self):
        self.assertTrue(settings.ai_takeover(takeover(), "default"))
        self.assertFalse(settings.ai_takeover(takeover(), "other"))
        self.assertFalse(settings.ai_takeover(dict(takeover(), mcp_website_actions=False), "default"))
        self.assertFalse(settings.ai_takeover(dict(takeover(), smart_rules=""), "default"))
        self.assertFalse(settings.ai_takeover({}, "default"))

    def test_no_ask_amount_is_never_a_silent_default(self):
        self.assertEqual(0, settings.smart(takeover())["auto_confirm_max"])          # existing configs: always ask
        self.assertEqual(0, settings.smart(dict(takeover(), smart_config=dict(auto_confirm_max=999)))["auto_confirm_max"])
        cfg = settings.validate_smart(dict(enabled=True, confirmed=True))
        self.assertNotIn("auto_confirm_max", cfg)
        cfg = settings.validate_smart(dict(enabled=True, confirmed=True, auto_confirm_max=10))
        self.assertEqual(10, cfg["auto_confirm_max"])
        self.assertTrue(cfg["auto_confirm_since"])
        kept = settings.validate_smart(dict(enabled=True, confirmed=True, daily_cap=300), cfg)   # GUI save without the field keeps it
        self.assertEqual((10, cfg["auto_confirm_since"]), (kept["auto_confirm_max"], kept["auto_confirm_since"]))
        for bad in (-1, 51, True, "10"):
            with self.assertRaises(ValueError):
                settings.validate_smart(dict(enabled=True, confirmed=True, auto_confirm_max=bad))

    def test_raise_is_limited_to_the_agreed_amount_under_takeover(self):
        self.assertEqual(10, settings.raise_settings_for(takeover(), "default", 10)["max"])
        self.assertEqual(20, settings.raise_settings_for(takeover(auto=20), "default", 10)["max"])
        self.assertEqual(50, settings.raise_settings_for(dict(takeover(), mcp_website_actions=False), "default", 10)["max"])   # plain smart mode
        ok, why = pp.raise_decision(30, 500, settings.raise_settings_for(takeover(), "default", 10))
        self.assertFalse(ok)
        self.assertIn("需用户决定", why)


class RaiseTests(PublishBase):
    def test_raised_minimum_above_the_confirmed_reward_stops_and_asks(self):
        self.store.update_preferences(**takeover())
        item_id = self.item(request_confirmed=True, request_reward=10)
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIn("30", self.run_publish(item_id))
        self.assertStopped(item_id, "min_reward_raised")
        self.assertEqual("min_bounty", self.agent.notifier.notify.call_args.args[0])

    def test_raise_within_the_no_ask_amount_is_accepted(self):
        self.store.update_preferences(**takeover(auto=30))
        item_id = self.item(request_confirmed=True, request_reward=10)
        self.script(text=lookup_text(minimum=30), response=dict(code=0))
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(30, self.store.get(item_id)["publish_action"]["point"])


class PipelineBase(unittest.TestCase):
    prefs = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.update_preferences(oa_first=False, **(self.prefs if self.prefs is not None else takeover()))
        self.store.update_account("default", points=500, last_sync_time=datetime.now().astimezone().isoformat())
        self.fake = FakeAgent()

    def make(self):
        return Pipeline(self.store, Accounts(self.temp.name), self.fake, start=False)


class RestartTests(PipelineBase):
    def add_confirmed(self):
        return self.store.add("t", "10.1234/t", account_id="default", request_confirmed=True, request_mcp_auto_publish=True,
                              request_reward=10, oa_check=dict(status="not_oa"))

    def test_restart_keeps_background_work_under_takeover(self):
        iid = self.add_confirmed()
        self.store.update_preferences(pipeline_enabled=True, mcp_action_generation=4)
        self.make()
        prefs = self.store.snapshot()["preferences"]
        self.assertTrue(self.store.get(iid)["request_mcp_auto_publish"])
        self.assertEqual(4, prefs["mcp_action_generation"])
        self.assertFalse(prefs.get("pipeline_restart_notice"))

    def test_restart_still_pauses_without_takeover(self):
        iid = self.add_confirmed()
        self.store.update_preferences(mcp_website_actions=False, pipeline_enabled=True, mcp_action_generation=4)
        self.make()
        self.assertFalse(self.store.get(iid)["request_mcp_auto_publish"])
        self.assertEqual(5, self.store.snapshot()["preferences"]["mcp_action_generation"])
        self.assertTrue(self.store.snapshot()["preferences"]["pipeline_restart_notice"])


class TickTests(PipelineBase):
    def test_paper_confirmed_in_the_app_is_published_without_the_switches(self):
        pipeline = self.make()
        iid = self.store.add("t", "10.1234/t", account_id="default", request_confirmed=True, request_reward=10, oa_check=dict(status="not_oa"))
        self.assertFalse(pipeline.enabled())
        planned = pipeline.tick_mcp_authorized()
        self.assertEqual(("publish", iid), (planned["action"], planned["item_id"]))
        self.assertTrue(self.store.get(iid)["request_mcp_auto_publish"])

    def test_without_takeover_an_app_confirmed_paper_waits(self):
        self.store.update_preferences(smart_mode=False)
        pipeline = self.make()
        self.store.add("t", "10.1234/t", account_id="default", request_confirmed=True, request_reward=10, oa_check=dict(status="not_oa"))
        self.assertIsNone(pipeline.tick_mcp_authorized())
        self.assertEqual([], self.fake.enqueued)

    def test_answer_to_a_request_made_on_the_site_is_downloaded_first(self):
        pipeline = self.make()
        self.store.add("q", "10.1234/q", account_id="default", request_confirmed=True, request_reward=10, oa_check=dict(status="not_oa"))
        site = self.store.add("s", "", source="website", owned=True, account_id="default", website_status="待确认",
                              assist_url="https://www.ablesci.com/assist/detail?id=x", fulfilled_at="2026-11-02T23:36:50+08:00")
        planned = pipeline.tick_mcp_authorized()
        self.assertEqual(("download", site), (planned["action"], planned["item_id"]))

    def test_blocked_paper_is_not_resumed(self):
        pipeline = self.make()
        iid = self.store.add("t", "10.1234/t", account_id="default", request_confirmed=True, request_reward=10, status="attention",
                             publish_blocked=True, publish_check=dict(kind="min_reward_raised"))
        self.assertIsNone(pipeline.tick_mcp_authorized())
        self.assertFalse(self.store.get(iid).get("request_mcp_auto_publish"))


class AutoConfirmTests(PipelineBase):
    prefs = takeover(auto=10)

    def batch(self, ids, screened=None):
        intake.create_batch(self.store, "b1", "default", ids)
        intake.mark(self.store, "b1", "default", screening_done_at=screened or datetime.now().astimezone().isoformat(timespec="seconds"))

    def add(self, name, **fields):
        return self.store.add(name, "10.1234/" + name, account_id="default", oa_check=dict(status="not_oa"), batch_id="b1", **fields)

    def test_papers_within_the_amount_are_confirmed_once(self):
        pipeline = self.make()
        pipeline.notifier = MagicMock()
        a, b = self.add("a"), self.add("b", request_reward=20)
        self.batch([a, b])
        self.assertEqual([a], pipeline.ai_auto_confirm())
        self.assertTrue(self.store.get(a)["request_confirmed"])
        self.assertTrue(self.store.get(a)["request_mcp_auto_publish"])
        self.assertNotEqual(True, self.store.get(b).get("request_confirmed"))
        self.assertEqual("min_bounty", pipeline.notifier.notify.call_args.args[0])   # the dearer one is asked about
        self.assertEqual([], pipeline.ai_auto_confirm())                            # each batch only once

    def test_batches_from_before_the_amount_was_set_are_left_alone(self):
        self.store.update_preferences(**takeover(auto=10, since=datetime.now().astimezone().isoformat(timespec="seconds")))
        pipeline = self.make()
        a = self.add("a")
        self.batch([a], screened=(datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec="seconds"))
        self.assertEqual([], pipeline.ai_auto_confirm())
        self.assertNotEqual(True, self.store.get(a).get("request_confirmed"))

    def test_reserve_check_still_applies(self):
        self.store.update_account("default", points=15)
        self.store.update_preferences(publish_reserve_points=10)
        pipeline = self.make()
        pipeline.notifier = MagicMock()
        a = self.add("a")
        self.batch([a])
        self.assertEqual([], pipeline.ai_auto_confirm())
        self.assertNotEqual(True, self.store.get(a).get("request_confirmed"))
        self.assertIn("可用额度", pipeline.notifier.notify.call_args.args[3])

    def test_amount_zero_means_always_ask(self):
        self.store.update_preferences(**takeover())
        pipeline = self.make()
        a = self.add("a")
        self.batch([a])
        self.assertEqual([], pipeline.ai_auto_confirm())


class ScheduleTests(unittest.TestCase):
    def interval(self, minutes, failures=0):
        now = datetime(2026, 11, 9, 12, 0).astimezone()
        published = (now - timedelta(minutes=minutes)).isoformat()
        items = [dict(id="w", source="website", website_status="求助中", publish_action=dict(status="confirmed", time=published))]
        return mon.choose_interval(items, {}, now.timestamp(), failures, takeover=True)["seconds"]

    def test_one_minute_five_times_then_five_minutes_five_times_then_slower(self):
        self.assertEqual([60, 60, 300, 300, 600, 900], [self.interval(m) for m in (0, 4, 5, 29, 31, 180)])

    def test_failures_back_off_to_fifteen_minutes(self):
        self.assertEqual(120, self.interval(1, failures=1))
        self.assertEqual(900, self.interval(1, failures=6))
        self.assertEqual(900, self.interval(180, failures=6))

    def test_nothing_waiting_keeps_the_idle_interval(self):
        self.assertEqual("idle", mon.choose_interval([], {}, 0, 0, takeover=True)["band"])


class NotifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.store.update_preferences(**takeover())
        self.store.mcp_last_call = __import__("time").time()   # an AI is connected right now
        self.shown = []
        self.notifier = Notifier(self.store, sink=lambda t, m: self.shown.append(t))

    def test_nothing_pops_up_results_go_to_the_inbox(self):
        self.assertEqual("ai", self.notifier.notify("applied", "x", "有人应助", "m1"))
        self.assertEqual("ai", self.notifier.notify("download_ok", "x", "下载完成", "m2"))
        self.assertEqual("ai", self.notifier.notify("min_bounty", "batch:b1", "需要你确认积分", "m3"))
        self.assertEqual([], self.shown)
        inbox = self.notifier.inbox()
        self.assertEqual([("download_ok", "x", ""), ("min_bounty", "", "b1")], [(e["kind"], e["item_id"], e["batch_id"]) for e in inbox])
        self.assertEqual(1, self.notifier.ack([inbox[0]["seq"]]))
        self.assertEqual(["m3"], [e["message"] for e in self.notifier.inbox()])
        self.assertEqual(1, self.notifier.ack())
        self.assertEqual([], self.notifier.inbox())
        self.assertEqual(2, len(self.notifier.inbox(include_acked=True)))

    def test_gui_popup_switches_do_not_mute_the_inbox(self):
        self.store.update_preferences(notify_master=False, notify_items=dict(oa_ok=False))
        self.assertEqual("ai", self.notifier.notify("oa_ok", "y", "免费全文已下载", "《Y》已下载免费全文并核对 DOI 与标题一致。"))
        self.assertEqual(["oa_ok"], [e["kind"] for e in self.notifier.inbox()])
        self.assertEqual([], self.shown)

    def test_opting_out_of_silent_mode_shows_everything(self):
        self.store.update_preferences(mcp_silent=False)
        self.assertEqual("sent", self.notifier.notify("applied", "x", "有人应助", "m"))


class FakeClient:
    def __init__(self):
        self.posts = []
        self.smart = dict(enabled=False, raise_max=50, reserve=50, daily_cap=200, auto_confirm_max=0, spent_today=0)

    def state(self):
        return dict(settings=dict(smart=self.smart, ai_takeover=self.smart["enabled"]))

    def post(self, path, body=None):
        self.posts.append((path, body))
        if "smart_mode" not in body:
            return dict(ok=True)
        mode = body["smart_mode"]
        self.smart = dict(self.smart, enabled=mode["enabled"], **{k: v for k, v in mode.items() if k in ("auto_confirm_max", "daily_cap")})
        return dict(ok=True)


class CliTests(unittest.TestCase):
    def run_cli(self, client, *argv):
        import contextlib
        import io
        import json
        import kytdl
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = kytdl.main(list(argv) + ["--json"], client)
        return code, json.loads(out.getvalue())

    def test_on_needs_confirmation_and_shows_the_rules(self):
        client = FakeClient()
        code, data = self.run_cli(client, "ai", "on", "--auto-max", "10")
        self.assertEqual(1, code)
        self.assertIn("10 积分", data["rules"])
        self.assertEqual([], client.posts)
        code, data = self.run_cli(client, "ai", "on", "--auto-max", "10", "--confirmed")
        self.assertEqual(0, code)
        self.assertTrue(data["ai_takeover"])
        self.assertEqual(dict(enabled=True, confirmed=True, raise_max=50, reserve=50, daily_cap=200, auto_confirm_max=10), client.posts[-2][1]["smart_mode"])
        self.assertEqual({"disclaimer_ack": True}, client.posts[-1][1])
        code, data = self.run_cli(client, "ai", "off")
        self.assertEqual((0, False), (code, data["ai_takeover"]))


class AiVerifyTests(PipelineBase):
    """Step 3: the AI reads the PDF; a matching DOI/title on the same file lets the background flow adopt."""

    def setUp(self):
        super().setUp()
        import hashlib
        from mcp_api import McpApi
        folder = Path(self.temp.name) / "downloads"
        folder.mkdir(exist_ok=True)
        (folder / "p.pdf").write_bytes(b"%PDF-1.4 fixture")
        self.sha = hashlib.sha256(b"%PDF-1.4 fixture").hexdigest()
        self.iid = self.store.add("CRISPR-Cas systems of lactic acid bacteria", "10.1016/j.biotechadv.2024.108323", source="website",
                                  owned=True, account_id="default", website_status="待确认", status="downloaded",
                                  assist_url="https://www.ablesci.com/assist/detail?id=c", pdf="downloads/p.pdf",
                                  request_confirmed=True, request_mcp_auto_publish=True, request_reward=10,
                                  metadata_ref=dict(title="CRISPR-Cas systems of lactic acid bacteria and applications in food science"),
                                  verification=dict(status="review", sha256=self.sha, bibliography=dict(status="partial")))
        self.pipeline = self.make()
        self.api = McpApi(self.store, Accounts(self.temp.name), self.pipeline, None, lambda: True)

    def verify(self, **body):
        base = dict(id=self.iid, doi="10.1016/j.biotechadv.2024.108323",
                    title="CRISPR-Cas systems of lactic acid bacteria and applications in food science", confirmed=True)
        return self.api.handle("ai-verify", dict(base, **body))

    def test_pass_lets_the_background_flow_adopt(self):
        from pipeline import ai_adopt_ok
        self.assertTrue(self.verify()["passed"])
        self.assertTrue(ai_adopt_ok(self.store.get(self.iid)))
        planned = self.pipeline.tick_mcp_authorized()
        self.assertEqual(("adopt", self.iid), (planned["action"], planned["item_id"]))

    def test_wrong_doi_or_title_fails(self):
        from pipeline import ai_adopt_ok
        self.assertFalse(self.verify(doi="10.1016/j.other.2024.1")["passed"])
        self.assertFalse(ai_adopt_ok(self.store.get(self.iid)))
        self.assertFalse(self.verify(title="Something else entirely")["passed"])
        self.assertIsNone(self.pipeline.tick_mcp_authorized())

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.verify(confirmed=False)
        (Path(self.temp.name) / "downloads" / "p.pdf").write_bytes(b"%PDF-1.4 changed")
        with self.assertRaises(ValueError):
            self.verify()
        self.store.update_preferences(smart_mode=False)
        with self.assertRaises(ValueError):
            self.verify()

    def test_bibliographic_mismatch_is_never_overridden(self):
        from pipeline import ai_adopt_ok
        self.store.update(self.iid, verification=dict(status="review", sha256=self.sha, bibliography=dict(status="mismatch")))
        self.assertFalse(self.verify()["passed"])
        self.assertFalse(ai_adopt_ok(self.store.get(self.iid)))

    def test_inbox_through_the_api(self):
        notifier = Notifier(self.store)
        self.pipeline.notifier = notifier
        notifier.notify("download_ok", self.iid, "下载完成", "done")
        reply = self.api.handle("inbox", {"ack": True})
        self.assertEqual(["done"], [e["message"] for e in reply["entries"]])
        self.assertEqual(0, self.api.handle("inbox", {})["unread"])


class ScanTests(unittest.TestCase):
    def setUp(self):
        from tests.helpers import FakeAccounts, MonitorAgent, deciding
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.store.update_preferences(**takeover())
        self.notifier = MagicMock()
        self.monitor = mon.Monitor(self.store, FakeAccounts(), MonitorAgent(), notifier=self.notifier, start=False)
        self.deciding = deciding

    def kinds(self):
        return [c.args[0] for c in self.notifier.notify.call_args_list]

    def test_no_notice_before_the_automatic_adoption_one_after(self):
        item = self.deciding("d", request_confirmed=True, request_mcp_auto_publish=True)
        self.monitor.scan([item])
        self.assertNotIn("download_ok", self.kinds())
        done = dict(item, website_status="已完结", status="adopted", adopt_action=dict(status="confirmed", auto=True))
        self.monitor.scan([done])
        self.assertIn("已自动采纳", self.notifier.notify.call_args.args[3])

    def test_manual_adoption_is_not_announced_again(self):
        item = self.deciding("d", website_status="已完结", status="adopted", adopt_action=dict(status="confirmed"))
        self.monitor.scan([item])
        self.assertNotIn("download_ok", self.kinds())


class FeeBookkeepingTests(unittest.TestCase):
    def agent(self, points, minutes_ago):
        from agent_download import DownloadMixin
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        holder = DownloadMixin()
        holder.store, holder.accounts = Store(temp.name), MagicMock(active_id="default")
        synced = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat()
        holder.store.update_account("default", points=points, last_sync_time=synced)
        return holder

    def test_fresh_balance_well_above_500_is_free(self):
        self.assertTrue(self.agent(952, 1).recent_balance_at_least(510))
        self.assertFalse(self.agent(952, 30).recent_balance_at_least(510))   # stale sync
        self.assertFalse(self.agent(505, 1).recent_balance_at_least(510))    # too close to 500


class CliFlowTests(unittest.TestCase):
    def test_get_row(self):
        import kytdl
        rows = {}
        self.assertEqual(("done", True), kytdl.get_row(dict(id="a", stage="done"), None, rows, 0))
        self.assertEqual(("needs_points", True), kytdl.get_row(dict(id="a", stage="queued"), dict(phase="awaiting_confirmation"), rows, 0))
        self.assertEqual(("screening", False), kytdl.get_row(dict(id="a", stage="queued"), dict(phase="screening"), rows, 0))
        self.assertEqual(("waiting_answer", False), kytdl.get_row(dict(id="a", stage="submitted", request_confirmed=True), None, rows, 0))
        self.assertEqual(("adopting", False), kytdl.get_row(dict(id="b", stage="awaiting_decision"), None, rows, 10))
        self.assertEqual(("needs_check", True), kytdl.get_row(dict(id="b", stage="awaiting_decision"), None, rows, 10 + kytdl.DECISION_GRACE))
        self.assertEqual(("needs_person", True), kytdl.get_row(dict(id="c", stage="attention"), None, rows, 0))

    def test_confirm_previews_unless_confirmed(self):
        import contextlib
        import io
        import json
        import kytdl

        class Client:
            def __init__(self):
                self.posts = []

            def state(self):
                return dict(items=[], batches=[])

            def post(self, path, body=None):
                self.posts.append(path)
                if path.endswith("confirm-preview"):
                    return dict(items=[dict(id="a", title="T", doi="10.1/a", reward=20)], estimated_points=20, points_now=900, confirm_token="tok")
                return dict(ok=True, confirmed=1, estimated_points=20)

        client = Client()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, kytdl.main(["confirm", "b1", "--json"], client))
        self.assertIn("20 积分", json.loads(out.getvalue())["error"])
        self.assertEqual(["/api/mcp/confirm-preview"], client.posts)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, kytdl.main(["confirm", "b1", "--confirmed", "--json"], client))
        self.assertEqual("/api/mcp/confirm", client.posts[-1])


class DuplicateRequestTests(PipelineBase):
    """Real 2026-11-09: re-importing a DOI that had already been requested, downloaded and adopted created a second
    queue item, which AI takeover then confirmed and tried to publish again (10 points)."""
    prefs = takeover(auto=10)

    def test_import_of_an_already_requested_doi_is_a_duplicate(self):
        done = self.store.add("CRISPR", "10.1016/j.biotechadv.2024.108323", source="website", owned=True, account_id="default",
                              website_status="已完结", assist_url="https://www.ablesci.com/assist/detail?id=c")
        again = self.store.add("", "https://doi.org/10.1016/J.BIOTECHADV.2024.108323", account_id="default", batch_id="b2")
        self.assertEqual(done, again)
        other = self.store.add("", "10.1016/j.biotechadv.2024.108323", account_id="someone-else")
        self.assertNotEqual(done, other)   # another account is a different request

    def test_auto_confirm_skips_a_doi_the_account_already_has(self):
        pipeline = self.make()
        self.store.add("CRISPR", "10.1016/j.biotechadv.2024.108323", source="website", owned=True, account_id="default",
                       website_status="已完结", assist_url="https://www.ablesci.com/assist/detail?id=c")
        # an item that slipped in some other way (e.g. created before the Store.add fix)
        self.store.data["items"].append(dict(id="dup", title="CRISPR", doi="10.1016/j.biotechadv.2024.108323", source="import",
                                             status="ready", created="2026-11-09T03:03:23+08:00", account_id="default",
                                             batch_id="b1", oa_check=dict(status="not_oa"), verification=None))
        intake.create_batch(self.store, "b1", "default", ["dup"])
        intake.mark(self.store, "b1", "default", screening_done_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        self.assertEqual([], pipeline.ai_auto_confirm())
        self.assertNotEqual(True, self.store.get("dup").get("request_confirmed"))


class CliExtraTests(unittest.TestCase):
    def test_pages_login_sync_post_the_right_routes(self):
        import contextlib
        import io
        import kytdl

        class Client:
            def __init__(self):
                self.posts = []

            def post(self, path, body=None):
                self.posts.append((path, body))
                if path == "/api/mcp/render":
                    return dict(id="a", pages=[dict(page=1, path="x.png", width=1, height=1)])
                return dict(ok=True)

        client = Client()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, kytdl.main(["pages", "a", "--pages", "1,2", "--json"], client))
            self.assertEqual(0, kytdl.main(["login", "--json"], client))
            self.assertEqual(0, kytdl.main(["sync", "--json"], client))
        self.assertEqual([("/api/mcp/render", {"id": "a", "pages": [1, 2]}),
                          ("/api/browser", {"action": "login", "use_saved": True}),
                          ("/api/monitor", {"sync_now": True})], client.posts)


class LockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / ".kytdl.lock"

    def lock(self, pid, who):
        return InstanceLock(self.path, pid=pid, alive=lambda p: True, identity=lambda p: who.get(p))

    def test_reused_pid_is_stale(self):
        self.lock(53748, {53748: ("python.exe", "100")}).acquire()
        # the service died; msedge.exe now holds PID 53748 with a different start time
        taken = self.lock(60000, {53748: ("msedge.exe", "999"), 60000: ("python.exe", "200")}).acquire()
        self.assertTrue(taken.held)
        self.assertEqual(60000, taken.owner())

    def test_live_service_still_blocks(self):
        self.lock(53748, {53748: ("pythonw.exe", "100")}).acquire()
        with self.assertRaises(AlreadyRunning):
            self.lock(60000, {53748: ("pythonw.exe", "100"), 60000: ("python.exe", "200")}).acquire()

    def test_old_pid_only_lock(self):
        self.path.write_text("53748", "ascii")
        self.assertTrue(self.lock(60000, {53748: ("msedge.exe", "999")}).acquire().held)
        self.path.write_text("53748", "ascii")
        with self.assertRaises(AlreadyRunning):
            self.lock(60001, {53748: ("python.exe", "999")}).acquire()

    def test_unknown_identity_is_treated_as_running(self):
        self.lock(53748, {53748: ("python.exe", "100")}).acquire()
        with self.assertRaises(AlreadyRunning):
            self.lock(60000, {}).acquire()

    def test_real_process_identity(self):
        import os
        from instance_lock import process_identity
        own = process_identity(os.getpid())
        self.assertIsNotNone(own)
        self.assertIn("python", own[0])
        self.assertIsNone(process_identity(0))


if __name__ == "__main__":
    unittest.main()


class SimpleSetupTests(unittest.TestCase):
    """2026-11-09: the AI version needs (almost) no settings — `kytdl ai on` is the one setup step."""
    def test_ai_on_proposes_ten_points_when_nothing_is_stored(self):
        client = FakeClient()
        code, data = CliTests.run_cli(self, client, "ai", "on")
        self.assertEqual(1, code)
        self.assertIn("不超过 10 积分", data["rules"])
        self.assertEqual([], client.posts)          # still nothing without --confirmed

    def test_publisher_check_never_stops_or_opens_a_window_under_takeover(self):
        self.assertEqual("skip", settings.oa_challenge_mode(dict(takeover(), oa_challenge_mode="popup"), "default"))
        self.assertEqual("skip", settings.oa_challenge_mode(takeover(), "default"))
        self.assertEqual("prompt", settings.oa_challenge_mode({}, "default"))
        self.assertEqual("popup", settings.oa_challenge_mode(dict(oa_challenge_mode="popup"), "other"))

    def test_status_lists_only_real_blockers(self):
        import kytdl

        class Client:
            def __init__(self, on, attention):
                self.on, self.attention = on, attention

            def post(self, path, body=None):
                assert path == "/api/mcp/needs-attention"
                return self.attention

        attention = dict(account=[dict(kind="login_required", reason="科研通登录已失效")],
                         setup=[dict(kind="oa_contact_email", reason="x", optional=True), dict(kind="zotero_local_api_off", reason="z")])
        rows = kytdl.needs_user(Client(True, attention), dict(settings=dict(ai_takeover=True)))
        self.assertEqual(["login_required", "zotero_local_api_off"], [r["kind"] for r in rows])
        self.assertEqual("kytdl login", rows[0]["next"])
        rows = kytdl.needs_user(Client(False, dict(account=[], setup=[dict(kind="smart_mode_off", reason="")])), dict(settings={}))
        self.assertEqual(["ai_takeover_off"], [r["kind"] for r in rows])


class Utf8OutputTests(unittest.TestCase):
    def test_json_output_survives_a_gbk_pipe(self):
        """Real 2026-11-09: `kytdl search ... --json > file` crashed on a title containing 'ł' (GBK console)."""
        import io
        import json
        import sys
        from unittest.mock import patch
        import kytdl
        fake = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with patch.object(sys, "stdout", fake):
            text = kytdl.json_text(dict(title="Łódź 研究 \U0001F600"))
        text.encode("gbk")                                        # writable to the console as is
        self.assertEqual("Łódź 研究 \U0001F600", json.loads(text)["title"])   # and still the same data


class RealRun3Tests(unittest.TestCase):
    """Real run 2026-11-09 05:31 (three papers): what it showed and what changed."""
    def test_a_stored_ask_does_not_stop_a_raise_within_the_agreed_amount(self):
        prefs = dict(takeover(auto=10), publish_raise_mode="ask")
        out = settings.raise_settings_for(prefs, "default", 30)
        self.assertEqual(("auto", 30), (out["mode"], out["max"]))
        self.assertEqual(10, settings.raise_settings_for(prefs, "default", 10)["max"])     # 30 asked for a 10-point paper -> stops
        self.assertEqual("ask", settings.raise_settings_for(dict(prefs, smart_mode=False), "default", 30)["mode"])

    def oa_item(self, **v):
        verification = dict(status="review", primary_dois=["10.1016/j.ijfoodmicro.2025.111436"], title_score=100.0,
                            bibliography=dict(status="unverified", title_status="match", author_status="partial", abstract_status="match"))
        verification.update(v)
        return dict(id="o", doi="10.1016/j.ijfoodmicro.2025.111436", source="import", status="oa_review", pdf="oa/o.pdf",
                    verification=verification)

    def test_free_file_with_matching_doi_and_title_is_kept(self):
        from pipeline import oa_auto_keep_ok
        self.assertTrue(oa_auto_keep_ok(self.oa_item()))
        self.assertFalse(oa_auto_keep_ok(self.oa_item(status="conflict", primary_dois=["10.4014/jmb.2203.03018"])))
        self.assertFalse(oa_auto_keep_ok(self.oa_item(primary_dois=[])))
        self.assertFalse(oa_auto_keep_ok(self.oa_item(title_score=70.0)))
        self.assertFalse(oa_auto_keep_ok(self.oa_item(bibliography=dict(status="mismatch", title_status="match"))))
        self.assertFalse(oa_auto_keep_ok(self.oa_item(bibliography=dict(status="unverified", title_status="match", author_status="mismatch"))))

    def test_get_waits_for_the_background_auto_confirm(self):
        import kytdl
        item = dict(id="x", stage="queued", request_reward=10)
        batch = dict(phase="awaiting_confirmation")
        self.assertEqual(("screening", False), kytdl.get_row(item, batch, {}, 0, auto=10))
        self.assertEqual(("needs_points", True), kytdl.get_row(item, dict(batch, ai_auto=True), {}, 0, auto=10))
        self.assertEqual(("needs_points", True), kytdl.get_row(dict(item, request_reward=30), batch, {}, 0, auto=10))
        self.assertEqual(("needs_points", True), kytdl.get_row(item, batch, {}, 0))

    def test_resolve_needs_confirmation_and_posts_the_right_route(self):
        import contextlib
        import io
        import kytdl

        class Client:
            posts = []

            def post(self, path, body=None):
                self.posts.append((path, body))
                return dict(ok=True)

        client = Client()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, kytdl.main(["resolve", "a", "keep", "--json"], client))
            self.assertEqual(0, kytdl.main(["resolve", "a", "keep", "--confirmed", "--json"], client))
            self.assertEqual(0, kytdl.main(["resolve", "a", "request", "--confirmed", "--json"], client))
        self.assertEqual([("/api/oa-accept", {"id": "a", "confirmed": True}), ("/api/oa-discard", {"id": "a", "confirmed": True})], client.posts)


class InboxDedupTests(unittest.TestCase):
    def test_the_same_stop_is_listed_once(self):
        n = Notifier(Store(tempfile.mkdtemp()))
        reason = "该文献要求的最低悬赏为 30 积分，需用户决定"
        n.to_inbox("min_bounty", "publish", "最低悬赏", reason)
        n.to_inbox("needs_action", "abc", "需处理", "《X》需处理：" + reason)
        rows = n.inbox()
        self.assertEqual(1, len(rows))
        self.assertEqual(("abc", "needs_action"), (rows[0]["item_id"], rows[0]["kind"]))
        n.to_inbox("download_ok", "abc", "下载完成", "《X》已下载并核对通过，已自动采纳。")
        self.assertEqual(["download_ok"], [e["kind"] for e in n.inbox()])   # the stop was resolved by the result


class AcquisitionLabelTests(unittest.TestCase):
    def test_an_accepted_partial_match_file_is_reported_as_obtained(self):
        from pipeline import acquisition_state
        kept = dict(id="o", source="import", status="checked", done_via="oa", pdf="oa/o.pdf", verification=dict(status="review"))
        self.assertEqual("file_available", acquisition_state(kept)["code"])
        waiting = dict(kept, status="oa_review", done_via=None)
        self.assertEqual("file_needs_review", acquisition_state(waiting)["code"])


class InboxSupersedeTests(unittest.TestCase):
    def test_a_final_result_clears_earlier_problems_for_that_paper(self):
        n = Notifier(Store(tempfile.mkdtemp()))
        n.to_inbox("needs_action", "abc", "需处理", "《X》核对未通过或需确认，请采纳或驳回。")
        n.to_inbox("needs_action", "other", "需处理", "《Y》需处理：发布结果未确认。请在科研通查看")
        n.to_inbox("download_ok", "abc", "下载完成", "《X》已下载并核对通过，已自动采纳。")
        self.assertEqual(["other", "abc"], [e["item_id"] for e in n.inbox()])
        self.assertEqual("download_ok", n.inbox()[-1]["kind"])
