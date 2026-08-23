"""Splice the generated tables from rf_report.sh into the study markdown.

Keeps the narrative and the numbers in one file without hand-copying either -- re-running
rf_report.sh then this script refreshes every table in place.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPORT = Path(os.path.expanduser("~/.pmt/resfarm/report.txt"))
DOC = Path(__file__).with_name("resolution_farmer_study.md")


def block(text: str, start: str, end: str | None, drop_start: bool = False) -> str:
    """Lines from the first line containing `start` up to (not including) the first later
    line containing `end`."""
    lines = text.splitlines()
    try:
        i = next(k for k, l in enumerate(lines) if start in l)
    except StopIteration:
        raise SystemExit(f"splice: start marker not found: {start!r}")
    j = len(lines)
    if end:
        for k in range(i + 1, len(lines)):
            if end in lines[k]:
                j = k
                break
    out = lines[i + 1:j] if drop_start else lines[i:j]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def fenced(s: str) -> str:
    return "```\n" + s + "\n```"


def main():
    t = REPORT.read_text()
    doc = DOC.read_text()

    reps = {
        "PLACEHOLDER_UNBUYABLE": fenced(block(t, "Δ=6h:", "### ADVERSE")),
        "PLACEHOLDER_ADVERSE": fenced(block(t, "population ", "  buyable share by category")),
        "PLACEHOLDER_BUYABLE_BY_CAT": fenced(block(t, "    esports ", "  a wide/one-sided")),
        "PLACEHOLDER_BASERATES": fenced(
            block(t, "### ALL CATEGORIES by price bucket", "### weather (") + "\n\n"
            + block(t, "### by category (Δ=3h, px>=0.93)", "### category x bucket")),
        "PLACEHOLDER_FLIP": fenced(block(t, "DID THE MARKET DISCOVER IT FIRST?", "wrote ")),
        "PLACEHOLDER_CLUSTERING": fenced(block(t, "=== WORST DAYS:", None).split("####")[0]),
        "PLACEHOLDER_ECONOMICS": fenced(
            block(t, "=== price band sweep", "=== slip sensitivity")),
        "PLACEHOLDER_OPS": fenced(block(t, "=== SUPPLY:", "=== EVENT CLUSTERING")),
        "PLACEHOLDER_STOP": fenced(block(t, "  stop      n", "per-category")),
        "PLACEHOLDER_STOP_PROSE": "",
        "PLACEHOLDER_NOISE": fenced(block(t, "band                n", "* mean ask")),
        "PLACEHOLDER_FILTERS": fenced(block(t, "=== top 12 IN-SAMPLE", "=== whole-corpus")),
        "PLACEHOLDER_HEADLINE_FILTERS": "",
    }
    # longest key first: PLACEHOLDER_STOP is a prefix of PLACEHOLDER_STOP_PROSE, and replacing
    # the short one first would corrupt the long one
    for k, v in sorted(reps.items(), key=lambda kv: -len(kv[0])):
        if k not in doc:
            print(f"  (no such placeholder in doc: {k})")
            continue
        doc = doc.replace(k, v)
    DOC.write_text(doc)
    left = re.findall(r"PLACEHOLDER_\w+", doc)
    print(f"spliced. remaining placeholders: {sorted(set(left)) or 'none'}")


if __name__ == "__main__":
    main()
