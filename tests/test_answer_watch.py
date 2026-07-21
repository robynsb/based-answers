"""The draft answer is pushed to the browser as the searcher writes it, not
only after the agent exits. AnswerWatcher owns that: one per round, baselined
on the previous round's leftover file so a round only shows its own output."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from .support import load_script

based_answers = load_script("based-answers.py")


class WatcherCase(unittest.TestCase):
    """Runs in a temp CWD (the scripts are working-directory-relative) with a
    stub emit that records what would have been pushed to the browser."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        Path("answers").mkdir()
        self.emitted = []
        self.fail_next = 0

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def emit(self, run_id, yaml_path):
        """Stands in for emit_answer: returns the rendered html, or None when
        the file could not be rendered (a YAML caught half-written)."""
        if self.fail_next:
            self.fail_next -= 1
            return None
        text = Path(yaml_path).read_text()
        self.emitted.append(text)
        return f"<div>{text}</div>"

    def watcher(self):
        return based_answers.AnswerWatcher("slug", "slug", emit_fn=self.emit)

    def write(self, text):
        Path("answers/slug.yml").write_text(text)


class TestAnswerWatcher(WatcherCase):
    def test_preexisting_file_is_not_emitted(self):
        self.write("question: q\nanswers: []\n")
        w = self.watcher()
        self.assertFalse(w.poll())
        self.assertEqual(self.emitted, [])
        self.assertFalse(w.emitted)

    def test_missing_file_is_not_emitted(self):
        w = self.watcher()
        self.assertFalse(w.poll())
        self.assertEqual(self.emitted, [])

    def test_each_rewrite_emits_once(self):
        w = self.watcher()
        self.write("first")
        self.assertTrue(w.poll())
        self.assertFalse(w.poll())
        self.write("second")
        self.assertTrue(w.poll())
        self.assertEqual(self.emitted, ["first", "second"])
        self.assertTrue(w.emitted)

    def test_identical_rewrite_does_not_emit(self):
        self.write("same")
        w = self.watcher()
        self.write("changed")
        self.assertTrue(w.poll())
        self.write("changed")  # agent rewrote byte-identical content
        self.assertFalse(w.poll())
        self.assertEqual(self.emitted, ["changed"])

    def test_unrenderable_write_is_retried(self):
        w = self.watcher()
        self.write("half-writ")
        self.fail_next = 1
        self.assertFalse(w.poll())
        self.assertFalse(w.emitted)
        # same content, now renderable — the failed write is not swallowed
        self.assertTrue(w.poll())
        self.assertEqual(self.emitted, ["half-writ"])

    def test_raising_emit_is_retried(self):
        def boom(run_id, yaml_path):
            raise RuntimeError("render blew up")

        w = based_answers.AnswerWatcher("slug", "slug", emit_fn=boom)
        self.write("content")
        self.assertFalse(w.poll())
        w.emit_fn = self.emit
        self.assertTrue(w.poll())
        self.assertEqual(self.emitted, ["content"])


class TestWatchUntil(WatcherCase):
    def test_final_poll_catches_the_last_write(self):
        w = self.watcher()
        stop = threading.Event()
        stop.set()  # stop immediately: only the post-loop poll runs
        self.write("written just before the agent exited")
        w.watch_until(stop, interval=0.01)
        self.assertEqual(self.emitted, ["written just before the agent exited"])

    def test_polls_while_running(self):
        w = self.watcher()
        stop = threading.Event()
        t = threading.Thread(target=w.watch_until, args=(stop,), kwargs={"interval": 0.01})
        t.start()
        try:
            self.write("draft")
            for _ in range(200):
                if self.emitted:
                    break
                threading.Event().wait(0.01)
        finally:
            stop.set()
            t.join(timeout=5)
        self.assertEqual(self.emitted, ["draft"])


if __name__ == "__main__":
    unittest.main()
