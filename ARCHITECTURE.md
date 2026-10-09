# Auto-Editor Phase 1 — Architecture Plan (for Gemini review)

Desktop PySide6 app (Python 3.12, target Windows 10, dev on Linux).
No network at runtime except: embedded browser (user-driven) and ai_helper (user key).

## Modules
- `auto_editor/__init__.py` — version.
- `auto_editor/timeline.py` — pure-Python data model, no Qt.
  - `Slot`: id, start_sec, end_sec, visual_path, visual_type ('photo'|'clip'),
    motion_preset ('zoom-in'|'zoom-out'|'pan-left'|'pan-right'|'none'),
    transition ('xfade'|'cut'|'fadeblack'), clip_in/clip_out (for clips), prompt (visual prompt text, Flow workflow).
  - `Track`: name, kind in {narration, visuals, bgm, sfx, logo}.
    visuals track holds Slots; narration/bgm/logo hold one media path each; sfx holds SfxEvent{time_sec, path, volume}.
  - `Project`: name, fps=30, width=1920, height=1080,
    settings {max_clips=30, max_clip_duration_sec=600, watch_folder=''},
    tracks dict. `to_dict()/from_dict()`, saved as JSON `.deproj`.
  - `check_clip_limits(project)` -> list of warning strings (clip count / total clip seconds).
- `auto_editor/media.py` — `import_files(paths)` (type detect by extension),
  `scan_watch_folder(folder, known_paths)` -> new files (Flow workflow: user drops
  generated photos in folder, app auto-imports), `export_slot_prompts(project, out_md)`
  (per-slot copy-paste visual prompts markdown).
- `auto_editor/settings_store.py` — JSON config at `<app-data>/config.json`:
  gemini api key stored obfuscated (XOR with app salt + base64, never plaintext),
  browser profile dir path, last project. `get_api_key()/set_api_key()`; key never logged.
- `auto_editor/ai_helper.py` — urllib calls to
  `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent`
  with `x-goog-api-key` header from settings. Phase 1 only:
  (a) `suggest_slot_splits(script_text)` -> JSON list [{start_sec,end_sec,label}] (script has [MM:SS] markers or plain paragraphs; ask model to return JSON only),
  (b) `suggest_visual_prompt(slot_description)` -> prompt string for image tool.
  Timeouts 60s, errors return (ok=False, error msg) — key never in messages. Phase 2: title/desc/tags, pacing tips.
- `auto_editor/browser.py` — `BrowserWindow(QMainWindow)` with QWebEngineView +
  QWebEngineProfile persistent: `profile.setPersistentStoragePath(<app-data>/browser_profile)`,
  `setCachePath`, `setPersistentCookiesPolicy(Allow)`. Phase 1: open URL, back/forward,
  session persists across restarts (one-time Google/Flow login). No automation yet (Phase 2).
  Graceful fallback if QtWebEngine missing (show message, rest of app works).
- `auto_editor/render_worker.py` — headless: `render_project(proj_json, media_dir, out_mp4, progress_cb=None)`.
  - ffmpeg lookup: `<app>/ffmpeg-bin/ffmpeg(.exe)` then system PATH.
  - Video: per-slot segment — photo: `-loop 1 -framerate 30 -t dur -i img` + `scale=3840:-1,zoompan` d=1 s=1920x1080 fps=30 (bake frame count N=dur*30 into z/x/y expressions);
    clip: `-ss in -t dur -i` + scale/pad to 1920x1080, fps=30.
  - Transitions: xfade chain between consecutive segments (offset = cumulative - trans_dur);
    if all 'cut', use concat demuxer path.
  - Logo: `-loop 1 -i logo` overlay at W-w-20:20, scale logo to w=180, full duration.
  - Audio (single fast pass at end): narration as-is; bgm `-stream_loop -1` + `sidechaincompress`
    keyed by narration (duck under voice); sfx events via adelay+amix; final `amix` then `loudnorm` single pass.
  - Restart-safe: split visuals into ~60s chunks -> `chunks/chunk_NNN.mp4` + `manifest.json`;
    resume skips manifest-listed chunks; concat video chunks (re-encode, concat filter), then mux full-length audio.
- `auto_editor/app.py` — MainWindow: dark stylesheet, media bin (drag-drop + file dialog),
  slot list table (Phase 1; graphical timeline in Phase 2), menu: Browser, Settings (API key field),
  Render button -> QThread running render_worker with progress bar + log.

## Questions for review
1. zoompan d=1 with `-loop 1` input: correct pattern for smooth per-frame motion? Jitter pitfalls?
2. xfade chaining math across N segments — recommended robust pattern?
3. Chunk (video-only) + single audio pass + final mux: sound restart-safe strategy?
4. sidechaincompress params for voice-over ducking sanity check.
5. QWebEngine persistent profile: correct calls for cookies/storage persistence?
6. ai_helper: response JSON parsing robustness tips; any security concern with local obfuscated key?
7. Windows packaging gotchas (PySide6 + ffmpeg + WebEngine).
