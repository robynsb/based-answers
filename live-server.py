#!/usr/bin/env python3
"""Flask web app for the citation-grounded QA pipeline ("Based Answers").

Serves the question bar / run history UI, streams pipeline events to the
browser over a WebSocket per run, and persists runs + events in
citation-qa.db (SQLite, WAL mode) in the working directory, so past runs
stay fully viewable across server restarts.

Loaded as a module by based-answers.py (see tests/support.py for the
dash-named import pattern); it has no CLI of its own.
"""

import datetime
import json
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file
from flask_sock import Sock

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Phases that mean a run's event stream is complete
TERMINAL_PHASES = {"passed", "exhausted", "error"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  slug TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  finished_at REAL,
  yaml_path TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event TEXT NOT NULL,
  data TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class PipelineServer:
    """SQLite-backed run/event store + Flask app + WebSocket fan-out.

    `submit(question) -> run_id` is provided by based-answers.py and starts a
    pipeline worker thread; `build_context(yaml_path, pdf_url_for)` is
    format-answers.py's context builder (injected to avoid a module cycle).
    """

    def __init__(self, db_path="citation-qa.db", max_rounds=5,
                 submit=None, build_context=None, skill_dir=None):
        self.db_path = str(db_path)
        self.max_rounds = max_rounds
        self.submit = submit
        self.build_context = build_context
        self.skill_dir = Path(skill_dir) if skill_dir else Path(__file__).parent.resolve()
        self.pdfs: dict[str, str] = {}  # basename -> absolute path
        self.pdf_sources: list[dict] = []  # [{name, pages}] for the sources side panel
        self._local = threading.local()
        self._clients: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self.url = None
        self._init_db()
        self.app = self._create_app()

    # ── database ────────────────────────────────────────────────

    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _init_db(self):
        db = self.db()
        db.executescript(SCHEMA)
        # Runs left "running" by a previous server that died mid-run: flip to
        # error and append a terminal phase event so replays end cleanly
        stale = db.execute("SELECT run_id FROM runs WHERE status='running'").fetchall()
        now = time.time()
        for row in stale:
            db.execute(
                "INSERT INTO events (run_id, event, data, ts) VALUES (?,?,?,?)",
                (row["run_id"], "phase", json.dumps({"phase": "error"}), now),
            )
        db.execute(
            "UPDATE runs SET status='error', finished_at=? WHERE status='running'", (now,)
        )
        db.commit()

    def create_run(self, question: str, slug: str) -> str:
        db = self.db()
        with self._lock:
            run_id = slug
            n = 2
            while db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                run_id = f"{slug}-{n}"
                n += 1
            db.execute(
                "INSERT INTO runs (run_id, question, slug, status, created_at) VALUES (?,?,?,?,?)",
                (run_id, question, slug, "running", time.time()),
            )
            db.commit()
        return run_id

    def set_status(self, run_id: str, status: str, yaml_path: str | None = None):
        db = self.db()
        finished = time.time() if status in TERMINAL_PHASES else None
        db.execute(
            "UPDATE runs SET status=?, finished_at=COALESCE(?, finished_at), "
            "yaml_path=COALESCE(?, yaml_path) WHERE run_id=?",
            (status, finished, yaml_path, run_id),
        )
        db.commit()

    def get_run(self, run_id: str):
        return self.db().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def list_runs(self) -> list:
        return self.db().execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()

    def delete_run(self, run_id: str):
        db = self.db()
        run = self.get_run(run_id)
        if run is None:
            return
        db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        db.commit()
        # Remove the answer YAML too, unless another run still points at it —
        # otherwise it would resurface under "Answers without logs"
        if run["yaml_path"]:
            shared = db.execute(
                "SELECT 1 FROM runs WHERE yaml_path=? LIMIT 1", (run["yaml_path"],)
            ).fetchone()
            if not shared:
                Path(run["yaml_path"]).unlink(missing_ok=True)

    def events_after(self, run_id: str, last_id: int) -> list:
        return self.db().execute(
            "SELECT id, event, data FROM events WHERE run_id=? AND id>? ORDER BY id",
            (run_id, last_id),
        ).fetchall()

    # ── event emission / fan-out ────────────────────────────────

    def emit(self, run_id: str, event: str, data: dict) -> int:
        db = self.db()
        cur = db.execute(
            "INSERT INTO events (run_id, event, data, ts) VALUES (?,?,?,?)",
            (run_id, event, json.dumps(data), time.time()),
        )
        db.commit()
        msg = {"id": cur.lastrowid, "event": event, "data": data}
        with self._lock:
            subscribers = list(self._clients.get(run_id, []))
        for q in subscribers:
            q.put(msg)
        return cur.lastrowid

    def _subscribe(self, run_id: str) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._clients.setdefault(run_id, []).append(q)
        return q

    def _unsubscribe(self, run_id: str, q: queue.Queue):
        with self._lock:
            subs = self._clients.get(run_id, [])
            if q in subs:
                subs.remove(q)
            if not subs:
                self._clients.pop(run_id, None)

    # ── PDFs ────────────────────────────────────────────────────

    def register_pdf(self, path, pages=None):
        p = Path(path).resolve()
        self.pdfs[p.name] = str(p)
        self.pdf_sources = [s for s in self.pdf_sources if s["name"] != p.name]
        self.pdf_sources.append({"name": p.name, "pages": pages})
        self.pdf_sources.sort(key=lambda s: s["name"])

    def pdf_url_for(self, source_name: str, source_path: str) -> str:
        return f"/pdf/{Path(source_path).name}"

    # ── answer fragment ─────────────────────────────────────────

    def render_answer_fragment(self, yaml_path) -> str:
        context = self.build_context(Path(yaml_path), pdf_url_for=self.pdf_url_for)
        with self.app.app_context():
            return render_template("answer-body.html", **context)

    # ── Flask app ───────────────────────────────────────────────

    def _create_app(self) -> Flask:
        app = Flask("based-answers", template_folder=str(self.skill_dir))
        sock = Sock(app)
        server = self

        @app.get("/")
        def index():
            runs = []
            run_yaml_names = set()
            for r in server.list_runs():
                if r["yaml_path"]:
                    run_yaml_names.add(Path(r["yaml_path"]).name)
                runs.append({
                    "run_id": r["run_id"],
                    "question": r["question"],
                    "status": r["status"],
                    "created": datetime.datetime.fromtimestamp(
                        r["created_at"]).strftime("%Y-%m-%d %H:%M"),
                })
            legacy = [
                p.name for p in sorted(Path("answers").glob("*.yml"))
                if p.name not in run_yaml_names
            ] if Path("answers").is_dir() else []
            return render_template("index.html", runs=runs, legacy=legacy,
                                   sources=server.pdf_sources)

        @app.post("/ask")
        def ask():
            question = (request.form.get("question") or "").strip()
            if not question:
                return redirect("/")
            run_id = server.submit(question)
            return redirect(f"/run/{run_id}")

        @app.post("/run/<run_id>/delete")
        def delete_run(run_id):
            run = server.get_run(run_id)
            if run is None:
                abort(404)
            if run["status"] == "running":
                abort(409, description="Cannot delete a run that is still running")
            server.delete_run(run_id)
            return redirect("/")

        @app.get("/run/<run_id>")
        def run_page(run_id):
            run = server.get_run(run_id)
            if run is None:
                abort(404)
            return render_template(
                "run.html",
                run_id=run_id,
                question=run["question"],
                status=run["status"],
                max_rounds=server.max_rounds,
                prerendered=None,
                sources=server.pdf_sources,
            )

        @app.get("/answer/<name>")
        def legacy_answer(name):
            # Answer YAMLs written before the web app existed: no run row, no logs
            path = Path("answers") / name
            if "/" in name or not path.is_file() or path.suffix != ".yml":
                abort(404)
            try:
                fragment = server.render_answer_fragment(path)
            except (ValueError, OSError) as e:
                abort(500, description=str(e))
            context = server.build_context(path, pdf_url_for=server.pdf_url_for)
            return render_template(
                "run.html",
                run_id=None,
                question=context["question"] or name,
                status="legacy",
                max_rounds=server.max_rounds,
                prerendered=fragment,
                sources=server.pdf_sources,
            )

        @app.post("/answer/<name>/delete")
        def delete_legacy_answer(name):
            path = Path("answers") / name
            if "/" in name or not path.is_file() or path.suffix != ".yml":
                abort(404)
            # Only legacy files are deletable here; a run's YAML belongs to
            # its run and goes through /run/<run_id>/delete
            rows = server.db().execute(
                "SELECT yaml_path FROM runs WHERE yaml_path IS NOT NULL"
            ).fetchall()
            if any(Path(r["yaml_path"]).name == name for r in rows):
                abort(409, description="This answer belongs to a run")
            path.unlink(missing_ok=True)
            return redirect("/")

        @app.get("/pdf/<name>")
        def pdf(name):
            path = server.pdfs.get(name)
            if path is None:
                abort(404)
            return send_file(path, mimetype="application/pdf", conditional=True)

        @sock.route("/ws/<run_id>")
        def ws(sock_conn, run_id):
            if server.get_run(run_id) is None:
                sock_conn.close()
                return
            try:
                hello = json.loads(sock_conn.receive(timeout=10) or "{}")
            except (json.JSONDecodeError, TypeError):
                hello = {}
            last_id = int(hello.get("last_id", 0))

            q = server._subscribe(run_id)
            try:
                saw_terminal = False

                def send(msg):
                    nonlocal last_id, saw_terminal
                    if msg["id"] <= last_id:
                        return
                    last_id = msg["id"]
                    if msg["event"] == "phase" and msg["data"].get("phase") in TERMINAL_PHASES:
                        saw_terminal = True
                    sock_conn.send(json.dumps(msg))

                for row in server.events_after(run_id, last_id):
                    send({"id": row["id"], "event": row["event"], "data": json.loads(row["data"])})

                while not saw_terminal:
                    try:
                        send(q.get(timeout=20))
                    except queue.Empty:
                        if server.get_run(run_id)["status"] not in ("running",):
                            # finished while we waited; anything left is in the DB
                            for row in server.events_after(run_id, last_id):
                                send({"id": row["id"], "event": row["event"],
                                      "data": json.loads(row["data"])})
                            break
                        sock_conn.send(json.dumps({"event": "ping"}))
            finally:
                server._unsubscribe(run_id, q)

        return app

    # ── serving ─────────────────────────────────────────────────

    def start(self, port: int = 0) -> str:
        from werkzeug.serving import make_server
        self._httpd = make_server("127.0.0.1", port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        return self.url

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
