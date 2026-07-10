#!/usr/bin/env python3
"""
Coherence and completeness checker for citation-grounded QA.

Reads a YAML answer file and question, evaluates the concatenation
for coherence and completeness using the coherence-checker opencode agent.

Usage:
  python3 check-coherence.py <yaml-path> <question>

Exit code: 0 if passes, 1 otherwise.
Outputs JSON with result.
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
    if len(sys.argv) < 3:
        print("Usage: check-coherence.py <yaml-path> <question>", file=sys.stderr)
        sys.exit(1)

    yaml_path = Path(sys.argv[1])
    question = sys.argv[2]

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

    concatenation = (data or {}).get("concatenation", "")
    if not concatenation:
        result = {"passed": True, "output": "No concatenation to check"}
        print(json.dumps(result))
        sys.exit(0)

    rubric = f"""You are a coherence and completeness verifier.

QUESTION: {question}

CONCATENATION: {concatenation}

Evaluate:
1. COHERENCE: sensible paragraph with established concepts?
2. COMPLETENESS: totally answers the question?

Respond with:
- PASS
- FAIL: <what is missing, unclear, or doesn't make sense>
"""
    output = run_checker(rubric)
    passed = "PASS" in output.upper() and "FAIL" not in output.upper()

    result = {"passed": passed, "output": output[:2000]}
    print(json.dumps(result, indent=2))

    if not passed:
        print("\nFAILED: coherence check", file=sys.stderr)
        sys.exit(1)
    print("\nPASS: coherence check", file=sys.stderr)


if __name__ == "__main__":
    main()
