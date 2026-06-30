#!/usr/bin/env python3
"""
Token-efficient PDF text search for citation-grounded QA.

Usage (substitute actual path for SKILL_DIR):
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> info
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> search <query>
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/pdf-search.py <file.pdf> get <page-num>

Caches extracted text as <file.pdf>.json sidecar for fast re-use.
Depends on PyMuPDF (provided by the skill's flake.nix).
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
    return pdf_path + ".json"


def load_or_extract(pdf_path: str) -> dict:
    cache = cache_path(pdf_path)
    if os.path.exists(cache):
        with open(cache, "r") as f:
            return json.load(f)

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


def cmd_search(data: dict, query: str, limit: int = 10, context_chars: int = 300):
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = []
    for chunk in data["chunks"]:
        if pattern.search(chunk["text"]):
            text = chunk["text"]
            # Find position of match for context window
            match = pattern.search(text)
            start = max(0, match.start() - context_chars // 2)
            end = min(len(text), match.end() + context_chars // 2)
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
    elif parsed.command == "get":
        cmd_get(data, parsed.pages)


if __name__ == "__main__":
    main()
