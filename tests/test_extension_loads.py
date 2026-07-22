"""The tools are TypeScript that only pi ever compiles.

`test_tool_names.py` pins the names the extensions register against the
allowlist, but nothing there would notice a file pi cannot parse — which
surfaces at runtime as a round that mysteriously has no tools. Starting pi
with each extension and no prompt costs no API call and fails loudly on a
parse error, so it is checked here instead of in a live run.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .support import SKILL_DIR

sys.path.insert(0, str(SKILL_DIR))
import pi_rpc  # noqa: E402


@unittest.skipUnless(shutil.which("pi"), "pi is not on PATH")
class TestExtensionsLoad(unittest.TestCase):
    def _start_and_exit(self, extension: Path) -> subprocess.CompletedProcess:
        """Run pi with one extension and immediately close stdin."""
        cmd = pi_rpc.build_command(
            extensions=[extension],
            tools=[],
            system_prompt="x",
        )
        env = dict(
            os.environ,
            BA_PYTHON=sys.executable,
            BA_SKILL_DIR=str(SKILL_DIR),
            ANSWER_SLUG="extension-load-test",
            ANSWER_QUESTION="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            env["PI_CODING_AGENT_DIR"] = tmp
            return subprocess.run(cmd, input="", capture_output=True,
                                  text=True, env=env, cwd=tmp, timeout=120)

    def test_every_tool_extension_loads(self):
        for path in sorted((SKILL_DIR / "tools").glob("*.ts")):
            with self.subTest(extension=path.name):
                proc = self._start_and_exit(path)
                self.assertEqual(proc.returncode, 0,
                                 msg=f"{path.name}: {proc.stderr.strip()}")
                self.assertNotIn("Failed to load extension", proc.stderr)

    def test_a_broken_extension_would_be_caught(self):
        """Without this the test above could pass for the wrong reason."""
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.ts"
            broken.write_text("export default function ((( {\n")
            proc = self._start_and_exit(broken)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to load extension", proc.stderr)


if __name__ == "__main__":
    unittest.main()
