#!/usr/bin/env python3
"""
Orchestrator for citation-grounded QA.

Spawns opencode agents to search PDFs, answer questions, and verify
citations deterministically. Handles retry loops and auto-installs the
citation-searcher agent.

Usage:
  python3 run-pipeline.py <question> [pdf1 pdf2 ...]
  
If no PDFs are given, all *.pdf in the working directory are used.
Must be run from the skill directory (nix develop handles dependencies).
"""

import argparse
import json
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
TOOLS_DIR = Path.home() / ".config/opencode/tools"
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


def run_search_agent(prompt_path: Path, question: str, timeout: int = 600) -> int:
    print(f"\n{'─' * 60}", flush=True)
    print(f"CONTEXT GIVEN TO {AGENT_NAME}:", flush=True)
    print(f"{'─' * 60}", flush=True)
    print(prompt_path.read_text(), flush=True)
    print(f"{'─' * 60}\n", flush=True)

    cmd = ["opencode", "run", "--agent", AGENT_NAME, question, "-f", str(prompt_path)]
    print(f"  Agent: {AGENT_NAME}", flush=True)
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


def run_deterministic(yaml_path: Path, pdf_dir: str = ".") -> dict:
    cmd = [
        "nix", "develop", f"path:{SKILL_DIR}", "-c",
        sys.executable, str(SKILL_DIR / "verify-citations.py"),
        "--pdf-dir", pdf_dir,
        "-v", str(yaml_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    return {"passed": result.returncode == 0, "output": output}


def run_checker(prompt_text: str, timeout: int = 120) -> str:
    print(f"\n{'─' * 60}", flush=True)
    print(f"CONTEXT GIVEN TO CHECKER:", flush=True)
    print(f"{'─' * 60}", flush=True)
    print(prompt_text, flush=True)
    print(f"{'─' * 60}\n", flush=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(prompt_text)
        tmp_path = f.name

    cmd = ["opencode", "run", "Evaluate the following.", "-f", tmp_path]
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


def _agent_instructions() -> str:
    text = AGENT_SRC.read_text()
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) > 2 else text


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
        for i, r in enumerate(rounds, 1):
            lines += ["", f"### Round {i}", "```", r["feedback"], "```"]

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


def search_loop(slug: str, question: str, pdf_info: list[dict], rounds: list[dict], pdf_dir: str) -> Path | None:
    for round_num in range(1, MAX_ROUNDS + 1):
        label = f"SEARCH ROUND {round_num}/{MAX_ROUNDS}"
        if rounds:
            label += " (with feedback)"
        install_banner(label)

        ctx = write_context(slug, question, pdf_info, rounds)
        run_search_agent(ctx, question)

        yaml_path = find_yaml(slug)
        if not yaml_path:
            rounds.append({"feedback": "No YAML file produced. Search PDFs and write answers/<slug>.yml."})
            print("  [FAIL] No YAML file found\n")
            continue

        v = run_deterministic(yaml_path, pdf_dir)
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


def install_tools():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    skill_dir_escaped = str(SKILL_DIR).replace("\\", "\\\\")
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
  description: "Search PDFs for text, retrieve full page content, or get document info",
  args: {{
    action: tool.schema.enum(["search", "get", "info"]).describe("Action to perform"),
    pdf: tool.schema.string().describe("Path to the PDF file"),
    query: tool.schema.string().optional().describe("Text to search for (required for search)"),
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
  description: "Write a citation-grounded answer YAML file. Derives the slug from the question and handles -N suffix for retries.",
  args: {{
    question: tool.schema.string().describe("The original question (used to derive the filename slug)"),
    yaml_content: tool.schema.string().describe("Full YAML content to write"),
  }},
  async execute(args) {{
    fs.mkdirSync("answers", {{ recursive: true }})

    let slug = args.question.toLowerCase()
      .replace(/[^a-z0-9\\s-]/g, "")
      .replace(/\\s+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 80)

    let filename = `answers/${{slug}}.yml`
    if (fs.existsSync(filename)) {{
      let n = 2
      while (fs.existsSync(`answers/${{slug}}-${{n}}.yml`)) {{
        n++
      }}
      filename = `answers/${{slug}}-${{n}}.yml`
    }}

    fs.writeFileSync(filename, args.yaml_content, "utf-8")
    return `${{filename}}`
  }},
}})
""")

    print(f"Installed tools: pdf-search, verify-citations, write-answer")


def main():
    parser = argparse.ArgumentParser(description="Citation-grounded QA pipeline orchestrator")
    parser.add_argument("question", help="Question to answer")
    parser.add_argument("pdfs", nargs="*", help="PDF source file(s) — defaults to all *.pdf in working directory")
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

    # Install custom tools
    install_tools()

    # Collect PDFs from args or scan working directory
    pdf_paths = args.pdfs if args.pdfs else sorted(Path(".").glob("*.pdf"))
    if not pdf_paths:
        print("Error: no PDF files found (provide as args or put them in working directory)", file=sys.stderr)
        sys.exit(1)

    # Index PDFs
    pdf_info = []
    for pdf in pdf_paths:
        pdf_path = Path(pdf).resolve()
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

    # Determine PDF directory for verify-citations
    pdf_dir = str(pdf_paths[0].parent) if pdf_paths else "."

    # Main search loop
    rounds = []
    yaml_path = search_loop(slug, question, pdf_info, rounds, pdf_dir)

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
            result = run_checker(rubric)
            passed = "PASS" in result.upper() and "FAIL" not in result.upper()
            print(f"  {'[PASS]' if passed else '[FAIL]'} Claim {i+1}: {claim[:80]}")
            if not passed:
                semantic_failures.append({"claim": claim, "output": result[:2000]})

    if semantic_failures:
        for sf in semantic_failures:
            rounds.append({"feedback": f"Semantic checker FAILED for claim: {sf['claim']}\nChecker output:\n{sf['output']}"})
        print(f"\n  Restarting search with {len(semantic_failures)} semantic failure(s)...")
        yaml_path = search_loop(slug, question, pdf_info, rounds, pdf_dir)

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
        result = run_checker(rubric)
        passed = "PASS" in result.upper() and "FAIL" not in result.upper()
        print(f"  {'[PASS]' if passed else '[FAIL]'} Coherence check")
        if not passed:
            rounds.append({"feedback": f"Coherence checker FAILED:\n{result[:2000]}"})
            print(f"\n  Restarting search with coherence failure...")
            yaml_path = search_loop(slug, question, pdf_info, rounds, pdf_dir)
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