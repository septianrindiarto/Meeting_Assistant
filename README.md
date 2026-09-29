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
pip install -r gitignore\requirements.txt

# Optional but recommended: pre-download the Whisper model for offline use
python scripts\download_models.py
```

> If `Activate.ps1` is blocked, run this once, then retry:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

> Downloaded the project from GitHub as a ZIP? Windows extracts it as
> `Meeting_Assistant-main\Meeting_Assistant-main\…` — `cd` into the **inner**
> folder (the one containing `main.py`) before running the commands.

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

1. **Audio** — leave **Microphone** and **System Audio** on **Automatic** (recommended). The app then:
   - uses the **first Bluetooth headset you connect** (🎧 in the lists), otherwise the Windows default devices;
   - notices devices you connect **after** the app started — the lists refresh by themselves every few seconds (↻ forces a re-scan);
   - remembers devices **by name**, so plugging things in or out never changes your choice.

   Choose what a Bluetooth headset is used for: **microphone + system audio** (phone-call quality) or **system audio only** (keeps your laptop/USB mic — better quality). The line **"Right now: A recording would use …"** shows exactly which devices a recording will use. If you pick a combination that would record silence, a red warning explains why.

   Click **🎤 Test audio setup (3 s)** — it records from exactly those devices and shows a separate meter for the **mic** and for **system audio** (play a video while testing to check system audio).

2. **Transcription** — Quality **Balanced** and Override Model **auto** are good defaults. The spoken language is chosen **per meeting** in the Import Wizard; the Language field here is only the default for live recordings.

3. **Transcription Backend** — choose where transcription runs:
   - **Local (Whisper)** — default. Private, works offline, slower for long recordings.
   - **Groq Cloud** — free, dramatically faster (a 2-hour meeting in ~3 minutes), best accuracy. Requires a free API key — see section 7.

Click **💾 Save Settings**.

---

## 4. Recording a meeting

1. Click **Home → 🎙️ New Meeting**, give it a title.
2. A small **recording bar** appears (always on top — drag it anywhere). Its bottom line shows the **mic** and **system audio** in use plus a live verdict: 🟢 *Good level* / 🟡 *Quiet* / 🔴 *Silent*. "(nothing playing)" next to system audio is normal while nobody else is talking.
3. **Devices can change mid-recording — it stays one recording:**
   - Connect a Bluetooth headset → the app switches to it (per your Settings) and shows *"🔄 New device detected — switched to …"*.
   - A device disconnects → it falls back to the laptop devices and shows an amber alert.
   - The mic stops sending audio → the app reconnects it; after repeated failures it moves to another mic (red alert).
   - A switch leaves at most ~1 second of silence; the timer never freezes. Every switch is written into the meeting, so gaps are explained in the transcript file.
4. Speak normally. **The transcript appears in real time** (a fast draft, a few seconds behind).
5. Use **⏸️** to pause/resume, **⏹️** to stop, then **Process** (re-transcribes with the better model; **✕ Cancel** keeps partial results) and **Save Bundle**.

> ⚠️ Bluetooth headsets drop to phone-call quality when their microphone is used. For the best transcripts, use the headset for **system audio only** and a laptop/USB/wired mic for your voice.

---

## 5. Importing recordings (mp3, mp4, …) — the Import Wizard

Click **📂 Import audio / video** on Home, **File → Import Audio/Video** (Ctrl+I), or simply **drag files onto the window**. The app can't know what a recording is about, so a short wizard asks:

| Step | What you confirm |
|---|---|
| **1 · Files** | One file, or **several parts of one meeting** (e.g. a 7-hour meeting saved as 3 videos). Parts are ordered automatically using the recording time stored inside the files, then a date/time in the names, then numbers in the names. If the evidence is unclear, the order shows **"⚠ Order not confirmed"** and you arrange the parts (drag ↑/↓) before continuing. **▶** plays the last 10 s of a part and the first 10 s of the next — the conversation should flow on. |
| **2 · Details** | Title, date, client, engagement/project, meeting type, our role, topic/purpose, participants. |
| **3 · Language** | Spoken language (big accuracy gain for Bahasa) — optionally **🔎 Detect from first 30 seconds**; language the documents should be written in. |
| **4 · Context** | **Key terms / correct spellings** (products, acronyms, companies — fed to speech recognition) and instructions for the documents. |
| **5 · Documents** | Suggested by meeting type: MoM, FAQ, Executive summary, Action tracker, Decision log, Items to confirm with the client, or your own. |
| **6 · Review** | Everything at a glance, with the expected **time, Groq quota and disk space**. Click **Start**. |

Then the app transcribes each part in turn onto **one timeline** (part 2 continues where part 1 ended), joins the audio, and — if *Save automatically* is ticked — saves the meeting and shows **"What's next"**.

- **No conversion needed:** mp4 and mp3 are read directly (converting would only lose quality). Only the audio is kept; the video picture is dropped.
- **Interrupted?** Finished parts are saved as they complete. Import the same files again — or accept the **"Unfinished import — Resume now"** offer when the app starts — and only the remaining parts are transcribed.
- **Long meetings on Groq's free tier** (2 audio-hours per clock-hour, 8 per day): a 7-hour meeting takes ~3 hours of mostly waiting; the app waits and continues on its own.

---

## 5b. The document flow

Documents are **not** written automatically. After a meeting is saved, three files sit in your meetings folder:

| File | Purpose |
|---|---|
| `2026-09-15_Aanwijzing_CIMB.mscribe` | The full bundle (audio + transcript + context) |
| `2026-09-15_Aanwijzing_CIMB.md` | **Readable transcript** with a **Meeting context** section (client, engagement, roles, participants, key terms, instructions, part boundaries, any recording issues) |
| `2026-09-15_Aanwijzing_CIMB.request.md` | The documents you asked for + the same context + writing rules, marked **PENDING** |

**Writing the documents with Claude (Cowork):** Home → **Document queue** → select the meeting → **📋 Copy prompt for Claude** → paste it into Cowork. Claude reads the transcript and context and writes the documents next to them.

**The Document queue** on Home shows every request as ⏳ PENDING / 📝 DRAFTED / ✅ DONE / ⏭ SKIPPED, with buttons to open the transcript or request, mark done, skip, or put back to pending.

**In-app AI (optional):** with a backend configured (Settings → AI Document Structuring, e.g. free Groq), the Save dialog also offers *"Generate them now"* (off by default) and the **✨ Ask AI** button drafts documents inside the app. Treat those as drafts — verify before sharing.

---

## 6. Generating documents

The **Documents** panel (right side) has three buttons, for three ways to turn a meeting into a document:

**Generate from Template** — fills a fixed `.docx` template with the meeting's data. Select a template (the app ships with Minutes of Meeting, Decision Log, Interview Notes, One-on-One Recap and more), click the button, and a `.docx` (plus PDF if MS Word is installed) appears in *Generated Files* — double-click to open. Same layout every time; best for formal formats.

**✨ Ask AI for a Document** — describe any document you want and the AI writes it from the transcript. Pick a preset (formal MoM, MoM in Bahasa, executive summary, follow-up email, client report, decision log) or type your own instruction. Needs an AI backend configured (Groq is free — see section 8). The finished `.docx` opens automatically.

**📄 Export Transcript** — saves the transcript as `.txt` or `.md`. No AI needed, always free. Paste the result into [claude.ai](https://claude.ai) or any AI chat and ask for whatever document you want. This is also the easiest way to get the raw transcript out of a meeting.

**Convert .md → .docx** — documents written as Markdown (e.g. `…_mom.md`) become Word files with one click: Home → Document queue → **Convert .md → .docx (N)**. Runs locally, no AI, no internet. (Command-line equivalent: `python scripts\convert_md_to_docx.py`.)

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
| Document requests (`.request.md`) | Same folder — shown in Home → Document queue |
| Unfinished imports (resume files) | `%APPDATA%\MeetingScribe\import_jobs\` — removed once the meeting is saved |
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

Your saved `.mscribe` bundles, transcripts and generated documents are **never** touched by cleanup — only the app's own temp area. Bundle audio is compressed to Opus (~15–25 MB per audio-hour) — via ffmpeg if installed, otherwise via PyAV, which comes with the app. You can adjust the retention window or turn off auto-cleanup in Settings → Storage.

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
|-- gitignore/requirements.txt # Python dependencies
|-- scripts/
|   |-- download_models.py     # Pre-download Whisper models for offline use
|   |-- convert_md_to_docx.py  # Command-line .md -> .docx conversion
|-- src/
|   |-- core/                  # Business logic
|   |   |-- audio_capture.py   # Capture engine: wall-clock mixer, hot-swap, watchdog
|   |   |-- devices.py         # Device model, Bluetooth grouping, automatic choice
|   |   |-- device_probe.py    # Helper process that sees hot-plugged devices
|   |   |-- media_parts.py     # Media probing, part ordering, previews
|   |   |-- import_plan.py     # Import Wizard answers, estimates, meeting context
|   |   |-- request_queue.py   # Document queue (.request.md status)
|   |   |-- doc_convert.py     # Local .md -> .docx
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
|   |-- ui/                    # PyQt6 interface (import_wizard.py = Import Wizard)
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
The mic wasn't really recording. Check the recording bar's bottom line; run 🎤 Test audio setup in Settings. Quiet audio is the #1 cause of bad transcripts.

**A Bluetooth headset connected after starting the app doesn't appear**
Wait a few seconds (lists refresh automatically) or click ↻. With Microphone/System Audio on **Automatic**, the app also switches to it on its own — even mid-recording.

**Bluetooth headset records nothing / other participants are missing**
Leave **System Audio on Automatic**: when a headset's mic is used, Windows moves its sound to the Hands-Free output and silences the stereo one — Automatic captures both. If you picked devices manually, the red warning in Settings tells you when a combination would record silence. Bluetooth mics are phone-call quality; for your own voice a wired/USB mic is noticeably better.

**The recording bar says "(nothing playing)" for system audio**
Normal while nobody else speaks — Windows sends no system audio when nothing plays. The recording continues.

**Bahasa / mixed-language meetings come out wrong**
Choose the language in the Import Wizard (step 3), add key terms (step 4), and avoid *tiny/base* models for non-English speech — use Groq or local *small* and up.

**An import was interrupted**
Restart the app and choose **Resume now**, or import the same files again — finished parts are reused.

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
