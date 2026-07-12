#!/usr/bin/env python3
"""
Generates a self-contained HTML answer page with PDF page previews.

Usage:
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/format-answers.py answers/<slug>.yml

Output: answers/<slug>.html (printed absolute path to stdout)
"""

import argparse
import json
import sys
from pathlib import Path

import yaml as _yaml
from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound


def load_yaml(path: str) -> dict:
    with open(path) as f:
        return _yaml.safe_load(f)


def _source_candidates(source_name: str, yaml_dir: Path) -> list[Path]:
    return [yaml_dir / source_name, Path(source_name)]


def load_cache(source_name: str, yaml_dir: Path) -> dict | None:
    for p in _source_candidates(source_name, yaml_dir):
        resolved = p.resolve()
        cache = Path.cwd() / "indexed-pdfs" / (resolved.name + ".json")
        if cache.exists():
            with open(cache) as f:
                return json.load(f)
    return None


def get_source_abs_path(source_name: str, yaml_dir: Path) -> str | None:
    for p in _source_candidates(source_name, yaml_dir):
        if p.exists():
            return str(p.resolve())
    return None


def ensure_cache_has_pages_data(pdf_path: str, source_name: str, yaml_dir: Path, skill_dir: Path):
    cache_file = None
    for p in _source_candidates(source_name, yaml_dir):
        resolved = p.resolve()
        cf = Path.cwd() / "indexed-pdfs" / (resolved.name + ".json")
        if cf.exists():
            cache_file = cf
            break
    if cache_file is None:
        return
    try:
        with open(cache_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: corrupt or unreadable cache {cache_file}: {e}", file=sys.stderr)
        return
    if "pages_data" in data:
        return
    import importlib.util
    pdf_search_path = str(skill_dir / "pdf-search.py")
    if not Path(pdf_search_path).exists():
        print(f"Warning: pdf_search.py not found at {pdf_search_path}", file=sys.stderr)
        return
    spec = importlib.util.spec_from_file_location("pdf_search", pdf_search_path)
    if spec is None or spec.loader is None:
        return
    pdf_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pdf_mod)
    extract_pages_with_coords = getattr(pdf_mod, "extract_pages_with_coords", None)
    if extract_pages_with_coords is None:
        print("Warning: pdf_search.py has no extract_pages_with_coords function", file=sys.stderr)
        return
    try:
        pages = extract_pages_with_coords(str(pdf_path))
        data["pages_data"] = pages
        with open(cache_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to extract pages data from {pdf_path}: {e}", file=sys.stderr)


# Minimum suffix/prefix overlap (chars) for two same-paragraph quotes to merge
MIN_OVERLAP = 7


def _norm_text(text: str) -> str:
    return " ".join(text.split())


def _overlap_len(a: str, b: str) -> int:
    """Length of the longest suffix of a that is a prefix of b."""
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


def paragraph_ids(cache: dict | None, page: int, text: str) -> frozenset[int]:
    """Indices of the cached paragraph chunks on this page containing the quote."""
    if not cache:
        return frozenset()
    norm = _norm_text(text).lower()
    ids = set()
    for idx, chunk in enumerate(cache.get("chunks", [])):
        if chunk.get("page") != page:
            continue
        if norm in _norm_text(chunk.get("text", "")).lower():
            ids.add(idx)
    return frozenset(ids)


def _try_merge_texts(a: str, b: str, same_paragraph: bool) -> str | None:
    """Merge two quotes if identical or one contains the other; quotes from the
    same paragraph also merge when they overlap by at least MIN_OVERLAP chars."""
    if b in a:
        return a
    if a in b:
        return b
    if same_paragraph:
        k = _overlap_len(a, b)
        if k >= MIN_OVERLAP:
            return a + b[k:]
        k = _overlap_len(b, a)
        if k >= MIN_OVERLAP:
            return b + a[k:]
    return None


def merge_citations(flat: list[dict], paras: list[frozenset[int]]) -> tuple[list[int], dict[int, str]]:
    """Group citations pointing at the same passage: same source and page, and
    texts that are identical, contained in one another, or (within the same
    paragraph) overlapping.

    Returns (root index per citation, merged text per root). Roots are the
    lowest flat index of each group.
    """
    parent = list(range(len(flat)))
    texts = {i: _norm_text(flat[i]["text"]) for i in range(len(flat))}
    paras = list(paras)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    changed = True
    while changed:
        changed = False
        roots = sorted({find(i) for i in range(len(flat))})
        for x, i in enumerate(roots):
            if find(i) != i:
                continue
            for j in roots[x + 1:]:
                if find(j) != j:
                    continue
                if flat[i]["source"] != flat[j]["source"] or flat[i]["page"] != flat[j]["page"]:
                    continue
                merged = _try_merge_texts(texts[i], texts[j], bool(paras[i] & paras[j]))
                if merged is not None:
                    parent[j] = i
                    texts[i] = merged
                    if paras[i] and paras[j]:
                        paras[i] = paras[i] & paras[j]
                    else:
                        paras[i] = paras[i] | paras[j]
                    changed = True

    root_of = [find(i) for i in range(len(flat))]
    return root_of, {r: texts[r] for r in set(root_of)}


def get_highlights_for_text(pages_data: dict, page: int, text: str, max_results: int = 5) -> tuple[list[dict], float, float]:
    page_key = str(page)
    page_info = pages_data.get(page_key, {})
    spans = page_info.get("spans", [])
    page_w = page_info.get("page_width", 1)
    page_h = page_info.get("page_height", 1)
    seen_bboxes = set()
    matching = []
    for s in spans:
        if text in s["text"]:
            key = tuple(s["bbox"])
            if key not in seen_bboxes:
                seen_bboxes.add(key)
                matching.append({"bbox": s["bbox"]})
    if not matching:
        norm = text.replace("\n", " ").strip()
        for s in spans:
            if norm in s["text"].replace("\n", " ").strip():
                key = tuple(s["bbox"])
                if key not in seen_bboxes:
                    seen_bboxes.add(key)
                    matching.append({"bbox": s["bbox"]})
    if not matching:
        norm = text.replace("\n", " ").strip().lower()
        for s in spans:
            if norm in s["text"].replace("\n", " ").strip().lower():
                key = tuple(s["bbox"])
                if key not in seen_bboxes:
                    seen_bboxes.add(key)
                    matching.append({"bbox": s["bbox"]})
    return matching[:max_results], page_w, page_h


def main():
    parser = argparse.ArgumentParser(
        description="Generate HTML answer page with PDF previews"
    )
    parser.add_argument("yaml", help="YAML file with claims and citations")
    args = parser.parse_args()

    yaml_path = Path(args.yaml)
    yaml_dir = yaml_path.parent
    html_dir = Path("answer-pages")
    html_dir.mkdir(parents=True, exist_ok=True)
    if not yaml_path.exists():
        print(f"Error: YAML file not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)
    data = load_yaml(str(yaml_path))

    if not isinstance(data.get("answers"), list):
        print("Error: YAML 'answers' field must be a list", file=sys.stderr)
        sys.exit(1)

    question = data.get("question", "").strip()
    concatenation = data.get("concatenation", "").strip()
    answers = data["answers"]
    unable = not answers

    all_refs = []
    errors = []

    skill_dir = Path(__file__).parent.resolve()

    # Flatten citations, remembering which claim each belongs to
    flat = []
    claim_cits = []  # (claim, [flat indices]) per non-empty claim
    for answer in answers:
        claim = answer.get("claim", "").strip()
        if not claim:
            continue
        idxs = []
        for cit in answer.get("citations", []):
            idxs.append(len(flat))
            flat.append({
                "text": cit.get("text", "").strip(),
                "page": cit.get("page", 0),
                "source": cit.get("source", ""),
            })
        claim_cits.append((claim, idxs))

    caches = {c["source"]: load_cache(c["source"], yaml_dir) for c in flat}

    # Merge citations that quote the same passage into a single numbered reference
    paras = [paragraph_ids(caches[c["source"]], c["page"], c["text"]) for c in flat]
    root_of, merged_texts = merge_citations(flat, paras)

    ref_num = {}  # root -> reference number, in order of first appearance
    for root in root_of:
        if root not in ref_num:
            ref_num[root] = len(ref_num) + 1

    for root, num in sorted(ref_num.items(), key=lambda kv: kv[1]):
        text = merged_texts[root]
        page = flat[root]["page"]
        source_name = flat[root]["source"]
        member_texts = [flat[i]["text"] for i in range(len(flat)) if root_of[i] == root]

        source_path = get_source_abs_path(source_name, yaml_dir)
        if source_path is None:
            source_path = source_name
            msg = f"Warning: Source file not found for {source_name}"
            errors.append(msg)
            print(msg, file=sys.stderr)

        cache = caches.get(source_name)

        highlights = []
        page_w = 0
        page_h = 0

        if cache:
            pages_data = cache.get("pages_data")
            if not pages_data:
                ensure_cache_has_pages_data(source_path, source_name, yaml_dir, skill_dir)
                cache = load_cache(source_name, yaml_dir)
                if cache:
                    caches[source_name] = cache
                    pages_data = cache.get("pages_data", {})
            if pages_data:
                highlights, page_w, page_h = get_highlights_for_text(pages_data, page, text)
                if not highlights:
                    # merged text may span line boundaries; fall back to the original quotes
                    seen = set()
                    for mt in member_texts:
                        hs, page_w, page_h = get_highlights_for_text(pages_data, page, mt)
                        for h in hs:
                            key = tuple(h["bbox"])
                            if key not in seen:
                                seen.add(key)
                                highlights.append(h)
        else:
            msg = f"Warning: Cache not found for {source_name} -- PDF preview unavailable"
            errors.append(msg)
            print(msg, file=sys.stderr)

        all_refs.append({
            "num": num,
            "text": text,
            "page": page,
            "source_name": source_name,
            "source_path": source_path,
            "highlights": highlights,
            "highlights_json": json.dumps(highlights),
            "page_width": page_w,
            "page_height": page_h,
        })

    # Build concatenation HTML: insert clickable citation superscripts after each claim
    concat_parts = []
    for claim, idxs in claim_cits:
        nums = []
        for i in idxs:
            n = ref_num[root_of[i]]
            if n not in nums:
                nums.append(n)
        links = []
        for n in nums:
            ref = all_refs[n - 1]
            links.append(f'<a href="file://{ref["source_path"]}#page={ref["page"]}">{n}</a>')
        sup = f"<sup>{','.join(links)}</sup>" if links else ""
        concat_parts.append(f"{claim}{sup}")

    cat_html = ". ".join(concat_parts)

    try:
        env = Environment(loader=FileSystemLoader(str(skill_dir)))
        template = env.get_template("answer-template.html")
        html = template.render(
            question=question,
            concatenation=cat_html,
            all_references=all_refs,
            unable=unable,
            errors=errors if errors else None,
        )
    except TemplateNotFound:
        print("Error: Template 'answer-template.html' not found in skill directory", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TypeError, ValueError) as e:
        print(f"Error rendering template: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = html_dir / (yaml_path.stem + ".html")
    out_path.write_text(html, encoding="utf-8")

    print(out_path.resolve())


if __name__ == "__main__":
    main()