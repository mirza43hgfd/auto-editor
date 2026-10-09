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
