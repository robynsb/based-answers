"""Streamed token deltas must reach the browser as whole lines.

`agent-line` is one line per event and the UI renders it that way. pi
streams text a token at a time, so feeding deltas straight to emit_line
put every word on its own line.
"""

import unittest

from .support import load_script


class TestLineBuffer(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")
        self.lines = []
        self.buf = self.based.LineBuffer(self.lines.append)

    def test_token_deltas_become_one_line(self):
        for tok in ["Now", " I", " have", " all", " the", " evidence", "."]:
            self.buf.feed(tok)
        self.assertEqual(self.lines, [], "emitted before seeing a newline")
        self.buf.flush()
        self.assertEqual(self.lines, ["Now I have all the evidence."])

    def test_newline_inside_a_delta_splits(self):
        self.buf.feed("first line\nsecond ")
        self.assertEqual(self.lines, ["first line"])
        self.buf.feed("line\n")
        self.assertEqual(self.lines, ["first line", "second line"])

    def test_several_newlines_in_one_delta(self):
        self.buf.feed("a\nb\nc\n")
        self.assertEqual(self.lines, ["a", "b", "c"])

    def test_blank_lines_are_preserved(self):
        """Markdown paragraph breaks must survive."""
        self.buf.feed("para one\n\npara two\n")
        self.assertEqual(self.lines, ["para one", "", "para two"])

    def test_flush_is_idempotent(self):
        self.buf.feed("tail")
        self.buf.flush()
        self.buf.flush()
        self.assertEqual(self.lines, ["tail"])

    def test_flush_of_empty_buffer_emits_nothing(self):
        self.buf.flush()
        self.assertEqual(self.lines, [])

    def test_trailing_newline_leaves_nothing_to_flush(self):
        self.buf.feed("done\n")
        self.buf.flush()
        self.assertEqual(self.lines, ["done"], "flush emitted a spurious blank")

    def test_a_line_split_across_many_deltas_is_reassembled(self):
        for ch in "hello world\n":
            self.buf.feed(ch)
        self.assertEqual(self.lines, ["hello world"])


if __name__ == "__main__":
    unittest.main()
