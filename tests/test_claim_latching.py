"""A claim whose text is unchanged since a round in which it passed is not
re-judged: the semantic checker is not deterministic at the margin, so a
second verdict on identical text is a re-roll, not new evidence."""

import tempfile
import unittest
from pathlib import Path

from .support import load_script

based_answers = load_script("based-answers.py")

TWO_CLAIMS = """question: "q?"
answers:
  - claim: "claim one"
    citations: [{text: "t1", page: 1, source: "s.pdf"}]
  - claim: "claim two"
    citations: [{text: "t2", page: 1, source: "s.pdf"}]
"""

REWORDED_SECOND = """question: "q?"
answers:
  - claim: "claim one"
    citations: [{text: "t1", page: 1, source: "s.pdf"}]
  - claim: "claim two, but stated more carefully"
    citations: [{text: "t2", page: 1, source: "s.pdf"}]
"""


class TestClaimLatching(unittest.TestCase):
    def run_round(self, yaml_text, verdicts, passed_claims):
        """One semantic pass over yaml_text, with the checker's verdict fixed
        per claim index; returns (claims actually sent to the checker,
        failures)."""
        checked = []

        def fake_checker(rubric, ledger=None, **kwargs):
            i = kwargs.get("extra", {}).get("claim")
            checked.append(i)
            return verdicts[i]

        orig = based_answers.run_checker
        based_answers.run_checker = fake_checker
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "a.yml"
                p.write_text(yaml_text)
                failures = based_answers.run_semantic_checkers(
                    p, based_answers.TokenLedger(None), passed_claims=passed_claims)
        finally:
            based_answers.run_checker = orig
        return checked, failures

    def test_passed_claim_is_not_rechecked_next_round(self):
        latched = set()
        checked, failures = self.run_round(TWO_CLAIMS, {0: "PASS", 1: "FAIL: nope"}, latched)
        self.assertEqual(checked, [0, 1])
        self.assertEqual(len(failures), 1)
        self.assertEqual(latched, {"claim one"})

        # Next round: the searcher re-offers claim one verbatim. Even with the
        # checker now primed to fail it, it is never asked — this is the exact
        # shape of the run that lost a passing claim to a re-roll in round 5.
        checked, failures = self.run_round(TWO_CLAIMS, {0: "FAIL: re-roll", 1: "PASS"}, latched)
        self.assertEqual(checked, [1])
        self.assertEqual(failures, [])
        self.assertEqual(latched, {"claim one", "claim two"})

    def test_reworded_claim_is_rechecked(self):
        latched = set()
        self.run_round(TWO_CLAIMS, {0: "PASS", 1: "FAIL: nope"}, latched)
        checked, _ = self.run_round(REWORDED_SECOND, {0: "PASS", 1: "PASS"}, latched)
        self.assertEqual(checked, [1])  # claim one latched, reworded claim two judged

    def test_no_latch_set_preserves_old_behaviour(self):
        checked, failures = self.run_round(TWO_CLAIMS, {0: "PASS", 1: "PASS"}, None)
        self.assertEqual(checked, [0, 1])
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
