"""
Meeting Scribe — Media inspection & multi-part ordering (Import Wizard)

The app can't know what a video is about, but it CAN read a few facts from
the file itself, which the wizard shows to the user for confirmation:

    - duration, file size, whether there is an audio track
    - the recording time stored inside the file (mp4 "creation_time")
    - a date/time or number written in the file name

For a meeting split across several files, those signals decide the order of
the parts. The order is only accepted automatically when the evidence is
clear; otherwise the wizard asks the user to arrange the parts themselves.
"""
from __future__ import annotations

import os
import re
import wave
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SR = 16000

# 2026-07-15 14-06-30 · 2026-07-15_14.06.30 · 20260915_093012 · 2026-09-15T09-30-12
_NAME_DT_RE = re.compile(
    r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[ _T.\-]*(\d{2})[-_.:h]?(\d{2})(?:[-_.:m]?(\d{2}))?")
_NUM_RE = re.compile(r"(\d+)")


@dataclass
class MediaInfo:
    path: str
    name: str
    size: int = 0
    duration: float = 0.0              # seconds (0 = unknown)
    has_audio: bool = False
    has_video: bool = False
    recorded_at: Optional[datetime] = None   # from the file's metadata
    name_time: Optional[datetime] = None     # from the file name
    mtime: Optional[datetime] = None         # Windows file date
    error: Optional[str] = None

    @property
    def size_mb(self) -> float:
        return self.size / 1024 / 1024


# ─── Probing ────────────────────────────────────────────────────────────

def _parse_creation_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            break
        except ValueError:
            dt = None
    if dt is None or dt.year < 2000:          # 1904/1970 placeholders = unknown
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)   # show in local time
    return dt


def time_from_name(name: str) -> Optional[datetime]:
    m = _NAME_DT_RE.search(name)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        return None


def probe_media(path: str) -> MediaInfo:
    info = MediaInfo(path=path, name=os.path.basename(path))
    try:
        st = os.stat(path)
        info.size = st.st_size
        info.mtime = datetime.fromtimestamp(st.st_mtime)
    except OSError as e:
        info.error = f"File not accessible: {e}"
        return info
    info.name_time = time_from_name(os.path.splitext(info.name)[0])

    try:
        import av
    except ImportError:
        info.error = "PyAV not installed — cannot read media files"
        return info
    try:
        with av.open(path) as c:
            audio = [s for s in c.streams if s.type == "audio"]
            info.has_audio = bool(audio)
            info.has_video = any(s.type == "video" for s in c.streams)
            if c.duration:
                info.duration = float(c.duration) / 1_000_000   # AV_TIME_BASE
            elif audio and audio[0].duration and audio[0].time_base:
                info.duration = float(audio[0].duration * audio[0].time_base)
            meta = dict(c.metadata or {})
            if audio:
                meta.update({k: v for k, v in (audio[0].metadata or {}).items()
                             if k not in meta})
            info.recorded_at = _parse_creation_time(
                meta.get("creation_time") or meta.get("date") or "")
    except Exception as e:
        info.error = f"Could not read file: {e}"
    return info


# ─── Ordering ───────────────────────────────────────────────────────────

def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in _NUM_RE.split(name)]


def _distinct(values) -> bool:
    return all(v is not None for v in values) and len(set(values)) == len(values)


def order_parts(items: List[MediaInfo]) -> Tuple[List[MediaInfo], str, str]:
    """Return (ordered_items, confidence, reason).

    confidence:  "high"  recording times inside the files or in the names
                 "medium" numbers in the file names
                 "low"   only Windows file dates, or the signals disagree —
                         the wizard then requires the user to confirm.
    """
    if len(items) <= 1:
        return list(items), "high", "single file"

    candidates = []   # (label, confidence, ordered list)
    if _distinct([i.recorded_at for i in items]):
        candidates.append(("the recording time stored in each file", "high",
                           sorted(items, key=lambda i: i.recorded_at)))
    if _distinct([i.name_time for i in items]):
        candidates.append(("the date/time in the file names", "high",
                           sorted(items, key=lambda i: i.name_time)))
    names = [os.path.splitext(i.name)[0] for i in items]
    if all(_NUM_RE.search(n) for n in names) and len(set(names)) == len(names):
        candidates.append(("the numbers in the file names", "medium",
                           sorted(items, key=lambda i: _natural_key(i.name))))

    if not candidates:
        ordered = sorted(items, key=lambda i: i.mtime or datetime.min)
        return (ordered, "low",
                "No recording times or numbers found — ordered by Windows file "
                "date, which is often wrong after copying. Please check the order.")

    label, conf, ordered = candidates[0]
    orders = {tuple(i.path for i in c[2]) for c in candidates}
    if len(orders) > 1:
        return (ordered, "low",
                f"Ordered by {label}, but other clues suggest a different order. "
                "Please check the order (use ▶ to listen across each boundary).")
    return ordered, conf, f"Ordered by {label}."


def duplicate_warnings(items: List[MediaInfo]) -> List[str]:
    out = []
    for a_i in range(len(items)):
        for b_i in range(a_i + 1, len(items)):
            a, b = items[a_i], items[b_i]
            if a.size == b.size and abs(a.duration - b.duration) < 1.0:
                out.append(f"'{a.name}' and '{b.name}' look identical "
                           "(same size and length) — was the same file added twice?")
    return out


def estimate_gaps(items: List[MediaInfo]) -> List[Optional[float]]:
    """Estimated break (seconds) BEFORE each part; first entry is None.

    Recorders store either the START or the END time in the file. We try
    both interpretations and keep the one where no part overlaps the next.
    Returns all-None when there isn't enough information.
    """
    n = len(items)
    none = [None] * n
    if n < 2 or not all(i.recorded_at and i.duration for i in items):
        return none
    t = [i.recorded_at.timestamp() for i in items]
    d = [i.duration for i in items]
    as_start = [t[k + 1] - (t[k] + d[k]) for k in range(n - 1)]
    as_end = [(t[k + 1] - d[k + 1]) - t[k] for k in range(n - 1)]
    for gaps in (as_start, as_end):
        if all(-120 <= g <= 18 * 3600 for g in gaps):
            return [None] + [max(0.0, g) for g in gaps]
    return none


# ─── Audio clips (language detection & ▶ previews) ──────────────────────

def extract_clip(path: str, start: float, duration: float) -> np.ndarray:
    """Decode `duration` seconds of audio from `start` as 16 kHz mono float32."""
    import av
    from av.audio.resampler import AudioResampler

    need = int(duration * SR)
    pieces, got = [], 0
    with av.open(path) as c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros(0, dtype=np.float32)
        if start > 0:
            try:
                c.seek(int(start / stream.time_base), stream=stream, backward=True)
            except Exception:
                pass
        res = AudioResampler(format="s16", layout="mono", rate=SR)
        for frame in c.decode(stream):
            ft = frame.time
            for rf in res.resample(frame):
                arr = rf.to_ndarray().flatten()
                if ft is not None and ft < start - 0.05:
                    skip = int((start - ft) * SR)
                    arr = arr[skip:] if skip < len(arr) else arr[:0]
                if len(arr):
                    pieces.append(arr)
                    got += len(arr)
            if got >= need:
                break
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return (np.concatenate(pieces)[:need].astype(np.float32) / 32768.0)


def boundary_preview(a: MediaInfo, b: MediaInfo, out_path: str,
                     seconds: float = 10.0) -> str:
    """Write the last `seconds` of part A + a short beep + the first `seconds`
    of part B to a WAV, so the user can hear whether the parts connect."""
    tail = extract_clip(a.path, max(0.0, a.duration - seconds), seconds)
    head = extract_clip(b.path, 0.0, seconds)
    t = np.arange(int(0.25 * SR)) / SR
    beep = (0.15 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    gap = np.zeros(int(0.15 * SR), dtype=np.float32)
    audio = np.concatenate([tail, gap, beep, gap, head])
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return out_path


def language_sample(info: MediaInfo, seconds: float = 30.0) -> np.ndarray:
    """30 s of audio for language detection — skip the first minute when the
    file is long enough (meetings often start with silence or small talk)."""
    start = 60.0 if info.duration > 60 + seconds + 10 else 0.0
    return extract_clip(info.path, start, seconds)
