"""
Meeting Scribe — Audio device probe (standalone helper)

Why this exists
---------------
PortAudio (the library behind PyAudioWPatch) reads the list of Windows audio
devices only when it initialises. While ANY stream or PyAudio instance is open
in a process, creating another instance does NOT re-scan — it silently returns
the old list. That is why a Bluetooth headset connected after the app started
never showed up in the Microphone list.

This module can therefore run as a tiny separate process: each scan starts
PortAudio fresh, reads the devices, and shuts it down again, so it always sees
the current hardware — even while the main app is recording.

It deliberately imports nothing from `src` so it can be launched directly:

    python device_probe.py --once           # print one JSON snapshot
    python device_probe.py --watch 3        # print a snapshot every 3 seconds

In --watch mode the probe exits automatically when its parent closes stdin
(i.e. when the app exits or crashes), so it never lingers in the background.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time


def scan_pyaudio(p) -> dict:
    """Enumerate WASAPI devices using an already-created PyAudio instance.

    Returns a plain dict (JSON-serialisable):
        {
          "devices": [ {name, kind, channels, sample_rate, index, is_default}, ...],
          "default_input": str, "default_output": str,
          "error": str | None,
        }
    kind is one of "input", "loopback", "output".
    """
    import pyaudiowpatch as pa  # imported lazily; Windows-only package

    result = {"devices": [], "default_input": "", "default_output": "", "error": None}
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    except OSError:
        result["error"] = "WASAPI is not available on this system"
        return result

    def _name_at(idx):
        try:
            if idx is not None and idx >= 0:
                return p.get_device_info_by_index(idx)["name"]
        except Exception:
            pass
        return ""

    result["default_input"] = _name_at(wasapi.get("defaultInputDevice", -1))
    result["default_output"] = _name_at(wasapi.get("defaultOutputDevice", -1))

    default_loopback = ""
    try:
        default_loopback = p.get_default_wasapi_loopback()["name"]
    except Exception:
        pass

    for i in range(p.get_device_count()):
        try:
            d = p.get_device_info_by_index(i)
        except Exception:
            continue
        if d.get("hostApi") != wasapi.get("index"):
            continue  # WASAPI only: full friendly names, consistent indices

        name = d.get("name", "")
        if d.get("isLoopbackDevice", False):
            kind = "loopback"
            channels = int(d.get("maxInputChannels", 0))
            is_default = bool(default_loopback) and name == default_loopback
        elif int(d.get("maxInputChannels", 0)) > 0:
            kind = "input"
            channels = int(d.get("maxInputChannels", 0))
            is_default = name == result["default_input"]
        elif int(d.get("maxOutputChannels", 0)) > 0:
            kind = "output"
            channels = int(d.get("maxOutputChannels", 0))
            is_default = name == result["default_output"]
        else:
            continue

        if channels <= 0:
            continue

        result["devices"].append({
            "name": name,
            "kind": kind,
            "channels": channels,
            "sample_rate": int(d.get("defaultSampleRate", 48000) or 48000),
            "index": i,
            "is_default": is_default,
        })
    return result


def scan_fresh() -> dict:
    """Start PortAudio, scan, shut it down again."""
    try:
        import pyaudiowpatch as pa
    except ImportError:
        return {"devices": [], "default_input": "", "default_output": "",
                "error": "PyAudioWPatch is not installed"}
    p = pa.PyAudio()
    try:
        return scan_pyaudio(p)
    except Exception as e:  # pragma: no cover - hardware dependent
        return {"devices": [], "default_input": "", "default_output": "",
                "error": f"Device scan failed: {e}"}
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def _exit_when_parent_closes_stdin():
    """Block on stdin; when the parent process goes away the pipe closes."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    os._exit(0)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--watch" in argv:
        try:
            interval = float(argv[argv.index("--watch") + 1])
        except (IndexError, ValueError):
            interval = 3.0
        threading.Thread(target=_exit_when_parent_closes_stdin, daemon=True).start()
        while True:
            snap = scan_fresh()
            snap["scanned_at"] = time.time()
            sys.stdout.write(json.dumps(snap) + "\n")
            sys.stdout.flush()
            time.sleep(max(1.0, interval))

    snap = scan_fresh()
    snap["scanned_at"] = time.time()
    sys.stdout.write(json.dumps(snap) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
