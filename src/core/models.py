"""
Meeting Scribe — Shared Data Models
Dataclasses used across all modules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from enum import Enum


class AudioSource(Enum):
    """Audio input source selection."""
    MIC = "mic"
    SYSTEM = "system"
    BOTH = "both"


class MeetingPhase(Enum):
    """Phase labels for transcript sections."""
    INTRO = "intro"
    DISCUSSION = "discussion"
    DECISION = "decision"
    ACTION = "action"
    CLOSING = "closing"
    UNKNOWN = "unknown"


class LLMBackend(Enum):
    """Supported LLM backends for document structuring."""
    OLLAMA = "ollama"        # local, free, requires Ollama installed
    GROQ = "groq"            # cloud, FREE TIER, uses the same key as Groq STT
    OPENAI = "openai"        # cloud, paid
    ANTHROPIC = "anthropic"  # cloud, paid
    NONE = "none"


# ─── Audio & Transcript ─────────────────────────────────────────────

@dataclass
class AudioSegment:
    """A segment of audio identified by VAD."""
    start: float  # seconds from start of recording
    end: float
    audio_data: Optional[bytes] = None  # raw PCM if in-memory

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TranscriptSegment:
    """A single transcribed utterance with optional speaker label."""
    start: float      # seconds
    end: float        # seconds
    text: str
    speaker: str = ""
    confidence: float = 0.0
    language: str = "en"
    phase: str = "unknown"  # MeetingPhase value

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Structured Meeting Data ────────────────────────────────────────

@dataclass
class ActionItem:
    """An extracted action item with owner and optional due date."""
    owner: str
    description: str
    due_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """A decision recorded during the meeting."""
    description: str
    speaker: str
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimelineEvent:
    """A timestamped event in the meeting timeline."""
    timestamp: float
    phase: str  # MeetingPhase value
    topic: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StructuredMeeting:
    """LLM-extracted structured data from a meeting transcript."""
    summary: str = ""
    decisions: List[Decision] = field(default_factory=list)
    action_items: List[ActionItem] = field(default_factory=list)
    timeline: List[TimelineEvent] = field(default_factory=list)
    phases: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "decisions": [d.to_dict() for d in self.decisions],
            "action_items": [a.to_dict() for a in self.action_items],
            "timeline": [t.to_dict() for t in self.timeline],
            "phases": self.phases,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "StructuredMeeting":
        return cls(
            summary=data.get("summary", ""),
            decisions=[Decision(**d) for d in data.get("decisions", [])],
            action_items=[ActionItem(**a) for a in data.get("action_items", [])],
            timeline=[TimelineEvent(**t) for t in data.get("timeline", [])],
            phases=data.get("phases", []),
        )


# ─── Meeting Metadata & Container ──────────────────────────────────

@dataclass
class MeetingMetadata:
    """Metadata stored in meta.json inside a .mscribe bundle."""
    title: str = "Untitled Meeting"
    date: str = ""         # ISO 8601
    duration: str = ""     # human-readable e.g. "32m 15s"
    duration_seconds: float = 0.0
    attendees: List[str] = field(default_factory=list)
    app_version: str = "1.0.0"
    template_used: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingMetadata":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Meeting:
    """
    Top-level container for a single meeting session.
    This is the central object passed between all modules.
    """
    metadata: MeetingMetadata = field(default_factory=MeetingMetadata)
    transcript: List[TranscriptSegment] = field(default_factory=list)
    structured: Optional[StructuredMeeting] = None
    audio_path: str = ""        # path to final audio file (WAV or Opus)
    bundle_path: str = ""       # path to .mscribe bundle
    chunk_paths: List[str] = field(default_factory=list)  # rolling chunk WAVs

    def transcript_to_json(self) -> str:
        return json.dumps(
            [s.to_dict() for s in self.transcript],
            indent=2, ensure_ascii=False
        )

    @property
    def speaker_list(self) -> List[str]:
        """Unique speakers found in transcript, preserving order."""
        seen = set()
        speakers = []
        for seg in self.transcript:
            if seg.speaker and seg.speaker not in seen:
                seen.add(seg.speaker)
                speakers.append(seg.speaker)
        return speakers
