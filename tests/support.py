"""Helpers for loading the skill's dash-named scripts as modules."""

import importlib.util
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def load_script(filename: str):
    """Import a top-level script (e.g. "pdf-search.py") as a module."""
    path = SKILL_DIR / filename
    name = filename.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
