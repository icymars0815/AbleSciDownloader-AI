import unittest

from kytdl_client import item_states


class OaWaitStateTests(unittest.TestCase):
    def test_old_human_check_does_not_end_wait_during_retry(self):
        paper = {"id": "p1", "stage": "queued", "oa_check": {"status": "needs_human"}}
        running = item_states(paper, {"pipeline": {"oa_running": "p1"}})
        self.assertNotIn("needs_human", running)
        self.assertNotIn("oa_finished", running)
        finished = item_states(paper, {"pipeline": {"oa_running": None}})
        self.assertIn("needs_human", finished)
        self.assertIn("oa_finished", finished)

    def test_confirmed_site_request_does_not_stop_for_oa_captcha(self):
        paper = {"id": "p1", "stage": "queued", "request_confirmed": True, "oa_check": {"status": "needs_human"}}
        self.assertNotIn("needs_human", item_states(paper, {"pipeline": {"oa_running": None}}))


class PublishingWaitStateTests(unittest.TestCase):
    # Real 2026-11-02: wait_for stopped with needs_person 2 s into a publish that then succeeded.
    def paper(self):
        return {"id": "p1", "stage": "attention", "publish_action": {"status": "attempted"}, "publish_check": {"kind": "submitting"}}

    def test_submit_in_flight_is_publishing(self):
        self.assertEqual({"publishing"}, item_states(self.paper(), {"busy": True}))

    def test_attempt_left_behind_still_needs_a_person(self):
        self.assertIn("attention", item_states(self.paper(), {"busy": False}))
        stuck = dict(self.paper(), publish_check={"kind": "uncertain"})
        self.assertIn("attention", item_states(stuck, {"busy": True}))
