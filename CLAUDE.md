# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An opencode skill implementing a citation-grounded QA pipeline: given a question and PDF sources, an LLM agent produces a YAML answer where every claim cites a verbatim source quote with page number, verified by deterministic and LLM-based checkers, then rendered as an HTML page with PDF highlights.

## Running

Python dependencies (PyMuPDF, PyYAML, Jinja2) come from the Nix flake — scripts invoked directly will re-enter the flake themselves, but the canonical invocations are:

```sh
# Full pipeline (run from a working directory containing the PDFs, not from the skill dir)
nix develop "path:/Users/robin/.config/opencode/skills/citation-grounded-qa" -c python3 /Users/robin/.config/opencode/skills/citation-grounded-qa/run-pipeline.py "In RP2040 is there a PIO instruction that can detect if a pull will be blocking? or if a pull has blocked?"
```

There are no tests or linters.

All scripts are **working-directory-relative**: they read/write `answers/`, `indexed-pdfs/` (extracted-text cache, keyed by PDF basename), and `answer-pages/` in the CWD. Run the pipeline from the directory where the question's artifacts should live.

## Architecture

`run-pipeline.py` is the orchestrator. Per question (slug derived from the question text):

1. **Setup** — copies `citation-searcher.md` and `coherence-checker.md` into `~/.config/opencode/agents/`, and generates three TypeScript tool wrappers into `~/.config/opencode/tools/` (`pdf-search.ts`, `verify-citations.ts`, `write-answer.ts`) that shell out to the Python scripts via `nix develop`. These are **installed on every run** (searcher always overwritten; checker only if missing), so agent/tool edits belong in this repo, not in `~/.config/opencode/`.
2. **Search loop** (max 5 rounds) — round 1 writes `answers/<slug>-context.md` (question, PDF index info, existing answer files, the agent instructions from `citation-searcher.md`, prior feedback) and starts a fresh `opencode run --agent citation-searcher` session. The session ID is discovered by diffing `opencode session list` before/after; subsequent rounds send accumulated failure feedback to the **same session** via `--session`, so the agent keeps its search history.
3. **Three checkers per round**, each failure appended to `rounds` feedback and triggering a retry:
   - **Deterministic** (`verify-citations.py`): every citation `text` must appear on the stated page of the cached extraction, tried through a ladder of increasingly lenient matches (exact → whitespace-normalized → case-insensitive → hyphen-stripped → arrow-normalized → punctuation-stripped); also verifies `concatenation` equals all claims joined with `". "`. On failure it reports closest-match suggestions and first-diff position.
   - **Semantic** (per claim, with all preceding claims passed as trusted premises) and **coherence** (whole concatenation): rubric prompts sent to the tool-less `coherence-checker` opencode agent; pass requires "PASS" and no "FAIL" in the output. The rubrics live inline in `run-pipeline.py`.
4. **Render** — `format-answers.py` renders `answer-template.html` (Jinja2) to `answer-pages/<slug>.html` with per-citation bounding-box highlights (from `pages_data` in the cache, added lazily on first render) and opens it via `open` (macOS).

### The YAML answer contract

Defined in `citation-searcher.md` and enforced by `verify-citations.py`:

```yaml
question: "..."
concatenation: "<all claims joined with '. '>"   # checked character-exactly
answers:
  - claim: "..."
    citations:
      - text: "<verbatim quote>"   # must appear on that page
        page: 14
        source: "file.pdf"
```

Empty `concatenation` + empty `answers` is the valid "unable to answer" output (and skips the semantic/coherence checks).

### Key couplings to keep in sync

- The `write_answer` TS tool (generated inline in `run-pipeline.py`) and `derive_slug()` in the same file implement the **same slug algorithm** in TS and Python; `find_yaml()` matches `<slug>.yml` or `<slug>-N.yml`. Changing any of these requires changing all.
- `citation-searcher.md` frontmatter is an opencode permission allowlist: the agent gets only read/glob/grep, writes limited to `answers/*.yml`, and the three custom tools — bash, web, and subtasks are denied. The pipeline counts denied-permission lines in agent output, so loosening/tightening permissions affects the retry loop's diagnostics.
- `pdf-search.py`'s cache format (`chunks` with `page`/`text`, plus lazily-added `pages_data` with span bboxes) is consumed by `verify-citations.py` and `format-answers.py` directly.
