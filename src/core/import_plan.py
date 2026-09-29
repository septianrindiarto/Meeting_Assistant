"""
Meeting Scribe — Import plan (what the Import Wizard collects)

An ImportPlan holds everything the user confirms in the wizard: the ordered
media parts, meeting details, language, key terms and the documents wanted.
It also renders the "Meeting context" block that is written into the
transcript .md and the request file, so whoever writes the documents later
(Claude, manually) has the full picture: client, engagement, roles, topic,
participants, vocabulary and instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# ─── Vocabularies shown in the wizard ───────────────────────────────────

MEETING_TYPES = [
    "Client meeting",
    "Internal checkpoint / team meeting",
    "Vendor briefing / Aanwijzing",
    "Product demo",
    "Workshop / requirements session",
    "Interview",
    "Other",
]

OUR_ROLES = [
    "We are the vendor / partner",
    "We are the client",
    "Internal meeting (no external party)",
    "Other / not applicable",
]

# (label, whisper code or None, note)
LANGUAGES = [
    ("Auto-detect", None),
    ("Bahasa Indonesia", "id"),
    ("Bahasa Malaysia", "ms"),
    ("English", "en"),
    ("Mixed — Indonesian + English", "id"),
    ("Mixed — Malay + English", "ms"),
]

DOC_LANGUAGES = ["Same as the meeting", "Bahasa Indonesia", "English", "Bahasa Malaysia"]

# key -> (label, instruction for whoever writes the document)
DOCUMENTS: Dict[str, tuple] = {
    "mom": ("Minutes of Meeting (MoM)",
            "A formal Minutes of Meeting: title, date, duration, attendees, "
            "agenda/topics discussed, decisions made (with who made them), action "
            "items in a table (owner, action, due date), and next steps."),
    "faq": ("FAQ",
            "Questions raised during the meeting and the answers given, as clear "
            "Q&A pairs grouped by topic. Unanswered questions go under 'Open Questions'."),
    "summary": ("Executive summary",
                "3-5 short paragraphs: what was discussed, what was decided, what "
                "happens next, plus a bulleted 'Key Points' list. Under one page."),
    "actions": ("Action item tracker",
                "A table of every action item: owner, action, due date, status "
                "'Open', and the timestamp where it was agreed."),
    "decisions": ("Decision log",
                  "Every decision made, who made it, the rationale given, and the "
                  "timestamp. Include decisions explicitly postponed."),
    "confirm": ("Items to confirm with the client",
                "Open points, assumptions and ambiguities that must be clarified "
                "with the other party, each with the timestamp it came from."),
}

SUGGESTED_DOCUMENTS = {
    "Client meeting": ["mom", "summary"],
    "Internal checkpoint / team meeting": ["mom", "actions"],
    "Vendor briefing / Aanwijzing": ["mom", "faq", "confirm"],
    "Product demo": ["summary", "faq"],
    "Workshop / requirements session": ["mom", "decisions", "confirm"],
    "Interview": ["summary"],
    "Other": ["mom"],
}


# ─── Plan ───────────────────────────────────────────────────────────────

@dataclass
class ImportPart:
    path: str
    name: str
    size: int = 0
    duration: float = 0.0
    recorded_at: str = ""            # ISO string or ""
    gap_before: Optional[float] = None


@dataclass
class ImportPlan:
    parts: List[ImportPart] = field(default_factory=list)
    order_note: str = ""

    title: str = ""
    date: str = ""                   # YYYY-MM-DD
    client: str = ""
    engagement: str = ""
    meeting_type: str = ""
    our_role: str = ""
    topic: str = ""
    participants: List[str] = field(default_factory=list)

    language_label: str = "Auto-detect"
    language: Optional[str] = None   # whisper code
    key_terms: List[str] = field(default_factory=list)
    use_names_as_terms: bool = True

    requested_documents: List[str] = field(default_factory=list)
    custom_document: str = ""
    document_language: str = "Same as the meeting"
    document_instructions: str = ""

    auto_save: bool = True

    # ── derived ──
    @property
    def total_duration(self) -> float:
        return sum(p.duration for p in self.parts)

    @property
    def job_id(self) -> str:
        """Stable id for resume: same files (path+size) → same job."""
        key = "|".join(f"{p.path}:{p.size}" for p in self.parts)
        return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]

    def vocabulary(self) -> List[str]:
        terms = list(self.key_terms)
        if self.use_names_as_terms:
            terms += [self.client, self.engagement]
            terms += [p.split("—")[0].split(" - ")[0].strip() for p in self.participants]
        seen, out = set(), []
        for t in terms:
            t = (t or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    def vocabulary_prompt(self, base_prompt: str = "", max_chars: int = 600) -> str:
        """Whisper 'prompt' = previous-dialogue style text, so terms are
        listed naturally (instructions would be recited back)."""
        terms = self.vocabulary()
        text = base_prompt.strip()
        if terms:
            text = (text + " " if text else "") + ", ".join(terms) + "."
        return text[:max_chars]

    def document_labels(self) -> List[str]:
        labels = [DOCUMENTS[k][0] for k in self.requested_documents if k in DOCUMENTS]
        if self.custom_document.strip():
            labels.append(self.custom_document.strip())
        return labels

    # ── serialisation (resume manifests) ──
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ImportPlan":
        data = dict(data)
        parts = [ImportPart(**p) for p in data.pop("parts", [])]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(parts=parts, **known)


# ─── Estimates shown on the Review page ─────────────────────────────────

def estimate(plan: ImportPlan, backend: str, local_model: str = "small") -> Dict[str, str]:
    hours = plan.total_duration / 3600.0
    out = {"audio": _fmt_hours(plan.total_duration)}
    if backend == "groq":
        # Free tier: 2 audio-hours per clock-hour, 8 audio-hours per day.
        # Each 2-hour block runs in minutes; blocks after the first wait for
        # the next hourly window.
        import math
        windows = max(1, math.ceil(hours / 2.0))
        wall_min = (windows - 1) * 60 + max(3, int(hours * 2))
        out["time"] = ("a few minutes" if windows == 1 else
                       f"about {wall_min // 60}h {wall_min % 60:02d}m "
                       "(the free tier allows 2 audio-hours per clock-hour; "
                       "the app waits and continues on its own)")
        out["quota"] = (f"uses {hours:.1f} of your 8 free audio-hours today"
                        if hours <= 8 else
                        f"needs {hours:.1f} audio-hours — more than today's 8; "
                        "the rest continues tomorrow (resume is automatic)")
    else:
        speed = {"tiny": 0.1, "base": 0.15, "small": 0.33, "medium": 0.7,
                 "large-v3-turbo": 0.5, "large-v3": 2.0}.get(local_model, 0.4)
        out["time"] = f"about {_fmt_hours(plan.total_duration * speed)} on this PC " \
                      f"(local '{local_model}' model)"
        out["quota"] = "no limits (runs on your computer)"
    out["disk"] = f"~{int(hours * 115 + 20)} MB temporary while processing " \
                  f"(removed after saving); bundle ~{max(1, int(hours * 25))} MB"
    return out


def _fmt_hours(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


# ─── Context block for transcript .md and request file ─────────────────

def context_markdown(meta, heading: str = "## Meeting context") -> List[str]:
    """Render MeetingMetadata context as Markdown lines (empty if none)."""
    if not getattr(meta, "has_context", lambda: False)():
        return []
    lines = [heading, ""]

    def row(label, value):
        if value:
            lines.append(f"- **{label}:** {value}")

    row("Client / organisation", meta.client)
    row("Engagement / project", meta.engagement)
    row("Meeting type", meta.meeting_type)
    row("Our role", meta.our_role)
    if meta.language:
        row("Spoken language", meta.language)
    row("Documents should be written in", meta.document_language)
    if meta.topic:
        lines += ["- **Topic / purpose:**", ""] + \
                 [f"  > {t}" for t in meta.topic.strip().splitlines() if t.strip()]
    if meta.participants:
        lines += ["- **Participants (as entered by the user):**"] + \
                 [f"  - {p}" for p in meta.participants]
    if meta.key_terms:
        lines.append(f"- **Key terms / correct spellings:** {', '.join(meta.key_terms)}")
    if meta.source_files and len(meta.source_files) > 1:
        lines.append("- **Recorded in parts:**")
        for i, f in enumerate(meta.source_files, 1):
            gap = f.get("gap_before")
            gap_txt = f" · break before this part ≈ {round(gap / 60)} min (estimated)" \
                if gap and gap >= 30 else ""
            lines.append(f"  - Part {i}: `{f.get('name')}` — starts at "
                         f"{_ts(f.get('offset', 0))}, length {_fmt_hours(f.get('duration', 0))}"
                         f"{gap_txt}")
    if meta.document_instructions:
        lines += ["- **Instructions for the documents:**", ""] + \
                 [f"  > {t}" for t in meta.document_instructions.strip().splitlines()
                  if t.strip()]
    if meta.recording_events:
        warn = [e for e in meta.recording_events if e.get("level") in ("warning", "error")]
        if warn:
            lines.append("- **Recording issues (audio may be missing around these times):**")
            lines += [f"  - {_ts(e.get('at', 0))} — {e.get('message', '')}" for e in warn[:20]]
    lines.append("")
    return lines


def _ts(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ─── Language detection ─────────────────────────────────────────────────

_NAME_TO_CODE = {"indonesian": "id", "malay": "ms", "english": "en",
                 "javanese": "jw", "sundanese": "su", "tagalog": "tl"}
_CODE_TO_NAME = {v: k.title() for k, v in _NAME_TO_CODE.items()}


def detect_language(audio, settings) -> Dict[str, object]:
    """Detect the spoken language of a short (≈30 s) 16 kHz clip.

    Uses Groq when it is the configured transcription backend (fast, no model
    download), otherwise the local Whisper model. Returns
    {"code", "name", "probability", "alternatives": [(name, p), ...], "method"}.
    """
    import numpy as np

    if settings.get("stt_backend") == "groq" and settings.get("groq_api_key"):
        try:
            return _detect_groq(np.asarray(audio, dtype=np.float32), settings)
        except Exception as e:  # fall back to local
            last_err = e
    from src.core.transcriber import WhisperTranscriber
    model = settings.get("live_model", "small") or "small"
    t = WhisperTranscriber(model_size=model, language=None)
    t._ensure_loaded()
    _segments, info = t._model.transcribe(np.asarray(audio, dtype=np.float32),
                                          language=None, beam_size=1)
    code = info.language or ""
    alts = []
    for item in (getattr(info, "all_language_probs", None) or [])[:3]:
        c, p = item
        alts.append((_CODE_TO_NAME.get(c, c), float(p)))
    return {"code": code, "name": _CODE_TO_NAME.get(code, code),
            "probability": float(info.language_probability or 0),
            "alternatives": alts, "method": f"local Whisper ({t.model_size})"}


def _detect_groq(audio, settings) -> Dict[str, object]:
    import io
    import wave as _wave
    import numpy as np
    import httpx

    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    resp = httpx.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {settings.get('groq_api_key')}"},
        data={"model": settings.get("groq_model", "whisper-large-v3-turbo"),
              "response_format": "verbose_json"},
        files={"file": ("sample.wav", buf.getvalue(), "audio/wav")},
        timeout=60,
    )
    resp.raise_for_status()
    name = (resp.json().get("language") or "").strip().lower()
    code = _NAME_TO_CODE.get(name, name[:2])
    return {"code": code, "name": name.title() or code, "probability": None,
            "alternatives": [], "method": "Groq"}
