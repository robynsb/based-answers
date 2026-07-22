"""A searcher that returns zero tokens has not answered badly — it has not
run at all (rejected model, bad key, throttled provider). The loop must
abort after round 1 instead of retrying it MAX_ROUNDS times and reporting
`exhausted`, which reads as "tried and could not answer"."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import pi_rpc

from .support import load_script

based_answers = load_script("based-answers.py")


class TestSearcherProducedNothing(unittest.TestCase):
    def ledger_with(self, usage):
        ledger = based_answers.TokenLedger(None, emit_fn=lambda *a: None)
        if usage is not None:
            ledger.add("searcher", usage)
        return ledger

    def test_no_calls_yet_is_not_a_failure(self):
        # Before the first message lands there is nothing to conclude.
        self.assertFalse(based_answers.searcher_produced_nothing(self.ledger_with(None)))

    def test_zero_tokens_across_a_call(self):
        self.assertTrue(based_answers.searcher_produced_nothing(
            self.ledger_with({"input": 0, "output": 0})))

    def test_any_tokens_at_all_is_fine(self):
        self.assertFalse(based_answers.searcher_produced_nothing(
            self.ledger_with({"input": 12, "output": 0})))

    def test_cache_reads_alone_count_as_output(self):
        # cacheRead is a usage field, so a cache-served turn is a real turn.
        self.assertFalse(based_answers.searcher_produced_nothing(
            self.ledger_with({"cacheRead": 900})))

    def test_other_agents_do_not_mask_a_silent_searcher(self):
        ledger = self.ledger_with({"input": 0, "output": 0})
        ledger.add("semantic", {"input": 500, "output": 40})
        self.assertTrue(based_answers.searcher_produced_nothing(ledger))


class TestSearchRoundsAborts(unittest.TestCase):
    def run_rounds(self, usage):
        """Drive _search_rounds with a searcher that records `usage` per round
        and never writes YAML; returns the number of rounds it got through."""
        rounds_run = []

        def fake_search_round(session, message, what, ledger, **kwargs):
            rounds_run.append(what)
            ledger.add("searcher", usage)

        orig = based_answers.run_search_round
        based_answers.run_search_round = fake_search_round
        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                (Path(tmp) / "answers").mkdir()
                ledger = based_answers.TokenLedger(None, emit_fn=lambda *a: None)
                rounds: list[dict] = []
                err = None
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        based_answers._search_rounds(
                            None, "slug", "q?", [], rounds, ".", None, ledger)
                    except pi_rpc.PiError as e:
                        err = e
        finally:
            os.chdir(cwd)
            based_answers.run_search_round = orig
        return rounds_run, err

    def test_zero_token_round_one_aborts(self):
        rounds_run, err = self.run_rounds({"input": 0, "output": 0})
        self.assertEqual(len(rounds_run), 1)
        self.assertIsNotNone(err)
        # The message has to point at the configuration, not at the answer.
        self.assertIn("zero tokens", str(err))
        self.assertIn("BA_PI_MODEL", str(err))

    def test_a_working_searcher_still_uses_every_round(self):
        rounds_run, err = self.run_rounds({"input": 40, "output": 10})
        self.assertIsNone(err)
        self.assertEqual(len(rounds_run), based_answers.MAX_ROUNDS)
        self.assertEqual(rounds_run[0], "CONTEXT")


if __name__ == "__main__":
    unittest.main()
