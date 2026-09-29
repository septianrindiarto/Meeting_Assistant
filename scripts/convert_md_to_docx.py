"""
Meeting Scribe — Local Markdown -> DOCX converter (offline, no cloud)

The same conversion is available inside the app (Home → Document queue →
"Convert .md → .docx"). This script is the command-line equivalent.

Usage (from the project root, with the venv activated):

    # Convert specific files:
    python scripts\\convert_md_to_docx.py "meetings\\2026-09-11_Aantwijzing_CIMB_mom.md"

    # Or convert every document .md in meetings\\ that has no .docx yet:
    python scripts\\convert_md_to_docx.py
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.core.doc_convert import convert_many, find_unconverted  # noqa: E402


def main(argv: list) -> int:
    targets = argv or find_unconverted(os.path.join(PROJECT_ROOT, "meetings"))
    if not targets:
        print("Nothing to convert — every document .md already has a .docx.")
        return 0
    done, failed = convert_many(targets)
    for p in done:
        print(f"[OK] {os.path.basename(p)}")
    for p, err in failed:
        print(f"[FAIL] {os.path.basename(p)}: {err}")
    print(f"\nDone: {len(done)}/{len(targets)} converted.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
