"""Feedback numbering: several checks can fail in the same round (e.g. one
semantic failure per claim), and the searcher's feedback message and the
context file must group them under the round they happened in — not present
each failure as its own invented round."""

import os
import tempfile
import unittest
from pathlib import Path

from .support import load_script

based_answers = load_script("based-answers.py")

# Two semantic failures in round 1, then a coherence failure in round 2 —
# the shape of the incident: entries per failure, not per round
ROUNDS = [
    {"round": 1, "feedback": "Semantic checker FAILED for claim: A"},
    {"round": 1, "feedback": "Semantic checker FAILED for claim: B"},
    {"round": 2, "feedback": "Coherence checker FAILED:\nnot coherent"},
]


class TestGroupFeedbackByRound(unittest.TestCase):
    def test_groups_same_round_failures(self):
        grouped = based_answers.group_feedback_by_round(ROUNDS)
        self.assertEqual(
            grouped,
            [(1, ["Semantic checker FAILED for claim: A",
                  "Semantic checker FAILED for claim: B"]),
             (2, ["Coherence checker FAILED:\nnot coherent"])])

    def test_empty(self):
        self.assertEqual(based_answers.group_feedback_by_round([]), [])


class TestBuildFeedbackMessage(unittest.TestCase):
    def test_round_headers_match_actual_rounds(self):
        msg = based_answers.build_feedback_message(3, ROUNDS)
        self.assertIn(f"Round 3/{based_answers.MAX_ROUNDS}", msg)
        self.assertIn("--- Round 1 feedback (2 failures) ---", msg)
        self.assertIn("--- Round 2 feedback (1 failure) ---", msg)
        self.assertNotIn("Round 3 feedback", msg)
        # both round-1 failures sit under the single round-1 header
        round1_block = msg.split("--- Round 1 feedback")[1].split("--- Round 2")[0]
        self.assertIn("claim: A", round1_block)
        self.assertIn("claim: B", round1_block)


class TestWriteContextFeedback(unittest.TestCase):
    def test_context_file_groups_by_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                Path("answers").mkdir()
                path = based_answers.write_context("slug", "q?", [], ROUNDS)
                text = path.read_text()
            finally:
                os.chdir(old)
        self.assertEqual(text.count("### Round 1"), 1)
        self.assertEqual(text.count("### Round 2"), 1)
        self.assertNotIn("### Round 3", text)
        round1_block = text.split("### Round 1")[1].split("### Round 2")[0]
        self.assertIn("claim: A", round1_block)
        self.assertIn("claim: B", round1_block)


if __name__ == "__main__":
    unittest.main()
