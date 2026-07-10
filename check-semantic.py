#!/usr/bin/env python3
"""
Semantic claim verifier for citation-grounded QA.

Reads a YAML answer file, checks each claim against its source texts
using the coherence-checker opencode agent.

Usage:
  python3 check-semantic.py <yaml-path>

Exit code: 0 if all claims pass, 1 otherwise.
Outputs JSON with per-claim results.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_checker(rubric: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(rubric)
        tmp = f.name

    cmd = [
        "opencode", "run", "--agent", "coherence-checker",
        "Evaluate the following.", "-f", tmp,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    os.unlink(tmp)
    return (result.stdout or "") + (result.stderr or "")


def main():
    if len(sys.argv) < 2:
        print("Usage: check-semantic.py <yaml-path>", file=sys.stderr)
        sys.exit(1)

    yaml_path = Path(sys.argv[1])
    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"Error reading YAML: {e}", file=sys.stderr)
        sys.exit(1)

    answers = data.get("answers", []) if data else []
    results = []

    for i, a in enumerate(answers):
        claim = a.get("claim", "")
        if not claim:
            continue
        texts = [c.get("text", "") for c in a.get("citations", [])]

        rubric = f"""You are a claim verifier.

CLAIM: {claim}

SOURCE_TEXTS:
"""
        for t in texts:
            rubric += f"  - \"{t}\"\n"
        rubric += """

OUT-OF-CONTEXT CHECK: Every source must include text before, text related to the claim, and text after. If bare snippet → FAIL.

Does the SYNTHESIS of all SOURCE_TEXTS strictly imply CLAIM?
- YES: Respond "PASS"
- FAIL (out-of-context): Respond "FAIL: out-of-context — <why>"
- NO: Respond "FAIL: <what cannot be inferred>"

Rules: direct logical inference OK. Cross-source inference OK. External domain knowledge = FAIL. Never use your own knowledge.
"""
        output = run_checker(rubric)
        passed = "PASS" in output.upper() and "FAIL" not in output.upper()
        results.append({
            "claim": claim,
            "passed": passed,
            "output": output[:2000],
        })

    print(json.dumps(results, indent=2))

    failed = sum(1 for r in results if not r["passed"])
    if failed:
        print(f"\nFAILED: {failed}/{len(results)} claims", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\nPASS: {len(results)}/{len(results)} claims", file=sys.stderr)


if __name__ == "__main__":
    main()
