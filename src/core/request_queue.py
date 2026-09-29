"""
Meeting Scribe — Document request queue

Every saved meeting that asks for documents gets a `<name>.request.md` next
to its bundle. The first "> Status:" line of that file is the queue state:

    PENDING   waiting for documents to be written
    DRAFTED   written as Markdown, .docx still to be produced
    DONE      finished
    SKIPPED   deliberately taken out of the queue

This module reads and updates those files for the Home screen's queue panel.
It never touches anything else in the file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

STATUSES = ("PENDING", "DRAFTED", "DONE", "SKIPPED")
_STATUS_RE = re.compile(r"^>\s*Status:\s*\*\*(\w+)\*\*", re.I)


@dataclass
class RequestItem:
    path: str
    title: str = ""
    date: str = ""
    duration: str = ""
    status: str = "PENDING"
    status_note: str = ""
    documents: List[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return self.path[: -len(".request.md")]

    @property
    def transcript_path(self) -> str:
        return self.base + ".md"

    @property
    def bundle_path(self) -> str:
        return self.base + ".mscribe"


def parse_request(path: str) -> Optional[RequestItem]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    item = RequestItem(path=path)
    in_docs = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# ") and not item.title:
            item.title = s[2:].split("—", 1)[-1].strip()
        m = _STATUS_RE.match(s)
        if m and item.status_note == "" and item.status == "PENDING":
            item.status = m.group(1).upper()
            note = s[m.end():].strip(" —-")
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith(">"):
                note += " " + lines[j].strip().lstrip("> ").strip()
                j += 1
            item.status_note = note.strip()
        if s.startswith("- **Date:**"):
            item.date = s.split("**Date:**", 1)[1].strip()
        if s.startswith("- **Duration:**"):
            item.duration = s.split("**Duration:**", 1)[1].strip()
        if s.startswith("## "):
            in_docs = s.lower().startswith("## documents requested")
            continue
        if in_docs and s.startswith("### "):
            item.documents.append(s[4:].strip())
    if item.status not in STATUSES:
        item.status = "PENDING"
    return item


def list_requests(folder: str) -> List[RequestItem]:
    items = []
    if not folder or not os.path.isdir(folder):
        return items
    for name in os.listdir(folder):
        if name.endswith(".request.md"):
            it = parse_request(os.path.join(folder, name))
            if it:
                items.append(it)
    order = {"PENDING": 0, "DRAFTED": 1, "DONE": 2, "SKIPPED": 3}
    items.sort(key=lambda r: (order.get(r.status, 9), r.date), reverse=False)
    # newest first within each status
    items.sort(key=lambda r: r.date, reverse=True)
    items.sort(key=lambda r: order.get(r.status, 9))
    return items


def counts(items: List[RequestItem]) -> Dict[str, int]:
    c = {s: 0 for s in STATUSES}
    for it in items:
        c[it.status] = c.get(it.status, 0) + 1
    return c


def set_status(path: str, status: str, note: str = "") -> None:
    """Replace the status block (the '> Status:' line and any '>' lines
    directly after it) with a single new status line."""
    status = status.upper()
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status}")
    if not note:
        note = {
            "PENDING": "change to DONE once the documents exist.",
            "DRAFTED": f"documents drafted {date.today().isoformat()}.",
            "DONE": f"completed {date.today().isoformat()}.",
            "SKIPPED": f"removed from the queue {date.today().isoformat()}. Not to be processed.",
        }[status]
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    new_line = f"> Status: **{status}** — {note}"
    for i, line in enumerate(lines):
        if _STATUS_RE.match(line.strip()):
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith(">"):
                j += 1
            lines[i:j] = [new_line]
            break
    else:
        # No status line yet: insert after the title.
        insert_at = 1 if lines and lines[0].startswith("# ") else 0
        lines[insert_at:insert_at] = ["", new_line]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def claude_prompt(item: RequestItem, folder_hint: str = "meetings") -> str:
    """Text the user can paste into Claude (Cowork) to produce the documents."""
    name = os.path.basename(item.path)
    transcript = os.path.basename(item.transcript_path)
    docs = ", ".join(item.documents) or "the requested documents"
    return (f"Please produce the documents requested in `{folder_hint}/{name}` "
            f"({docs}) for the meeting \"{item.title}\". Read the transcript "
            f"`{folder_hint}/{transcript}` and the meeting context in both files, "
            "use only facts from the transcript, flag anything unclear for "
            "verification, save the documents as .docx next to them, and update "
            "the request status when done.")
