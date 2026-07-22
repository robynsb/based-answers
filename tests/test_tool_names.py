"""The tool names in tools/*.ts must match what the pipeline allowlists.

pi does not validate `--tools`: an allowlist naming a tool that no extension
registers is accepted silently, leaving the agent with no tools at all. That
fails as a confusing "I can't search the PDFs" several rounds deep rather
than as a startup error, so the two lists are pinned together here.
"""

import re
import unittest
from pathlib import Path

from .support import SKILL_DIR, load_script

TOOLS_DIR = SKILL_DIR / "tools"


def registered_tool_names() -> set[str]:
    """Every name passed to pi.registerTool() across the checked-in tools."""
    names = set()
    for ts in sorted(TOOLS_DIR.glob("*.ts")):
        src = ts.read_text()
        for m in re.finditer(r"registerTool\(\{\s*name:\s*[\"']([^\"']+)[\"']", src):
            names.add(m.group(1))
    return names


class TestToolNames(unittest.TestCase):
    def setUp(self):
        self.based = load_script("based-answers.py")

    def test_every_allowlisted_tool_is_registered(self):
        registered = registered_tool_names()
        missing = set(self.based.SEARCH_TOOLS) - registered
        self.assertEqual(
            missing, set(),
            f"SEARCH_TOOLS names no extension registers: {sorted(missing)}. "
            f"pi would accept this silently and give the agent no such tool.")

    def test_every_registered_tool_is_allowlisted(self):
        registered = registered_tool_names()
        extra = registered - set(self.based.SEARCH_TOOLS)
        self.assertEqual(
            extra, set(),
            f"tools/ registers {sorted(extra)}, which SEARCH_TOOLS omits, so "
            f"the agent cannot call it.")

    def test_the_three_tools_are_present(self):
        self.assertEqual(registered_tool_names(),
                         {"pdf_search", "verify_citations", "write_answer"})

    def test_every_allowlisted_tool_has_an_extension_file(self):
        """--tools and -e are separate flags; both must cover the same tools."""
        passed = {Path(p).name for p in self.based.TOOL_EXTENSIONS}
        self.assertEqual(
            passed,
            {p.name for p in TOOLS_DIR.glob("*.ts")},
            "a tool file exists that TOOL_EXTENSIONS does not pass to pi with -e")


if __name__ == "__main__":
    unittest.main()
