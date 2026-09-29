"""
Meeting Scribe — Local Markdown → .docx conversion (no cloud, no AI)

Documents written as Markdown (e.g. `<meeting>_mom.md`) are converted to Word
files with the app's own converter and your local python-docx install.
Used by the queue panel, the workspace, and scripts/convert_md_to_docx.py.
"""
from __future__ import annotations

import glob
import os
from typing import List, Tuple

from src.core.markdown_docx import markdown_to_docx

# Document suffixes produced for meetings (see import_plan.DOCUMENTS).
DOC_SUFFIXES = ("_mom", "_faq", "_summary", "_actions", "_decisions",
                "_confirm", "_custom")


def is_document_md(path: str) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0]
    return path.lower().endswith(".md") and \
        not path.lower().endswith(".request.md") and \
        any(stem.endswith(sfx) for sfx in DOC_SUFFIXES)


def find_unconverted(folder: str) -> List[str]:
    """Document .md files in `folder` that don't have a .docx yet."""
    out = []
    for path in glob.glob(os.path.join(folder, "*.md")):
        if is_document_md(path) and not os.path.exists(os.path.splitext(path)[0] + ".docx"):
            out.append(path)
    return sorted(out)


def convert(md_path: str) -> str:
    with open(md_path, encoding="utf-8") as f:
        markdown = f.read()
    docx_path = os.path.splitext(md_path)[0] + ".docx"
    markdown_to_docx(markdown, docx_path)
    return docx_path


def convert_many(paths: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    done, failed = [], []
    for p in paths:
        try:
            done.append(convert(p))
        except Exception as e:  # keep going; report at the end
            failed.append((p, str(e)))
    return done, failed
