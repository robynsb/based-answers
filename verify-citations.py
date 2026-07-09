#!/usr/bin/env python3
"""
Deterministic citation verifier for citation-grounded QA.

Checks that every citation text appears verbatim in the source PDF's
extracted text. Uses string matching — no LLM involved.

Usage:
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/verify-citations.py answers/<slug>.yml

YAML input format:
  question: "..."
  concatenation: "all claims joined with '. '"
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


def check_citation(citation_text: str, page: int, cache: dict) -> dict:
    chunks_on_page = [c for c in cache["chunks"] if c["page"] == page]

    if not chunks_on_page:
        return {"found": False, "reason": f"No text extracted for page {page}"}

    page_text = "\n".join(c["text"] for c in chunks_on_page)

    # Check 1: exact substring match
    if citation_text in page_text:
        return {"found": True, "method": "exact"}

    # Check 2: normalized (collapse whitespace)
    norm_citation = normalize_text(citation_text)
    norm_page = normalize_text(page_text)
    if norm_citation in norm_page:
        return {"found": True, "method": "normalized"}

    # Check 3: normalized + case-insensitive
    if norm_citation.lower() in norm_page.lower():
        return {"found": True, "method": "normalized_case_insensitive"}

    # Check 4: remove hyphens (handles "sea-level" vs "sealevel" after dehyphenation)
    nohyphen_citation = norm_citation.replace("-", "").replace("–", "")
    nohyphen_page = norm_page.replace("-", "").replace("–", "")
    if nohyphen_citation in nohyphen_page:
        return {"found": True, "method": "hyphen_normalized"}

    # Check 5: normalize ASCII arrows to Unicode
    def _norm_arrows(t):
        for a, u in [("->", "→"), ("<-", "←"), ("=>", "⇒"), ("<=", "⇐")]:
            t = t.replace(a, u)
        return t
    arrow_citation = _norm_arrows(norm_citation)
    arrow_page = _norm_arrows(norm_page)
    if arrow_citation in arrow_page:
        return {"found": True, "method": "arrow_normalized"}

    # Check 6: strip all common citation-inconsequential characters and spaces
    def _strip_artifacts(t):
        return re.sub(r"[/\\–—.,;:!?'\"()\[\]{}<>→←⇒⇐\s]", "", t)
    artifact_citation = _strip_artifacts(arrow_citation.lower())
    artifact_page = _strip_artifacts(arrow_page.lower())
    if artifact_citation in artifact_page:
        return {"found": True, "method": "artifact_stripped"}

    # Fallback: find closest-matching chunks to help the user debug
    suggestions = _find_closest_matches(norm_citation, chunks_on_page)
    reason = "Citation text not found on specified page"
    if suggestions:
        parts = []
        for sim, txt in suggestions:
            preview = txt[:120].replace("\n", " ")
            parts.append(f"{preview} (sim: {sim:.2f})")
        reason += ". Closest matches:\n  " + "\n  ".join(parts)
    return {"found": False, "reason": reason, "suggestions": suggestions}


def _find_first_diff(expected: str, actual: str) -> dict:
    """Return detailed debug info about the first difference between two strings."""
    result = {
        "expected_length": len(expected),
        "actual_length": len(actual),
    }
    for i in range(min(len(expected), len(actual))):
        if expected[i] != actual[i]:
            result["index"] = i
            result["expected_char"] = expected[i]
            result["expected_ord"] = ord(expected[i])
            result["actual_char"] = actual[i]
            result["actual_ord"] = ord(actual[i])
            return result
    if len(expected) != len(actual):
        result["index"] = min(len(expected), len(actual))
        result["expected_char"] = expected[result["index"]] if result["index"] < len(expected) else ""
        result["actual_char"] = actual[result["index"]] if result["index"] < len(actual) else ""
        result["expected_ord"] = ord(result["expected_char"]) if result["expected_char"] else None
        result["actual_ord"] = ord(result["actual_char"]) if result["actual_char"] else None
        return result
    return {}


def check_concatenation(data: dict) -> dict:
    claims = [a.get("claim", "") for a in data.get("answers", [])]
    expected = ". ".join(claims)
    actual = data.get("concatenation", "")
    if actual == expected:
        return {"found": True}
    result = {"found": False, "expected": expected, "actual": actual}
    diff = _find_first_diff(expected, actual)
    result.update(diff)
    index = diff.get("index")
    if index is not None and "expected_char" in diff and "actual_char" in diff:
        ec, ac = diff["expected_char"], diff["actual_char"]
        eo = diff.get("expected_ord")
        ao = diff.get("actual_ord")
        ec_repr = repr(ec) if ec else "end-of-string"
        ac_repr = repr(ac) if ac else "end-of-string"
        eo_str = f" (U+{eo:04X})" if eo is not None else ""
        ao_str = f" (U+{ao:04X})" if ao is not None else ""
        if ec == "":
            result["reason"] = (
                f"Length mismatch (expected={len(expected)}, actual={len(actual)}). "
                f"Extra trailing content starts at index {index}: {repr(actual[index:index+50])}"
            )
        elif ac == "":
            result["reason"] = (
                f"Length mismatch (expected={len(expected)}, actual={len(actual)}). "
                f"Missing trailing content from index {index}: {repr(expected[index:index+50])}"
            )
        else:
            result["reason"] = (
                f"First diff at index {index}: "
                f"expected {ec_repr}{eo_str}, got {ac_repr}{ao_str}"
            )
    else:
        result["reason"] = (
            f"Length mismatch (expected={len(expected)}, actual={len(actual)})"
        )
    return result


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
            if cache is None:
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

    # Check concatenation
    total += 1
    concat_result = check_concatenation(data)
    if concat_result["found"]:
        concat_status = "PASS"
        passed += 1
    else:
        concat_status = "FAIL"
        reason = concat_result.get("reason", "")
        failed += 1

    if args.format == "table":
        concat_extra = f" ({reason})" if concat_status == "FAIL" else ""
        print(f"{'CONCATENATION':<60} {'':<50} {'':<6} {concat_status:<10}{concat_extra}")
        if concat_status == "FAIL" and args.verbose:
            exp = concat_result.get("expected", "")
            act = concat_result.get("actual", "")
            first_diff = concat_result.get("index")
            if first_diff is not None:
                ctx_start = max(0, first_diff - 40)
                ctx_end = min(len(exp), first_diff + 40)
                if ctx_start > 0:
                    print(f"{'':>60} {'':>50} {'':>6} expected: ...{repr(exp[ctx_start:ctx_end])}...")
                    print(f"{'':>60} {'':>50} {'':>6} actual:   ...{repr(act[ctx_start:ctx_end])}...")
                    marker = " " * (8 + len(repr(exp[ctx_start:first_diff]))) + "^"
                    print(f"{'':>60} {'':>50} {'':>6} {'':>8}{marker}")
                else:
                    print(f"{'':>60} {'':>50} {'':>6} expected: {repr(exp[:80])}")
                    print(f"{'':>60} {'':>50} {'':>6} actual:   {repr(act[:80])}")
    elif args.format == "json":
        results.append({
            "claim": "CONCATENATION",
            "citation": "",
            "page": 0,
            "source": "",
            "status": concat_status,
            "reason": reason if concat_status == "FAIL" else None,
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
