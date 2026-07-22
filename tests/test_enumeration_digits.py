"""An enumeration pattern whose character classes cannot match digits gets
the wrong answer on names like pio_sm_set_pindirs_with_mask64 — and does so
invisibly, because the claimed-vs-actual set comparison reruns that same
crippled pattern and both sides agree. verify-citations must re-enumerate
with a digit-permissive variant and fail when it finds more."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from .support import load_script

verify_citations = load_script("verify-citations.py")

# The real shape of the RP2040 SDK page that broke this: three pindirs
# setters, one of which ends in a digit.
CACHE = {"chunks": [
    {"page": 40, "text": "void pio_sm_set_consecutive_pindirs (PIO pio, uint sm, uint pin_base)"},
    {"page": 41, "text": "void pio_sm_set_pindirs_with_mask (PIO pio, uint sm, uint32_t values)"},
    {"page": 41, "text": "void pio_sm_set_pindirs_with_mask64 (PIO pio, uint sm, uint64_t values)"},
]}


class TestDigitPermissiveVariant(unittest.TestCase):
    def test_adds_digits_to_a_digitless_class(self):
        self.assertEqual(
            verify_citations._digit_permissive_variant("pio_sm_set_[a-z][a-z_]*"),
            "pio_sm_set_[a-z0-9][a-z_0-9]*")

    def test_leaves_classes_that_already_match_digits(self):
        for pattern in ("pio_sm_set_[a-z0-9_]*", r"pio_[\w]*", r"pio_[\d]+", "x[a-z5]"):
            self.assertIsNone(verify_citations._digit_permissive_variant(pattern), pattern)

    def test_no_class_means_nothing_to_repair(self):
        self.assertIsNone(verify_citations._digit_permissive_variant(r"pio_sm_get\w*"))
        self.assertIsNone(verify_citations._digit_permissive_variant("pio_sm_get"))

    def test_negated_class_is_left_alone(self):
        self.assertIsNone(verify_citations._digit_permissive_variant("pio[^a-z]"))

    def test_escapes_and_literal_brackets_survive(self):
        self.assertEqual(verify_citations._digit_permissive_variant(r"a\[b[a-z]\]"),
                         r"a\[b[a-z0-9]\]")
        # A ] as the first class member is literal, not a terminator
        self.assertEqual(verify_citations._digit_permissive_variant("x[]a-z]"), "x[]a-z0-9]")


class TestEnumerationDigitCheck(unittest.TestCase):
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

    def test_digitless_class_truncating_a_name_fails(self):
        # Exactly the round-3 pattern: it reports ..._with_mask and never
        # reveals that _with_mask64 exists, yet the set comparison passes.
        cit = self.cit("pio_sm_set_[a-z][a-z_]*",
                       ["pio_sm_set_consecutive_pindirs", "pio_sm_set_pindirs_with_mask"])
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("cannot match digits", r["reason"])
        self.assertIn("pio_sm_set_pindirs_with_mask64", r["reason"])

    def test_digitless_class_dropping_a_name_fails(self):
        # The omission form: the required "\s*\(" suffix cannot follow "64",
        # so mask64 vanishes from the enumeration entirely.
        cit = self.cit(r"pio_sm_set_[a-z_]*\s*\(",
                       ["pio_sm_set_consecutive_pindirs (", "pio_sm_set_pindirs_with_mask ("])
        r = verify_citations.check_search_result(cit)
        self.assertFalse(r["found"])
        self.assertIn("pio_sm_set_pindirs_with_mask64 (", r["reason"])

    def test_digit_permissive_pattern_passes(self):
        cit = self.cit("pio_sm_set_[a-z][a-z0-9_]*",
                       ["pio_sm_set_consecutive_pindirs", "pio_sm_set_pindirs_with_mask",
                        "pio_sm_set_pindirs_with_mask64"])
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))

    def test_digitless_class_that_changes_nothing_passes(self):
        # No digit-bearing name in this family, so the exclusion is harmless
        # and must not be reported — the check fails on evidence, not syntax.
        cit = self.cit("pio_sm_set_[a-z]*ecutive_pindirs", ["pio_sm_set_consecutive_pindirs"])
        r = verify_citations.check_search_result(cit)
        self.assertTrue(r["found"], r.get("reason"))


if __name__ == "__main__":
    unittest.main()
