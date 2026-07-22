"""Helpers for loading the skill's dash-named scripts as modules."""

import importlib.util
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# The skill's own modules (e.g. pi_rpc) are importable from any test, without
# each one re-deriving the path and re-inserting it.
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


def load_script(filename: str):
    """Import a top-level script (e.g. "pdf-search.py") as a module."""
    path = SKILL_DIR / filename
    name = filename.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
