# Auto-Editor — Phase 1

Desktop documentary assembler (PySide6, Python 3.12, Windows 10 target).
Two ways to work: **manual timeline** or **script-first auto pipeline**.

## Run (development)

```bash
cd ~/workspace/auto-editor
.venv/bin/python -m auto_editor.app      # needs a display; on headless use xvfb
.venv/bin/python -m auto_editor.smoke   # headless import/model check (CI-safe, no Qt)
```

Windows EXE: GitHub Actions (`.github/workflows/build.yml`) builds
`AutoEditor.exe` with PyInstaller onefile + bundled ffmpeg. Only the EXE is
delivered — the user installs nothing.

## Pipeline panel (script -> video)

1. Paste the script, pick a tone (Deep Dramatic = Charon, the Tariq/Khalid voice),
   write the character brief, choose the work folder.
2. Stages: **Voiceover Banao** (+ Preview tone) -> **Auto Slots** -> **Reference
   Sheet** -> **Photos Generate Karo** -> **Auto Assemble** -> **Render**.
3. STEP-BY-STEP: approve each stage (Approve / Regenerate; Edit = tweak in the
   main window). FULL AUTO: runs straight through to render.

The voiceover is generated chunk-by-chunk via Gemini TTS, so slot timings are
exact — no transcription needed. Photos are generated against the character
reference sheet for consistency (big improvement, not 100% guaranteed).

## Manual mode

- Media bin: drag-drop MP3/WAV (narration), JPG/PNG (photos), MP4 (clips), logo.
- Slot table: each slot = 1 visual on a narration line (graphical timeline = Phase 2).
- Watch folder: Flow-generated photos dropped here are auto-imported on scan.
- Export slot prompts -> markdown with copy-paste visual prompts per slot.
- Clip limits: project setting, max 30 clips / 600s total by default (photos unlimited);
  exceeding shows a warning, render still works.

## Interface contracts (cross-workstream)

Canonical pipeline JSON (`Project.to_contract_dict()` / `from_contract_dict()`):

```json
{
  "version": 1, "name": "...",
  "narration_mp3": "media/voiceover_final.mp3",
  "slots": [{"id":"a1b2","start":0.0,"end":14.2,"text":"...",
             "visual_path":"media/slot_001.png","visual_type":"photo",
             "motion":"zoom-in","transition":"xfade",
             "clip_in":0.0,"clip_out":0.0,"prompt":"..."}],
  "bgm": "media/bgm.mp3",
  "sfx_events": [{"time_sec":7.0,"path":"assets/sfx/sword.mp3","volume":0.7}],
  "logo_path": "media/logo.png",
  "grade_preset": "cinematic",
  "settings": {"max_clips":30,"max_clip_duration_sec":600,"watch_folder":""}
}
```

Media paths are relative to the project dir. `render_project()` accepts this
dict directly (media_dir = project dir).

## Render engine (`render_worker.py`)

- 1920x1080, 30fps, H.264 (CRF 18), AAC 192k, faststart.
- Photos: Ken Burns via `zoompan` (zoom-in/out, pan-left/right), upscaled first
  to avoid jitter. Clips: `-ss`/`-t` subsegments.
- Transitions: `xfade` chains (1s); each non-final segment is extended by the
  fade duration so the wall-clock timeline never shrinks and every transition
  starts exactly on a slot boundary. `cut` breaks the chain (joined safely via
  the concat filter); `fadeblack` currently renders as crossfade (Phase 2).
- Chunks are encoded with closed GOPs (`open-gop=0`) so the final demuxer
  concat (`-c copy`) is safe; intra-chunk joins use the concat filter.
- Grade presets: none / cinematic / teal-orange / war-desat / night-cold
  (applied after transitions, logo is never graded).
- Logo overlay top-right, full duration. Audio: narration dominant, BGM ducked
  via `sidechaincompress`, SFX mixed in, `loudnorm` on master.
- Restart-safe: ~60s video chunks + `manifest.json`; resume skips done chunks.
  Delete `.render_cache/` next to the output if the project changes mid-render.
- ffmpeg lookup: `ffmpeg-bin/` next to the app (bundled in EXE), then system PATH.

## Settings

- Gemini API key **pool** (up to 7 slots). Auto-failover: on 429/quota/5xx the key
  is cooled down (10 min) and the next key is tried. Status dots: green ok /
  orange limited / red failed. Keys are obfuscated in local config, never logged.
- All AI calls use the user's own key(s) — their quota. The app itself works
  offline except AI features and the embedded browser.
- Embedded browser: QtWebEngine with persistent profile (`browser_profile/`) —
  log in once (Google/Flow), session persists.

## AI calls (Phase 1)

- `suggest_slot_splits(script)` -> JSON slot boundaries
- `suggest_visual_prompt(description)` -> image prompt
- `transcribe_audio(mp3)` -> transcript (<=15MB inline; chunked = Phase 2)
- `generate_reference_sheet(brief)` -> character board PNG
- `generate_image(prompt, ref)` -> 16:9 PNG, reference as multimodal input
- `synthesize_chunk(text, tone)` -> WAV via Gemini TTS (voice + style direction)

Phase 2: title/description/tags generator, pacing tips, graphical timeline,
dip-to-black, 4K, render queue, 9:16 vertical, SEO export, thumbnail grabber.

## Test

```bash
cd ~/workspace/auto-editor && .venv/bin/python tests/run_test.py
```

Generates synthetic media (testsrc clips, sine audio, solid PNGs), renders a
~20s project (photo motion + clip segment + xfade + logo + ducked BGM), and
verifies with ffprobe: duration ~20s, 1920x1080, 30fps, audio present.
