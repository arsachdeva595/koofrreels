# KoofrReels

**Agent-first Instagram Reel generator.** Type a prompt → Claude writes a scene-by-scene storyboard → ElevenLabs voices it → your Koofr clips are fuzzy-matched to each scene → FFmpeg assembles a 1080×1920 MP4 → schedule to Instagram via Publer.

No timeline editor. No manual clip picking. The AI is the director.

---

## How it works

```
Prompt
  └─► Storyboard (Claude)          — scene plan, overlay text, voiceover script
        └─► [APPROVAL GATE]        — edit scenes in the browser before anything renders
              └─► Voiceover (ElevenLabs) — per-scene MP3s concatenated into one track
                    └─► Clip Selection  — fuzzy clip_hint search across your Koofr library
                          └─► Normalize  — parallel FFmpeg: trim → 1080×1920 blur-fill
                                └─► Compose  — concat demuxer stream copy (seconds, not minutes)
                                      └─► Text + Audio — timed overlays, voiceover + music mix
                                            └─► QA Review  — ffprobe codec/resolution/duration check
                                                  └─► Download / Publer push
```

Every stage writes a JSON checkpoint so runs are resumable if interrupted.

---

## Tech stack

| Layer | Choice |
|-------|--------|
| Backend | Python 3.11, FastAPI |
| Video processing | FFmpeg (local) |
| Storyboard + scoring | Anthropic Claude (claude-sonnet-4-6) |
| Voiceover | ElevenLabs REST API |
| AI video generation | Google Vertex AI — Veo 3.1 Lite |
| Prompt safety | Claude Haiku (claude-haiku-4-5) + custom sanitizer |
| Cloud storage | Koofr API |
| Social scheduling | Publer API |
| Gap-fill stock video | Pexels API (optional) |
| Frontend | Vanilla HTML/JS (no build step) |
| Job state | JSON checkpoints on disk |

---

## Prerequisites

- **Python 3.11+**
- **FFmpeg** on your PATH — `ffmpeg -version` should work
- A [Koofr](https://koofr.eu) account with video clips uploaded
- An [Anthropic](https://console.anthropic.com) API key
- An [ElevenLabs](https://elevenlabs.io) API key (for voiceover; optional but recommended)
- A [Publer](https://publer.io) API key (for Instagram scheduling; optional)
- A Google Cloud project with **Vertex AI API** enabled (for AI Reels with Veo3; optional)
  - OAuth2 credentials (`client_id`, `client_secret`, `refresh_token`) with `cloud-platform` scope
  - Veo3 access (currently in allowlist; request at [cloud.google.com/vertex-ai](https://cloud.google.com/vertex-ai))

---

## Installation

### Prerequisites

- **Python 3.11+** — [python.org/downloads](https://www.python.org/downloads/)
- **FFmpeg** on your PATH
  - **Mac:** `brew install ffmpeg`
  - **Windows:** download from [ffmpeg.org](https://ffmpeg.org/download.html), extract, and add the `bin/` folder to your system PATH
  - **Linux:** `sudo apt install ffmpeg`
  - Verify: `ffmpeg -version`

### Setup

```bash
git clone https://github.com/arsachdeva595/koofrreels.git
cd koofrreels

# Create and activate a virtual environment
python -m venv .venv

# Mac / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt

# Start the server
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000** for the dashboard and **http://localhost:8000/settings** to enter your API keys — no `.env` file needed, the settings UI saves everything locally.

---

## Configuration

All settings are editable at `/settings` in the UI. They are saved to `settings.json` and synced into environment variables at startup.

| Setting | Where to get it |
|---------|----------------|
| Koofr email | Your Koofr login email |
| Koofr app password | [app.koofr.net/app/admin/app-passwords](https://app.koofr.net/app/admin/app-passwords) — create a dedicated app password |
| Anthropic API key | [console.anthropic.com/keys](https://console.anthropic.com/keys) |
| ElevenLabs API key | [elevenlabs.io/app/settings/api-keys](https://elevenlabs.io/app/settings/api-keys) |
| ElevenLabs Voice ID | From the ElevenLabs voice library (e.g. `21m00Tcm4TlvDq8ikWAM` = Rachel) |
| Publer API key | Publer → Settings → API |
| Pexels API key | [pexels.com/api](https://www.pexels.com/api/) — only needed if Pexels gap-fill is on |
| Google Cloud Project ID | Your GCP project ID (e.g. `my-project-123`) |
| Google OAuth2 credentials | Client ID, client secret, and refresh token for Vertex AI access |
| Veo3 region | Vertex AI region (e.g. `us-central1`) |

**Brand voice**: Paste your tone-of-voice guidelines in Settings → Brand Voice. Claude uses this when writing overlay text and voiceover scripts. You can disable it per-run using the "Use brand guidelines" toggle in the AI Reels or Stock tabs.

---

## Pipeline stages

| # | Stage | What it does | Approval gate | Artifact |
|---|-------|-------------|:---:|---------|
| 1 | brief | Records your request parameters | — | `reel_brief.json` |
| 2 | storyboard | Claude generates N scenes with visual direction, overlay text, voiceover | — | `storyboard.json` |
| — | **APPROVAL** | You edit overlay text + voiceover per scene in the browser | **Yes** | — |
| 3 | voiceover | ElevenLabs TTS generates per-scene MP3s, concatenated into one track | — | `voiceover/voiceover_full.mp3` |
| 4 | clip_selection | Fuzzy-matches each scene's `clip_hint` to available Koofr clips; downloads + analyzes in parallel | — | `clip_manifest.json` |
| 5 | edit_decisions | Calculates trim in/out points per clip to hit target duration | — | `edit_decisions.json` |
| 6 | compose | Normalizes clips in parallel (blur-fill 9:16), joins with concat demuxer, burns text overlays, mixes audio | — | `output/reel_final.mp4` |
| 7 | final_review | ffprobe checks resolution (1080×1920), codec, duration ±10% | — | `final_review.json` |
| 8 | deliver | File ready for download or Publer push | — | `publish_log.json` |

---

## AI Reels (Veo3)

The **AI Reels** tab bypasses your clip library entirely. Instead of matching scenes to existing footage, every clip is generated from scratch by [Google Veo3](https://cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-model-overview) via Vertex AI. The same storyboard, approval gate, and editing pipeline applies — only clip acquisition is different.

### Pipeline stages (AI Reels)

| # | Stage | What it does |
|---|-------|-------------|
| 1 | brief | Records prompt + duration + reel type |
| 2 | storyboard | Claude writes scenes with visual descriptions, voiceovers, and clip hints — **or** a raw script is parsed directly if Skip Storyboard is on |
| — | **APPROVAL** | Pre-generation policy scan; review flags before spending Veo3 credits |
| 3 | voiceover | ElevenLabs TTS (or skip with toggle) |
| 4 | clip_generation | Parallel Veo3 calls — one MP4 per scene; auto-retry on policy violation |
| 5 | edit_decisions | Trim in/out; character mode trims first 0.5s to skip the keyframe freeze |
| 6 | compose | Same FFmpeg pipeline as standard reels |
| 7 | final_review | ffprobe QA |
| 8 | deliver | Download or Publer push |

### Reel type: Story Driven vs Character Driven

| Mode | What you upload | How it's used |
|------|----------------|--------------|
| **Story Driven** | 1 keyframe image per 5 seconds of target duration | Each image becomes frame 0 of its corresponding scene in Veo3 (image-to-video). Slots update live as you change the duration slider. |
| **Character Driven** | 1 reference image of your character | The same image is sent to every Veo3 call as frame 0, keeping the character visually consistent across all clips. The first 0.5s is trimmed from each clip in the edit stage to remove any freeze artifact. |
| **Text Only** | Nothing | Falls back to pure text-to-video — same as Story/Character with no images uploaded. |

Dynamic slot count for Story mode: the number of keyframe upload slots equals `ceil(duration / 5)`. For a 30s reel that's 6 slots; for a 15s reel, 3 slots. If you upload fewer images than slots, the available ones cycle.

### Policy guardrails

Veo3 enforces [Google's Responsible AI practices](https://ai.google/responsibility/responsible-ai-practices/). Several layers of protection are built in:

**1. Storyboard rules** — Claude's storyboard system prompt for AI Reels includes hard rules:
- `visual_description` and `clip_hint` must be in English
- Never use age/minor terms (`girl`, `young`, `teenage`, `ladki`, `bachi`) — this is the #1 cause of [error 58061214](https://cloud.google.com/vertex-ai), which Google classifies as minor-detection, not violence
- Avoid all other content-policy trigger categories

**2. Prompt sanitizer** (`tools/ai/prompt_sanitizer.py`) — regex ruleset applied to every Veo3 prompt before the API call:
- Covers: weapons, violence/gore, self-harm, sexual content, hate speech, dangerous activities
- Includes Hinglish/transliterated Hindi trigger words (`maar daala`, `khoon`, `tabahi`, `bandook`, etc.)
- Preserves sentence flow and leading capitalisation in replacements

**3. Voiceover sanitisation** — before passing voiceover text to Veo3 as context, Claude Haiku rewrites trigger words in the **same language** (Hinglish in → Hinglish out). No phonetics conversion, no length reduction — only the blocked words are swapped.

**4. Auto-retry on violation** — if a scene fails with a policy error, Claude Haiku rewrites the prompt (rephrasing the blocked concept) and the scene is retried once automatically. The retry clip is named `clip_{n:03d}_veo3_retry.mp4`.

**5. Policy Review tab** — after storyboard approval, a pre-generation scan flags risky scenes in an orange "⚠ Policy Review (N)" tab in the approval modal. Review before spending Veo3 credits.

---

## Reel modes

| Mode | How clips are picked |
|------|---------------------|
| `random` | Shuffles all available Koofr clips; Claude fuzzy-matches each scene's `clip_hint` |
| `folder` | Uses all clips in a specific Koofr folder (set as default in settings) |
| `browse` | You pass explicit Koofr file IDs — useful for hand-curated selections |
| `describe` | Prompt-driven; Claude writes a storyboard and fuzzy-matches clips per scene |

---

## Fuzzy clip matching

The storyboard stage produces a `clip_hint` per scene (e.g. `"beach sunset waves"`). The clip selector scores every available Koofr clip against the hint using a three-tier system:

| Match type | Score per token |
|-----------|:-:|
| Exact token match (`waves` == `waves`) | 2.0 |
| Substring match (`sun` in `sunset`) | 1.2 |
| Sequence similarity ≥ 0.7 (`wavy` ≈ `waves`) | ~0.7 |

The search runs against the **full Koofr path**, not just the filename — so a clip at `summer/beach/waves_crash.mp4` scores for both `beach` and `waves` even if the filename alone only has `waves`. Scores are normalized by hint length. The `selection_reason` field logged to `clip_manifest.json` shows `hint='...' score=X.XX` for every clip picked.

---

## Approval gate

After the storyboard is generated, the pipeline pauses and the browser shows a card for each scene:

- **Scene role** (opening / build-up / peak / outro)
- **Visual description** — what the clip should show
- **Overlay text** — editable, max 8 words
- **Voiceover script** — editable, the line ElevenLabs will speak

For **AI Reels**, a second tab — **⚠ Policy Review** — appears if any scenes contain terms that may trigger Veo3 content filters. Each flagged scene shows the offending word and the policy category. You can edit those scenes before submitting.

Submit the form to resume. The pipeline has a 30-minute timeout at this gate.

---

## Per-run toggles

Available in the generation form for every module:

| Toggle | Modules | Effect when off |
|--------|---------|----------------|
| **Generate voiceover** | All (Reels, Stock, AI Reels, Meme, Audio) | ElevenLabs call is skipped entirely; reel renders with music only |
| **Use brand guidelines** | Stock, AI Reels | Storyboard is written from prompt alone, without your Settings → Brand Voice injected |
| **Skip storyboard — use my own script** | Stock, AI Reels | Claude is skipped entirely; your prompt is parsed as a structured scene script (see below) |

---

## Skip Storyboard mode

Available on **Stock** and **AI Reels** modules. Enable the toggle and paste a fully-written scene script directly into the prompt box. Claude is bypassed entirely — the script is parsed as-is and sent straight to the approval gate.

### Script format

```
[MM:SS - MM:SS] SCENE TITLE[VISUAL] visual direction for the clip [AUDIO] optional sound note NARRATOR: spoken voiceover line
```

Each scene is one block. Fields are read in this order:

| Field | Tag | Used for |
|---|---|---|
| Duration | `[MM:SS - MM:SS]` | Sets `duration_seconds` per scene; sum becomes the reel's total target duration |
| Title | Text between timestamp and `[VISUAL]` | Becomes `overlay_text` (parentheticals stripped, max 6 words); also determines scene `role` |
| Visual | `[VISUAL]` | Sent as the Veo3 prompt (AI Reels) or Pexels search query (Stock) |
| Audio | `[AUDIO]` | Noted but not passed to any API — Veo3 does not accept audio direction |
| Voiceover | `NARRATOR:` | Sent to ElevenLabs TTS |

### Scene roles (auto-detected from title)

| Title contains | Role assigned |
|---|---|
| `HOOK` or first scene | `hook` |
| `AND` | `and` |
| `BUT` | `but` |
| `THEREFORE` | `therefore` |
| `PAYOFF` | `trigger` |
| `OUTRO`, `CLOSE`, or last scene | `outro` |

### Example

```
[0:00 - 0:03] THE "AND" (Visual & Audio Hook)[VISUAL] Cinematic slow macro-glide across thick yellow oil paint on canvas. [AUDIO] Heavy bass drop. NARRATOR: In 1888, Van Gogh painted his Sunflowers, AND the world fell in love.

[0:03 - 0:12] THE "BUT" (The Tension)[VISUAL] High-contrast digital overlay splitting the canvas into Chrome Yellow and zinc gradients. NARRATOR: BUT his secret letters from that week reveal this masterpiece was a psychological trap.

[0:12 - 0:25] THE "THEREFORE" (The Open Loop)[VISUAL] Historical handwritten letters with words like Gauguin and Studio highlighted. NARRATOR: THEREFORE we have to decode his motive — how did he weaponize a single pigment to trick his idol?

[0:25 - 0:35] THE PAYOFF (The Pattern Interrupt)[VISUAL] Jarring zoom-ins on dying, decaying sunflower seed heads. NARRATOR: Van Gogh was painting an artificial paradise out of Chrome Yellow — a pigment known to physically degrade over time.

[0:35 - 0:40] THE OUTRO (Close the Loop)[VISUAL] Camera pulls back, screen fades to black leaving one wilting petal. NARRATOR: The sunflowers were never a symbol of joy. They were a cry for help.
```

The approval gate opens exactly as in normal mode — you can still edit any scene's overlay text or voiceover before the pipeline continues.

> **Stock note:** The `clip_hint` sent to Pexels is auto-extracted from the first 4 meaningful words of each `[VISUAL]` description. If you want a different search term, edit it in the approval gate.

> **AI Reels note:** Veo3 content policy still applies. The policy scan runs on your parsed visual descriptions before the approval gate, and the auto-retry on policy violation still fires if a scene is blocked.

---

## Project artifacts

Every run creates an isolated workspace:

```
projects/
└── proj-{8-char-hex}/
    ├── artifacts/          # JSON outputs: brief, storyboard, clip_manifest, edit_decisions, final_review, publish_log
    ├── checkpoints/        # Stage completion records (resumable)
    ├── clips/              # Downloaded Koofr clips
    ├── voiceover/          # Per-scene MP3s + concatenated voiceover_full.mp3
    ├── tmp/
    │   └── normalized/     # Trimmed + 9:16 blur-filled intermediates
    └── output/
        └── reel_final.mp4  # The finished reel
```

---

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/reel/generate` | Start a new reel job |
| `GET` | `/reel/status/{job_id}` | Poll job progress and stage log |
| `POST` | `/reel/{job_id}/approve` | Submit scene edits at approval gate |
| `GET` | `/reel/download/{job_id}` | Download the finished MP4 |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/api/queue` | List queued reel ideas |
| `POST` | `/api/queue` | Add a reel idea to the queue |
| `DELETE` | `/api/queue/{idea_id}` | Remove a queued idea |
| `POST` | `/api/queue/{idea_id}/run` | Run a queued idea now |
| `POST` | `/api/publer/push` | Upload completed reel to Publer for scheduling |
| `GET` | `/api/settings` | Get current settings (secrets masked) |
| `POST` | `/api/settings` | Update settings |
| `GET` | `/koofr/folders` | List Koofr folders |
| `GET` | `/koofr/clips?path=` | List video clips in a Koofr path |
| `GET` | `/music/tracks` | List local music library |
| `POST` | `/api/ai/upload-image` | Upload keyframe or character reference image for AI Reels |
| `POST` | `/ai/generate` | Start an AI Reels job (Veo3) |
| `GET` | `/ai/status/{job_id}` | Poll AI Reels job progress |
| `POST` | `/ai/{job_id}/approve` | Submit scene edits + policy acknowledgement |
| `GET` | `/ai/download/{job_id}` | Download finished AI reel |

### POST /reel/generate

```json
{
  "mode": "random | describe | browse | folder",
  "target_duration_seconds": 30,
  "koofr_folder": "/My Videos/Summer",
  "prompt": "A day at the beach with golden hour vibes",
  "music_file": "lofi-track.mp3",
  "include_text": true,
  "text_hints": "Keep it warm and nostalgic"
}
```

### POST /reel/{job_id}/approve

```json
{
  "scenes": [
    { "scene": 1, "overlay_text": "Golden hour magic", "voiceover": "Nothing beats a slow summer day." },
    { "scene": 2, "overlay_text": "Just breathe", "voiceover": "Find your wave and ride it." }
  ]
}
```

---

## Project structure

```
koofrreels/
├── backend/
│   ├── main.py               # FastAPI app + all REST endpoints
│   ├── pipeline_runner.py    # 8-stage reel generation pipeline
│   ├── ai_runner.py          # AI Reels pipeline (Veo3 clip generation)
│   ├── job_manager.py        # Job lifecycle + approval gate (threading.Event)
│   ├── queue_manager.py      # Persistent reel idea queue
│   ├── settings_manager.py   # settings.json read/write + env sync
│   └── config.py             # Path constants
├── frontend/
│   ├── index.html            # Dashboard (stage timeline, approval modal, download)
│   └── settings.html         # API key + brand voice configuration
├── tools/
│   ├── base_tool.py          # BaseTool abstract base, ToolResult, enums
│   ├── tool_registry.py      # Auto-discovery
│   ├── ai/
│   │   ├── claude_text_generator.py
│   │   ├── elevenlabs_tts.py
│   │   ├── veo3_client.py        # Google Vertex AI Veo3 REST client (OAuth2 + polling)
│   │   ├── fal_client.py         # fal.ai image generation client
│   │   ├── wavespeed_client.py   # WaveSpeed AI client
│   │   └── prompt_sanitizer.py   # Regex sanitizer — removes Veo3 policy trigger words
│   ├── analysis/
│   │   ├── clip_analyzer.py
│   │   ├── clip_scorer.py
│   │   ├── video_prober.py
│   │   ├── frame_sampler.py
│   │   └── audio_prober.py
│   ├── audio/
│   │   └── audio_mixer.py
│   ├── koofr/
│   │   ├── koofr_browser.py
│   │   └── koofr_downloader.py
│   ├── publer/
│   │   └── publer_client.py
│   └── video/
│       ├── video_normalizer.py
│       ├── video_composer.py
│       ├── video_trimmer.py
│       ├── text_renderer.py
│       └── pexels_downloader.py
├── lib/
│   ├── checkpoint.py         # write_artifact(), write_checkpoint(), get_next_stage()
│   ├── pipeline_loader.py    # YAML pipeline manifest loader
│   └── schema_validator.py   # JSON schema validation
├── skills/
│   ├── meta/
│   │   ├── reviewer.md
│   │   └── checkpoint-protocol.md
│   └── pipelines/reels/
│       ├── brief-director.md
│       ├── clip-selector-director.md
│       ├── edit-director.md
│       ├── text-director.md
│       └── compose-director.md
├── schemas/
│   ├── artifacts/            # JSON schemas for all pipeline artifacts
│   └── checkpoints/
├── pipeline_defs/
│   └── instagram-reel.yaml   # Full pipeline manifest
├── projects/                 # Per-reel workspaces (gitignored)
├── music/                    # Local music library
├── settings.json             # Persisted settings (gitignored)
├── requirements.txt
└── .env.example
```

---

## Troubleshooting

**iPhone MOV "mebx metadata stream" error**
Already handled. The normalizer passes `-ignore_unknown` to FFmpeg and selects only the first audio track (`0:a:0?`), skipping Apple's proprietary metadata streams.

**Compose step is slow**
The composer uses FFmpeg's concat demuxer with stream copy — no re-encoding, no filter_complex. A 6-clip reel should join in under 10 seconds. If you see it hang, check that all normalized clips have the same codec/resolution (they should after the normalization step).

**"Not enough Koofr clips" — falls back to Pexels**
Enable Pexels gap-fill in Settings, or point the tool at a Koofr folder that has more than 2 video files. The default Koofr folder is set in Settings → Koofr Default Folder.

**Settings page is blank**
The settings page is served by the FastAPI server. Make sure `uvicorn` is running and visit `http://localhost:8000/settings` (not a local file path).

**Publer 401 Unauthorized**
Generate a fresh API key from Publer → Settings → Integrations → API. Workspace ID is found in the Publer URL: `app.publer.io/workspaces/{workspace_id}`.

**Storyboard returns no scenes**
Check that your Anthropic API key is saved and valid. The pipeline falls back to a default storyboard (random clips, no text) if the Claude call fails.

**Veo3 error 58061214 — "The prompt could not be submitted"**
This is Google's minor-detection classifier (not violence). The most common triggers in lifestyle/brand content are: `girl`, `young girl`, `college girl`, `teenage`, `ladki`, `bachi`. The storyboard prompt for AI Reels explicitly prohibits these terms. If you still see the error, check the voiceover text — the word may appear there. The Policy Review tab in the approval modal will flag suspected violations before generation.

**Veo3 auto-retry didn't fix the error**
Claude Haiku rewrites the prompt once on a policy violation. If the retry also fails, the scene is marked failed and the job continues with the remaining scenes. Check the approval modal to manually rewrite any scene with a flagged voiceover.

**AI Reels — clip is a frozen image for the first half-second**
This is normal when an image is passed to Veo3 as frame 0. The edit stage trims the first 0.5s of every clip in character mode to skip the freeze. If it's still visible, increase `CHARACTER_TRIM` in `ai_runner.py`.

**AI Reels — "Upload failed" when adding keyframe images**
The upload endpoint saves to `uploads/ai/`. Make sure the `uploads/` directory exists (created automatically at startup) and the server has write permission. HEIC images from iPhone are converted to JPEG automatically.

---

## Roadmap

- [ ] Trim preview in the approval UI (scrub clip before confirming)
- [ ] Re-upload finished reel back to Koofr
- [ ] Scheduled queue auto-run (configurable interval)
- [ ] Multi-user support
- [ ] Instagram direct publish (bypassing Publer)
- [ ] B-roll gap-fill: auto-insert Pexels clips when a scene has no matching Koofr clip
- [ ] AI Reels: live policy scan during storyboard editing (before approval submit)
- [ ] AI Reels: per-scene retry UI — manual prompt edit + re-generate a single failed clip
- [ ] Voiceover preview in approval modal before committing to ElevenLabs credits
- [ ] Character mode: support video reference (not just image) as the character anchor

---

## License

MIT
