"""A claim evidenced only by literal searches that found nothing is arguing
"I guessed N names and none existed, therefore no name exists" — the shape
that spun rounds 1-3 of …pin-directions-of-a-pio-10, with the checker naming
a different missing spelling each time. The verifier flags the shape with
fixed wording; it does not fail the claim, and the wording rides along with
whatever feedback the round produces."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .support import SKILL_DIR, load_script

verify_citations = load_script("verify-citations.py")
based_answers = load_script("based-answers.py")


def sr(query, results=()):
    return {"type": "search_result", "source": "sdk.pdf", "query": query,
            "results": list(results)}


class TestGuessListAdvisory(unittest.TestCase):
    def test_three_empty_literal_searches_are_flagged(self):
        answer = {"claim": "no such function exists",
                  "citations": [sr("a_get"), sr("a_read"), sr("a_query")]}
        note = verify_citations.guess_list_advisory(answer)
        self.assertIsNotNone(note)
        self.assertIn("mode: regex", note)

    def test_two_is_below_the_threshold(self):
        answer = {"claim": "c", "citations": [sr("a_get"), sr("a_read")]}
        self.assertIsNone(verify_citations.guess_list_advisory(answer))

    def test_a_quote_alongside_them_is_real_evidence(self):
        answer = {"claim": "c", "citations": [
            sr("a_get"), sr("a_read"), sr("a_query"),
            {"text": "x" * 300, "page": 1, "source": "sdk.pdf"}]}
        self.assertIsNone(verify_citations.guess_list_advisory(answer))

    def test_an_enumeration_alongside_them_is_real_evidence(self):
        answer = {"claim": "c", "citations": [
            sr("a_get"), sr("a_read"), sr("a_query"),
            {"type": "search_result", "mode": "regex", "source": "sdk.pdf",
             "query": "a_[a-z]*", "results": [{"match": "a_set", "page": 1, "text": "a_set"}]}]}
        self.assertIsNone(verify_citations.guess_list_advisory(answer))

    def test_searches_that_found_something_are_not_guesses(self):
        answer = {"claim": "c", "citations": [
            sr("a_get"), sr("a_read"), sr("a_query", [{"page": 2, "text": "a_query(...)"}])]}
        self.assertIsNone(verify_citations.guess_list_advisory(answer))


class TestAdvisoryReachesTheAgent(unittest.TestCase):
    def test_verifier_prints_it_and_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "indexed-pdfs").mkdir()
            (work / "indexed-pdfs" / "sdk.pdf.json").write_text(
                json.dumps({"chunks": [{"page": 1, "text": "nothing relevant here"}]}))
            y = work / "a.yml"
            y.write_text(
                'question: "q?"\n'
                'answers:\n'
                '  - claim: "no getter exists"\n'
                '    citations:\n'
                '      - {type: search_result, source: "sdk.pdf", query: "a_get", results: []}\n'
                '      - {type: search_result, source: "sdk.pdf", query: "a_read", results: []}\n'
                '      - {type: search_result, source: "sdk.pdf", query: "a_query", results: []}\n')
            proc = subprocess.run(
                [sys.executable, str(SKILL_DIR / "verify-citations.py"), str(y)],
                cwd=work, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Failed: 0", proc.stdout)
        self.assertIn("ADVISORY: ", proc.stdout)

    def test_advisory_is_appended_to_round_feedback(self):
        out = ("Total: 3  |  Passed: 3  |  Failed: 0\n"
               'ADVISORY: "no getter exists" — this claim\'s only evidence is 3 literal searches\n')
        block = based_answers.deterministic_advisories(out)
        self.assertIn("ADVISORY:", block)
        self.assertTrue(block.startswith("\n\nAlso note:"))

    def test_no_advisory_adds_nothing(self):
        self.assertEqual(
            based_answers.deterministic_advisories("Total: 3  |  Passed: 3  |  Failed: 0\n"), "")


if __name__ == "__main__":
    unittest.main()
