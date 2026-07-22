"""Line addressing: the numbering a search result reports is the numbering
`resolve_span` resolves, so a hit can be cited back as a quote without the
agent ever retyping the passage."""

import io
import unittest
from contextlib import redirect_stdout

from .support import load_script

pdf_search = load_script("pdf-search.py")

PDF = "tests/fixtures/RP-008371-DS-1-rp2040-datasheet.pdf"
DATA = pdf_search.load_or_extract(PDF)


class TestPageLines(unittest.TestCase):
    def test_page_lines_are_the_page_text(self):
        self.assertEqual(
            "\n".join(pdf_search.page_lines(DATA, 314)),
            pdf_search.page_text(DATA, 314),
        )

    def test_missing_page_is_none(self):
        self.assertIsNone(pdf_search.page_lines(DATA, 99999))


class TestResolveSpan(unittest.TestCase):
    def test_span_is_the_lines_get_prints(self):
        lines = pdf_search.page_lines(DATA, 314)
        self.assertEqual(
            pdf_search.resolve_span(DATA, 314, 4, 6),
            "\n".join(lines[3:6]),
        )

    def test_single_line_span(self):
        self.assertEqual(
            pdf_search.resolve_span(DATA, 314, 1, 1),
            pdf_search.page_lines(DATA, 314)[0],
        )

    def test_past_end_of_page_names_the_line_count(self):
        n = len(pdf_search.page_lines(DATA, 314))
        with self.assertRaises(ValueError) as cm:
            pdf_search.resolve_span(DATA, 314, 1, n + 1)
        self.assertIn(str(n), str(cm.exception))

    def test_inverted_range_rejected(self):
        with self.assertRaises(ValueError):
            pdf_search.resolve_span(DATA, 314, 9, 4)

    def test_missing_page_rejected(self):
        with self.assertRaises(ValueError):
            pdf_search.resolve_span(DATA, 99999, 1, 2)


class TestSearchReportsCitableSpans(unittest.TestCase):
    """The whole point of the numbering: what a search reports back can be
    resolved into the same text, so the agent cites the hit it just read."""

    def _assert_span_covers_query(self, match, query):
        span = pdf_search.resolve_span(DATA, match["page"], *match["lines"])
        norm_span, _ = pdf_search.normalize_for_match(span)
        norm_query, _ = pdf_search.normalize_for_match(query)
        self.assertIn(norm_query.strip().lower(), norm_span.lower())

    def test_literal_search_spans_contain_the_query(self):
        query = "set pindirs"
        matches = pdf_search.find_matches(DATA, query)
        self.assertTrue(matches)
        for m in matches[:5]:
            self._assert_span_covers_query(m, query)

    def test_regex_enumeration_spans_contain_the_match(self):
        result = pdf_search.find_distinct_matches(DATA, r"pio_sm_set_[a-z0-9_]*")
        self.assertIn("matches", result)
        self.assertTrue(result["matches"])
        for m in result["matches"][:5]:
            self._assert_span_covers_query(m, m["match"])


class TestGetOutput(unittest.TestCase):
    def test_get_numbers_every_line_from_one(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pdf_search.cmd_get(DATA, [314])
        printed = buf.getvalue().splitlines()
        lines = pdf_search.page_lines(DATA, 314)

        self.assertIn(f"({len(lines)} lines)", printed[0])
        body = [ln for ln in printed[1:] if ln.strip()]
        self.assertEqual(len(body), len(lines))
        for n, (out, src) in enumerate(zip(body, lines), 1):
            number, _, text = out.partition("| ")
            self.assertEqual(int(number.strip()), n)
            self.assertEqual(text, src)


if __name__ == "__main__":
    unittest.main()
