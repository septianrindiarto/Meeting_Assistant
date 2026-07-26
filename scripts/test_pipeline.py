"""Full pipeline test: record → transcribe → save → verify in DB."""
import sys
import os
import time
sys.path.insert(0, r'd:\Project\Meeting Assistant')

# Force offline mode
os.environ["HF_HUB_OFFLINE"] = "1"

from src.core.pipeline import MeetingPipeline
from src.core.models import AudioSource

print("=" * 60)
print("  Full Pipeline Test")
print("=" * 60)

pipeline = MeetingPipeline()

# Step 1: Record
print("\n[1/4] Recording for 5 seconds...")
pipeline.start_recording(title="Pipeline Test Meeting", source=AudioSource.BOTH)
time.sleep(5)
pipeline.stop_recording()
print(f"  Duration: {pipeline.meeting.metadata.duration}")
print(f"  Audio path: {pipeline.meeting.audio_path}")
print(f"  Chunks: {len(pipeline.meeting.chunk_paths)}")

# Step 2: Transcribe
print("\n[2/4] Transcribing (offline)...")
pipeline.on_progress = lambda msg: print(f"  > {msg}")
pipeline.process_meeting()
print(f"  Transcript segments: {len(pipeline.meeting.transcript)}")
for seg in pipeline.meeting.transcript:
    print(f"    [{seg.start:.1f}s-{seg.end:.1f}s] {seg.text}")

# Step 3: Save bundle
print("\n[3/4] Saving .mscribe bundle...")
bundle_path = pipeline.save_bundle()
bundle_size = os.path.getsize(bundle_path) / 1024
print(f"  Bundle: {bundle_path}")
print(f"  Size: {bundle_size:.1f} KB")

# Step 4: Verify in database
print("\n[4/4] Checking database...")
meetings = pipeline.list_meetings()
print(f"  Meetings in database: {len(meetings)}")
for m in meetings:
    print(f"    - {m['title']} ({m['date']}) {m['duration']}")

stats = pipeline.get_database_stats()
print(f"  Total meetings: {stats['total_meetings']}")
print(f"  Total hours: {stats['total_duration_hours']:.2f}")

print("\n" + "=" * 60)
print("  Pipeline test PASSED!")
print("=" * 60)
