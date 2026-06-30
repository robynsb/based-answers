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


def load_pdf_cache(pdf_path: str) -> dict:
    cache = pdf_path + ".json"
    if not os.path.exists(cache):
        return None
    with open(cache) as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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

    return {"found": False, "reason": "Citation text not found on specified page"}


def check_concatenation(data: dict) -> dict:
    claims = [a.get("claim", "") for a in data.get("answers", [])]
    expected = ". ".join(claims)
    actual = data.get("concatenation", "")
    if actual == expected:
        return {"found": True}
    return {"found": False, "reason": f"Expected: '{expected}', got: '{actual}'"}


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic citation verifier"
    )
    parser.add_argument("yaml", help="YAML file with claims and citations")
    parser.add_argument("--pdf-dir", default=".",
                        help="Directory containing PDFs and cache files "
                             "(default: current working directory)")
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

            cache_path = os.path.join(args.pdf_dir, source)
            cache = load_pdf_cache(cache_path)

            if cache is None:
                status = "FAIL"
                reason = f"Cache not found for {source}"
                failed += 1
            else:
                result = check_citation(cit_text, page, cache)
                if result["found"]:
                    status = "PASS"
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
