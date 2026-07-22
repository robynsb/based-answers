"""When a search_result snippet isn't on its page, say where it is or hand
back the real text — "copy the snippet verbatim from pdf_search's output" is
true and useless, since the agent believes it did. Run …-14 spent four verify
calls on one snippet with nothing but that sentence to go on."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from .support import load_script

verify_citations = load_script("verify-citations.py")

CACHE = {"chunks": [
    {"page": 671, "text": "Introduction to the onewire example and its wiring."},
    {"page": 672, "text": "38 // ow: pointer to an OW driver struct.\n"
                          "\xa039 // data: The word to be sent.\n"
                          "\xa040 void ow_send (OW *ow, uint data) {\n"
                          "\xa041     pio_sm_put_blocking (ow->pio, ow->sm, (uint32_t)data);\n"
                          "\xa042     pio_sm_get_blocking (ow->pio, ow->sm);  // discard it\n"},
    {"page": 673, "text": "Unrelated prose about the state machine's clock divider."},
]}


class TestVerbatimHelp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        work = Path(self._tmp.name)
        (work / "indexed-pdfs").mkdir()
        (work / "indexed-pdfs" / "sdk.pdf.json").write_text(json.dumps(CACHE))
        self._old = Path.cwd()
        os.chdir(work)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()

    def cit(self, text, page, mode=None):
        c = {"type": "search_result", "source": "sdk.pdf", "query": "pio_sm_get_blocking",
             "results": [{"page": page, "text": text}]}
        if mode:
            c["mode"] = mode
            c["results"][0]["match"] = "pio_sm_get_blocking"
        return c

    def test_right_text_wrong_page_names_the_real_page(self):
        r = verify_citations.check_search_result(
            self.cit("void ow_send (OW *ow, uint data) {", 671))
        self.assertFalse(r["found"])
        self.assertIn("it does appear on page 672", r["reason"])
        self.assertIn("use 672", r["reason"])

    def test_diverging_wording_gets_the_real_text_back(self):
        r = verify_citations.check_search_result(
            self.cit("void ow_send (OW *ow, unsigned int data) { // send it", 672))
        self.assertFalse(r["found"])
        self.assertIn("closest text on page 672", r["reason"])
        # Between the markers, and long enough to paste as-is
        body = r["reason"].split(">>>")[1].split("<<<")[0]
        self.assertIn("void ow_send (OW *ow, uint data)", body)

    def test_the_returned_text_actually_verifies(self):
        r = verify_citations.check_search_result(
            self.cit("void ow_send (OW *ow, unsigned int data) { // send it", 672))
        offered = r["reason"].split(">>>")[1].split("<<<")[0]
        # The whole point: pasting it back must end the loop
        again = verify_citations.check_search_result(self.cit(offered, 672))
        self.assertTrue(again["found"], again.get("reason"))

    def test_a_page_with_no_text_says_so(self):
        r = verify_citations.check_search_result(self.cit("anything at all", 999))
        self.assertFalse(r["found"])
        self.assertIn("no text was extracted for page 999", r["reason"])

    def test_regex_mode_gets_the_same_help(self):
        r = verify_citations.check_search_result(
            self.cit("void ow_send (OW *ow, unsigned int data) {", 672, mode="regex"))
        self.assertFalse(r["found"])
        self.assertIn("closest text on page 672", r["reason"])

    def test_a_correct_snippet_still_passes(self):
        r = verify_citations.check_search_result(
            self.cit("pio_sm_get_blocking (ow->pio, ow->sm);  // discard it", 672))
        self.assertTrue(r["found"], r.get("reason"))


class TestExpandShortCitation(TestVerbatimHelp):
    """The other half of run …-14's struggle: a citation that is on the page
    and simply too short. The agent has the right passage and no way to know
    which direction to grow it, so it guesses and re-runs."""

    def test_a_short_quote_is_widened_past_the_minimum(self):
        short = "void ow_send (OW *ow, uint data)"
        wider = verify_citations._expand_verbatim(
            short, 672, CACHE, verify_citations.MIN_CITATION_CHARS)
        self.assertIsNotNone(wider)
        self.assertIn(short, verify_citations.normalize_text(wider))
        self.assertGreater(len(wider), len(short))

    def test_the_widened_text_is_on_the_page_verbatim(self):
        wider = verify_citations._expand_verbatim(
            "void ow_send (OW *ow, uint data)", 672, CACHE, 200)
        page = verify_citations._page_text(CACHE, 672)
        self.assertIsNotNone(verify_citations._match_in_text(wider, page))

    def test_text_that_is_not_on_the_page_expands_to_nothing(self):
        self.assertIsNone(verify_citations._expand_verbatim("not here at all", 672, CACHE, 200))

    def test_a_whole_chunk_is_not_padded_beyond_itself(self):
        # target longer than the chunk: return what exists, never invent
        wider = verify_citations._expand_verbatim("ow_send", 672, CACHE, 100000)
        self.assertLessEqual(len(wider), len(CACHE["chunks"][1]["text"]))

    def test_the_failure_message_carries_the_wider_quote(self):
        import subprocess, sys as _sys
        from .support import SKILL_DIR
        y = Path("a.yml")
        y.write_text(
            'question: "q?"\n'
            'answers:\n'
            '  - claim: "c"\n'
            '    citations:\n'
            '      - text: "void ow_send (OW *ow, uint data)"\n'
            '        page: 672\n'
            '        source: "sdk.pdf"\n')
        proc = subprocess.run([_sys.executable, str(SKILL_DIR / "verify-citations.py"), str(y)],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Citation too short", proc.stdout)
        self.assertIn(">>>", proc.stdout)


if __name__ == "__main__":
    unittest.main()
