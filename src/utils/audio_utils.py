"""
Meeting Scribe — Audio Utilities
Resampling, mixing, chunk concatenation, and format conversion helpers.
"""
import os
import logging
import struct
import wave
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """
    Resample audio from src_rate to dst_rate using linear interpolation.
    For production quality, consider using scipy.signal.resample, but
    numpy-only approach avoids the scipy dependency.

    Args:
        audio: 1D numpy array of float32 samples.
        src_rate: Original sample rate in Hz.
        dst_rate: Target sample rate in Hz.

    Returns:
        Resampled 1D numpy array of float32 samples.
    """
    if src_rate == dst_rate:
        return audio

    duration = len(audio) / src_rate
    num_samples = int(duration * dst_rate)

    # Create interpolation indices
    indices = np.linspace(0, len(audio) - 1, num_samples)
    resampled = np.interp(indices, np.arange(len(audio)), audio)

    return resampled.astype(np.float32)


def mix_to_mono(audio: np.ndarray, channels: int) -> np.ndarray:
    """
    Convert multi-channel audio to mono by averaging channels.

    Args:
        audio: numpy array of shape (samples,) or (samples, channels).
        channels: Number of channels in the audio.

    Returns:
        1D mono numpy array.
    """
    if channels == 1 or audio.ndim == 1:
        return audio.flatten()

    # Average across channels
    if audio.ndim == 2:
        return audio.mean(axis=1).astype(np.float32)

    # If somehow shaped differently, just flatten
    return audio.flatten().astype(np.float32)


def normalize_audio(audio: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
    """
    Normalize audio to target peak amplitude.

    Args:
        audio: 1D numpy array of float32 samples.
        target_peak: Target maximum absolute amplitude (0.0 to 1.0).

    Returns:
        Normalized audio array.
    """
    peak = np.max(np.abs(audio))
    if peak > 0:
        return (audio / peak * target_peak).astype(np.float32)
    return audio


def normalize_for_whisper(audio: np.ndarray, sample_rate: int = 16000,
                          target_rms: float = 0.1,
                          target_peak: float = 0.95) -> np.ndarray:
    """
    Prepare audio for Whisper: remove DC offset, soft RMS-normalize toward a
    target loudness, and peak-limit so Whisper sees a consistent signal level.

    This is the single biggest accuracy improvement for low-level inputs
    (Bluetooth HFP mics in particular), where Whisper often produces silence
    or hallucinations because the audio sits near the noise floor.

    Args:
        audio: 1D float32 numpy array, range roughly -1.0 to 1.0.
        sample_rate: Sample rate in Hz (unused today, kept for future filters).
        target_rms: Target RMS loudness. 0.1 ~ -20 dBFS, a typical podcast level.
        target_peak: Maximum absolute value after normalization (true-peak cap).

    Returns:
        Normalized 1D float32 numpy array.
    """
    if len(audio) == 0:
        return audio

    # 1) DC offset removal — Bluetooth/HFP audio is notorious for this.
    audio = audio - float(np.mean(audio))

    # 2) RMS-target gain. Push quiet audio up but don't crush already-loud audio.
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms > 1e-5:
        gain = target_rms / rms
        # Clamp the gain so we don't apply +40 dB to a near-silent recording
        # (which would amplify noise to absurd levels).
        gain = min(gain, 10.0)  # +20 dB max
        audio = audio * gain

    # 3) Peak limit. Soft-clip anything that would exceed target_peak.
    peak = float(np.max(np.abs(audio)))
    if peak > target_peak:
        audio = audio * (target_peak / peak)

    return audio.astype(np.float32)


SILENCE_RMS_THRESHOLD = 0.005  # ~ -46 dBFS — treat below this as effectively silent


def mix_streams(mic_audio: np.ndarray, system_audio: np.ndarray,
                mic_volume: float = 1.0, system_volume: float = 1.0) -> np.ndarray:
    """
    Mix microphone and system audio streams into a single mono stream.
    Both inputs must be the same sample rate and mono.

    The previous implementation divided the sum by 2 unconditionally, which
    halved the mic volume whenever the system loopback was silent (i.e. no
    music or call playing). For a Bluetooth HFP mic (already low level), that
    extra -6 dB was enough to make Whisper hallucinate on silence. We now
    only attenuate when both streams carry real signal.

    Args:
        mic_audio: 1D numpy array from microphone.
        system_audio: 1D numpy array from system loopback.
        mic_volume: Volume multiplier for mic (0.0 to 2.0).
        system_volume: Volume multiplier for system audio (0.0 to 2.0).

    Returns:
        Mixed 1D numpy array.
    """
    # Pad shorter array with zeros
    max_len = max(len(mic_audio), len(system_audio))
    if len(mic_audio) < max_len:
        mic_audio = np.pad(mic_audio, (0, max_len - len(mic_audio)))
    if len(system_audio) < max_len:
        system_audio = np.pad(system_audio, (0, max_len - len(system_audio)))

    mic_active = float(np.sqrt(np.mean(mic_audio ** 2))) > SILENCE_RMS_THRESHOLD \
        if len(mic_audio) else False
    sys_active = float(np.sqrt(np.mean(system_audio ** 2))) > SILENCE_RMS_THRESHOLD \
        if len(system_audio) else False

    if mic_active and sys_active:
        # Both speaking — true mix with attenuation to avoid clipping.
        mixed = (mic_audio * mic_volume + system_audio * system_volume) / 2.0
    elif mic_active:
        mixed = mic_audio * mic_volume
    elif sys_active:
        mixed = system_audio * system_volume
    else:
        # Both effectively silent — return their gentle sum, lets VAD skip it.
        mixed = mic_audio * mic_volume + system_audio * system_volume

    # Clip to prevent distortion
    mixed = np.clip(mixed, -1.0, 1.0)

    return mixed.astype(np.float32)


def save_wav_chunk(audio: np.ndarray, filepath: str, sample_rate: int = 16000) -> str:
    """
    Save audio data as a 16-bit PCM WAV file.

    Args:
        audio: 1D numpy array of float32 samples (-1.0 to 1.0).
        filepath: Output file path.
        sample_rate: Sample rate in Hz.

    Returns:
        The filepath that was written.
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Convert float32 to int16
    audio_int16 = (audio * 32767).astype(np.int16)

    with wave.open(filepath, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())

    logger.debug(f"Saved WAV chunk: {filepath} ({len(audio_int16)} samples, {len(audio_int16)/sample_rate:.1f}s)")
    return filepath


def load_wav(filepath: str) -> tuple:
    """
    Load a WAV file and return (audio_float32, sample_rate).

    Args:
        filepath: Path to WAV file.

    Returns:
        Tuple of (numpy array float32, sample_rate int).
    """
    try:
        import soundfile as sf
        audio, sr = sf.read(filepath, dtype='float32')
        return audio, sr
    except ImportError:
        # Fallback to wave module
        with wave.open(filepath, 'rb') as wf:
            sr = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
            return audio, sr


def concatenate_chunks(chunk_paths: List[str], output_path: str,
                       sample_rate: int = 16000) -> str:
    """
    Concatenate multiple WAV chunk files into a single WAV file.

    Args:
        chunk_paths: List of paths to WAV chunks, in order.
        output_path: Path for the combined output file.
        sample_rate: Expected sample rate (all chunks must match).

    Returns:
        The output file path.
    """
    if not chunk_paths:
        raise ValueError("No chunk paths provided")

    existing = [p for p in chunk_paths if p and os.path.exists(p)]
    for p in chunk_paths:
        if p and not os.path.exists(p):
            logger.warning(f"Chunk file not found, skipping: {p}")
    if not existing:
        raise ValueError("No valid audio chunks found")

    # Stream from disk instead of loading everything: a 7-hour recording would
    # otherwise need ~1.6 GB of RAM just to join the pieces.
    return concat_wavs_streaming(existing, output_path, sample_rate)


def concat_wavs_streaming(paths: List[str], output_path: str,
                          sample_rate: int = 16000,
                          block_frames: int = 16000 * 30) -> str:
    """Join 16-bit mono WAV files block by block (constant memory).

    Files with a different sample rate / format are converted in memory
    one at a time (rare — our own chunks and decoded parts are 16 kHz mono).
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with wave.open(output_path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for path in paths:
            with wave.open(path, "rb") as src:
                same = (src.getnchannels() == 1 and src.getsampwidth() == 2
                        and src.getframerate() == sample_rate)
                if same:
                    while True:
                        frames = src.readframes(block_frames)
                        if not frames:
                            break
                        out.writeframes(frames)
                    continue
            audio, sr = load_wav(path)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            if sr != sample_rate:
                audio = resample(audio.astype(np.float32), sr, sample_rate)
            out.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return output_path


def iter_wav_blocks(path: str, block_frames: int = 16000 * 60):
    """Yield float32 mono blocks from a 16-bit WAV without loading it all."""
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        while True:
            raw = wf.readframes(block_frames)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if ch > 1:
                a = a[: len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
            yield a


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate() or 1)


def convert_to_opus(wav_path: str, opus_path: str, bitrate: int = 32000) -> Optional[str]:
    """
    Convert a WAV file to Opus for compact bundle storage (~15-25 MB per hour
    of speech instead of ~115 MB as WAV).

    Tries the ffmpeg command first, then falls back to PyAV (installed with
    faster-whisper), so compression works even when ffmpeg isn't installed.

    Returns the opus path, or None if both methods fail (the caller then
    stores WAV instead).
    """
    import subprocess

    os.makedirs(os.path.dirname(opus_path) or ".", exist_ok=True)
    try:
        # Long meetings take a while to encode — allow ~2 min per audio hour.
        timeout = max(120, int(wav_duration(wav_path) / 3600 * 120) + 60)
    except Exception:
        timeout = 600

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus",
             "-b:a", str(bitrate), opus_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and os.path.exists(opus_path):
            logger.info(f"Converted to Opus (ffmpeg): {opus_path}")
            return opus_path
        logger.warning(f"ffmpeg conversion failed: {result.stderr[-300:]}")
    except FileNotFoundError:
        logger.info("ffmpeg not on PATH — using PyAV for Opus encoding")
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg conversion timed out — trying PyAV")

    return _convert_to_opus_pyav(wav_path, opus_path, bitrate)


def _convert_to_opus_pyav(wav_path: str, opus_path: str, bitrate: int) -> Optional[str]:
    try:
        import av
    except ImportError:
        logger.warning("PyAV not available — storing WAV instead of Opus")
        return None
    try:
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
        # Opus only supports 8/12/16/24/48 kHz; our audio is 16 kHz.
        out_rate = rate if rate in (8000, 12000, 16000, 24000, 48000) else 48000
        container = av.open(opus_path, "w", format="ogg")
        stream = container.add_stream("libopus", rate=out_rate)
        stream.bit_rate = bitrate
        try:
            stream.layout = "mono"
        except Exception:
            pass
        for block in iter_wav_blocks(wav_path, block_frames=rate * 10):
            if rate != out_rate:
                block = resample(block, rate, out_rate)
            pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = out_rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
        container.close()
        logger.info(f"Converted to Opus (PyAV): {opus_path}")
        return opus_path
    except Exception as e:
        logger.warning(f"PyAV Opus encoding failed ({e}) — storing WAV instead")
        try:
            if os.path.exists(opus_path):
                os.remove(opus_path)
        except OSError:
            pass
        return None


class LinearResampler:
    """Streaming linear resampler that keeps its position between blocks.

    The old per-block `resample()` rounded each tiny block's length down,
    losing a fraction of a sample every ~20 ms — about 3-4 seconds of drift
    per hour of recording. This version carries the fractional position and
    the last sample across calls, so the timeline stays exact.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.ratio = self.src_rate / float(self.dst_rate)
        self._pos = 0.0          # read position within the next buffer
        self._tail: Optional[float] = None

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.src_rate == self.dst_rate:
            return x
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        buf = x if self._tail is None else np.concatenate(
            (np.array([self._tail], dtype=np.float32), x))
        n = len(buf)
        if n < 2:
            self._tail = float(buf[-1])
            return np.zeros(0, dtype=np.float32)
        t = np.arange(self._pos, n - 1, self.ratio)
        out = np.interp(t, np.arange(n), buf).astype(np.float32) if len(t) else \
            np.zeros(0, dtype=np.float32)
        next_pos = (t[-1] + self.ratio) if len(t) else self._pos
        # The next buffer starts with this buffer's last sample (index n-1).
        self._pos = next_pos - (n - 1)
        self._tail = float(buf[-1])
        return out


def mix_sources(arrays: List[np.ndarray]) -> np.ndarray:
    """Mix any number of equal-length mono streams.

    Silent sources are ignored (so an idle loopback never halves the mic
    level); when several carry sound they are summed with gentle
    attenuation and clipped.
    """
    if not arrays:
        return np.zeros(0, dtype=np.float32)
    n = max(len(a) for a in arrays)
    padded = [a if len(a) == n else np.pad(a, (0, n - len(a))) for a in arrays]
    active = [a for a in padded
              if len(a) and float(np.sqrt(np.mean(a ** 2))) > SILENCE_RMS_THRESHOLD]
    if not active:
        mixed = np.sum(padded, axis=0) if len(padded) > 1 else padded[0]
    elif len(active) == 1:
        mixed = active[0]
    else:
        mixed = np.sum(active, axis=0) / np.sqrt(len(active))
    return np.clip(mixed, -1.0, 1.0).astype(np.float32)


def get_audio_duration(filepath: str) -> float:
    """
    Get the duration of an audio file in seconds.

    Args:
        filepath: Path to audio file.

    Returns:
        Duration in seconds.
    """
    try:
        import soundfile as sf
        info = sf.info(filepath)
        return info.duration
    except ImportError:
        with wave.open(filepath, 'rb') as wf:
            return wf.getnframes() / wf.getframerate()


def compute_rms_level(audio: np.ndarray) -> float:
    """
    Compute the RMS (Root Mean Square) level of audio.
    Useful for waveform display and level metering.

    Args:
        audio: 1D numpy array.

    Returns:
        RMS level as a float (0.0 to 1.0 for normalized audio).
    """
    if len(audio) == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio ** 2)))


def format_duration(seconds: float) -> str:
    """
    Format seconds into a human-readable duration string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string like "1h 23m 45s" or "5m 30s" or "45s".
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")

    return " ".join(parts)
