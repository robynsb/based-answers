#!/usr/bin/env python3
"""
Deterministic citation verifier for citation-grounded QA.

Checks that every citation text appears verbatim in the source PDF's
extracted text. Uses string matching — no LLM involved.

Usage:
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml

YAML input format:
  question: "..."
  answers:
    - claim: "..."
      citations:
        - text: "verbatim quote..."
          page: 12
          source: "paper.pdf"
"""

import argparse
import difflib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path


def _load_pdf_search_module():
    """Import pdf-search.py by file path: this script's cwd is the working
    directory (where the PDFs/cache live), not the skill dir, so a plain
    `import pdf_search` won't resolve — the module must be found relative to
    this file's own location instead."""
    path = Path(__file__).parent / "pdf-search.py"
    spec = importlib.util.spec_from_file_location("pdf_search", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pdf_search = _load_pdf_search_module()


def load_yaml(path: str) -> dict:
    try:
        import yaml as _yaml
        with open(path) as f:
            return _yaml.safe_load(f)
    except ImportError:
        print("PyYAML not available. Trying manual parse...", file=sys.stderr)
        return _parse_yaml_simple(path)


def _parse_yaml_simple(path: str) -> dict:
    content = Path(path).read_text()
    result = {"question": "", "answers": []}
    current_answer = None
    current_citation = None
    in_citations = False

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("question:"):
            result["question"] = stripped.split(":", 1)[1].strip().strip("\"'")
        elif stripped == "- claim:" or stripped.startswith("- claim:"):
            if current_answer:
                result["answers"].append(current_answer)
            current_answer = {"claim": "", "citations": []}
            in_citations = False
            if ":" in stripped:
                current_answer["claim"] = stripped.split(":", 1)[1].strip().strip("\"'")
        elif current_answer and stripped.startswith("claim:"):
            current_answer["claim"] = stripped.split(":", 1)[1].strip().strip("\"'")
        elif current_answer and stripped == "citations:":
            in_citations = True
        elif current_answer and in_citations and stripped.startswith("- text:"):
            current_citation = {"text": stripped.split(":", 1)[1].strip().strip("\"'")}
        elif current_citation and stripped.startswith("page:"):
            current_citation["page"] = int(stripped.split(":", 1)[1].strip())
        elif current_citation and stripped.startswith("source:"):
            current_citation["source"] = stripped.split(":", 1)[1].strip().strip("\"'")
            current_answer["citations"].append(current_citation)
            current_citation = None
        elif current_answer and in_citations and stripped.startswith("-"):
            if current_citation:
                current_answer["citations"].append(current_citation)
                current_citation = None
            current_citation = {"text": stripped.lstrip("-").strip().strip("\"'")}

    if current_answer:
        result["answers"].append(current_answer)

    return result


def load_pdf_cache(source_name: str) -> dict:
    cache = os.path.join(os.getcwd(), "indexed-pdfs", source_name + ".json")
    if not os.path.exists(cache):
        return None
    with open(cache) as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_closest_matches(norm_citation: str, chunks_on_page: list, n: int = 3, cutoff: float = 0.3) -> list:
    """Find the closest-matching text spans for a failed citation using sliding windows."""
    if not norm_citation:
        return []
    citation_len = len(norm_citation)
    window_size = max(citation_len * 2, citation_len + 100)
    best = []
    seen = set()
    for chunk in chunks_on_page:
        norm_chunk = normalize_text(chunk["text"])
        if not norm_chunk:
            continue
        # Slide a window through the chunk, comparing to the citation
        step = max(1, window_size // 4)
        for start in range(0, len(norm_chunk), step):
            window = norm_chunk[start:start + window_size]
            if not window or window in seen:
                continue
            seen.add(window)
            ratio = difflib.SequenceMatcher(None, norm_citation, window).ratio()
            if ratio >= cutoff:
                # Store the original (non-normalized) text for display
                orig_start = max(0, start - 10)
                orig_text = chunk["text"][orig_start:orig_start + window_size + 20]
                best.append((ratio, orig_text))
    best.sort(key=lambda x: -x[0])
    # Deduplicate by keeping only the best entry per similarity bucket
    unique = []
    seen_ratios = set()
    for ratio, txt in best:
        bucket = round(ratio, 2)
        if bucket not in seen_ratios:
            seen_ratios.add(bucket)
            unique.append((ratio, txt))
    return unique[:n]


def _fold_typography(t: str) -> str:
    for a, b in [("‘", "'"), ("’", "'"), ("‚", "'"), ("‛", "'"),
                 ("“", '"'), ("”", '"'), ("„", '"'),
                 ("–", "-"), ("—", "-"), ("−", "-"), (" ", " "),
                 ("ﬀ", "ff"), ("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬃ", "ffi"), ("ﬄ", "ffl")]:
        t = t.replace(a, b)
    return t


def _norm_arrows(t: str) -> str:
    for a, u in [("->", "→"), ("<-", "←"), ("=>", "⇒"), ("<=", "⇐")]:
        t = t.replace(a, u)
    return t


def _strip_artifacts(t: str) -> str:
    return re.sub(r"[/\\–—.,;:!?'\"‘’“”()\[\]{}<>→←⇒⇐\s]", "", t)


def _match_in_text(citation_text: str, page_text: str) -> str | None:
    """Run the leniency ladder; return the name of the first rung that matches."""
    # Check 1: exact substring match
    if citation_text in page_text:
        return "exact"

    # Check 2: normalized (collapse whitespace)
    norm_citation = normalize_text(citation_text)
    norm_page = normalize_text(page_text)
    if norm_citation in norm_page:
        return "normalized"

    # Check 3: normalized + case-insensitive
    if norm_citation.lower() in norm_page.lower():
        return "normalized_case_insensitive"

    # Check 4: fold typographic characters (curly quotes/dashes, nbsp, ligatures)
    # so "instruction's" matches the PDF's "instruction’s"
    folded_citation = _fold_typography(norm_citation).lower()
    folded_page = _fold_typography(norm_page).lower()
    if folded_citation in folded_page:
        return "typography_folded"

    # Check 5: remove hyphens (handles "sea-level" vs "sealevel" after dehyphenation)
    nohyphen_citation = folded_citation.replace("-", "")
    nohyphen_page = folded_page.replace("-", "")
    if nohyphen_citation in nohyphen_page:
        return "hyphen_normalized"

    # Check 6: normalize ASCII arrows to Unicode
    arrow_citation = _norm_arrows(folded_citation)
    arrow_page = _norm_arrows(folded_page)
    if arrow_citation in arrow_page:
        return "arrow_normalized"

    # Check 7: strip all common citation-inconsequential characters and spaces
    artifact_citation = _strip_artifacts(arrow_citation)
    artifact_page = _strip_artifacts(arrow_page)
    if artifact_citation in artifact_page:
        return "artifact_stripped"

    return None


def _page_text(cache: dict, page: int) -> str | None:
    chunks = [c["text"] for c in cache["chunks"] if c["page"] == page]
    return "\n".join(chunks) if chunks else None


# A cross-page match must put at least this many normalized characters of the
# quote on the stated page, so a trivial shared word can't validate a wrong page
MIN_CROSS_PAGE_CHARS = 20

# Every citation must quote at least this many characters, so claims are backed
# by full passages with surrounding context rather than bare snippets
MIN_CITATION_CHARS = 200
# Headroom over the minimum when offering a widened quote, so the agent isn't
# handed something that lands one character above the threshold.
SHORT_CITATION_MARGIN = 120


def _split_match(citation_text: str, first_text: str, second_text: str,
                 max_prefix_words: int | None = None) -> tuple[str, str] | None:
    """Match a quote that runs across a page break: the longest word-boundary
    prefix found in first_text, with the remainder found in second_text.

    Prefix matching is monotone (every word-boundary prefix of a matching
    prefix also matches), so the longest matching prefix is found by binary
    search; and because a shorter remainder is a substring of a longer one,
    only the split at that longest prefix needs checking.
    """
    words = list(re.finditer(r"\S+", citation_text))
    hi = len(words) - 1  # the remainder must be non-empty
    if max_prefix_words is not None:
        hi = min(hi, max_prefix_words)
    if hi < 1:
        return None
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _match_in_text(citation_text[:words[mid - 1].end()], first_text) is not None:
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        return None
    prefix = citation_text[:words[lo - 1].end()]
    suffix = citation_text[words[lo].start():]
    if _match_in_text(suffix, second_text) is None:
        return None
    return prefix, suffix


def check_spans(cit: dict, cache: dict) -> dict:
    """Verify a citation that names its lines instead of reproducing them.

    `resolve-answer.py` materialises the text from these spans, so this is a
    re-resolution rather than a search: it confirms the file on disk still
    says what the spans address, and nothing else. There is no leniency
    ladder here and no cross-page special case — a passage interrupted by a
    running header is simply two spans that skip it, and the text either
    resolves identically or the file has been edited out from under the
    extraction cache.
    """
    spans = cit.get("spans")
    if not isinstance(spans, list) or not spans:
        return {"found": False, "reason": "citation's 'spans' must be a non-empty list"}

    parts = []
    for span in spans:
        if not isinstance(span, dict) or not all(
                isinstance(span.get(k), int) for k in ("page", "from", "to")):
            return {"found": False,
                    "reason": f"each span needs integer 'page', 'from' and 'to': got {span!r}"}
        try:
            parts.append(_pdf_search.resolve_span(
                cache, span["page"], span["from"], span["to"]))
        except ValueError as e:
            return {"found": False, "reason": f"span {span!r}: {e}"}

    resolved = "\n".join(parts)
    if resolved != cit.get("text"):
        return {"found": False,
                "reason": "citation text no longer matches the lines it cites — "
                          "the answer file was edited by hand, or the source was "
                          "re-indexed; re-cite the passage"}

    if cit.get("page") != spans[0]["page"]:
        return {"found": False,
                "reason": f"citation's page {cit.get('page')} is not where its "
                          f"first span starts (page {spans[0]['page']})"}

    return {"found": True, "method": "spans"}


def check_citation(citation_text: str, page: int, cache: dict) -> dict:
    chunks_on_page = [c for c in cache["chunks"] if c["page"] == page]

    if not chunks_on_page:
        return {"found": False, "reason": f"No text extracted for page {page}"}

    page_text = "\n".join(c["text"] for c in chunks_on_page)

    method = _match_in_text(citation_text, page_text)
    if method is not None:
        return {"found": True, "method": method}

    # The quote may run across a page break (running headers/footers sit
    # between the halves in the extraction, so joined page text can't match):
    # accept a substantial start on the stated page continuing on the next
    next_text = _page_text(cache, page + 1)
    if next_text is not None:
        split = _split_match(citation_text, page_text, next_text)
        if split and len(normalize_text(split[0])) >= MIN_CROSS_PAGE_CHARS:
            return {"found": True, "method": "cross_page",
                    "pages": [page, page + 1]}

    # ...or a quote that started on the previous page and ends on this one
    prev_text = _page_text(cache, page - 1)
    if prev_text is not None:
        words = list(re.finditer(r"\S+", citation_text))
        # cap the prefix so the stated page keeps the minimum share of the quote
        max_prefix = None
        for k in range(len(words) - 1, 0, -1):
            if len(normalize_text(citation_text[words[k].start():])) >= MIN_CROSS_PAGE_CHARS:
                max_prefix = k
                break
        if max_prefix is not None:
            split = _split_match(citation_text, prev_text, page_text,
                                 max_prefix_words=max_prefix)
            if split:
                return {"found": True, "method": "cross_page",
                        "pages": [page - 1, page]}

    # Not on the stated page at all — say so if it appears on another page
    for other in sorted({c["page"] for c in cache["chunks"]}):
        if other == page:
            continue
        other_text = _page_text(cache, other)
        if other_text and _match_in_text(citation_text, other_text) is not None:
            return {"found": False,
                    "reason": f"Citation text not found on page {page}, "
                              f"but it appears on page {other} — fix the page number"}

    # Fallback: find closest-matching chunks to help the user debug
    norm_citation = normalize_text(citation_text)
    suggestions = _find_closest_matches(norm_citation, chunks_on_page)
    reason = "Citation text not found on specified page"
    if suggestions:
        parts = []
        for sim, txt in suggestions:
            preview = txt[:120].replace("\n", " ")
            parts.append(f"{preview} (sim: {sim:.2f})")
        reason += ". Closest matches:\n  " + "\n  ".join(parts)
    return {"found": False, "reason": reason, "suggestions": suggestions}


SUGGESTION_CHARS = 500


def _expand_verbatim(text: str, page: int, cache: dict, target: int) -> str | None:
    """The passage on `page` containing `text`, widened to about `target`
    characters — or None if `text` isn't locatable there.

    A citation that is on the page but too short is the one failure the agent
    can't reason its way out of: it has the right passage and no way to know
    which direction to grow it, so it guesses, re-runs, and guesses again.
    The surrounding text is right there in the extraction.
    """
    norm_needle, _ = _pdf_search.normalize_for_match(text)
    norm_needle = norm_needle.strip()
    if not norm_needle:
        return None
    for chunk in (c for c in cache["chunks"] if c["page"] == page):
        norm_chunk, idx_map = _pdf_search.normalize_for_match(chunk["text"])
        at = norm_chunk.find(norm_needle)
        if at == -1:
            at = norm_chunk.lower().find(norm_needle.lower())
        if at == -1:
            continue
        start = idx_map[at]
        end = idx_map[min(at + len(norm_needle), len(idx_map)) - 1] + 1
        pad = max(0, target - (end - start))
        # Grow both ways, then spend whatever one side couldn't take on the other
        left = max(0, start - pad // 2)
        right = min(len(chunk["text"]), end + (pad - (start - left)))
        left = max(0, left - (pad - (start - left) - (right - end)))
        return chunk["text"][left:right]
    return None


def _verbatim_help(text: str, page: int, cache: dict) -> str:
    """What to say when a snippet isn't on its page: where it actually is, or
    the real text to copy.

    "copy the snippet verbatim from pdf_search's output" is true and useless
    — the agent believes it did, and cannot see how its copy differs. The
    match ladder is lenient about whitespace, case, typography and
    punctuation, so a failure here means the *content* diverges: a word
    dropped, two results spliced, or a page number off by one. Run …-14
    spent four verify calls on a single snippet with nothing but that
    sentence to go on. Handing back the real text, verbatim and long enough
    to paste, replaces the guessing with a copy.
    """
    for other in sorted({c["page"] for c in cache["chunks"]}):
        if other == page:
            continue
        other_text = _page_text(cache, other)
        if other_text and _match_in_text(text, other_text) is not None:
            return (f" — it does appear on page {other}, so the text is right and the page "
                    f"number is wrong: use {other}")

    chunks_on_page = [c for c in cache["chunks"] if c["page"] == page]
    if not chunks_on_page:
        return f" — no text was extracted for page {page} at all; check the page number"

    closest = _find_closest_matches(normalize_text(text), chunks_on_page, n=1)
    if not closest:
        return (f" — and nothing on page {page} resembles it; re-run the search and copy a "
                f"snippet from its output")
    sim, actual = closest[0]
    excerpt = actual[:SUGGESTION_CHARS]
    return (f" — the closest text on page {page} (similarity {sim:.2f}) is, verbatim between "
            f"the markers:\n>>>{excerpt}<<<\nUse that text as the snippet. Whitespace, case, "
            f"typography and punctuation are already matched leniently, so what differs is "
            f"the wording itself — do not retype it")


def check_search_result(cit: dict) -> dict:
    """Verify a search_result citation. `mode: regex` citations enumerate
    every distinct string a regex matches anywhere in the source (proving
    absence/exhaustiveness across a whole family of names); anything else
    is the original literal-query mode (one result per literal hit)."""
    if cit.get("mode") == "regex":
        return _check_search_result_regex(cit)
    return _check_search_result_literal(cit)


def _check_search_result_literal(cit: dict) -> dict:
    """Verify a literal-query search_result citation two ways:

    1. Each claimed result's `text` must actually appear on its stated
       `page` (via the same leniency ladder as normal citations) — otherwise
       an agent could pair a real hit page with a fabricated snippet, which
       would corrupt both the semantic checker's judgment and the quote
       shown to the user.
    2. The *set* of claimed pages must exactly match the set of pages an
       independent rerun of `query` actually hits (unbounded — no `limit`).
       Set (not multiset) equality, so a page containing two matching
       chunks doesn't spuriously fail when the agent lists that page once.
       Exact equality catches both a fabricated page (claimed, not found)
       and a dropped page (found, not claimed) — the latter being the
       failure mode that would let an agent quietly break an exhaustiveness
       claim by omitting an inconvenient result.
    """
    query = cit.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"found": False, "reason": "search_result citation missing a non-empty 'query'"}

    results = cit.get("results")
    if not isinstance(results, list):
        return {"found": False, "reason": "search_result citation's 'results' must be a list"}

    for r in results:
        if not isinstance(r, dict) or not isinstance(r.get("page"), int) \
                or not isinstance(r.get("text"), str) or not r.get("text").strip():
            return {"found": False,
                    "reason": f"each result needs an int 'page' and non-empty 'text': got {r!r}"}

    source = cit.get("source", "")
    cache = load_pdf_cache(source)
    if cache is None:
        return {"found": False, "reason": f"Cache not found for {source}"}

    for r in results:
        page_text = _page_text(cache, r["page"])
        if page_text is None or _match_in_text(r["text"], page_text) is None:
            return {"found": False,
                    "reason": f'result text for page {r["page"]} not found on that page'
                              + _verbatim_help(r["text"], r["page"], cache)}

    actual = _pdf_search.find_matches(cache, query)
    claimed_pages = {r["page"] for r in results}
    actual_pages = {m["page"] for m in actual}

    if claimed_pages == actual_pages:
        return {"found": True}

    fabricated = sorted(claimed_pages - actual_pages)
    omitted = sorted(actual_pages - claimed_pages)
    parts = [f're-running query "{query}" against {source} does not match the claimed results']
    if fabricated:
        parts.append(f"claimed but not actually found on: page(s) {fabricated}")
    if omitted:
        parts.append(f"actually found but omitted from results: page(s) {omitted}")
    return {"found": False, "reason": " -- ".join(parts)}


def _digit_permissive_variant(pattern: str) -> str | None:
    """Return `pattern` with `0-9` added to every character class that cannot
    already match a digit, or None if there is no such class.

    An enumeration pattern written as an identifier family — say
    `pio_sm_set_[a-z][a-z_]*` — silently gets the wrong answer on any name
    with a digit in it. `pio_sm_set_pindirs_with_mask64` either comes back
    truncated to `...with_mask` or, if the class sits before a required
    suffix like `\\s*\\(`, drops out of the enumeration entirely. Neither
    shows up in the claimed-vs-actual set comparison, because the rerun that
    comparison uses is the same crippled pattern: both sides agree, and an
    exhaustiveness claim built on a list that is missing exactly the name
    that would refute it verifies clean.

    Repairing the classes and re-enumerating turns that into evidence: if the
    permissive pattern finds something the original could not, the original's
    list was never exhaustive.
    """
    out = []
    changed = False
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if ch != "[":
            out.append(ch)
            i += 1
            continue
        # Scan one character class, honouring escapes and a leading ]/^]
        j = i + 1
        if j < n and pattern[j] == "^":
            j += 1
        if j < n and pattern[j] == "]":
            j += 1
        while j < n and pattern[j] != "]":
            j += 2 if pattern[j] == "\\" else 1
        if j >= n:                       # unterminated — leave it to re.compile
            out.append(pattern[i:])
            break
        body = pattern[i + 1:j]
        negated = body.startswith("^")
        has_digits = (negated
                      or "0-9" in body
                      or r"\d" in body
                      or r"\w" in body
                      or any(c.isdigit() for c in body))
        if has_digits:
            out.append(pattern[i:j + 1])
        else:
            out.append(f"[{body}0-9]")
            changed = True
        i = j + 1
    return "".join(out) if changed else None


def _check_enumeration_covers_digits(pattern: str, cache: dict,
                                     actual_matches: set[str]) -> dict | None:
    """Fail an enumeration whose character classes exclude digits when that
    exclusion demonstrably changes the match set. Returns a failure dict, or
    None when the pattern is fine (or the question can't be settled)."""
    permissive = _digit_permissive_variant(pattern)
    if permissive is None:
        return None
    regen = _pdf_search.find_distinct_matches(cache, permissive)
    if regen.get("error"):
        # A repaired pattern that is invalid or too broad proves nothing about
        # the original; stay silent rather than fail on an artefact of repair.
        return None
    missed = sorted({m["match"] for m in regen["matches"]} - actual_matches)
    # Matches the original truncated mid-name are the interesting half: report
    # them first, since they read as "found it" while hiding the real symbol.
    truncations = [m for m in missed if any(m.startswith(a) for a in actual_matches)]
    if not missed:
        return None
    parts = [f"pattern {pattern!r} has a character class that cannot match digits, and that "
             f"changes the result: {permissive!r} finds {len(missed)} string(s) it misses"]
    if truncations:
        parts.append(f"names the original truncated or dropped: {truncations}")
    parts.append(f"missing from the enumeration: {missed}")
    parts.append("an enumeration used for exhaustiveness must be able to match digits "
                 "(e.g. [a-z0-9_] rather than [a-z_])")
    return {"found": False, "reason": " -- ".join(parts)}


# The predicate lives in pdf-search.py so the search tool can warn about a
# degenerate pattern at the point the agent writes it, not just here.
_spelled_out_literals = _pdf_search.spelled_out_literals


def _check_enumeration_is_general(pattern: str, actual_matches: set[str]) -> dict | None:
    """Fail an enumeration whose pattern can only match the literals it names.

    Like the digit check, this fires on evidence: a pattern that spells
    everything out but finds nothing has ruled out those literals and made no
    exhaustiveness argument at all, so it is left alone. One that finds them
    is being used to close a family it could never have opened.
    """
    if not actual_matches:
        return None
    literals = _spelled_out_literals(pattern)
    if literals is None:
        return None
    return {"found": False,
            "reason": f"pattern {pattern!r} has no character class, quantifier or wildcard -- "
                      f"it can only match the {len(literals)} literal string(s) it spells out, "
                      f"so finding them proves nothing about what else exists -- "
                      f"an enumeration must be able to match names it does not already name; "
                      f"widen the pattern to cover the family (a class and a quantifier around "
                      f"the part that varies), or cite these as ordinary search_result queries"}


def _check_search_result_regex(cit: dict) -> dict:
    """Verify a mode: regex search_result citation, which enumerates every
    DISTINCT string a regex pattern matches anywhere in the source (for
    proving absence/exhaustiveness across a whole family of possible names,
    e.g. "no pio_sm_* getter reads enabled state") instead of one result per
    literal query hit.

    1. Each claimed result's `text` must appear verbatim on its stated
       `page`, same as the literal mode.
    2. Each claimed `match` must actually be produced by re-running `query`
       (the pattern) against that same `text` — checked on the same
       normalized (typography-folded) form find_distinct_matches used to
       derive `match` in the first place, so folding differences can't
       cause a spurious mismatch across a fold boundary.
    3. The *set* of claimed `match` strings must exactly equal the set an
       independent, unbounded rerun of the pattern actually finds across
       the whole document — catches both a fabricated symbol and one
       dropped to make an exhaustiveness claim look cleaner than it is.
    4. The pattern must be able to match something it does not already spell
       out (_check_enumeration_is_general), and must be able to match digits
       wherever it uses a character class if that changes what it finds
       (_check_enumeration_covers_digits). Both catch the blind spot rules 2
       and 3 share: a crippled pattern rerun against itself always agrees.
    """
    pattern = cit.get("query")
    if not isinstance(pattern, str) or not pattern.strip():
        return {"found": False,
                "reason": "search_result citation missing a non-empty 'query' (regex pattern)"}

    results = cit.get("results")
    if not isinstance(results, list):
        return {"found": False, "reason": "search_result citation's 'results' must be a list"}

    for r in results:
        if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not r.get("match").strip() \
                or not isinstance(r.get("page"), int) \
                or not isinstance(r.get("text"), str) or not r.get("text").strip():
            return {"found": False,
                    "reason": f"each regex-mode result needs a non-empty 'match', int 'page', "
                              f"and non-empty 'text': got {r!r}"}

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"found": False, "reason": f"invalid regex pattern {pattern!r}: {e}"}

    source = cit.get("source", "")
    cache = load_pdf_cache(source)
    if cache is None:
        return {"found": False, "reason": f"Cache not found for {source}"}

    for r in results:
        page_text = _page_text(cache, r["page"])
        if page_text is None or _match_in_text(r["text"], page_text) is None:
            return {"found": False,
                    "reason": f'result text for page {r["page"]} not found on that page'
                              + _verbatim_help(r["text"], r["page"], cache)}
        norm_text, _ = _pdf_search.normalize_for_match(r["text"])
        found_in_snippet = {m.group(0) for m in compiled.finditer(norm_text)}
        if r["match"] not in found_in_snippet:
            return {"found": False,
                    "reason": f'claimed match "{r["match"]}" is not actually produced by pattern '
                              f'{pattern!r} within its own result text'}

    regen = _pdf_search.find_distinct_matches(cache, pattern)
    if regen.get("error") == "invalid_pattern":
        return {"found": False, "reason": f"invalid regex pattern {pattern!r}: {regen['detail']}"}
    if regen.get("error") == "too_broad":
        return {"found": False,
                "reason": f"pattern {pattern!r} matches {regen['count']}+ distinct strings — "
                          f"exhaustiveness isn't checkable at this scale, narrow the pattern"}

    claimed_matches = {r["match"] for r in results}
    actual_matches = {m["match"] for m in regen["matches"]}

    if claimed_matches == actual_matches:
        return (_check_enumeration_is_general(pattern, actual_matches)
                or _check_enumeration_covers_digits(pattern, cache, actual_matches)
                or {"found": True})

    fabricated = sorted(claimed_matches - actual_matches)
    omitted = sorted(actual_matches - claimed_matches)
    parts = [f're-running pattern {pattern!r} against {source} does not match the claimed results']
    if fabricated:
        parts.append(f"claimed but not actually found: {fabricated}")
    if omitted:
        parts.append(f"actually found but omitted from results: {omitted}")
    return {"found": False, "reason": " -- ".join(parts)}


GUESS_LIST_MIN = 3
ADVISORY_PREFIX = "ADVISORY: "


def guess_list_advisory(answer: dict) -> str | None:
    """Flag a claim whose entire evidence is literal searches that found
    nothing — "I guessed N names and none existed, therefore no name exists".

    That argument does not get stronger with N, and the semantic checker
    responds to it by naming an N+1th thing to try, which is a different
    objection every round: the searcher patches the list instead of changing
    method, and three rounds go by. A fixed message, delivered the first time
    the shape appears, is worth more than a better objection later.

    Advisory rather than a failure on purpose. The rule reads only the shape
    of the argument, never what the queries say, so it cannot tell a genuinely
    exhaustive handful of probes from a hopeful one — and the checker, which
    can, still has the final say.
    """
    citations = answer.get("citations") or []
    if len(citations) < GUESS_LIST_MIN:
        return None
    for c in citations:
        if (not isinstance(c, dict) or c.get("type") != "search_result"
                or c.get("mode") == "regex" or (c.get("results") or [])):
            return None
    return (f"this claim's only evidence is {len(citations)} literal searches that each found "
            f"nothing. An absence claim cannot be established by listing queries that returned "
            f"no results — there is always another spelling. Use a mode: regex citation to "
            f"enumerate what the source does contain, or cite a passage that states the absence.")


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic citation verifier"
    )
    parser.add_argument("yaml", help="YAML file with claims and citations")
    parser.add_argument("--pdf-dir", default=".",
                        help="Directory containing PDFs and cache files "
                             "(default: current working directory)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show match methods, closest-match hints, and detailed diffs")
    parser.add_argument("--show-passes", action="store_true",
                        help="List passing citations too (default: failures and the summary "
                             "only — a passing row tells the agent nothing it must act on)")
    parser.add_argument("--format", choices=["table", "json", "summary"],
                        default="table", help="Output format")

    args = parser.parse_args()
    data = load_yaml(args.yaml)

    total = 0
    passed = 0
    failed = 0
    results = []

    print(f"Question: {data.get('question', '(none)')}\n")

    # Printed lazily, so an all-passing run is just the question and the summary
    header_printed = False

    def print_header():
        nonlocal header_printed
        if header_printed:
            return
        print(f"{'Claim':<60} {'Citation':<50} {'Page':<6} {'Result':<10}")
        print("-" * 130)
        header_printed = True

    advisories = []

    for answer in data.get("answers", []):
        claim = answer.get("claim", "")
        note = guess_list_advisory(answer)
        if note:
            advisories.append((claim, note))
        for cit in answer.get("citations", []):
            total += 1
            is_search_result = cit.get("type") == "search_result"
            source = cit.get("source", "")

            result = None
            if is_search_result:
                result = check_search_result(cit)
                if result["found"]:
                    status = "PASS"
                    passed += 1
                else:
                    status = "FAIL"
                    reason = result.get("reason", "Not found")
                    failed += 1
                cit_text = f'query: "{cit.get("query", "")}" ({len(cit.get("results") or [])} result(s))'
                page = "n/a"
            else:
                cit_text = cit.get("text", "")
                page = cit.get("page", 0)
                cache = load_pdf_cache(source)

                if cache is not None and cit.get("spans"):
                    result = check_spans(cit, cache)
                    if result["found"]:
                        status = f"PASS ({result['method']})" if args.verbose else "PASS"
                        passed += 1
                    else:
                        status = "FAIL"
                        reason = result.get("reason", "Not found")
                        failed += 1
                elif len(cit_text) < MIN_CITATION_CHARS:
                    status = "FAIL"
                    reason = (f"Citation too short: {len(cit_text)} chars, minimum is "
                              f"{MIN_CITATION_CHARS}. Quote a longer contiguous passage "
                              f"around the supporting text")
                    wider = (_expand_verbatim(cit_text, page, cache,
                                              MIN_CITATION_CHARS + SHORT_CITATION_MARGIN)
                             if cache else None)
                    if wider and len(wider) >= MIN_CITATION_CHARS:
                        reason += (f" — the same quote with its surroundings, verbatim between "
                                   f"the markers:\n>>>{wider}<<<")
                    failed += 1
                elif cache is None:
                    status = "FAIL"
                    reason = f"Cache not found for {source}"
                    failed += 1
                else:
                    result = check_citation(cit_text, page, cache)
                    if result["found"]:
                        status = f"PASS ({result['method']})" if args.verbose else "PASS"
                        passed += 1
                    else:
                        status = "FAIL"
                        reason = result.get("reason", "Not found")
                        failed += 1

            short_claim = claim[:58] + ".." if len(claim) > 58 else claim
            short_cit = cit_text[:48] + ".." if len(cit_text) > 48 else cit_text

            if args.format == "table" and (status == "FAIL" or args.show_passes):
                print_header()
                extra = f" ({reason})" if status == "FAIL" else ""
                print(f"{short_claim:<60} {short_cit:<50} {page:<6} {status:<10}{extra}")
                if args.verbose and status == "FAIL" and result and result.get("suggestions"):
                    for sim, txt in result["suggestions"]:
                        preview = txt[:120].replace("\n", " ")
                        print(f"{'':>60} {'':>50} {'':>6}   Suggested: {preview} (sim: {sim:.2f})")
            elif args.format == "json":
                results.append({
                    "claim": claim,
                    "citation": cit_text,
                    "page": page,
                    "source": source,
                    "status": status,
                    "reason": reason if status == "FAIL" else None,
                })

    if args.format == "table":
        if header_printed:
            print(f"\n{'=' * 130}")
        print(f"Total: {total}  |  Passed: {passed}  |  Failed: {failed}")
        # Advisories never change the exit code — they are guidance carried
        # into the next round's feedback, not verdicts on the citations.
        for claim, note in advisories:
            short = claim[:70] + ".." if len(claim) > 70 else claim
            print(f'{ADVISORY_PREFIX}"{short}" — {note}')

    if args.format == "json":
        print(json.dumps(results, indent=2))
    elif args.format == "summary":
        print(f"{'=' * 60}")
        print(f"Total: {total}  |  Passed: {passed}  |  Failed: {failed}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
