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
        cache = resolved.parent / (resolved.name + ".json")
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
        cf = resolved.parent / (resolved.name + ".json")
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
    ref_counter = 0
    errors = []

    skill_dir = Path(__file__).parent.resolve()

    for a_idx, answer in enumerate(answers):
        claim = answer.get("claim", "").strip()
        if not claim:
            continue
        citations = answer.get("citations", [])

        for cit in citations:
            ref_counter += 1
            text = cit.get("text", "").strip()
            page = cit.get("page", 0)
            source_name = cit.get("source", "")

            source_path = get_source_abs_path(source_name, yaml_dir)
            if source_path is None:
                source_path = source_name
                msg = f"Warning: Source file not found for {source_name}"
                errors.append(msg)
                print(msg, file=sys.stderr)

            cache = load_cache(source_name, yaml_dir)

            highlights = []
            page_w = 0
            page_h = 0

            if cache:
                pages_data = cache.get("pages_data")
                if not pages_data:
                    ensure_cache_has_pages_data(source_path, source_name, yaml_dir, skill_dir)
                    cache = load_cache(source_name, yaml_dir)
                    if cache:
                        pages_data = cache.get("pages_data", {})
                if pages_data:
                    highlights, page_w, page_h = get_highlights_for_text(pages_data, page, text)
            else:
                msg = f"Warning: Cache not found for {source_name} -- PDF preview unavailable"
                errors.append(msg)
                print(msg, file=sys.stderr)

            all_refs.append({
                "num": ref_counter,
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
    ref_index = 0
    for answer in answers:
        claim = answer.get("claim", "").strip()
        if not claim:
            continue
        cits = answer.get("citations", [])
        links = []
        for i in range(len(cits)):
            n = ref_index + i + 1
            ref = all_refs[n - 1]
            links.append(f'<a href="file://{ref["source_path"]}#page={ref["page"]}">{n}</a>')
        ref_index += len(cits)
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

    out_path = yaml_dir / (yaml_path.stem + ".html")
    out_path.write_text(html, encoding="utf-8")

    print(out_path.resolve())


if __name__ == "__main__":
    main()