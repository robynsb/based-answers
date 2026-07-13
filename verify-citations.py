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
import json
import os
import re
import sys
from pathlib import Path


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
    parser.add_argument("--format", choices=["table", "json", "summary"],
                        default="table", help="Output format")

    args = parser.parse_args()
    data = load_yaml(args.yaml)

    total = 0
    passed = 0
    failed = 0
    results = []

    print(f"Question: {data.get('question', '(none)')}\n")

    if args.format == "table":
        print(f"{'Claim':<60} {'Citation':<50} {'Page':<6} {'Result':<10}")
        print("-" * 130)

    for answer in data.get("answers", []):
        claim = answer.get("claim", "")
        for cit in answer.get("citations", []):
            total += 1
            cit_text = cit.get("text", "")
            page = cit.get("page", 0)
            source = cit.get("source", "")

            cache = load_pdf_cache(source)

            result = None
            if len(cit_text) < MIN_CITATION_CHARS:
                status = "FAIL"
                reason = (f"Citation too short: {len(cit_text)} chars, minimum is "
                          f"{MIN_CITATION_CHARS}. Quote a longer contiguous passage "
                          f"around the supporting text")
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

            if args.format == "table":
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
        print(f"\n{'=' * 130}")
        print(f"Total: {total}  |  Passed: {passed}  |  Failed: {failed}")

    if args.format == "json":
        print(json.dumps(results, indent=2))
    elif args.format == "summary":
        print(f"{'=' * 60}")
        print(f"Total: {total}  |  Passed: {passed}  |  Failed: {failed}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
