"""
Meeting Scribe — Hardware Probe
Detects system capabilities to recommend appropriate Whisper model sizes
and warn about resource constraints.
"""
import os
import platform
import subprocess
import logging

logger = logging.getLogger(__name__)


def get_cpu_cores() -> int:
    """Return the number of physical CPU cores."""
    try:
        count = os.cpu_count()
        return count if count else 2
    except Exception:
        return 2


def get_ram_gb() -> float:
    """Return total system RAM in GB."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        c_ulong = ctypes.c_ulong
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', c_ulong),
                ('dwMemoryLoad', c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullTotalPhys / (1024 ** 3)
    except Exception as e:
        logger.warning(f"Could not detect RAM: {e}")
        return 8.0  # conservative default


def get_available_ram_gb() -> float:
    """Return available (free) system RAM in GB."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        c_ulong = ctypes.c_ulong
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', c_ulong),
                ('dwMemoryLoad', c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullAvailPhys / (1024 ** 3)
    except Exception as e:
        logger.warning(f"Could not detect available RAM: {e}")
        return 4.0


def has_nvidia_gpu() -> bool:
    """Check if an NVIDIA GPU is available (nvidia-smi accessible)."""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_vram_gb() -> float:
    """Return total NVIDIA GPU VRAM in GB. Returns 0 if no GPU."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            # nvidia-smi reports in MiB
            total_mib = float(result.stdout.strip().split('\n')[0])
            return total_mib / 1024.0
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return 0.0


def recommend_whisper_model(quality_preset: str = "balanced") -> str:
    """
    Recommend a Whisper model size based on detected hardware + a quality
    preset. The preset is the user-visible knob; hardware is the safety net.

    Presets:
    - fast      : favor speed — small or below
    - balanced  : best speed/accuracy tradeoff for the hardware (DEFAULT)
    - accurate  : push toward medium/large where possible
    - best      : large-v3 if remotely feasible
    """
    cores = get_cpu_cores()
    ram = get_ram_gb()
    gpu = has_nvidia_gpu()
    vram = get_vram_gb() if gpu else 0.0

    logger.info(
        f"Hardware probe: {cores} cores, {ram:.1f}GB RAM, "
        f"GPU={gpu}, VRAM={vram:.1f}GB, quality={quality_preset}"
    )

    # GPU path — much more headroom, push aggressively
    if gpu and vram >= 6.0:
        if quality_preset in ("best", "accurate") or vram >= 10.0:
            return "large-v3"
        if quality_preset == "fast":
            return "small"
        return "medium"

    # CPU-only path.
    # IMPORTANT: on CPU, large-v3 is a ~3GB first-run download and runs
    # slower than real-time — a 20s clip can take minutes. Never pick it
    # on CPU unless the user explicitly asks for "best".
    if quality_preset == "fast":
        if ram < 4.0 or cores < 4:
            return "tiny"
        return "base"

    # large-v3-turbo dominates on CPU: near large-v3 accuracy at ~8x the
    # speed — better than medium on BOTH axes. Prefer it whenever RAM allows.
    if quality_preset == "best":
        if ram >= 8.0:
            return "large-v3-turbo"
        return "small"

    if quality_preset == "accurate":
        if ram < 8.0:
            return "small"
        return "large-v3-turbo"

    # balanced (default) — small is the sweet spot on CPU:
    # good accuracy, ~500MB download, 3-4x faster than real-time.
    if ram < 4.0 or cores < 4:
        return "base"
    return "small"


def get_system_info() -> dict:
    """Return a summary of system capabilities for display in the UI."""
    gpu = has_nvidia_gpu()
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "cpu_cores": get_cpu_cores(),
        "ram_gb": round(get_ram_gb(), 1),
        "available_ram_gb": round(get_available_ram_gb(), 1),
        "has_nvidia_gpu": gpu,
        "vram_gb": round(get_vram_gb(), 1) if gpu else 0.0,
        "recommended_whisper_model": recommend_whisper_model(),
    }
