"""
Meeting Scribe — Audio Capture Engine

Captures microphone + system audio (WASAPI loopback) on Windows, mixes them
into one 16 kHz mono stream, and writes 30-second chunks to disk.

What changed (and why)
----------------------
1. ONE audio library. Mic and system audio are both captured through
   PyAudioWPatch (the old mic path used `sounddevice`, a second copy of
   PortAudio that never re-scanned devices — so headsets connected after
   launch were invisible to the Microphone list).

2. Callback streams. Loopback devices deliver NO data while nothing is
   playing, which used to block the reading thread forever. Callback-mode
   streams never block, so they can always be stopped and swapped.

3. A wall-clock mixer. Output is produced at real-time pace regardless of
   what the devices deliver; missing audio becomes silence. The elapsed
   timer therefore never freezes, and a device switch mid-recording leaves
   at most a short silent gap in ONE continuous recording.

4. Hot-swap + fallback + watchdog. A background DeviceWatcher (separate
   process, sees hot-plugged devices) tells the engine when devices change.
   The engine re-resolves the device plan (first Bluetooth headset first,
   then Windows defaults) and switches streams on the fly. If a device
   disconnects it falls back automatically; if the mic stops sending audio
   the watchdog reconnects it. Every event is reported to the UI.
"""
from __future__ import annotations

import os
import time
import uuid
import logging
import threading
from collections import deque
from queue import Queue, Empty
from typing import Callable, Dict, List, Optional

import numpy as np

from src.core.models import AudioSource
from src.core.devices import (
    AudioDevice, DevicePlan, DeviceSnapshot, DeviceWatcher,
    resolve_plan, scan_with, scan_devices,
)
from src.utils.audio_utils import (
    LinearResampler, mix_sources, save_wav_chunk, compute_rms_level,
)
from src.utils.file_utils import get_recording_temp_dir

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────
TARGET_SAMPLE_RATE = 16000   # Whisper expects 16 kHz
CHUNK_DURATION_SEC = 30      # write a new chunk every 30 s (crash safety)
FRAMES_PER_BUFFER = 1024
MIXER_TICK_SEC = 0.05
MIXER_LATENCY_SEC = 0.40     # small jitter buffer so sources have data ready
MAX_BACKLOG_SEC = 1.5        # drop older audio if a device runs fast (clock drift)
MIC_STALL_SEC = 5.0          # mic silent-stream watchdog
RETRY_COOLDOWN_SEC = 10.0


class _Source:
    """One open PortAudio input stream (mic or loopback) feeding the mixer."""

    def __init__(self, device: AudioDevice, role: str):
        self.device = device
        self.role = role                      # "mic" | "system"
        self.rate = int(device.sample_rate or 48000)
        self.channels = max(1, int(device.channels or 1))
        self.resampler = LinearResampler(self.rate, TARGET_SAMPLE_RATE)
        self._raw: deque = deque()            # bytes from the PortAudio callback
        self._buf: deque = deque()            # 16 kHz float32 arrays for the mixer
        self.buf_len = 0
        self.stream = None
        self.opened_at = time.monotonic()
        self.last_data = 0.0                  # monotonic time of last callback with data
        self.paused = False

    # PortAudio calls this from its own thread — keep it tiny.
    def callback(self, in_data, frame_count, time_info, status):
        if in_data and not self.paused:
            self._raw.append(in_data)
            self.last_data = time.monotonic()
        elif in_data:
            self.last_data = time.monotonic()
        return (None, 0)  # 0 == paContinue

    def pump(self) -> None:
        """Convert queued raw bytes to 16 kHz mono (mixer thread)."""
        chunks = []
        while True:
            try:
                chunks.append(self._raw.popleft())
            except IndexError:
                break
        if not chunks:
            return
        data = np.frombuffer(b"".join(chunks), dtype=np.float32)
        if self.channels > 1:
            usable = len(data) // self.channels * self.channels
            data = data[:usable].reshape(-1, self.channels).mean(axis=1)
        out = self.resampler.process(data)
        if len(out):
            self._buf.append(out)
            self.buf_len += len(out)

    def trim_backlog(self, keep: int) -> None:
        excess = self.buf_len - keep
        while excess > 0 and self._buf:
            head = self._buf[0]
            if len(head) <= excess:
                self._buf.popleft()
                self.buf_len -= len(head)
                excess -= len(head)
            else:
                self._buf[0] = head[excess:]
                self.buf_len -= excess
                excess = 0

    def take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        while filled < n and self._buf:
            head = self._buf[0]
            k = min(len(head), n - filled)
            out[filled:filled + k] = head[:k]
            filled += k
            if k == len(head):
                self._buf.popleft()
            else:
                self._buf[0] = head[k:]
        self.buf_len -= filled
        return out

    def clear(self) -> None:
        self._raw.clear()
        self._buf.clear()
        self.buf_len = 0

    def close(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop_stream()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


class AudioCaptureEngine:
    """
    Orchestrates device selection, capture, mixing and chunk writing.

    Usage:
        engine = AudioCaptureEngine(source=AudioSource.BOTH)
        engine.on_level_change = cb       # (float) -> None
        engine.on_audio_chunk = cb        # (np.ndarray) -> None  (live transcription)
        engine.on_device_event = cb       # (level: str, message: str) -> None
        engine.start(); ...; engine.pause(); engine.resume(); ...
        chunk_paths = engine.stop()
    """

    def __init__(self, source: AudioSource = AudioSource.BOTH,
                 mic_device: Optional[str] = None,
                 system_device: Optional[str] = None,
                 prefer_bluetooth: bool = True,
                 bluetooth_mode: str = "both",
                 watch_devices: bool = True):
        self.source = source
        # Device keys from Settings ("input:Name" / "loopback:Name") or None
        # for Automatic. Legacy integer indices are ignored (not stable).
        self.mic_pref = mic_device if isinstance(mic_device, str) else None
        self.system_pref = system_device if isinstance(system_device, str) else None
        self.prefer_bluetooth = prefer_bluetooth
        self.bluetooth_mode = bluetooth_mode
        self.watch_devices = watch_devices

        self.session_id = str(uuid.uuid4())[:8]
        self.session_dir = get_recording_temp_dir(self.session_id)

        self._pa = None
        self._sources: List[_Source] = []
        self._sources_lock = threading.RLock()
        self._switch_lock = threading.Lock()
        self._switch_requests: "Queue[str]" = Queue()
        self._switch_thread: Optional[threading.Thread] = None
        self._mixer_thread: Optional[threading.Thread] = None
        self._watcher: Optional[DeviceWatcher] = None
        self._bt_order: List[str] = []           # Bluetooth groups, first-seen order
        self._plan: Optional[DevicePlan] = None
        self._last_switch = 0.0

        self._is_recording = False
        self._is_paused = False
        self._stop_event = threading.Event()

        self._clock_ref = 0.0
        self._produced = 0                        # samples emitted on the timeline
        self._chunk_index = 0
        self._chunk_paths: List[str] = []
        self._current_chunk_audio: List[np.ndarray] = []
        self._chunk_samples = 0

        self.events: List[dict] = []              # device events for the log / UI
        self._mic_error: Optional[str] = None
        self._chunk_lock = threading.Lock()
        self._excluded: set = set()               # devices that kept failing this session
        self._stall_counts: Dict[str, int] = {}

        # Callbacks
        self.on_level_change: Optional[Callable[[float], None]] = None
        self.on_chunk_saved: Optional[Callable[[str], None]] = None
        self.on_audio_chunk: Optional[Callable[[np.ndarray], None]] = None
        self.on_device_event: Optional[Callable[[str, str], None]] = None

    # ─── Public state ────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def elapsed_seconds(self) -> float:
        return self._produced / TARGET_SAMPLE_RATE

    @property
    def chunk_paths(self) -> List[str]:
        return list(self._chunk_paths)

    @property
    def active_mic_device(self) -> Optional[str]:
        with self._sources_lock:
            for s in self._sources:
                if s.role == "mic":
                    return s.device.display_name
        return None

    @property
    def mic_start_error(self) -> Optional[str]:
        return self._mic_error

    def stream_status(self) -> List[dict]:
        """Per-stream health for the recording bar.

        state: "ok"      data arriving
               "idle"    loopback with nothing playing (normal)
               "stalled" mic stopped sending audio
        """
        now = time.monotonic()
        out = []
        with self._sources_lock:
            for s in self._sources:
                silent_for = now - (s.last_data or s.opened_at)
                if silent_for < 1.5:
                    state = "ok"
                elif s.role == "system":
                    state = "idle"
                else:
                    state = "stalled" if silent_for > MIC_STALL_SEC else "ok"
                out.append({"role": s.role, "device": s.device.display_name,
                            "state": state})
        return out

    # ─── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        if self._is_recording:
            logger.warning("Already recording")
            return
        logger.info(f"Starting audio capture (source={self.source.value}, "
                    f"session={self.session_id})")
        self._is_recording = True
        self._is_paused = False
        self._stop_event.clear()
        self._produced = 0
        self._clock_ref = time.monotonic()

        self._apply_devices("start")

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True,
                                              name="AudioMixer")
        self._mixer_thread.start()
        self._switch_thread = threading.Thread(target=self._switch_loop, daemon=True,
                                               name="DeviceSwitcher")
        self._switch_thread.start()

        if self.watch_devices:
            self._watcher = DeviceWatcher(self._on_devices_changed, interval=3.0)
            if not self._watcher.start():
                self._event("warning", "Automatic device detection unavailable — "
                                       "devices connected now won't be picked up.")

    def pause(self) -> None:
        if not self._is_recording or self._is_paused:
            return
        self._is_paused = True
        with self._sources_lock:
            for s in self._sources:
                s.paused = True
                s.clear()
        self._save_current_chunk()
        logger.info("Recording paused")

    def resume(self) -> None:
        if not self._is_recording or not self._is_paused:
            return
        # Restart the clock from where the timeline stopped.
        self._clock_ref = time.monotonic() - self._produced / TARGET_SAMPLE_RATE
        with self._sources_lock:
            for s in self._sources:
                s.clear()
                s.paused = False
        self._is_paused = False
        logger.info("Recording resumed")

    def stop(self) -> List[str]:
        if not self._is_recording:
            return list(self._chunk_paths)
        logger.info("Stopping audio capture...")
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._stop_event.set()
        self._switch_requests.put("__stop__")
        if self._mixer_thread:
            self._mixer_thread.join(timeout=5)
        if self._switch_thread:
            self._switch_thread.join(timeout=5)
        self._close_all()
        self._save_current_chunk()
        self._is_recording = False
        self._is_paused = False
        logger.info(f"Recording stopped. {len(self._chunk_paths)} chunks, "
                    f"{self.elapsed_seconds:.1f}s, session={self.session_id}")
        return list(self._chunk_paths)

    # ─── Devices ─────────────────────────────────────────────────

    def _event(self, level: str, message: str) -> None:
        entry = {"t": time.time(), "at": self.elapsed_seconds,
                 "level": level, "message": message}
        self.events.append(entry)
        log = logger.error if level == "error" else (
            logger.warning if level == "warning" else logger.info)
        log(f"[devices] {message}")
        if self.on_device_event:
            try:
                self.on_device_event(level, message)
            except Exception:
                pass

    def _remember_bluetooth(self, snapshot: DeviceSnapshot) -> None:
        for g in snapshot.bluetooth_groups():
            if g.base not in self._bt_order:
                self._bt_order.append(g.base)

    def _resolve(self, snapshot: DeviceSnapshot) -> DevicePlan:
        if self._excluded:
            snapshot = DeviceSnapshot(
                [d for d in snapshot.devices if d.name not in self._excluded],
                snapshot.default_input, snapshot.default_output,
                snapshot.error, snapshot.scanned_at)
        return resolve_plan(snapshot, source=self.source.value,
                            mic_pref=self.mic_pref, system_pref=self.system_pref,
                            prefer_bluetooth=self.prefer_bluetooth,
                            bluetooth_mode=self.bluetooth_mode,
                            priority_order=self._bt_order)

    def _close_all(self) -> None:
        with self._sources_lock:
            old, self._sources = self._sources, []
        for s in old:
            s.close()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def _apply_devices(self, reason: str) -> None:
        """(Re)open streams for the current best device plan.

        PortAudio only notices new devices after a full shutdown, so a switch
        closes every stream, restarts PortAudio, re-scans and reopens. The
        mixer keeps the timeline running with silence during the ~1 s gap.
        """
        with self._switch_lock:
            previous = self._plan
            self._close_all()
            try:
                import pyaudiowpatch as pa
            except ImportError:
                self._mic_error = ("PyAudioWPatch is not installed — "
                                   "pip install PyAudioWPatch")
                self._event("error", self._mic_error)
                return
            try:
                self._pa = pa.PyAudio()
                snapshot = scan_with(self._pa)
            except Exception as e:
                self._mic_error = f"Audio system error: {e}"
                self._event("error", self._mic_error)
                return

            if snapshot.error:
                self._event("error", snapshot.error)
            self._remember_bluetooth(snapshot)
            plan = self._resolve(snapshot)
            self._plan = plan
            self._last_switch = time.monotonic()

            new_sources: List[_Source] = []
            self._mic_error = None
            wanted = ([("mic", plan.mic)] if plan.mic else []) + \
                     [("system", d) for d in plan.loopbacks]
            if self.source in (AudioSource.MIC, AudioSource.BOTH) and plan.mic is None:
                self._mic_error = "No microphone found"
            for role, dev in wanted:
                src = _Source(dev, role)
                src.paused = self._is_paused
                try:
                    src.stream = self._pa.open(
                        format=pa.paFloat32, channels=src.channels, rate=src.rate,
                        input=True, input_device_index=dev.index,
                        frames_per_buffer=FRAMES_PER_BUFFER,
                        stream_callback=src.callback,
                    )
                    src.stream.start_stream()
                    new_sources.append(src)
                except Exception as e:
                    msg = f"Could not open {dev.display_name}: {e}"
                    if role == "mic":
                        self._mic_error = msg
                    self._event("error", msg)

            with self._sources_lock:
                self._sources = new_sources

            desc = plan.describe()
            if reason == "start":
                self._event("info", f"Recording from {desc}")
            elif previous is None or previous.names() != plan.names():
                level = "warning" if reason in ("disconnected", "stalled") else "info"
                prefix = {"connected": "New device detected — switched to",
                          "disconnected": "Device disconnected — switched to",
                          "stalled": "Microphone stopped sending audio — reconnected to",
                          "default": "Windows audio device changed — switched to",
                          }.get(reason, "Switched to")
                self._event(level, f"{prefix} {desc}")

    def _on_devices_changed(self, snapshot: DeviceSnapshot) -> None:
        """Called by DeviceWatcher (background thread) when devices change."""
        if not self._is_recording:
            return
        # Hardware changed — give previously failing devices another chance.
        self._excluded.clear()
        self._stall_counts.clear()
        self._remember_bluetooth(snapshot)
        new_plan = self._resolve(snapshot)
        current = self._plan
        if current is not None and current.names() == new_plan.names():
            return
        current_names = set()
        if current is not None:
            current_names = {n for n in [current.mic.name if current.mic else None]
                             if n} | {d.name for d in current.loopbacks}
        present = {d.name for d in snapshot.devices}
        if current_names and not current_names.issubset(present):
            reason = "disconnected"
        elif new_plan.bluetooth_group and (current is None or
                                            current.bluetooth_group != new_plan.bluetooth_group):
            reason = "connected"
        else:
            reason = "default"
        self._switch_requests.put(reason)

    def _switch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                reason = self._switch_requests.get(timeout=0.5)
            except Empty:
                continue
            if reason == "__stop__":
                break
            # Coalesce bursts of requests into one switch.
            try:
                while True:
                    nxt = self._switch_requests.get_nowait()
                    if nxt == "__stop__":
                        return
                    reason = nxt if nxt == "disconnected" else reason
            except Empty:
                pass
            if self._stop_event.is_set():
                break
            try:
                self._apply_devices(reason)
            except Exception as e:
                self._event("error", f"Device switch failed: {e}")

    def _watchdog(self, now: float) -> None:
        if self._is_paused or now - self._last_switch < RETRY_COOLDOWN_SEC:
            return
        with self._sources_lock:
            sources = list(self._sources)
        wants_mic = self.source in (AudioSource.MIC, AudioSource.BOTH)
        mic = next((s for s in sources if s.role == "mic"), None)
        if wants_mic and mic is None:
            self._last_switch = now
            self._switch_requests.put("stalled")
            return
        if mic is not None:
            silent_for = now - (mic.last_data or mic.opened_at)
            if silent_for > MIC_STALL_SEC:
                self._last_switch = now
                name = mic.device.name
                self._stall_counts[name] = self._stall_counts.get(name, 0) + 1
                if self._stall_counts[name] >= 3:
                    # Two reconnects didn't help — skip this mic for now.
                    self._excluded.add(name)
                    self._event("error", f"{mic.device.display_name} isn't sending "
                                         "audio — switching to another microphone")
                else:
                    self._event("warning", f"{mic.device.display_name} stopped sending "
                                           "audio — reconnecting...")
                self._switch_requests.put("stalled")
        if not sources and self.source != AudioSource.MIC:
            self._last_switch = now
            self._switch_requests.put("stalled")

    # ─── Mixing ──────────────────────────────────────────────────

    def _mixer_loop(self) -> None:
        last_watchdog = 0.0
        while not self._stop_event.is_set():
            time.sleep(MIXER_TICK_SEC)
            now = time.monotonic()
            with self._sources_lock:
                sources = list(self._sources)
            for s in sources:
                s.pump()
            if self._is_paused:
                for s in sources:
                    s.clear()
                continue
            target = int((now - self._clock_ref - MIXER_LATENCY_SEC) * TARGET_SAMPLE_RATE)
            need = target - self._produced
            if need > 0:
                self._emit(sources, need)
            if now - last_watchdog > 1.0:
                last_watchdog = now
                self._watchdog(now)

        # Final flush: include the jitter buffer so the last words are kept.
        with self._sources_lock:
            sources = list(self._sources)
        for s in sources:
            s.pump()
        if not self._is_paused:
            target = int((time.monotonic() - self._clock_ref) * TARGET_SAMPLE_RATE)
            need = target - self._produced
            if need > 0:
                self._emit(sources, need)

    def _emit(self, sources: List[_Source], n: int) -> None:
        keep = n + int(MAX_BACKLOG_SEC * TARGET_SAMPLE_RATE)
        parts = []
        for s in sources:
            s.trim_backlog(keep)
            parts.append(s.take(n))
        mixed = mix_sources(parts) if parts else np.zeros(n, dtype=np.float32)

        with self._chunk_lock:
            self._current_chunk_audio.append(mixed)
            self._chunk_samples += len(mixed)
        self._produced += len(mixed)

        if self.on_level_change:
            try:
                self.on_level_change(compute_rms_level(mixed))
            except Exception:
                pass
        if self.on_audio_chunk:
            try:
                self.on_audio_chunk(mixed)
            except Exception:
                pass
        if self._chunk_samples >= CHUNK_DURATION_SEC * TARGET_SAMPLE_RATE:
            self._save_current_chunk()

    def _save_current_chunk(self) -> None:
        with self._chunk_lock:
            if not self._current_chunk_audio:
                return
            audio = np.concatenate(self._current_chunk_audio)
            self._current_chunk_audio = []
            self._chunk_samples = 0
        if len(audio) == 0:
            return
        path = os.path.join(str(self.session_dir), f"chunk_{self._chunk_index:04d}.wav")
        try:
            save_wav_chunk(audio, path, TARGET_SAMPLE_RATE)
            self._chunk_paths.append(path)
            self._chunk_index += 1
            if self.on_chunk_saved:
                try:
                    self.on_chunk_saved(path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to save chunk: {e}", exc_info=True)

    # ─── Compatibility helper ────────────────────────────────────

    @staticmethod
    def list_audio_devices() -> dict:
        """Legacy shape: {'mic_devices': [...], 'system_devices': [...]}."""
        snap = scan_devices()
        return {
            "mic_devices": [{"key": d.key, "name": d.display_name, "index": d.index,
                             "channels": d.channels, "sample_rate": d.sample_rate}
                            for d in snap.inputs()],
            "system_devices": [{"key": d.key, "name": d.name, "index": d.index,
                                "channels": d.channels, "sample_rate": d.sample_rate}
                               for d in snap.loopbacks()],
        }
