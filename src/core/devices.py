"""
Meeting Scribe — Audio device discovery & automatic device choice

Concepts
--------
AudioDevice   one Windows audio endpoint (a mic, a speaker, or a speaker's
              "loopback" = what that speaker is playing).
DeviceGroup   all endpoints of one physical device. A Bluetooth headset shows
              up as several endpoints, e.g.
                  Headset (soundcore V20i Hands-Free)          <- mic (input)
                  Headphones (soundcore V20i)                  <- A2DP output
                  Headset (soundcore V20i Hands-Free)          <- HFP output
              plus a [Loopback] for each output. Grouping them by their base
              name ("soundcore v20i") lets us treat the headset as one thing.
DevicePlan    which mic + which loopback(s) a recording should use.

The Bluetooth rule
------------------
When a headset's microphone is used, Windows switches it to Hands-Free mode:
the stereo "Headphones" output goes silent and audio moves to the Hands-Free
output. If we captured only the "Headphones" loopback we'd record silence.
So for a Bluetooth group we capture ALL of its loopbacks and mix them —
whichever one is actually carrying the meeting audio ends up in the recording.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_probe.py")

# Name fragments Windows uses for Bluetooth audio endpoints.
_BT_MARKERS = ("hands-free", "handsfree", "bluetooth", "ag audio")
_SUFFIX_RE = re.compile(r"\s*(hands-?free(\s+ag\s+audio)?|ag\s+audio|stereo)\s*$", re.I)
_LOOPBACK_RE = re.compile(r"\s*\[loopback\]\s*$", re.I)


# ─── Model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AudioDevice:
    name: str               # full WASAPI friendly name
    kind: str               # "input" | "loopback" | "output"
    channels: int
    sample_rate: int
    is_default: bool = False
    index: int = -1         # PortAudio index — only valid in the process that scanned

    @property
    def key(self) -> str:
        """Stable identity used in settings (survives device renumbering)."""
        return f"{self.kind}:{self.name}"

    @property
    def base(self) -> str:
        return device_base_name(self.name)

    @property
    def looks_bluetooth(self) -> bool:
        low = self.name.lower()
        return any(m in low for m in _BT_MARKERS)

    @property
    def is_hands_free(self) -> bool:
        low = self.name.lower()
        return "hands-free" in low or "handsfree" in low

    @property
    def display_name(self) -> str:
        return _LOOPBACK_RE.sub("", self.name)


def device_base_name(name: str) -> str:
    """'Headset (soundcore V20i Hands-Free) [Loopback]' -> 'soundcore v20i'."""
    s = _LOOPBACK_RE.sub("", name or "").strip()
    first, last = s.find("("), s.rfind(")")
    inner = s[first + 1:last] if 0 <= first < last else s
    inner = _SUFFIX_RE.sub("", inner.strip())
    return inner.strip().lower()


@dataclass
class DeviceGroup:
    base: str
    devices: List[AudioDevice] = field(default_factory=list)

    @property
    def is_bluetooth(self) -> bool:
        return any(d.looks_bluetooth for d in self.devices)

    @property
    def inputs(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.kind == "input"]

    @property
    def loopbacks(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.kind == "loopback"]

    @property
    def best_input(self) -> Optional[AudioDevice]:
        ins = self.inputs
        hf = [d for d in ins if d.is_hands_free]
        return (hf or ins or [None])[0]

    @property
    def label(self) -> str:
        for d in self.devices:
            m = re.search(r"\((.*)\)", d.display_name)
            if m:
                return _SUFFIX_RE.sub("", m.group(1)).strip()
        return self.base


@dataclass
class DeviceSnapshot:
    devices: List[AudioDevice] = field(default_factory=list)
    default_input: str = ""
    default_output: str = ""
    error: Optional[str] = None
    scanned_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceSnapshot":
        devs = []
        for d in data.get("devices", []):
            try:
                devs.append(AudioDevice(
                    name=d["name"], kind=d["kind"],
                    channels=int(d.get("channels", 1)),
                    sample_rate=int(d.get("sample_rate", 48000)),
                    is_default=bool(d.get("is_default", False)),
                    index=int(d.get("index", -1)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(devs, data.get("default_input", "") or "",
                   data.get("default_output", "") or "",
                   data.get("error"), float(data.get("scanned_at", 0) or 0))

    # ── lookups ──
    def inputs(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.kind == "input"]

    def loopbacks(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.kind == "loopback"]

    def find(self, key: Optional[str]) -> Optional[AudioDevice]:
        if not key:
            return None
        for d in self.devices:
            if d.key == key:
                return d
        return None

    def groups(self) -> List[DeviceGroup]:
        order: Dict[str, DeviceGroup] = {}
        for d in self.devices:
            order.setdefault(d.base, DeviceGroup(d.base)).devices.append(d)
        return list(order.values())

    def group_of(self, device: Optional[AudioDevice]) -> Optional[DeviceGroup]:
        if device is None:
            return None
        for g in self.groups():
            if g.base == device.base:
                return g
        return None

    def bluetooth_groups(self) -> List[DeviceGroup]:
        return [g for g in self.groups() if g.is_bluetooth]

    def default_input_device(self) -> Optional[AudioDevice]:
        ins = self.inputs()
        return next((d for d in ins if d.is_default), ins[0] if ins else None)

    def default_loopback_device(self) -> Optional[AudioDevice]:
        lbs = self.loopbacks()
        chosen = next((d for d in lbs if d.is_default), None)
        if chosen is None and self.default_output:
            chosen = next((d for d in lbs if d.name.startswith(self.default_output)), None)
        return chosen or (lbs[0] if lbs else None)

    def signature(self) -> str:
        """Changes whenever a device appears/disappears or a default changes."""
        names = sorted(d.key for d in self.devices)
        return json.dumps([names, self.default_input, self.default_output])


# ─── Choosing devices ───────────────────────────────────────────────────

@dataclass
class DevicePlan:
    mic: Optional[AudioDevice] = None
    loopbacks: List[AudioDevice] = field(default_factory=list)
    bluetooth_group: Optional[str] = None     # base name, if a BT headset is used
    reason: str = ""

    def names(self) -> tuple:
        return ((self.mic.name if self.mic else None),
                tuple(sorted(d.name for d in self.loopbacks)))

    def describe(self) -> str:
        mic = self.mic.display_name if self.mic else "none"
        sys_ = ", ".join(d.display_name for d in self.loopbacks) or "none"
        return f"mic: {mic} · system audio: {sys_}"


def _dedupe(devs: List[AudioDevice]) -> List[AudioDevice]:
    seen, out = set(), []
    for d in devs:
        if d.name not in seen:
            seen.add(d.name)
            out.append(d)
    return out


def choose_bluetooth_group(snapshot: DeviceSnapshot,
                           priority_order: Optional[List[str]] = None) -> Optional[DeviceGroup]:
    """First Bluetooth headset connected wins. `priority_order` lists group
    base names in the order they were first seen; unknown groups follow in
    enumeration order."""
    bt = snapshot.bluetooth_groups()
    if not bt:
        return None
    rank = {b: i for i, b in enumerate(priority_order or [])}
    return sorted(bt, key=lambda g: rank.get(g.base, len(rank)))[0]


def resolve_plan(snapshot: DeviceSnapshot,
                 source: str = "both",
                 mic_pref: Optional[str] = None,
                 system_pref: Optional[str] = None,
                 prefer_bluetooth: bool = True,
                 bluetooth_mode: str = "both",
                 priority_order: Optional[List[str]] = None) -> DevicePlan:
    """Decide which devices to record from.

    source          "mic" | "system" | "both"
    mic_pref        device key chosen in Settings, or None/"" for Automatic
    system_pref     loopback key chosen in Settings, or None/"" for Automatic
    prefer_bluetooth / bluetooth_mode ("both" | "system")
                    Automatic choices prefer the first connected BT headset.
    """
    plan = DevicePlan()
    want_mic = source in ("mic", "both")
    want_sys = source in ("system", "both")

    bt = choose_bluetooth_group(snapshot, priority_order) if prefer_bluetooth else None
    reasons = []

    # ── microphone ──
    if want_mic:
        explicit = snapshot.find(mic_pref)
        if explicit is not None:
            plan.mic = explicit
            reasons.append("mic chosen in Settings")
        elif mic_pref:
            reasons.append("chosen mic not connected — using automatic")
        if plan.mic is None:
            if bt is not None and bluetooth_mode == "both" and bt.best_input is not None:
                plan.mic = bt.best_input
                plan.bluetooth_group = bt.base
                reasons.append(f"Bluetooth headset '{bt.label}' (mic)")
            else:
                plan.mic = snapshot.default_input_device()
                if plan.mic is not None:
                    reasons.append("Windows default mic")

    # ── system audio (loopback) ──
    if want_sys:
        explicit = snapshot.find(system_pref)
        if explicit is not None:
            plan.loopbacks = [explicit]
            reasons.append("system audio chosen in Settings")
        else:
            if system_pref:
                reasons.append("chosen system audio not connected — using automatic")
            if bt is not None:
                plan.loopbacks = list(bt.loopbacks)
                plan.bluetooth_group = bt.base
                reasons.append(f"Bluetooth headset '{bt.label}' (system audio)")
            else:
                d = snapshot.default_loopback_device()
                plan.loopbacks = [d] if d else []
                if d:
                    reasons.append("Windows default output")

        # Pairing rule: if the mic is a Bluetooth headset, its audio may move
        # to the Hands-Free output — capture that group's loopbacks too.
        mic_group = snapshot.group_of(plan.mic)
        if mic_group is not None and mic_group.is_bluetooth and explicit is None:
            plan.loopbacks = _dedupe(plan.loopbacks + mic_group.loopbacks)

    plan.reason = "; ".join(reasons)
    return plan


def conflict_warning(snapshot: DeviceSnapshot, mic_pref: Optional[str],
                     system_pref: Optional[str]) -> Optional[str]:
    """Warn about manual choices that would record silence on Bluetooth."""
    mic = snapshot.find(mic_pref)
    sys_ = snapshot.find(system_pref)
    if mic is None or sys_ is None:
        return None
    if mic.base == sys_.base and snapshot.group_of(mic) and snapshot.group_of(mic).is_bluetooth:
        if not sys_.is_hands_free:
            return ("This headset's microphone switches it to Hands-Free mode, "
                    "which silences its stereo output. Recording would miss "
                    "the other participants. Choose \"Automatic\" for System "
                    "Audio (captures both outputs), or the Hands-Free output.")
    return None


# ─── Scanning ───────────────────────────────────────────────────────────

def _creation_flags() -> int:
    return 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def scan_devices(timeout: float = 20.0) -> DeviceSnapshot:
    """Fresh device scan in a separate process (sees hot-plugged devices).
    Falls back to an in-process scan if the helper can't run."""
    try:
        proc = subprocess.run(
            [sys.executable, "-u", PROBE_SCRIPT, "--once"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_creation_flags(),
        )
        line = (proc.stdout or "").strip().splitlines()
        if line:
            return DeviceSnapshot.from_dict(json.loads(line[-1]))
        logger.warning(f"Device probe produced no output: {proc.stderr[:200]}")
    except Exception as e:
        logger.warning(f"Device probe subprocess failed ({e}); scanning in-process")
    return scan_in_process()


def scan_in_process() -> DeviceSnapshot:
    """In-process scan. Only accurate when no other PortAudio instance is open."""
    from src.core.device_probe import scan_fresh
    data = scan_fresh()
    data["scanned_at"] = time.time()
    return DeviceSnapshot.from_dict(data)


def scan_with(p) -> DeviceSnapshot:
    """Scan using an existing PyAudio instance (indices valid for `p`)."""
    from src.core.device_probe import scan_pyaudio
    data = scan_pyaudio(p)
    data["scanned_at"] = time.time()
    return DeviceSnapshot.from_dict(data)


class DeviceWatcher:
    """Runs the probe in --watch mode and reports device changes.

    `on_change(snapshot)` fires (from a background thread) only after the
    device list has been stable for `stable_scans` consecutive scans — a
    Bluetooth headset registers its endpoints one after another over a few
    seconds, and switching halfway through would pick the wrong endpoint.
    """

    def __init__(self, on_change: Callable[[DeviceSnapshot], None],
                 interval: float = 3.0, stable_scans: int = 2):
        self.on_change = on_change
        self.interval = interval
        self.stable_scans = max(1, stable_scans)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.latest: Optional[DeviceSnapshot] = None
        self._reported_sig: Optional[str] = None

    def start(self, initial: Optional[DeviceSnapshot] = None) -> bool:
        if initial is not None:
            self.latest = initial
            self._reported_sig = initial.signature()
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", PROBE_SCRIPT, "--watch", str(self.interval)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                creationflags=_creation_flags(),
            )
        except Exception as e:
            logger.warning(f"Device watcher unavailable: {e}")
            self._proc = None
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="DeviceWatcher")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()   # probe exits when stdin closes
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _read_loop(self) -> None:
        pending_sig, seen = None, 0
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            if self._stop.is_set():
                break
            try:
                snap = DeviceSnapshot.from_dict(json.loads(line))
            except Exception:
                continue
            if snap.error:
                continue
            sig = snap.signature()
            if sig == pending_sig:
                seen += 1
            else:
                pending_sig, seen = sig, 1
            if seen >= self.stable_scans and sig != self._reported_sig:
                self._reported_sig = sig
                self.latest = snap
                try:
                    self.on_change(snap)
                except Exception:
                    logger.exception("Device change handler failed")
            elif self.latest is None:
                self.latest = snap
