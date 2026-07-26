"""
Meeting Scribe — Audio Capture Engine
Captures microphone and system loopback audio simultaneously on Windows
using WASAPI. Mixes into a single 16kHz mono stream and writes rolling
chunks to disk for crash safety.

Architecture:
    - MicCaptureThread:    reads from the default mic via sounddevice
    - SystemCaptureThread: reads system audio via PyAudioWPatch (WASAPI loopback)
    - MixerThread:         pulls from both queues, mixes, writes chunks
    - AudioCaptureEngine:  orchestrates all threads, exposes start/pause/stop
"""
from __future__ import annotations

import os
import time
import uuid
import logging
import threading
from queue import Queue, Empty
from typing import Optional, List, Callable

import numpy as np

from src.core.models import AudioSource
from src.utils.audio_utils import (
    resample, mix_to_mono, mix_streams, save_wav_chunk,
    compute_rms_level, concatenate_chunks
)
from src.utils.file_utils import get_recording_temp_dir

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────
TARGET_SAMPLE_RATE = 16000   # Whisper expects 16kHz
CHUNK_DURATION_SEC = 30      # Write a new chunk every 30 seconds
BUFFER_SIZE_FRAMES = 1024    # Audio read buffer size


class MicCaptureThread(threading.Thread):
    """
    Captures audio from the default microphone using sounddevice.
    Pushes (timestamp, audio_float32_mono_16k) tuples into a queue.
    """

    def __init__(self, output_queue: Queue, device_index: Optional[int] = None):
        super().__init__(daemon=True, name="MicCapture")
        self.output_queue = output_queue
        self.device_index = device_index
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially
        # Expose the actual device that ends up open so the UI can show it.
        # If capture fails, this remains None and start_error holds the reason.
        self.active_device_name: Optional[str] = None
        self.start_error: Optional[str] = None

    def run(self):
        try:
            import sounddevice as sd

            # Query device info
            if self.device_index is not None:
                dev_info = sd.query_devices(self.device_index, 'input')
            else:
                dev_info = sd.query_devices(kind='input')

            # Refuse to silently fall back if the requested device has no input.
            if dev_info.get('max_input_channels', 0) <= 0:
                self.start_error = (
                    f"Device '{dev_info.get('name', self.device_index)}' has no "
                    "audio input channels. For Bluetooth headphones, make sure "
                    "you picked the Hands-Free / Headset profile, not the "
                    "stereo (A2DP) output."
                )
                logger.error(self.start_error)
                return

            src_rate = int(dev_info['default_samplerate'])
            channels = min(dev_info['max_input_channels'], 2)
            self.active_device_name = dev_info['name']

            logger.info(
                f"Mic capture OPENED: device='{dev_info['name']}' "
                f"(index={self.device_index}), rate={src_rate}, ch={channels}"
            )

            def callback(indata, frames, time_info, status):
                if status:
                    logger.warning(f"Mic callback status: {status}")
                if not self._pause_event.is_set():
                    return  # paused — discard audio

                # Convert to mono float32
                audio = indata.copy().astype(np.float32).flatten()
                if channels > 1:
                    audio = mix_to_mono(indata.copy().astype(np.float32), channels)

                # Resample to 16kHz
                if src_rate != TARGET_SAMPLE_RATE:
                    audio = resample(audio, src_rate, TARGET_SAMPLE_RATE)

                self.output_queue.put(('mic', time.time(), audio))

            with sd.InputStream(
                device=self.device_index,
                samplerate=src_rate,
                channels=channels,
                dtype='float32',
                blocksize=BUFFER_SIZE_FRAMES,
                callback=callback
            ):
                while not self._stop_event.is_set():
                    self._stop_event.wait(0.1)

        except Exception as e:
            logger.error(f"Mic capture error: {e}", exc_info=True)

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def stop(self):
        self._stop_event.set()


class SystemCaptureThread(threading.Thread):
    """
    Captures system audio (what you hear) via WASAPI loopback
    using PyAudioWPatch. Pushes audio into a shared queue.
    """

    def __init__(self, output_queue: Queue, device_index: Optional[int] = None):
        super().__init__(daemon=True, name="SystemCapture")
        self.output_queue = output_queue
        self.device_index = device_index
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

    def run(self):
        try:
            import pyaudiowpatch as pyaudio

            p = pyaudio.PyAudio()

            # Find WASAPI loopback device
            loopback_device = None

            if self.device_index is not None:
                loopback_device = p.get_device_info_by_index(self.device_index)
            else:
                # Auto-detect: find the default WASAPI loopback
                try:
                    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                except OSError:
                    logger.error("WASAPI not available on this system")
                    p.terminate()
                    return

                default_speakers = p.get_device_info_by_index(
                    wasapi_info["defaultOutputDevice"]
                )

                # Find the loopback version of the default speakers.
                # Bluetooth / USB devices often have decorated names like
                # "Headphones (Sony WF-1000XM5 Stereo)" vs loopback name
                # "Sony WF-1000XM5 Hands-Free", so we use a fuzzy match.
                def _normalize(name: str) -> str:
                    # Strip parentheticals and lowercase for matching
                    base = name.split('(')[0].strip().lower()
                    return base

                default_norm = _normalize(default_speakers["name"])

                for i in range(p.get_device_count()):
                    dev = p.get_device_info_by_index(i)
                    if not dev.get("isLoopbackDevice", False):
                        continue
                    name_norm = _normalize(dev["name"])
                    # Try several matching strategies in order of strictness
                    if (dev["name"].startswith(default_speakers["name"]) or
                            default_norm == name_norm or
                            default_norm in name_norm or
                            name_norm in default_norm):
                        loopback_device = dev
                        logger.info(f"Matched loopback device: {dev['name']}")
                        break

                if loopback_device is None:
                    # Fallback: look for any loopback device
                    for i in range(p.get_device_count()):
                        dev = p.get_device_info_by_index(i)
                        if dev.get("isLoopbackDevice", False):
                            loopback_device = dev
                            logger.info(f"Using fallback loopback: {dev['name']}")
                            break

            if loopback_device is None:
                logger.error("No WASAPI loopback device found")
                p.terminate()
                return

            src_rate = int(loopback_device["defaultSampleRate"])
            channels = loopback_device["maxInputChannels"]

            logger.info(
                f"System capture: device={loopback_device['name']}, "
                f"rate={src_rate}, ch={channels}"
            )

            stream = p.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=src_rate,
                input=True,
                input_device_index=loopback_device["index"],
                frames_per_buffer=BUFFER_SIZE_FRAMES,
            )

            while not self._stop_event.is_set():
                if not self._pause_event.is_set():
                    time.sleep(0.05)
                    continue

                try:
                    raw_data = stream.read(BUFFER_SIZE_FRAMES, exception_on_overflow=False)
                    audio = np.frombuffer(raw_data, dtype=np.float32)

                    # Convert to mono
                    if channels > 1:
                        audio = audio.reshape(-1, channels)
                        audio = mix_to_mono(audio, channels)

                    # Resample to 16kHz
                    if src_rate != TARGET_SAMPLE_RATE:
                        audio = resample(audio, src_rate, TARGET_SAMPLE_RATE)

                    self.output_queue.put(('system', time.time(), audio))

                except Exception as e:
                    logger.warning(f"System audio read error: {e}")

            stream.stop_stream()
            stream.close()
            p.terminate()

        except ImportError:
            logger.error(
                "PyAudioWPatch not installed. System audio capture unavailable. "
                "Install with: pip install PyAudioWPatch"
            )
        except Exception as e:
            logger.error(f"System capture error: {e}", exc_info=True)

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def stop(self):
        self._stop_event.set()


class AudioCaptureEngine:
    """
    Orchestrates mic + system audio capture, mixing, and chunk writing.

    Usage:
        engine = AudioCaptureEngine(source=AudioSource.BOTH)
        engine.on_level_change = my_callback  # (float) -> None
        engine.on_chunk_saved = my_callback    # (str) -> None
        engine.start()
        ...
        engine.pause()
        engine.resume()
        ...
        chunk_paths = engine.stop()
    """

    def __init__(self, source: AudioSource = AudioSource.BOTH,
                 mic_device: Optional[int] = None,
                 system_device: Optional[int] = None):
        self.source = source
        self.mic_device = mic_device
        self.system_device = system_device

        # Session management
        self.session_id = str(uuid.uuid4())[:8]
        self.session_dir = get_recording_temp_dir(self.session_id)

        # Audio queues
        self._mic_queue: Queue = Queue(maxsize=500)
        self._system_queue: Queue = Queue(maxsize=500)

        # Capture threads
        self._mic_thread: Optional[MicCaptureThread] = None
        self._system_thread: Optional[SystemCaptureThread] = None
        self._mixer_thread: Optional[threading.Thread] = None

        # State
        self._is_recording = False
        self._is_paused = False
        self._stop_event = threading.Event()

        # Chunk tracking
        self._chunk_index = 0
        self._chunk_paths: List[str] = []
        self._current_chunk_audio: List[np.ndarray] = []
        self._chunk_start_time: float = 0.0
        self._recording_start_time: float = 0.0
        self._total_samples: int = 0

        # Callbacks
        self.on_level_change: Optional[Callable[[float], None]] = None
        self.on_chunk_saved: Optional[Callable[[str], None]] = None
        # Called with every mixed 16kHz mono block — used by live transcription.
        self.on_audio_chunk: Optional[Callable[[np.ndarray], None]] = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def elapsed_seconds(self) -> float:
        if not self._is_recording:
            return self._total_samples / TARGET_SAMPLE_RATE
        return self._total_samples / TARGET_SAMPLE_RATE

    @property
    def chunk_paths(self) -> List[str]:
        return list(self._chunk_paths)

    @property
    def active_mic_device(self) -> Optional[str]:
        """The actual mic device that ended up open, or None if capture failed."""
        if self._mic_thread is not None:
            return self._mic_thread.active_device_name
        return None

    @property
    def mic_start_error(self) -> Optional[str]:
        """Reason mic capture failed to start, or None if it's running fine."""
        if self._mic_thread is not None:
            return self._mic_thread.start_error
        return None

    def start(self) -> None:
        """Begin audio capture."""
        if self._is_recording:
            logger.warning("Already recording")
            return

        logger.info(f"Starting audio capture (source={self.source.value}, session={self.session_id})")

        self._is_recording = True
        self._is_paused = False
        self._stop_event.clear()
        self._recording_start_time = time.time()
        self._chunk_start_time = time.time()

        # Start capture threads based on source selection
        if self.source in (AudioSource.MIC, AudioSource.BOTH):
            self._mic_thread = MicCaptureThread(self._mic_queue, self.mic_device)
            self._mic_thread.start()

        if self.source in (AudioSource.SYSTEM, AudioSource.BOTH):
            self._system_thread = SystemCaptureThread(self._system_queue, self.system_device)
            self._system_thread.start()

        # Start mixer thread
        self._mixer_thread = threading.Thread(
            target=self._mixer_loop, daemon=True, name="AudioMixer"
        )
        self._mixer_thread.start()

    def pause(self) -> None:
        """Pause audio capture (FR-08)."""
        if not self._is_recording or self._is_paused:
            return
        self._is_paused = True
        if self._mic_thread:
            self._mic_thread.pause()
        if self._system_thread:
            self._system_thread.pause()
        # Save current chunk when pausing
        self._save_current_chunk()
        logger.info("Recording paused")

    def resume(self) -> None:
        """Resume audio capture after pause."""
        if not self._is_recording or not self._is_paused:
            return
        self._is_paused = False
        if self._mic_thread:
            self._mic_thread.resume()
        if self._system_thread:
            self._system_thread.resume()
        self._chunk_start_time = time.time()
        logger.info("Recording resumed")

    def stop(self) -> List[str]:
        """
        Stop recording and return a list of chunk file paths.
        The caller is responsible for concatenating them if needed.
        """
        if not self._is_recording:
            return self._chunk_paths

        logger.info("Stopping audio capture...")
        self._stop_event.set()

        # Stop capture threads
        if self._mic_thread:
            self._mic_thread.stop()
            self._mic_thread.join(timeout=3)

        if self._system_thread:
            self._system_thread.stop()
            self._system_thread.join(timeout=3)

        # Wait for mixer to finish
        if self._mixer_thread:
            self._mixer_thread.join(timeout=3)

        # Save any remaining audio
        self._save_current_chunk()

        self._is_recording = False
        self._is_paused = False

        total_duration = self._total_samples / TARGET_SAMPLE_RATE
        logger.info(
            f"Recording stopped. {len(self._chunk_paths)} chunks, "
            f"{total_duration:.1f}s total, session={self.session_id}"
        )

        return list(self._chunk_paths)

    def _mixer_loop(self) -> None:
        """
        Main mixer loop: pulls audio from mic and system queues,
        mixes them, accumulates into chunks, and periodically saves.
        """
        mic_buffer: List[np.ndarray] = []
        sys_buffer: List[np.ndarray] = []

        while not self._stop_event.is_set():
            # Drain queues
            while True:
                try:
                    source, ts, audio = self._mic_queue.get_nowait()
                    mic_buffer.append(audio)
                except Empty:
                    break

            while True:
                try:
                    source, ts, audio = self._system_queue.get_nowait()
                    sys_buffer.append(audio)
                except Empty:
                    break

            # Mix available audio
            if mic_buffer or sys_buffer:
                mic_audio = np.concatenate(mic_buffer) if mic_buffer else np.array([], dtype=np.float32)
                sys_audio = np.concatenate(sys_buffer) if sys_buffer else np.array([], dtype=np.float32)

                if len(mic_audio) > 0 and len(sys_audio) > 0:
                    mixed = mix_streams(mic_audio, sys_audio)
                elif len(mic_audio) > 0:
                    mixed = mic_audio
                elif len(sys_audio) > 0:
                    mixed = sys_audio
                else:
                    mixed = np.array([], dtype=np.float32)

                if len(mixed) > 0:
                    self._current_chunk_audio.append(mixed)
                    self._total_samples += len(mixed)

                    # Report level for waveform display
                    if self.on_level_change:
                        level = compute_rms_level(mixed)
                        try:
                            self.on_level_change(level)
                        except Exception:
                            pass

                    # Feed live transcription (non-blocking on its side)
                    if self.on_audio_chunk:
                        try:
                            self.on_audio_chunk(mixed)
                        except Exception:
                            pass

                mic_buffer.clear()
                sys_buffer.clear()

            # Check if it's time to save a chunk
            elapsed = time.time() - self._chunk_start_time
            if elapsed >= CHUNK_DURATION_SEC and self._current_chunk_audio:
                self._save_current_chunk()
                self._chunk_start_time = time.time()

            # Small sleep to prevent busy-waiting
            time.sleep(0.02)

    def _save_current_chunk(self) -> None:
        """Save accumulated audio as a WAV chunk file."""
        if not self._current_chunk_audio:
            return

        chunk_audio = np.concatenate(self._current_chunk_audio)
        if len(chunk_audio) == 0:
            return

        chunk_path = os.path.join(
            str(self.session_dir),
            f"chunk_{self._chunk_index:04d}.wav"
        )

        try:
            save_wav_chunk(chunk_audio, chunk_path, TARGET_SAMPLE_RATE)
            self._chunk_paths.append(chunk_path)
            self._chunk_index += 1
            self._current_chunk_audio.clear()

            logger.debug(f"Saved chunk {self._chunk_index - 1}: {chunk_path}")

            if self.on_chunk_saved:
                try:
                    self.on_chunk_saved(chunk_path)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Failed to save chunk: {e}", exc_info=True)

    @staticmethod
    def list_audio_devices() -> dict:
        """
        List available audio input and output devices.
        Returns a dict with 'mic_devices' and 'system_devices' lists.
        """
        devices = {
            'mic_devices': [],
            'system_devices': [],
        }

        # List microphone devices via sounddevice.
        # Include both MME (host API 0, legacy) AND WASAPI — Bluetooth earphones,
        # USB headsets, and modern audio interfaces typically only appear under WASAPI.
        try:
            import sounddevice as sd

            host_apis = sd.query_hostapis()
            wasapi_idx = next(
                (i for i, h in enumerate(host_apis) if 'WASAPI' in h.get('name', '')),
                None
            )
            allowed_apis = {0}
            if wasapi_idx is not None:
                allowed_apis.add(wasapi_idx)

            seen_labels = set()
            for i, dev in enumerate(sd.query_devices()):
                if dev['max_input_channels'] <= 0:
                    continue
                if dev['hostapi'] not in allowed_apis:
                    continue
                api_name = host_apis[dev['hostapi']].get('name', 'Unknown')
                # Append API to the visible name so duplicates across MME/WASAPI
                # are distinguishable in the dropdown.
                label = f"{dev['name']} ({api_name})"
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                devices['mic_devices'].append({
                    'index': i,
                    'name': label,
                    'channels': dev['max_input_channels'],
                    'sample_rate': int(dev['default_samplerate']),
                    'hostapi': dev['hostapi'],
                })
        except Exception as e:
            logger.warning(f"Could not list mic devices: {e}")

        # List WASAPI loopback devices via PyAudioWPatch
        try:
            import pyaudiowpatch as pyaudio
            p = pyaudio.PyAudio()
            for i in range(p.get_device_count()):
                dev = p.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice", False):
                    devices['system_devices'].append({
                        'index': i,
                        'name': dev['name'],
                        'channels': dev['maxInputChannels'],
                        'sample_rate': int(dev['defaultSampleRate']),
                    })
            p.terminate()
        except ImportError:
            logger.warning("PyAudioWPatch not installed — no system audio devices")
        except Exception as e:
            logger.warning(f"Could not list system devices: {e}")

        return devices
