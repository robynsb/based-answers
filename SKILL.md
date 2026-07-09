---
name: citation-grounded-qa
description: Use when answering questions from PDF sources (scientific papers, textbooks, datasheets) where every claim must cite a verbatim source passage and no world knowledge may be used. Use when you need a checker sub-agent to verify each claim against its citation. Use when saying "unable to answer" is a valid outcome.
---

# Citation-Grounded QA

## Overview

Answer questions exclusively from given PDF sources. Every claim must cite a verbatim passage with page number. No world knowledge. Answers are produced as structured YAML. A deterministic script verifies every citation text exists verbatim in the source PDF and that the `concatenation` field joins all claims exactly. Then checker sub-agents verify semantic implication. Then a final sub-agent checks the concatenation for coherence and completeness. Failures loop back to re-search. "Unable to answer" is a valid, first-class output.

## Setup

This skill ships with its own Nix flake (`flake.nix` in the same directory as this SKILL.md). Below, `SKILL_DIR` is a placeholder for the directory containing this `SKILL.md` file — substitute the actual path when running commands.

Place this skill in `.opencode/skills/` (project-level) or `~/.config/opencode/skills/` (global) for opencode to discover it automatically.

All commands use this pattern:
```
nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> search "<query>"
```

For example, if this file is at `.opencode/skills/citation-grounded-qa/SKILL.md`, use:
```
nix develop "path:.opencode/skills/citation-grounded-qa" -c python3 .opencode/skills/citation-grounded-qa/pdf-search.py paper.pdf search "climate"
```

## Workflow

### 0. Setup
- `mkdir -p answers`
- Derive `<slug>` from question (kebab-case)
- Check for prior-session file at that slug

### 1. Index
- Run `nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <source> info` for each source file
- Understand structure, estimate token budget

### 2. Search
- Run `nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <source> search "<term>"`
- Iterate across all sources for the question
- Check `answers/` for existing `.yml` files — reuse supported claims from prior sessions
- Vary search terms to cover all aspects
- → Returns matching paragraph chunks + pages

### 3. Retrieve
- Run `nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <source> get <pages>`
- Retrieve only pages with relevant matches
- → Full page text for answer formulation

### 4. Answer (as YAML)
- Write `answers/<slug>.yml` with claims + citations (see Rule 2 for structure)
- If file from prior session exists, use `<slug>-N.yml` instead (N = lowest free)

### 5. Deterministic Verification
- Run `nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml`
- Checks every citation text exists verbatim in the source PDF (string match, no LLM)
- Also checks concatenation is exact join of all claims with ". "
- Fails if any text not found on its page or concatenation is wrong

### 6. Semantic Verification
- For each claim with ALL its citations:
  - Use Task tool to dispatch checker sub-agent with: CLAIM, all SOURCE_TEXTS
  - Does the SYNTHESIS of all sources imply the CLAIM?
- If **ALL PASS** → proceed to step 7
- If **ANY FAIL** → return to step 2 (Search), max 3 rounds total

### 7. Coherence & Completeness Verification
- Dispatch sub-agent with QUESTION and CONCATENATION of all claims
  - Is the concatenation a sensible paragraph where concepts are established?
  - Does it totally answer the question?
- If **PASS** → proceed to step 8
- If **FAIL** → return to step 2 (Search), max 3 rounds total

### 8. Output
- Final answer as `answers/<slug>.yml` (verified)
- Or: "Unable to answer after 3 rounds."

## Core Rules

### Rule 1: No World Knowledge
Every fact in the answer must trace to a verbatim source quote with page number. If you know something from training data, you cannot use it unless the source says it.

### Rule 2: Answer File & Format
Every answer is written as `answers/<slug>.yml`. Derive the slug from the question: lowercase, replace non-alphanumeric characters (except spaces) with nothing, replace spaces with hyphens, collapse consecutive hyphens, and strip leading/trailing hyphens. Truncate to 80 chars if needed.

Before writing, check if `answers/<slug>.yml` already exists from a prior session. If it does and you did not create it in this session, use `answers/<slug>-2.yml`, `answers/<slug>-3.yml`, etc. (lowest free N). Within the same session, retries edit the same file in-place.

The YAML structure is:

```yaml
question: "What is the maximum clock speed of the RP2350?"
concatenation: "The dual Cortex-M33 or Hazard3 processors run at 150 MHz. The maximum system frequency is 150 MHz."
answers:
  - claim: "The dual Cortex-M33 or Hazard3 processors run at 150 MHz"
    citations:
      - text: "Dual Cortex-M33 or Hazard3 processors at 150 MHz"
        page: 14
        source: "RP-008373-DS-2-rp2350-datasheet.pdf"
  - claim: "The maximum system frequency is 150 MHz"
    citations:
      - text: "the maximum system frequency of 150 MHz"
        page: 90
        source: "RP-008373-DS-2-rp2350-datasheet.pdf"
```

If no answer is possible:
```yaml
question: "..."
concatenation: ""
answers: []
```

Rules for the YAML:
- `text` must be a verbatim quote from the source (exact characters)
- `page` is the page number where the text appears
- `source` is the PDF filename
- Each claim can have multiple citations
- `concatenation` is the exact concatenation of all claims joined with `". "` (period space). The deterministic verifier checks this.
- If no evidence exists, output `concatenation: ""` and an empty answers list

### Rule 3: Direct Logical Inference Only
You may infer direct consequences of source statements:
- ✅ "X > 10" and "Y = 2X" → "Y > 20" (arithmetic)
- ✅ "All A are B" and "X is A" → "X is B" (syllogism)
- ❌ "Patient has fever and cough" + domain knowledge → "Patient has flu"
- ❌ "The voltage is 5V" + knowledge of Ohm's law → inferring current without explicit R value

If an inference requires ANY external domain knowledge, it is banned.

### Rule 4: "Unable to Answer" Is Mandatory
When:
- No search results match the question
- Retrieved text does not support a full answer
- Sources conflict without resolution
- 3 retry rounds exhausted

Output as empty YAML:
```yaml
question: "Unable to answer this question given the sources."
answers: []
```

Do not pad with world knowledge. Do not add "but in general..." or "typically...".

### Rule 5: Surface Conflicts
If sources disagree, present both positions as separate claims with their citations.

Do not pick a side. Do not reconcile with world knowledge.

### Rule 6: Search Before Answer
Always run pdf-search.py search before reading full pages. Do not read entire PDFs. The search tool returns matching chunks — retrieve full pages only for matches.

### Rule 7: Citations Must Be Verbatim
The `text` field in YAML must be EXACTLY as it appears in the source — same characters, same punctuation. The deterministic verifier (`verify-citations.py`) will check this with string matching, not LLM judgement.

- ✅ Source: "Dual Cortex-M33 processors at 150 MHz" → text: "Dual Cortex-M33 processors at 150 MHz"
- ❌ Source: "Dual Cortex-M33 processors at 150 MHz" → text: "processors at 150 MHz" (partial — must match the exact span used)
- ❌ Source: "Dual Cortex-M33 processors at 150 MHz" → text: "Dual Cortex-M33 processors at 150 MHz." (trailing period not in source)

### Rule 8: Citations Must Support the Claim
The cited passage(s) must support what the claim says:
- A single passage directly asserting the claim is sufficient.
- Multiple passages can **together** support a claim through synthesis (e.g., source A says X, source B says Y, and X+Y implies the claim).
- ✅ Single: Source: "Processors run at 150 MHz" → Claim: "Processors run at 150 MHz"
- ✅ Synthesis: Source A: "Supply voltage 5V" + Source B: "Max current 500mA" → Claim: "Max power 2.5W" (5V × 0.5A = 2.5W, arithmetic inference)
- ❌ Source A: "flexible system clock up to 150 MHz" → Claim: "Processors run at 150 MHz" (system clock ≠ processor speed — no synthesis can fix a mismatch)

If no passage(s) support your claim, search for better passages or drop the claim.

### Rule 9: Answers Directory & Prior-Session Claims

Before any search step, ensure `answers/` exists (`mkdir -p answers`). During the search phase, read all `.yml` files in `answers/`. If any contain claims and citations that are relevant to the current question AND are supported by the current source PDFs, reuse them. This avoids re-doing work from prior sessions.

Never overwrite a file you did not create in the current session. Use the `-N` suffix scheme from Rule 2 to pick a non-conflicting name.

## Three-Stage Verification

This skill uses three independent verification stages:

### Stage 1: Deterministic Citation Verification (verify-citations.py)

Runs `verify-citations.py answers/<slug>.yml` — a Python script that performs exact string matching against the extracted PDF text. No LLM involved. This catches:
- Paraphrased citations (text doesn't match source exactly)
- Wrong page numbers
- Hallucinated quotes
- Incorrect `concatenation` field (must be exact join of all claims with `". "`)

Run: `nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml`

### Stage 2: Semantic Checker Sub-Agent

For each claim with ALL its associated citations that pass deterministic verification, dispatch a single checker sub-agent via the Task tool. The checker first evaluates whether any source text is too short or fragmentary to be reliably cited (out-of-context check), then evaluates whether the **synthesis** of all source texts implies the claim.

### Stage 3: Coherence & Completeness Sub-Agent

After all claims pass semantic verification, dispatch a single sub-agent with the **question** and the **concatenation** of all claims. The checker evaluates whether the concatenation forms a coherent, complete answer.

## Stage 2: Semantic Checker Sub-Agent Rubric

For every claim (with all its citations), dispatch a sub-agent with this instruction:

```
You are a claim verifier. Your job is to check whether a CLAIM is
strictly implied by the SYNTHESIS of all SOURCE_TEXTS provided.
You have NO other context.

CLAIM: <one sentence / atomic fact>
SOURCE_TEXTS:
  - <verbatim quote from source 1>
  - <verbatim quote from source 2>

PRELIMINARY OUT-OF-CONTEXT CHECK: Before assessing implication,
examine each SOURCE_TEXT. Every source must include three parts:
some text immediately before the claim-relevant portion, the text
directly related to the claim, and some text immediately after.
If any SOURCE_TEXT lacks surrounding context on both sides — if it
is just the bare snippet — reject it as taken out of context.

If all SOURCE_TEXTS pass the preliminary check, proceed:

Does the SYNTHESIS of all SOURCE_TEXTS strictly imply CLAIM?
- YES: Respond with "PASS"
- FAIL (out-of-context): Respond with "FAIL: Source text too short,
  taken out of context — <explain why>"
- NO: Respond with "FAIL: <explain what was added or cannot be inferred>"

Rules:
0. OUT-OF-CONTEXT CHECK (takes priority over all other rules): Every
   source must include text before, the claim-relevant portion, and
   text after. If any source is just the bare snippet without
   surrounding context on both sides, reject with FAIL. Do not
   proceed to implication checking.
1. Direct logical inference (arithmetic, boolean, set membership) is allowed.
2. Inference across sources is allowed: if source A says X and source B
   says Y, you may combine them to conclude X+Y implies the claim.
3. Any inference requiring external domain knowledge = FAIL.
4. If the SYNTHESIS of all sources does not imply the claim, it is a FAIL.
5. Do NOT use your own knowledge to fill gaps.
6. Do NOT assume the SOURCE_TEXTS are correct — only check implication.
7. A single source text containing the full claim is still PASS.
```

The Stage 2 checker sub-agent receives ONLY:
- The claim text
- All source quote texts (verbatim, no metadata)
- The rubric above

NOT the full PDF, NOT the question, NOT other claims.

## Stage 3: Coherence & Completeness Sub-Agent Rubric

After all claims pass semantic verification (Stage 2), dispatch a **single** sub-agent with this instruction:

```
You are a coherence and completeness verifier. Your job is to judge
whether the provided CONCATENATION of claims forms a sensible,
self-contained answer to the QUESTION.

QUESTION: <original question>
CONCATENATION: <concatenation of all claims>

Evaluate:
1. COHERENCE: Is the concatenation a sensible paragraph where concepts
   are established and explained, and each idea makes sense in its
   respective context?
2. COMPLETENESS: Does the concatenation totally answer the question?
   Is any information missing?

Respond with:
- PASS if BOTH coherence and completeness are satisfied
- FAIL: <explain what is missing, unclear, or doesn't make sense>
```

The Stage 3 sub-agent receives ONLY:
- The original question
- The concatenation of all claims
- The rubric above

NOT the individual claims, NOT the citations, NOT the PDFs.

## Retry Behavior

If Stage 3 returns FAIL, return to step 2 (Search) with feedback from the sub-agent, up to max 3 rounds total. If all 3 rounds are exhausted, output `answers: []`.

## Retry Loop

| Round | Action |
|-------|--------|
| 1 | Search → Answer (YAML) → Deterministic verify → Semantic verify → Coherence & completeness verify |
| 2 | Re-search with broader/narrower terms → Revise YAML → Verify → Coherence & completeness verify |
| 3 | Final attempt with different search strategy → Revise → Verify → Coherence & completeness verify |
| After 3 | Output `answers: []` |

## Rationalization Table

| Excuse | Reality |
|--------|---------|
| "I already know this, the PDF will confirm it" | Cite the actual quote or don't include it. Memory is not source. |
| "The source implies this with common sense" | If "common sense" = domain knowledge, it's banned. |
| "The citation is close enough" | verify-citations.py checks exact string match. "Close enough" = FAIL. |
| "I'll read the whole PDF to be safe" | You're burning tokens. Search first. Retrieve only matches. |
| "This fact is too basic to need a citation" | Every fact needs a citation. No exceptions. |
| "Unable to answer feels like failure" | It's the correct output when evidence is insufficient. |
| "I'll add a little context from my knowledge" | Adding any info from outside the source violates Rule 1. |
| "I described what the checker would do, that's enough" | verify-citations.py is deterministic — it must actually run. |
| "The system clock implies processor speed" | If the source doesn't directly say it, don't claim it. Find a direct citation or drop the claim. |
| "Each source alone doesn't imply it, but they could together" | The semantic verifier checks ALL your citations together. If the synthesis implies the claim, it passes. But if no single source covers it, you MUST have multiple citations that together cover it. |
| "This source implies part of the claim, I'll just assume the rest" | No. Every part of the claim must trace to a verbatim source. If source A covers half and source B covers the other half, include BOTH as citations. |

## Red Flags — STOP

- You're writing an answer before searching the PDF
- You're paraphrasing instead of quoting verbatim
- You think "everyone knows this" about a claim
- You want to "just add a bit of context"
- You're about to say "typically" or "in general" or "as is well known"
- You caught yourself not knowing which page a claim comes from
- You're considering reading the entire PDF rather than searching
- You're printing what the checker "would say" instead of dispatching it
- You haven't run verify-citations.py yet
- You forgot to add the `concatenation` field to the answer YAML
- The concatenation won't read as a coherent paragraph when claims are joined

Any of these → stop, search the PDF, find the exact quote, run verification.

## Tools Reference

### pdf-search.py

Substitute the actual path to this skill's directory for `SKILL_DIR`:

```
nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> info
  → Pages, chunks, estimated tokens

nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> search "<query>" [--limit N] [--context-chars C]
  → Matching paragraph chunks with page numbers
  → Default limit=10, context=300 chars around match

nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> get <page-num> [page-num...]
  → Full text of specified pages
```

Cache: extracted text is cached as `<file.pdf>.json`. Re-run to refresh.

### verify-citations.py

Deterministic citation verifier. Checks every citation text exists verbatim in the source PDF, and that the `concatenation` field is the exact join of all claims with `". "`.

```
nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml
  → PASS/FAIL for every (claim, citation) pair
  → PASS/FAIL for concatenation check
  → Exit code 0 = all pass, 1 = any fail

nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml --format json
  → Machine-readable JSON output
```

The verifier performs these checks:
1. Exact substring match on the specified page
2. Whitespace-normalized match (handles line breaks)
3. Normalized + case-insensitive match (last resort)
4. `concatenation` equals `". ".join(all claim strings)`

### format-answers.py

Generates a self-contained HTML page presenting the answer with styled citations,
PDF.js page previews (with highlighted text), and clickable links that open the
source PDF at the cited page. Writes to `answers/<slug>.html` and prints the absolute
path to stdout.

```
nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/format-answers.py answers/<slug>.yml
```

The output is a single absolute path to the HTML file &mdash; CMD+CLICK it to open.

The HTML page features:
- Auto dark/light mode
- Numbered claims with clickable citation superscripts
- Reference cards with verbatim quotes and source metadata
- PDF.js rendered page thumbnails with the cited text highlighted in yellow
- Fallback graceful degradation when PDF cache or PDF.js is unavailable

### Environment

This skill ships with its own `flake.nix` (same directory as this file). All dependencies (PyMuPDF for extraction, PyYAML for verification) are provided by the flake. No pip or system packages needed.

Do not reference `nixpkgs#` derivations directly. Always use `nix develop "path:SKILL_DIR"` (with the real directory substituted) to load the skill's own flake.

## Common Mistakes

1. **Answering from memory** — Always search the PDF first. Your training data is not a source.
2. **Paraphrasing citations** — verify-citations.py will catch this. Use verbatim quotes.
3. **Wrong page numbers** — verify-citations.py checks the citation against the stated page. Wrong page = FAIL.
4. **Trying to answer despite no matches** — "Unable to answer" is the correct output. Do not improvise.
5. **One big answer blob** — Split into atomic claim+citation pairs in YAML.
6. **Not varying search terms** — If first search returns nothing, try synonyms and related terms before giving up.
7. **Skipping deterministic verification** — You must run verify-citations.py before semantic checkers.
8. **Weak citations** — Citing something that "implies" or "suggests" the claim rather than directly stating it. The citation must DIRECTLY assert the claim (or combine with other citations to do so).
9. **Ignoring synthesis opportunity** — Each source alone doesn't cover the claim, but together they do. Always include ALL relevant citations for a claim so the semantic verifier can evaluate the synthesis.
10. **One citation for a multi-part claim** — If your claim has multiple assertions that come from different passages, include all those passages as separate citations under the same claim.
11. **Forgetting the concatenation field** — The `concatenation` at the top level must exactly equal all claims joined with `". "`. Forgetting it or getting it wrong = deterministic FAIL.
12. **Claims that don't form a coherent paragraph** — The concatenation of all claims must read as a sensible, self-contained answer. If claims jump between topics without establishing concepts, the Stage 3 verifier will FAIL and you'll need to revise.
13. **Not creating answers/ directory** — Run `mkdir -p answers` at the start. Writing to a non-existent directory will fail.
14. **Overwriting a prior-session answer file** — Check if `answers/<slug>.yml` exists before writing. If it does and you didn't create it this session, use `<slug>-N.yml`.
15. **Not reusing prior claims** — Check existing `answers/*.yml` files during search. If a prior answer has relevant, well-supported claims, reuse them instead of starting from scratch.
