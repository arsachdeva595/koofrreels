# I Prompted My Way to a Video Editor: Building KoofrReels in 11 Iterations

*How one "this should be simple" idea turned into an 8-stage AI pipeline — told through the exact prompts that shaped it.*

---

I have almost 2,000 iPhone videos sitting in Koofr. Trips, random golden-hour moments, clips I swore I'd edit into Reels "this weekend." The bottleneck was never the camera. It was always the edit: open Premiere, drag clips, write captions, pick music, export, upload, write a caption again. An hour minimum for a 30-second video.

So I started a conversation with Claude and said: let's build something that does this for me.

What followed was 11 iterations spread across multiple sessions — each one exposing something I hadn't thought about, each one forcing a better design. This is that story, told in the prompts that drove it.

---

## Iteration 1 — The seed

> *"I need to create a system that picks clips (mp4 or MOV) from Koofr's folder. It has an API here: https://app.koofr.net/developers/api"*

The first version was naive in the best way. The idea was simple: list clips from a Koofr folder, download some of them, stitch them together with FFmpeg, output an MP4. No AI. No script. No music. Just: cloud storage in, video out.

It worked. Sort of. It downloaded clips sequentially (slow), composed them without any normalization (different resolutions, wrong aspect ratio), and produced a file that didn't look like an Instagram Reel at all. But it ran end-to-end. That was enough to show the shape of the problem.

The real question was: what should *choose* the clips? And what should *direct* how they're cut?

---

## Iteration 2 — The architecture shift

> *"Check this: https://github.com/calesthio/OpenMontage — AI IS the orchestrator. There is no code orchestrator."*

This was the idea that changed everything. OpenMontage's principle: **don't write an orchestrator in Python. Let the AI be the orchestrator.** Python only holds tools — capabilities with no opinions. The intelligence lives in YAML pipeline manifests and Markdown skills files that Claude reads and follows.

The hierarchy became clear:
- **YAML** = *what* stages exist, in what order, with what tools
- **Markdown skills** = *how* to execute each stage (the director's brief)
- **Python tools** = *dumb capabilities*: browse Koofr, download a file, run FFmpeg, call an API
- **Claude** = the director who reads everything and makes decisions

This meant the pipeline could evolve without touching Python. Want Claude to pick clips differently? Update the skill. Want to add a new stage? Add it to the YAML. The tools stay stable; the intelligence is parameterized.

The tool tiers fell out naturally: SOURCE → ANALYZE → PROCESS → AI → DELIVER. Each tool declares its tier, runtime (local/API), and cost profile. Nothing in Python decides *when* to call what — Claude does.

---

## Iteration 3 — Making it actually usable

> *"I need a settings page. I should be able to select/change the Koofr folder, enter API keys. And connect to Publer for Instagram scheduling."*

The first version had hardcoded credentials in `.env`. That's fine for solo development, terrible for anyone else (or for yourself six months later). The ask was a settings UI — and with it came the requirement to connect the whole output chain: Koofr → KoofrReels → Publer → Instagram.

This produced `settings.html`, `settings_manager.py`, and the Publer client. API keys are masked in the UI (shown as `••••••••`), never overwritten with the placeholder when re-saving, and synced into environment variables at startup so every tool can just read `os.getenv()`.

First real end-to-end: type a prompt, it browsed Koofr, downloaded clips, ran FFmpeg, and returned a link to schedule on Instagram.

---

## Iteration 4 — The UX problem hiding in plain sight

> *"It needs to be responsive. And the first input should be: what we want to make this reel about. Right now it asks for 'text hint (optional)' — didn't understand what that meant."*

This one sounds small. It wasn't. "Text hint (optional)" was the developer's framing for what became the product's core: the **base prompt**. It's what Claude uses to write the entire storyboard. Calling it a "hint" undersold it completely and made users treat it as optional decoration.

Renaming it to the primary input — "What do you want this reel to be about?" — changed how the whole UI was structured. The prompt became the anchor. Everything else (mode, duration, music) was secondary.

The responsive layout fix happened alongside this. The tool needed to work on mobile because that's where you'd use it — lying on the couch, deciding to post something from the clips you shot that morning.

---

## Iteration 5 — The 20-minute silence

> *"Composing reel took 20 minutes. Then nothing. I'm still waiting."*

This was the first real production failure. The pipeline ran. FFmpeg ran. And then... silence. No progress, no error, no output. Just a spinner.

The problem was architectural: the backend had no way to tell the frontend what was happening. FFmpeg was crunching through 8 clips sequentially, each taking 2-3 minutes to normalize, and the UI had no idea.

Two things came out of this:

**Stage progress timeline.** Every pipeline stage now calls `begin_stage()` at the start and `end_stage()` at completion, logging a message and timestamp. The frontend polls every 2 seconds and renders a live timeline with elapsed time per stage.

**The approval gate.** If the pipeline is going to take minutes to render, the user should get to see — and change — what it's going to make *before* it renders. So the pipeline now pauses after the storyboard, presents the full scene plan, and waits for the user to confirm. This turned a black-box automaton into an interactive creative tool.

These two features together were the moment KoofrReels stopped being a script and became a product.

---

## Iteration 6 — iPhone MOVs hate you

> *"Taking too much time and ending at error: Normalize failed for IMG_0382.MOV: Media Metadata Stream #0:5 [0x6] (und): Data: none (mebx / 0x7862656D)"*

Here's a fun fact: every video you shoot on an iPhone contains hidden metadata streams with Apple's proprietary `mebx` format. FFmpeg doesn't know how to decode them. It doesn't crash outright — it just dies partway through normalization with an unhelpful error about a "none" codec.

The fix was two lines:
1. Pass `-ignore_unknown` to FFmpeg so it skips streams it can't handle
2. Change audio stream selection from `0:a?` to `0:a:0?` — take only the first audio track, ignoring the rest

The lesson: real-world files are never as clean as test files. Every production video tool needs escape hatches for the weird stuff. Your users will throw things at it that you never tested.

---

## Iteration 7 — Parallel everything

> *"Too much time taken. Either we have a compression engine after we retrieve, or a better way."*

At this point a 6-clip, 30-second reel was taking 12-15 minutes. That's unusable.

The root cause was sequential normalization. Each clip went through FFmpeg one at a time: download clip 1, normalize clip 1, download clip 2, normalize clip 2... For 8 clips at 90-120 seconds each, that's 12+ minutes of serial work.

The fix: `ThreadPoolExecutor(max_workers=4)` — download 4 clips at once, normalize 4 clips at once. Clips are independent of each other; there's no reason they can't run in parallel.

On top of that: VideoToolbox hardware H.264 encoder on macOS. The system tries `h264_videotoolbox` first (offloads encoding to the media engine chip), falls back to `libx264` with `ultrafast` preset. This also meant bumping normalization from CRF 23 to CRF 28 and switching from `fast` to `ultrafast` preset for intermediates — since these are throwaway files, not the final output.

Result: 12-15 minutes → 2-3 minutes for the same 6-clip reel.

---

## Iteration 8 — The biggest redesign

> *"Every scene should have an overlay. Secondly, I need ElevenLabs for voiceovers. First step after a prompt: storyboard → script (scene by scene) → approval → voiceover creation → picking the clips → rendering."*

This was the iteration that flipped the entire creative model.

The original pipeline was: pick clips → figure out what text to put on them → render. This is how a human editor thinks — you have footage, you work with what you have.

But Claude doesn't work that way. Claude can *write the story first* — before a single clip is downloaded. If you give it a prompt and a target duration, it can write a 5-scene storyboard: each scene's role (opening / build-up / peak / outro), a visual description, the text that should appear on screen, and the voiceover line to speak.

Then — and this is the key shift — you find clips that *match* the story. Not the other way around.

The pipeline order became:
1. Brief
2. **Storyboard** (Claude writes the full scene plan)
3. **Approval** (user edits every scene's text and voiceover)
4. **Voiceover** (ElevenLabs TTS, per-scene, concatenated into one track)
5. Clip selection (fuzzy-match each scene's `clip_hint` to available clips)
6. Edit decisions, compose, review, deliver

ElevenLabs integration was straightforward: POST to `/v1/text-to-speech/{voice_id}` with the script, get back an MP3. Each scene generates its own segment; FFmpeg concatenates them into a single voiceover track that sits under the video.

Per-scene text overlays required tracking timestamps. The compose step now computes when each clip starts and ends in the composed timeline, then burns text with FFmpeg's `drawtext` filter using `enable='between(t,{start},{end})'` — so scene 1's text appears during clip 1, scene 2's text during clip 2, and so on.

---

## Iteration 9 — The compose bottleneck

> *"Still stuck at: Joining 6 clips with transitions... 10m 12s elapsed. Project ID: proj-2916429b."*

After all the parallelization, the compose step was still the bottleneck. VideoToolbox handles H.264 encoding fast — but the FFmpeg `xfade` filter (dissolve, fadeblack, cross-dissolve) is CPU-bound regardless of the output encoder.

Here's why: xfade blends frames. For a dissolve transition at 1080×1920 × 30fps, FFmpeg computes a weighted sum of two frames for every single frame during the transition. That's pure CPU work — VideoToolbox can't help with it. With 5 transitions and a 0.5s overlap each, that's 75 frames of blend computation per transition, times 5 transitions, times the full 1080×1920 resolution. It was burning CPU for 10+ minutes.

The fix: ditch `xfade` entirely.

The primary compose path now uses the **FFmpeg concat demuxer with stream copy** (`-c copy`). Since all normalized clips are already H.264 at 1080×1920 at the same framerate, FFmpeg can join them by copying the stream bytes without decoding or re-encoding a single frame. No filter graph. No blending.

6-clip compose: from 10+ minutes to under 5 seconds.

If stream copy fails (codec mismatch), it falls back to a concat filter with VideoToolbox encoding — still no xfade, just a clean cut. Hard cuts are now a feature, not a limitation.

---

## Iteration 10 — Smarter clip matching

> *"Right now, script generates a clip_hint as tag. I want you to use that to fuzzy search the clips in selected folder to pick the clip for that scene."*

The storyboard stage produces a `clip_hint` per scene — something like `"beach sunset waves"` or `"city night lights"`. The old clip selector split this into words and checked if each word appeared literally in the clip filename. `"beach"` matched `"beach_2023.mp4"` but not `"beachside.mp4"`. `"waves"` matched nothing if the file was named `"ocean_trip.mp4"`.

The new scorer uses three tiers:

| Match type | Score per token |
|-----------|:-:|
| Exact token match | 2.0 |
| Substring match (either direction) | 1.2 |
| difflib sequence similarity ≥ 0.7 | ~0.7 |

More importantly: it searches the **full Koofr path**, not just the filename. A clip at `summer/beach/waves_crash.mp4` scores for `beach` (from the folder name) and `waves` (from the filename) even though neither word appears in the filename alone. Score is normalized by hint length so a 2-word hint and a 4-word hint compete fairly.

The `selection_reason` field in `clip_manifest.json` now logs `hint='beach sunset waves' score=1.85` for every clip — so you can see in the approval UI which scenes found a strong match and which fell back to random.

---

## The final architecture

Ten iterations later, here's the complete picture:

```
User types: "A day at the beach — golden hour, slow pace, nostalgic"

  Stage 1: Brief
  └── Records: mode=describe, duration=30s, prompt, music_file

  Stage 2: Storyboard (Claude API)
  └── Generates: 5 scenes × {visual_description, clip_hint, overlay_text, voiceover}

  [APPROVAL GATE — browser]
  └── User edits: overlay_text + voiceover per scene → submits

  Stage 3: Voiceover (ElevenLabs)
  └── TTS per scene → FFmpeg concat → voiceover_full.mp3

  Stage 4: Clip Selection
  ├── KoofrBrowser: list all clips in selected folder recursively
  ├── Fuzzy match: clip_hint → {exact, substring, difflib} score per clip
  ├── ThreadPoolExecutor(4): download + ffprobe in parallel
  └── Produces: clip_manifest.json (5 clips, with selection_reason + score)

  Stage 5: Edit Decisions
  └── Trim in/out per clip, weighted by scene duration, sum = target ±10%

  Stage 6: Compose
  ├── ThreadPoolExecutor(4): normalize each clip (trim → 1080×1920 blur-fill)
  ├── Concat demuxer stream copy: join 5 clips in < 5 seconds
  ├── TextRenderer: per-scene drawtext with enable='between(t,start,end)'
  └── AudioMixer: duck original, layer voiceover, layer music

  Stage 7: Final Review (ffprobe)
  └── Checks: 1080×1920, H.264, duration within ±10% of target

  Stage 8: Deliver
  └── reel_final.mp4 → download link → optional Publer push to Instagram
```

Every stage writes a checkpoint JSON. If the server restarts mid-pipeline, the run can resume from the last completed stage.

---

## What's next

The tool works. It produces real Reels from real footage with real voiceovers. But there's more to do:

- **Trim preview in approval UI** — scrub each matched clip before confirming it
- **Re-upload to Koofr** — put the finished reel back in cloud storage automatically
- **Scheduled queue** — run the queue automatically on an interval (the infrastructure is already there)
- **Instagram direct publish** — bypass Publer for accounts that support it
- **Clip score display** — show each scene's fuzzy match score in the approval modal so you can swap low-confidence clips before they're downloaded
- **Voiceover preview** — generate a quick ElevenLabs sample in the approval modal before committing

---

## The thing I didn't expect

Going into this, I thought the hard part would be the FFmpeg commands. It wasn't. FFmpeg is well-documented and mostly predictable (iPhone MOV files notwithstanding).

The hard part was the **design question**: who is the creative director? The code? The user? The AI?

Every major iteration was an answer to this question:
- Iteration 2: Claude is the director, Python is the crew
- Iteration 5: The user stays in the loop via an approval gate
- Iteration 8: Claude writes the story *before* choosing footage

That last shift — storyboard first, then find footage to match — changed what the tool is. It's not a clip stitcher with AI sprinkled on top. It's a director that happens to be AI, working with footage that happens to be yours, producing a story that serves the script rather than the script serving the footage.

That insight didn't come from planning. It came from iteration 8, from a prompt that said: *first step should be storyboard → script → approval → voiceover → clips → render.*

Eleven prompts to get there. Worth every one.

---

*KoofrReels is open source. If you have a Koofr account, an Anthropic key, and FFmpeg, you can run it locally today. See the [README](README.md) for setup.*

---

# When the AI Becomes the Cinematographer: Building AI Reels with Google Veo3

*Eleven iterations got us a pipeline that finds and assembles footage you already shot. One session turned it into a studio that generates footage that doesn't exist yet.*

---

After the first eleven iterations, KoofrReels was a solid creative tool. Type a prompt, Claude writes a storyboard, ElevenLabs voices it, the system finds your best-matching Koofr clips, and FFmpeg assembles a finished reel in minutes.

The bottleneck was footage. You could only make what your clip library allowed. Shoot in winter and want a summer beach reel? Not happening. Running a brand that doesn't have product footage yet? Stuck.

So the obvious next question: what if the AI could just *generate* the clips?

---

## Iteration 11 — Connecting Google Veo3

The idea: instead of fuzzy-matching scenes to existing clips, send each scene's visual description to [Google Veo3](https://cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-model-overview) and generate one MP4 per scene from scratch.

Veo3 runs on Vertex AI. The API pattern is a long-running operation: POST the prompt, get back an operation name, poll until complete, download the base64-encoded video. The whole flow wraps into `veo3_client.py` — OAuth2 refresh, submit, poll every 10 seconds, decode the result.

The integration worked on the first real test. A 5-scene, 30-second reel from pure text — no Koofr, no FFmpeg normalization, no clip library needed. Just a prompt and some compute.

That felt significant.

---

## Iteration 12 — Character consistency

The next problem was obvious once generation worked: every clip was visually independent. Scene 1 might show a woman in a blue top. Scene 2 generates a totally different-looking woman. For brand content this is unusable — you need the same face, the same look, across all clips.

Veo3 supports image-to-video: pass an image as `instance["image"]` alongside the prompt, and Veo3 uses it as frame 0. The model anchors the visual style to that reference.

The implementation is simple: base64-encode the uploaded image, attach it to every Veo3 call, and the output clips now start from the same visual anchor. Call this **Character Driven** mode — upload one reference photo, and every scene generates from that same character.

The wrinkle: when you set an image as frame 0, the clip often starts with a brief freeze on that frame before the motion begins. Half a second of frozen image at the start of every clip is visible and jarring. The fix: trim `0.5s` off the front of every character-mode clip in the edit decisions stage. The viewer never sees the freeze; the visual reference still guides the generation.

---

## Iteration 13 — Story-driven keyframes

Character Driven locks the look. But sometimes you want different visuals per scene — just with your own frames as the starting point. That's **Story Driven** mode.

The idea: upload one keyframe image per five seconds of target duration. Each keyframe becomes the first frame of its corresponding scene. For a 30-second reel, that's 6 keyframe slots. Change the duration slider, and the slots update live.

The UI generates slots dynamically based on `ceil(duration / 5)`. Each slot is a small drop-zone with a thumbnail preview. If you upload fewer images than scenes, the available ones cycle (`keyframe_paths[i % len(keyframe_paths)]`). If you upload nothing, it falls back to pure text-to-video.

This gives you a middle ground: your footage, your framing, but AI fills in the motion between frames.

---

## Iteration 14 — The policy wall

Then the real work began.

First test with actual brand content — a lifestyle reel for an Indian women's brand — and almost every scene failed with:

> *"The prompt could not be submitted. This prompt contains sensitive words that violate Google's Responsible AI practices."*

No violent content. No weapons. No explicit material. Just a lifestyle reel. Something was triggering the classifier, and Google's error messages are famously unhelpful.

After methodically testing variations: the trigger was `girl`. Specifically: `young girl`, `college girl`, `girl in her 20s`. Even completely innocent usage. The classifier Google uses for Veo3 includes a **minor-detection** model — error code 58061214 is not a violence flag, it's an age-ambiguity flag. The model can't confirm the person in the prompt isn't a minor.

This is a real problem for Indian lifestyle content. Hinglish scripts use `ladki` (girl/woman) constantly and naturally. Visual descriptions for a women's brand say `young woman` because that's the audience. All of it triggers the same classifier.

The fix: hard rules baked into the storyboard system prompt for AI Reels. Never use `girl`, `teenage`, `young`, `ladki`, `bachi`. Use `woman`, `person`, `she`. The storyboard prompt now says explicitly: *"This is rule #1 — it is the single most common cause of generation failures."*

---

## Iteration 15 — The prompt sanitizer

Fixing the storyboard prompt helped for new generations. But it didn't protect against words that crept into voiceovers and clip hints. And it certainly didn't protect against the full range of Veo3's content filters.

So: `tools/ai/prompt_sanitizer.py`.

A compiled regex ruleset with ~40 rules, applied to every prompt before it hits the Veo3 API. The rules cover English and Hinglish — common transliterations that map to blocked concepts:

- `maar daala` → `outshone` (past tense of "killed" in Hinglish, used colloquially for "crushed it")
- `khoon` → `essence` (blood, but also used figuratively)
- `tabahi` → `transformed` (destruction/havoc, but also used for "went crazy at a party")
- `bandook` → `tool` (gun, but also appears in song lyrics and metaphor)
- `dhamaka` → `burst of energy` (explosion, but common Bollywood hype word)

The sanitizer returns both the cleaned text and a substitution log — so you can see exactly what changed and debug false positives.

---

## Iteration 16 — Voiceover context for better clips

The clips were generating. But they felt disconnected from the voiceover narrative. The visual description described what to show; the voiceover described what to say. Veo3 only knew about the visual side.

Adding voiceover context to the Veo3 prompt makes a meaningful difference. A scene whose visual description says `woman walking through a morning market` paired with `Character says: "Kal se diet, aaj toh chai lo"` generates something much more alive than the visual description alone. The model understands the emotional register — the wry humour, the casual pace.

The implementation: append `Character says: "{dialogue}"` to the Veo3 prompt for any scene that has a non-empty voiceover.

But immediately: this broke things. Hinglish voiceovers passed directly to Veo3 triggered policy violations. `maar daala` in a voiceover — even as pure slang for "totally nailed it" — hit the violence classifier.

---

## Iteration 17 — The language problem (and a wrong turn)

First attempt at fixing the voiceover safety problem: convert Hinglish voiceovers to phonetic English before sending to Veo3. The reasoning: English phonetics carry no semantic content, so nothing would trigger the classifier.

The result was immediately wrong. ElevenLabs uses the same phonetic text for TTS. `"kal se diet"` phonetically rendered as `"kul say dee-yet"` came out completely mangled — pronunciation shifted, the natural Hinglish cadence gone. The voiceovers sounded like a broken text-to-speech system reading English with a fake accent.

> *"The phonetics idea ruined the pronunciation. Let's erase that entirely."*

Complete revert.

The right approach: semantic sanitisation in the **same language**. Claude Haiku rewrites every voiceover in one batch call. The rules: keep the same language, keep the full length, only replace trigger words — and replace them with same-language safe equivalents.

`"maar daala"` → `"jeet liya"` (won it / crushed it — same casual triumph energy, no violence flag)  
`"marne wali thi"` → `"haari si lag rahi thi"` (felt like losing — preserves the emotional contrast)  
`"ladai"` → `"mushkil"` (difficulty — same grammatical role, different register)

The model instructions are explicit: "If Hinglish, output Hinglish. No length limit. Only swap the trigger words."

This worked. The voiceovers sound natural, the policy violations dropped, and ElevenLabs handles the Hinglish pronunciation fine because the text is still Hinglish.

---

## Iteration 18 — Auto-retry on failure

Even with the sanitizer and the storyboard rules, generation failures happen. The policy classifiers are probabilistic — a borderline word at a borderline confidence level will sometimes fail.

The solution: auto-retry once with a rewritten prompt.

If a scene fails with a policy error, Claude Haiku rewrites the prompt. The brief to Haiku: "The following Veo3 prompt was rejected for policy reasons. Write a completely different version that conveys the same visual idea — different words, different framing — safe for Google content policy." 

The rewritten prompt is submitted as a second Veo3 call. If the retry succeeds, the clip is placed in the storyboard at the correct position with a `_retry` suffix on the filename. If the retry also fails, the scene is marked as failed and the job continues with whatever clips succeeded.

This cut the number of manual interventions dramatically. Most single-word failures recover automatically.

---

## Iteration 19 — The Policy Review tab

Auto-retry helps. But if your storyboard has 6 scenes and 4 of them use `girl` variants in the visual descriptions, all 4 will fail, all 4 retries will spend API credits, and you'll end up with an incomplete reel.

Better to catch this before spending credits.

The approval modal now has a second tab: **⚠ Policy Review (N)**. It's only shown when the pre-scan finds something to flag. The scan runs the sanitizer and checks for minor-detection terms across all scenes' visual descriptions, clip hints, and voiceovers.

Each flag shows which scene, which field, and which word was flagged, with the policy category. You can click directly into that scene's edit card and fix it before hitting generate.

For a brand that does a lot of AI content, this is the most valuable feature in the session. The feedback loop goes from: `submit → wait 8 minutes for 6 Veo3 calls → get 4 failures → fix → resubmit → wait 8 more minutes` to: `submit → see 4 orange flags → fix in 2 minutes → generate once → done`.

---

## Iteration 20 — The toggles

Two small features that turned out to matter a lot for workflow:

**Voiceover toggle** — across all five modules (Reels, Stock, AI Reels, Meme, Audio). When off, the ElevenLabs stage is skipped entirely. This is useful for testing, for cost control, and for music-only reels where voiceover doesn't fit the aesthetic. Before this toggle existed, disabling voiceover required editing settings or commenting out code.

**Brand guidelines toggle** — for Stock and AI Reels specifically. When on (default), the storyboard stage injects your Settings → Brand Voice as Claude's context. When off, the storyboard is written purely from the prompt with no brand-voice context. Useful when you're making content for a different brand, or when you want Claude's default creative instincts rather than your trained tone-of-voice.

Both toggles are per-run, not per-session — you don't have to change Settings to use them.

---

## What this day built

Ten iterations over multiple earlier sessions produced a pipeline for existing footage. One day produced a generation engine.

The architectural insight that emerged: **the same pipeline handles both cases.** Storyboard, approval, voiceover, edit decisions, compose, review, deliver — identical stages, identical approval gate, identical FFmpeg output. The only difference is where clip acquisition comes from: Koofr browser vs. Veo3 API.

This wasn't planned. It was a consequence of keeping the pipeline abstract enough that the clip source was just a detail the runner handled. The edit stage doesn't care whether a clip was downloaded from Koofr or generated by an AI. It just trims it.

The harder thing to build wasn't the Veo3 integration. It was learning Google's content policy from the inside out — discovering that `girl` in an innocent lifestyle reel triggers a minor-detection classifier, that Hinglish slang maps to English concepts that trip violence filters, that the only reliable fix is semantic rewriting in the same language by a fast model that understands the intent.

The technical part took hours. The policy research took the rest of the day.

---

*KoofrReels now supports both modes: clip library and AI generation. The README covers setup for both. The tools are in `tools/ai/`.*
