#!/usr/bin/env python3
"""
Resolve a citation *specification* into this run's answer YAML.

The searcher agent does not write citation text. It names the evidence — a
span of numbered lines, a literal query, a regex pattern — and this script
materialises it from the same extracted-text cache the verifier reads. A
quote is therefore verbatim by construction, and a search_result's `results`
are a real rerun rather than a transcription the agent could get wrong.

Reads the spec as JSON on stdin:

  {"answers": [
     {"claim": "...",
      "citations": [
        {"type": "quote",  "source": "x.pdf", "spans": [{"page": 14, "from": 4, "to": 9}]},
        {"type": "search", "source": "x.pdf", "query": "plague"},
        {"type": "regex",  "source": "x.pdf", "pattern": "pio_sm_set_[a-z0-9_]*"}
      ]}
  ]}

Writes answers/<slug>.yml and prints the realised answer back, so the agent
can read what it actually said rather than what it meant to say.

Nothing is written unless every citation resolves: a half-resolved answer
would go to the checkers as if the agent had meant it.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load(filename: str, name: str):
    """Import a sibling script by path — this script's cwd is the working
    directory (where the PDFs and cache live), not the skill dir."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pdf_search = _load("pdf-search.py", "pdf_search")
_verify = _load("verify-citations.py", "verify_citations")

MIN_CITATION_CHARS = _verify.MIN_CITATION_CHARS

# A resolved search_result's snippets are already in the agent's context from
# the tool call that found them; echoing all of them back in the realised
# answer would double-carry an enumeration that can run to 100 matches. The
# file keeps the full list — only this readback is summarised.
RESULTS_ECHO = 3


class SpecError(Exception):
    """An authoring mistake to hand straight back to the agent."""


# ── Resolving one citation ──────────────────────────────────────────────

def _cache_for(source: str):
    if not isinstance(source, str) or not source.strip():
        raise SpecError("citation needs a 'source' PDF filename")
    cache = _verify.load_pdf_cache(source)
    if cache is None:
        raise SpecError(f"no indexed text for source {source!r} — "
                        f"check the filename against the context file")
    return cache


def _resolve_quote(cit: dict) -> dict:
    """A quote citation: one or more line spans, joined in the order given.

    A list rather than a single span because a passage that runs across a
    page break has a running header or footer sitting between its halves —
    the agent simply leaves those lines out instead of the verifier having to
    match around them.
    """
    source = cit.get("source", "")
    cache = _cache_for(source)

    spans = cit.get("spans")
    if not isinstance(spans, list) or not spans:
        raise SpecError("quote citation needs a non-empty 'spans' list")

    parts, resolved = [], []
    for span in spans:
        if not isinstance(span, dict):
            raise SpecError(f"each span must be an object, got {span!r}")
        page, first, last = span.get("page"), span.get("from"), span.get("to")
        if not all(isinstance(v, int) for v in (page, first, last)):
            raise SpecError(
                f"each span needs integer 'page', 'from' and 'to': got {span!r}")
        try:
            parts.append(_pdf_search.resolve_span(cache, page, first, last))
        except ValueError as e:
            raise SpecError(f"{source} span {span!r}: {e}") from e
        resolved.append({"page": page, "from": first, "to": last})

    text = "\n".join(parts)
    if len(text) < MIN_CITATION_CHARS:
        raise SpecError(
            f"{source} spans {resolved} resolve to {len(text)} characters, "
            f"below the {MIN_CITATION_CHARS}-character minimum — widen the "
            f"span to quote the whole passage, not one line of it")

    return {
        "text": text,
        "page": resolved[0]["page"],
        "source": source,
        "spans": resolved,
    }


def _resolve_search(cit: dict) -> dict:
    """A literal-query search_result: the query is rerun here, so `results`
    is what the source actually says rather than what the agent recorded."""
    source = cit.get("source", "")
    cache = _cache_for(source)

    query = cit.get("query")
    if not isinstance(query, str) or not query.strip():
        raise SpecError("search citation needs a non-empty 'query'")

    matches = _pdf_search.find_matches(cache, query)
    return {
        "type": "search_result",
        "source": source,
        "query": query,
        "results": [{"page": m["page"], "text": m["snippet"]} for m in matches],
    }


def _resolve_regex(cit: dict) -> dict:
    """A regex enumeration: every distinct string the pattern matches.

    The pattern's own failure modes stay loud — an invalid or too-broad
    pattern is an authoring error here, not a truncated list that reads like
    an answer.
    """
    source = cit.get("source", "")
    cache = _cache_for(source)

    pattern = cit.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise SpecError("regex citation needs a non-empty 'pattern'")

    result = _pdf_search.find_distinct_matches(cache, pattern)
    if result.get("error") == "invalid_pattern":
        raise SpecError(f"pattern {pattern!r} is not a valid regex: "
                        f"{result['detail']}")
    if result.get("error") == "too_broad":
        raise SpecError(
            f"pattern {pattern!r} matches more than {result['count'] - 1} "
            f"distinct strings — narrow it, or the enumeration proves nothing")

    return {
        "type": "search_result",
        "mode": "regex",
        "source": source,
        "query": pattern,
        "results": [{"match": m["match"], "page": m["page"], "text": m["snippet"]}
                    for m in result["matches"]],
    }


_RESOLVERS = {
    "quote": _resolve_quote,
    "search": _resolve_search,
    "regex": _resolve_regex,
}


def resolve_citation(cit: dict) -> dict:
    if not isinstance(cit, dict):
        raise SpecError(f"each citation must be an object, got {cit!r}")
    kind = cit.get("type")
    if kind not in _RESOLVERS:
        raise SpecError(
            f"citation 'type' must be one of {sorted(_RESOLVERS)}, got {kind!r}")
    return _RESOLVERS[kind](cit)


# ── Resolving the whole answer ──────────────────────────────────────────

def resolve_answer(spec: dict, question: str) -> dict:
    """Materialise the spec, or raise SpecError naming every claim that
    could not be resolved — one round trip should report all of them."""
    answers = spec.get("answers")
    if not isinstance(answers, list):
        raise SpecError("spec needs an 'answers' list "
                        "(use [] for 'unable to answer')")

    resolved, problems = [], []
    for i, ans in enumerate(answers, 1):
        if not isinstance(ans, dict):
            problems.append(f"answer {i}: must be an object, got {ans!r}")
            continue
        claim = ans.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            problems.append(f"answer {i}: needs a non-empty 'claim'")
            continue
        citations = ans.get("citations")
        if not isinstance(citations, list) or not citations:
            problems.append(f"claim {i}: needs a non-empty 'citations' list")
            continue

        out = []
        for j, cit in enumerate(citations, 1):
            try:
                out.append(resolve_citation(cit))
            except SpecError as e:
                problems.append(f"claim {i}, citation {j}: {e}")
        resolved.append({"claim": claim, "citations": out})

    if problems:
        raise SpecError("\n".join(problems))

    return {"question": question, "answers": resolved}


def dump_yaml(answer: dict) -> str:
    import yaml as _yaml
    return _yaml.safe_dump(answer, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=10 ** 6)


def readback(answer: dict) -> str:
    """The realised answer, for the agent to check against what it meant.

    Quote text is shown in full — the agent never typed it, so this is its
    only look at what it actually cited. Search results are summarised: it
    has already seen them in the tool call that produced them.
    """
    if not answer["answers"]:
        return "No claims — this is the valid 'unable to answer' output."
    lines = [f"Realised answer, {len(answer['answers'])} claim(s):", ""]
    for i, ans in enumerate(answer["answers"], 1):
        lines.append(f"CLAIM {i}: {ans['claim']}")
        for cit in ans["citations"]:
            if cit.get("type") == "search_result":
                lines.append(_readback_search(cit))
            else:
                spans = ", ".join(f"p{s['page']} lines {s['from']}-{s['to']}"
                                  for s in cit["spans"])
                lines.append(f"  QUOTE {cit['source']} {spans} "
                             f"({len(cit['text'])} chars):")
                lines.extend(f"    {ln}" for ln in cit["text"].split("\n"))
        lines.append("")
    return "\n".join(lines)


def _readback_search(cit: dict) -> str:
    kind = "REGEX" if cit.get("mode") == "regex" else "SEARCH"
    results = cit["results"]
    head = f"  {kind} {cit['source']} {cit['query']!r} -> {len(results)} result(s)"
    if not results:
        return head + " (found nothing — proves absence)"
    if cit.get("mode") == "regex":
        shown = [r["match"] for r in results[:RESULTS_ECHO]]
        more = f", +{len(results) - len(shown)} more" if len(results) > len(shown) else ""
        return head + ": " + ", ".join(repr(s) for s in shown) + more
    pages = sorted({r["page"] for r in results})
    return head + f" on page(s) {pages}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--question", default="")
    parsed = parser.parse_args()

    try:
        spec = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"ERROR: citations are not valid JSON: {e}")
        return 1

    try:
        answer = resolve_answer(spec, parsed.question)
    except SpecError as e:
        print("ERROR: nothing was written — fix these and call again:\n"
              f"{e}")
        return 1

    Path("answers").mkdir(exist_ok=True)
    path = Path("answers") / f"{parsed.slug}.yml"
    path.write_text(dump_yaml(answer))

    print(f"Wrote {path}")
    print()
    print(readback(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
