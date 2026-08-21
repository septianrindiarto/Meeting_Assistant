# Meeting Scribe — Local Meeting Assistant

**Record or import any meeting → get an accurate transcript → generate polished documents. No bot joins your call. Your audio stays on your device unless you say otherwise.**

Meeting Scribe is a standalone Windows desktop app that listens to meetings (microphone + system audio), transcribes them with Whisper AI — locally or via the free Groq cloud tier — identifies speakers, extracts action items and decisions, and fills your own Word templates to produce Minutes of Meeting, recaps, and reports.

---

## 1. What you need

| Requirement | Notes |
|---|---|
| Windows 10 / 11 (64-bit) | The app uses Windows WASAPI audio capture |
| Python 3.10 or newer | [python.org/downloads](https://www.python.org/downloads/) — tick "Add Python to PATH" during install |
| ~2–4 GB free disk space | For AI models (downloaded on first use) |
| 8 GB+ RAM recommended | 4 GB minimum with the smallest models |
| Internet (optional) | Only for first-time model downloads and optional cloud features |

Optional extras (all free, all skippable):

- **Groq API key** — free cloud transcription, dramatically faster for long meetings ([console.groq.com](https://console.groq.com))
- **Ollama** — local AI summaries & action-item extraction ([ollama.com](https://ollama.com))
- **HuggingFace token** — speaker identification ([huggingface.co/settings/tokens](https://huggingface.co/settings/tokens))
- **MS Word** — needed only for PDF export of generated documents

---

## 2. Installation

Open **PowerShell** and run:

```powershell
cd "D:\Project\Meeting Assistant"

# Create an isolated Python environment (first time only)
python -m venv venv

# Activate it
.\venv\Scripts\Activate.ps1

# Install dependencies (first time only, ~5-10 minutes)
pip install -r requirements.txt

# Optional but recommended: pre-download the Whisper model for offline use
python scripts\download_models.py
```

> If `Activate.ps1` is blocked, run this once, then retry:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### Start the app

```powershell
cd "D:\Project\Meeting Assistant"
.\venv\Scripts\Activate.ps1
python main.py
```

The Meeting Scribe window opens. That's the whole app — everything below happens inside it.

---

## 3. First-time setup (2 minutes)

Open **Settings** (left sidebar) and check three things:

1. **Audio** — pick your microphone from the dropdown. Using a Bluetooth headset? Choose the entry containing **"Hands-Free"** or **"Headset"** (the plain stereo entry is output-only and cannot record). Click **🎤 Test Microphone (3s)** and speak — the meter should move and show "✓ OK". If it shows "No audio detected", pick a different device and test again.

2. **Transcription** — set **Language** if your meetings are mostly one language: `ms` (Bahasa Malaysia), `id` (Indonesian), `en` (English). Leave empty for auto-detect. Leave Quality on **Balanced** and Override Model on **auto** to start.

3. **Transcription Backend** — choose where transcription runs:
   - **Local (Whisper)** — default. Private, works offline, slower for long recordings.
   - **Groq Cloud** — free, dramatically faster (a 2-hour meeting in ~3 minutes), best accuracy. Requires a free API key — see section 7.

Click **💾 Save Settings**.

---

## 4. Recording a meeting

1. Click **Home → 🎙️ New Meeting**, give it a title.
2. A small **recording bar** appears (always on top — drag it anywhere, e.g. a second monitor). It shows:
   - a live waveform and elapsed time
   - **which microphone is actually in use** and a live quality verdict: 🟢 *Good level* / 🟡 *Quiet — move closer* / 🔴 *Silent — mic not picking up*
3. Speak normally. **The transcript appears in the app in real time** as you talk (a few seconds behind — this is a fast draft).
4. Use **⏸️** to pause/resume, **⏹️** to stop.
5. After stopping, click **Process**. This re-transcribes everything with the higher-quality model, adds speaker labels (if enabled), and extracts the summary / action items / decisions (if an AI backend is configured). A progress % and time estimate is shown; click **✕ Cancel** anytime — partial results are kept.
6. Click **Save Bundle**. The meeting is saved and appears on the Home screen.

> ⚠️ **Watch the quality verdict while recording.** If it says *Quiet* or *Silent*, fix it before continuing — no AI model can transcribe audio it can't hear. A cheap wired headset consistently beats Bluetooth for recording quality.

---

## 5. Transcribing an existing file (mp3, mp4, ...)

You don't need to have recorded with the app — transcribe anything:

1. Click **📂 Import Media** (Meeting screen) or **File → Import Audio/Video** (Ctrl+I).
2. Pick your file — supported: `mp3, mp4, m4a, wav, opus, ogg, flac, aac, mkv, webm, mov`.
3. Transcription starts automatically. There is **no need to convert mp4 to mp3 first** — the app decodes the audio track directly (conversion would actually lose quality).
4. When it finishes, generate documents or save the bundle as usual.

---

## 5b. The document flow (what happens when you click Save Bundle)

When you click **Save Bundle**, the app asks **which documents you want** from this meeting:

- ☑ Minutes of Meeting (MoM)
- ☐ FAQ
- ☐ Summary
- ☑ *Generate them now using the app's AI backend*

Whatever you tick, three files are always written into your meetings folder:

| File | Purpose |
|---|---|
| `2026-07-20_Weekly_Sync.mscribe` | The full bundle (audio + transcript + analysis) |
| `2026-07-20_Weekly_Sync.md` | **Readable transcript — no unzipping needed** |
| `2026-07-20_Weekly_Sync.request.md` | What documents you asked for, marked PENDING |

Then one of two things produces your documents:

**Path A — the app writes them (works for everyone, free)**
Leave *"Generate them now"* ticked. The app calls its AI backend (Groq free tier) and the .docx files appear in the folder immediately. No other software needed.

**Path B — your own AI assistant writes them (better quality)**
Untick *"Generate them now"*. The `.request.md` file sits there marked **PENDING**. Any AI assistant with access to the folder — e.g. Claude in Cowork — reads the transcript and produces the documents. Two ways to trigger it:

- Say: *"process pending meeting requests"*
- Or let a **scheduled task** check the folder automatically (hourly), so documents appear without you asking.

The assistant marks the request **DONE** when finished, so nothing is produced twice.

> **Why two paths?** The app is a standalone program — it cannot call an external AI assistant by itself. The request file is the hand-off: the app states what's needed, the assistant fulfils it. Path A needs nothing extra; Path B gives noticeably better writing.

---

## 6. Generating documents

The **Documents** panel (right side) has three buttons, for three ways to turn a meeting into a document:

**Generate from Template** — fills a fixed `.docx` template with the meeting's data. Select a template (the app ships with Minutes of Meeting, Decision Log, Interview Notes, One-on-One Recap and more), click the button, and a `.docx` (plus PDF if MS Word is installed) appears in *Generated Files* — double-click to open. Same layout every time; best for formal formats.

**✨ Ask AI for a Document** — describe any document you want and the AI writes it from the transcript. Pick a preset (formal MoM, MoM in Bahasa, executive summary, follow-up email, client report, decision log) or type your own instruction. Needs an AI backend configured (Groq is free — see section 8). The finished `.docx` opens automatically.

**📄 Export Transcript** — saves the transcript as `.txt` or `.md`. No AI needed, always free. Paste the result into [claude.ai](https://claude.ai) or any AI chat and ask for whatever document you want. This is also the easiest way to get the raw transcript out of a meeting.

### Make your own template

Any Word document becomes a template:

1. Design a `.docx` in Word with your branding and layout.
2. Where you want generated content, type placeholders:

| Placeholder | Produces |
|---|---|
| `{{ meeting.title }}` | Meeting title |
| `{{ meeting.date }}` / `{{ meeting.duration }}` | Date / duration |
| `{{ attendees \| join(', ') }}` | Speaker names |
| `{{ summary }}` | AI executive summary |
| `{%p for d in decisions %}` … `{{ d.description }}` ({{ d.speaker }}) … `{%p endfor %}` | Decision list |
| `{%tr for a in action_items %}` … `{{ a.owner }}`, `{{ a.description }}`, `{{ a.due_date }}` … `{%tr endfor %}` | Action-item table rows |
| `{%p for s in transcript %}` … `{{ s.speaker }}: {{ s.text }}` … `{%p endfor %}` | Full transcript |

3. Go to **Templates → 📥 Import Template** and select your file.
4. Use **🧪 Test Render** to preview it with sample data before a real meeting.

---

## 7. Groq cloud transcription (free, fast, recommended for long meetings)

Local transcription of a 2–3 hour recording takes 1–3 hours of CPU time. The free Groq tier does the same job in minutes with top accuracy.

**Setup (once):**

1. Create a free account at [console.groq.com](https://console.groq.com) → **API Keys** → create a key (starts with `gsk_`).
2. In the app: **Settings → Transcription Backend → Groq Cloud**, paste the key, **Save**.

**How it behaves:**

- Meetings **up to 2 hours**: transcribed in ~2–5 minutes.
- **Longer meetings**: the free tier allows 2 audio-hours per clock-hour (8 hours/day). The app automatically splits your file, sends what it can, shows *"Waiting for quota window — X min until part N..."*, and continues by itself. A 4-hour recording completes in about 1 hour, hands-off.
- **Crash-safe**: every finished part is saved to disk immediately. If the app closes mid-job, just click **Process** again — it resumes where it stopped without re-spending quota.
- **Automatic rollback**: if the cloud is unreachable (no internet, bad key), the app falls back to local Whisper on its own, so you always get a transcript. (Toggle in Settings.)

**Privacy note:** with Groq selected, audio is sent to Groq's servers over TLS. For sensitive meetings, switch the backend to Local — everything then stays on your device.

---

## 8. AI summaries, action items & speaker names (optional)

**Summaries / action items / decisions** need an AI backend (Settings → AI Document Structuring):

- **`groq` — FREE, recommended.** Reuses the same API key as Groq transcription (leave the API Key field empty). Model: `llama-3.3-70b-versatile`. This also powers the "✨ Ask AI for a Document" button and Path A above.
- *Free & fully local*: install [Ollama](https://ollama.com), run `ollama pull llama3.1:8b`, select backend **ollama**.
- *Paid*: **anthropic** (Claude) or **openai** with your own API key (~$0.05–0.15 per meeting). Note: a Claude Pro/Max subscription does **not** include API access — that is billed separately.
- Without a backend, you still get the full transcript — only the Analysis tabs stay empty.

> **Free forever, without any API key:** use **📄 Export Transcript**, then paste the `.md` into [claude.ai](https://claude.ai) (or any AI chat) and ask for whatever document you need.

**Speaker identification** (who said what) — Settings → Speaker Diarization:

1. Install the extras: `pip install torch pyannote.audio`
2. Create a free [HuggingFace](https://huggingface.co) account, accept the terms at [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1), create a token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
3. Paste the token in Settings, tick *Enable speaker identification*, Save.
4. After processing, speakers appear as SPEAKER_00, SPEAKER_01... — rename them in the transcript panel.

---

## 9. Where your data lives

| What | Where |
|---|---|
| Meeting bundles (`.mscribe`) | Your project folder (default: `<app folder>\meetings\`) — change in Settings |
| Companion transcripts (`.md`) | Same folder, next to each bundle |
| Document requests (`.request.md`) | Same folder — PENDING until fulfilled |
| Each bundle contains | audio + transcript + analysis + generated docs + metadata, zipped |
| App settings, models, logs | `%APPDATA%\MeetingScribe\` |
| Search index | `%APPDATA%\MeetingScribe\meetings.db` (rebuilt automatically from bundles if deleted) |

**Moving to a new PC:** point the project folder at a synced location (OneDrive/Dropbox). On the new machine, install the app and select the same folder — your meetings reappear. No account, no cloud lock-in: bundles are ordinary ZIP files with open formats inside.

### Storage & automatic cleanup

Transcription needs a temporary working copy of the audio — an imported mp4/mp3 is decoded to about **115 MB per hour of audio** while it's being processed. Without management this would pile up, so the app cleans up on its own:

- **After Save Bundle** — the audio is now inside the `.mscribe`, so the working copies are deleted automatically (you'll see "freed 340 MB" in the status bar).
- **On startup** — anything orphaned by a crash or force-quit older than 24 hours is purged.
- **Cancel a transcription** — the app asks whether to keep the meeting or discard it and free the temp audio.
- **Manual** — **Settings → Storage** shows a live breakdown (recordings / imports / models / cloud jobs) with a **🧹 Clean Now** button.

Your saved `.mscribe` bundles, transcripts and generated documents are **never** touched by cleanup — only the app's own temp area. Compressed bundles are small (~25 MB per audio-hour). You can adjust the retention window or turn off auto-cleanup in Settings → Storage.

---

## 10. Choosing quality vs speed (cheat sheet)

| Your situation | Recommended setting |
|---|---|
| Everyday use | Backend **Groq Cloud** (free) — fastest and most accurate |
| Confidential meeting | Backend **Local**, Quality **Accurate** (large-v3-turbo) |
| Old / slow PC, quick draft | Backend **Local**, Quality **Fast** |
| No internet | Backend **Local** — everything still works |
| Live draft too laggy while recording | Settings → Live Model → **tiny** or **base**, or untick live transcription |

### Local Whisper models

| Model | Download | RAM needed | Quality | 1h recording takes* |
|-------|----------|-----------|---------|---------------------|
| tiny | 75 MB | 2 GB | Draft only | ~6 min |
| base | 150 MB | 4 GB | Weak for non-English | ~9 min |
| small | 500 MB | 6 GB | Good baseline | ~20 min |
| medium | 1.5 GB | 10 GB | High | ~40 min |
| large-v3 | 3 GB | 16 GB | Highest | ~2 h |
| **large-v3-turbo** | **1.6 GB** | **8 GB** | **Near-highest** | **~30 min** |

*approximate, on a typical 8-core CPU. The app auto-selects based on your hardware and Quality preset; first use of any model downloads it once.

---

## 11. Project structure (for developers)

```
Meeting Assistant/
|-- main.py                    # Entry point
|-- requirements.txt
|-- scripts/
|   |-- download_models.py     # Pre-download Whisper models for offline use
|-- src/
|   |-- core/                  # Business logic
|   |   |-- audio_capture.py   # WASAPI mic + system audio
|   |   |-- transcriber.py     # faster-whisper (local)
|   |   |-- live_transcriber.py# Real-time transcription during recording
|   |   |-- groq_transcriber.py# Groq cloud backend (chunking, resume, quota)
|   |   |-- media_import.py    # mp3/mp4/... direct decode
|   |   |-- diarizer.py        # Speaker identification (optional)
|   |   |-- structurer.py      # LLM summaries/actions + free-form docs
|   |   |-- markdown_docx.py   # Converts AI markdown output to .docx
|   |   |-- template_engine.py # docxtpl rendering
|   |   |-- bundle_manager.py  # .mscribe bundles
|   |   |-- database.py        # SQLite FTS5 search index
|   |   |-- pipeline.py        # Orchestrator
|   |-- ui/                    # PyQt6 interface
|   |-- utils/
|   |   |-- housekeeping.py    # Temp-storage cleanup
|   |   |-- audio_utils.py     # Mixing, resampling, normalization
|   |   |-- file_utils.py      # Paths & safe file I/O
|   |   |-- hardware_probe.py  # Model auto-selection
|-- templates/                 # Starter .docx templates
|-- meetings/                  # Saved bundles + transcripts (git-ignored)
|-- venv/
```

---

## 12. Troubleshooting

**Transcript is empty or nonsense ("cccc...", "thank you for watching")**
The mic wasn't really recording. Check the recording bar's verdict; run 🎤 Test Microphone in Settings; for Bluetooth pick the *Hands-Free* device. Quiet audio is the #1 cause of bad transcripts.

**Bluetooth headset records nothing**
Windows exposes two entries per headset: the stereo one (playback only) and *Hands-Free* (has the mic). Select Hands-Free in Settings → Audio. Note: Windows Bluetooth mics are limited to phone-call quality — a wired/USB mic is noticeably better.

**Bahasa / mixed-language meetings come out wrong**
Set Settings → Transcription → Language to `ms` or `id` explicitly. Avoid *tiny/base* models for non-English speech — use Groq or local *small* and up.

**"Transcribing..." seems stuck**
First use downloads the model (up to 3 GB) — the status bar says so; let it finish once, or pre-download with `python scripts\download_models.py`. During transcription you should see a percentage and ETA. Click ✕ Cancel to stop and keep the partial transcript.

**"ConnectError: getaddrinfo failed"**
No internet while a model download was needed. Pre-download models once with `python scripts\download_models.py`, then everything runs offline.

**Groq errors / quota**
"API key rejected" → re-paste the key. Long files pause automatically for the hourly quota — that's normal, not an error. Daily free cap is 8 audio-hours. Any permanent failure automatically falls back to local Whisper.

**A saved meeting doesn't show on Home**
Click Home again (it refreshes) or use the search bar. If the index is ever corrupted, delete `%APPDATA%\MeetingScribe\meetings.db` — it rebuilds from your bundles.

**Where are the logs?**
`%APPDATA%\MeetingScribe\logs\meeting_scribe.log` — include it when reporting a bug.

---

## 13. Privacy summary

- **Default state:** zero outbound network traffic. Audio, transcripts and documents never leave your PC.
- **Opt-in cloud features** (each off until you configure it): Groq transcription (audio sent to Groq), Claude/OpenAI structuring (transcript text sent), HuggingFace (one-time model download only).
- No account, no telemetry, no vendor lock-in — your data is plain files in a folder you chose.

---

*Meeting Scribe v1.0.0 — built with faster-whisper, PyQt6, pyannote, docxtpl, Groq. Local recording & transcription, real-time draft, mp3/mp4 import, free Groq cloud transcription with resume, AI document generation, and automatic storage cleanup. Runs entirely on your machine unless you choose otherwise. Cost per meeting: $0.*
