# BUG LOG — WS4 cross-testing (build + integration)

Every bug found during WS4 cross-testing, fixed or not.
Format: what broke / root cause / fixed-by-whom / retest status.
Rule: WS4 fixes only its own files. Another workstream's file = ESCALATED
with repro steps, never edited by WS4.

---

## BUG-001 — FileNotFoundError in render_worker chunk cleanup (shared work_dir race)
- **Status:** ESCALATED to WS1 via coordinator (WS1 owns `auto_editor/render_worker.py` — not edited by WS4)
- **Found:** 2026-10-09 ~21:55 PDT, by WS4 cross-test run of `tests/run_test.py`
- **What broke:** `render_project()` crashed at `os.remove(p)` (render_worker.py, chunk part-file cleanup after the intra-chunk join) with `FileNotFoundError: .../chunks/chunk_000_part0.mp4`.
- **Root cause:** Two `render_project()` invocations ran concurrently against the same
  default `work_dir` (`<outdir>/.render_cache`, deterministic per out_mp4). Both wrote
  the same `chunk_000_partN.mp4` filenames; process A finished its join and deleted the
  part files while process B was between its own join and its cleanup → B's
  `os.remove(part0)` hit a file A had already removed. (WS1 was live-testing new
  `_concat_filter` code in the same `tests/out/` dir at the time: render_worker.py
  mtime 21:53, mid my run 21:52–21:55; a second WS1 render process was observed.)
  Single-process, the join→cleanup path is sound (`_concat_filter` itself verified OK).
- **Suggested fix (for WS1):** make the default work_dir unique per invocation
  (e.g. pid/uuid suffix) or guard the manifest+chunks with a lock file, so two
  renders to the same out_mp4 cannot interleave part files.
- **Severity:** LOW in normal use (app renders serially); hit here only because two
  agents tested the same out dir concurrently.
- **Repro:** run two `render_project(proj, T, same_out.mp4)` concurrently (no explicit
  work_dir) with a project whose chunk splits into ≥2 xfade runs (needs a "cut"
  transition mid-chunk); one of them raises FileNotFoundError in cleanup.
- **Retest:** pending WS1 fix. WS4 integration tests use isolated dirs
  (`tests/out_integration/.cache_*`) and pass. Supporting evidence: WS4's
  integration test (a) renders a project whose chunk splits into 2 xfade runs
  (a "cut" transition mid-chunk) through the NEW `_concat_filter` path
  single-process — 10.0s, 1920x1080, 30fps, audio OK — so the join code itself
  is sound; the crash needed the second concurrent process.

## BUG-002 — PyInstaller entry pointed at `auto_editor/app.py` (frozen EXE would crash on startup)
- **Status:** FIXED by WS4
- **Found:** 2026-10-09, build.yml audit
- **What broke:** `build.yml` ran `pyinstaller ... auto_editor/app.py`. app.py uses
  relative imports throughout (`from . import settings_store`, `from .timeline ...`,
  lines 21–26). A PyInstaller entry script executes as `__main__` with no parent
  package, so the shipped AutoEditor.exe would have died at startup with
  `ImportError: attempted relative import with no known parent package`.
- **Root cause:** entry script chosen without checking the package's import style.
- **Fix (WS4):** new repo-root launcher `autoeditor.py` (`from auto_editor.app import main`,
  Mdola pattern); `build.yml` now builds `autoeditor.py`. PyInstaller analyzes the
  `auto_editor` package properly and all relative imports resolve in the bundle.
- **Retest:** launcher `py_compile`d; full frozen-EXE verification needs the Windows CI
  runner (no Windows here). CI smoke step covers the import path.

## BUG-003 — CI smoke step `python auto_editor/app.py --smoke` always fails
- **Status:** FIXED by WS4
- **Found:** 2026-10-09, build.yml audit
- **What broke:** first smoke attempt ran app.py as a plain script → `ImportError:
  attempted relative import with no known parent package` (same root cause as
  BUG-002); the `|| python -m auto_editor.app --smoke` fallback masked it, so CI
  stayed green but the primary command was dead.
- **Fix (WS4):** smoke step is now just `python -m auto_editor.app --smoke`, which
  keeps the package context so relative imports resolve. Comment in build.yml
  explains why.
- **Retest:** `-m` form verified locally (import path OK; Qt platform libs missing on
  this Linux VM, so the GUI import itself is env-skipped here — see test (f)).

## Notes / non-bugs
- `gh` CLI: **NO_GH_AUTH** on this machine (`gh auth status`: not logged into any
  GitHub hosts; no GH_TOKEN in env). Repo creation (`mirza43hgfd/auto-editor`) +
  Actions + Release cannot be done from here. ESCALATED to coordinator as blocker —
  WS4 did NOT attempt any credential creation.
- `tests/run_test.py` (WS1's Phase-1 proof) currently FAILS at render pass 1 for the
  BUG-001 reason above (environmental collision, not a code regression — WS1's own
  concurrent multi-chunk test of the new `_concat_filter` passed in their process).
  Rerun it in an isolated dir once BUG-001 is addressed.
- requirements.txt VERIFIED OK: `PySide6>=6.6` runtime; `pyinstaller`+`imageio-ffmpeg`
  are build-only and installed explicitly in build.yml (matches the frozen-EXE model).
- build.yml audit otherwise clean: `--onefile --windowed`, `--add-binary
  "ffmpeg-bin/ffmpeg.exe;ffmpeg-bin"` (`;` = correct Windows separator, matches
  `find_ffmpeg()`'s `_MEIPASS/ffmpeg-bin/ffmpeg.exe` lookup), `--add-data
  "assets;assets"` (matches `ASSETS=<pkg>/../assets` → `<_MEIPASS>/assets`),
  py_compile guard (now also covers `autoeditor.py`), imageio-ffmpeg exe fetch,
  `permissions: contents: write` (Mdola lesson), tag-triggered release. No exotic
  untested flags added.

## BUG-004 — xfade offset formula off by one (WS1, render_worker.py)
- **Status:** FIXED by WS1 (coordinator)
- **Found:** 2026-10-09, multi-chunk render test (70s project)
- **What broke:** `_xfade_offsets()` returned [offset_1..offset_{n-1}] instead of [offset_0..offset_{n-2}] — every transition used the NEXT transition's offset. Chunk durations came out wrong (12.07s instead of 25s).
- **Root cause:** loop appended `total-(k+1)*f` for k>0, skipping k=0. Correct: offset_k = sum(d_0..d_k)-(k+1)*f for k=0..n-2.
- **Retest:** multi-chunk render → chunks exactly 25.0/25.0/20.0s, final 70.0s. PASS.

## BUG-005 — BGM ended when narration ended (sidechain EOF)
- **Status:** FIXED by WS1
- **Found:** 2026-10-09, multi-chunk test (20s narration, 70s timeline)
- **What broke:** `sidechaincompress` terminates when its sidechain (narration) hits EOF, so the ducked BGM was only 20s; final audio 20s instead of 70s.
- **Root cause:** filter-graph EOF semantics — one input ending ends the filter.
- **Fix:** split narration (`asplit`); pad one copy with silence to full duration (`apad=whole_dur`) to drive the sidechain; BGM now runs the full timeline and swells back after the voice stops. Pad/split only created when BGM exists (see BUG-007).
- **Retest:** audio_full.m4a = 70.0s. PASS.

## BUG-006 — concat demuxer list used CWD-relative paths
- **Status:** FIXED by WS1
- **Found:** 2026-10-09, multi-chunk final assembly
- **What broke:** `Impossible to open 'tests/out3/.cache/tests/out3/.cache/chunks/chunk_000.mp4'` — the concat demuxer resolves list entries relative to the LIST FILE's directory, not the process CWD.
- **Fix:** write absolute paths in the concat list file.
- **Retest:** final assembly OK. PASS.

## BUG-007 — unconnected apad filter when no BGM
- **Status:** FIXED by WS1
- **Found:** 2026-10-09, no-BGM render
- **What broke:** `Filter 'apad' has output 0 (n_pad) unconnected` — the padded sidechain was built unconditionally but only consumed when BGM exists.
- **Fix:** asplit/apad only when BGM is present.
- **Retest:** no-BGM render OK. PASS.

## BUG-008 — render lock TOCTOU race
- **Status:** FIXED by WS1
- **Found:** 2026-10-09, concurrent-render lock test (follow-up to BUG-001)
- **What broke:** check-then-create on render.lock let two threads both pass the existence check before either created the file → same interleaving crash as BUG-001.
- **Fix:** atomic lock creation via os.open(O_CREAT|O_EXCL); one retry after clearing a stale (>6h) lock.
- **Retest:** 2 concurrent renders → one fails fast with "Another render is already using…", winner completes. PASS.

## Review feedback applied (Gemini architecture review, 2026-10-09)
- zoompan: switched from `-loop 1`+d=1 to single-image+d=N (deterministic `on`, less decode work). Verified: 20.00s exact.
- xfade math confirmed correct (after BUG-004 fix); final chain segment correctly NOT extended.
- Audio: aresample=async:1 + explicit aformat sample_rates/channel_layouts on all chains.
- Concat: closed GOPs (open-gop=0) on chunk encodes; intra-chunk joins via concat filter (re-encode, always safe); inter-chunk via demuxer -c copy.
- TTS request shape verified LIVE 2026-10-09: gemini-2.5-flash-preview-tts + Charon + AUDIO modality OK; response inlineData audio/L16 24kHz raw PCM; WAV wrap verified (3.93s). Urdu prompt text accepted.

## BUG-009 — E2E audio check reported silence on healthy audio (test bug)
- **Status:** FIXED (tests/test_e2e.py)
- **Found:** 2026-10-09, E2E run
- **What broke:** assertion "audio silent at head" even though the WAV/MP3/M4A/MP4 chain all had real speech (-15.7/-17.0/-20.2 dB).
- **Root cause:** the volumedetect probe used `-v error`, which suppresses volumedetect's info-level stats; the regex found nothing and defaulted to -99 dB.
- **Fix:** probe uses `-hide_banner` (default verbosity) for volumedetect; added `--verify-only` flag so re-verification does not re-spend TTS quota.
- **Retest:** head -16.6 dB, tail -22.4 dB. PASS.

## BUG-010 — no-BGM audio ended early, truncating the video (WS1, render_worker.py)
- **Status:** FIXED by WS1
- **Found:** 2026-10-09, cancel/resume test (20s narration, 40s timeline, no BGM)
- **What broke:** audio_full.m4a was 20s instead of 40s; the `-shortest` mux then cut the 40s video down to 20s.
- **Root cause:** `apad=whole_dur` was only applied to the sidechain copy (BGM case). In the narration-only path the 20s mix hit `amix` (longest=20s); `atrim`/`-t` can only cut, never extend — so the audio stream simply ended at 20s.
- **Fix:** pad the narration MIX copy with `apad=whole_dur=total_dur` in both paths (also harmless in the BGM case: the infinite looped BGM still dominates `amix=longest`, `atrim` cuts to total_dur as before).
- **Retest:** cancel → resume → video 40.0s, audio 40.0s. PASS. run_test.py re-run: ALL TESTS PASSED.

## BUG-011 — renderer ignored Project width/height/fps (always 1920x1080@30)
- **Status:** FIXED on branch m1-stabilize (M1)
- **Found:** 2026-10-10, M1 synthetic E2E (9s project with width=1280, height=720)
- **What broke:** project set to 1280x720 rendered as 1920x1080. `Project` supports configurable width/height/fps (timeline.py, round-tripped in to_dict/from_dict) but `render_worker.py` used hardcoded module constants `W,H,FPS = 1920,1080,30` in `_motion_filter`, `_segment_chain`, `_render_chain_part`, `_concat_filter` — user geometry was silently discarded.
- **Fix:** threaded `(w, h, fps)` through the render chain; `render_project` reads them from the project (module constants remain as fallback for old projects); photo pre-upscale is now 2x target instead of hardcoded 3840x2160; manifest `proj_hash` now includes `WxH@fps` so a geometry change invalidates stale cached chunks (previously a resolution change would have reused wrong-resolution chunks).
- **Retest:** M1 synthetic E2E — 9s project renders 1280x720 30fps, ffprobe duration=9.00s, audio stream present, decode-to-null clean; resume pass 1.2s (chunks skipped); cancel before chunk 2 raises RenderCancelled cleanly, chunk_000.mp4 cached, re-run completes → 75.00s valid MP4. Full suite re-run: run_test.py ALL PASSED, test_integration.py 8/8 PASS (1 env-skip), test_ai.py 26 passed/0 failed/1 deferred. PASS.

## BUG-012 — ai_helper cache would poison test_ai.py on re-runs (M2, ai_helper.py / test_ai.py)
- **Status:** FIXED on branch m2-provider (M2)
- **Found:** 2026-10-10, M2 refactor review (before first run)
- **What broke:** `call_gemini()` gained a persistent SQLite prompt cache. test_ai.py monkeypatches `ai_helper._post_model_pool` and asserts on captured model/payload. On a SECOND `test_ai.py` run in the same environment, the cache (pointing at the real user config dir) would serve stale hits, the mock would never fire, `captured` would stay empty, and assertions would fail/crash. Worse: tests would read/write the user's real cache file.
- **Root cause:** process-wide cache singleton defaulted to the real `<app-data>/prompt_cache.sqlite3` with no test isolation hook.
- **Fix:** added `ai_helper.configure_cache(path)`; test_ai.py now points it at a fresh temp dir on import. Explicit `api_key=` calls and `use_cache=False` bypass the cache.
- **Retest:** test_ai.py run twice back-to-back: 26 passed, 0 failed, 1 deferred both times. PASS.

## BUG-013 — _post_pool refactor would bypass the stubbed _post_single, breaking the zero-network test contract (M2, ai_helper.py)
- **Status:** FIXED on branch m2-provider (M2)
- **Found:** 2026-10-10, M2 refactor design review (before first run)
- **What broke:** test_integration.py `t_ai_pool_failover` stubs `ai_helper._post_single` and asserts `_post_pool` uses it ("zero network/quota is touched"). A naive refactor (`_post_pool` -> `Provider.post` with provider's own urllib transport) would have bypassed the stub and hit the real network in tests.
- **Root cause:** provider's default transport does not go through the module-global `_post_single` name that tests patch.
- **Fix:** `_post_pool` builds the Provider with `_pool_transport`, which delegates to the module-global `_post_single` (looked up at call time, so stubs keep working) and translates AIError("HTTP_### ...") back into HTTPStatusError via `provider.http_status_from_ai_error()` (round-trips `retry_after=` through the message). Real Retry-After headers still flow: `_post_single` encodes `retry_after=N` into the AIError text.
- **Retest:** test_integration.py (g)+(h) pass — no-key path raises clean AIError with no network; 429 failover marks key0 limited+cooldown and key1 wins. PASS.

## BUG-014 — MainWorkspace.set_project wiped the undo stack on every refresh (M3, workspace.py)
- **Status:** FIXED by M3 (self-review, before any test run)
- **Found:** 2026-10-10, while wiring app.py (`_workspace_changed` -> `_refresh_table` -> `set_project`)
- **What broke:** first draft of `set_project()` unconditionally reset `self.history = TimelineHistory()`. Since every workspace edit emits `project_changed` -> `_refresh_table()` -> `set_project(same_object)`, the undo stack would have been destroyed after every single edit — undo/redo buttons permanently dead.
- **Root cause:** rebind treated as "new project" even when the object identity was unchanged.
- **Fix:** only reset history + selection when `project is not self.project`; same-object rebind just refreshes the views.
- **Retest:** covered by tests/test_timeline_model.py undo/redo suite (37/37); widget-level undo flow could not be exercised here (no Qt on this VM — ENV-SKIP, must be verified on Windows).

## BUG-015 — inspector prompt editor was never wired (M3, workspace.py)
- **Status:** FIXED by M3 (self-review, before any test run)
- **Found:** 2026-10-10, code review of workspace.py
- **What broke:** the visual-prompt QPlainTextEdit was created with a placeholder but no signal connections — typing did nothing (a dead control, violating the "every visible control must work" rule).
- **Fix:** `textChanged` -> write-through to the slot model; event filter: FocusIn -> history checkpoint (one undo step per edit session), FocusOut -> `project_changed` emit. `_loading` guard prevents clobbering during programmatic refresh.
- **Retest:** prompt write-through is covered at model level by test_timeline_model.py (`set prompt`); widget focus behavior needs Windows verification (ENV-SKIP here).

## BUG-016 — test_timeline_model.py helper functions used before definition (M3, tests)
- **Status:** FIXED by M3
- **Found:** 2026-10-10, first test run (NameError at module level)
- **What broke:** `find_label`/`find_trans` were called by checks placed above their `def` lines.
- **Fix:** moved the helpers above first use. Plus a missing closing paren (SyntaxError) on the "undo restores trim" check — both fixed; suite now 37/37 green.

## BUG-017 — no-introduction heuristic fired on the test's own "clean" fixture (M4, tests)
- **Status:** FIXED by M4 (test fixture, not product code)
- **Found:** 2026-10-10, first run of tests/test_characters.py (2 failures)
- **What broke:** `make_cast_project()` attached Zara to scene 2 but no earlier scene named her, so `check_continuity()` correctly raised `no-introduction` — the "clean project: no warnings" and "named earlier" checks failed. The rule was right; the fixture was sloppy.
- **Root cause:** fixture built an actually-unintroduced character while asserting a warning-free project.
- **Fix:** scene 1's `narration_segment` now introduces Zara ("Khalid rides out with Zara at his side."); the negative test appends rather than overwrites the segment. Suite: 68/68 green.
- **Retest:** tests/test_characters.py — 68 passed, 0 failed. PASS.

## BUG-018 — dead `can` variable with nonsense predicate in _update_regen_button (M4, workspace.py)
- **Status:** FIXED by M4 (self-review, before any test run)
- **Found:** 2026-10-10, code review of workspace.py
- **What broke:** a leftover `can = (... and self.btn_regen_scene.isEnabled() is not None)` line — dead variable, and `isEnabled() is not None` is always True, so it could mislead a future reader into thinking the button state depended on it.
- **Root cause:** editing residue from drafting the enable logic.
- **Fix:** removed the dead variable; the single `setEnabled(...)` below it is the real logic. Widget behavior needs Windows verification (no Qt on this VM — ENV-SKIP).

## BUG-019 — "fadeblack" was silently rendered as xfade (Phase 1, render_worker.py)
- **Status:** FIXED by M5
- **Found:** 2026-10-10, M5 audit of render_worker.py (inline comment admitted it: "fadeblack is rendered as xfade in Phase 1")
- **What broke:** a slot whose transition was "fadeblack" got an xfade dissolve — the exact silent substitution the product brief forbids. The user-selected effect never rendered.
- **Fix:** genuine dip-to-black (fade=t=out on the previous run's tail + fade=t=in on the next run's head, pixel-only, wall-clock preserved); "fadeblack" kept as a legacy alias rendering identically. `_check_transitions()` now raises RuntimeError on any unrenderable transition instead of substituting.
- **Retest:** tests/test_editing.py render proof — dip midpoint frame mean brightness < 15 (an xfade substitute would give ~55-85); `_split_runs` unit checks for the fadeblack alias. PASS.

## BUG-020 — fixed MOTION_CYCLE zoom-preset rotation (auto_pipeline.py)
- **Status:** FIXED by M5
- **Found:** 2026-10-10, M5 audit (brief §6 explicitly bans cycling repetitive zoom presets)
- **What broke:** `motion_preset=MOTION_CYCLE[i % len(MOTION_CYCLE)]` gave every project the same zoom-in/out/pan-left/pan-right loop — the slideshow feel the brief targets.
- **Fix:** deleted MOTION_CYCLE; both auto pipelines call `editing.apply_editing_decisions(project)` so motion/transitions derive from each scene's content.
- **Retest:** tests/test_editing.py "no fixed cycling" check (6 varied scenes -> >1 distinct transition). PASS.

## BUG-021 — OPEN rule shadowed by CONTINUE: first slot got "cut" instead of "fade-in" (M5, editing.py)
- **Status:** FIXED by M5 (self-review via failing test, before merge)
- **Found:** 2026-10-10, first run of tests/test_editing.py ("opening -> fade-in" FAIL)
- **What broke:** rule order put CONTINUE (next scene exists -> hard cut) before OPEN, so a neutral opening slot with a following slot got "cut" — the video would start with a hard cut from nothing instead of fading in from black.
- **Root cause:** rule priority mistake in decide_edit().
- **Fix:** OPEN moved before CONTINUE in the rule chain (END > ACTION > QUIET > CHANGE > OPEN > CONTINUE > DEFAULT); docstring renumbered.
- **Retest:** tests/test_editing.py — 46/46 green. PASS.

## BUG-022 — test fixture text accidentally hit ACTION_KW (M5, tests only)
- **Status:** FIXED by M5 (test fixture, not product code)
- **Found:** 2026-10-10, first run of tests/test_editing.py (2 FAILs: "continuation -> restrained", "character-change -> crossfade")
- **What broke:** fixture narration "He raised his sword." contains "sword" which is legitimately an ACTION keyword, so decide_edit correctly returned the action decision — the engine was right, the fixture was sloppy.
- **Fix:** fixture texts rewritten neutral ("He looked at the horizon.", "The wind moved the sand.") with a non-None prev so the OPEN rule doesn't shadow the case under test.
- **Retest:** tests/test_editing.py — 46/46 green. PASS.

## BUG-023 — pop()-during-index corrupted/split_into_phrases crashed (M6, captions.py)
- **Status:** FIXED by M6 (self-review via failing test, before merge)
- **Found:** 2026-10-10, first run of tests/test_captions.py (2 FAILs + 1 IndexError traceback)
- **What broke:** the backward-merge step did `merged[-2] = (merged[-2] + " " + merged.pop()).strip()`. Python evaluates the RHS fully before the assignment: `pop()` shrank the list first, so `merged[-2]` rebound to the WRONG element — silent corruption with 3+ phrases (first phrase dropped, duplicate appeared), IndexError with exactly 2.
- **Root cause:** evaluation-order bug: indexing a list in the same expression that mutates it.
- **Fix:** pop into a local first, then assign: `last = merged.pop(); merged[-1] = (merged[-1] + " " + last).strip()`.
- **Retest:** tests/test_captions.py — 62/62 green. PASS.

## BUG-024 — shift_cue collapsed cue duration when shifting past zero (M6, captions.py)
- **Status:** FIXED by M6 (self-review via failing test, before merge)
- **Found:** 2026-10-10, first run of tests/test_captions.py ("shift: clamps at 0" FAIL)
- **What broke:** shifting a cue before 0 clamped start AND end independently at 0, so a 3s cue became 0.1s — the CaptionCue constructor's end>start guard collapsed the duration.
- **Root cause:** clamping start/end separately instead of moving the window.
- **Fix:** clamp start at 0, preserve duration: `CaptionCue(start, start + dur, ...)`.
- **Retest:** tests/test_captions.py — 62/62 green. PASS.

## BUG-025 — JobStore._load early-return skipped stage init (M7, jobs.py)
- **Status:** FIXED by M7 (self-review via failing test, before merge)
- **Found:** 2026-10-10, first run of tests/test_jobs.py ("roundtrip" FAIL, KeyError: 'photos')
- **What broke:** `JobStore(fresh_dir).start_stage("photos")` raised KeyError — on a fresh work dir (no job.json yet) `_load()` returned before populating `data["stages"]`, so all six stages were missing.
- **Root cause:** early `return data` on missing/corrupt manifest skipped the STAGES initialization loop.
- **Fix:** restructured `_load()` to always build the six stage entries (from saved data when present, blanks otherwise); no early return.
- **Retest:** tests/test_jobs.py — 31/31 green. PASS.

## BUG-026 — ai_helper swallowed the AllKeysExhausted pause signal (M7, ai_helper.py)
- **Status:** FIXED by M7 (design review, before merge)
- **Found:** 2026-10-10, while wiring M7 pause handling.
- **What broke:** `_post_pool` converted `AllKeysExhausted` into a plain `AIError(user_message)` — the pipeline could not distinguish "pause, progress kept" from "failed", so quota exhaustion looked like a hard failure.
- **Root cause:** pause metadata (resume_hint, error category) discarded at the ai_helper boundary.
- **Fix:** the raised AIError now carries `.paused=True`, `.resume_hint`, `.error_category`; `jobs.classify_error()` checks the attribute first, then falls back to sniffing the (stable, codebase-owned) AllKeysExhausted message markers since most callers stringify AIError.
- **Retest:** tests/test_jobs.py "classify: paused attribute" + "exhausted: paused not failed" — PASS.

## BUG-027 (2026-10-10) — v2.0 EXE startup crash: stale MOTION_CYCLE import [FIXED]
- **Symptom:** User's screenshot on Windows: `ImportError: cannot import name 'MOTION_CYCLE' from 'auto_editor.auto_pipeline'` at `pipeline_panel.py` line 41 — app died before the window opened.
- **Root cause:** M5 deleted the banned fixed-rotation `MOTION_CYCLE` from auto_pipeline.py but missed (a) its import in pipeline_panel.py:41 and (b) its use in `PipelinePanel._stage_assemble` (line 491: `motion_preset=MOTION_CYCLE[i % len(MOTION_CYCLE)]`) — the Full Auto assemble path was still cycling presets, the exact pattern brief §6 bans. The headless smoke test never imported the Qt-bearing modules, so CI stayed green.
- **Fix:** Removed the import; assemble now builds slots with defaults then calls `apply_editing_decisions(project)` (intelligent, content-derived). Added two regression gates: `auto_editor/smoke_gui.py` (imports all Qt modules; new Windows CI step) and `tests/test_imports.py` (Qt-free static AST check of intra-package imports). Proven: test_imports FAILS on the pre-fix file with the exact ImportError, PASSES after.
- **Also caught by the new gate:** none stale besides this (one false positive for try/except-defined `WEBENGINE_AVAILABLE` — test refined to scan top-level try/if bodies).
- **Retest:** full Qt-free suite green (333 checks + run_test.py ALL PASSED).
