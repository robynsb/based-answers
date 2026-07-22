"""PiSession against a fake pi that speaks the RPC protocol.

pi itself is not needed (or installed) to test the client: the fake is a
small executable that reads commands on stdin and replays a scripted event
sequence on stdout, so framing, settle-detection and failure modes are all
exercised deterministically.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from . import support  # noqa: F401  (puts SKILL_DIR on sys.path)
import pi_rpc  # noqa: E402


def make_fake_pi(script_body: str, version: str | None = "0.79.1") -> str:
    """Write an executable fake `pi` and return its path.

    `version` is what it reports to `--version`, which is how PiSession
    decides whether to expect an `agent_settled` event: 0.79 has none, 0.80+
    does, and None (unparseable) falls back to the grace period.
    """
    d = tempfile.mkdtemp()
    path = os.path.join(d, "fake-pi")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n")
        f.write("import json, sys\n")
        f.write("if '--version' in sys.argv:\n")
        f.write(f"    print({(version or 'unknown')!r}); sys.exit(0)\n")
        f.write("def emit(o):\n")
        f.write("    sys.stdout.write(json.dumps(o) + '\\n')\n")
        f.write("    sys.stdout.flush()\n")
        f.write("def read():\n")
        f.write("    line = sys.stdin.readline()\n")
        f.write("    return json.loads(line) if line.strip() else None\n")
        f.write(script_body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def session(fake: str, **kw) -> pi_rpc.PiSession:  # noqa: D103
    tmp = tempfile.mkdtemp()
    return pi_rpc.PiSession(
        cwd=tmp,
        config_dir=os.path.join(tmp, "pi"),
        extensions=[],
        tools=["pdf_search"],
        system_prompt="test",
        pi_bin=fake,
        **kw,
    )


class TestBuildCommand(unittest.TestCase):
    def test_isolation_flags_present(self):
        cmd = pi_rpc.build_command(
            extensions=["/a/one.ts", "/a/two.ts"],
            tools=["pdf_search", "write_answer"],
            system_prompt="be terse",
            session_dir="/tmp/s",
            model="deepseek/deepseek-chat",
        )
        # No global or project state may leak into a run.
        self.assertIn("--no-extensions", cmd)
        self.assertIn("--no-approve", cmd)
        # The agent gets exactly the pipeline's tools — no bash, no edit.
        self.assertIn("--no-builtin-tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "pdf_search,write_answer")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "be terse")
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek/deepseek-chat")
        self.assertEqual(cmd[cmd.index("--session-dir") + 1], "/tmp/s")

    def test_every_extension_passed_by_path(self):
        cmd = pi_rpc.build_command(
            extensions=["/a/one.ts", "/a/two.ts"], tools=["t"], system_prompt="p",
        )
        passed = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
        self.assertEqual(passed, ["/a/one.ts", "/a/two.ts"])

    def test_optional_flags_omitted(self):
        cmd = pi_rpc.build_command(extensions=[], tools=["t"], system_prompt="p")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--session-dir", cmd)


class TestPromptLifecycle(unittest.TestCase):
    def test_returns_when_settled(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'command': 'prompt', 'success': True})\n"
            "emit({'type': 'agent_start'})\n"
            "emit({'type': 'agent_end', 'willRetry': False})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        with session(fake) as s:
            out = s.prompt("hello", timeout=30)
        self.assertTrue(out["settled"])

    def test_agent_end_alone_does_not_end_the_round(self):
        """A retry after agent_end must still be waited out."""
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_end', 'willRetry': True})\n"
            "emit({'type': 'auto_retry_start'})\n"
            "emit({'type': 'agent_end', 'willRetry': False})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        seen = []
        with session(fake) as s:
            s.prompt("hi", on_event=lambda e: seen.append(e["type"]), timeout=30)
        self.assertIn("auto_retry_start", seen)
        self.assertEqual(seen.count("agent_end"), 2)

    def test_settles_on_agent_end_when_there_is_no_agent_settled(self):
        """pi 0.79 (the nixpkgs version) has no agent_settled event at all."""
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_end', 'messages': []})\n"
            "import time; time.sleep(20)\n"  # stays alive, emits nothing further
        )
        with session(fake) as s:
            out = s.prompt("hi", timeout=25)
        self.assertTrue(out["settled"])

    def test_work_resuming_after_agent_end_disarms_the_settle(self):
        """A queued continuation on 0.79 must not be cut short by the grace."""
        fake = make_fake_pi(
            "import time\n"
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_end', 'messages': []})\n"
            "emit({'type': 'agent_start'})\n"          # resumed within the grace
            "time.sleep(4)\n"
            "emit({'type': 'late', 'n': 1})\n"
            "emit({'type': 'agent_end', 'willRetry': False})\n"
            "emit({'type': 'agent_settled'})\n",
            version="0.81.1",
        )
        seen = []
        with session(fake) as s:
            s.prompt("hi", on_event=lambda e: seen.append(e.get("type")), timeout=30)
        self.assertIn("late", seen, "settled before the resumed work finished")

    def test_exit_right_after_agent_end_is_a_settle_not_a_crash(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_end', 'messages': []})\n"
            "sys.exit(0)\n"
        )
        with session(fake) as s:
            out = s.prompt("hi", timeout=25)
        self.assertTrue(out["settled"])

    def test_streams_events_to_callback(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'message_update', 'assistantMessageEvent': "
            "     {'type': 'text_delta', 'delta': 'par'}})\n"
            "emit({'type': 'message_update', 'assistantMessageEvent': "
            "     {'type': 'text_delta', 'delta': 'tial'}})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        deltas = []
        with session(fake) as s:
            s.prompt("hi", on_event=lambda e: deltas.append(pi_rpc.text_delta(e)),
                     timeout=30)
        self.assertEqual("".join(d for d in deltas if d), "partial")

    def test_two_prompts_share_one_process(self):
        """Round 2 feedback goes to the same conversation — no new session."""
        fake = make_fake_pi(
            "while True:\n"
            "    cmd = read()\n"
            "    if cmd is None: break\n"
            "    emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "    emit({'type': 'echo', 'message': cmd['message']})\n"
            "    emit({'type': 'agent_settled'})\n"
        )
        echoes = []
        with session(fake) as s:
            pid_before = s.proc.pid
            s.prompt("round one", on_event=lambda e: echoes.append(e), timeout=30)
            s.prompt("round two", on_event=lambda e: echoes.append(e), timeout=30)
            self.assertEqual(s.proc.pid, pid_before)
        msgs = [e["message"] for e in echoes if e.get("type") == "echo"]
        self.assertEqual(msgs, ["round one", "round two"])


class TestFraming(unittest.TestCase):
    def test_line_separators_inside_strings_survive(self):
        """U+2028/U+2029 and CR are valid inside JSON strings, not delimiters."""
        payload = "a b c\rd"
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            f"emit({{'type': 'message_update', 'assistantMessageEvent': "
            f"     {{'type': 'text_delta', 'delta': {payload!r}}}}})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        deltas = []
        with session(fake) as s:
            s.prompt("hi", on_event=lambda e: deltas.append(pi_rpc.text_delta(e)),
                     timeout=30)
        self.assertEqual([d for d in deltas if d], [payload])

    def test_non_protocol_stdout_noise_is_ignored(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "sys.stdout.write('warning: something\\n'); sys.stdout.flush()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        with session(fake) as s:
            out = s.prompt("hi", timeout=30)
        self.assertTrue(out["settled"])


class TestFailureModes(unittest.TestCase):
    def test_rejected_prompt_raises(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': False, "
            "      'error': 'agent is streaming'})\n"
        )
        with session(fake) as s:
            with self.assertRaises(pi_rpc.PiError) as cm:
                s.prompt("hi", timeout=30)
        self.assertIn("agent is streaming", str(cm.exception))

    def test_exit_before_settle_raises(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'agent_start'})\n"
            "sys.exit(3)\n"
        )
        with session(fake) as s:
            with self.assertRaises(pi_rpc.PiError) as cm:
                s.prompt("hi", timeout=30)
        self.assertIn("exited before the run settled", str(cm.exception))

    def test_timeout_raises(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "import time; time.sleep(30)\n"
        )
        with session(fake) as s:
            with self.assertRaises(pi_rpc.PiError) as cm:
                s.prompt("hi", timeout=2)
        self.assertIn("did not settle", str(cm.exception))

    def test_extension_error_is_collected_not_fatal(self):
        fake = make_fake_pi(
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'extension_error', 'error': 'pdf_search blew up'})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        with session(fake) as s:
            out = s.prompt("hi", timeout=30)
        self.assertTrue(out["settled"])
        self.assertEqual(out["errors"], ["pdf_search blew up"])


class TestEventHelpers(unittest.TestCase):
    def test_text_delta_only_matches_text_deltas(self):
        self.assertIsNone(pi_rpc.text_delta({"type": "agent_settled"}))
        self.assertIsNone(pi_rpc.text_delta(
            {"type": "message_update",
             "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"}}))
        self.assertEqual(pi_rpc.text_delta(
            {"type": "message_update",
             "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}), "x")


class TestIsolation(unittest.TestCase):
    def test_config_dir_is_created_and_exported(self):
        fake = make_fake_pi(
            "import os, json, sys\n"
            "cmd = read()\n"
            "emit({'type': 'response', 'id': cmd['id'], 'success': True})\n"
            "emit({'type': 'env', 'dir': os.environ.get('PI_CODING_AGENT_DIR', '')})\n"
            "emit({'type': 'agent_settled'})\n"
        )
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "pi-local")
        seen = []
        s = pi_rpc.PiSession(
            cwd=tmp, config_dir=cfg, extensions=[], tools=["t"],
            system_prompt="p", pi_bin=fake,
        )
        try:
            s.prompt("hi", on_event=lambda e: seen.append(e), timeout=30)
        finally:
            s.close()
        reported = [e["dir"] for e in seen if e.get("type") == "env"]
        self.assertEqual(reported, [cfg])
        self.assertTrue(Path(cfg).is_dir())


if __name__ == "__main__":
    unittest.main()
