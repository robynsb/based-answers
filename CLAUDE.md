# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Based Answers** — a citation-grounded QA web app (it lives under `~/.config/opencode/skills/` for historical reasons, but the agent it drives is `pi`, not opencode): questions are typed into a browser question bar; per question, an LLM agent produces a YAML answer where every claim cites a verbatim source quote with page number, verified by deterministic and LLM-based checkers. The run's progress (draft answer, per-claim check status, live output of all three agents) streams to the browser and is persisted, so past runs stay fully viewable across server restarts.

## Running

Python dependencies (PyMuPDF, PyYAML, Jinja2, Flask, flask-sock, simple-websocket) and the `pi` agent binary come from the Nix flake. Nix appears only at the entry point: nothing in the source shells out to `nix develop`, and the scripts do *not* re-enter the flake themselves — they need an interpreter that already has the dependencies, which is what the canonical invocation provides:

```sh
# Start the web app (run from a working directory containing the PDFs, not from the skill dir)
nix develop "path:/Users/robin/.config/opencode/skills/citation-grounded-qa" -c python3 /Users/robin/.config/opencode/skills/citation-grounded-qa/based-answers.py
```

This indexes the CWD's `*.pdf`, starts Flask on an auto-assigned port (`--port` pins one, `--no-open` skips the browser), and serves until Ctrl-C. Questions are asked in the UI, never as CLI arguments.

**Credentials**: the LLM is reached through pi, which needs `DEEPSEEK_API_KEY` in its process environment. `api_key()` takes it from the environment if set, else from the macOS Keychain generic password under service `deepseek` (`BA_KEYCHAIN_SERVICE` overrides). A global `pi /login` does **not** apply — `PI_CODING_AGENT_DIR` is redirected (see below), so pi never reads `~/.pi/agent/auth.json`. Startup prints which source was used, never the key. The model is `deepseek/deepseek-v4-flash` (`BA_PI_MODEL` overrides).

Tests (stdlib unittest, in `tests/`; `tests/support.py` loads the dash-named scripts as modules; `tests/fixtures/` holds the RP2040 datasheet + real answer YAMLs driving the end-to-end tests):

```sh
nix develop "path:." -c python3 -m unittest discover -s tests -t .
```

There are no linters.

All scripts are **working-directory-relative**: they read/write `answers/`, `indexed-pdfs/` (extracted-text cache, keyed by PDF basename), `.based-answers/` (all pi state), and `citation-qa.db` in the CWD. Run the app from the directory where the question artifacts should live.

## Architecture

`based-answers.py` is the entry point: it indexes PDFs, then constructs `live-server.py`'s `PipelineServer` (Flask + flask-sock served by werkzeug in a daemon thread) and blocks. Each submitted question spawns its own worker thread (`run_worker`), so multiple runs proceed in parallel.

**Nothing is installed into `$HOME`.** Earlier versions copied agent definitions into `~/.config/opencode/agents/` and generated tool wrappers into `~/.config/opencode/tools/` on every start; none of that exists now. The agent prompts are read from this repo and passed as `--system-prompt`, and the tools are checked-in `.ts` files handed to pi by absolute path.

Per run (run-id = question slug, `-N` suffixed on re-asks):

1. **The agent transport** (`pi_rpc.py`) — `PiSession` runs `pi --mode rpc`, a JSONL protocol on stdin/stdout, and drives it as a deterministic subroutine: `prompt()` sends a message and blocks until the run is over, then control returns to the pipeline. `build_command()` fixes the isolation, and every flag there is load-bearing:
   - `--no-extensions --no-approve` — nothing is discovered from `~/.pi` or a project `.pi`; only explicit `-e` paths load (pi's own help confirms `-e` still works under `--no-extensions`).
   - `--no-builtin-tools` + `--tools` — the agent gets exactly the pipeline's tools and no bash/edit/write/web. **pi does not validate `--tools`**: a name no extension registers is accepted silently, leaving the agent with no tools and failing several rounds later as a vague "cannot search". `tests/test_tool_names.py` pins `SEARCH_TOOLS`, `TOOL_EXTENSIONS` and the `registerTool()` names in `tools/*.ts` to each other so drift fails at test time.
   - `--system-prompt` — `citation-searcher.md` / `coherence-checker.md` read straight from this repo.
   - `PI_CODING_AGENT_DIR=.based-answers/pi` — settings, auth, trust and sessions all relocate under the CWD, so a run can neither read nor mutate global pi state.

   **Settling is version-dependent.** pi ≥ 0.80 emits `agent_settled` once it will not continue automatically; pi 0.79 (the version in nixpkgs) has no such event and its `agent_end` is terminal with no `willRetry`. So `agent_end` arms a settle `_SETTLE_GRACE` seconds out rather than ending the round: on newer pi the `agent_settled` that follows wins the race, on 0.79 the grace expires. Any event in `_WORK_RESUMED` disarms it, so retries and queued continuations are still waited out on both. **When something looks wrong, the authoritative reference is the docs in the nix store** (`$(dirname $(readlink -f $(which pi)))/../lib/node_modules/pi-monorepo/docs/`), not the latest npm tarball — nixpkgs lags, and both bugs found in the first live run came from reading 0.81 docs against a 0.79 binary.

   The three tools live in `tools/*.ts` as checked-in pi extensions. They need no `package.json` or `node_modules` — only pi's own types and node builtins — and take `BA_PYTHON` (this process's already-resolved interpreter) and `BA_SKILL_DIR` from the environment, so they invoke the Python scripts directly whatever provided that interpreter.
2. **Search loop** (max `MAX_ROUNDS` = 5 rounds) — `search_loop()` opens **one `PiSession` for the whole run** and closes it at the end. Round 1 prompts with the context written to `answers/<slug>-context.md`; later rounds prompt the *same* process with that round's feedback, which is the same conversation. There is no session id to discover, pass, or lose: the previous `opencode session list` diffing, title matching, `_claimed_sessions` bookkeeping and fresh-session fallback are all gone.

   **The context file carries only what the session doesn't already have** (`write_context`): the question, one line per PDF (bare filename + page count — the tools run in the CWD, so no absolute path is needed), and prior feedback. It deliberately does *not* repeat `citation-searcher.md`'s body, which pi already loads via `--system-prompt`. Past answers are offered as a ranked shortlist, not a directory listing: `relevant_answer_files()` scores each `answers/*.yml`'s own `question:` field by content-word overlap with the current question, dedupes by question text so `-N` re-asks collapse to one entry, and keeps the top 10 as one line each. `build_feedback_message()` sends **only the round just finished** — the one `PiSession` holds every earlier round verbatim. `write_context()` still assembles the full `group_feedback_by_round()` history, but with the fresh-session fallback gone it is only ever used for round 1, where `rounds` is empty; the history handling is now dead weight kept for the file it writes.
3. **Three checkers per round**, each failure appended to `rounds` feedback and triggering a retry:
   - **Deterministic** (`verify-citations.py`): every citation `text` must be at least 200 chars (`MIN_CITATION_CHARS`) and appear on the stated page of the cached extraction, tried through a ladder of increasingly lenient matches (exact → whitespace-normalized → case-insensitive → hyphen-stripped → arrow-normalized → punctuation-stripped); a quote spanning a page break passes when ≥20 chars of it match the stated page and the remainder matches the adjacent page (headers/footers sit between the halves, so joined page text can't match). On failure it reports closest-match suggestions and a found-on-page-X hint when the quote lives on a different page. Its table lists **only failing citations** plus the totals — that output is both the retry feedback and the browser's verifier-output block (which `run.html` renders only on failure), and a passing row gives the agent nothing to act on; `--show-passes` restores the full table for humans.
   - **Semantic** (per claim in order, with all preceding claims passed as trusted premises; stops at the first failing claim and returns to the searcher without checking the rest) and **coherence** (all claims joined into one paragraph): rubric prompts sent by `run_checker()`, which builds a **fresh, tool-less `PiSession` per check** (`tools=[]` means `--tools` is omitted entirely, so the checker judges the text it is given and cannot go consult the sources). Pass requires "PASS" and no "FAIL" in the output. The rubrics live inline in `based-answers.py`.

     A checker that fails to run reports the error **into its own output** rather than returning empty — empty contains no "FAIL" and would be read as a pass, which is the worst failure mode this pipeline has. `run_checker()` must also build its session with `pi_env()`: the API key is read from the Keychain, so it is absent from `os.environ` and is *not* inherited by a subprocess. A hand-written env dict there leaves every check unauthenticated while the searcher keeps working, so the pipeline looks healthy and silently verifies nothing.
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

A `search_result` citation may also set `mode: regex`, for claims that rule out a whole *family* of possible names rather than one literal phrase (e.g. "no `pio_sm_*` function reads back enabled state") — a handful of guessed literal queries can never be exhaustive. In `mode: regex`, `query` is a full Python regex and each result is `{match, page, text}` — `pdf_search.find_distinct_matches()` (agent-facing as the `search_regex` tool action) enumerates every *distinct* string the pattern matches anywhere in the doc, deduplicated, capped (currently 100, tunable) so a too-broad pattern fails loudly rather than silently truncating (a truncated enumeration could hide the exact symbol that disproves the claim). Its per-match snippets are half the width of a normal search's (150 vs 300 chars): 100 of them are carried twice, once in the tool result and again in the YAML the `ENUMERATION` block quotes back. `verify-citations.py`'s regex branch checks each claimed result's `text` is on-page and its `match` is actually produced by re-running the pattern against that same (normalized) text, then requires the claimed match-string *set* to exactly equal an independent rerun's. The rubric's `ENUMERATION` block shows the checker the complete, verified match list and asks it to judge whether that list (plus SOURCE_TEXTS) supports CLAIM, and whether the pattern itself covers the right family — ordinary LLM judgment, same as the rest of this checker's reasoning.

### Key couplings to keep in sync

- A run's filesystem slug is always its `run_id` (the possibly `-N`-suffixed id `PipelineServer.create_run()` assigns once, up front, to dedupe re-asks of an identical question) — `run_worker()` never re-derives a slug from the question text. `open_search_session()` passes that slug to the pi subprocess via `pi_env()`'s `ANSWER_SLUG`; `tools/write-answer.ts` reads it from `process.env` and unconditionally overwrites `answers/<slug>.yml` — the searcher agent itself never sees or handles a slug, and the tool never derives its own or bumps `-N`, so a run's answer file never sprawls across rounds. `find_yaml()` still tolerates a `<slug>-N.yml` match (max by mtime) for legacy multi-file directories predating this, but a live run only ever produces the exact `<slug>.yml`. `derive_slug()` is used only to seed `create_run()`'s dedup check and as the env-var fallback if `run_id` is ever absent.
- The agent's permissions are set **at the call site**, not in frontmatter: `--no-builtin-tools` plus `--tools` in `build_command()` is the whole allowlist. `citation-searcher.md` is now only a system prompt — any tool/permission frontmatter left in it is inert. (The old pipeline counted denied-permission lines in agent output for retry diagnostics; with no built-in tools to deny, there is nothing to count.)
- `pdf-search.py`'s cache format (`chunks` with `page`/`text`, plus lazily-added `pages_data` with span bboxes) is consumed by `verify-citations.py` and `format-answers.py` directly.
- `answer-body.html` is the shared answer markup: included by `answer-template.html` (static pages via `format-answers.py`, which remains a standalone CLI renderer writing `answer-pages/<slug>.html`) and rendered as the `answer` event fragment by `PipelineServer.render_answer_fragment()`. Refs carry a `pdf_url` (`file://…` for static output, `/pdf/<basename>` in the app) — `build_context(yaml_path, pdf_url_for=...)` controls it.
- `run.html`'s status bar takes `max_rounds` from `MAX_ROUNDS` in `based-answers.py` (via the server); never hardcode the round count in templates.
