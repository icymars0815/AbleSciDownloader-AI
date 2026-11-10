"""Independent-reference gate: fictional records, mocked public APIs, temporary queue and scripted site only."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from ablesci.cli import kytdl
from ablesci.cli.kytdl_client import Client
from ablesci.flow import intake
from ablesci.website import reference_check as rc
from tests.helpers import PublishBase, DOI, TITLE, DETAIL, El, start, stop
from tests import test_flow_fixes_20261102 as old_fixes


RECORD = dict(source="Crossref", doi=DOI, title=TITLE, kind="journal-article", authors=["Alice Smith"],
              container="Fictional Research", year=2026, publisher="Fictional Press")


class ReferenceEvidenceTests(unittest.TestCase):
    def get(self, cr=True, pm=None, offline=False):
        def get(url):
            if offline:
                raise OSError("fictional offline")
            if "crossref" in url:
                if cr is False:
                    raise HTTPError(url, 404, "Not Found", None, None)
                return {"message": dict(DOI=DOI, title=[TITLE], type="journal-article", author=[dict(given="Alice", family="Smith")])}
            if "esearch" in url:
                return {"esearchresult": {"idlist": ["12345"] if pm else []}}
            return {"result": {"12345": pm}}
        return get

    def test_crossref_only_and_pubmed_only_are_both_supported(self):
        pub = dict(title=TITLE, articleids=[dict(idtype="doi", value=DOI)], authors=[dict(name="Smith A")])
        for get, sources in [(self.get(), ["Crossref"]), (self.get(cr=False, pm=pub), ["PubMed"])]:
            out = rc.verify(DOI, TITLE, get, authors=["Alice Smith"])
            self.assertEqual((True, "verified", sources), (out["verified"], out["state"], out["sources"]))

    def test_not_found_and_outage_are_distinct_and_neither_passes(self):
        for get, state in [(self.get(cr=False), "not_found"), (self.get(offline=True), "unavailable")]:
            out = rc.verify(DOI, TITLE, get)
            self.assertEqual((False, state), (out["verified"], out["state"]))

    def test_pubmed_markup_and_author_etal_are_formatting(self):
        first, rest = TITLE.split(" ", 1)
        pub = dict(title="<i>" + first + "</i> " + rest + ".", articleids=[dict(idtype="doi", value=DOI)], authors=[dict(name="Smith A")])
        out = rc.verify(DOI, TITLE, self.get(cr=False, pm=pub), authors=["Smith A, et al."])
        self.assertTrue(out["verified"])

    def test_conflicting_primary_sources_do_not_pass_by_majority_or_priority(self):
        pub = dict(title="A different fictional paper", articleids=[dict(idtype="doi", value=DOI)])
        out = rc.verify(DOI, TITLE, self.get(pm=pub))
        self.assertEqual((False, "mismatch"), (out["verified"], out["state"]))

    def test_number_strain_and_negation_conflicts_override_high_scores(self):
        title = "Fictional effects of fermentation with Lactobacillus strain FRT7 in 2026"
        for changed in [title.replace("FRT7", "FRT8"), title.replace("2026", "2025"), title + " without recovery"]:
            with patch.object(rc, "crossref", return_value=dict(RECORD, title=title)), patch.object(rc, "pubmed", return_value=None):
                out = rc.verify(DOI, changed)
            self.assertFalse(out["verified"], changed)

    def test_full_citation_is_allowed_but_arbitrary_title_containment_is_not(self):
        full = "Smith A, et al. " + TITLE + ". Fictional Research. 2026;1:20. DOI: " + DOI
        self.assertTrue(rc.title_agrees(full, TITLE))
        self.assertFalse(rc.title_agrees("An invented interpretation: " + TITLE + " without recovery", TITLE))
        self.assertFalse(rc.title_agrees("Smith A, et al. " + TITLE + " without recovery. Fictional Research. 2026;1:20.", TITLE))

    def test_doi_only_is_explicit_existence_evidence(self):
        out = rc.verify(DOI, "", self.get(), allow_doi_only=True)
        self.assertTrue(out["verified"])
        self.assertIn("DOI only", out["against"])
        self.assertFalse(rc.verify(DOI, "", self.get())["verified"])

    def test_wrong_authors_and_unavailable_authors_are_not_silently_replaced(self):
        for name in ["Invented Jones", "James Smith"]:
            out = rc.verify(DOI, TITLE, self.get(), authors=[name])
            self.assertEqual((False, "mismatch"), (out["verified"], out["state"]))

    def test_missing_doi_book_is_unsupported_not_nonexistent(self):
        out = rc.verify("", "Fictional book with ISBN only")
        self.assertEqual((False, "unsupported"), (out["verified"], out["state"]))
        self.assertIn("不代表", out["problems"][0])

    def test_original_doi_cannot_be_silently_replaced(self):
        out = rc.verify_item(dict(doi=DOI, title=TITLE, submitted_reference=dict(doi="10.1234/original", title=TITLE)))
        self.assertEqual((False, "mismatch"), (out["verified"], out["state"]))

    def test_malformed_pubmed_is_unavailable_not_a_negative_inventory(self):
        def get(url):
            if "crossref" in url:
                raise HTTPError(url, 404, "Not Found", None, None)
            return {"error": "fictional rate limit"}
        out = rc.verify(DOI, TITLE, get)
        self.assertEqual("unavailable", out["state"])


class PublishReferenceGateTests(PublishBase):
    mock_reference_gate = False

    def setUp(self):
        super().setUp()
        self.cr = self.enterContext(patch.object(rc, "crossref", return_value=dict(RECORD)))
        self.pm = self.enterContext(patch.object(rc, "pubmed", return_value=None))

    def missing(self, text=None, minimum="10"):
        manual = old_fixes.UndefinedLookupTests.undefined(self, low_after_type=minimum)
        if text:
            self.page.search.on_click = lambda: self.page.layers.append(El(text, buttons=[manual]))
        return manual

    def test_valid_reference_precedes_normal_site_lookup(self):
        iid = self.item(request_confirmed=True, authors=["Smith A"])
        self.script(response=dict(code=0), nav=DETAIL)
        self.assertIsNone(self.run_publish(iid))
        self.assertEqual(1, self.cr.call_count)
        item = self.store.get(iid)
        self.assertEqual("verified", item["reference_check"]["state"])
        self.assertEqual("confirmed", item["publish_action"]["status"])

    def test_nonexistent_reference_stops_before_browser_or_spending(self):
        self.cr.return_value = None
        iid = self.item(request_confirmed=True)
        self.script(response=dict(code=0))
        self.run_publish(iid)
        self.assertStopped(iid, "unverified_reference")
        self.agent.goto.assert_not_called()
        self.assertEqual(0, self.page.search.clicks)
        self.assertEqual(100, self.store.snapshot()["account_metrics"]["default"]["points"])
        self.assertEqual("not_found", self.store.get(iid)["reference_check"]["state"])
        self.assertEqual("reference_review", intake.classify(self.store.get(iid))["code"])

    def test_outage_stops_without_claiming_nonexistence(self):
        self.cr.side_effect = OSError("fictional outage")
        self.pm.side_effect = OSError("fictional outage")
        iid = self.item()
        self.assertIn("不等于", self.run_publish(iid))
        self.assertEqual("unavailable", self.store.get(iid)["reference_check"]["state"])
        self.agent.goto.assert_not_called()

    def test_real_doi_wrong_citation_and_forged_metadata_are_blocked(self):
        iid = self.item(title="Invented findings about a fictional battery", metadata_ref=dict(title=TITLE),
                        reference_check=dict(verified=True), request_confirmed=True)
        self.script(response=dict(code=0))
        self.run_publish(iid)
        self.assertStopped(iid, "unverified_reference")
        self.agent.goto.assert_not_called()
        self.assertEqual("mismatch", self.store.get(iid)["reference_check"]["state"])

    def test_enrichment_cannot_erase_the_original_citation(self):
        iid = self.item(title_from_metadata=True, submitted_reference=dict(title="An invented citation", doi=DOI, authors=[]))
        self.run_publish(iid)
        self.assertStopped(iid, "unverified_reference")
        self.agent.goto.assert_not_called()

    def test_doi_only_does_not_claim_to_verify_an_absent_cited_title(self):
        iid = self.item(title="", submitted_reference=dict(title="", doi=DOI, authors=[]))
        self.script(response=dict(code=0))
        self.assertIsNone(self.run_publish(iid))
        self.assertIn("DOI only", self.store.get(iid)["reference_check"]["against"])

    def test_verified_book_missing_on_site_is_rechecked_then_filled(self):
        self.cr.return_value = dict(RECORD, kind="book")
        iid = self.item(request_confirmed=True)
        manual = self.missing("未找到该 DOI 的文献记录，请手动填写")
        self.assertIsNone(self.run_publish(iid))
        self.assertEqual(2, self.cr.call_count)
        self.assertEqual(1, manual.clicks)
        item = self.store.get(iid)
        self.assertEqual("3", item["publish_check"]["form_type"])
        self.assertEqual(TITLE, self.page.el["#Assist-title"].value)
        self.assertIn("Fictional Press", self.page.el["#Assist-note"].value)
        self.assertEqual(1, self.page.submit.clicks)

    def test_verified_chapter_undefined_lookup_keeps_the_fallback(self):
        self.cr.return_value = dict(RECORD, kind="book-chapter")
        iid = self.item()
        self.missing()
        self.assertIsNone(self.run_publish(iid))
        self.assertEqual(2, self.cr.call_count)
        self.assertEqual("4", self.store.get(iid)["publish_check"]["form_type"])

    def test_missing_popup_can_be_closed_with_its_unique_acknowledgement(self):
        iid = self.item()
        self.missing()
        ack = El("确定")
        self.page.search.on_click = lambda: self.page.layers.append(El("未收录该 DOI 的文献", buttons=[ack]))
        self.assertIsNone(self.run_publish(iid))
        self.assertEqual(1, ack.clicks)

    def test_missing_popup_without_a_unique_close_does_not_touch_the_form(self):
        iid = self.item()
        self.missing()
        self.page.search.on_click = lambda: self.page.layers.append(El("未找到文献记录"))
        self.run_publish(iid)
        self.assertStopped(iid, "lookup_confirm")
        self.assertEqual("", self.page.el["#Assist-title"].value)

    def test_second_check_failure_never_fills_or_submits(self):
        self.cr.side_effect = [dict(RECORD), None]
        iid = self.item()
        manual = self.missing("未找到文献记录")
        self.run_publish(iid)
        self.assertStopped(iid, "unverified_reference")
        self.assertEqual(0, manual.clicks)
        self.assertEqual("", self.page.el["#Assist-title"].value)

    def test_verified_book_does_not_override_points_permission(self):
        self.cr.return_value = dict(RECORD, kind="book")
        iid = self.item(request_reward=10)
        self.missing("未找到该 DOI", minimum="30")
        self.run_publish(iid)
        self.assertStopped(iid, "min_reward_raised")

    def test_query_error_and_account_restriction_do_not_use_missing_fallback(self):
        for text, kind in [("查询失败，请稍后重试", "lookup_failed"), ("账号受限，未找到文献", "restricted")]:
            self.page.layers.clear()
            iid = self.item(doi=DOI + "/" + kind)
            self.cr.return_value = dict(RECORD, doi=DOI + "/" + kind)
            manual = self.missing(text)
            self.run_publish(iid)
            self.assertStopped(iid, kind)
            self.assertEqual(0, manual.clicks)


class ReadOnlyCheckTests(unittest.TestCase):
    def test_cli_checks_original_fields_and_does_not_import_or_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            server = start(folder)
            self.addCleanup(stop, server)
            client = Client("http://127.0.0.1:%d" % server.server_port, token_path=Path(folder) / ".token")
            reply = client.post("/api/import", dict(records=[dict(doi=DOI, title="An invented citation", authors=["Fake Author"])], ai_assisted=True))
            iid = reply["item_ids"][0]
            self.assertEqual("An invented citation", server.store.get(iid)["submitted_reference"]["title"])
            server.store.update(iid, title=TITLE, title_from_metadata=True)
            before = server.store.snapshot()
            for target, expected_code in [(iid, 4), (DOI, 0)]:
                output = io.StringIO()
                with patch.object(rc, "crossref", return_value=dict(RECORD)), patch.object(rc, "pubmed", return_value=None), contextlib.redirect_stdout(output):
                    code = kytdl.main(["check", target, "--json"], client)
                self.assertEqual(expected_code, code)
                result = json.loads(output.getvalue())
                self.assertEqual(code == 0, result["verified"])
            self.assertEqual(before, server.store.snapshot())
            self.assertFalse(server.agent.busy)
