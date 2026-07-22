"""An enumeration pattern that spells out every string it can match is
circular: `a|b|c` is offered as proof that a, b and c are the only members of
a family, but it could never have found a fourth. The set comparison can't
see this — the rerun uses the same pattern and agrees — and the digit repair
has no character class to widen. This is the round-5 failure of
…pin-directions-of-a-pio-10, caught deterministically instead."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from .support import load_script

verify_citations = load_script("verify-citations.py")

CACHE = {"chunks": [
    {"page": 40, "text": "void pio_sm_set_consecutive_pindirs (PIO pio, uint sm, uint pin_base)"},
    {"page": 41, "text": "void pio_sm_set_pindirs_with_mask (PIO pio, uint sm, uint32_t values)"},
    {"page": 41, "text": "void pio_sm_set_pindirs_with_mask64 (PIO pio, uint sm, uint64_t values)"},
]}


class TestSpelledOutLiterals(unittest.TestCase):
    def test_plain_alternation_is_all_literals(self):
        self.assertEqual(verify_citations._spelled_out_literals("abc|def"), ["abc", "def"])

    def test_single_literal(self):
        self.assertEqual(verify_citations._spelled_out_literals("abc"), ["abc"])

    def test_escaped_metacharacters_are_still_literal(self):
        self.assertEqual(verify_citations._spelled_out_literals(r"a\.b|c\(d"), ["a.b", "c(d"])

    def test_a_class_makes_it_generative(self):
        self.assertIsNone(verify_citations._spelled_out_literals("pio_[a-z]"))

    def test_a_quantifier_makes_it_generative(self):
        for pattern in ("ab*", "ab+", "ab?", "a{2,3}", "a.b"):
            self.assertIsNone(verify_citations._spelled_out_literals(pattern), pattern)

    def test_a_shorthand_class_makes_it_generative(self):
        for pattern in (r"pio\w", r"pio\d", r"pio\s"):
            self.assertIsNone(verify_citations._spelled_out_literals(pattern), pattern)

    def test_alternation_inside_a_group_is_not_split_at_top_level(self):
        self.assertEqual(verify_citations._spelled_out_literals("(a|b)c"), ["(a|b)c"])


class TestEnumerationGeneralityCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        work = Path(self._tmp.name)
        (work / "indexed-pdfs").mkdir()
        (work / "indexed-pdfs" / "sdk.pdf.json").write_text(json.dumps(CACHE))
        self._old_cwd = Path.cwd()
        os.chdir(work)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def cit(self, pattern, matches):
        by_page = {c["text"]: c["page"] for c in CACHE["chunks"]}
        results = []
        for m in matches:
            text = next(t for t in by_page if m in t)
            results.append({"match": m, "page": by_page[text], "text": text})
        return {"type": "search_result", "mode": "regex", "source": "sdk.pdf",
                "query": pattern, "results": results}

    def test_alternation_of_the_names_it_proves_fails(self):
        # Verbatim from the run: three literals used to show those three are
        # the only ones. Every other check passes it.
        names = ["pio_sm_set_pindirs_with_mask64", "pio_sm_set_pindirs_with_mask",
                 "pio_sm_set_consecutive_pindirs"]
        cit = self.cit("|".join(names), names)
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("no character class, quantifier or wildcard", r["reason"])

    def test_a_family_pattern_passes(self):
        cit = self.cit("pio_sm_set_[a-z0-9_]*pindirs[a-z0-9_]*",
                       ["pio_sm_set_consecutive_pindirs", "pio_sm_set_pindirs_with_mask",
                        "pio_sm_set_pindirs_with_mask64"])
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))

    def test_a_literal_that_finds_nothing_is_left_alone(self):
        # Absence of one exact string is a real (if narrow) result, not a
        # circular exhaustiveness argument — fail on evidence, not syntax.
        cit = self.cit("pio_sm_get_pindirs", [])
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))


if __name__ == "__main__":
    unittest.main()
