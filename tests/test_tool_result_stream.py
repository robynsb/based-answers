"""The verifier's result is echoed into the searcher's stream, so the browser
shows which citations failed and why at the moment the agent saw it — that is
the whole reason a round repeats. Other tools' results are not: a pdf_search
enumeration runs to 100 matches with snippets and would bury the agent's own
reasoning in the stream meant to explain it."""

import io
import unittest
from contextlib import redirect_stdout

import pi_rpc

from .support import load_script


def tool_end(name, text, is_error=False):
    return {"type": "tool_execution_end", "toolName": name, "isError": is_error,
            "result": {"content": [{"type": "text", "text": text}]}}


class FakeSession:
    def __init__(self, events):
        self.events = events

    def prompt(self, message, on_event=None, timeout=900.0):
        for ev in self.events:
            on_event(ev)
        return {"settled": True, "errors": []}


class TestToolResultText(unittest.TestCase):
    def test_reads_name_text_and_error_flag(self):
        self.assertEqual(pi_rpc.tool_result_text(tool_end("verify_citations", " out ")),
                         ("verify_citations", "out", False))

    def test_error_flag_survives(self):
        self.assertEqual(
            pi_rpc.tool_result_text(tool_end("verify_citations", "boom", True))[2], True)

    def test_non_text_blocks_are_skipped(self):
        ev = {"type": "tool_execution_end", "toolName": "t",
              "result": {"content": [{"type": "image"}, {"type": "text", "text": "keep"}]}}
        self.assertEqual(pi_rpc.tool_result_text(ev)[1], "keep")

    def test_other_events_are_not_tool_results(self):
        self.assertIsNone(pi_rpc.tool_result_text({"type": "tool_execution_start"}))
        self.assertIsNone(pi_rpc.tool_result_text({"type": "message_end"}))

    def test_a_result_with_no_content_is_empty_not_an_error(self):
        ev = {"type": "tool_execution_end", "toolName": "t", "result": {}}
        self.assertEqual(pi_rpc.tool_result_text(ev), ("t", "", False))


class TestSearcherStream(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")
        self.lines = []
        self.based.emit = lambda run_id, event, data: (
            self.lines.append(data["line"]) if event == "agent-line" else None)

    def run_events(self, events):
        ledger = self.based.TokenLedger(None, emit_fn=lambda *a: None)
        with redirect_stdout(io.StringIO()):
            self.based.run_search_round(FakeSession(events), "go", "CONTEXT", ledger)
        return "\n".join(self.lines)

    def test_verifier_result_is_echoed(self):
        table = "Total: 3  |  Passed: 2  |  Failed: 1\nclaim  cit  40  FAIL (not found)"
        out = self.run_events([tool_end("verify_citations", table)])
        self.assertIn("verify_citations result", out)
        self.assertIn("Failed: 1", out)
        self.assertIn("FAIL (not found)", out)

    def test_a_failing_call_says_so(self):
        out = self.run_events([tool_end("verify_citations", "traceback", True)])
        self.assertIn("verify_citations FAILED", out)

    def test_search_results_are_not_echoed(self):
        out = self.run_events([tool_end("pdf_search", "x" * 5000)])
        self.assertNotIn("x" * 100, out)

    def test_a_long_result_is_truncated_with_a_count(self):
        out = self.run_events([tool_end("verify_citations", "y" * 6000)])
        self.assertIn("more characters]", out)
        self.assertLess(len(out), 6000)

    def test_an_empty_result_emits_no_block(self):
        # Only the round's own context banner, no empty "result" block
        self.assertNotIn("verify_citations", self.run_events([tool_end("verify_citations", "")]))

    def test_the_start_note_still_comes_first(self):
        events = [{"type": "tool_execution_start", "toolName": "verify_citations",
                   "args": {"yaml": "answers/a.yml"}},
                  tool_end("verify_citations", "Total: 1  |  Passed: 1  |  Failed: 0")]
        out = self.run_events(events)
        self.assertLess(out.index("[tool] verify_citations"), out.index("verify_citations result"))


if __name__ == "__main__":
    unittest.main()
