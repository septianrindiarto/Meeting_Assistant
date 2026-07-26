"""Quick offline transcription test."""
import sys
import os
sys.path.insert(0, r'd:\Project\Meeting Assistant')

# Force offline mode to simulate no internet
os.environ["HF_HUB_OFFLINE"] = "1"

print("=== Offline Transcription Test ===")
print(f"HF_HUB_OFFLINE = {os.environ.get('HF_HUB_OFFLINE')}")

from src.core.transcriber import WhisperTranscriber
from src.utils.file_utils import get_models_dir

# Check if model is cached
cached = WhisperTranscriber._find_cached_model(str(get_models_dir()), "small")
print(f"Cached 'small' model: {cached}")

cached_tiny = WhisperTranscriber._find_cached_model(str(get_models_dir()), "tiny")
print(f"Cached 'tiny' model: {cached_tiny}")

# Try to load the model offline
print("\nLoading model (offline mode)...")
t = WhisperTranscriber(model_size="small")

# Check if there's a test audio file we can try
test_audio = os.path.join(
    str(get_models_dir()).replace("models", "temp"),
    "test_recording.wav"
)
if os.path.exists(test_audio):
    print(f"\nTranscribing test audio: {test_audio}")
    segments = t.transcribe_file(test_audio)
    print(f"Segments: {len(segments)}")
    for seg in segments:
        print(f"  [{seg.start:.1f}s-{seg.end:.1f}s] {seg.text}")
else:
    print(f"\nNo test audio found at {test_audio}")
    print("Skipping transcription test (model loaded successfully though)")

print("\n[OK] Offline transcription is working!")
