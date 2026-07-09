#!/usr/bin/env python3
"""
Orchestrator for citation-grounded QA.

Spawns opencode agents to search PDFs, answer questions, and verify
citations deterministically. Handles retry loops and auto-installs the
citation-searcher agent.

Usage:
  python3 run-pipeline.py <question> <pdf1> [pdf2 ...]

Must be run from the skill directory (nix develop handles dependencies).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).parent.resolve()
AGENT_NAME = "citation-searcher"
AGENT_SRC = SKILL_DIR / f"{AGENT_NAME}.md"
AGENT_DST = Path.home() / ".config/opencode/agents" / f"{AGENT_NAME}.md"
MAX_ROUNDS = 3


def derive_slug(question: str) -> str:
    slug = question.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = slug.replace(" ", "-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


def print_banner(label: str):
    print(f"\n{'#' * 60}\n#  {label}\n{'#' * 60}\n", flush=True)


def run_opencode(session: str, prompt_path: Path, is_retry: bool = False, timeout: int = 600) -> int:
    cmd = ["opencode", "run", "--session", session, "--agent", AGENT_NAME, "-f", str(prompt_path)]
    if is_retry:
        cmd.append("--continue")

    print(f"  Session: {session}  |  Agent: {AGENT_NAME}  |  {'retry' if is_retry else 'initial'}", flush=True)
    print(f"{'-' * 50}", flush=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    denied = 0
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        print(line, end="", flush=True)
        if "permission requested" in line.lower() or "auto-rejecting" in line.lower():
            denied += 1
            print(f"  \033[33m[DENIED]\033[0m {line.strip()}")

    proc.wait(timeout=timeout)

    if denied:
        print(f"  \033[33m[{denied} permission(s) denied this round]\033[0m")

    if proc.returncode != 0:
        print(f"  (exit code {proc.returncode})")

    return proc.returncode


def run_deterministic(yaml_path: Path) -> dict:
    cmd = [
        "nix", "develop", f"path:{SKILL_DIR}", "-c",
        sys.executable, str(SKILL_DIR / "verify-citations.py"),
        "-v", str(yaml_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    return {"passed": result.returncode == 0, "output": output}


def run_checker(session: str, prompt_text: str, timeout: int = 120) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, prefix=f"{session}-") as f:
        f.write(prompt_text)
        tmp_path = f.name

    cmd = ["opencode", "run", "--session", session, "-f", tmp_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        print(line, end="", flush=True)
        lines.append(line)
    proc.wait(timeout=timeout)
    os.unlink(tmp_path)
    return "".join(lines)


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

    lines += [
        "",
        "## Instructions",
        "",
        f"You are a citation-grounded QA agent. Full instructions: {AGENT_SRC}",
        "",
        "Key rules:",
        "- Every claim needs a verbatim citation with page number",
        "- No world knowledge",
        "- Write to `answers/<slug>.yml` (slug from this file's name)",
        "- Run `nix develop \"path:SKILL_DIR\" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml`",
        "  to check your work before exiting",
        "- If you cannot find evidence, write empty YAML",
        "",
        f"SKILL_DIR (for nix develop commands): {SKILL_DIR}",
        "",
        "## Prior Attempts & Feedback",
    ]

    if not rounds:
        lines.append("None yet. This is your first attempt.")
    else:
        for i, r in enumerate(rounds, 1):
            lines += ["", f"### Round {i}", "```", r["feedback"], "```"]

    path.write_text("\n".join(lines) + "\n")
    return path


def find_yaml(slug: str) -> Path | None:
    for p in sorted(Path("answers").glob(f"{slug}*.yml")):
        stem = p.stem
        if stem == slug or re.match(rf"^{re.escape(slug)}-\d+$", stem):
            return p
    return None


def search_loop(slug: str, question: str, pdf_info: list[dict], rounds: list[dict]) -> Path | None:
    for round_num in range(1, MAX_ROUNDS + 1):
        label = f"SEARCH ROUND {round_num}/{MAX_ROUNDS}"
        if rounds:
            label += " (with feedback)"
        install_banner(label)

        ctx = write_context(slug, question, pdf_info, rounds)
        run_opencode(slug, ctx, is_retry=bool(rounds))

        yaml_path = find_yaml(slug)
        if not yaml_path:
            rounds.append({"feedback": "No YAML file produced. Search PDFs and write answers/<slug>.yml."})
            print("  [FAIL] No YAML file found\n")
            continue

        v = run_deterministic(yaml_path)
        if v["passed"]:
            print(f"  [PASS] {yaml_path.name} — all citations verified\n")
            return yaml_path

        print(f"  [FAIL] Verification failed\n")
        rounds.append({"feedback": f"Deterministic verification FAILED for {yaml_path.name}:\n" + v["output"]})

    return None


def install_banner(label: str):
    print(f"\n{'#' * 60}", flush=True)
    print(f"#  {label}", flush=True)
    print(f"{'#' * 60}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Citation-grounded QA pipeline orchestrator")
    parser.add_argument("question", help="Question to answer")
    parser.add_argument("pdfs", nargs="+", help="PDF source file(s)")
    args = parser.parse_args()

    question = args.question.strip()
    if not question:
        print("Error: question is required", file=sys.stderr)
        sys.exit(1)

    slug = derive_slug(question)
    os.makedirs("answers", exist_ok=True)

    # Install agent
    AGENT_DST.parent.mkdir(parents=True, exist_ok=True)
    if not AGENT_DST.exists():
        shutil.copy2(AGENT_SRC, AGENT_DST)
        print(f"Installed agent: {AGENT_DST}")
    else:
        print(f"Agent already installed: {AGENT_DST}")

    # Index PDFs
    pdf_info = []
    for pdf in args.pdfs:
        pdf_path = Path(pdf)
        if not pdf_path.exists():
            print(f"Error: PDF not found: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Indexing: {pdf_path.name}")
        cmd = [
            "nix", "develop", f"path:{SKILL_DIR}", "-c",
            sys.executable, str(SKILL_DIR / "pdf-search.py"),
            str(pdf_path), "info",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        info = {"file": pdf_path.name, "pages": "?", "chunks": "?", "tokens": "?"}
        for line in (r.stdout or "").split("\n"):
            if "Pages:" in line:
                info["pages"] = line.split(":", 1)[1].strip()
            elif "Chunks:" in line:
                info["chunks"] = line.split(":", 1)[1].strip()
            elif "tokens:" in line.lower():
                info["tokens"] = line.split(":", 1)[1].strip()
        pdf_info.append(info)
        print(f"  {info['file']}: {info['pages']} pages, {info['chunks']} chunks, ~{info['tokens']} tokens")

    # Main search loop
    rounds = []
    yaml_path = search_loop(slug, question, pdf_info, rounds)

    if yaml_path is None:
        print(f"\n[EXHAUSTED] All {MAX_ROUNDS} rounds failed.")
        yaml_path = Path("answers") / f"{slug}.yml"
        yaml_path.write_text(f"question: \"{question}\"\nconcatenation: \"\"\nanswers: []\n")
        print(f"Wrote empty answer: {yaml_path}")

    # Semantic checkers
    try:
        import yaml as pyyaml
        with open(yaml_path) as f:
            data = pyyaml.safe_load(f)
        answers = data.get("answers", []) if data else []
    except Exception:
        answers = []

    semantic_failures = []
    if answers:
        print()
        install_banner(f"SEMANTIC CHECKERS ({len(answers)} claims)")

        for i, a in enumerate(answers):
            claim = a.get("claim", "")
            if not claim:
                continue
            texts = [c.get("text", "") for c in a.get("citations", [])]
            rubric = f"""You are a claim verifier.

CLAIM: {claim}

SOURCE_TEXTS:
"""
            for t in texts:
                rubric += f"  - \"{t}\"\n"
            rubric += """

OUT-OF-CONTEXT CHECK: Every source must include text before, text related to the claim, and text after. If bare snippet → FAIL.

Does the SYNTHESIS of all SOURCE_TEXTS strictly imply CLAIM?
- YES: Respond "PASS"
- FAIL (out-of-context): Respond "FAIL: out-of-context — <why>"
- NO: Respond "FAIL: <what cannot be inferred>"

Rules: direct logical inference OK. Cross-source inference OK. External domain knowledge = FAIL. Never use your own knowledge.
"""
            result = run_checker(f"{slug}-sem-{i}", rubric)
            passed = "PASS" in result.upper() and "FAIL" not in result.upper()
            print(f"  {'[PASS]' if passed else '[FAIL]'} Claim {i+1}: {claim[:80]}")
            if not passed:
                semantic_failures.append({"claim": claim, "output": result[:2000]})

    if semantic_failures:
        for sf in semantic_failures:
            rounds.append({"feedback": f"Semantic checker FAILED for claim: {sf['claim']}\nChecker output:\n{sf['output']}"})
        print(f"\n  Restarting search with {len(semantic_failures)} semantic failure(s)...")
        yaml_path = search_loop(slug, question, pdf_info, rounds)

        if yaml_path is None:
            print(f"\n[EXHAUSTED] After semantic failures.")
            yaml_path = Path("answers") / f"{slug}.yml"
            if not yaml_path.exists():
                yaml_path.write_text(f"question: \"{question}\"\nconcatenation: \"\"\nanswers: []\n")
            print(f"Proceeding with: {yaml_path}")

    # Coherence checker
    try:
        with open(yaml_path) as f:
            data = pyyaml.safe_load(f)
        concatenation = (data or {}).get("concatenation", "")
    except Exception:
        concatenation = ""

    if concatenation:
        print()
        print_banner("COHERENCE CHECKER")

        rubric = f"""You are a coherence and completeness verifier.

QUESTION: {question}

CONCATENATION: {concatenation}

Evaluate:
1. COHERENCE: sensible paragraph with established concepts?
2. COMPLETENESS: totally answers the question?

Respond with:
- PASS
- FAIL: <what is missing, unclear, or doesn't make sense>
"""
        result = run_checker(f"{slug}-coh", rubric)
        passed = "PASS" in result.upper() and "FAIL" not in result.upper()
        print(f"  {'[PASS]' if passed else '[FAIL]'} Coherence check")
        if not passed:
            rounds.append({"feedback": f"Coherence checker FAILED:\n{result[:2000]}"})
            print(f"\n  Restarting search with coherence failure...")
            yaml_path = search_loop(slug, question, pdf_info, rounds)
            if yaml_path is None:
                print(f"\n[EXHAUSTED] After coherence search.")
                yaml_path = Path("answers") / f"{slug}.yml"
                if not yaml_path.exists():
                    yaml_path.write_text(f"question: \"{question}\"\nconcatenation: \"\"\nanswers: []\n")

    # Format & open
    print()
    print(f"{'#' * 60}")
    print(f"#  GENERATING HTML")
    print(f"{'#' * 60}")
    print()

    fmt_cmd = [
        "nix", "develop", f"path:{SKILL_DIR}", "-c",
        sys.executable, str(SKILL_DIR / "format-answers.py"),
        str(yaml_path),
    ]
    r = subprocess.run(fmt_cmd, capture_output=True, text=True)
    html_path = r.stdout.strip()
    if html_path and Path(html_path).exists():
        print(f"HTML: {html_path}")
        subprocess.run(["open", html_path])
        print("Opened in browser.")
    else:
        print(f"Error: {r.stdout}\n{r.stderr}", file=sys.stderr)
        print("Falling back to verify-citations summary:")
        subprocess.run([
            "nix", "develop", f"path:{SKILL_DIR}", "-c",
            sys.executable, str(SKILL_DIR / "verify-citations.py"),
            str(yaml_path),
        ])

    print(f"\nDone. Answer file: {yaml_path}")


if __name__ == "__main__":
    main()