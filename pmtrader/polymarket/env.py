"""Project .env discovery.

Walks from cwd toward the filesystem root and loads the first .env found,
so SDK scripts and `pmt` pick up repo-root creds from any directory.
"""

from __future__ import annotations

from pathlib import Path

_loaded = False


def load_project_env() -> None:
    """Load the nearest .env above cwd. Idempotent; never overrides exported vars."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for d in (Path.cwd(), *Path.cwd().parents):
        candidate = d / ".env"
        if candidate.is_file():
            from dotenv import load_dotenv

            load_dotenv(candidate, override=False)
            return
