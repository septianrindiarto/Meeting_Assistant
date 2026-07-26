import sys
sys.path.insert(0, r'd:\Project\Meeting Assistant')

from src.core.pipeline import MeetingPipeline
p = MeetingPipeline()
print('Pipeline: OK')

from src.core.audio_capture import AudioCaptureEngine
devs = AudioCaptureEngine.list_audio_devices()
print(f'Mic devices: {len(devs["mic_devices"])}')
print(f'System devices: {len(devs["system_devices"])}')
for d in devs['mic_devices']:
    print(f'  MIC: {d["name"]}')
for d in devs['system_devices']:
    print(f'  SYS: {d["name"]}')
