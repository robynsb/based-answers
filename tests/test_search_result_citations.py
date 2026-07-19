"""search_result citations prove absence ("this query found nothing") or
exhaustiveness ("this query found exactly these hits, nothing more"). The
deterministic checker must independently rerun the query and catch both
fabricated hits and hits the agent dropped to make an exhaustiveness claim
look cleaner than it is. The renderer must show these citations distinctly
(no PDF preview / highlights) and deduplicate identical ones across claims."""

import tempfile
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .support import SKILL_DIR, load_script

verify_citations = load_script("verify-citations.py")
format_answers = load_script("format-answers.py")
based_answers = load_script("based-answers.py")
pdf_search = load_script("pdf-search.py")

# A small fake cache: "alpha beta" appears on page 1 and page 3, nowhere else.
CACHE = {"chunks": [
    {"page": 1, "text": "Some text mentioning alpha beta gamma in context."},
    {"page": 2, "text": "Unrelated content on this page entirely."},
    {"page": 3, "text": "More text: alpha beta shows up again here too."},
]}


class TestCheckSearchResult(unittest.TestCase):
    def setUp(self):
        # check_search_result() loads the cache via load_pdf_cache(), which
        # reads indexed-pdfs/<source>.json relative to cwd — write it there.
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        (self.work / "indexed-pdfs").mkdir()
        import json
        (self.work / "indexed-pdfs" / "fake.pdf.json").write_text(json.dumps(CACHE))
        self._old_cwd = Path.cwd()
        import os
        os.chdir(self.work)

    def tearDown(self):
        import os
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_empty_claimed_matches_empty_actual(self):
        cit = {"type": "search_result", "source": "fake.pdf",
               "query": "nonexistent term entirely", "results": []}
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))

    def test_exhaustive_claimed_matches_actual(self):
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": [{"page": 1, "text": "alpha beta gamma"},
                           {"page": 3, "text": "alpha beta shows up again"}]}
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))

    def test_result_text_not_on_stated_page_fails(self):
        # page 1 really matches the query, but this snippet is fabricated —
        # a real hit page paired with an invented quote must still fail
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": [{"page": 1, "text": "this sentence is not on page 1 at all"},
                           {"page": 3, "text": "alpha beta shows up again"}]}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("not found on that page", r["reason"])

    def test_page_listed_once_for_multiple_matching_chunks_still_passes(self):
        # a page can contribute more than one matching chunk to find_matches;
        # the agent lists that page once, which must not look like an omission
        two_chunk_cache = {"chunks": [
            {"page": 1, "text": "alpha beta appears here once."},
            {"page": 1, "text": "alpha beta appears here twice on the same page."},
        ]}
        import json
        (self.work / "indexed-pdfs" / "two-chunk.pdf.json").write_text(json.dumps(two_chunk_cache))
        cit = {"type": "search_result", "source": "two-chunk.pdf", "query": "alpha beta",
               "results": [{"page": 1, "text": "alpha beta appears here once"}]}
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))

    def test_fabricated_hit_fails(self):
        # page 7 doesn't exist in the doc at all
        cit = {"type": "search_result", "source": "fake.pdf",
               "query": "nonexistent term entirely",
               "results": [{"page": 7, "text": "made up"}]}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])

    def test_fabricated_page_with_real_text_still_fails_query_mismatch(self):
        # the snippet is verbatim from page 2, so it passes the on-page
        # check, but page 2 doesn't actually match the "alpha beta" query —
        # the set-equality check must still catch this
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": [{"page": 2, "text": "Unrelated content on this page entirely"}]}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("claimed but not actually found", r["reason"])
        self.assertIn("omitted from results", r["reason"])

    def test_omitted_hit_fails(self):
        # query really hits pages 1 and 3; agent only reports page 1
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": [{"page": 1, "text": "alpha beta gamma"}]}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("omitted from results", r["reason"])

    def test_non_list_results_fails(self):
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": "not a list"}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])

    def test_missing_query_fails(self):
        cit = {"type": "search_result", "source": "fake.pdf", "query": "", "results": []}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])

    def test_result_missing_page_or_text_fails(self):
        cit = {"type": "search_result", "source": "fake.pdf", "query": "alpha beta",
               "results": [{"page": 1}]}
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])

    def test_raw_find_matches_output_round_trips(self):
        # what the agent actually copies from pdf_search's output includes
        # leading/trailing "..." and can span newlines — confirm a snippet
        # straight out of find_matches() (not hand-cleaned) still passes
        long_chunk = (
            "Padding text before the match so the context window truncates "
            "it and the tool adds a leading ellipsis. " * 3 +
            "Here is the alpha beta needle in the haystack.\n"
            "It continues on a wrapped line after a newline character. " +
            "More padding text after the match so a trailing ellipsis is "
            "also added by the snippet extractor. " * 3
        )
        long_cache = {"chunks": [{"page": 5, "text": long_chunk}]}
        import json
        (self.work / "indexed-pdfs" / "long.pdf.json").write_text(json.dumps(long_cache))

        matches = pdf_search.find_matches(long_cache, "alpha beta")
        self.assertEqual(len(matches), 1)
        snippet = matches[0]["snippet"]
        self.assertTrue(snippet.startswith("...") and snippet.endswith("..."),
                         "test setup should exercise the ellipsis truncation path")

        cit = {"type": "search_result", "source": "long.pdf", "query": "alpha beta",
               "results": [{"page": 5, "text": snippet}]}
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))


class TestFindDistinctMatches(unittest.TestCase):
    """pdf_search.find_distinct_matches() enumerates every distinct string a
    regex matches anywhere in the doc, for family-of-names absence claims
    that can't honestly be proven by guessing a handful of literal names."""

    ENUM_CACHE = {"chunks": [
        {"page": 1, "text": "The API defines pio_sm_set_enabled and pio_sm_get_pc for control."},
        {"page": 2, "text": "See also pio_sm_is_claimed for ownership checks."},
        {"page": 3, "text": "pio_sm_set_enabled is mentioned again here for emphasis."},
    ]}

    def test_dedups_by_matched_string_across_pages(self):
        result = pdf_search.find_distinct_matches(self.ENUM_CACHE, r"pio_sm_[a-z_]+")
        matches = {m["match"] for m in result["matches"]}
        self.assertEqual(matches, {"pio_sm_set_enabled", "pio_sm_get_pc", "pio_sm_is_claimed"})
        # first occurrence (page 1) is kept as the example for the repeated symbol
        rep = next(m for m in result["matches"] if m["match"] == "pio_sm_set_enabled")
        self.assertEqual(rep["page"], 1)

    def test_invalid_pattern_reported(self):
        result = pdf_search.find_distinct_matches(self.ENUM_CACHE, r"pio_sm_[a-z")
        self.assertEqual(result.get("error"), "invalid_pattern")

    def test_too_broad_never_silently_truncates(self):
        result = pdf_search.find_distinct_matches(self.ENUM_CACHE, r"pio_sm_[a-z_]+", max_matches=2)
        self.assertEqual(result.get("error"), "too_broad")
        self.assertEqual(result.get("count"), 3)
        self.assertNotIn("matches", result)


class TestCheckSearchResultRegex(unittest.TestCase):
    """verify_citations.check_search_result() dispatches to the regex branch
    when mode == "regex": rerun the pattern and require the claimed set of
    distinct matches to exactly equal what's actually in the source."""

    CACHE = {"chunks": [
        {"page": 1, "text": "The API defines pio_sm_set_enabled and pio_sm_get_pc for control."},
        {"page": 2, "text": "See also pio_sm_is_claimed for ownership checks."},
    ]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        (self.work / "indexed-pdfs").mkdir()
        import json
        (self.work / "indexed-pdfs" / "fake.pdf.json").write_text(json.dumps(self.CACHE))
        self._old_cwd = Path.cwd()
        import os
        os.chdir(self.work)

    def tearDown(self):
        import os
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _cit(self, **overrides):
        cit = {"type": "search_result", "mode": "regex", "source": "fake.pdf",
               "query": r"pio_sm_[a-z_]+",
               "results": [
                   {"match": "pio_sm_set_enabled", "page": 1, "text": "pio_sm_set_enabled and pio_sm_get_pc"},
                   {"match": "pio_sm_get_pc", "page": 1, "text": "pio_sm_set_enabled and pio_sm_get_pc"},
                   {"match": "pio_sm_is_claimed", "page": 2, "text": "See also pio_sm_is_claimed for ownership"},
               ]}
        cit.update(overrides)
        return cit

    def test_honest_complete_enumeration_passes(self):
        r = verify_citations.check_search_result(self._cit())
        self.assertTrue(r["found"], r.get("reason"))

    def test_fabricated_symbol_fails(self):
        results = self._cit()["results"] + [
            {"match": "pio_sm_totally_made_up", "page": 1, "text": "pio_sm_set_enabled and pio_sm_get_pc"}]
        r = verify_citations.check_search_result(self._cit(results=results))
        self.assertFalse(r["found"])
        self.assertIn("not actually produced by pattern", r["reason"])

    def test_omitted_symbol_fails(self):
        results = self._cit()["results"][:-1]  # drop pio_sm_is_claimed
        r = verify_citations.check_search_result(self._cit(results=results))
        self.assertFalse(r["found"])
        self.assertIn("omitted from results", r["reason"])
        self.assertIn("pio_sm_is_claimed", r["reason"])

    def test_match_not_on_stated_page_fails(self):
        results = self._cit()["results"][:]
        results[0] = {**results[0], "page": 99}
        r = verify_citations.check_search_result(self._cit(results=results))
        self.assertFalse(r["found"])
        self.assertIn("not found on that page", r["reason"])

    def test_match_not_produced_by_pattern_in_its_own_text_fails(self):
        # real page, real verbatim text, but the claimed "match" string isn't
        # what the pattern actually finds in it — must not be accepted
        results = self._cit()["results"][:]
        results[0] = {"match": "pio_sm_bogus", "page": 1,
                      "text": "pio_sm_set_enabled and pio_sm_get_pc"}
        r = verify_citations.check_search_result(self._cit(results=results))
        self.assertFalse(r["found"])
        self.assertIn("not actually produced by pattern", r["reason"])

    def test_invalid_pattern_fails(self):
        r = verify_citations.check_search_result(self._cit(query="pio_sm_[a-z"))
        self.assertFalse(r["found"])
        self.assertIn("invalid regex pattern", r["reason"])

    def test_too_broad_pattern_fails_regardless_of_claim(self):
        big_cache = {"chunks": [{"page": 1, "text": " ".join(f"tok{i}" for i in range(150))}]}
        import json
        (self.work / "indexed-pdfs" / "big.pdf.json").write_text(json.dumps(big_cache))
        r = verify_citations.check_search_result(
            self._cit(source="big.pdf", query=r"tok[0-9]+", results=[]))
        self.assertFalse(r["found"])
        self.assertIn("narrow the pattern", r["reason"])

    def test_missing_match_field_fails(self):
        r = verify_citations.check_search_result(
            self._cit(results=[{"page": 1, "text": "pio_sm_set_enabled and pio_sm_get_pc"}]))
        self.assertFalse(r["found"])

    def test_raw_find_distinct_matches_output_round_trips(self):
        # what the agent actually copies from search_regex's output includes
        # leading/trailing "..." and can span newlines — confirm the raw
        # (not hand-cleaned) snippets find_distinct_matches produces still
        # pass, the same seam test_raw_find_matches_output_round_trips
        # covers for literal mode
        long_chunk = (
            "Padding text before the match so the context window truncates "
            "it and the tool adds a leading ellipsis. " * 3 +
            "Here is the pio_sm_set_enabled symbol in the haystack.\n"
            "It continues on a wrapped line after a newline character. " +
            "More padding text after the match so a trailing ellipsis is "
            "also added by the snippet extractor. " * 3
        )
        long_cache = {"chunks": [{"page": 5, "text": long_chunk}]}
        import json
        (self.work / "indexed-pdfs" / "long.pdf.json").write_text(json.dumps(long_cache))

        result = pdf_search.find_distinct_matches(long_cache, r"pio_sm_[a-z_]+")
        self.assertEqual(len(result["matches"]), 1)
        snippet = result["matches"][0]["snippet"]
        self.assertTrue(snippet.startswith("...") and snippet.endswith("..."),
                         "test setup should exercise the ellipsis truncation path")

        cit = {"type": "search_result", "mode": "regex", "source": "long.pdf",
               "query": r"pio_sm_[a-z_]+",
               "results": [{"match": "pio_sm_set_enabled", "page": 5, "text": snippet}]}
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))


IDENTICAL_SEARCH_RESULT_YAML = """question: "q"
answers:
  - claim: "claim one absence"
    citations:
      - type: search_result
        source: "fake.pdf"
        query: "foo bar"
        results: []
  - claim: "claim two absence"
    citations:
      - type: search_result
        source: "fake.pdf"
        query: "foo bar"
        results: []
"""

DIFFERING_SEARCH_RESULT_YAML = """question: "q"
answers:
  - claim: "claim one"
    citations:
      - type: search_result
        source: "fake.pdf"
        query: "foo bar"
        results: []
  - claim: "claim two"
    citations:
      - type: search_result
        source: "fake.pdf"
        query: "a different query"
        results: []
"""

SINGLE_SEARCH_RESULT_YAML = """question: "q"
answers:
  - claim: "exhaustive claim"
    citations:
      - type: search_result
        source: "fake.pdf"
        query: "alpha beta"
        results:
          - page: 1
            text: "alpha beta gamma"
          - page: 3
            text: "alpha beta shows up again"
"""

SINGLE_ENUMERATION_YAML = """question: "q"
answers:
  - claim: "enumeration claim"
    citations:
      - type: search_result
        mode: regex
        source: "fake.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_set_enabled"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
          - match: "pio_sm_get_pc"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
"""

IDENTICAL_ENUMERATION_YAML = """question: "q"
answers:
  - claim: "claim one enumeration"
    citations:
      - type: search_result
        mode: regex
        source: "fake.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_set_enabled"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
  - claim: "claim two enumeration"
    citations:
      - type: search_result
        mode: regex
        source: "fake.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_set_enabled"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
"""

DIFFERING_ENUMERATION_YAML = """question: "q"
answers:
  - claim: "claim one enumeration"
    citations:
      - type: search_result
        mode: regex
        source: "fake.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_set_enabled"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
  - claim: "claim two enumeration"
    citations:
      - type: search_result
        mode: regex
        source: "fake.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_get_pc"
            page: 1
            text: "pio_sm_set_enabled and pio_sm_get_pc"
"""


class TestBuildContextSearchResult(unittest.TestCase):
    def _build(self, yaml_text):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.yml"
            p.write_text(yaml_text)
            return format_answers.build_context(p)

    def test_search_result_ref_shape(self):
        context = self._build(SINGLE_SEARCH_RESULT_YAML)
        self.assertFalse(context["unable"])
        self.assertEqual(len(context["all_references"]), 1)
        ref = context["all_references"][0]
        self.assertEqual(ref["kind"], "search_result")
        self.assertEqual(ref["query"], "alpha beta")
        self.assertEqual(len(ref["results"]), 2)
        self.assertIn("<sup>", context["concatenation"])

    def test_search_result_ref_renders_in_template(self):
        context = self._build(SINGLE_SEARCH_RESULT_YAML)
        env = Environment(loader=FileSystemLoader(str(SKILL_DIR)))
        html = env.get_template("answer-template.html").render(**context)
        self.assertIn("alpha beta", html)
        self.assertIn("search-results", html)
        self.assertNotIn("No results found", html)  # this fixture has results, not the empty case
        # no PDF preview element for search_result refs (the CSS rule for the
        # class is always present in <style>, so check for the rendered element)
        self.assertNotIn('<details class="pdf-preview-details">', html)

    def test_identical_search_results_across_claims_dedup(self):
        context = self._build(IDENTICAL_SEARCH_RESULT_YAML)
        self.assertEqual(len(context["all_references"]), 1,
                          "identical search_result citations on different claims must collapse to one ref")
        self.assertEqual(context["concatenation"].count("<sup>"), 2)
        # both claims' superscripts point at reference [1]
        self.assertEqual(context["concatenation"].count(">1</a>"), 2)

    def test_differing_search_results_across_claims_not_deduped(self):
        context = self._build(DIFFERING_SEARCH_RESULT_YAML)
        self.assertEqual(len(context["all_references"]), 2,
                          "search_result citations with different queries must not collapse")

    def test_enumeration_ref_shape(self):
        context = self._build(SINGLE_ENUMERATION_YAML)
        self.assertEqual(len(context["all_references"]), 1)
        ref = context["all_references"][0]
        self.assertEqual(ref["kind"], "search_result")
        self.assertEqual(ref["mode"], "regex")
        self.assertEqual(ref["query"], "pio_sm_[a-z_]+")
        self.assertEqual({r["match"] for r in ref["results"]},
                          {"pio_sm_set_enabled", "pio_sm_get_pc"})

    def test_enumeration_ref_renders_in_template(self):
        context = self._build(SINGLE_ENUMERATION_YAML)
        env = Environment(loader=FileSystemLoader(str(SKILL_DIR)))
        html = env.get_template("answer-template.html").render(**context)
        self.assertIn("pio_sm_[a-z_]+", html)
        self.assertIn("pio_sm_set_enabled", html)
        self.assertIn("distinct match", html)
        self.assertNotIn('<details class="pdf-preview-details">', html)

    def test_identical_enumerations_across_claims_dedup(self):
        context = self._build(IDENTICAL_ENUMERATION_YAML)
        self.assertEqual(len(context["all_references"]), 1,
                          "identical enumeration citations on different claims must collapse to one ref")
        self.assertEqual(context["concatenation"].count("<sup>"), 2)

    def test_differing_enumerations_across_claims_not_deduped(self):
        context = self._build(DIFFERING_ENUMERATION_YAML)
        self.assertEqual(len(context["all_references"]), 2,
                          "enumeration citations with different match sets must not collapse")


NORMAL_CITATION_ONLY_YAML = """question: "q"
answers:
  - claim: "claim one"
    citations: [{text: "t1", page: 1, source: "s.pdf"}]
"""

MIXED_CITATION_YAML = """question: "q"
answers:
  - claim: "absence claim"
    citations:
      - type: search_result
        source: "s.pdf"
        query: "some query"
        results: []
"""

ENUMERATION_CITATION_YAML = """question: "q"
answers:
  - claim: "no getter exists"
    citations:
      - type: search_result
        mode: regex
        source: "s.pdf"
        query: "pio_sm_[a-z_]+"
        results:
          - match: "pio_sm_set_enabled"
            page: 1
            text: "static void pio_sm_set_enabled(...)"
"""


class TestSemanticRubricSearchResult(unittest.TestCase):
    def _rubric_for(self, yaml_text):
        captured = {}

        def fake_checker(rubric, **kwargs):
            captured[kwargs.get("extra", {}).get("claim")] = rubric
            return "PASS"

        orig = based_answers.run_checker
        based_answers.run_checker = fake_checker
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "a.yml"
                p.write_text(yaml_text)
                based_answers.run_semantic_checkers(p)
        finally:
            based_answers.run_checker = orig
        return captured[0]

    def test_rubric_unchanged_for_normal_citations_only(self):
        rubric = self._rubric_for(NORMAL_CITATION_ONLY_YAML)
        self.assertNotIn("SEARCH_RESULTS", rubric)
        self.assertIn("Does the SYNTHESIS of all SOURCE_TEXTS together with PREVIOUS_CLAIMS", rubric)

    def test_rubric_includes_search_results_block_when_present(self):
        rubric = self._rubric_for(MIXED_CITATION_YAML)
        self.assertIn("SEARCH_RESULTS", rubric)
        self.assertIn("some query", rubric)
        self.assertIn("NO RESULTS FOUND", rubric)
        self.assertIn("query not comprehensive", rubric)
        # literal-only claim: no ENUMERATION block or checklist, though the
        # SEARCH_RESULTS instruction text may still recommend enumeration as
        # a remedy in prose — check for the actual block/synthesis wording
        self.assertNotIn("ENUMERATION (every distinct match", rubric)
        self.assertNotIn("ENUMERATION CHECK", rubric)
        self.assertIn("Does the SYNTHESIS of all SOURCE_TEXTS and SEARCH_RESULTS together with PREVIOUS_CLAIMS",
                       rubric)

    def test_rubric_includes_enumeration_block_when_present(self):
        rubric = self._rubric_for(ENUMERATION_CITATION_YAML)
        self.assertIn("ENUMERATION", rubric)
        self.assertIn("pio_sm_[a-z_]+", rubric)
        self.assertIn("pio_sm_set_enabled", rubric)
        self.assertNotIn("SEARCH_RESULTS (used to demonstrate", rubric)  # not a literal-mode citation
        self.assertIn("Does the SYNTHESIS of all SOURCE_TEXTS and ENUMERATION together with PREVIOUS_CLAIMS",
                       rubric)


if __name__ == "__main__":
    unittest.main()
