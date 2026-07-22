#!/usr/bin/env python3
"""Based Answers — web app for citation-grounded QA.

Run from a working directory containing the source PDFs:

  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/based-answers.py [--port N] [--no-open]

Starts a Flask server (see live-server.py), opens the browser, and runs
until Ctrl-C. Questions are submitted through the web UI; each one spawns
its own worker thread running the search/check pipeline:

The search agent runs in a SINGLE persistent `pi --mode rpc` process, held
open for the whole run. Round 1 sends the full context; each prompt returns
only once pi reports the run has settled, at which point all three checkers
(deterministic, semantic, coherence) run. If any fails, the feedback is the
next prompt on that same process, so the agent retains all search results
and conversation history across rounds.

pi is run with no global or project configuration: PI_CODING_AGENT_DIR is
redirected under the CWD, discovery is disabled, and the agent is given
exactly the three tools in tools/ and no built-ins.

All progress events are persisted to citation-qa.db and streamed live to
the browser; past runs stay viewable across server restarts.
"""

import argparse
import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pi_rpc


SKILL_DIR = Path(__file__).parent.resolve()
AGENT_NAME = "citation-searcher"
AGENT_SRC = SKILL_DIR / f"{AGENT_NAME}.md"
CHECKER_NAME = "coherence-checker"
CHECKER_SRC = SKILL_DIR / f"{CHECKER_NAME}.md"

# The agent tools are checked-in files handed to pi by absolute path; nothing
# is installed into, or read from, a global agent config directory.
TOOL_EXTENSIONS = [
    SKILL_DIR / "tools" / "pdf-search.ts",
    SKILL_DIR / "tools" / "verify-citations.ts",
    SKILL_DIR / "tools" / "write-answer.ts",
]
SEARCH_TOOLS = ["pdf_search", "verify_citations", "write_answer"]
# Tools whose result is echoed into the searcher's stream, and how much of it
ECHOED_TOOLS = {"verify_citations"}
TOOL_RESULT_CHARS = 4000

# Everything pi would otherwise keep in ~/.pi lives here, under the CWD.
PI_STATE_DIR = Path(".based-answers")
PI_MODEL = os.environ.get("BA_PI_MODEL", "deepseek/deepseek-v4-flash")

# Credentials: the env var wins, else the macOS Keychain generic password
# stored under this service name (`security add-generic-password -s ... -w`).
API_KEY_ENV = "DEEPSEEK_API_KEY"
KEYCHAIN_SERVICE = os.environ.get("BA_KEYCHAIN_SERVICE", "deepseek")

MAX_ROUNDS = 5
# How often the draft answer file is checked for a rewrite while the agent runs
ANSWER_POLL_SECONDS = 0.7

# Per-agent stream colors so interleaved agent output is tellable apart
SEARCH_COLOR = "\033[36m"     # cyan: citation-searcher
SEMANTIC_COLOR = "\033[35m"   # magenta: semantic checks
COHERENCE_COLOR = "\033[34m"  # blue: coherence check
DIM = "\033[2m"               # dim: any agent's reasoning tokens
RESET = "\033[0m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Set in main(): the PipelineServer, indexed-PDF info, and PDF directory
SERVER = None
PDF_INFO: list[dict] = []
PDF_DIR = "."


def _load_script(filename: str):
    path = SKILL_DIR / filename
    name = filename.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit(run_id: str | None, event: str, data: dict):
    if SERVER is None or not run_id:
        return
    try:
        SERVER.emit(run_id, event, data)
    except Exception as e:  # never let UI plumbing kill a run
        print(f"  [emit error] {e}", file=sys.stderr)


def emit_line(run_id: str | None, agent: str, line: str, extra: dict | None = None):
    data = {"agent": agent, "line": ANSI_RE.sub("", line.rstrip("\n"))}
    if extra:
        data.update(extra)
    emit(run_id, "agent-line", data)


class TokenLedger:
    """Cumulative token and cost totals for one run, split by agent.

    Every assistant message from every pi session in the run lands here, so
    the figures cover the whole question: the searcher's session across all
    rounds, plus a fresh session for each semantic and coherence check.
    Emits the full running total each time, so a browser that reconnects or
    replays only needs the latest `tokens` event.
    """

    def __init__(self, run_id: str | None, emit_fn=None):
        self.run_id = run_id
        self._emit = emit_fn if emit_fn is not None else emit
        self._lock = threading.Lock()
        # Buckets are created on demand, so a new agent needs no roster edit.
        self.by_agent: dict[str, dict] = {}

    @staticmethod
    def _zero() -> dict:
        d = {k: 0 for k in pi_rpc.USAGE_FIELDS}
        d["cost"] = 0.0
        d["calls"] = 0
        return d

    def add(self, agent: str, usage: dict):
        with self._lock:
            bucket = self.by_agent.setdefault(agent, self._zero())
            for k in pi_rpc.USAGE_FIELDS:
                bucket[k] += usage.get(k, 0)
            bucket["cost"] += usage.get("cost", 0.0)
            bucket["calls"] += 1
            payload = self.snapshot()
        self._emit(self.run_id, "tokens", payload)

    def snapshot(self) -> dict:
        """`total` is derived here rather than tracked, so it cannot drift."""
        def with_total(b):
            out = dict(b)
            out["total"] = sum(b[k] for k in pi_rpc.USAGE_FIELDS)
            return out

        run_total = self._zero()
        for bucket in self.by_agent.values():
            for k in list(pi_rpc.USAGE_FIELDS) + ["cost", "calls"]:
                run_total[k] += bucket[k]
        return {"by_agent": {a: with_total(b) for a, b in self.by_agent.items()},
                "total": with_total(run_total)}

    def observer(self, agent: str):
        """An on_event hook that records usage for `agent`."""
        def observe(ev):
            usage = pi_rpc.message_usage(ev)
            if usage:
                self.add(agent, usage)
        return observe


class LineBuffer:
    """Re-assembles streamed token deltas into whole lines.

    The `agent-line` event is one line per event and the browser renders it
    as such. pi streams text a token at a time, so emitting per delta puts
    every word on its own line — the deltas have to be joined and re-split
    on newlines before they become events.
    """

    def __init__(self, emit_one):
        self._emit_one = emit_one
        self._buf = ""

    def feed(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit_one(line)

    def flush(self):
        """Emit any trailing text not terminated by a newline."""
        if self._buf:
            self._emit_one(self._buf)
            self._buf = ""


def emit_answer(run_id: str | None, yaml_path: Path) -> str | None:
    """Render the answer YAML and push it to the browser. Returns the emitted
    HTML, or None if nothing was emitted (no server, or the file did not
    render — e.g. a half-written YAML caught mid-write)."""
    if SERVER is None or not run_id:
        return None
    try:
        html = SERVER.render_answer_fragment(yaml_path)
    except Exception as e:
        print(f"  [answer render error] {e}", file=sys.stderr)
        return None
    emit(run_id, "answer", {"html": html})
    return html


def run_tag(run_id: str | None) -> str:
    return f"[{run_id[-16:]}] " if run_id else ""


def derive_slug(question: str) -> str:
    slug = question.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = slug.replace(" ", "-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


@functools.cache
def api_key() -> str | None:
    """The DeepSeek key, from the environment or the macOS Keychain.

    pi's own auth.json is not reachable here: PI_CODING_AGENT_DIR is
    redirected under the CWD so a run cannot touch global pi state, which
    also means a global `pi /login` does not apply. The key is therefore
    passed in through the environment, and read from the Keychain so it
    need not live in a dotfile or a shell profile.
    """
    key = os.environ.get(API_KEY_ENV)
    if key:
        return key.strip()
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def pi_env(slug: str, question: str = "") -> dict:
    """Environment for a pi subprocess.

    ANSWER_SLUG tells the write_answer tool which file to overwrite, so the
    searcher agent never sees or handles a slug; ANSWER_QUESTION goes into
    the answer file the same way, for the same reason — the pipeline knows
    the question, so having the agent retype it is one more thing it can get
    wrong. BA_PYTHON is this process's own interpreter — already resolved,
    already carrying pymupdf/pyyaml — so the tools invoke the scripts
    directly whatever provided that interpreter.
    """
    env = {
        "ANSWER_SLUG": slug,
        "ANSWER_QUESTION": question,
        "BA_PYTHON": sys.executable,
        "BA_SKILL_DIR": str(SKILL_DIR),
    }
    key = api_key()
    if key:
        env[API_KEY_ENV] = key
    return env


def open_session(prompt_src: Path, *, slug: str = "", question: str = "",
                 tools=(), extensions=(),
                 session_dir: Path | None = None) -> pi_rpc.PiSession:
    """The only way this app builds a pi session.

    Every caller gets the run-local config dir, the configured model, and —
    the point of routing everything through here — credentials. The key is
    read from the Keychain, so it is absent from os.environ and a session
    built with a hand-written env would run unauthenticated; that failed
    silently once, because the searcher kept working while every check
    errored out. There is now no way to construct one without it.
    """
    return pi_rpc.PiSession(
        config_dir=PI_STATE_DIR / "pi",
        session_dir=session_dir,
        extensions=list(extensions),
        tools=list(tools),
        system_prompt=prompt_src.read_text(),
        model=PI_MODEL,
        env=pi_env(slug, question),
    )


def open_search_session(slug: str, question: str = "") -> pi_rpc.PiSession:
    """One pi process per run, holding the whole multi-round conversation."""
    return open_session(AGENT_SRC, slug=slug, question=question,
                        tools=SEARCH_TOOLS, extensions=TOOL_EXTENSIONS,
                        session_dir=PI_STATE_DIR / "sessions" / slug)


def stream_prompt(session: pi_rpc.PiSession, message: str, *, agent: str,
                  color: str, ledger: "TokenLedger", run_id: str | None = None,
                  extra: dict | None = None, on_extra_event=None,
                  timeout: float = 900.0) -> str:
    """Prompt a session, mirroring its output to stdout and the browser.

    Every agent streams the same way — token deltas to stdout in the agent's
    colour, re-assembled into whole `agent-line` events, with usage recorded
    against that agent — so it is written once here rather than per caller.

    Reasoning tokens stream alongside as `kind: thinking` lines, so the
    browser can show what an agent was working through and dim it apart from
    the reply. They are deliberately kept out of the returned text: the
    checkers match PASS/FAIL against that, and a checker weighing a failure
    aloud before answering PASS must not read as a failure.
    """
    chunks = []
    think_extra = {**(extra or {}), "kind": "thinking"}
    buf = LineBuffer(lambda line: emit_line(run_id, agent, line, extra))
    think = LineBuffer(lambda line: emit_line(run_id, agent, line, think_extra))
    record_usage = ledger.observer(agent)

    def on_event(ev):
        record_usage(ev)
        delta = pi_rpc.text_delta(ev)
        if delta:
            # Flush the other buffer first: its trailing partial line belongs
            # before this one, not merged into whatever arrives next.
            think.flush()
            print(f"{color}{delta}{RESET if color else ''}", end="", flush=True)
            chunks.append(delta)
            buf.feed(delta)
            return
        thought = pi_rpc.thinking_delta(ev)
        if thought:
            buf.flush()
            print(f"{DIM}{thought}{RESET}", end="", flush=True)
            think.feed(thought)
            return
        if on_extra_event is not None:
            on_extra_event(ev, buf)

    try:
        session.prompt(message, on_event=on_event, timeout=timeout)
    finally:
        think.flush()
        buf.flush()
    return "".join(chunks)


def run_search_round(session: pi_rpc.PiSession, message: str, label: str,
                     ledger: "TokenLedger", run_id: str | None = None,
                     watcher=None, timeout: int = 900) -> str:
    """Send one round's message and block until the agent has fully settled."""
    print(f"\n{'─' * 60}", flush=True)
    print(f"{run_tag(run_id)}{label} GIVEN TO {AGENT_NAME}:", flush=True)
    print(f"{'─' * 60}", flush=True)
    print(message, flush=True)
    print(f"{'─' * 60}\n", flush=True)
    emit_line(run_id, "searcher",
              f"── {label} GIVEN TO {AGENT_NAME} ──\n{message}\n{'─' * 40}\n")

    # The draft answer streams to the browser as the agent writes and rewrites it
    stop_watch = threading.Event()
    watch_thread = None
    if watcher is not None:
        watch_thread = threading.Thread(target=watcher.watch_until, args=(stop_watch,),
                                        daemon=True, name="watch-answer")
        watch_thread.start()

    def tool_note(ev, buf):
        if ev.get("type") == "tool_execution_start":
            note = f"[tool] {ev.get('toolName')} {json.dumps(ev.get('args') or {})[:300]}"
        else:
            done = pi_rpc.tool_result_text(ev)
            # Only the verifier's result is echoed. It is the one the reader
            # needs — which citations failed and why is the whole reason a
            # round repeats — and it is a compact table. A pdf_search result
            # is up to 100 enumerated matches with snippets, which would bury
            # the agent's own reasoning in the stream it is meant to explain.
            if done is None or done[0] not in ECHOED_TOOLS or not done[1]:
                return
            name, text, is_error = done
            if len(text) > TOOL_RESULT_CHARS:
                text = (text[:TOOL_RESULT_CHARS]
                        + f"\n… [{len(text) - TOOL_RESULT_CHARS} more characters]")
            head = f"── {name} {'FAILED' if is_error else 'result'} ──"
            note = f"{head}\n{text}\n{'─' * 40}"
        print(f"{SEARCH_COLOR}{run_tag(run_id)}{note}\n{RESET}", end="", flush=True)
        buf.flush()  # don't let a half-finished sentence merge into the note
        emit_line(run_id, "searcher", note)

    try:
        return stream_prompt(session, message, agent="searcher", color=SEARCH_COLOR,
                             ledger=ledger, run_id=run_id, on_extra_event=tool_note,
                             timeout=timeout)
    finally:
        stop_watch.set()
        if watch_thread is not None:
            watch_thread.join(timeout=10)


def run_deterministic(yaml_path: Path, pdf_dir: str = ".") -> dict:
    cmd = [
        sys.executable, str(SKILL_DIR / "verify-citations.py"),
        "--pdf-dir", pdf_dir,
        "-v", str(yaml_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"passed": False,
                "output": "Verification timed out after 120s — check for a slow or "
                          "pathological regex in a search_result citation"}
    output = (result.stdout or "") + (result.stderr or "")
    return {"passed": result.returncode == 0, "output": output}


def run_checker(prompt_text: str, ledger: "TokenLedger", color: str = "",
                timeout: int = 120, agent: str = "coherence",
                run_id: str | None = None, extra: dict | None = None) -> str:
    """Judge one rubric with a fresh, tool-less pi session.

    Each check is independent, so it gets its own session with no tools at
    all — the checker judges the text it is given and cannot go consult the
    sources itself.
    """
    emit_line(run_id, agent,
              f"── PROMPT GIVEN TO {CHECKER_NAME} ──\n{prompt_text}\n{'─' * 40}\n", extra)

    session = open_session(CHECKER_SRC)
    try:
        return stream_prompt(session, prompt_text, agent=agent, color=color,
                             ledger=ledger, run_id=run_id, extra=extra,
                             timeout=timeout)
    except pi_rpc.PiError as e:
        # A checker that cannot run must not silently read as a pass: an empty
        # result contains no "FAIL" and would be taken for one.
        msg = f"\n[checker error: {e}]\n"
        emit_line(run_id, agent, msg, extra)
        return msg
    finally:
        session.close()
    return "".join(chunks)


def claim_key(answer: dict) -> str:
    """Identity of one claim for latching: its text plus its citations.

    Everything the semantic checker is shown about a claim comes from these
    two fields, so two answers with the same key pose the checker exactly the
    same question. Serialized with sorted keys so a YAML rewrite that only
    reorders mapping keys doesn't read as a change; `default=str` keeps an
    odd scalar (a YAML date, say) from raising here.
    """
    return json.dumps([answer.get("claim", ""), answer.get("citations", [])],
                      sort_keys=True, default=str)


def run_semantic_checkers(yaml_path: Path, ledger: "TokenLedger",
                          run_id: str | None = None,
                          passed_claims: set[str] | None = None) -> list[dict]:
    """Check claims in order; stops at the first failing claim so the round
    goes straight back to the search agent (later claims stay unchecked —
    they may shift anyway once the failing one is fixed).

    `passed_claims` latches claims that already passed in an earlier round of
    this run. The checker is an LLM and is not deterministic at the margin: a
    claim that passed in round 3 can fail in round 5 and end an
    otherwise-succeeding run purely on a re-roll. Re-judging an unchanged
    claim buys nothing — a second verdict on identical input is a new sample,
    not new evidence. Passing the set in from the loop is what makes it
    survive across rounds.

    The latch key is `claim_key()`: the claim text *and* its citations. The
    citations are the evidence the verdict was actually about, so a claim
    whose quotes, queries or enumerations changed is a different question and
    goes back through the checker even when its wording is identical."""
    try:
        import yaml as pyyaml
        with open(yaml_path) as f:
            data = pyyaml.safe_load(f)
        answers = data.get("answers", []) if data else []
    except Exception:
        answers = []

    failures = []
    if not answers:
        return failures

    for i, a in enumerate(answers):
        claim = a.get("claim", "")
        if not claim:
            continue
        key = claim_key(a)
        if passed_claims is not None and key in passed_claims:
            print(f"  {run_tag(run_id)}[PASS] Claim {i+1} (latched from an earlier round): {claim[:80]}")
            emit_line(run_id, "semantic",
                      "── SKIPPED ──\nThis claim and its citations are unchanged since a "
                      "round in which it passed, so its earlier PASS is carried forward.\n"
                      f"{'─' * 40}\n", {"claim": i})
            emit(run_id, "claim-status", {"index": i, "claim": claim, "status": "pass"})
            continue
        emit(run_id, "claim-status", {"index": i, "claim": claim, "status": "checking"})
        citations = a.get("citations", [])
        texts = [c.get("text", "") for c in citations if c.get("type") != "search_result"]
        search_results = [c for c in citations
                          if c.get("type") == "search_result" and c.get("mode") != "regex"]
        enumerations = [c for c in citations
                        if c.get("type") == "search_result" and c.get("mode") == "regex"]
        previous_claims = [p.get("claim", "") for p in answers[:i] if p.get("claim", "")]
        rubric = f"""You are a claim verifier.

CLAIM: {claim}

SOURCE_TEXTS:
"""
        for t in texts:
            rubric += f"  - \"{t}\"\n"

        # Only claims that actually carry a search_result citation get these
        # blocks and their judgment instructions below — claims backed purely
        # by normal citations must see exactly today's rubric, unchanged.
        if search_results:
            rubric += "\nSEARCH_RESULTS (used to demonstrate absence/exhaustiveness):\n"
            for sr in search_results:
                query = sr.get("query", "")
                source = sr.get("source", "")
                sr_results = sr.get("results") or []
                if not sr_results:
                    rubric += f'  - QUERY: "{query}" on {source} → NO RESULTS FOUND\n'
                else:
                    rubric += f'  - QUERY: "{query}" on {source} → {len(sr_results)} result(s):\n'
                    for r in sr_results:
                        rubric += f'      - page {r.get("page")}: "{r.get("text", "")}"\n'

        if enumerations:
            rubric += "\nENUMERATION (every distinct match for a pattern — used to rule out a whole family of possible names):\n"
            for en in enumerations:
                pattern = en.get("query", "")
                source = en.get("source", "")
                en_results = en.get("results") or []
                rubric += f'  - PATTERN: /{pattern}/ on {source} → {len(en_results)} distinct match(es):\n'
                for r in en_results:
                    rubric += f'      - "{r.get("match", "")}" (page {r.get("page")}) — snippet: "{r.get("text", "")}"\n'

        rubric += "\nPREVIOUS_CLAIMS (assume these are true; they may be used as premises):\n"
        if previous_claims:
            for p in previous_claims:
                rubric += f"  - \"{p}\"\n"
        else:
            rubric += "  (none)\n"
        rubric += """

OUT-OF-CONTEXT CHECK: Every source must include text before, text related to the claim, and text after. If bare snippet → FAIL.
If the source text does not itself name what the statement is about — i.e. the subject must be inferred (e.g. it only says "It was completed a year later" or "This method is unreliable" without naming the thing) — that source is out-of-context. If the claim needs or uses that inferred subject, respond FAIL.
PREVIOUS_CLAIMS may only be combined with what the sources state; never use them to resolve pronouns or fill in the subject of a source text.
"""
        if search_results:
            rubric += """
SEARCH_RESULTS CHECK: For each SEARCH_RESULTS entry, judge whether QUERY is a sufficiently thorough probe for what CLAIM asserts is absent/exhaustive. Consider obvious alternate phrasings, synonyms, abbreviations, or related terms a thorough search would also have tried (e.g. an acronym and its expansion, singular/plural, a formal term and its shortened form). If an obvious rephrasing or keyword is missing from QUERY that could plausibly surface different results, respond "FAIL: query not comprehensive — also try '<term>'". If CLAIM asserts absence across a whole family of possible names (not one specific exact term), a small number of literal exact-string queries can never be judged thorough without inventing what else might exist — respond "FAIL: use an ENUMERATION (regex) search instead of guessing individual names" — unless the family is small enough that every plausible member was actually tried literally. Only if every SEARCH_RESULTS query is adequately thorough should its (non-)results be treated as supporting CLAIM.
"""
        if enumerations:
            rubric += """
ENUMERATION CHECK: Each ENUMERATION entry is a COMPLETE, independently verified list — every distinct string that pattern matches anywhere in the source is listed above, nothing held back. Judge whether the enumerated list, together with SOURCE_TEXTS, supports CLAIM. Also judge whether the PATTERN itself covers the right family for CLAIM (e.g. is this the only relevant prefix) — if an obviously relevant related prefix/pattern was not also enumerated, respond "FAIL: also enumerate '<pattern>'".
"""
        parts = ["SOURCE_TEXTS"]
        if search_results:
            parts.append("SEARCH_RESULTS")
        if enumerations:
            parts.append("ENUMERATION")
        if len(parts) == 1:
            synthesis_sources = parts[0]
        elif len(parts) == 2:
            synthesis_sources = f"{parts[0]} and {parts[1]}"
        else:
            synthesis_sources = ", ".join(parts[:-1]) + f", and {parts[-1]}"
        rubric += f"""
Does the SYNTHESIS of all {synthesis_sources} together with PREVIOUS_CLAIMS strictly imply CLAIM?
- YES: Respond "PASS"
- FAIL (out-of-context): Respond "FAIL: out-of-context — <why>"
- NO: Respond "FAIL: <what cannot be inferred>"

Rules: direct logical inference OK. Cross-source inference OK. PREVIOUS_CLAIMS may be treated as established facts and combined with SOURCE_TEXTS. External domain knowledge = FAIL. Never use your own knowledge.
"""
        result = run_checker(rubric, ledger, color=SEMANTIC_COLOR, agent="semantic",
                             run_id=run_id,
                             extra={"claim": i})
        passed = "PASS" in result.upper() and "FAIL" not in result.upper()
        print(f"  {run_tag(run_id)}{'[PASS]' if passed else '[FAIL]'} Claim {i+1}: {claim[:80]}")
        emit(run_id, "claim-status",
             {"index": i, "claim": claim, "status": "pass" if passed else "fail"})
        if not passed:
            failures.append({"claim": claim, "output": result[:2000]})
            return failures
        if passed_claims is not None:
            passed_claims.add(key)

    return failures


def run_coherence_checker(yaml_path: Path, question: str, ledger: "TokenLedger",
                          run_id: str | None = None) -> dict:
    try:
        import yaml as pyyaml
        with open(yaml_path) as f:
            data = pyyaml.safe_load(f)
        claims = [a.get("claim", "") for a in (data or {}).get("answers", [])
                  if a.get("claim", "")]
    except Exception:
        claims = []

    if not claims:
        return {"passed": True, "output": "No answer to check"}

    answer_text = ". ".join(claims)
    rubric = f"""You are a coherence and completeness verifier.

QUESTION: {question}

ANSWER: {answer_text}

Evaluate:
1. COHERENCE: sensible paragraph with established concepts?
2. COMPLETENESS: totally answers the question?

Respond with:
- PASS
- FAIL: <what is missing, unclear, or doesn't make sense>
"""
    result = run_checker(rubric, ledger, color=COHERENCE_COLOR, agent="coherence",
                         run_id=run_id)
    passed = "PASS" in result.upper() and "FAIL" not in result.upper()
    print(f"  {run_tag(run_id)}{'[PASS]' if passed else '[FAIL]'} Coherence check")
    return {"passed": passed, "output": result[:2000]}


def group_feedback_by_round(rounds: list[dict]) -> list[tuple[int, list[str]]]:
    """Group failure entries by the round they occurred in — a round can fail
    several checks (e.g. one semantic failure per claim), and each is its own
    entry in `rounds`."""
    grouped: list[tuple[int, list[str]]] = []
    for r in rounds:
        if grouped and grouped[-1][0] == r["round"]:
            grouped[-1][1].append(r["feedback"])
        else:
            grouped.append((r["round"], [r["feedback"]]))
    return grouped


def build_feedback_message(round_num: int, rounds: list[dict]) -> str:
    """The follow-up message for the next round.

    Only the failures from the round just finished: this goes to the SAME
    pi session, which already holds every earlier round's feedback verbatim.
    """
    grouped = group_feedback_by_round(rounds)
    if not grouped:
        return (f"Round {round_num}/{MAX_ROUNDS} — checks failed. "
                "Fix the issues and run verify_citations again.")
    rnd, feedbacks = grouped[-1]
    plural = "s" if len(feedbacks) != 1 else ""
    msg_lines = [f"Round {round_num}/{MAX_ROUNDS} — round {rnd} failed "
                 f"({len(feedbacks)} failure{plural}). Fix the issues below and "
                 "run verify_citations again."]
    msg_lines.extend(feedbacks)
    return "\n".join(msg_lines)


# Dropped when scoring question overlap: too common to say anything about topic
STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can",
    "do", "does", "for", "from", "has", "have", "how", "i", "if", "in", "is",
    "it", "its", "me", "my", "not", "of", "on", "or", "should", "so", "some",
    "that", "the", "their", "them", "then", "there", "they", "this", "to", "was",
    "way", "we", "what", "when", "which", "will", "with", "would", "you", "your",
}


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in STOPWORDS}


def relevant_answer_files(question: str, slug: str, limit: int = 10) -> list[tuple[str, str]]:
    """Past answer files worth reading for this question, best first.

    Listing every answers/*.yml grows without bound — and re-asks pile up as -N
    duplicates of one question — so rank by content-word overlap and keep the
    top `limit`. Returns [(filename, that file's question), ...]; the agent gets
    one line each instead of being told to read them all.
    """
    try:
        import yaml as pyyaml
    except ImportError:
        return []

    wanted = content_words(question)
    if not wanted:
        return []

    # One entry per distinct question, newest file wins — collapses -N re-asks
    by_question: dict[str, tuple[float, str, str]] = {}
    for path in Path("answers").glob("*.yml"):
        if path.stem == slug:
            continue
        try:
            data = pyyaml.safe_load(path.read_text())
            past = (data or {}).get("question", "")
        except Exception:
            continue
        if not past:
            continue
        key = " ".join(re.findall(r"[a-z0-9_]+", past.lower()))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if key not in by_question or mtime > by_question[key][0]:
            by_question[key] = (mtime, path.name, past)

    scored = [(len(wanted & content_words(past)), name, past)
              for _, name, past in by_question.values()]
    scored = [s for s in scored if s[0]]
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(name, past) for _, name, past in scored[:limit]]


def write_context(slug: str, question: str, pdf_info: list[dict]) -> Path:
    """The round-1 context file. Later rounds send only the newest feedback to
    the same session, so this never carries a feedback history."""
    path = Path("answers") / f"{slug}-context.md"
    lines = [
        "# Question",
        question,
        "",
        "# PDF Sources",
    ]
    # Bare filenames: the tools run in this working directory, so the name is
    # the whole path the agent needs for `pdf`
    for info in pdf_info:
        lines.append(f"- {info['file']} ({info['pages']} pages)")

    related = relevant_answer_files(question, slug)
    if related:
        lines += ["", "# Related Past Answers (read only those clearly relevant to this question)"]
        for name, past_question in related:
            lines.append(f'- {name} — "{past_question}"')

    lines += [
        "",
        "# Prior Attempts & Feedback",
        "None yet. This is your first attempt.",
    ]

    path.write_text("\n".join(lines) + "\n")
    return path


def find_yaml(slug: str) -> Path | None:
    candidates = []
    for p in Path("answers").glob(f"{slug}*.yml"):
        stem = p.stem
        if stem == slug or re.match(rf"^{re.escape(slug)}-\d+$", stem):
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


class AnswerWatcher:
    """Pushes the draft answer to the browser while the searcher is still running.

    The searcher writes answers/<slug>.yml early and then iterates on it within
    the round (calling verify_citations itself), so waiting for the subprocess to
    exit hides the answer for most of the round. One watcher per round: it
    baselines the file already on disk — the previous round's leftover — so a new
    round only shows what that round actually produced.
    """

    def __init__(self, slug: str, run_id: str | None, emit_fn=None):
        self.slug = slug
        self.run_id = run_id
        self.emit_fn = emit_fn or emit_answer
        self.emitted = False
        self.last_text = self._read()

    def _read(self) -> str | None:
        path = find_yaml(self.slug)
        if path is None:
            return None
        try:
            return path.read_text()
        except OSError:
            return None

    def poll(self) -> bool:
        """Emit an `answer` event if the file changed since the last one. A
        rewrite caught half-written fails to render and is retried next poll."""
        path = find_yaml(self.slug)
        if path is None:
            return False
        try:
            text = path.read_text()
        except OSError:
            return False
        if text == self.last_text:
            return False
        try:
            html = self.emit_fn(self.run_id, path)
        except Exception as e:  # never let UI plumbing kill a run
            print(f"  [answer watch error] {e}", file=sys.stderr)
            return False
        if html is None:
            return False
        self.last_text = text
        self.emitted = True
        return True

    def watch_until(self, stop_event: threading.Event, interval: float = ANSWER_POLL_SECONDS):
        while not stop_event.wait(interval):
            self.poll()
        self.poll()  # catch a write that landed just before the agent exited


def install_banner(label: str):
    print(f"\n{'#' * 60}", flush=True)
    print(f"#  {label}", flush=True)
    print(f"{'#' * 60}", flush=True)


def search_loop(slug: str, question: str, pdf_info: list[dict], rounds: list[dict],
                pdf_dir: str, run_id: str | None = None) -> tuple[Path | None, int]:
    """Returns (yaml_path, round_num) on success, (None, MAX_ROUNDS) when exhausted."""
    session = open_search_session(slug, question)
    ledger = TokenLedger(run_id)
    try:
        return _search_rounds(session, slug, question, pdf_info, rounds, pdf_dir,
                              run_id, ledger)
    finally:
        session.close()
        t = ledger.snapshot()["total"]
        print(f"  {run_tag(run_id)}Tokens: {t['total']:,} across {t['calls']} calls "
              f"(${t['cost']:.4f})", flush=True)


def deterministic_advisories(output: str) -> str:
    """The ADVISORY lines from the verifier's output, as a feedback block.

    An advisory is guidance about the *shape* of an argument, so it does not
    fail the deterministic check and the round carries on. But a round that
    later fails semantically sends only the checker's objection back, and the
    advisory — which is usually the reason the objection exists — would never
    reach the agent. Appending it to whatever feedback the round produces is
    what makes it worth emitting at all.
    """
    lines = [l for l in output.splitlines() if l.startswith("ADVISORY: ")]
    return "\n\nAlso note:\n" + "\n".join(lines) if lines else ""


def searcher_produced_nothing(ledger: "TokenLedger") -> bool:
    """True when the search agent completed calls but returned zero tokens.

    A provider that rejects the model (404) or throttles it (429) makes pi
    emit no assistant message at all, so the round looks like an agent that
    simply wrote no YAML — and the loop dutifully retries it MAX_ROUNDS
    times before reporting `exhausted`, i.e. "tried and could not answer".
    Zero tokens across completed calls means the model never ran, which is
    a configuration fault and not something another round can fix.
    """
    bucket = ledger.snapshot()["by_agent"].get("searcher")
    return bool(bucket) and bucket["calls"] > 0 and bucket["total"] == 0


def _search_rounds(session, slug: str, question: str, pdf_info: list[dict],
                   rounds: list[dict], pdf_dir: str,
                   run_id: str | None, ledger: "TokenLedger") -> tuple[Path | None, int]:
    # Verbatim claim texts that have passed the semantic checker in any round
    # of this run; lives for the whole run so a re-offered claim is not
    # re-rolled against a nondeterministic judge (see run_semantic_checkers).
    passed_claims: set[str] = set()
    for round_num in range(1, MAX_ROUNDS + 1):
        label = f"ROUND {round_num}/{MAX_ROUNDS}"
        if rounds:
            label += " (with feedback)"
        install_banner(f"{run_tag(run_id)}{label}")
        emit(run_id, "phase", {"phase": "searching", "round": round_num})

        # One watcher per round, baselined on the previous round's leftover file
        watcher = AnswerWatcher(slug, run_id)

        # Round 1 sends the full context; later rounds send only the round
        # just finished, because the session still holds every earlier one.
        if round_num == 1:
            message = write_context(slug, question, pdf_info).read_text()
            what = "CONTEXT"
        else:
            message = build_feedback_message(round_num, rounds)
            what = "FEEDBACK"

        try:
            run_search_round(session, message, what, ledger, run_id=run_id,
                             watcher=watcher)
        except pi_rpc.PiError as e:
            print(f"  {run_tag(run_id)}[FAIL] search agent: {e}\n")
            feedback = f"The search agent did not complete: {e}"
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            raise

        # Fail fast rather than burn the remaining rounds on a model that is
        # never going to answer. Only round 1 is checked: the ledger is
        # cumulative, so a later round reading zero implies round 1 did too.
        if round_num == 1 and searcher_produced_nothing(ledger):
            calls = ledger.snapshot()["by_agent"]["searcher"]["calls"]
            raise pi_rpc.PiError(
                f"the search agent returned zero tokens across {calls} call(s) — "
                f"the model produced no output at all. Check the model slug "
                f"(BA_PI_MODEL, currently {PI_MODEL!r}) and the API key.")

        yaml_path = find_yaml(slug)
        if not yaml_path:
            feedback = "No YAML file produced. Search PDFs and write answers/<slug>.yml."
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            print(f"  {run_tag(run_id)}[FAIL] No YAML file found\n")
            continue

        # The watcher has normally already shown this round's answer; emit only
        # if it never did (e.g. the agent left the previous round's file as-is),
        # so the round always displays the answer the checkers are about to check
        if not watcher.emitted:
            emit_answer(run_id, yaml_path)

        # ── Deterministic verification ──
        print()
        emit(run_id, "phase", {"phase": "deterministic", "round": round_num})
        det = run_deterministic(yaml_path, pdf_dir)
        emit(run_id, "check-result",
             {"check": "deterministic", "passed": det["passed"], "output": det["output"][:2000]})
        if not det["passed"]:
            print(f"  {run_tag(run_id)}[FAIL] Deterministic verification failed\n")
            feedback = f"Deterministic verification FAILED for {yaml_path.name}:\n" + det["output"]
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            continue

        print(f"  {run_tag(run_id)}[PASS] {yaml_path.name} — all citations verified\n")
        advisories = deterministic_advisories(det["output"])

        # ── Semantic checkers ──
        emit(run_id, "phase", {"phase": "semantic", "round": round_num})
        semantic_failures = run_semantic_checkers(yaml_path, ledger, run_id=run_id,
                                                  passed_claims=passed_claims)
        if semantic_failures:
            sf = semantic_failures[0]
            feedback = (f"Semantic checker FAILED for claim: {sf['claim']}\n"
                        f"(checking stopped at the first failing claim; later claims were not checked)\n"
                        f"Checker output:\n{sf['output']}{advisories}")
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            print(f"  {run_tag(run_id)}Restarting with semantic failure on claim: {sf['claim'][:80]}...\n")
            continue

        # ── Coherence checker ──
        emit(run_id, "phase", {"phase": "coherence", "round": round_num})
        coherence = run_coherence_checker(yaml_path, question, ledger, run_id=run_id)
        emit(run_id, "check-result",
             {"check": "coherence", "passed": coherence["passed"], "output": coherence["output"][:2000]})
        if not coherence["passed"]:
            feedback = f"Coherence checker FAILED:\n{coherence['output']}{advisories}"
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            print(f"  {run_tag(run_id)}Restarting with coherence failure...\n")
            continue

        # All checks passed
        print(f"\n{'=' * 60}")
        print(f"{run_tag(run_id)}ALL CHECKS PASSED - round {round_num}")
        print(f"{'=' * 60}\n")
        return yaml_path, round_num

    return None, MAX_ROUNDS


def run_worker(run_id: str, question: str):
    """Executes one question's full pipeline; one thread per run."""
    # run_id is already the disambiguated slug (create_run bumps -N for
    # re-asks of an identical question), so file operations must key off
    # it rather than re-deriving a base slug that could collide with an
    # earlier, unrelated run of the same question.
    slug = run_id
    try:
        rounds: list[dict] = []
        yaml_path, passed_round = search_loop(slug, question, PDF_INFO, rounds, PDF_DIR, run_id=run_id)

        if yaml_path is not None:
            emit_answer(run_id, yaml_path)
            emit(run_id, "phase", {"phase": "passed", "round": passed_round})
            SERVER.set_status(run_id, "passed", str(yaml_path))
            return

        print(f"\n{run_tag(run_id)}[EXHAUSTED] All {MAX_ROUNDS} rounds failed.")
        yaml_path = find_yaml(slug) or Path("answers") / f"{slug}.yml"
        if not yaml_path.exists():
            yaml_path.write_text(f"question: \"{question}\"\nanswers: []\n")
            print(f"{run_tag(run_id)}Wrote empty answer: {yaml_path}")
        else:
            print(f"{run_tag(run_id)}Using last answer: {yaml_path}")
        emit_answer(run_id, yaml_path)
        emit(run_id, "phase", {"phase": "exhausted", "round": MAX_ROUNDS})
        SERVER.set_status(run_id, "exhausted", str(yaml_path))
    except Exception as e:
        print(f"{run_tag(run_id)}[ERROR] {e}", file=sys.stderr)
        emit(run_id, "agent-line", {"agent": "searcher", "line": f"[pipeline error] {e}"})
        emit(run_id, "phase", {"phase": "error"})
        SERVER.set_status(run_id, "error")


def submit_question(question: str) -> str:
    slug = derive_slug(question)
    run_id = SERVER.create_run(question, slug)
    threading.Thread(target=run_worker, args=(run_id, question), daemon=True,
                     name=f"run-{run_id}").start()
    return run_id


def index_pdfs(pdf_paths: list[Path]) -> list[dict]:
    pdf_info = []
    for pdf in pdf_paths:
        pdf_path = Path(pdf).resolve()
        cache_file = Path("indexed-pdfs") / f"{pdf_path.name}.json"
        if cache_file.exists():
            print(f"  {pdf_path.name} (cached)")
            with open(cache_file) as f:
                data = json.load(f)
            info = {
                "file": pdf_path.name,
                "abspath": str(pdf_path),
                "pages": str(data.get("pages", "?")),
                "chunks": str(len(data.get("chunks", []))),
                "tokens": str(data.get("estimated_tokens", len(json.dumps(data)) // 1000)),
            }
        else:
            print(f"Indexing: {pdf_path.name}")
            cmd = [
                sys.executable, str(SKILL_DIR / "pdf-search.py"),
                str(pdf_path), "info",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            info = {"file": pdf_path.name, "abspath": str(pdf_path), "pages": "?", "chunks": "?", "tokens": "?"}
            for line in (r.stdout or "").split("\n"):
                if "Pages:" in line:
                    info["pages"] = line.split(":", 1)[1].strip()
                elif "Chunks:" in line:
                    info["chunks"] = line.split(":", 1)[1].strip()
                elif "tokens:" in line.lower():
                    info["tokens"] = line.split(":", 1)[1].strip()
        pdf_info.append(info)
        print(f"  {info['file']}: {info['pages']} pages, {info['chunks']} chunks, ~{info['tokens']} tokens")
    return pdf_info


def main():
    global SERVER, PDF_INFO, PDF_DIR

    parser = argparse.ArgumentParser(description="Based Answers — citation-grounded QA web app")
    parser.add_argument("--port", type=int, default=0, help="Port to serve on (default: auto-assign)")
    parser.add_argument("--no-open", action="store_true", help="Don't open the browser")
    args = parser.parse_args()

    os.makedirs("answers", exist_ok=True)

    # Nothing is installed into a global agent directory: the prompts are read
    # from this repo and passed as --system-prompt, and the tools are handed to
    # pi by path. Everything pi persists stays under PI_STATE_DIR in the CWD.
    PI_STATE_DIR.mkdir(parents=True, exist_ok=True)
    missing = [t for t in TOOL_EXTENSIONS if not t.exists()]
    if missing:
        print(f"Error: missing agent tools: {', '.join(str(m) for m in missing)}",
              file=sys.stderr)
        sys.exit(1)
    if api_key():
        src = "env" if os.environ.get(API_KEY_ENV) else f"keychain:{KEYCHAIN_SERVICE}"
        print(f"Credentials: {API_KEY_ENV} from {src}")
    else:
        print(f"Warning: no {API_KEY_ENV} in the environment and no Keychain item "
              f"for service '{KEYCHAIN_SERVICE}' — pi will have no credentials. "
              f"(A global `pi /login` does not apply: PI_CODING_AGENT_DIR is "
              f"redirected, so auth.json is not read.)", file=sys.stderr)
    print(f"Agent: pi --mode rpc, model {PI_MODEL}, state in {PI_STATE_DIR}/")

    # Collect and index PDFs from the working directory
    pdf_paths = sorted(Path(".").glob("*.pdf"))
    if not pdf_paths:
        print("Error: no PDF files found in the working directory", file=sys.stderr)
        sys.exit(1)

    PDF_INFO = index_pdfs(pdf_paths)
    PDF_DIR = str(Path(pdf_paths[0]).resolve().parent)

    live_server = _load_script("live-server.py")
    format_answers = _load_script("format-answers.py")

    SERVER = live_server.PipelineServer(
        db_path="citation-qa.db",
        max_rounds=MAX_ROUNDS,
        submit=submit_question,
        build_context=format_answers.build_context,
        skill_dir=SKILL_DIR,
    )
    for info in PDF_INFO:
        SERVER.register_pdf(info["abspath"], pages=info["pages"])

    url = SERVER.start(args.port)
    install_banner(f"Based Answers serving at {url}")
    if not args.no_open:
        subprocess.run(["open", url])

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down.")
        SERVER.stop()


if __name__ == "__main__":
    main()
