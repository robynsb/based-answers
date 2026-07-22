"""The searcher names its evidence; the resolver materialises it.

These pin the seam that makes the design work: what `resolve-answer.py`
writes must pass `verify-citations.py` unchanged, every time, because the
text was never typed — so a verifier failure on a resolved answer can only
mean the two disagree about how a span is read.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from .support import load_script, SKILL_DIR

pdf_search = load_script("pdf-search.py")
verify_citations = load_script("verify-citations.py")
resolve_answer = load_script("resolve-answer.py")

SOURCE = "RP-008371-DS-1-rp2040-datasheet.pdf"
CACHE_SRC = SKILL_DIR / "indexed-pdfs" / f"{SOURCE}.json"


def quote(page, first, last, source=SOURCE):
    return {"type": "quote", "source": source,
            "spans": [{"page": page, "from": first, "to": last}]}


class ResolverTestCase(unittest.TestCase):
    """Both scripts resolve their cache relative to the CWD, so the tests run
    in a scratch directory holding a copy of the indexed datasheet."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        (Path(cls.tmp) / "indexed-pdfs").mkdir()
        shutil.copy(CACHE_SRC, Path(cls.tmp) / "indexed-pdfs" / f"{SOURCE}.json")
        cls.prev_cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.cache = verify_citations.load_pdf_cache(SOURCE)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.prev_cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def resolve(self, answers, question="q?"):
        return resolve_answer.resolve_answer({"answers": answers}, question)


class TestQuoteCitations(ResolverTestCase):
    def test_resolved_text_is_the_source_lines(self):
        out = self.resolve([{"claim": "c", "citations": [quote(314, 2, 6)]}])
        cit = out["answers"][0]["citations"][0]
        self.assertEqual(cit["text"], pdf_search.resolve_span(self.cache, 314, 2, 6))
        self.assertEqual(cit["page"], 314)
        self.assertEqual(cit["source"], SOURCE)

    def test_resolved_quote_passes_the_verifier(self):
        out = self.resolve([{"claim": "c", "citations": [quote(314, 2, 6)]}])
        cit = out["answers"][0]["citations"][0]
        self.assertTrue(verify_citations.check_spans(cit, self.cache)["found"])

    def test_multi_span_quote_skips_the_lines_between(self):
        """The cross-page/header case: two spans, joined, nothing in between."""
        out = self.resolve([{"claim": "c", "citations": [{
            "type": "quote", "source": SOURCE,
            "spans": [{"page": 314, "from": 2, "to": 6},
                      {"page": 314, "from": 14, "to": 17}]}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertEqual(cit["text"],
                         pdf_search.resolve_span(self.cache, 314, 2, 6) + "\n"
                         + pdf_search.resolve_span(self.cache, 314, 14, 17))
        # page is where the quote starts, as with a single-span citation
        self.assertEqual(cit["page"], 314)
        self.assertTrue(verify_citations.check_spans(cit, self.cache)["found"])

    def test_too_short_a_span_is_an_authoring_error(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [quote(314, 3, 3)]}])
        self.assertIn(str(verify_citations.MIN_CITATION_CHARS), str(cm.exception))

    def test_span_past_the_end_of_the_page_is_reported(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [quote(314, 1, 9999)]}])
        self.assertIn("314", str(cm.exception))

    def test_unknown_source_is_reported(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [quote(314, 2, 6, "nope.pdf")]}])
        self.assertIn("nope.pdf", str(cm.exception))


class TestSearchCitations(ResolverTestCase):
    def test_results_come_from_a_real_rerun(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "search", "source": SOURCE, "query": "pioasm"}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertEqual(cit["type"], "search_result")
        expected = pdf_search.find_matches(self.cache, "pioasm")
        self.assertEqual([r["page"] for r in cit["results"]],
                         [m["page"] for m in expected])

    def test_resolved_search_passes_the_verifier(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "search", "source": SOURCE, "query": "pioasm"}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertTrue(verify_citations.check_search_result(cit)["found"])

    def test_a_query_with_no_hits_resolves_to_no_results(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "search", "source": SOURCE,
             "query": "zzz no such phrase in the datasheet zzz"}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertEqual(cit["results"], [])
        self.assertTrue(verify_citations.check_search_result(cit)["found"])


class TestRegexCitations(ResolverTestCase):
    PATTERN = r"pio_sm_set_[a-z0-9_]*"

    def test_enumeration_is_the_tool_s_own_output(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "regex", "source": SOURCE, "pattern": self.PATTERN}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertEqual(cit["mode"], "regex")
        self.assertEqual(cit["query"], self.PATTERN)
        expected = pdf_search.find_distinct_matches(self.cache, self.PATTERN)
        self.assertEqual({r["match"] for r in cit["results"]},
                         {m["match"] for m in expected["matches"]})

    def test_resolved_enumeration_passes_the_verifier(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "regex", "source": SOURCE, "pattern": self.PATTERN}]}])
        cit = out["answers"][0]["citations"][0]
        self.assertTrue(verify_citations.check_search_result(cit)["found"])

    def test_invalid_pattern_is_an_authoring_error(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [
                {"type": "regex", "source": SOURCE, "pattern": "pio_sm_(["}]}])
        self.assertIn("not a valid regex", str(cm.exception))

    def test_too_broad_a_pattern_is_an_authoring_error(self):
        """A truncated enumeration could hide the symbol that refutes the
        claim, so breadth fails loudly here rather than silently."""
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [
                {"type": "regex", "source": SOURCE, "pattern": r"[a-z]+"}]}])
        self.assertIn("narrow it", str(cm.exception))

    def test_pattern_that_still_cannot_match_digits_is_left_to_the_verifier(self):
        """The digit blind spot is a property of the pattern, not of the
        transcription, so resolving cannot fix it — the verifier still must."""
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "regex", "source": SOURCE,
             "pattern": r"pio_sm_set_pindirs[a-z_]*"}]}])
        cit = out["answers"][0]["citations"][0]
        result = verify_citations.check_search_result(cit)
        self.assertFalse(result["found"])
        self.assertIn("mask64", result["reason"])


class TestSpecErrors(ResolverTestCase):
    def test_every_bad_citation_is_reported_in_one_pass(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([
                {"claim": "one", "citations": [quote(314, 1, 9999)]},
                {"claim": "two", "citations": [quote(99999, 1, 2)]},
            ])
        message = str(cm.exception)
        self.assertIn("claim 1", message)
        self.assertIn("claim 2", message)

    def test_unknown_citation_type_names_the_valid_ones(self):
        with self.assertRaises(resolve_answer.SpecError) as cm:
            self.resolve([{"claim": "c", "citations": [
                {"type": "verbatim", "source": SOURCE, "text": "..."}]}])
        self.assertIn("quote", str(cm.exception))

    def test_empty_answers_is_the_valid_unable_to_answer_output(self):
        out = self.resolve([])
        self.assertEqual(out["answers"], [])


class TestReadback(ResolverTestCase):
    """The agent never typed the quote, so the readback is its only look at
    what it actually cited."""

    def test_quote_text_is_echoed_in_full(self):
        out = self.resolve([{"claim": "c", "citations": [quote(314, 2, 6)]}])
        text = resolve_answer.readback(out)
        for line in out["answers"][0]["citations"][0]["text"].split("\n"):
            self.assertIn(line, text)

    def test_search_results_are_summarised_not_re_dumped(self):
        """They are already in the agent's context from the call that found
        them; an enumeration echoed in full would double-carry 100 snippets."""
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "regex", "source": SOURCE, "pattern": r"pio_sm_set_[a-z0-9_]*"}]}])
        text = resolve_answer.readback(out)
        cit = out["answers"][0]["citations"][0]
        self.assertIn(f"{len(cit['results'])} result(s)", text)
        self.assertNotIn(cit["results"][0]["text"], text)

    def test_a_search_finding_nothing_says_so(self):
        out = self.resolve([{"claim": "c", "citations": [
            {"type": "search", "source": SOURCE, "query": "zzz nothing zzz"}]}])
        self.assertIn("proves absence", resolve_answer.readback(out))

    def test_empty_answer_readback_is_explicit(self):
        self.assertIn("unable to answer",
                      resolve_answer.readback(self.resolve([])))


class TestEndToEndThroughTheFile(ResolverTestCase):
    def test_written_yaml_round_trips_through_the_verifier(self):
        spec = {"answers": [{"claim": "c", "citations": [
            quote(314, 2, 6),
            {"type": "search", "source": SOURCE, "query": "pioasm"},
            {"type": "regex", "source": SOURCE, "pattern": r"pio_sm_set_[a-z0-9_]*"},
        ]}]}
        out = resolve_answer.resolve_answer(spec, "q?")
        Path("answers").mkdir(exist_ok=True)
        path = Path("answers") / "round-trip.yml"
        path.write_text(resolve_answer.dump_yaml(out))

        loaded = verify_citations.load_yaml(str(path))
        self.assertEqual(loaded["question"], "q?")
        for cit in loaded["answers"][0]["citations"]:
            if cit.get("type") == "search_result":
                result = verify_citations.check_search_result(cit)
            else:
                result = verify_citations.check_spans(cit, self.cache)
            self.assertTrue(result["found"], msg=result.get("reason"))


if __name__ == "__main__":
    unittest.main()
