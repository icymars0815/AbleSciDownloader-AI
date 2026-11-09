import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from literature_search import search_pubmed, search_works


class SearchTests(unittest.TestCase):
    def test_paging_filters_and_record_mapping(self):
        data = {"meta": {"count": 42, "next_cursor": "abc+/="}, "results": [{
            "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/example", "title": "Study",
            "authorships": [{"author": {"display_name": "Researcher"}}], "publication_year": 2025,
            "primary_location": {"source": {"display_name": "Journal"}}, "open_access": {"is_oa": True},
            "cited_by_count": 7, "is_retracted": False}]}
        with patch.dict("os.environ", {"OPENALEX_API_KEY": "example"}), patch("literature_search.get_json", return_value=data) as get:
            result = search_works("gut AND fiber", "abc+/=", 2020, 2026, True, 10)
        params = parse_qs(urlparse(get.call_args.args[0]).query)
        self.assertEqual(["abc+/="], params["cursor"])
        self.assertIn("is_oa:true", params["filter"][0])
        self.assertEqual("10.1234/example", result["records"][0]["doi"])
        self.assertEqual(["Researcher"], result["records"][0]["authors"])
        self.assertEqual("abc+/=", result["next_cursor"])

    def test_invalid_inputs_do_not_call_network(self):
        with patch("literature_search.get_json") as get:
            for kwargs in ({"query": ""}, {"query": "x", "page_size": 101},
                           {"query": "x", "year_from": 2026, "year_to": 2020},
                           {"query": "x", "cursor": "z" * 4097}):
                with self.assertRaises(ValueError): search_works(**kwargs)
            get.assert_not_called()

    def test_network_error_does_not_reveal_key(self):
        with patch.dict("os.environ", {"OPENALEX_API_KEY": "secret-key"}), patch("literature_search.get_json", side_effect=Exception("secret-key")):
            with self.assertRaises(ValueError) as exc: search_works("x")
        self.assertNotIn("secret-key", str(exc.exception))

    def test_semantic_query_and_anonymous_paging_limits(self):
        with patch.dict("os.environ", {}, clear=True), patch("literature_search.get_json", return_value={"meta": {}, "results": []}) as get:
            result = search_works("dietary compounds and microbial metabolism", mode="semantic", page_size=20, year_from=2021, year_to=2025)
            self.assertIn("search.semantic", parse_qs(urlparse(get.call_args.args[0]).query))
            self.assertIn("publication_year:2021-2025", parse_qs(urlparse(get.call_args.args[0]).query)["filter"])
            self.assertIsNone(result["next_cursor"])
            with self.assertRaisesRegex(ValueError, "API Key"): search_works("x", cursor="next")
            with self.assertRaises(ValueError): search_works("x", mode="semantic", page_size=51)


class PubMedTests(unittest.TestCase):
    SEARCH = {"esearchresult": {"count": "3", "idlist": ["111", "222"], "querytranslation": "\"enterococcus\"[MeSH Terms] AND optrA[tiab]"}}
    SUMMARY = {"result": {"uids": ["111", "222"],
                          "111": {"title": "optrA in Enterococcus.", "pubdate": "2015 Aug", "fulljournalname": "J Antimicrob Chemother",
                                  "authors": [{"name": "Wang Y"}], "articleids": [{"idtype": "doi", "value": "10.1093/jac/dkv116"}, {"idtype": "pmc", "value": "PMC1"}],
                                  "pubtype": ["Journal Article"]},
                          "222": {"title": "Retracted thing", "pubdate": "2020", "authors": [], "articleids": [], "pubtype": ["Retracted Publication"]}}}

    def test_query_passed_unchanged_and_records_mapped(self):
        with patch("literature_search.get_json", side_effect=[self.SEARCH, self.SUMMARY]) as get, patch("literature_search.time.sleep"):
            result = search_pubmed("enterococcus[MeSH] AND optrA[tiab]", year_from=2010, year_to=2020, open_access=True, page_size=2)
        first = parse_qs(urlparse(get.call_args_list[0].args[0]).query)
        self.assertEqual(["enterococcus[MeSH] AND optrA[tiab] AND free full text[sb]"], first["term"])
        self.assertEqual((["relevance"], ["2010"], ["2020"], ["pdat"]), (first["sort"], first["mindate"], first["maxdate"], first["datetype"]))
        self.assertIn("eutils.ncbi.nlm.nih.gov", get.call_args_list[0].args[0])
        rec = result["records"]
        self.assertEqual(("10.1093/jac/dkv116", "optrA in Enterococcus", 2015, True), (rec[0]["doi"], rec[0]["title"], rec[0]["year"], rec[0]["is_oa"]))
        self.assertTrue(rec[1]["is_retracted"])
        self.assertIn("MeSH", result["query_translation"])
        self.assertEqual("2", result["next_cursor"])

    def test_invalid_inputs_do_not_call_network(self):
        with patch("literature_search.get_json") as get:
            for kwargs in ({"query": ""}, {"query": "x", "cursor": "abc"}, {"query": "x", "page_size": 0}, {"query": "x", "year_from": 2026, "year_to": 2020}):
                with self.assertRaises(ValueError):
                    search_pubmed(**kwargs)
            get.assert_not_called()
