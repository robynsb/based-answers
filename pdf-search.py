#!/usr/bin/env python3
"""
Token-efficient PDF text search for citation-grounded QA.

Usage (substitute actual path for SKILL_DIR):
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> info
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> search <query>
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> get <page-num>

  Caches extracted text in indexed-pdfs/ for fast re-use.
Depends on PyMuPDF (provided by the skill's flake.nix).

# Public API for import:
#   extract_pages_with_coords(path) -> dict[int, dict]
#     Extracts per-page text, spans with bounding boxes, and page dimensions.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


# ── PDF Extraction ──────────────────────────────────────────────────────

def extract_text_pymupdf(path: str) -> list[dict]:
    import fitz
    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text()
        pages.append({"page": i, "text": text})
    doc.close()
    return pages


def extract_pages_with_coords(path: str) -> dict[int, dict]:
    import fitz
    doc = fitz.open(path)
    pages = {}
    for i, page in enumerate(doc, start=1):
        blocks = page.get_text("dict")["blocks"]
        spans = []
        for block in blocks:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    spans.append({
                        "text": span["text"],
                        "bbox": [round(v, 1) for v in span["bbox"]],
                    })
        pages[i] = {
            "text": page.get_text(),
            "spans": spans,
            "page_height": page.rect.height,
            "page_width": page.rect.width,
        }
    doc.close()
    return pages


def extract_text_pypdf(path: str) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        pages.append({"page": i, "text": text})
    return pages


def extract_text_pdfplumber(path: str) -> list[dict]:
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append({"page": i, "text": text})
    return pages


def extract_text_pdfminer(path: str) -> list[dict]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextBox, LTTextLine
    pages = []
    for i, page_layout in enumerate(extract_pages(path), start=1):
        text = ""
        for element in page_layout:
            if isinstance(element, (LTTextBox, LTTextLine)):
                text += element.get_text()
        pages.append({"page": i, "text": text})
    return pages


EXTRACTORS = [
    ("PyMuPDF", extract_text_pymupdf),
    ("pypdf", extract_text_pypdf),
    ("pdfplumber", extract_text_pdfplumber),
    ("pdfminer", extract_text_pdfminer),
]


def extract_pages(path: str) -> list[dict] | None:
    for name, func in EXTRACTORS:
        try:
            pages = func(path)
            if pages and any(p["text"].strip() for p in pages):
                return pages
        except Exception:
            continue
    return None


# ── Chunking ────────────────────────────────────────────────────────────

def dehyphenate(text: str) -> str:
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)


def chunk_paragraphs(pages: list[dict]) -> list[dict]:
    chunks = []
    for p in pages:
        text = dehyphenate(p["text"])
        paragraphs = re.split(r"\n\s*\n", text)
        for para in paragraphs:
            stripped = para.strip()
            if len(stripped) < 20:
                continue
            chunks.append({"page": p["page"], "text": stripped})
    return chunks


# ── Token Estimation ────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return len(text) // 4


# ── Cache ───────────────────────────────────────────────────────────────

def cache_path(pdf_path: str) -> str:
    cache_dir = os.path.join(os.getcwd(), "indexed-pdfs")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, os.path.basename(pdf_path) + ".json")


def load_or_extract(pdf_path: str) -> dict:
    cache = cache_path(pdf_path)
    if os.path.exists(cache):
        with open(cache, "r") as f:
            data = json.load(f)
        if "pages_data" not in data:
            pages = extract_pages_with_coords(pdf_path)
            data["pages_data"] = pages
            with open(cache, "w") as f:
                json.dump(data, f, indent=2)
        return data

    pages = extract_pages(pdf_path)
    if pages is None:
        print(
            "Error: No PDF extraction library available. "
            "Ensure the project flake includes pymupdf (python3.withPackages (p: with p; [ pymupdf ]))"
            " and run via: nix develop -c python3 ...",
            file=sys.stderr,
        )
        sys.exit(1)

    chunks = chunk_paragraphs(pages)
    full_text = "\n".join(p["text"] for p in pages)
    data = {
        "file": os.path.abspath(pdf_path),
        "pages": len(pages),
        "chunks": chunks,
        "estimated_tokens": estimate_tokens(full_text),
    }

    with open(cache, "w") as f:
        json.dump(data, f, indent=2)

    return data


# ── Commands ────────────────────────────────────────────────────────────

def cmd_info(data: dict):
    print(f"File: {data['file']}")
    print(f"Pages: {data['pages']}")
    print(f"Chunks: {len(data['chunks'])}")
    print(f"Estimated tokens: {data['estimated_tokens']}")


# Typographic characters folded to ASCII so queries typed with plain quotes,
# hyphens, or spaces still match the PDF's text (e.g. ' vs ’ in "instruction's")
CHAR_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
}


def normalize_for_match(text: str) -> tuple[str, list[int]]:
    """Fold typographic chars and collapse whitespace (incl. newlines).

    Returns the normalized string plus a map from each normalized index back
    to the original index, so matches can be located in the verbatim text.
    """
    out = []
    idx_map = []
    prev_space = False
    for i, ch in enumerate(text):
        for c in CHAR_FOLD.get(ch, ch):
            if c.isspace():
                if not prev_space:
                    out.append(" ")
                    idx_map.append(i)
                    prev_space = True
            else:
                out.append(c)
                idx_map.append(i)
                prev_space = False
    return "".join(out), idx_map


def find_matches(data: dict, query: str, context_chars: int = 300) -> list[dict]:
    """Find every chunk matching query, returning all of them (no limit) as
    [{"page", "snippet"}, ...]. Shared by the pdf_search CLI/tool and by
    verify-citations.py, which reruns a search_result citation's query
    against the same cache to confirm the claimed results are complete."""
    norm_query, _ = normalize_for_match(query)
    norm_query = norm_query.strip()
    if not norm_query:
        return []
    pattern = re.compile(re.escape(norm_query), re.IGNORECASE)
    matches = []
    for chunk in data["chunks"]:
        text = chunk["text"]
        norm_text, idx_map = normalize_for_match(text)
        match = pattern.search(norm_text)
        if match:
            # Map match back to the verbatim text for the context window
            orig_start = idx_map[match.start()]
            orig_end = idx_map[match.end() - 1] + 1
            start = max(0, orig_start - context_chars // 2)
            end = min(len(text), orig_end + context_chars // 2)
            snippet = text[start:end]
            # Add ellipsis if truncated
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            matches.append({
                "page": chunk["page"],
                "snippet": snippet,
            })
    return matches


def find_distinct_matches(data: dict, pattern: str, context_chars: int = 150,
                          max_matches: int = 100) -> dict:
    """Enumerate every distinct string a regex matches anywhere in the doc —
    for search_result citations proving absence/exhaustiveness across a
    whole family of names (e.g. "no pio_sm_* getter reads enabled state"),
    where trying a handful of guessed literal names can never be exhaustive.

    Matching runs on the same normalized (typography-folded) text as
    find_matches, so a match string is always re-findable in a verbatim
    snippet by re-normalizing that snippet the same way (see
    verify-citations.py's check_search_result). Deduplicates by the exact
    matched substring, keeping the first chunk/page it's seen on for its
    example snippet. Snippets are shorter than a normal search's: an
    enumeration can run to 100 matches, and each snippet is copied into the
    answer YAML and quoted back to the semantic checker, so only enough
    context to place the match is worth carrying.

    Returns {"matches": [{"match", "page", "snippet"}, ...]} on success, or
    {"error": "invalid_pattern", "detail": ...} / {"error": "too_broad",
    "count": N} — never a silently truncated list, since a truncated
    enumeration could hide the exact symbol that disproves the claim.
    """
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": "invalid_pattern", "detail": str(e)}

    seen: dict[str, dict] = {}
    for chunk in data["chunks"]:
        text = chunk["text"]
        norm_text, idx_map = normalize_for_match(text)
        for m in compiled.finditer(norm_text):
            matched = m.group(0)
            if matched in seen:
                continue
            orig_start = idx_map[m.start()]
            orig_end = idx_map[m.end() - 1] + 1
            start = max(0, orig_start - context_chars // 2)
            end = min(len(text), orig_end + context_chars // 2)
            snippet = text[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            seen[matched] = {"match": matched, "page": chunk["page"], "snippet": snippet}
            if len(seen) > max_matches:
                return {"error": "too_broad", "count": len(seen)}

    return {"matches": list(seen.values())}


def cmd_search_regex(data: dict, pattern: str, context_chars: int = 150, max_matches: int = 100):
    result = find_distinct_matches(data, pattern, context_chars=context_chars, max_matches=max_matches)
    if "error" in result:
        if result["error"] == "invalid_pattern":
            print(f"Error: invalid regex pattern: {result['detail']}")
        else:
            print(f"Error: pattern matches too many distinct strings (>{max_matches}) "
                  f"to enumerate exhaustively — narrow the pattern")
        return
    matches = result["matches"]
    if not matches:
        print("No matches found.")
        return
    print(f"{len(matches)} distinct match(es):\n")
    for m in matches:
        print(f"--- {m['match']} (page {m['page']}) ---")
        print(m["snippet"])
        print()


def cmd_search(data: dict, query: str, limit: int = 10, context_chars: int = 300):
    matches = find_matches(data, query, context_chars=context_chars)

    if not matches:
        print("No matches found.")
        return

    for m in matches[:limit]:
        print(f"--- Page {m['page']} ---")
        print(m["snippet"])
        print()


def cmd_get(data: dict, page_nums: list[int]):
    page_set = set(page_nums)
    chunks_on_pages = [c for c in data["chunks"] if c["page"] in page_set]
    page_texts = {}
    for c in chunks_on_pages:
        page_texts.setdefault(c["page"], []).append(c["text"])

    for page in sorted(page_texts):
        print(f"=== Page {page} ===")
        print("\n\n".join(page_texts[page]))
        print()


# ── CLI ─────────────────────────────────────────────────────────────────

def make_parser():
    parser = argparse.ArgumentParser(
        description="Token-efficient PDF text search"
    )
    parser.add_argument("pdf", help="Path to PDF file")
    sub = parser.add_subparsers(dest="command", required=True)

    info_p = sub.add_parser("info", help="Show document info")

    search_p = sub.add_parser("search", help="Search for text")
    search_p.add_argument("query", nargs="+", help="Search query")
    search_p.add_argument("--limit", type=int, default=10)
    search_p.add_argument("--context-chars", type=int, default=300)

    search_regex_p = sub.add_parser(
        "search-regex", help="Enumerate every distinct match of a regex pattern")
    search_regex_p.add_argument("pattern", help="Regex pattern (matched case-insensitively)")
    search_regex_p.add_argument("--context-chars", type=int, default=150)
    search_regex_p.add_argument("--max-matches", type=int, default=100)

    get_p = sub.add_parser("get", help="Get full page text")
    get_p.add_argument("pages", nargs="+", type=int, help="Page numbers")

    return parser


def main():
    parser = make_parser()
    parsed = parser.parse_args()

    if not os.path.exists(parsed.pdf):
        print(f"Error: File not found: {parsed.pdf}", file=sys.stderr)
        sys.exit(1)

    data = load_or_extract(parsed.pdf)

    if parsed.command == "info":
        cmd_info(data)
    elif parsed.command == "search":
        cmd_search(data, " ".join(parsed.query), parsed.limit, parsed.context_chars)
    elif parsed.command == "search-regex":
        cmd_search_regex(data, parsed.pattern, parsed.context_chars, parsed.max_matches)
    elif parsed.command == "get":
        cmd_get(data, parsed.pages)


if __name__ == "__main__":
    main()
