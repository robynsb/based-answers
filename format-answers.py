#!/usr/bin/env python3
"""
Formats answers YAML (e.g. answers/<slug>.yml) into footnote-style output.

Output format:
  Question text

  Claim text [1].
  Second claim text [2,3].

  References:
  [1] "verbatim quote" (p.12) — file.pdf
  [2] "verbatim quote" (p.5)  — file.pdf
  [3] "verbatim quote" (p.5)  — file.pdf

Usage:
  nix develop "path:SKILL_DIR" -c python3 SKILL_DIR/format-answers.py answers/<slug>.yml
"""

import argparse
import sys
from pathlib import Path


def load_yaml(path: str) -> dict:
    import yaml as _yaml
    with open(path) as f:
        return _yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Format answers YAML (e.g. answers/<slug>.yml) as footnote-style text"
    )
    parser.add_argument("yaml", help="YAML file with claims and citations")
    args = parser.parse_args()

    data = load_yaml(args.yaml)
    q = data.get("question", "").strip()
    answers = data.get("answers", [])

    if not answers:
        print(q)
        print()
        print("Unable to answer this question given the sources.")
        return

    print(q)
    print()

    refs = []
    ref_counter = 0

    for answer in answers:
        claim = answer.get("claim", "").strip()
        citations = answer.get("citations", [])

        if not claim:
            continue

        numbers = []
        for cit in citations:
            ref_counter += 1
            numbers.append(str(ref_counter))
            refs.append({
                "num": ref_counter,
                "text": cit.get("text", "").strip(),
                "page": cit.get("page", "?"),
                "source": cit.get("source", ""),
            })

        print(f"  {claim.rstrip('.')} [{','.join(numbers)}].")

    print()
    print("  References:")
    for r in refs:
        src = r["source"].rsplit("/", 1)[-1] if "/" in r["source"] else r["source"]
        print(f"  [{r['num']}] \"{r['text']}\" (p.{r['page']}) \u2014 {src}")

    print()


if __name__ == "__main__":
    main()
