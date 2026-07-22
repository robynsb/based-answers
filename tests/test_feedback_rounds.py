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
    """The message goes to the session that already holds every earlier round,
    so it carries only the failures of the round just finished."""

    def test_only_the_latest_round(self):
        msg = based_answers.build_feedback_message(3, ROUNDS)
        self.assertIn(f"Round 3/{based_answers.MAX_ROUNDS}", msg)
        self.assertIn("round 2 failed (1 failure)", msg)
        self.assertIn("not coherent", msg)
        # round 1 is already in the session transcript — not resent
        self.assertNotIn("claim: A", msg)
        self.assertNotIn("claim: B", msg)

    def test_all_failures_of_that_round(self):
        msg = based_answers.build_feedback_message(2, ROUNDS[:2])
        self.assertIn("round 1 failed (2 failures)", msg)
        self.assertIn("claim: A", msg)
        self.assertIn("claim: B", msg)

    def test_no_rounds_yet(self):
        msg = based_answers.build_feedback_message(2, [])
        self.assertIn(f"Round 2/{based_answers.MAX_ROUNDS}", msg)


class TestWriteContext(unittest.TestCase):
    def _write(self, pdf_info=(), past_answers=()):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                Path("answers").mkdir()
                for name, question in past_answers:
                    (Path("answers") / name).write_text(
                        f'question: "{question}"\nanswers: []\n')
                return based_answers.write_context(
                    "slug", "how does a state machine set pin directions?",
                    list(pdf_info)).read_text()
            finally:
                os.chdir(old)

    def test_context_file_never_carries_a_feedback_history(self):
        """Only round 1 writes it, and later rounds send the newest feedback
        to the same session, so there is never a history to render."""
        text = self._write()
        self.assertNotIn("## Round", text)
        self.assertNotIn("Feedback", text)

    def test_the_question_and_the_sources_are_what_it_carries(self):
        text = self._write(pdf_info=[{"file": "rp2040.pdf", "pages": 640}])
        self.assertIn("how does a state machine set pin directions?", text)
        self.assertIn("rp2040.pdf (640 pages)", text)

    def test_past_answer_files_are_not_offered(self):
        """The searcher has pdf_search, verify_citations and write_answer —
        no read tool. Naming a file it cannot open is an instruction to do
        something impossible, so the ranked shortlist is gone."""
        text = self._write(past_answers=[
            ("pin-directions-of-a-pio.yml", "how does a pio set pin directions?"),
        ])
        self.assertNotIn(".yml", text)
        self.assertNotIn("how does a pio set pin directions?", text)


if __name__ == "__main__":
    unittest.main()
