"""Quotes that run across a page break (the "manipulated PC." incident):
the extraction puts running headers/footers between the two halves, so the
verifier must match a prefix on the stated page and the remainder on the
adjacent page, and the highlighter must mark the on-page portion."""

import unittest

from .support import load_script

verify_citations = load_script("verify-citations.py")
format_answers = load_script("format-answers.py")

# Mimics pages 343-345 of the RP2040 datasheet: the sentence breaks after
# "unless the written instruction itself", the page footer follows, and
# "manipulated PC." opens the next page
DATA = {"chunks": [
    {"page": 343, "text": "Some unrelated earlier page about state machines."},
    {"page": 344, "text": (
        "While the state machine is still\n"
        "running, the C program forces in a jmp instruction.\n"
        "When an instruction is written to the INSTR register, the state machine immediately decodes and executes that\n"
        "instruction. The program counter\n"
        "does not advance, so on the next cycle the state\n"
        "machine continues to execute its current program from the point where it left off, unless the written instruction itself\n"
        "RP2040 Datasheet\n"
        "3.5. Functional Details\n"
        "343"
    )},
    {"page": 345, "text": (
        "manipulated PC.\n"
        "Delay cycles are ignored on instructions written to the INSTR register, and execute immediately, ignoring the state\n"
        "machine clock divider."
    )},
]}

SPANNING_QUOTE = (
    "The program counter does not advance, so on the next cycle the state "
    "machine continues to execute its current program from the point where "
    "it left off, unless the written instruction itself manipulated PC."
)


class TestCrossPageCitations(unittest.TestCase):
    def test_quote_continuing_on_next_page(self):
        r = verify_citations.check_citation(SPANNING_QUOTE, 344, DATA)
        self.assertTrue(r["found"], r.get("reason"))
        self.assertEqual(r["method"], "cross_page")
        self.assertEqual(r["pages"], [344, 345])

    def test_quote_starting_on_previous_page(self):
        quote = ("unless the written instruction itself manipulated PC. "
                 "Delay cycles are ignored on instructions written to the INSTR register")
        r = verify_citations.check_citation(quote, 345, DATA)
        self.assertTrue(r["found"], r.get("reason"))
        self.assertEqual(r["method"], "cross_page")
        self.assertEqual(r["pages"], [344, 345])

    def test_quote_fully_on_page_still_passes(self):
        r = verify_citations.check_citation(
            "the state machine continues to execute its current program", 344, DATA)
        self.assertTrue(r["found"])
        self.assertNotEqual(r["method"], "cross_page")

    def test_trivial_stated_page_share_rejected(self):
        # "the state machine" (17 normalized chars) is on 344 and
        # "manipulated PC." is on 345, but the stated page's share of the
        # quote is below the minimum — a shared phrase must not validate it
        r = verify_citations.check_citation(
            "the state machine manipulated PC.", 344, DATA)
        self.assertFalse(r["found"])

    def test_wrong_page_reports_where_the_quote_is(self):
        r = verify_citations.check_citation(
            "Delay cycles are ignored on instructions written to the INSTR register",
            343, DATA)
        self.assertFalse(r["found"])
        self.assertIn("appears on page 345", r["reason"])

    def test_absent_quote_still_fails(self):
        r = verify_citations.check_citation(
            "this text is nowhere in the document at all", 344, DATA)
        self.assertFalse(r["found"])
        self.assertNotIn("appears on page", r["reason"])


PAGE_344_SPANS = [
    {"text": "from the point where it left off, unless the written", "bbox": [10.0, 700.0, 300.0, 710.0]},
    {"text": "instruction itself", "bbox": [10.0, 712.0, 100.0, 722.0]},
]
PAGE_345_SPANS = [
    {"text": "manipulated PC.", "bbox": [10.0, 60.0, 90.0, 70.0]},
    {"text": "Delay cycles are ignored on instructions", "bbox": [10.0, 72.0, 250.0, 82.0]},
]


class TestCrossPageHighlights(unittest.TestCase):
    def test_prefix_of_spanning_quote_highlighted(self):
        quote = ("from the point where it left off, unless the written "
                 "instruction itself manipulated PC.")
        lines = format_answers._multispan_highlights(PAGE_344_SPANS, quote)
        self.assertEqual(len(lines), 2)

    def test_suffix_of_spanning_quote_highlighted(self):
        quote = ("unless the written instruction itself manipulated PC. "
                 "Delay cycles are ignored on instructions")
        lines = format_answers._multispan_highlights(PAGE_345_SPANS, quote)
        self.assertEqual(len(lines), 2)

    def test_absent_quote_gets_no_highlight(self):
        lines = format_answers._multispan_highlights(
            PAGE_344_SPANS, "totally different words that are not here")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
