"""Semantic checking stops at the first failing claim: the round goes back
to the search agent without spending checker runs on the remaining claims."""

import tempfile
import unittest
from pathlib import Path

from .support import load_script

based_answers = load_script("based-answers.py")

YAML = """question: "q?"
answers:
  - claim: "claim one"
    citations: [{text: "t1", page: 1, source: "s.pdf"}]
  - claim: "claim two"
    citations: [{text: "t2", page: 1, source: "s.pdf"}]
  - claim: "claim three"
    citations: [{text: "t3", page: 1, source: "s.pdf"}]
"""


class TestSemanticFailFast(unittest.TestCase):
    def run_with_stub(self, verdicts):
        """Run run_semantic_checkers with run_checker stubbed to return the
        given verdict per call; returns (claim indices checked, failures)."""
        calls = []

        def fake_checker(rubric, **kwargs):
            calls.append(kwargs.get("extra", {}).get("claim"))
            return verdicts[len(calls) - 1]

        orig = based_answers.run_checker
        based_answers.run_checker = fake_checker
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "a.yml"
                p.write_text(YAML)
                failures = based_answers.run_semantic_checkers(p)
        finally:
            based_answers.run_checker = orig
        return calls, failures

    def test_stops_at_first_failure(self):
        calls, failures = self.run_with_stub(["PASS", "FAIL: nope", "PASS"])
        self.assertEqual(calls, [0, 1])  # claim three never checked
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["claim"], "claim two")

    def test_all_passing_claims_all_checked(self):
        calls, failures = self.run_with_stub(["PASS", "PASS", "PASS"])
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
