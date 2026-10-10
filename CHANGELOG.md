# AutoEditor CHANGELOG

## v2.0.1 (2026-10-10) — Hotfix: startup crash + CI regression gates

- **What:** Fixed v2.0 startup crash (`ImportError: cannot import name
  'MOTION_CYCLE' from 'auto_editor.auto_pipeline'`). M5 deleted the banned
  fixed-rotation `MOTION_CYCLE` but missed its import + one use in
  `pipeline_panel._stage_assemble` — the Full Auto assemble path was still
  cycling presets. Now assemble builds slots with defaults and calls
  `apply_editing_decisions(project)` (intelligent, content-derived).
- **Files:** `auto_editor/pipeline_panel.py`, `auto_editor/smoke_gui.py` (new),
  `tests/test_imports.py` (new), `.github/workflows/build.yml`,
  `auto_editor/__init__.py` (2.0.1).
- **Regression gates (would have caught the v2.0 crash):**
  - `python -m auto_editor.smoke_gui` — imports every Qt-bearing module on the
    Windows runner (offscreen); new CI step in build.yml.
  - `tests/test_imports.py` — Qt-free static AST check that every
    intra-package `from .mod import name` resolves; proven to fail on the
    pre-fix file with the exact ImportError and pass after.
- **Test evidence:** full Qt-free suite green (see v2.0.0 entry), plus the two
  new gates above.

## v2.0.0 (2026-10-10) — Milestones M1–M7 merged to master

Premium desktop AI video editor: script → coherent, captioned, graded 1080p MP4
with an editing-first workspace, character continuity, intelligent transitions
and a hardened provider/job layer. Built incrementally on stacked branches
m1-stabilize → m7-jobs, fast-forward merged to master.

---

### M1 — Renderer stabilization (BUG-011)
- **What:** `render_worker` now honors the Project's own width/height/fps instead
  of hardcoded 1920x1080@30; render-cache hash includes geometry so a changed
  project can never silently reuse stale chunks.
- **Files:** `auto_editor/render_worker.py`, `tests/BUG_LOG.md`.
- **Test evidence:** full suite green on master (see below); render contract tests
  in `tests/test_integration.py` verify 10s 1920x1080 30fps output + selective
  resume.

### M2 — Centralized provider / key-ring layer
- **What:** all AI traffic moved behind `provider.py`: per-key state
  (enabled, validation, success/failure, rate-limit, Retry-After, error
  category), failover that honors Retry-After, invalid-key marking with user
  notification, bounded exponential backoff + jitter. SQLite prompt cache
  (`cache.py`) — identical prompts never re-sent. TTS chunk batching
  (`tts_batch.py`) to burn less RPM. Explicit no-evasion rule: keys may share
  account quota; rotation is never used to dodge limits.
- **Files:** `auto_editor/provider.py` (new), `auto_editor/cache.py` (new),
  `auto_editor/tts_batch.py` (new), `auto_editor/ai_helper.py`,
  `auto_editor/settings_store.py`, `ARCHITECTURE.md`, `tests/BUG_LOG.md`.
- **Test evidence:** `tests/test_provider.py` — 56 passed (key-ring failover,
  cooldown, obfuscated storage, no plaintext keys).

### M3 — Editing workspace UI
- **What:** editing-first main window (`workspace.py`): preview monitor,
  storyboard/scene panel, media + character library panel, properties
  inspector, multitrack timeline widget (`timeline_widget.py`) with drag
  reorder, trim, per-slot transition picker, playhead, zoom/scroll, undo/redo.
  Timeline data model extended in `timeline.py` (trim, reorder, transitions,
  prompts, save/reopen round-trip).
- **Files:** `auto_editor/workspace.py` (new), `auto_editor/timeline_widget.py`
  (new), `auto_editor/timeline.py`, `auto_editor/app.py`, `ARCHITECTURE.md`.
- **Test evidence:** `tests/test_timeline_model.py` — 37 passed (trim/reorder/
  transition/prompt survive save→reopen).

### M4 — Character library + scene continuity
- **What:** proper character library (`characters.py`, Qt-free model): name,
  description, reference images, face/hair/age/clothing features, stable ID,
  scene appearances, reference selection/replacement, reuse across scenes,
  continuity warnings, dangling-reference safety. Pipeline passes reference
  images (not text alone) to the image provider per scene; per-slot
  regeneration without rebuilding the project.
- **Files:** `auto_editor/characters.py` (new), `auto_editor/timeline.py`,
  `auto_editor/auto_pipeline.py`, `auto_editor/workspace.py`,
  `ARCHITECTURE.md`.
- **Test evidence:** `tests/test_characters.py` — 68 passed (mocked provider;
  no real API calls).

### M5 — Intelligent editing decisions + genuine transitions
- **What:** editing-decision layer (`editing.py`): transition choice from
  narration boundaries, scene meaning, emotional intensity, action, music cues
  and section position — quiet scenes hold + fade gently, action cuts tighter,
  section ends dip to black. Renderer genuinely implements the selected
  transition (`xfade`, `cut`, `fade-in`, `fade-out`, `dip-to-black`,
  `fadeblack`); never a silent substitute.
- **Files:** `auto_editor/editing.py` (new), `auto_editor/render_worker.py`,
  `auto_editor/timeline.py`, `auto_editor/auto_pipeline.py`,
  `auto_editor/workspace.py`, `ARCHITECTURE.md`.
- **Test evidence:** `tests/test_editing.py` — 46 passed, including pixel-level
  proofs (fade-in dark→bright, dip-to-black midpoint near-black).

### M6 — Captions engine + audio mixer + templates
- **What:** real captions (`captions.py`): auto timing from narration chunks,
  word/phrase timing, manual text+timing correction, font/size/color/outline/
  background, position + safe margins, alignment/wrap, word highlights,
  animation presets, reusable templates, caption timeline track, burned-in
  subtitles + separate SRT export, sync validation. Audio mixer: narration
  dominant, BGM ducked, SFX mixed, loudnorm master.
- **Files:** `auto_editor/captions.py` (new), `auto_editor/render_worker.py`,
  `auto_editor/timeline.py`, `auto_editor/timeline_widget.py`,
  `auto_editor/workspace.py`, `ARCHITECTURE.md`.
- **Test evidence:** `tests/test_captions.py` — 62 passed, including pixel
  proofs that burn-in draws captions and cueless renders stay clean.

### M7 — Job persistence, pause/resume, recovery hardening
- **What:** persistent pipeline jobs (`jobs.py`): stage checkpoints, pause/
  resume, cancel, crash recovery — a failed scene never restarts the whole
  workflow. Pipeline panel rewritten around job state; per-stage
  Approve/Edit/Regenerate in step-by-step mode; FULL AUTO runs end-to-end.
- **Files:** `auto_editor/jobs.py` (new), `auto_editor/auto_pipeline.py`,
  `auto_editor/pipeline_panel.py`, `auto_editor/app.py`,
  `auto_editor/voiceover_studio.py`, `auto_editor/ai_helper.py`,
  `ARCHITECTURE.md`.
- **Test evidence:** `tests/test_jobs.py` — 38 passed (stage completion,
  re-run idempotency, resume).

---

### Full-suite results on master (v2.0.0, venv python, no network)

| Test file | Result |
|---|---|
| tests/test_provider.py | 56 passed, 0 failed |
| tests/test_timeline_model.py | 37 passed, 0 failed |
| tests/test_characters.py | 68 passed, 0 failed |
| tests/test_editing.py | 46 passed, 0 failed |
| tests/test_captions.py | 62 passed, 0 failed |
| tests/test_jobs.py | 38 passed, 0 failed |
| tests/test_ai.py | 26 passed, 0 failed, 1 deferred (live API — no key/quota burn) |
| tests/test_integration.py | 8/8 passed (app --smoke env-skipped: no libEGL on Linux VM; runs on Windows CI) |
| tests/test_e2e.py | full mode NOT RUN (needs live Gemini TTS; no live API calls in this task) — --verify-only on the prior milestone render PASSED (audio head/tail OK) |
| tests/run_test.py | ALL TESTS PASSED — 20.00s 1920x1080 30fps, audio present, resume pass 2.6s |

Honesty notes: `tests/test_e2e.py` full mode requires live Gemini TTS via a vault
key, so it was not run in this offline task; its `--verify-only` mode
re-verified the render produced during milestone work (audio present at head
and tail — passed). No live Gemini API calls were made in any test (mocked,
deferred, or synthetic).

### Known limitations (v2.0.0)
- 4K output profile, 9:16 vertical preset, title/description/tags generator,
  render queue UI, SEO export and thumbnail grabber are not built yet.
- Character identity is improved by reference-image conditioning, not
  guaranteed — continuity warnings surface problems honestly.
- Free-tier Gemini quota is slow; the app throttles and shows queue ETA but
  cannot create quota that isn't there.
- GUI smoke test needs Windows CI (Qt system libs missing on the Linux VM).
