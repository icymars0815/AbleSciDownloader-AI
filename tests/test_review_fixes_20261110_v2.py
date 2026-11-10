"""Cross-flow regressions: temp data, fictional PDFs and mocked network only."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymupdf

from ablesci.cli import kytdl
from ablesci.cli.kytdl_client import Client
from ablesci.flow import settings
from ablesci.flow.core import Store, verify_pdf
from ablesci.flow.notifications import Notifier
from ablesci.flow.pipeline import Pipeline, ai_adopt_ok, ai_review_task, local_match_ok, oa_auto_keep_ok
from ablesci.papers import oa_fetch
from ablesci.papers.pdf_review import export_review
from ablesci.website.accounts import Accounts
from tests.helpers import FakeAgent, start, stop
from tests.test_oa_fetch import Router, j

DOI = "10.1234/fictional.review"
TITLE = "Fictional fermentation effects on dietary recovery"
ABSTRACT = "This fictional study evaluates fermentation effects on dietary recovery using simulated observations and controlled comparisons. All materials are invented for a local software test."
META = dict(status="available", doi=DOI, title=TITLE, authors=["Alice Smith", "Bob Jones"], abstract=ABSTRACT, sources=["fictional"])
FRONT = TITLE + "\nAlice Smith and Bob Jones\nDOI: " + DOI + "\nABSTRACT\n" + ABSTRACT + "\nKeywords\nfictional"


def make_pdf(path, text=FRONT):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 760), text, fontsize=10)
        doc.save(path)


def run_cli(client, *argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = kytdl.main(list(argv) + ["--json"], client)
    return code, [json.loads(s) for s in output.getvalue().splitlines() if s.strip()]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "fictional.pdf"
        make_pdf(self.pdf)

    def item(self, verification):
        return dict(id="paper1", title=TITLE, doi=DOI, source="website", owned=True, status="checked", pdf="fictional.pdf",
                    assist_url="https://www.ablesci.com/assist/detail?id=fictional", request_confirmed=True,
                    request_mcp_auto_publish=True, verification=verification)

    def test_reading_orders_are_compared_without_lowering_threshold(self):
        scrambled = FRONT.replace(TITLE, "Dietary recovery fermentation fictional effects on")
        fake = MagicMock()
        fake.is_pdf, fake.needs_pass, fake.page_count, fake.metadata = True, False, 1, {}
        fake.get_xml_metadata.return_value = ""
        fake.__getitem__.return_value.get_text.side_effect = lambda **kw: scrambled if kw.get("sort") else FRONT
        with patch("ablesci.flow.core.pymupdf.open") as opened:
            opened.return_value.__enter__.return_value = fake
            result = verify_pdf(self.pdf, TITLE, DOI, META)
        self.assertEqual((100, "original", "identity_match"), (result["title_score"], result["title_text_order"], result["status"]))
        self.assertEqual("match", result["bibliography"]["title_status"])

    def test_supplement_cannot_complete_even_when_every_identity_field_matches(self):
        supplement = self.root / "supplement.pdf"
        make_pdf(supplement, "Supporting Information\n" + FRONT)
        v = verify_pdf(supplement, TITLE, DOI, META)
        self.assertEqual("review", v["status"])
        self.assertIn("supplement", v["review_blockers"])
        item = self.item(v)
        self.assertFalse(local_match_ok(item))
        self.assertFalse(oa_auto_keep_ok(dict(item, source="import", status="oa_review")))
        self.assertIsNotNone(ai_review_task(item))

    def test_body_link_to_supporting_information_does_not_reject_main_text(self):
        path = self.root / "main.pdf"
        make_pdf(path, FRONT + "\nIntroduction\nMain text.\n* Supporting Information\nAdditional tables are available.")
        self.assertEqual("identity_match", verify_pdf(path, TITLE, DOI, META)["status"])

    def test_acs_navigation_strip_is_not_a_supplement_heading(self):
        path = self.root / "main-navigation.pdf"
        text = FRONT.replace("ABSTRACT", "Read Online\nACCESS\nMetrics & More\nArticle Recommendations\n*\nsi\nSupporting Information\nABSTRACT")
        make_pdf(path, text)
        self.assertEqual("identity_match", verify_pdf(path, TITLE, DOI, META)["status"])

    def test_retraction_blocks_scheduler_ai_override_and_oa_keep(self):
        v = verify_pdf(self.pdf, TITLE, DOI, dict(META, retraction=dict(retracted=True, updates=[])))
        item = self.item(v)
        self.assertEqual("review", v["status"])
        self.assertFalse(local_match_ok(item))
        self.assertFalse(ai_adopt_ok(dict(item, ai_verification=dict(status="passed", doi=DOI, sha256=v["sha256"]))))
        self.assertFalse(oa_auto_keep_ok(dict(item, source="import", status="oa_review")))
        self.assertIsNotNone(ai_review_task(item))
        store = Store(self.root / "isolated")
        store.update_preferences(**settings.smart_on({}, "default", dict(settings.DEFAULTS, auto_confirm_max=10)))
        store.update_account(account_id="default", points=500)
        store.add(**{k: val for k, val in item.items() if k != "id"})
        agent = FakeAgent()
        pipeline = Pipeline(store, Accounts(store.root), agent, start=False)
        pipeline.tick_mcp_authorized()
        self.assertFalse(any(action == "adopt" for action, _ in agent.enqueued))

    def test_markdown_is_page_bound_hashed_and_does_not_modify_pdf(self):
        original = self.pdf.read_bytes()
        result = export_review(self.pdf, self.root, "paper1")
        text = Path(result["path"]).read_text("utf-8")
        self.assertEqual([1], result["pages"])
        self.assertIn("## Page 1", text)
        self.assertIn(TITLE, text)
        self.assertIn(hashlib.sha256(original).hexdigest(), text)
        self.assertTrue(result["evidence_only"])
        self.assertEqual(original, self.pdf.read_bytes())
        with self.assertRaises(ValueError):
            export_review(self.pdf, self.root, "paper1", expected_sha256="stale")
        with self.assertRaises(ValueError):
            export_review(self.pdf, self.root, "../escape")
        with self.assertRaises(ValueError):
            export_review(self.pdf, self.root, "paper1", pages=[2])

    def test_blank_scan_produces_warning_instead_of_invented_evidence(self):
        path = self.root / "scan.pdf"
        make_pdf(path, "")
        self.assertTrue(export_review(path, self.root, "scan")["warnings"])

    def test_optional_converter_is_explicit_and_missing_package_is_actionable(self):
        with patch("ablesci.papers.pdf_review.importlib.import_module", side_effect=ImportError):
            with self.assertRaisesRegex(ValueError, "未安装"):
                export_review(self.pdf, self.root, "paper1", engine="pymupdf4llm")
        converter = MagicMock()
        converter.to_markdown.return_value = "# Extracted fictional title"
        with patch("ablesci.papers.pdf_review.importlib.import_module", return_value=converter):
            result = export_review(self.pdf, self.root, "paper1", engine="pymupdf4llm")
        self.assertIn("# Extracted fictional title", Path(result["path"]).read_text("utf-8"))
        self.assertFalse(converter.to_markdown.call_args.kwargs["use_ocr"])


class OaTests(unittest.TestCase):
    def test_supplement_link_is_not_an_oa_candidate_and_hosts_are_not_filtered(self):
        router = Router({"api.openalex.org": j(dict(open_access=dict(is_oa=False, oa_status="closed"))),
                         "semanticscholar": j(dict(openAccessPdf=dict(url="https://doi.org/" + DOI + ".s001")))})
        with patch.object(oa_fetch, "_request", router):
            result = oa_fetch.find_oa_locations(DOI)
        self.assertEqual([], result["candidates"])
        self.assertFalse(result["is_oa"])
        for url in ("https://s2.example/main.pdf", "https://bucket.s3.amazonaws.com/main.pdf"):
            self.assertFalse(oa_fetch.is_supplement_url(url))
        for suffix in (".s001", ".s1", "/supplementary/file.pdf", "/supporting-information"):
            self.assertTrue(oa_fetch.is_supplement_url("https://example.org/paper" + suffix))

    def test_cached_supplements_are_filtered_without_cache_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = dict(candidates=[dict(url="https://doi.org/" + DOI + ".s001", source="fictional")],
                         attempts=[], lookup_ok=True, is_oa=True, oa_status="closed", manual_url="https://doi.org/" + DOI + ".s001")
            oa_fetch._cache_write(folder, DOI, saved)
            path = oa_fetch._cache_file(folder, DOI)
            with patch.object(oa_fetch, "_request", side_effect=AssertionError("network forbidden")):
                result = oa_fetch.find_oa_locations(DOI, cache_root=folder)
            self.assertTrue(path.is_file())
            self.assertFalse(result["is_oa"])
            self.assertEqual([], result["candidates"])
            self.assertIsNone(result["manual_url"])

    def test_valid_repository_survives_closed_status_from_another_source(self):
        result = oa_fetch.sanitize_locations(dict(candidates=[dict(url="https://repo.example/main.pdf")], is_oa=True, oa_status="closed"))
        self.assertTrue(result["is_oa"])
        self.assertEqual(("unknown", "closed"), (result["oa_status"], result["source_oa_status"]))

    def test_download_and_landing_both_filter_supplements(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(oa_fetch, "_get", side_effect=AssertionError("network forbidden")):
            result = oa_fetch.download_oa([dict(url="https://doi.org/" + DOI + ".s001")], Path(folder) / "out.pdf")
        self.assertFalse(result["ok"])
        self.assertEqual("skipped (supplement)", result["attempts"][0]["status"])
        self.assertEqual([], oa_fetch.parse_landing('<meta name="citation_pdf_url" content="/paper.s001">', "https://example.org"))


class ApiAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = start(self.temp.name, permission=True)
        self.addCleanup(stop, self.server)
        self.client = Client("http://127.0.0.1:%d" % self.server.server_port, token_path=self.server.store.root / ".token")

    def enable(self, auto=0, cap=10):
        self.client.post("/api/preferences", {"smart_mode": dict(enabled=True, confirmed=True, auto_confirm_max=auto, daily_cap=cap)})

    def test_adjusting_daily_cap_preserves_explicit_zero(self):
        self.enable(auto=0)
        code, _ = run_cli(self.client, "ai", "on", "--daily-cap", "220", "--confirmed")
        self.assertEqual(0, code)
        self.assertEqual(0, settings.smart(self.server.store.snapshot()["preferences"])["auto_confirm_max"])

    def test_cap_question_reaches_inbox_status_watch_and_resumes_after_confirmation(self):
        self.enable(auto=10, cap=20)
        settings.record_spend(self.server.store, "default", 15)
        iid = self.server.store.add(TITLE, DOI, account_id="default", request_confirmed=True, request_mcp_auto_publish=True,
                                    request_reward=10, oa_check=dict(status="not_oa"))
        task = self.server.pipeline.notify_spending_limit()
        self.assertEqual((15, 20, 10, 25), tuple(task[k] for k in ("spent_today", "daily_cap", "next_points", "suggested_daily_cap")))
        self.server.pipeline.notify_spending_limit()
        pending = self.server.notifier.inbox()
        self.assertEqual(1, len([e for e in pending if "是否同意" in e["message"]]))
        code, data = run_cli(self.client, "status")
        self.assertEqual("kytdl ai on --daily-cap 25 --confirmed", next(r for r in data[0]["needs_user"] if r["kind"] == "daily_cap_reached")["confirmation_command"])
        code, events = run_cli(self.client, "watch", iid, "--timeout", "5")
        self.assertEqual((4, "needs_attention"), (code, events[-1]["event"]))
        paper = next(e for e in events if e.get("event") == "paper")
        self.assertEqual("daily_cap_reached", paper["reason"])
        self.assertIn("询问", paper["next"])
        self.assertEqual("wait_user", self.server.pipeline.plan(local=False)[3]["action"])
        code, _ = run_cli(self.client, "ai", "on", "--daily-cap", "25", "--confirmed")
        self.assertEqual(0, code)
        self.assertFalse(self.server.notifier.inbox())
        planned = self.server.pipeline.plan(local=False)[3]
        # Global pipeline is off; the authorized AI scheduler supplies its own publish settings.
        with patch.object(self.server.pipeline, "execute_mcp", return_value=True):
            resumed = self.server.pipeline.tick_mcp_authorized()
        self.assertEqual("publish", resumed["action"])
        self.assertEqual(15, settings.spent_today(self.server.store.snapshot()["account_metrics"]["default"]))

    def test_actual_site_cost_is_used_in_limit_question_and_day_rollover_clears_it(self):
        self.enable(auto=10, cap=20)
        settings.record_spend(self.server.store, "default", 15)
        iid = self.server.store.add(TITLE, DOI, request_confirmed=True, request_reward=10,
                                    spending_wait=dict(kind="daily_cap", points=30))
        task = self.server.pipeline.notify_spending_limit()
        self.assertEqual((30, 45), (task["next_points"], task["suggested_daily_cap"]))
        account = self.server.store.snapshot()["account_metrics"]["default"]
        prefs = self.server.store.snapshot()["preferences"]
        # A next-day 30-point request still exceeds a 20-point cap; no silent bypass at midnight.
        task = settings.daily_cap_task(prefs, account, [self.server.store.get(iid)], datetime.now().astimezone() + timedelta(days=1))
        self.assertEqual((0, 30), (task["spent_today"], task["suggested_daily_cap"]))

    def test_pending_result_is_success_and_true_error_stays_error(self):
        iid = self.server.store.add(TITLE, DOI, account_id="default")
        code, data = run_cli(self.client, "result", iid)
        self.assertEqual((0, True, True), (code, data[0]["ok"], data[0]["pending"]))
        self.server.store.update(iid, status="attention", note="fictional failure")
        self.assertEqual(1, run_cli(self.client, "result", iid)[0])
        make_pdf(self.server.store.root / "failed.pdf")
        self.server.store.update(iid, pdf="failed.pdf")
        self.assertEqual(1, run_cli(self.client, "result", iid)[0])

    def test_paid_download_limit_is_asked_and_resume_never_republishes(self):
        self.enable(auto=10, cap=20)
        settings.record_spend(self.server.store, "default", 19)
        iid = self.server.store.add(TITLE, DOI, source="website", owned=True, request_confirmed=True,
                                    request_mcp_auto_publish=True, assist_url="https://www.ablesci.com/assist/detail?id=fictional",
                                    website_status="待确认", status="attention", spending_wait=dict(operation="download", points=2),
                                    download_failure=dict(kind="guard", time=datetime.now().astimezone().isoformat()))
        task = self.server.pipeline.notify_spending_limit()
        self.assertEqual((2, 21), (task["next_points"], task["suggested_daily_cap"]))
        self.assertEqual([], self.server.pipeline.resume_spending_waits())
        code, _ = run_cli(self.client, "ai", "on", "--daily-cap", "21", "--confirmed")
        self.assertEqual(0, code)
        self.assertIsNone(self.server.store.get(iid).get("download_failure"))
        with patch.object(self.server.pipeline, "execute_mcp", return_value=True):
            resumed = self.server.pipeline.tick_mcp_authorized()
        self.assertEqual("download", resumed["action"])
        self.assertFalse(self.server.notifier.inbox())

    def test_oa_attempt_primitives_keep_their_types(self):
        result = dict(status="not_oa", links=[], attempts=[dict(bytes=2058, status=200, ok=True, note="x" * 100)])
        with patch.object(oa_fetch, "lookup_only", return_value=result):
            out = self.server.mcp_api.do_oa_lookup(dict(doi=DOI))
        self.assertEqual(dict(bytes=2058, status=200, ok=True, note="x" * 80), out["tried"][0])

    def test_markdown_route_and_ai_verification_bind_to_current_pdf_hash(self):
        self.enable(auto=10)
        pdf = self.server.store.root / "fictional.pdf"
        make_pdf(pdf)
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        iid = self.server.store.add(TITLE, DOI, source="website", account_id="default", owned=True, request_confirmed=True,
                                    status="checked", pdf="fictional.pdf", assist_url="https://www.ablesci.com/assist/detail?id=fictional",
                                    verification=dict(status="review", sha256=digest, bibliography=dict(status="unverified")))
        code, data = run_cli(self.client, "text", iid)
        self.assertEqual((0, digest), (code, data[0]["sha256"]))
        body = dict(id=iid, doi=DOI, title=TITLE, confirmed=True, evidence="markdown", sha256="old")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.server.mcp_api.do_ai_verify(body)
        body["sha256"] = digest
        self.assertTrue(self.server.mcp_api.do_ai_verify(body)["passed"])

    def test_transient_saved_file_gets_grace_but_uncertain_action_does_not(self):
        item = dict(id="fictional", stage="attention", status="checked", pdf="fictional.pdf", publish_check=dict(kind="published"),
                    verification=dict(status="review"))
        clock = {}
        self.assertEqual(("adopting", False), kytdl.get_row(item, None, clock, 10))
        self.assertEqual(("needs_person", True), kytdl.get_row(item, None, clock, 10 + kytdl.DECISION_GRACE))
        self.assertEqual(("needs_person", True), kytdl.get_row(dict(item, adopt_action=dict(status="uncertain")), None, {}, 10))
        self.assertEqual(("needs_person", True), kytdl.get_row(dict(item, verification=dict(status="review", review_blockers=["retracted"])), None, {}, 10))

    def test_version_does_not_connect_to_service(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as exc:
            kytdl.main(["--version"], client=object())
        self.assertEqual(0, exc.exception.code)
        self.assertIn("kytdl", out.getvalue())


if __name__ == "__main__":
    unittest.main()
