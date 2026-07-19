#!/usr/bin/env python3
"""Based Answers — web app for citation-grounded QA.

Run from a working directory containing the source PDFs:

  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/based-answers.py [--port N] [--no-open]

Starts a Flask server (see live-server.py), opens the browser, and runs
until Ctrl-C. Questions are submitted through the web UI; each one spawns
its own worker thread running the search/check pipeline:

The search agent runs in a SINGLE persistent opencode session (--session).
It runs once with the full context, then all three checkers (deterministic,
semantic, coherence) run. If any fails, feedback is sent as a follow-up
message to the SAME session, so the agent retains all search results and
conversation history across rounds.

All progress events are persisted to citation-qa.db and streamed live to
the browser; past runs stay viewable across server restarts.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


SKILL_DIR = Path(__file__).parent.resolve()
AGENT_NAME = "citation-searcher"
AGENT_SRC = SKILL_DIR / f"{AGENT_NAME}.md"
AGENT_DST = Path.home() / ".config/opencode/agents" / f"{AGENT_NAME}.md"
CHECKER_NAME = "coherence-checker"
CHECKER_SRC = SKILL_DIR / f"{CHECKER_NAME}.md"
CHECKER_DST = Path.home() / ".config/opencode/agents" / f"{CHECKER_NAME}.md"
TOOLS_DIR = Path.home() / ".config/opencode/tools"
MAX_ROUNDS = 5

# Per-agent stream colors so interleaved opencode output is tellable apart
SEARCH_COLOR = "\033[36m"     # cyan: citation-searcher
SEMANTIC_COLOR = "\033[35m"   # magenta: semantic checks
COHERENCE_COLOR = "\033[34m"  # blue: coherence check
RESET = "\033[0m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Set in main(): the PipelineServer, indexed-PDF info, and PDF directory
SERVER = None
PDF_INFO: list[dict] = []
PDF_DIR = "."

# opencode session ids already claimed by a run (parallel runs must not
# steal each other's freshly created sessions)
_claimed_sessions: set[str] = set()
_claim_lock = threading.Lock()


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


def emit_answer(run_id: str | None, yaml_path: Path):
    if SERVER is None or not run_id:
        return
    try:
        html = SERVER.render_answer_fragment(yaml_path)
        emit(run_id, "answer", {"html": html})
    except Exception as e:
        print(f"  [answer render error] {e}", file=sys.stderr)


def run_tag(run_id: str | None) -> str:
    return f"[{run_id[-16:]}] " if run_id else ""


def derive_slug(question: str) -> str:
    slug = question.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = slug.replace(" ", "-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


def get_session_ids() -> set[str]:
    cmd = ["opencode", "session", "list", "--format", "json", "--max-count", "50"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try:
        sessions = json.loads(result.stdout)
        return {s["id"] for s in sessions if isinstance(s, dict)}
    except (json.JSONDecodeError, TypeError):
        return set()


def get_new_session_id(before_ids: set[str], run_id: str) -> str | None:
    """Find the session created by this run's search agent.

    Prefer the unique --title match (citation-qa-<run_id>); the "any new
    session" fallback is only trusted when exactly one unclaimed new id
    exists, so parallel runs don't steal each other's sessions.
    """
    cmd = ["opencode", "session", "list", "--format", "json", "--max-count", "50"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try:
        sessions = json.loads(result.stdout)
        if not isinstance(sessions, list):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    with _claim_lock:
        candidates = [
            s for s in sessions
            if isinstance(s, dict) and s["id"] not in before_ids
            and s["id"] not in _claimed_sessions
        ]
        for s in candidates:
            if f"citation-qa-{run_id}" in s.get("title", ""):
                _claimed_sessions.add(s["id"])
                return s["id"]
        if len(candidates) == 1:
            _claimed_sessions.add(candidates[0]["id"])
            return candidates[0]["id"]
    return None


def run_search_agent(prompt_path: Path | None, question: str, session_id: str | None = None,
                     message: str | None = None, timeout: int = 600,
                     run_id: str | None = None) -> int:
    cmd = ["opencode", "run", "--agent", AGENT_NAME]
    slug = run_id or derive_slug(question)

    if session_id:
        cmd.extend(["--session", session_id])
        if message:
            cmd.append(message)
            emit_line(run_id, "searcher",
                      f"── FEEDBACK SENT TO {AGENT_NAME} ──\n{message}\n{'─' * 40}\n")
    else:
        cmd.extend(["-f", str(prompt_path), "--title", f"citation-qa-{slug}", question])
        print(f"\n{'─' * 60}", flush=True)
        print(f"{run_tag(run_id)}CONTEXT GIVEN TO {AGENT_NAME}:", flush=True)
        print(f"{'─' * 60}", flush=True)
        print(prompt_path.read_text(), flush=True)
        print(f"{'─' * 60}\n", flush=True)
        emit_line(run_id, "searcher",
                  f"── CONTEXT GIVEN TO {AGENT_NAME} ──\n{prompt_path.read_text()}\n{'─' * 40}\n")

    label = f"{AGENT_NAME}{' (continuing session)' if session_id else ''}"
    print(f"  {run_tag(run_id)}Agent: {label}", flush=True)
    print(f"{'-' * 50}", flush=True)

    # The write_answer tool reads this to know which answers/*.yml file to
    # (over)write, so the searcher agent never has to know or pass a slug.
    env = {**os.environ, "ANSWER_SLUG": slug}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)

    denied = 0
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        print(f"{SEARCH_COLOR}{run_tag(run_id)}{line}{RESET}", end="", flush=True)
        emit_line(run_id, "searcher", line)
        if "permission requested" in line.lower() or "auto-rejecting" in line.lower():
            denied += 1
            print(f"  \033[33m[DENIED]\033[0m {line.strip()}")

    proc.wait(timeout=timeout)

    if denied:
        print(f"  \033[33m[{denied} permission(s) denied this round]\033[0m")

    if proc.returncode != 0:
        print(f"  (exit code {proc.returncode})")

    return proc.returncode


def run_deterministic(yaml_path: Path, pdf_dir: str = ".") -> dict:
    cmd = [
        "nix", "develop", f"path:{SKILL_DIR}", "-c",
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


def run_checker(prompt_text: str, color: str = "", timeout: int = 120,
                agent: str = "coherence", run_id: str | None = None,
                extra: dict | None = None) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(prompt_text)
        tmp_path = f.name

    emit_line(run_id, agent,
              f"── PROMPT GIVEN TO {CHECKER_NAME} ──\n{prompt_text}\n{'─' * 40}\n", extra)

    cmd = ["opencode", "run", "--agent", CHECKER_NAME, "Evaluate the following.", "-f", tmp_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        print(f"{color}{run_tag(run_id)}{line}{RESET if color else ''}", end="", flush=True)
        emit_line(run_id, agent, line, extra)
        lines.append(line)
    proc.wait(timeout=timeout)
    os.unlink(tmp_path)
    return "".join(lines)


def run_semantic_checkers(yaml_path: Path, run_id: str | None = None) -> list[dict]:
    """Check claims in order; stops at the first failing claim so the round
    goes straight back to the search agent (later claims stay unchecked —
    they may shift anyway once the failing one is fixed)."""
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
        result = run_checker(rubric, color=SEMANTIC_COLOR, agent="semantic", run_id=run_id,
                             extra={"claim": i})
        passed = "PASS" in result.upper() and "FAIL" not in result.upper()
        print(f"  {run_tag(run_id)}{'[PASS]' if passed else '[FAIL]'} Claim {i+1}: {claim[:80]}")
        emit(run_id, "claim-status",
             {"index": i, "claim": claim, "status": "pass" if passed else "fail"})
        if not passed:
            failures.append({"claim": claim, "output": result[:2000]})
            return failures

    return failures


def run_coherence_checker(yaml_path: Path, question: str, run_id: str | None = None) -> dict:
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
    result = run_checker(rubric, color=COHERENCE_COLOR, agent="coherence", run_id=run_id)
    passed = "PASS" in result.upper() and "FAIL" not in result.upper()
    print(f"  {run_tag(run_id)}{'[PASS]' if passed else '[FAIL]'} Coherence check")
    return {"passed": passed, "output": result[:2000]}


def _agent_instructions() -> str:
    text = AGENT_SRC.read_text()
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) > 2 else text


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
    msg_lines = [f"Round {round_num}/{MAX_ROUNDS} — the following checks failed. "
                 "Fix the issues and run verify_citations again."]
    for rnd, feedbacks in group_feedback_by_round(rounds):
        plural = "s" if len(feedbacks) != 1 else ""
        msg_lines.append(f"\n--- Round {rnd} feedback ({len(feedbacks)} failure{plural}) ---")
        msg_lines.extend(feedbacks)
    return "\n".join(msg_lines)


def write_context(slug: str, question: str, pdf_info: list[dict], rounds: list[dict]) -> Path:
    path = Path("answers") / f"{slug}-context.md"
    lines = [
        "# Citation-Grounded QA Pipeline",
        "",
        f"## Question",
        f"{question}",
        "",
        f"## PDF Sources",
    ]
    for info in pdf_info:
        lines.append(f"- {info['file']} (pages: {info['pages']}, chunks: {info['chunks']}, ~{info['tokens']}K tokens)")
        lines.append(f"  Full path: {info['abspath']}")

    # List existing answer files the agent can reuse
    existing = sorted(Path("answers").glob("*.yml"))
    if existing:
        lines += ["", "## Existing Answer Files (read with `read` tool to reuse claims)"]
        for f in existing:
            lines.append(f"- {f.name}")

    lines += [
        "",
        "## Instructions",
        "",
        _agent_instructions(),
        "",
        "## Prior Attempts & Feedback",
    ]

    if not rounds:
        lines.append("None yet. This is your first attempt.")
    else:
        for rnd, feedbacks in group_feedback_by_round(rounds):
            lines += ["", f"### Round {rnd}"]
            for fb in feedbacks:
                lines += ["```", fb, "```"]

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


def install_banner(label: str):
    print(f"\n{'#' * 60}", flush=True)
    print(f"#  {label}", flush=True)
    print(f"{'#' * 60}", flush=True)


def install_tools():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    pdf_search_ts = TOOLS_DIR / "pdf-search.ts"
    verify_citations_ts = TOOLS_DIR / "verify-citations.ts"
    pdf_search_ts.write_text(f"""import {{ tool }} from "@opencode-ai/plugin"
import {{ execSync }} from "child_process"

const SKILL_DIR = {json.dumps(str(SKILL_DIR))}

function run(args: string): string {{
  try {{
    return execSync(
      `nix develop "path:${{SKILL_DIR}}" -c python3 ${{SKILL_DIR}}/pdf-search.py ${{args}}`,
      {{ timeout: 60000, encoding: "utf-8" }}
    ).trim()
  }} catch (e: any) {{
    return e.stdout?.trim() || e.stderr?.trim() || e.message
  }}
}}

export default tool({{
  description: "Search PDFs for text, enumerate every distinct match of a regex, retrieve full page content, or get document info",
  args: {{
    action: tool.schema.enum(["search", "search_regex", "get", "info"]).describe("Action to perform"),
    pdf: tool.schema.string().describe("Path to the PDF file"),
    query: tool.schema.string().optional().describe("Text to search for (required for search)"),
    pattern: tool.schema.string().optional().describe(
      "Regex pattern to enumerate (required for search_regex). Returns every DISTINCT matching " +
      "string found anywhere in the document, deduplicated — use this instead of guessing a " +
      "handful of literal names when a claim needs to rule out a whole family of possible names."),
    pages: tool.schema.array(tool.schema.number()).optional().describe("Page numbers to retrieve (required for get)"),
    limit: tool.schema.number().optional().describe("Max search results (default: 10)"),
  }},
  async execute(args) {{
    if (args.action === "info") {{
      return run(`${{JSON.stringify(args.pdf)}} info`)
    }}
    if (args.action === "search") {{
      if (!args.query) return "Error: query is required for search"
      return run(`${{JSON.stringify(args.pdf)}} search ${{JSON.stringify(args.query)}} --limit ${{args.limit ?? 10}}`)
    }}
    if (args.action === "search_regex") {{
      if (!args.pattern) return "Error: pattern is required for search_regex"
      return run(`${{JSON.stringify(args.pdf)}} search-regex ${{JSON.stringify(args.pattern)}}`)
    }}
    if (args.action === "get") {{
      if (!args.pages || args.pages.length === 0) return "Error: page numbers required for get"
      return run(`${{JSON.stringify(args.pdf)}} get ${{args.pages.join(" ")}}`)
    }}
    return `Error: unknown action ${{args.action}}`
  }},
}})
""")

    verify_citations_ts.write_text(f"""import {{ tool }} from "@opencode-ai/plugin"
import {{ execSync }} from "child_process"

const SKILL_DIR = {json.dumps(str(SKILL_DIR))}

export default tool({{
  description: "Verify citations in a YAML answer file against source PDFs",
  args: {{
    yaml: tool.schema.string().describe("Path to the YAML answer file"),
    pdf_dir: tool.schema.string().optional().describe("Directory containing PDFs (default: working directory)"),
  }},
  async execute(args) {{
    const pdfDir = args.pdf_dir ?? "."
    try {{
      const result = execSync(
        `nix develop "path:${{SKILL_DIR}}" -c python3 ${{SKILL_DIR}}/verify-citations.py --pdf-dir ${{JSON.stringify(pdfDir)}} ${{JSON.stringify(args.yaml)}}`,
        {{ timeout: 60000, encoding: "utf-8" }}
      ).trim()
      return result
    }} catch (e: any) {{
      return e.stdout?.trim() || e.stderr?.trim() || e.message
    }}
  }},
}})
""")

    write_answer_ts = TOOLS_DIR / "write-answer.ts"
    write_answer_ts.write_text(f"""import {{ tool }} from "@opencode-ai/plugin"
import * as fs from "fs"

export default tool({{
  description: "Write this run's citation-grounded answer YAML file, overwriting any previous round's attempt.",
  args: {{
    yaml_content: tool.schema.string().describe("Full YAML content to write"),
  }},
  async execute(args) {{
    const slug = process.env.ANSWER_SLUG
    if (!slug) {{
      throw new Error("ANSWER_SLUG is not set — this tool must be run inside the citation-qa pipeline")
    }}
    fs.mkdirSync("answers", {{ recursive: true }})
    const filename = `answers/${{slug}}.yml`
    fs.writeFileSync(filename, args.yaml_content, "utf-8")
    return filename
  }},
}})
""")

    print(f"Installed tools: pdf-search, verify-citations, write-answer")


def search_loop(slug: str, question: str, pdf_info: list[dict], rounds: list[dict],
                pdf_dir: str, run_id: str | None = None) -> tuple[Path | None, int]:
    """Returns (yaml_path, round_num) on success, (None, MAX_ROUNDS) when exhausted."""
    session_id = None
    before_ids = get_session_ids()

    for round_num in range(1, MAX_ROUNDS + 1):
        label = f"ROUND {round_num}/{MAX_ROUNDS}"
        if rounds:
            label += " (with feedback)"
        install_banner(f"{run_tag(run_id)}{label}")
        emit(run_id, "phase", {"phase": "searching", "round": round_num})

        if round_num == 1:
            ctx = write_context(slug, question, pdf_info, rounds)
            run_search_agent(ctx, question, session_id=None, run_id=run_id)
            # Discover the new session ID created by this run
            for _ in range(5):
                time.sleep(1)
                session_id = get_new_session_id(before_ids, run_id or slug)
                if session_id:
                    print(f"  {run_tag(run_id)}[Session: {session_id[:8]}...]", flush=True)
                    break
            if not session_id:
                print(f"  {run_tag(run_id)}[WARN] Could not determine session ID; retries will be fresh sessions", flush=True)
        elif session_id:
            message = build_feedback_message(round_num, rounds)
            run_search_agent(None, question, session_id=session_id, message=message, run_id=run_id)
        else:
            # Fallback: fresh context + fresh session (session discovery failed)
            ctx = write_context(slug, question, pdf_info, rounds)
            run_search_agent(ctx, question, session_id=None, run_id=run_id)

        yaml_path = find_yaml(slug)
        if not yaml_path:
            feedback = "No YAML file produced. Search PDFs and write answers/<slug>.yml."
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            print(f"  {run_tag(run_id)}[FAIL] No YAML file found\n")
            continue

        # Draft answer is visible while the checkers run
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

        # ── Semantic checkers ──
        emit(run_id, "phase", {"phase": "semantic", "round": round_num})
        semantic_failures = run_semantic_checkers(yaml_path, run_id=run_id)
        if semantic_failures:
            sf = semantic_failures[0]
            feedback = (f"Semantic checker FAILED for claim: {sf['claim']}\n"
                        f"(checking stopped at the first failing claim; later claims were not checked)\n"
                        f"Checker output:\n{sf['output']}")
            rounds.append({"round": round_num, "feedback": feedback})
            emit(run_id, "feedback", {"round": round_num, "text": feedback})
            print(f"  {run_tag(run_id)}Restarting with semantic failure on claim: {sf['claim'][:80]}...\n")
            continue

        # ── Coherence checker ──
        emit(run_id, "phase", {"phase": "coherence", "round": round_num})
        coherence = run_coherence_checker(yaml_path, question, run_id=run_id)
        emit(run_id, "check-result",
             {"check": "coherence", "passed": coherence["passed"], "output": coherence["output"][:2000]})
        if not coherence["passed"]:
            feedback = f"Coherence checker FAILED:\n{coherence['output']}"
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
                "nix", "develop", f"path:{SKILL_DIR}", "-c",
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
        print(f"  {info['file']}: {info['pages']} pages, {info['chunks']} chunks, ~{info['tokens']}K tokens")
    return pdf_info


def main():
    global SERVER, PDF_INFO, PDF_DIR

    parser = argparse.ArgumentParser(description="Based Answers — citation-grounded QA web app")
    parser.add_argument("--port", type=int, default=0, help="Port to serve on (default: auto-assign)")
    parser.add_argument("--no-open", action="store_true", help="Don't open the browser")
    args = parser.parse_args()

    os.makedirs("answers", exist_ok=True)

    # Install agents (always copy to pick up changes)
    AGENT_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(AGENT_SRC, AGENT_DST)
    print(f"Installed agent: {AGENT_DST}")

    if not CHECKER_DST.exists():
        shutil.copy2(CHECKER_SRC, CHECKER_DST)
        print(f"Installed agent: {CHECKER_DST}")
    else:
        print(f"Checker already installed: {CHECKER_DST}")

    # Install custom tools
    install_tools()

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
