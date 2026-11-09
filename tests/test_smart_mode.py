"""Smart mode (settings.py): one switch with snapshot/restore and a daily spending cap. Temp data only; nothing contacts ablesci.com."""
import tempfile
import unittest
from datetime import datetime, timezone

import publish_policy as pp
import settings
from pipeline import next_step
from tests import helpers as hp
from tests.helpers import PublishBase, lookup_text

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ON = dict(smart_mode=True, smart_rules=settings.SMART_RULES, smart_config=dict(raise_max=50, reserve=50, daily_cap=30))


class PresetTests(unittest.TestCase):
    def test_off_by_default_and_needs_rules(self):
        self.assertFalse(settings.smart({})["enabled"])
        self.assertFalse(settings.smart(dict(smart_mode=True))["enabled"])
        self.assertTrue(settings.smart(ON)["enabled"])
        self.assertEqual(200, settings.smart(dict(ON, smart_config=dict(daily_cap=99999)))["daily_cap"])   # damaged -> default

    def test_validation(self):
        with self.assertRaises(ValueError):
            settings.validate_smart({"enabled": True})                       # must be confirmed
        for bad in ({"raise_max": 70}, {"reserve": -1}, {"daily_cap": 5}, {"daily_cap": True}, {"other": 1}):
            with self.assertRaises(ValueError):
                settings.validate_smart(dict(enabled=True, confirmed=True, **bad))
        self.assertEqual(dict(raise_max=100, reserve=0, daily_cap=500),
                         settings.validate_smart(dict(enabled=True, confirmed=True, raise_max=100, reserve=0, daily_cap=500)))
        self.assertEqual(settings.DEFAULTS, settings.validate_smart({"enabled": False}))

    def test_on_sets_everything_and_off_restores(self):
        before = dict(publish_raise_mode="ask", publish_raise_max=30, default_reward=20, monitor_enabled=False, mcp_website_actions=False)
        on = settings.smart_on(before, "acct", settings.DEFAULTS)
        self.assertEqual(("auto", pp.RAISE_RULES_TEXT, 50, 50, 10, True, True, "acct", True),
                         tuple(on[k] for k in ("publish_raise_mode", "publish_raise_rules", "publish_raise_max", "publish_reserve_points",
                                               "default_reward", "monitor_enabled", "mcp_website_actions", "mcp_actions_account", "mcp_silent")))
        merged = dict(before, **on)
        again = settings.smart_on(merged, "acct", dict(settings.DEFAULTS, raise_max=100))
        self.assertNotIn("smart_snapshot", again)                              # re-saving keeps the original snapshot
        off = settings.smart_off(dict(merged, **again))
        self.assertEqual(("ask", 30, 20, False, False, None), tuple(off[k] for k in ("publish_raise_mode", "publish_raise_max", "default_reward",
                                                                                     "monitor_enabled", "mcp_website_actions", "publish_reserve_points")))
        self.assertFalse(off["smart_mode"])


class SpendCapTests(unittest.TestCase):
    def test_ledger_and_cap(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from core import Store
        store = Store(temp.name)
        settings.record_spend(store, "default", 10, NOW)
        settings.record_spend(store, "default", 2, NOW)
        settings.record_spend(store, "default", 0, NOW)
        account = store.snapshot()["account_metrics"]["default"]
        self.assertEqual(12, settings.spent_today(account, NOW))
        self.assertEqual("", settings.cap_block(ON, account, 18, NOW))
        self.assertIn("每日上限 30", settings.cap_block(ON, account, 20, NOW))
        self.assertEqual("", settings.cap_block({}, account, 9999, NOW))         # no cap without smart mode

    def test_planner_holds_publishing_at_the_cap(self):
        prefs = dict(hp.AUTO, oa_first=False, **ON)
        account = dict(points=900, spend_log={"2026-11-02": 25})
        step = next_step([hp.queued(request_reward=10)], account, prefs, NOW)
        self.assertEqual(("wait_user", "已达每日花费上限"), (step["action"], step["label"]))
        self.assertEqual("publish", next_step([hp.queued(request_reward=10)], dict(account, spend_log={}), prefs, NOW)["action"])
        self.assertEqual("publish", next_step([hp.queued(request_reward=10)], account, dict(hp.AUTO, oa_first=False), NOW)["action"])


class PublishCapTests(PublishBase):
    def test_submit_is_refused_when_the_final_reward_passes_the_cap(self):
        self.store.update_preferences(**ON)
        self.store.update_account(account_id="default", points=100, spend_log={datetime.now().astimezone().strftime("%Y-%m-%d"): 25})
        item_id = self.item()
        self.script(text=lookup_text(minimum=10), response=dict(code=0))
        self.assertIn("每日上限", self.run_publish(item_id))
        self.assertEqual(0, self.page.submit.clicks)
        self.assertNotEqual("attention", self.store.get(item_id)["status"])   # nothing stopped for good: it waits

    def test_successful_publish_is_recorded(self):
        self.store.update_preferences(**dict(ON, smart_config=dict(daily_cap=200)))
        item_id = self.item()
        self.script(text=lookup_text(minimum=10), response=dict(code=0))
        self.assertIsNone(self.run_publish(item_id))
        self.assertEqual(10, settings.spent_today(self.store.snapshot()["account_metrics"]["default"]))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = hp.start(self.temp.name)
        self.addCleanup(hp.stop, self.server)
        from kytdl_client import Client
        self.client = Client("http://127.0.0.1:%d" % self.server.server_port, token_path=self.server.store.root / ".token")

    def prefs(self):
        return self.server.store.snapshot()["preferences"]

    def test_switch_on_and_off_through_the_api(self):
        from kytdl_client import ApiError
        with self.assertRaises(ApiError):
            self.client.post("/api/preferences", {"smart_mode": {"enabled": True}})
        self.assertIn("smart_mode_off", {h["kind"] for h in self.server.mcp_api.do_needs_attention({})["setup"]})
        reply = self.client.post("/api/preferences", {"smart_mode": {"enabled": True, "confirmed": True, "daily_cap": 300}})
        self.assertTrue(reply["smart"]["enabled"])
        prefs = self.prefs()
        self.assertEqual((True, "default", "auto", True), (prefs["mcp_website_actions"], prefs["mcp_actions_account"], prefs["publish_raise_mode"], prefs["monitor_enabled"]))
        self.assertTrue(self.client.get("/api/state")["settings"]["smart"]["enabled"])
        self.assertNotIn("smart_mode_off", {h["kind"] for h in self.server.mcp_api.do_needs_attention({})["setup"]})
        self.client.post("/api/preferences", {"smart_mode": {"enabled": False}})
        prefs = self.prefs()
        self.assertEqual((False, None, False), (prefs["smart_mode"], prefs.get("mcp_website_actions"), prefs["monitor_enabled"]))

    def test_cap_reached_is_reported_to_the_ai(self):
        self.client.post("/api/preferences", {"smart_mode": {"enabled": True, "confirmed": True, "daily_cap": 10}})
        settings.record_spend(self.server.store, "default", 10)
        self.assertIn("daily_cap_reached", {a["kind"] for a in self.server.mcp_api.do_needs_attention({})["account"]})


if __name__ == "__main__":
    unittest.main()
