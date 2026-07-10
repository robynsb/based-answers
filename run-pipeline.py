#!/usr/bin/env python3
"""
Orchestrator for citation-grounded QA.

Spawns a single opencode agent session that searches PDFs, answers
questions, and self-corrects using verify_citations, check_semantic,
and check_coherence tools in an internal loop.

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
from pathlib import Path


SKILL_DIR = Path(__file__).parent.resolve()
AGENT_NAME = "citation-searcher"
AGENT_SRC = SKILL_DIR / f"{AGENT_NAME}.md"
AGENT_DST = Path.home() / ".config/opencode/agents" / f"{AGENT_NAME}.md"
CHECKER_NAME = "coherence-checker"
CHECKER_SRC = SKILL_DIR / f"{CHECKER_NAME}.md"
CHECKER_DST = Path.home() / ".config/opencode/agents" / f"{CHECKER_NAME}.md"
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
    print(f"  Agent: {AGENT_NAME} (single session, self-correcting)", flush=True)
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


def _agent_instructions() -> str:
    text = AGENT_SRC.read_text()
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) > 2 else text


def write_context(slug: str, question: str, pdf_info: list[dict]) -> Path:
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
        f"## Max Retry Rounds",
        f"You have up to {MAX_ROUNDS} attempts total across all checkers. If none pass, write empty YAML.",
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


def install_tools():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    skill_dir_escaped = str(SKILL_DIR).replace("\\", "\\\\")
    pdf_search_ts = TOOLS_DIR / "pdf-search.ts"
    verify_citations_ts = TOOLS_DIR / "verify-citations.ts"
    check_semantic_ts = TOOLS_DIR / "check-semantic.ts"
    check_coherence_ts = TOOLS_DIR / "check-coherence.ts"

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

    check_semantic_ts.write_text(f"""import {{ tool }} from "@opencode-ai/plugin"
import {{ execSync }} from "child_process"

const SKILL_DIR = {json.dumps(str(SKILL_DIR))}

export default tool({{
  description: "Check all claims in a YAML answer against their source texts for semantic validity",
  args: {{
    yaml: tool.schema.string().describe("Path to the YAML answer file"),
  }},
  async execute(args) {{
    try {{
      const result = execSync(
        `nix develop "path:${{SKILL_DIR}}" -c python3 ${{SKILL_DIR}}/check-semantic.py ${{JSON.stringify(args.yaml)}}`,
        {{ timeout: 180000, encoding: "utf-8" }}
      ).trim()
      return result
    }} catch (e: any) {{
      return e.stdout?.trim() || e.stderr?.trim() || e.message
    }}
  }},
}})
""")

    check_coherence_ts.write_text(f"""import {{ tool }} from "@opencode-ai/plugin"
import {{ execSync }} from "child_process"

const SKILL_DIR = {json.dumps(str(SKILL_DIR))}

export default tool({{
  description: "Check the coherence and completeness of the concatenated answer against the question",
  args: {{
    yaml: tool.schema.string().describe("Path to the YAML answer file"),
    question: tool.schema.string().describe("The original question"),
  }},
  async execute(args) {{
    try {{
      const result = execSync(
        `nix develop "path:${{SKILL_DIR}}" -c python3 ${{SKILL_DIR}}/check-coherence.py ${{JSON.stringify(args.yaml)}} ${{JSON.stringify(args.question)}}`,
        {{ timeout: 180000, encoding: "utf-8" }}
      ).trim()
      return result
    }} catch (e: any) {{
      return e.stdout?.trim() || e.stderr?.trim() || e.message
    }}
  }},
}})
""")

    print(f"Installed tools: pdf-search, verify-citations, write-answer, check-semantic, check-coherence")


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

    # Install agents
    AGENT_DST.parent.mkdir(parents=True, exist_ok=True)
    if not AGENT_DST.exists():
        shutil.copy2(AGENT_SRC, AGENT_DST)
        print(f"Installed agent: {AGENT_DST}")
    else:
        print(f"Agent already installed: {AGENT_DST}")

    if not CHECKER_DST.exists():
        shutil.copy2(CHECKER_SRC, CHECKER_DST)
        print(f"Installed agent: {CHECKER_DST}")
    else:
        print(f"Checker already installed: {CHECKER_DST}")

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

    pdf_dir = str(pdf_paths[0].parent) if pdf_paths else "."

    # Single shot: write context and run the search agent once
    print_banner("SEARCH AGENT (single session, self-correcting)")
    ctx = write_context(slug, question, pdf_info)
    run_search_agent(ctx, question)

    # Find result
    yaml_path = find_yaml(slug)
    if yaml_path is None:
        print(f"\n[FAIL] No YAML file produced.")
        yaml_path = Path("answers") / f"{slug}.yml"
        yaml_path.write_text(f"question: \"{question}\"\nconcatenation: \"\"\nanswers: []\n")
        print(f"Wrote empty answer: {yaml_path}")

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
