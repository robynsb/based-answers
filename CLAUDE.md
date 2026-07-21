# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Based Answers** — an opencode skill implementing a citation-grounded QA web app: questions are typed into a browser question bar; per question, an LLM agent produces a YAML answer where every claim cites a verbatim source quote with page number, verified by deterministic and LLM-based checkers. The run's progress (draft answer, per-claim check status, live output of all three agents) streams to the browser and is persisted, so past runs stay fully viewable across server restarts.

## Running

Python dependencies (PyMuPDF, PyYAML, Jinja2, Flask, flask-sock, simple-websocket) come from the Nix flake — scripts invoked directly will re-enter the flake themselves, but the canonical invocation is:

```sh
# Start the web app (run from a working directory containing the PDFs, not from the skill dir)
nix develop "path:/Users/robin/.config/opencode/skills/citation-grounded-qa" -c python3 /Users/robin/.config/opencode/skills/citation-grounded-qa/based-answers.py
```

This indexes the CWD's `*.pdf`, starts Flask on an auto-assigned port (`--port` pins one, `--no-open` skips the browser), and serves until Ctrl-C. Questions are asked in the UI, never as CLI arguments.

Tests (stdlib unittest, in `tests/`; `tests/support.py` loads the dash-named scripts as modules; `tests/fixtures/` holds the RP2040 datasheet + real answer YAMLs driving the end-to-end tests):

```sh
nix develop "path:." -c python3 -m unittest discover -s tests -t .
```

There are no linters.

All scripts are **working-directory-relative**: they read/write `answers/`, `indexed-pdfs/` (extracted-text cache, keyed by PDF basename), and `citation-qa.db` in the CWD. Run the app from the directory where the question artifacts should live.

## Architecture

`based-answers.py` is the entry point: it installs agents/tools, indexes PDFs, then constructs `live-server.py`'s `PipelineServer` (Flask + flask-sock served by werkzeug in a daemon thread) and blocks. Each submitted question spawns its own worker thread (`run_worker`), so multiple runs proceed in parallel.

Per run (run-id = question slug, `-N` suffixed on re-asks):

1. **Setup (once at server startup, not per run)** — copies `citation-searcher.md` and `coherence-checker.md` into `~/.config/opencode/agents/`, and generates three TypeScript tool wrappers into `~/.config/opencode/tools/` (`pdf-search.ts`, `verify-citations.ts`, `write-answer.ts`) that shell out to the Python scripts via `nix develop`. These are **installed on every server start** (searcher always overwritten; checker only if missing), so agent/tool edits belong in this repo, not in `~/.config/opencode/`.
2. **Search loop** (max `MAX_ROUNDS` = 5 rounds) — round 1 writes `answers/<slug>-context.md` (question, PDF index info, existing answer files, the agent instructions from `citation-searcher.md`, prior feedback) and starts a fresh `opencode run --agent citation-searcher` session titled `citation-qa-<run-id>`. The session ID is discovered by diffing `opencode session list` before/after — the unique title is matched first, and the "any new session" fallback is only trusted when exactly one unclaimed new id exists (`_claimed_sessions`), so parallel runs don't steal each other's sessions. Subsequent rounds send accumulated failure feedback to the **same session** via `--session`.
3. **Three checkers per round**, each failure appended to `rounds` feedback and triggering a retry:
   - **Deterministic** (`verify-citations.py`): every citation `text` must be at least 200 chars (`MIN_CITATION_CHARS`) and appear on the stated page of the cached extraction, tried through a ladder of increasingly lenient matches (exact → whitespace-normalized → case-insensitive → hyphen-stripped → arrow-normalized → punctuation-stripped); a quote spanning a page break passes when ≥20 chars of it match the stated page and the remainder matches the adjacent page (headers/footers sit between the halves, so joined page text can't match). On failure it reports closest-match suggestions and a found-on-page-X hint when the quote lives on a different page.
   - **Semantic** (per claim in order, with all preceding claims passed as trusted premises; stops at the first failing claim and returns to the searcher without checking the rest) and **coherence** (all claims joined into one paragraph): rubric prompts sent to the tool-less `coherence-checker` opencode agent; pass requires "PASS" and no "FAIL" in the output. The rubrics live inline in `based-answers.py`.
4. **Live view** — the answer fragment is rendered (`format_answers.build_context` + `answer-body.html`) and pushed to the browser as an `answer` event, so the draft is visible while the searcher is still working and while the checkers run; the final state is `passed`, `exhausted` (last/empty answer shown), or `error`.

   An `AnswerWatcher` (one per round, in `based-answers.py`) polls `answers/<slug>.yml` every `ANSWER_POLL_SECONDS` for the lifetime of the searcher subprocess, so each `write_answer` call inside a round emits a fresh `answer` event and the answer updates in place as the agent iterates on it — several `answer` events per round is normal. It baselines the file already on disk at round start, so the previous round's leftover YAML is never shown as this round's output; it de-dupes on file content, and a rewrite caught half-written fails to render and is retried on the next poll. `search_loop()` emits once itself after the agent exits only if the watcher never emitted (`watcher.emitted`), so a round always displays the answer its checkers are checking. On the browser side, an `answer` arriving before the `deterministic` phase creates the "Citations verified verbatim against PDFs" check row in its pending state, so the draft is visibly unverified.

### Persistence & event stream

`citation-qa.db` (SQLite, WAL) in the CWD is the source of truth: `runs(run_id, question, slug, status running|passed|exhausted|error, created_at, finished_at, yaml_path)` and `events(id AUTOINCREMENT, run_id, event, data JSON, ts)`. `PipelineServer.emit()` inserts the event row and fans out to that run's connected WebSocket clients. `WS /ws/<run-id>` replays events after the client's last-seen id from the DB, then streams live — refresh, reconnect, and viewing after a restart are the same code path. A stream ends at a terminal `phase` event; server startup flips stale "running" runs to "error" and appends that terminal event.

**The event vocabulary is a coupling** shared by `based-answers.py` (emitters), the `events` table, and `run.html`'s client JS: `phase` (searching|deterministic|semantic|coherence|passed|exhausted|error, round), `agent-line` (agent searcher|semantic|coherence, ANSI-stripped line; semantic lines carry a `claim` index so the UI attaches them to that claim's stream; the context/feedback given to the searcher and the rubric given to each checker are emitted as stream lines too), `claim-status` (index, claim, checking|pass|fail), `check-result` (deterministic|coherence, passed, output), `answer` (html fragment), `feedback` (round, text — persisted but not displayed; the UI shows the feedback via the next round's searcher stream).

`run.html` buckets the ordered event stream into **rounds** client-side (`phase` events demarcate them): the page shows the latest round; earlier rounds' answers, citations, checks, and streams are reachable via the fixed round-navigation arrows at the bottom right. Streams are inline collapsibles: the searcher stream under the answer, a semantic-checker stream under each claim row, and the coherence stream under the coherence check row.

HTTP routes: `GET /` (question bar + run history + legacy `answers/*.yml` without run rows), `POST /ask`, `GET /run/<run-id>`, `GET /answer/<name>` (legacy YAML render), `GET /pdf/<basename>` (whitelist of registered PDFs only — pdf.js in the browser fetches these for page previews with highlight bboxes).

### The YAML answer contract

Defined in `citation-searcher.md` and enforced by `verify-citations.py`:

```yaml
question: "..."
answers:
  - claim: "..."
    citations:
      - text: "<verbatim quote>"   # must appear on that page
        page: 14
        source: "file.pdf"
```

An empty `answers` list is the valid "unable to answer" output (and skips the semantic/coherence checks). Older answer files may still carry a `concatenation` field (once required to equal the claims joined with `". "`); nothing reads it anymore.

A citation may instead be a `search_result` (`type: search_result`, `source`, `query`, `results: [{page, text}, ...]`), used to prove absence ("the source never mentions X") or exhaustiveness ("this is the complete list") rather than quote a passage. `verify-citations.py` reruns `query` against the cache (via `pdf_search.find_matches()`) and requires the claimed `results` to exactly match what comes back — by page, unbounded — so it catches both a fabricated hit and an omitted one; the 200-char/verbatim-match rules for normal citations don't apply. `based-answers.py`'s semantic rubric only adds the `SEARCH_RESULTS` block (and a query-adequacy critique — an obviously missing rephrasing/synonym should FAIL) to a claim that actually carries one; other claims see the unmodified rubric. `format-answers.py` renders these with no PDF preview/highlights, and deduplicates identical `(source, query, results)` search_result citations across claims into one reference — a separate exact-match pass from the fuzzy quote-overlap merge normal citations get.

A `search_result` citation may also set `mode: regex`, for claims that rule out a whole *family* of possible names rather than one literal phrase (e.g. "no `pio_sm_*` function reads back enabled state") — a handful of guessed literal queries can never be exhaustive. In `mode: regex`, `query` is a full Python regex and each result is `{match, page, text}` — `pdf_search.find_distinct_matches()` (agent-facing as the `search_regex` tool action) enumerates every *distinct* string the pattern matches anywhere in the doc, deduplicated, capped (currently 100, tunable) so a too-broad pattern fails loudly rather than silently truncating (a truncated enumeration could hide the exact symbol that disproves the claim). `verify-citations.py`'s regex branch checks each claimed result's `text` is on-page and its `match` is actually produced by re-running the pattern against that same (normalized) text, then requires the claimed match-string *set* to exactly equal an independent rerun's. The rubric's `ENUMERATION` block shows the checker the complete, verified match list and asks it to judge whether that list (plus SOURCE_TEXTS) supports CLAIM, and whether the pattern itself covers the right family — ordinary LLM judgment, same as the rest of this checker's reasoning.

### Key couplings to keep in sync

- A run's filesystem slug is always its `run_id` (the possibly `-N`-suffixed id `PipelineServer.create_run()` assigns once, up front, to dedupe re-asks of an identical question) — `run_worker()` never re-derives a slug from the question text. `run_search_agent()` passes that slug to each round's `opencode run` subprocess via the `ANSWER_SLUG` env var; the `write_answer` TS tool (generated inline in `based-answers.py`) reads it from `process.env` and unconditionally overwrites `answers/<slug>.yml` — the searcher agent itself never sees or handles a slug, and the tool never derives its own or bumps `-N`, so a run's answer file never sprawls across rounds. `find_yaml()` still tolerates a `<slug>-N.yml` match (max by mtime) for legacy multi-file directories predating this, but a live run only ever produces the exact `<slug>.yml`. `derive_slug()` is used only to seed `create_run()`'s dedup check and as the env-var fallback if `run_id` is ever absent.
- `citation-searcher.md` frontmatter is an opencode permission allowlist: the agent gets only read/glob/grep, writes limited to `answers/*.yml`, and the three custom tools — bash, web, and subtasks are denied. The pipeline counts denied-permission lines in agent output, so loosening/tightening permissions affects the retry loop's diagnostics.
- `pdf-search.py`'s cache format (`chunks` with `page`/`text`, plus lazily-added `pages_data` with span bboxes) is consumed by `verify-citations.py` and `format-answers.py` directly.
- `answer-body.html` is the shared answer markup: included by `answer-template.html` (static pages via `format-answers.py`, which remains a standalone CLI renderer writing `answer-pages/<slug>.html`) and rendered as the `answer` event fragment by `PipelineServer.render_answer_fragment()`. Refs carry a `pdf_url` (`file://…` for static output, `/pdf/<basename>` in the app) — `build_context(yaml_path, pdf_url_for=...)` controls it.
- `run.html`'s status bar takes `max_rounds` from `MAX_ROUNDS` in `based-answers.py` (via the server); never hardcode the round count in templates.
