# AutoEditor v2.0 — Architecture (post M1–M7)

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
  gemini API key slots (MAX_KEYS=7); on Windows stored via DPAPI
  (win32crypt user-scope, "DPAPI1:" prefix, real encryption); elsewhere
  obfuscated (XOR with app salt + base64, NOT encryption — labeled honestly
  via `key_storage_backend()`); legacy blobs still decode (backward
  compatible). Per-slot status {ok|limited|failed, cooldown_until};
  `usable_key_indices()`. Key never logged.
- `auto_editor/provider.py` (M2) — THE centralized provider/key-ring layer.
  `KeyRecord{label, enabled, models?, validation_state, last_success_at,
  last_error_category, rate_limited_until, retry_after_at,
  consecutive_failures}`. `Provider.post(model, payload)`:
  eligible-key selection (enabled + valid + not rate-limited +
  model-compatible, most-reliable first); 429 -> honor Retry-After header,
  else exponential backoff+jitter, mark key rate-limited for the window;
  failover to another key allowed ONLY as resilience — module docstring
  SHARED_QUOTA_NOTICE documents that same-account keys SHARE quota and
  rotation must NEVER be used to evade limits (ToS). Taxonomy: 429 backoff
  path; 401/403 -> mark invalid, never retry, surface "key galat hai";
  400 -> fail fast, no retry; 5xx/network -> bounded backoff+jitter
  (max 5). No eligible key -> `AllKeysExhausted` with Roman Urdu
  user_message + resume_hint (+ resume_at): job layer pauses, progress kept.
  `mask_key()` -> "****abcd"; full keys never logged. Transport injectable;
  default is urllib generateContent POST with Retry-After parsing
  (delta-seconds or HTTP date).
- `auto_editor/cache.py` (M2) — SQLite prompt cache at
  `<app-data>/prompt_cache.sqlite3`: key = sha256(model|prompt|params|
  media path+size+mtime); text as TEXT, images as raw bytes; stats
  hits/saves/entries for the quota dashboard.
- `auto_editor/tts_batch.py` (M2) — `merge_chunks(chunks, max_chars=1200)`:
  merge adjacent short TTS chunks to cut call count (RPM is the binding
  free-tier constraint). Never splits a chunk; documents the coarser-timing
  tradeoff. Wiring into generate_voiceover() is a follow-up (needs UI setting).
- `auto_editor/ai_helper.py` — public AI helpers, all routed through
  `provider.py` + `cache.py` (M2); public names unchanged
  (`call_gemini`, `suggest_slot_splits`, `suggest_visual_prompt`,
  `transcribe_audio`, `generate_image`, `generate_reference_sheet`).
  `call_gemini`/`generate_image` consult the SQLite cache first
  (`use_cache=False` bypasses; explicit api_key bypasses cache).
  `_post_single`/`_post_pool`/`_post_model_pool` kept as shims for
  voiceover_studio + tests. Model refs:
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
- `auto_editor/app.py` — MainWindow: dark stylesheet, top-level view tabs —
  **Workspace** (M3, primary: storyboard + preview monitor + inspector +
  multitrack timeline) and **Classic** (Phase 1 layout: media bin, pipeline
  panel, slot table, render bar). Media bin (drag-drop + file dialog), menu:
  Browser, Settings (API key field), Render button -> QThread running
  render_worker with progress bar + log. `workspace.project_changed` ->
  `_workspace_changed` -> `_refresh_table` keeps both views on the same
  Project object (workspace edits mutate in place; _new/_open/pipeline
  replace the object and rebind).
- `auto_editor/timeline.py` (M3 additions, all Qt-free) — `reorder_slot`
  (swap timing windows of adjacent slots), `trim_slot` (clamped: never
  negative, never overlapping neighbours, MIN_TRIM_DUR=0.5s enforced;
  returns applied timings), `set_slot_field` (validated inspector writes),
  `capture_timeline_state`/`restore_timeline_state`,
  `TimelineHistory` (bounded undo/redo over slot-dict snapshots; ids survive
  restore so selection stays valid).
- `auto_editor/timeline_widget.py` (M3) — `TimelineWidget(QWidget)`, painted
  only, no mutation logic: ruler (adaptive ticks) + VIDEO / NARRATION /
  MUSIC / SFX / CAPTION lanes; block width ∝ duration; click-select
  (slot_selected signal); gold playhead; zoom via px_per_sec; caption lane
  derived honestly from slot labels. Empty project -> honest empty text.
- `auto_editor/workspace.py` (M3) — `MainWorkspace`: storyboard list with
  thumbnails, preview monitor (image shown, clip/missing -> honest text
  label, never a fake frame), transport stepping through slot stills on a
  QTimer (declared in tooltip: no full video playback engine yet),
  properties inspector (start/end with auto-clamp write-back, label, visual
  path + browse/clear, type, transition, motion, clip in/out, prompt with
  focus-checkpointed write-through), timeline in a scroll area + zoom slider,
  move-left/right (honestly disabled at ends), undo/redo (Ctrl+Z /
  Ctrl+Shift+Z). Every control works or is disabled with a tooltip.
- `auto_editor/characters.py` (M4, Qt-free) — `Character` (stable id, name,
  description, ordered reference_paths with primary first, face/hairstyle/
  age_range/clothing/distinctive_features, scene_appearances),
  `CharacterLibrary` (add/remove/get, `merge` with id-collision handling,
  JSON export/import for cross-project reuse, persisted inside
  Project.to_dict/from_dict), `build_character_lock()` (locked
  visual-description block prepended to image prompts),
  `attach_character`/`detach_character`/`detach_character_everywhere`
  (two-sided slot<->library sync), `check_continuity(project)` returning
  {level, rule, scene, message} dicts: unknown-character, no-reference,
  conflicting-notes, missing-prompt, no-introduction (name-mention
  heuristic), unused-character (info). Warnings are surfaced, never
  blocking.
- `auto_editor/timeline.py` (M4) — Slot gains structured scene metadata:
  purpose, narration_segment, characters (ids), visual_prompt, assets,
  transition_intent, continuity_notes. `visual_prompt` and legacy `prompt`
  are one value under two names (properties, same storage); to_dict writes
  both keys, from_dict prefers the new one. Project persists the
  `CharacterLibrary`; contract dict carries characters + scene metadata
  (additive). Old project files load with sensible defaults.
- `auto_editor/auto_pipeline.py` (M4) — `build_scene_prompt()` (first
  attached character's primary reference + lock block, else plain prompt),
  `generate_scene_visual()` (exactly one `ai_helper.generate_image` call
  for ONE scene, updates slot.visual_path, honest failure when the slot has
  no visual and no work_dir). Both auto pipelines register the reference
  sheet as a library Character and attach it to every scene, so photos are
  generated with reference-image conditioning + lock block.
- `auto_editor/workspace.py` (M4) — Characters panel under the storyboard
  (list with scene counts + missing-image flag, ＋ Add via dialogs, → Scene
  attach, ✕ Unlink, 🖼 Set image… replacing primary, 🗑 Remove with
  confirm + scene unlink; all honestly disabled without project/character/
  slot selection), continuity warning badge (⚠ N + tooltip) in the
  storyboard header, inspector "Scene characters" row, and
  ⟳ Regenerate scene visual (QThread worker, disabled without API key or
  visual path, never a fake button).

- `auto_editor/editing.py` (M5, Qt-free) — `decide_edit(slot, prev_slot,
  next_slot, project)` -> `EditDecision(transition, motion_preset,
  hold_factor, grade_hint, notes)`. Content-derived rules in priority
  order: END (end/conclusion/epilogue -> dip-to-black, hold 1.2, no
  motion) > ACTION (battle/scream/... -> hard cut, zoom-in, hold 0.9) >
  QUIET (dawn/prayer/silence/... -> gentle xfade, or fade-in when opening,
  slow zoom-out or still, hold 1.25) > CHANGE (new characters on screen or
  meanwhile/new-location cues -> gentle xfade) > OPEN (first slot, neutral
  -> fade-in from black) > CONTINUE (same moment -> hard cut, restrained)
  > DEFAULT (gentle xfade). Keywords cover English + Urdu-script +
  transliterated forms. Deterministic (stable md5 tie-breaks, no
  randomness); never cycles a fixed preset order. `apply_editing_decisions
  (project)` stores transition/motion/hold/grade_hint/edit_notes on every
  slot. grade_hint is advisory (a GRADE_PRESETS key); the renderer still
  applies the project-level grade — per-slot grading is a known limitation.
- `auto_editor/timeline.py` (M5) — TRANSITIONS gains fade-in, fade-out,
  dip-to-black ("fadeblack" kept as the legacy alias, renders identically).
  Slot gains hold_factor (clamped 0.5-2.0), grade_hint, edit_notes; all
  three persist in to_dict/from_dict and the contract dict (additive) and
  are inspector-editable via set_slot_field (hold clamped, enums
  validated).
- `auto_editor/render_worker.py` (M5) — genuine per-slot transitions:
  `RENDERABLE_TRANSITIONS`; `_check_transitions()` raises RuntimeError on
  anything unrenderable (never silently substituted). `_split_runs()`
  groups xfade chains and computes head/tail fades per run boundary:
  cut -> hard concat; fade-in -> fade=t=in at run head; fade-out ->
  fade=t=out at previous run tail; dip-to-black/fadeblack -> both
  (1s out + 1s in, pixel-only, wall-clock preserved). `_segment_chain()`
  bakes the fade filters into the segment; `_motion_filter()` takes an
  intensity derived from hold_factor (quiet = gentler zoom). Chunked
  restart-safe architecture unchanged.
- `auto_editor/auto_pipeline.py` (M5) — the fixed `MOTION_CYCLE`
  zoom-preset rotation is DELETED (brief §6 ban); both auto pipelines call
  `apply_editing_decisions(project)` after slot creation.
- `auto_editor/workspace.py` (M5) — Inspector transition dropdown picks up
  the new TRANSITIONS automatically; new "Kyun:" row shows the decision's
  honest reasoning (`edit_notes`); a manual transition/motion override
  rewrites the note to "Manual choice ..." so the UI never shows stale
  auto reasoning. Timeline transition tick marks: cut = no tick, xfade =
  gold, others = dim gold (timeline_widget.py).
- `auto_editor/captions.py` (M6) — Qt-free caption engine. `CaptionCue`
  (id/start/end/text, JSON-serializable). `generate_cues(project)`: splits
  each slot's `narration_segment` (fallback: label) into phrase-level cues
  at sentence/punctuation boundaries (Latin + Urdu `۔ ؟`), distributes the
  slot window proportionally by character count — DOCUMENTED approximation
  (no word-level ASR timestamps; every cue is user-editable). `validate_cues`
  flags overlap (error), gap >0.75s (warning), out-of-bounds (error),
  outside-slots (warning), empty text (error). Edit ops: `shift_cue`
  (duration-preserving, clamps at 0), `split_cue` (word nearest the time
  fraction), `merge_cues`, `edit_cue`. `to_srt`/`parse_srt` round-trip.
  `CaptionStyle` (font/size/bold/color/outline/shadow/bg-box/position/
  margins/alignment/highlight-words) + 3 data templates (cinematic, clean,
  bold-pop); `to_ass` generates a real ASS file for the libass `subtitles`
  filter (colors as &HAABBGGRR, BorderStyle 3 for the bg box, per-word
  highlight override tags). `cue_hash` for cache invalidation.
- `auto_editor/timeline.py` (M6) — `Project` gains `caption_cues`
  (list[CaptionCue]), `caption_style` (dict), `mixer` (dict, cleaned by
  `_clean_mixer`: vols clamped 0..3, ducking 0..1); all three serialized in
  `to_dict`/`from_dict` and the additive contract format (pre-M6 files ->
  defaults; garbage -> defaults).
- `auto_editor/render_worker.py` (M6) — `_render_audio(..., mixer=None)`:
  mixer volumes wired (defaults reproduce the legacy mix bit-identically:
  narration 1.8, bgm 0.4, sfx neutral multiplier, ducking 1.0 -> sidechain
  ratio 6; ducking 0 disables the sidechain). Captions stage before mux:
  SRT always written next to the MP4 when cues exist (stale SRT deleted when
  they don't); burn-in merged INTO the mux pass via
  `-vf subtitles=<escaped ASS>` (no extra re-encode) when
  `_has_subtitles_filter()` (cached, honest probe) is true; otherwise a
  WARN goes to render.log, burn-in is skipped, SRT still ships. Caption
  changes reuse cached video chunks (captions are chunk-independent).
- `auto_editor/timeline_widget.py` (M6) — CAPTION lane draws real cue blocks
  when `project.caption_cues` is non-empty; otherwise the honest dimmed
  slot-label fallback.
- `auto_editor/workspace.py` (M6) — bottom area is now Timeline|Captions
  tabs. Captions tab: Generate from narration / Validate (error+warning
  list) / Export SRT / Add+Delete cue / editable cue table (start/end/text,
  invalid input reverts) / template picker (cinematic|clean|bold-pop|custom)
  / style fields (font/size/bold/color/outline/position/margin/box/
  highlight words; any edit -> "custom"). Mixer row under the transport:
  Narration/Music/SFX/Ducking sliders wired to `project.mixer` (persisted,
  applied at render). Undo covers timeline edits only (stated in the UI).
- `auto_editor/jobs.py` (M7, Qt-free) — pipeline-STAGE persistence: one
  `JobStore` per work dir, manifest `<work_dir>/job.json` (atomic tmp+rename
  writes). Stages voiceover/slots/reference/photos/assemble/render each
  {pending|running|paused|done|failed} + checkpoint + error record
  {category, Roman-Urdu message, what_to_do, retryable, at}. Per-unit
  checkpoints: voiceover `chunk_NNN` (+mp3+chunks), photos `slot_NNN`
  (+path+prompt); resume skips completed units only when the file is also
  on disk. `classify_error()`: 429/AllKeysExhausted/no_keys/transient/5xx/
  network -> retryable -> job PAUSED (progress kept); 400/401/403 ->
  permanent -> stage FAILED with message + what-to-do. Detection order:
  JobPaused/paused-attribute, then the AllKeysExhausted message markers
  (ai_helper stringifies them), then HTTP_<code>, then network keywords.
  `record_stage_error()` = the single pause/fail decision used by the
  pipeline, voiceover studio and panel. `resume_job()` -> next unfinished
  stage in order (+checkpoint). `scan_incomplete()` finds unfinished
  manifests; "running" untouched for 6h (same horizon as render.lock) is
  flagged stale/crashed — the UI offers Resume, never auto-starts.
- `auto_editor/ai_helper.py` (M7) — `_post_pool` keeps the pause signal
  machine-readable: the AIError raised for AllKeysExhausted now carries
  `.paused=True`, `.resume_hint`, `.error_category` (message sniffing in
  classify_error remains the fallback since callers stringify AIError).
- `auto_editor/voiceover_studio.py` (M7) — `generate_voiceover(...,
  job=None, pause_event=None)`: per-chunk checkpoint/skip, pause between
  chunks (raises JobPaused), failures classified+recorded, still returns
  (False, msg) so old callers keep working; new callers escalate with
  `jobs.raise_if_paused()`. Owns its stage lifecycle when job is given.
- `auto_editor/auto_pipeline.py` (M7) — `run_script_pipeline` /
  `run_auto_pipeline` take `job`/`pause_event`; thin wrapper records
  pause_job on JobPaused and re-raises. Voiceover/slots/reference/photos/
  assemble stages checkpoint; photo loops rebuild skipped slots from the
  manifest (project assembly is deterministic from checkpoints).
- `auto_editor/pipeline_panel.py` (M7) — working ⏸ Pause button (checked
  between units, never mid-API-call); paused stages show "⏸ Paused (d/t):
  reason" + what-to-do from the manifest and their Start button becomes
  "Resume"; StageWorker now emits the exception object so JobPaused is
  told apart from real failures; regenerate of a done stage resets that
  stage onward in the manifest (true redo); work dirs recorded to
  settings_store `recent_work_dirs` for crash-recovery scanning.
- `auto_editor/app.py` (M7) — startup scans recent work dirs
  (`_check_job_recovery`, 800ms after launch): a dialog lists unfinished
  jobs with stage progress + stale flags and offers Resume (fills the
  pipeline work folder; nothing auto-starts). Render completion marks the
  manifest render stage done; render failure -> paused when cancelled,
  failed(transient) otherwise (chunks stay resume-safe).

## Questions for review
1. zoompan d=1 with `-loop 1` input: correct pattern for smooth per-frame motion? Jitter pitfalls?
2. xfade chaining math across N segments — recommended robust pattern?
3. Chunk (video-only) + single audio pass + final mux: sound restart-safe strategy?
4. sidechaincompress params for voice-over ducking sanity check.
5. QWebEngine persistent profile: correct calls for cookies/storage persistence?
6. ai_helper: response JSON parsing robustness tips; any security concern with local obfuscated key?
7. Windows packaging gotchas (PySide6 + ffmpeg + WebEngine).
