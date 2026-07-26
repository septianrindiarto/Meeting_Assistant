"""Quick integration test: record 3 seconds, transcribe, save bundle."""
import sys
import os
import time
sys.path.insert(0, r'd:\Project\Meeting Assistant')

# Test 1: Record a short clip
print("=== Test 1: Audio Capture ===")
from src.core.audio_capture import AudioCaptureEngine
from src.core.models import AudioSource

engine = AudioCaptureEngine(source=AudioSource.BOTH)
engine.start()
print("Recording for 3 seconds...")
time.sleep(3)
chunks = engine.stop()
print(f"Chunks saved: {len(chunks)}")
for c in chunks:
    size = os.path.getsize(c)
    print(f"  {c} ({size} bytes)")

# Test 2: Concatenate and check duration
print("\n=== Test 2: Audio Processing ===")
from src.utils.audio_utils import concatenate_chunks, get_audio_duration, format_duration
from src.utils.file_utils import get_temp_dir

if chunks:
    combined = os.path.join(str(get_temp_dir()), "test_recording.wav")
    concatenate_chunks(chunks, combined)
    dur = get_audio_duration(combined)
    print(f"Combined audio: {format_duration(dur)} ({dur:.1f}s)")
    print(f"File size: {os.path.getsize(combined)} bytes")
else:
    print("No chunks - recording may have failed")

# Test 3: Transcription (only if we have audio)
if chunks:
    print("\n=== Test 3: Transcription ===")
    try:
        from src.core.transcriber import WhisperTranscriber
        t = WhisperTranscriber(model_size="tiny")  # Use tiny for speed
        segments = t.transcribe_file(combined)
        print(f"Segments: {len(segments)}")
        for seg in segments:
            print(f"  [{seg.start:.1f}s-{seg.end:.1f}s] {seg.text}")
    except Exception as e:
        print(f"Transcription error: {e}")

print("\n=== All Tests Complete ===")
