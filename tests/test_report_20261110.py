"""Fixes for the CLI test report of 2026-11-10 (11 real papers, 6 stopped at a manual check). Temp data, fictional titles."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ablesci.cli import kytdl
from ablesci.flow import intake
from ablesci.flow.core import Store
from ablesci.flow.pipeline import ai_review_task, local_match_ok, oa_auto_keep_ok, preprint_doi
from ablesci.website import publish_policy as pp

DOI = "10.1111/j.1365-2796.2005.01528.x"
TITLE = "A fictional broad-spectrum lipid drug: a fiftieth anniversary review"
CITATION = "Carlton LA. " + TITLE + ". J Fict Med. 2005;258(2):94-114. DOI:"


def answered(**v):
    verification = dict(status="review", sha256="abc", title_score=100.0, primary_dois=[],
                        bibliography=dict(status="match", title_status="match", author_status="match", abstract_status="match"))
    verification.update(v)
    return dict(id="w1", doi=DOI, title=TITLE, source="website", owned=True, assist_url="https://www.ablesci.com/assist/detail?id=x",
                website_status="待确认", status="downloaded", pdf="downloads/w1/a.pdf", request_confirmed=True,
                request_mcp_auto_publish=True, verification=verification,
                checked_at=(datetime.now().astimezone() - timedelta(minutes=3)).isoformat(timespec="seconds"))


class CitationTitleTests(unittest.TestCase):
    def test_a_whole_citation_is_reduced_to_the_registered_title(self):
        self.assertEqual(TITLE, pp.clean_cited_title(CITATION, TITLE))
        self.assertEqual("", pp.clean_cited_title(TITLE, TITLE))                       # already clean
        self.assertEqual("", pp.clean_cited_title("Something else entirely. J X. 2001.", TITLE))
        self.assertTrue(pp.title_matches(CITATION, TITLE))
        self.assertFalse(pp.title_matches("An invented citation about niacin", TITLE))

    def test_pipeline_keeps_the_citation_and_uses_the_clean_title(self):
        from tests.helpers import FakeAgent
        from ablesci.website.accounts import Accounts
        from ablesci.flow.pipeline import Pipeline
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            pipeline = Pipeline(store, Accounts(store.root), FakeAgent())
            iid = store.add(CITATION, DOI, account_id="default")
            record = dict(status="available", title=TITLE, authors=["L. A. Carlton"], year=2005, sources=["Crossref"])
            with patch("ablesci.flow.pipeline.fetch_metadata", return_value=record):
                pipeline.fill_from_metadata(iid)
            item = store.get(iid)
            self.assertEqual((TITLE, CITATION), (item["title"], item["cited_text"]))
            self.assertNotEqual(True, item.get("title_from_metadata"))      # the citation is still the person's input


class LocalMatchTests(unittest.TestCase):
    def test_full_bibliographic_match_without_a_printed_doi_is_accepted(self):
        self.assertTrue(local_match_ok(answered()))                                        # Carlson 2005 shape

    def test_target_doi_on_the_page_with_a_matching_bibliographic_title_is_accepted(self):
        item = answered(title_score=77.0, primary_dois=[DOI],
                        bibliography=dict(status="unverified", title_status="match", author_status="partial", abstract_status="review"))
        self.assertTrue(local_match_ok(item))                                              # Macia / Wang shape

    def test_weaker_evidence_goes_to_the_ai(self):
        self.assertFalse(local_match_ok(answered(primary_dois=["10.1101/2021.11.15.468729"])))   # another DOI (preprint)
        self.assertFalse(local_match_ok(answered(title_score=66.0, bibliography=dict(status="difference", title_status="match",
                                                                                      author_status="partial", abstract_status="difference"))))
        self.assertFalse(local_match_ok(answered(status="conflict")))
        self.assertFalse(local_match_ok(answered(sha256="")))
        self.assertFalse(local_match_ok(dict(answered(), adopt_action=dict(status="confirmed"))))

    def test_free_full_text_uses_the_same_rule(self):
        item = dict(answered(), source="import", owned=False, assist_url="", status="oa_review", website_status="")
        self.assertTrue(oa_auto_keep_ok(item))


class AiReviewTaskTests(unittest.TestCase):
    def test_uncertain_file_becomes_an_ai_task_with_next_steps_and_escalates_after_15_minutes(self):
        item = answered(title_score=60.0, bibliography=dict(status="unverified", title_status="review"))
        task = ai_review_task(item)
        self.assertEqual("pending_ai_review", task["kind"])
        self.assertIn("kytdl pages w1", task["next"])
        self.assertIn("kytdl verify w1", task["next"])
        self.assertFalse(task["escalate"])
        late = ai_review_task(item, datetime.now().astimezone() + timedelta(minutes=20))
        self.assertTrue(late["escalate"])
        self.assertIsNone(ai_review_task(answered()))                      # strong local match: no task, it is adopted

    def test_preprint_is_named(self):
        item = answered(status="conflict", primary_dois=["10.1101/2021.11.15.468729"])
        self.assertEqual("10.1101/2021.11.15.468729", preprint_doi(item))
        self.assertIn("预印本", ai_review_task(item)["hint"])

    def test_free_full_text_task_points_to_resolve(self):
        item = dict(answered(title_score=50.0, bibliography=dict(status="unverified", title_status="review")),
                    source="import", owned=False, assist_url="", status="oa_review", website_status="")
        self.assertIn("kytdl resolve w1 keep --confirmed", ai_review_task(item)["next"])


class BatchStepTests(unittest.TestCase):
    def batch(self, *items):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            ids = []
            for n, fields in enumerate(items):
                ids.append(store.add("Paper %d" % n, "10.1/p%d" % n, account_id="default", request_confirmed=True, request_batch_id="b",
                                     batch_id="b", **fields))
            intake.create_batch(store, "b", "default", ids)
            intake.mark(store, "b", "default", screening_done_at="2026-11-10T10:00:00+08:00", confirmed_at="2026-11-10T10:01:00+08:00")
            snap = store.snapshot()
            batch = next(b for b in snap["batches"] if b["id"] == "b")
            return intake.public_batch(batch, [i for i in snap["items"] if i["id"] in ids], {}, 500)

    def test_a_scheduled_retry_is_retrying_not_error(self):
        out = self.batch(dict(publish_retry=dict(count=1, next_at="2099-01-01T00:00:00+08:00")))
        request = next(s for s in out["steps"] if s["key"] == "request")
        self.assertEqual("retrying", request["status"])
        self.assertEqual("request", out["current_step"])

    def test_a_submit_in_flight_is_not_an_error(self):
        out = self.batch(dict(status="ready", publish_action=dict(status="attempted"), publish_check=dict(kind="submitting")))
        self.assertEqual(0, out["counts"]["errors"])


class CliTests(unittest.TestCase):
    class Client:
        def __init__(self, reply=None):
            self.reply, self.posts = reply or {}, []

        def post(self, path, body=None):
            self.posts.append((path, body))
            return self.reply

    def run_cli(self, argv, client, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sys.stdin", io.StringIO(stdin or "")):
            code = kytdl.main(argv + ["--json"], client)
        return code, json.loads(out.getvalue())

    def test_result_waiting_for_a_decision_is_exit_4_not_an_error(self):
        code, data = self.run_cli(["result", "w1"], self.Client(dict(id="w1", stage="awaiting_decision", has_pdf=True)))
        self.assertEqual((4, True, True), (code, data["ok"], data["needs_decision"]))

    def test_verify_title_from_stdin(self):
        client = self.Client(dict(passed=True))
        code, _ = self.run_cli(["verify", "w1", "--doi", DOI, "--title-file", "-", "--confirmed"], client, stdin="  " + TITLE + "\n")
        self.assertEqual(0, code)
        self.assertEqual(TITLE, client.posts[-1][1]["title"])

    def test_adopted_paper_is_done_even_if_a_sync_reads_an_unknown_status(self):
        item = dict(id="w1", stage="attention", adopt_action=dict(status="confirmed"))
        self.assertEqual(("done", True), kytdl.get_row(item, None, {}, 0))


if __name__ == "__main__":
    unittest.main()
