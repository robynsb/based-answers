"""PipelineServer: routes, SQLite persistence, WebSocket replay + live stream."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import simple_websocket

from .support import load_script

live_server = load_script("live-server.py")


def make_server(tmpdir: Path, submit=None, **kwargs):
    return live_server.PipelineServer(
        db_path=str(tmpdir / "citation-qa.db"),
        max_rounds=5,
        submit=submit,
        **kwargs,
    )


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestRoutes(ServerTestCase):
    def test_index_shows_question_bar_and_runs(self):
        server = make_server(self.tmpdir)
        server.create_run("What is the frequency of RP2040?", "what-is-the-frequency")
        client = server.app.test_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Based", body)
        self.assertIn("Answers", body)
        self.assertIn('action="/ask"', body)
        self.assertIn("What is the frequency of RP2040?", body)
        self.assertIn("running", body)

    def test_index_lists_legacy_answers(self):
        server = make_server(self.tmpdir)
        answers = self.tmpdir / "answers"
        answers.mkdir()
        (answers / "old-question.yml").write_text("question: q\nconcatenation: ''\nanswers: []\n")
        body = server.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("old-question.yml", body)

    def test_ask_submits_and_redirects(self):
        submitted = []

        def submit(question):
            submitted.append(question)
            return "some-run-id"

        server = make_server(self.tmpdir, submit=submit)
        resp = server.app.test_client().post("/ask", data={"question": "Why is the sky blue?"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/run/some-run-id"))
        self.assertEqual(submitted, ["Why is the sky blue?"])

    def test_ask_rejects_empty_question(self):
        server = make_server(self.tmpdir, submit=lambda q: self.fail("must not submit"))
        resp = server.app.test_client().post("/ask", data={"question": "   "})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_run_page_renders_max_rounds(self):
        server = make_server(self.tmpdir)
        run_id = server.create_run("A question?", "a-question")
        body = server.app.test_client().get(f"/run/{run_id}").get_data(as_text=True)
        self.assertIn("A question?", body)
        self.assertIn("Round –/5", body)
        self.assertEqual(server.app.test_client().get("/run/nope").status_code, 404)

    def test_pdf_whitelist(self):
        server = make_server(self.tmpdir)
        pdf = self.tmpdir / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        server.register_pdf(pdf)
        client = server.app.test_client()
        resp = client.get("/pdf/doc.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"%PDF-1.4 fake")
        self.assertEqual(client.get("/pdf/other.pdf").status_code, 404)


class TestPersistence(ServerTestCase):
    def test_create_run_derives_unique_ids(self):
        server = make_server(self.tmpdir)
        self.assertEqual(server.create_run("q", "slug"), "slug")
        self.assertEqual(server.create_run("q", "slug"), "slug-2")
        self.assertEqual(server.create_run("q", "slug"), "slug-3")

    def test_emit_ids_are_monotonic(self):
        server = make_server(self.tmpdir)
        run_id = server.create_run("q", "slug")
        ids = [server.emit(run_id, "agent-line", {"agent": "searcher", "line": str(i)})
               for i in range(5)]
        self.assertEqual(ids, sorted(ids))
        rows = server.events_after(run_id, ids[1])
        self.assertEqual(len(rows), 3)
        self.assertEqual(json.loads(rows[0]["data"])["line"], "2")

    def test_restart_survives_and_flips_stale_running(self):
        server = make_server(self.tmpdir)
        run_id = server.create_run("q", "slug")
        server.emit(run_id, "phase", {"phase": "searching", "round": 1})
        server.emit(run_id, "agent-line", {"agent": "searcher", "line": "hello"})

        reopened = make_server(self.tmpdir)
        runs = reopened.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], run_id)
        self.assertEqual(runs[0]["status"], "error")
        events = reopened.events_after(run_id, 0)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[:2], ["phase", "agent-line"])
        # terminal phase appended so replays of the crashed run end cleanly
        self.assertEqual(kinds[-1], "phase")
        self.assertEqual(json.loads(events[-1]["data"])["phase"], "error")

    def test_finished_status_survives_restart(self):
        server = make_server(self.tmpdir)
        run_id = server.create_run("q", "slug")
        server.emit(run_id, "phase", {"phase": "passed", "round": 1})
        server.set_status(run_id, "passed", "answers/slug.yml")

        reopened = make_server(self.tmpdir)
        run = reopened.get_run(run_id)
        self.assertEqual(run["status"], "passed")
        self.assertEqual(run["yaml_path"], "answers/slug.yml")


class TestWebSocket(ServerTestCase):
    def _connect(self, server, run_id, last_id=0):
        ws = simple_websocket.Client(f"{server.url.replace('http', 'ws')}/ws/{run_id}")
        ws.send(json.dumps({"last_id": last_id}))
        return ws

    def _recv(self, ws, timeout=10):
        raw = ws.receive(timeout=timeout)
        self.assertIsNotNone(raw, "timed out waiting for a WS message")
        return json.loads(raw)

    def test_replay_then_live_then_terminal_close(self):
        server = make_server(self.tmpdir)
        server.start(0)
        self.addCleanup(server.stop)
        run_id = server.create_run("q", "slug")
        server.emit(run_id, "phase", {"phase": "searching", "round": 1})
        server.emit(run_id, "agent-line", {"agent": "searcher", "line": "one"})

        ws = self._connect(server, run_id)
        first = self._recv(ws)
        self.assertEqual(first["event"], "phase")
        second = self._recv(ws)
        self.assertEqual(second["data"]["line"], "one")

        # live event delivered to the open socket
        threading.Timer(0.1, lambda: server.emit(
            run_id, "agent-line", {"agent": "searcher", "line": "two"})).start()
        third = self._recv(ws)
        self.assertEqual(third["data"]["line"], "two")

        # terminal phase ends the stream: server closes the socket
        server.emit(run_id, "phase", {"phase": "passed", "round": 1})
        final = self._recv(ws)
        self.assertEqual(final["data"]["phase"], "passed")
        with self.assertRaises(simple_websocket.ConnectionClosed):
            while True:
                msg = ws.receive(timeout=10)
                if msg is None:
                    self.fail("socket neither closed nor delivered")

    def test_replay_honors_last_id(self):
        server = make_server(self.tmpdir)
        server.start(0)
        self.addCleanup(server.stop)
        run_id = server.create_run("q", "slug")
        skip_up_to = server.emit(run_id, "agent-line", {"agent": "searcher", "line": "old"})
        server.emit(run_id, "agent-line", {"agent": "searcher", "line": "new"})
        server.emit(run_id, "phase", {"phase": "passed", "round": 1})

        ws = self._connect(server, run_id, last_id=skip_up_to)
        first = self._recv(ws)
        self.assertEqual(first["data"]["line"], "new")

    def test_unknown_run_closes(self):
        server = make_server(self.tmpdir)
        server.start(0)
        self.addCleanup(server.stop)
        ws = simple_websocket.Client(f"{server.url.replace('http', 'ws')}/ws/nope")
        with self.assertRaises(simple_websocket.ConnectionClosed):
            ws.send(json.dumps({"last_id": 0}))
            while ws.receive(timeout=10) is not None:
                pass
            raise simple_websocket.ConnectionClosed(1000)


if __name__ == "__main__":
    unittest.main()
