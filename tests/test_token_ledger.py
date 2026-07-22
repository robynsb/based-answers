"""Cumulative token/cost accounting, split by agent.

Usage comes from each completed assistant message (`message_end`), so the
totals accumulate across every pi session a question uses: the searcher's
one long session plus a fresh session per semantic and coherence check.
"""

import unittest

from . import support  # noqa: F401  (puts SKILL_DIR on sys.path)
import pi_rpc  # noqa: E402
from .support import load_script


def message_end(input=0, output=0, cacheRead=0, cacheWrite=0, cost=0.0):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {
                "input": input, "output": output,
                "cacheRead": cacheRead, "cacheWrite": cacheWrite,
                "cost": {"total": cost},
            },
        },
    }


class TestMessageUsage(unittest.TestCase):
    def test_extracts_and_totals(self):
        u = pi_rpc.message_usage(message_end(input=100, output=50, cost=0.001))
        self.assertEqual(u["input"], 100)
        self.assertEqual(u["output"], 50)
        self.assertEqual(u["total"], 150)
        self.assertAlmostEqual(u["cost"], 0.001)

    def test_total_includes_cache_tokens(self):
        u = pi_rpc.message_usage(message_end(input=10, output=5, cacheRead=40, cacheWrite=2))
        self.assertEqual(u["total"], 57)

    def test_ignores_other_events(self):
        self.assertIsNone(pi_rpc.message_usage({"type": "agent_end"}))
        self.assertIsNone(pi_rpc.message_usage(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}}))

    def test_ignores_user_messages(self):
        self.assertIsNone(pi_rpc.message_usage(
            {"type": "message_end", "message": {"role": "user", "content": "hi"}}))

    def test_missing_usage_block_is_none(self):
        self.assertIsNone(pi_rpc.message_usage(
            {"type": "message_end", "message": {"role": "assistant"}}))

    def test_absent_cost_does_not_crash(self):
        u = pi_rpc.message_usage(
            {"type": "message_end",
             "message": {"role": "assistant", "usage": {"input": 5, "output": 1}}})
        self.assertEqual(u["total"], 6)
        self.assertEqual(u["cost"], 0.0)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")
        self.emitted = []
        self.ledger = self.based.TokenLedger(
            "run-1", emit_fn=lambda rid, ev, data: self.emitted.append((rid, ev, data)))


class TestTokenLedger(LedgerCase):
    def test_accumulates_per_agent(self):
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100, output=20, cost=0.01)))
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=200, output=30, cost=0.02)))
        self.ledger.add("semantic", pi_rpc.message_usage(message_end(input=50, output=5, cost=0.003)))
        snap = self.ledger.snapshot()
        self.assertEqual(snap["by_agent"]["searcher"]["total"], 350)
        self.assertEqual(snap["by_agent"]["searcher"]["calls"], 2)
        self.assertAlmostEqual(snap["by_agent"]["searcher"]["cost"], 0.03)
        self.assertEqual(snap["by_agent"]["semantic"]["total"], 55)
        # Buckets appear only once an agent has actually spent something
        self.assertNotIn("coherence", snap["by_agent"])

    def test_run_total_sums_every_agent(self):
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100, cost=0.01)))
        self.ledger.add("semantic", pi_rpc.message_usage(message_end(input=10, cost=0.002)))
        self.ledger.add("coherence", pi_rpc.message_usage(message_end(input=5, cost=0.001)))
        total = self.ledger.snapshot()["total"]
        self.assertEqual(total["total"], 115)
        self.assertEqual(total["calls"], 3)
        self.assertAlmostEqual(total["cost"], 0.013)

    def test_emits_full_snapshot_each_time(self):
        """The UI rebuilds from one event, so replay lands on the same state."""
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100)))
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100)))
        self.assertEqual([e for _, e, _ in self.emitted], ["tokens", "tokens"])
        self.assertEqual(self.emitted[-1][2]["total"]["total"], 200,
                         "event carried a delta rather than the running total")

    def test_snapshot_is_a_copy(self):
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100)))
        snap = self.ledger.snapshot()
        snap["by_agent"]["searcher"]["total"] = 999999
        self.assertEqual(self.ledger.snapshot()["by_agent"]["searcher"]["total"], 100)

    def test_a_new_agent_needs_no_roster_edit(self):
        """Buckets are created on demand, so adding a fourth agent cannot
        silently report zero tokens for it."""
        self.ledger.add("a-new-agent", pi_rpc.message_usage(message_end(input=100)))
        snap = self.ledger.snapshot()
        self.assertEqual(snap["by_agent"]["a-new-agent"]["total"], 100)
        self.assertEqual(snap["total"]["total"], 100)

    def test_round_split_and_run_total_agree(self):
        self.ledger.set_round(1)
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100, cost=0.01)))
        self.ledger.add("semantic", pi_rpc.message_usage(message_end(input=10, cost=0.002)))
        self.ledger.set_round(2)
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=200, cost=0.02)))

        snap = self.ledger.snapshot()
        self.assertAlmostEqual(snap["by_round"]["1"]["by_agent"]["searcher"]["cost"], 0.01)
        self.assertAlmostEqual(snap["by_round"]["1"]["total"]["cost"], 0.012)
        self.assertAlmostEqual(snap["by_round"]["2"]["total"]["cost"], 0.02)
        self.assertNotIn("semantic", snap["by_round"]["2"]["by_agent"])
        # The split is a partition of the run, not a second, separate tally
        self.assertAlmostEqual(
            sum(r["total"]["cost"] for r in snap["by_round"].values()),
            snap["total"]["cost"])

    def test_round_keys_are_strings(self):
        """They cross JSON, where an int key comes back as a string anyway —
        one form, so the browser cannot look up the wrong one."""
        self.ledger.set_round(3)
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=1)))
        self.assertEqual(list(self.ledger.snapshot()["by_round"]), ["3"])

    def test_a_round_with_no_calls_still_appears(self):
        """Its timer and 'no model calls yet' still need somewhere to hang."""
        self.ledger.set_round(1)
        self.assertIn("1", self.ledger.snapshot()["by_round"])

    def test_usage_before_any_round_is_still_counted_in_the_run(self):
        """Nothing spends before round 1 today, but losing money silently is
        the wrong failure if something ever does."""
        self.ledger.add("searcher", pi_rpc.message_usage(message_end(input=100, cost=0.01)))
        snap = self.ledger.snapshot()
        self.assertAlmostEqual(snap["total"]["cost"], 0.01)
        self.assertEqual(snap["by_round"], {})

    def test_observer_only_records_message_end(self):
        observe = self.ledger.observer("coherence")
        observe({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}})
        observe({"type": "agent_end"})
        self.assertEqual(self.ledger.snapshot()["total"]["calls"], 0)
        observe(message_end(input=42, cost=0.004))
        snap = self.ledger.snapshot()
        self.assertEqual(snap["by_agent"]["coherence"]["total"], 42)
        self.assertAlmostEqual(snap["total"]["cost"], 0.004)


if __name__ == "__main__":
    unittest.main()
