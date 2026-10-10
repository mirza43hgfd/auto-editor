"""Headless render worker.

render_project(proj_json, media_dir, out_mp4, progress_cb=None)
  -> H.264 MP4 at the project's width/height/fps (default 1920x1080 30fps) with:
     - photo Ken Burns motion (ffmpeg zoompan)
     - video clip subsegments (-ss/-t)
     - GENUINE per-slot transitions (M5): cut (hard concat), xfade
       (crossfade/dissolve), fade-in (from black at slot head), fade-out
       (to black at previous tail), dip-to-black / fadeblack (out+in).
       The renderer implements the SELECTED transition; an unsupported
       transition raises instead of being silently substituted.
     - logo overlay top-right, full duration
     - narration + BGM ducked under narration (sidechaincompress) + SFX
     - loudnorm on the master
     - restart-safe: ~60s video chunks + manifest.json; resume skips done chunks

No network, no API keys. ffmpeg lookup: bundled ffmpeg-bin first, then PATH.
"""

import json
import math
import os
import shutil
import subprocess
import sys

from .timeline import Project, Slot
from . import captions as _captions

W, H, FPS = 1920, 1080, 30
FADE_DUR = 1.0          # xfade seconds
CHUNK_TARGET = 60.0     # ~60s restart-safe chunks
LOGO_WIDTH = 180
LOGO_MARGIN = 20


class RenderCancelled(Exception):
    """User cancelled the render (resume-safe: finished chunks stay cached)."""


# Every transition the renderer can genuinely build. "fadeblack" is the
# Phase-1 legacy name and renders exactly like "dip-to-black". Anything
# else raises in _check_transitions() — never silently substituted.
RENDERABLE_TRANSITIONS = frozenset(
    ("xfade", "cut", "fade-in", "fade-out", "dip-to-black", "fadeblack"))


def _check_transitions(slots):
    """Fail loudly on an unrenderable transition (no silent substitution)."""
    bad = [(s.id, s.transition) for s in slots
           if s.transition not in RENDERABLE_TRANSITIONS]
    if bad:
        raise RuntimeError(
            "Unsupported transition(s) — refusing to silently substitute: %s. "
            "Valid: %s." % (", ".join("%s=%r" % b for b in bad),
                            ", ".join(sorted(RENDERABLE_TRANSITIONS))))

# Cinematic grade presets (applied after transitions, before the logo so the
# logo itself is never graded). Values are deliberately modest.
GRADE_PRESETS = {
    "none": "",
    "cinematic": "colorbalance=rs=0.12:gs=0.04:bs=-0.12:rm=0.08:bm=-0.08,"
                 "eq=contrast=1.08:saturation=1.15",
    "teal-orange": "colorbalance=rs=-0.08:bs=0.14:rm=-0.04:bm=0.10,"
                   "eq=contrast=1.05:saturation=1.20",
    "war-desat": "eq=saturation=0.65:contrast=1.06:brightness=-0.02",
    "night-cold": "colorbalance=rs=-0.06:bs=0.18:bm=0.10,"
                  "eq=saturation=0.90:contrast=1.04",
}
DEFAULT_GRADE = "cinematic"


# --------------------------------------------------------------------------
# ffmpeg discovery
# --------------------------------------------------------------------------

def find_ffmpeg():
    """Bundled binary first (PyInstaller _MEIPASS or repo ffmpeg-bin), then PATH."""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(sys._MEIPASS, "ffmpeg-bin", exe))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "ffmpeg-bin", exe))
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.isfile(c):
            return c
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg not found (checked ffmpeg-bin/ and PATH).")


def _run(cmd, log_path=None):
    """Run ffmpeg, raise RuntimeError with tail of stderr on failure."""
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.splitlines()[-15:])
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("CMD: %s\n%s\n" % (" ".join(cmd), p.stderr))
        raise RuntimeError("ffmpeg failed:\n%s" % tail)
    return p


# --------------------------------------------------------------------------
# filter builders
# --------------------------------------------------------------------------

def _motion_filter(preset, frames, w, h, fps, intensity=1.0):
    """zoompan args for a Ken Burns preset. frames baked in (no runtime math).

    intensity scales the zoom amplitude (M5 pacing): hold_factor > 1
    (quiet scenes) -> gentler motion, hold_factor < 1 (action) -> stronger.
    "none" and the pan presets are unaffected by intensity.
    """
    n = max(frames, 1)
    i = max(0.0, min(2.0, intensity))
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    if preset == "zoom-out":
        z = "1.15-%.3f*on/%d" % (0.15 * i, n)
        x, y = cx, cy
    elif preset == "pan-left":
        z = "1.25"
        x = "(iw-iw/zoom)*on/%d" % n
        y = cy
    elif preset == "pan-right":
        z = "1.25"
        x = "(iw-iw/zoom)*(1-on/%d)" % n
        y = cy
    elif preset == "none":
        z, x, y = "1.0", cx, cy
    else:  # zoom-in (default)
        z = "1+%.3f*on/%d" % (0.15 * i, n)
        x, y = cx, cy
    return ("zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d"
            % (z, x, y, n, w, h, fps))


def _probe_duration(path):
    """Media duration in seconds via ffprobe (0.0 on failure)."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60)
        return float(p.stdout.strip())
    except Exception:
        return 0.0


def _segment_chain(idx, slot, media_path, dur, w, h, fps,
                   head_fade=0.0, tail_fade=0.0):
    """Return (input_args, filter) for one slot -> labeled [v{idx}].

    dur is the EXTENDED duration (slot + FADE_DUR for non-final chain
    segments) so xfade overlaps don't shrink the wall-clock timeline.
    head_fade/tail_fade (seconds, M5) bake genuine fade-in/out filters into
    the segment: fade=t=in at the head, fade=t=out at the tail. They only
    change pixels, never durations, so wall-clock is preserved.
    """
    # M5 pacing: hold_factor modulates motion intensity (gentler for quiet
    # scenes, stronger for action). Old slots default to 1.0.
    intensity = 1.0 / max(0.5, min(2.0, float(getattr(slot, "hold_factor", 1.0) or 1.0)))
    intensity = max(0.5, min(1.5, intensity))
    if slot.visual_type == "clip":
        in_args = ["-ss", "%.3f" % slot.clip_in, "-t", "%.3f" % dur, "-i", media_path]
        filt = ("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
                "crop=%d:%d,fps=%d,format=yuv420p,settb=AVTB,"
                "setpts=PTS-STARTPTS[v%d]"
                % (idx, w, h, w, h, fps, idx))
    else:  # photo: SINGLE image -> zoompan d=N (classic smooth pattern).
        # Upscaled first so sub-pixel moves don't shimmer; `on` runs 0..N-1
        # deterministically across the single input frame.
        frames = max(int(round(dur * fps)), 1)
        in_args = ["-i", media_path]
        filt = ("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
                "crop=%d:%d,%s,format=yuv420p,settb=AVTB,"
                "setpts=PTS-STARTPTS[v%d]"
                % (idx, 2 * w, 2 * h, 2 * w, 2 * h,
                   _motion_filter(slot.motion_preset, frames, w, h, fps,
                                  intensity=intensity), idx))
    if head_fade > 0:
        filt = filt.replace("[v%d]" % idx,
                            ",fade=t=in:st=0:d=%.2f[v%d]" % (head_fade, idx))
    if tail_fade > 0:
        st = max(dur - tail_fade, 0.0)
        filt = filt.replace("[v%d]" % idx,
                            ",fade=t=out:st=%.2f:d=%.2f[v%d]"
                            % (st, tail_fade, idx))
    return in_args, filt


def _xfade_offsets(durations):
    """offset_k for the transition between seg k and seg k+1.

    offset_k = sum(d_0..d_k) - (k+1)*FADE_DUR   (k = 0..n-2)
    With wall-clock-extended durations this lands every fade exactly on a
    slot boundary.
    """
    offsets, total = [], 0.0
    for k, d in enumerate(durations[:-1]):
        total += d
        offsets.append(total - (k + 1) * FADE_DUR)
    return offsets


def _split_runs(chunk):
    """Split a chunk's slots into xfade-connected runs with boundary fades.

    Returns [(run, head_fade, tail_fade), ...] where run is a list of
    chunk-local slot indices forming one xfade chain. head_fade/tail_fade
    are FADE_DUR when the boundary transition demands it:
      - next run's first slot transition "dip-to-black"/"fadeblack"/"fade-in"
        -> this boundary fades the next run IN from black (head_fade)
      - "dip-to-black"/"fadeblack"/"fade-out" -> fades the previous run OUT
        to black (tail_fade)
      - "cut" -> no fades (hard concat)
    Fades are pixel-only; durations never change, so wall-clock is preserved.
    """
    runs, cur_run = [], [0]
    for j in range(1, len(chunk)):
        if chunk[j].transition == "xfade":
            cur_run.append(j)
        else:
            runs.append(cur_run)
            cur_run = [j]
    runs.append(cur_run)
    out = []
    for ri, run in enumerate(runs):
        t_in = chunk[run[0]].transition
        head = FADE_DUR if t_in in ("dip-to-black", "fadeblack", "fade-in") else 0.0
        tail = 0.0
        if ri + 1 < len(runs):
            t_next = chunk[runs[ri + 1][0]].transition
            if t_next in ("dip-to-black", "fadeblack", "fade-out"):
                tail = FADE_DUR
        out.append((run, head, tail))
    return out


def _render_chain_part(ffmpeg, slots, resolved, seg_durs, logo_path,
                       out_path, log_path, w, h, fps,
                       grade_preset=DEFAULT_GRADE,
                       head_fade=0.0, tail_fade=0.0):
    """Render one xfade-connected run of segments (+logo) to out_path.

    slots/resolved/seg_durs are parallel lists. seg_durs are the EXTENDED
    durations (slot + FADE_DUR except the run's last segment), so the
    post-xfade part duration equals the plain sum of slot durations and
    every transition starts exactly on a slot boundary.
    head_fade/tail_fade (M5): genuine fade-in at the run's head /
    fade-out at the run's tail (used for fade-in, fade-out and
    dip-to-black boundaries). Pixel-only, no duration change.
    """
    in_args, filters = [], []
    for j, (slot, mp, dur) in enumerate(zip(slots, resolved, seg_durs)):
        ia, f = _segment_chain(j, slot, mp, dur, w, h, fps,
                               head_fade=head_fade if j == 0 else 0.0,
                               tail_fade=tail_fade if j == len(slots) - 1 else 0.0)
        in_args += ia
        filters.append(f)
    n = len(slots)
    # xfade chain
    if n == 1:
        vlabel = "[v0]"
    else:
        prev = "[v0]"
        for k, off in enumerate(_xfade_offsets(seg_durs)):
            nxt = "[x%d]" % k
            filters.append("%s[v%d]xfade=transition=fade:duration=%.1f:offset=%.3f%s"
                           % (prev, k + 1, FADE_DUR, off, nxt))
            prev = nxt
        vlabel = prev
    part_dur = sum(seg_durs) - (n - 1) * FADE_DUR if n > 1 else seg_durs[0]
    # cinematic grade (before the logo so the logo is never graded)
    grade = GRADE_PRESETS.get(grade_preset or DEFAULT_GRADE, "")
    if grade:
        filters.append("%s%s[vgrd]" % (vlabel, grade))
        vlabel = "[vgrd]"
    # logo overlay (looped still, top-right, full part duration)
    if logo_path:
        in_args += ["-loop", "1", "-framerate", str(fps),
                    "-t", "%.3f" % part_dur, "-i", logo_path]
        filters.append("[%d:v]scale=%d:-1,format=yuv420p[lg]" % (n, LOGO_WIDTH))
        filters.append("%s[lg]overlay=W-w-%d:%d:format=yuv420[vout]"
                       % (vlabel, LOGO_MARGIN, LOGO_MARGIN))
        vlabel = "[vout]"
    cmd = [ffmpeg, "-y", "-hide_banner"] + in_args + [
        "-filter_complex", ";".join(filters),
        "-map", vlabel, "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-x264-params", "open-gop=0",  # closed GOPs: safe for demuxer concat
        out_path]
    _run(cmd, log_path)


def _concat_copy(ffmpeg, parts, out_path, log_path):
    """Join MP4s with identical codec/settings via demuxer (no re-encode).

    Safe because every part is rendered by _render_chain_part with identical
    settings and closed GOPs (open-gop=0), each starting with an IDR frame.
    """
    lst = out_path + ".lst"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            # ABSOLUTE paths: the concat demuxer resolves list entries relative
            # to the list file's directory, not the process CWD.
            ap = os.path.abspath(p)
            f.write("file '%s'\n" % ap.replace("'", "'\\''"))
    _run([ffmpeg, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
          "-i", lst, "-c", "copy", out_path], log_path)
    os.remove(lst)


def _concat_filter(ffmpeg, parts, out_path, log_path, fps):
    """Join parts via the concat filter (re-encode). 100% safe for the small
    intra-chunk joins where 'cut' transitions split an xfade chain."""
    inputs = []
    for p in parts:
        inputs += ["-i", p]
    n = len(parts)
    filt = "".join("[%d:v]" % i for i in range(n)) + \
        "concat=n=%d:v=1:a=0,format=yuv420p,settb=AVTB[v]" % n
    _run([ffmpeg, "-y", "-hide_banner"] + inputs +
          ["-filter_complex", filt, "-map", "[v]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-r", str(fps),
           "-x264-params", "open-gop=0", out_path], log_path)


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

def _ms(sec):
    return int(round(sec * 1000))


def _clean_mixer_local(mixer):
    """Clamp mixer dict to sane ranges; None -> legacy defaults."""
    from .timeline import DEFAULT_MIXER
    m = dict(DEFAULT_MIXER)
    for k in m:
        try:
            v = float((mixer or {}).get(k, m[k]))
        except (TypeError, ValueError):
            continue
        m[k] = min(1.0, max(0.0, v)) if k == "ducking" else min(3.0, max(0.0, v))
    return m


def _render_audio(ffmpeg, narration, bgm, sfx_events, media_dir, total_dur,
                  out_path, log_path, mixer=None):
    # M6: mixer volumes. Defaults reproduce the legacy hardcoded mix exactly
    # (narration 1.8 / bgm 0.4 / sfx neutral / ducking ratio 6).
    m = _clean_mixer_local(mixer)
    in_args, filters, labels = [], [], []
    idx = 0
    n_pad = None
    if narration:
        in_args += ["-i", narration]
        norm = ("[%d:a]aresample=48000:async=1,aformat=sample_rates=48000:"
                "channel_layouts=stereo,volume=%.2f" % (idx, m["narration_vol"]))
        # Pad the mix copy to the full timeline so the final audio is always
        # total_dur (atrim/-t can only cut, never extend; without this a
        # short narration + -shortest mux would truncate the video).
        pad = ",apad=whole_dur=%.2f" % total_dur
        if bgm:
            # Split: one copy mixes, one copy (padded to full length with
            # silence) drives the ducking sidechain so the BGM never ends
            # early when the voice stops.
            filters.append(norm + ",asplit[n_raw][n_sc]")
            filters.append("[n_raw]%s[n_mix]" % pad.lstrip(","))
            filters.append("[n_sc]apad=whole_dur=%.2f[n_pad]" % total_dur)
            labels.append("[n_mix]")
            n_pad = "[n_pad]"
        else:
            filters.append(norm + pad + "[n]")
            labels.append("[n]")
        idx += 1
    if bgm:
        in_args += ["-stream_loop", "-1", "-i", bgm]
        filters.append("[%d:a]aresample=48000:async=1,aformat=sample_rates=48000:channel_layouts=stereo,"
                       "volume=%.2f[b0]" % (idx, m["music_vol"]))
        if n_pad:
            if m["ducking"] > 0:
                # ducking 1.0 -> ratio 6 (legacy); 0 -> sidechain off
                ratio = 1.0 + 5.0 * m["ducking"]
                filters.append("[b0]%s sidechaincompress=threshold=0.03:ratio=%.1f:"
                               "attack=300:release=800[b]" % (n_pad, ratio))
            else:
                filters.append("[b0]anull[b]")
        else:
            filters.append("[b0]anull[b]")
        labels.append("[b]")
        idx += 1
    for k, ev in enumerate(sfx_events):
        p = ev["path"] if os.path.isabs(ev["path"]) else os.path.join(media_dir, ev["path"])
        if not os.path.isfile(p):
            continue
        in_args += ["-i", p]
        ms = _ms(ev["time_sec"])
        # per-event volume scaled by the mixer SFX slider (neutral default 1.0)
        vol = float(ev.get("volume", 1.0)) * m["sfx_vol"]
        filters.append("[%d:a]aresample=48000:async=1,aformat=sample_rates=48000:channel_layouts=stereo,"
                       "volume=%.2f,adelay=%d|%d[s%d]"
                       % (idx, vol, ms, ms, k))
        labels.append("[s%d]" % k)
        idx += 1
    if not labels:
        # silent track so the MP4 always has audio
        _run([ffmpeg, "-y", "-hide_banner", "-f", "lavfi",
              "-i", "anullsrc=r=48000:cl=stereo:d=%.2f" % total_dur,
              "-c:a", "aac", "-b:a", "128k", out_path], log_path)
        return
    filters.append("%samix=inputs=%d:duration=longest:dropout_transition=0,"
                   "loudnorm=I=-16:TP=-1.5:LRA=11,atrim=0:%.2f[aout]"
                   % ("".join(labels), len(labels), total_dur))
    cmd = ([ffmpeg, "-y", "-hide_banner"] + in_args +
           ["-filter_complex", ";".join(filters), "-map", "[aout]",
            "-t", "%.2f" % total_dur, "-c:a", "aac", "-b:a", "192k", out_path])
    _run(cmd, log_path)



import time as _time
from contextlib import contextmanager as _contextmanager

@_contextmanager
def _render_lock(work_dir):
    """Fail fast if another render already holds this work dir.

    Prevents two concurrent render_project() calls with the same out_mp4
    from interleaving chunk part files (one process deleting the other's
    part files mid-join). The lock file is created ATOMICALLY (O_EXCL) so
    the check-then-create race cannot happen. Stale locks (>6h) are treated
    as crashed and replaced.
    """
    import errno as _errno
    lock_path = os.path.join(work_dir, "render.lock")

    def _stale():
        try:
            return (_time.time() - os.path.getmtime(lock_path)) >= 21600
        except OSError:
            return True

    for _ in range(2):  # one retry after clearing a stale lock
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as e:
            if e.errno != _errno.EEXIST:
                raise
            if _stale():
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                continue
            raise RuntimeError(
                "Another render is already using %s. "
                "Wait for it to finish, or delete render.lock if it is stale."
                % work_dir)
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            break
    try:
        yield
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def _load_project(proj_json):
    if isinstance(proj_json, Project):
        return proj_json
    if isinstance(proj_json, dict):
        # interface-contract format (flat slots list) vs native .deproj (tracks)
        if "slots" in proj_json and "tracks" not in proj_json:
            return Project.from_contract_dict(proj_json)
        return Project.from_dict(proj_json)
    return Project.load(proj_json)  # path


def _split_chunks(slots):
    """Group slots into ~60s chunks (never splits a slot)."""
    chunks, cur, acc = [], [], 0.0
    for s in slots:
        if cur and acc + s.duration > CHUNK_TARGET:
            chunks.append(cur)
            cur, acc = [], 0.0
        cur.append(s)
        acc += s.duration
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------
# M6 captions: libass detection + burn-in
# --------------------------------------------------------------------------

_subtitles_filter_cache = {}


def _has_subtitles_filter(ffmpeg):
    """True if this ffmpeg build has the libass `subtitles` filter.

    Cached per ffmpeg path. Honest detection: we actually ask ffmpeg.
    """
    if ffmpeg in _subtitles_filter_cache:
        return _subtitles_filter_cache[ffmpeg]
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-h", "filter=subtitles"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        ok = (r.returncode == 0)
    except (OSError, subprocess.SubprocessError):
        ok = False
    _subtitles_filter_cache[ffmpeg] = ok
    return ok


def _escape_subs_path(path):
    """Escape a file path for the subtitles filter argument."""
    p = os.path.abspath(path)
    for ch in ("\\", "'", ":", ",", "[", "]"):
        p = p.replace(ch, "\\" + ch)
    return p


def _project_cues(project):
    """CaptionCue list from the project (dicts tolerated, empties dropped)."""
    cues = []
    for c in (getattr(project, "caption_cues", None) or []):
        cue = c if isinstance(c, _captions.CaptionCue) else _captions.CaptionCue.from_dict(c)
        if cue.text.strip() and cue.end_sec > cue.start_sec:
            cues.append(cue)
    return sorted(cues, key=lambda x: x.start_sec)


def render_project(proj_json, media_dir, out_mp4, progress_cb=None, work_dir=None,
                   ffmpeg=None, cancel_cb=None):
    """Render the project. Returns out_mp4 path. progress_cb(phase, done, total).

    cancel_cb: optional zero-arg callable; when it returns True the render
    stops at the next chunk boundary (raises RenderCancelled). Resume-safe:
    already-finished chunks stay cached, so re-running continues where it left off.
    """
    def progress(phase, done, total):
        if progress_cb:
            try:
                progress_cb(phase, done, total)
            except Exception:
                pass

    def check_cancel():
        if cancel_cb:
            try:
                if cancel_cb():
                    raise RenderCancelled("Render cancelled by user.")
            except RenderCancelled:
                raise
            except Exception:
                pass

    ffmpeg = ffmpeg or find_ffmpeg()
    project = _load_project(proj_json)
    slots = project.slots
    if not slots:
        raise RuntimeError("Project has no visual slots.")
    # M5: refuse to silently substitute an unrenderable transition.
    _check_transitions(slots)
    total_dur = project.total_duration

    # resolve media
    resolved = []
    for s in slots:
        p = project.resolve(media_dir, s.visual_path)
        if not p:
            raise RuntimeError("Missing visual for slot %.1fs-%.1fs: %s"
                               % (s.start_sec, s.end_sec, s.visual_path))
        resolved.append(p)
    narration = project.resolve(media_dir, project.narration_path)
    if not narration:
        raise RuntimeError("Narration audio is required in Phase 1.")
    bgm = project.resolve(media_dir, project.bgm_path)
    logo = project.resolve(media_dir, project.logo_path)

    work_dir = work_dir or os.path.join(os.path.dirname(os.path.abspath(out_mp4)),
                                        ".render_cache")
    os.makedirs(work_dir, exist_ok=True)
    chunk_dir = os.path.join(work_dir, "chunks")
    os.makedirs(chunk_dir, exist_ok=True)
    manifest_path = os.path.join(work_dir, "manifest.json")
    log_path = os.path.join(work_dir, "render.log")

    # Output geometry comes from the project (user-configurable); the module
    # constants are only fallbacks for projects that predate these fields.
    w = int(getattr(project, "width", 0) or W)
    h = int(getattr(project, "height", 0) or H)
    fps = int(getattr(project, "fps", 0) or FPS)

    chunks = _split_chunks(slots)
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    # Invalidate the cache if the project changed (slots/grade/geometry/
    # duration) so stale chunks at an old resolution are never reused.
    proj_hash = "%s|%d|%.2f|%s|%dx%d@%d" % (project.name, len(slots), total_dur,
                                           project.grade_preset, w, h, fps)
    if manifest.get("proj_hash") != proj_hash:
        manifest = {"proj_hash": proj_hash, "chunks": {}}
    done_map = manifest.get("chunks", {})

    with _render_lock(work_dir):
        # ---- video chunks (restart-safe) ----
        chunk_files, offset = [], 0
        for ci, ch in enumerate(chunks):
            check_cancel()
            key = str(ci)
            cfile = os.path.join(chunk_dir, "chunk_%03d.mp4" % ci)
            entry = done_map.get(key)
            if entry and entry.get("done") and os.path.isfile(cfile) and os.path.getsize(cfile) > 0:
                chunk_files.append(cfile)  # resume: skip finished chunk
                offset += len(ch)
                progress("video", ci + 1, len(chunks))
                continue
            # map chunk-local indices back to global slot/resolved lists
            base = offset
            part_files = []
            for ri, (run, run_head_fade, run_tail_fade) in enumerate(_split_runs(ch)):
                gidx = [base + j for j in run]
                gslots = [slots[g] for g in gidx]
                gres = [resolved[g] for g in gidx]
                # Extended durations: every non-final run segment gets +FADE_DUR so
                # the post-xfade part duration equals the plain slot sum (wall-clock
                # preserved; transitions start exactly on slot boundaries).
                seg_durs = []
                for j, (s, mp) in enumerate(zip(gslots, gres)):
                    need = s.duration + (FADE_DUR if j < len(run) - 1 else 0.0)
                    if s.visual_type == "clip":
                        src_dur = _probe_duration(mp)
                        avail = src_dur - s.clip_in if src_dur > 0 else need
                        if avail < need - 0.01:
                            with open(log_path, "a", encoding="utf-8") as lf:
                                lf.write("WARN: clip %s too short (need %.1fs, have %.1fs); "
                                         "chain will drift slightly.\n" % (mp, need, max(avail, 0)))
                            need = max(avail, 0.5)
                    seg_durs.append(need)
                part = os.path.join(chunk_dir, "chunk_%03d_part%d.mp4" % (ci, ri))
                _render_chain_part(ffmpeg, gslots, gres, seg_durs,
                                   logo, part, log_path, w, h, fps,
                                   grade_preset=project.grade_preset,
                                   head_fade=run_head_fade,
                                   tail_fade=run_tail_fade)
                part_files.append(part)
            if len(part_files) == 1:
                os.replace(part_files[0], cfile)
            else:
                # intra-chunk joins: concat filter (re-encode, always safe)
                _concat_filter(ffmpeg, part_files, cfile, log_path, fps)
                for p in part_files:
                    os.remove(p)
            done_map[key] = {"done": True, "file": os.path.basename(cfile)}
            manifest["chunks"] = done_map
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            chunk_files.append(cfile)
            offset += len(ch)
            progress("video", ci + 1, len(chunks))

        # ---- assemble video ----
        full_video = os.path.join(work_dir, "video_full.mp4")
        if len(chunk_files) == 1:
            shutil.copyfile(chunk_files[0], full_video)
        else:
            _concat_copy(ffmpeg, chunk_files, full_video, log_path)

        # ---- audio (single fast pass) ----
        check_cancel()
        progress("audio", 0, 1)
        sfx = [{"path": e.path, "time_sec": e.time_sec, "volume": e.volume}
               for e in project.tracks["sfx"].sfx_events]
        audio_full = os.path.join(work_dir, "audio_full.m4a")
        _render_audio(ffmpeg, narration, bgm, sfx, media_dir, total_dur,
                      audio_full, log_path, mixer=getattr(project, "mixer", None))
        progress("audio", 1, 1)

        # ---- captions (M6): SRT is always exported next to the MP4 when cues
        # exist. Burn-in is merged into the mux pass (no extra re-encode)
        # when this ffmpeg has the libass `subtitles` filter; otherwise we
        # skip burn-in HONESTLY (warn in the log) and still ship the SRT.
        # Captions live outside the chunk cache: chunk .mp4s are
        # caption-independent, so caption edits reuse cached chunks and only
        # re-run this cheap tail of the pipeline.
        check_cancel()
        progress("captions", 0, 1)
        cues = _project_cues(project)
        srt_path = os.path.splitext(out_mp4)[0] + ".srt"
        burn = False
        ass_path = ""
        if cues:
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(_captions.to_srt(cues))
            style = _captions.CaptionStyle.from_dict(project.caption_style)
            ass_path = os.path.join(work_dir, "captions.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(_captions.to_ass(cues, style, w, h))
            if _has_subtitles_filter(ffmpeg):
                burn = True
            else:
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write("WARN: ffmpeg subtitles filter (libass) not "
                             "available — caption burn-in skipped; SRT still "
                             "exported to %s\n" % srt_path)
        elif os.path.isfile(srt_path):
            os.remove(srt_path)  # never leave a stale SRT beside a cueless MP4
        progress("captions", 1, 1)

        # ---- mux ----
        check_cancel()
        progress("mux", 0, 1)
        if burn:
            _run([ffmpeg, "-y", "-hide_banner", "-i", full_video, "-i", audio_full,
                  "-map", "0:v", "-map", "1:a",
                  "-vf", "subtitles=" + _escape_subs_path(ass_path),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-c:a", "aac", "-b:a", "192k", "-shortest",
                  "-movflags", "+faststart", out_mp4],
                 log_path)
        else:
            _run([ffmpeg, "-y", "-hide_banner", "-i", full_video, "-i", audio_full,
                  "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                  "-b:a", "192k", "-shortest", "-movflags",
                  "+faststart", out_mp4],
                 log_path)
        progress("mux", 1, 1)
    return out_mp4
