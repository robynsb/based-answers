"""search_regex returns distinct matched *strings*, which is not what a
pattern written as a prose search means. Two misreadings cost run
…pin-directions-of-a-pio-11 most of its rounds — a pattern with nothing
variable in it, and a greedy `.*` — so the tool answers both in its own
output instead of leaving the agent to infer them from a confusing result."""

import io
import contextlib
import unittest

from .support import load_script

pdf_search = load_script("pdf-search.py")

CACHE = {"chunks": [
    {"page": 224, "text": "static uint8_t pio_sm_get_pc (PIO pio, uint sm)\n"
                          "Return the current program counter for a state machine."},
    {"page": 225, "text": "static uint pio_sm_get_rx_fifo_level (PIO pio, uint sm)\n"
                          "Return the number of elements currently in a state machine's RX FIFO."},
]}


class TestRegexNotes(unittest.TestCase):
    def test_a_pattern_with_no_variable_part_is_called_out(self):
        # The trap verbatim: a doc full of pio_sm_get_* dedupes to one row
        # whose "name" is the prefix, which reads as a family of one.
        notes = pdf_search.regex_notes("pio_sm_get_", [{"match": "pio_sm_get_", "page": 224}])
        self.assertEqual(len(notes), 1)
        self.assertIn("no character class, quantifier or wildcard", notes[0])
        # The suggestion has to be a pattern that actually works
        self.assertIn("pio_sm_get_[a-z0-9_]*", notes[0])

    def test_span_length_matches_are_called_out(self):
        notes = pdf_search.regex_notes("pio.*pindir", [{"match": "x" * 900, "page": 40}])
        self.assertEqual(len(notes), 1)
        self.assertIn("running text rather than names", notes[0])
        self.assertIn("900", notes[0])

    def test_both_at_once(self):
        notes = pdf_search.regex_notes("literal", [{"match": "y" * 100, "page": 1}])
        self.assertEqual(len(notes), 2)

    def test_a_working_family_pattern_gets_no_notes(self):
        matches = [{"match": "pio_sm_get_pc", "page": 224},
                   {"match": "pio_sm_get_rx_fifo_level", "page": 225}]
        self.assertEqual(pdf_search.regex_notes("pio_sm_get_[a-z0-9_]*", matches), [])

    def test_a_literal_that_found_nothing_still_gets_the_note(self):
        # Absence is the one honest use of a bare literal, but the agent
        # still needs to know it proved nothing about the family.
        notes = pdf_search.regex_notes("pio_sm_get_pindirs", [])
        self.assertEqual(len(notes), 1)


class TestRegexOutput(unittest.TestCase):
    def render(self, pattern):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pdf_search.cmd_search_regex(CACHE, pattern)
        return buf.getvalue()

    def test_suggested_repair_actually_enumerates_the_family(self):
        out = self.render("pio_sm_get_[a-z0-9_]*")
        self.assertIn("pio_sm_get_pc", out)
        self.assertIn("pio_sm_get_rx_fifo_level", out)
        self.assertNotIn("Note:", out)

    def test_degenerate_pattern_prints_the_note(self):
        out = self.render("pio_sm_get_")
        self.assertIn("1 distinct match(es)", out)
        self.assertIn("Note:", out)

    def test_long_match_label_is_truncated_in_the_header(self):
        out = self.render("pio.*machine")
        header = next(l for l in out.splitlines() if l.startswith("--- "))
        self.assertIn("chars]", header)
        self.assertLess(len(header), 120)


if __name__ == "__main__":
    unittest.main()
