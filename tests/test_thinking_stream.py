"""Reasoning tokens reach the browser, tagged, and stay out of the reply.

A reasoning model streams its thinking as its own content block. It is worth
showing — it is most of what an agent is doing — but it must be marked as
thinking, and it must never join the assistant text: `run_checker`'s callers
match PASS/FAIL against that text, and a checker that weighs a failure aloud
before answering PASS would otherwise be read as a failure.
"""

import io
import unittest
from contextlib import redirect_stdout

import pi_rpc

from .support import load_script


def update(kind, delta):
    return {"type": "message_update",
            "assistantMessageEvent": {"type": kind, "delta": delta}}


class FakeSession:
    """Replays a canned event stream through `prompt`'s on_event hook."""

    def __init__(self, events):
        self.events = events

    def prompt(self, message, on_event=None, timeout=900.0):
        for ev in self.events:
            on_event(ev)
        return {"settled": True, "errors": []}


class TestThinkingDelta(unittest.TestCase):
    def test_thinking_delta_is_recognised(self):
        self.assertEqual(pi_rpc.thinking_delta(update("thinking_delta", "hm")), "hm")

    def test_text_and_thinking_do_not_cross(self):
        self.assertIsNone(pi_rpc.thinking_delta(update("text_delta", "hi")))
        self.assertIsNone(pi_rpc.text_delta(update("thinking_delta", "hm")))

    def test_block_boundaries_are_not_deltas(self):
        for kind in ("thinking_start", "thinking_end"):
            self.assertIsNone(pi_rpc.thinking_delta({
                "type": "message_update", "assistantMessageEvent": {"type": kind}}))


class TestStreamPrompt(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")
        self.emitted = []
        self.based.emit = lambda run_id, event, data: self.emitted.append((event, data))

    def run_events(self, events):
        session = FakeSession(events)
        ledger = self.based.TokenLedger(None, emit_fn=lambda *a: None)
        with redirect_stdout(io.StringIO()):
            return self.based.stream_prompt(session, "go", agent="semantic",
                                            color="", ledger=ledger, run_id="r1")

    def lines(self):
        return [(d.get("kind"), d["line"])
                for e, d in self.emitted if e == "agent-line"]

    def test_thinking_is_tagged_and_text_is_not(self):
        self.run_events([update("thinking_delta", "weighing it\n"),
                         update("text_delta", "PASS\n")])
        self.assertEqual(self.lines(), [("thinking", "weighing it"), (None, "PASS")])

    def test_thinking_stays_out_of_the_returned_text(self):
        out = self.run_events([update("thinking_delta", "this could FAIL\n"),
                               update("text_delta", "PASS\n")])
        self.assertEqual(out, "PASS\n")
        self.assertNotIn("FAIL", out)

    def test_switching_block_flushes_the_partial_line_first(self):
        """An unterminated thinking line must not swallow the reply's first line."""
        self.run_events([update("thinking_delta", "no trailing newline"),
                         update("text_delta", "PASS\n")])
        self.assertEqual(self.lines(),
                         [("thinking", "no trailing newline"), (None, "PASS")])

    def test_trailing_thinking_is_flushed_at_the_end(self):
        self.run_events([update("thinking_delta", "cut off mid-thought")])
        self.assertEqual(self.lines(), [("thinking", "cut off mid-thought")])

    def test_claim_index_still_rides_along(self):
        session = FakeSession([update("thinking_delta", "hm\n")])
        ledger = self.based.TokenLedger(None, emit_fn=lambda *a: None)
        with redirect_stdout(io.StringIO()):
            self.based.stream_prompt(session, "go", agent="semantic", color="",
                                     ledger=ledger, run_id="r1", extra={"claim": 2})
        data = [d for e, d in self.emitted if e == "agent-line"][0]
        self.assertEqual((data["claim"], data["kind"]), (2, "thinking"))

    def test_extra_is_not_mutated_by_the_thinking_tag(self):
        extra = {"claim": 0}
        session = FakeSession([update("thinking_delta", "hm\n")])
        ledger = self.based.TokenLedger(None, emit_fn=lambda *a: None)
        with redirect_stdout(io.StringIO()):
            self.based.stream_prompt(session, "go", agent="semantic", color="",
                                     ledger=ledger, run_id="r1", extra=extra)
        self.assertEqual(extra, {"claim": 0})


if __name__ == "__main__":
    unittest.main()
