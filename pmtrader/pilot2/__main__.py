"""`python -m pilot2` — the service entry point the systemd unit runs."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
