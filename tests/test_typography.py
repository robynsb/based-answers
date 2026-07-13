"""Typography tolerance: searching and verifying quotes whose PDF text uses
curly apostrophes, line breaks, etc. (the "A WAIT instruction's" incident)."""

import io
import unittest
from contextlib import redirect_stdout

from .support import load_script

pdf_search = load_script("pdf-search.py")
verify_citations = load_script("verify-citations.py")

# Mimics the page 318 chunk: curly apostrophe, phrase broken across lines
CHUNK_TEXT = (
    "State machines may momentarily pause execution\n"
    "for a number of reasons:\n"
    "• A WAIT instruction’s condition is not\n"
    "yet met"
)
DATA = {"chunks": [{"page": 318, "text": CHUNK_TEXT}]}


def search_output(query: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        pdf_search.cmd_search(DATA, query)
    return buf.getvalue()


class TestSearchTypography(unittest.TestCase):
    def test_straight_apostrophe_matches_curly(self):
        out = search_output("A WAIT instruction's condition is not yet met")
        self.assertIn("Page 318", out)

    def test_phrase_across_line_breaks(self):
        out = search_output("momentarily pause execution for a number of reasons")
        self.assertIn("Page 318", out)

    def test_snippet_is_verbatim(self):
        out = search_output("A WAIT instruction's condition")
        self.assertIn("instruction’s", out)

    def test_no_match(self):
        out = search_output("totally absent phrase")
        self.assertIn("No matches found.", out)


class TestVerifyTypography(unittest.TestCase):
    def test_straight_apostrophe_citation(self):
        r = verify_citations.check_citation(
            "A WAIT instruction's condition is not yet met", 318, DATA)
        self.assertTrue(r["found"])
        self.assertEqual(r["method"], "typography_folded")

    def test_verbatim_citation(self):
        r = verify_citations.check_citation(
            "A WAIT instruction’s condition is not yet met", 318, DATA)
        self.assertTrue(r["found"])

    def test_absent_citation_still_fails(self):
        r = verify_citations.check_citation(
            "this text is not on the page", 318, DATA)
        self.assertFalse(r["found"])


if __name__ == "__main__":
    unittest.main()
