"""Headless render worker.

render_project(proj_json, media_dir, out_mp4, progress_cb=None)
  -> 1920x1080 30fps H.264 MP4 with:
     - photo Ken Burns motion (ffmpeg zoompan)
     - video clip subsegments (-ss/-t)
     - xfade transitions between consecutive visuals
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

W, H, FPS = 1920, 1080, 30
FADE_DUR = 1.0          # xfade seconds
CHUNK_TARGET = 60.0     # ~60s restart-safe chunks
LOGO_WIDTH = 180
LOGO_MARGIN = 20


class RenderCancelled(Exception):
    """User cancelled the render (resume-safe: finished chunks stay cached)."""

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

def _motion_filter(preset, frames):
    """zoompan args for a Ken Burns preset. frames baked in (no runtime math)."""
    n = max(frames, 1)
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    if preset == "zoom-out":
        z = "1.15-0.15*on/%d" % n
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
        z = "1+0.15*on/%d" % n
        x, y = cx, cy
    return ("zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d"
            % (z, x, y, n, W, H, FPS))


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


def _segment_chain(idx, slot, media_path, dur):
    """Return (input_args, filter) for one slot -> labeled [v{idx}].

    dur is the EXTENDED duration (slot + FADE_DUR for non-final chain
    segments) so xfade overlaps don't shrink the wall-clock timeline.
    """
    if slot.visual_type == "clip":
        in_args = ["-ss", "%.3f" % slot.clip_in, "-t", "%.3f" % dur, "-i", media_path]
        filt = ("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
                "crop=%d:%d,fps=%d,format=yuv420p,settb=AVTB,"
                "setpts=PTS-STARTPTS[v%d]"
                % (idx, W, H, W, H, FPS, idx))
    else:  # photo: SINGLE image -> zoompan d=N (classic smooth pattern).
        # Upscaled first so sub-pixel moves don't shimmer; `on` runs 0..N-1
        # deterministically across the single input frame.
        frames = max(int(round(dur * FPS)), 1)
        in_args = ["-i", media_path]
        filt = ("[%d:v]scale=3840:2160:force_original_aspect_ratio=increase,"
                "crop=3840:2160,%s,format=yuv420p,settb=AVTB,"
                "setpts=PTS-STARTPTS[v%d]"
                % (idx, _motion_filter(slot.motion_preset, frames), idx))
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


def _render_chain_part(ffmpeg, slots, resolved, seg_durs, logo_path,
                       out_path, log_path, grade_preset=DEFAULT_GRADE):
    """Render one xfade-connected run of segments (+logo) to out_path.

    slots/resolved/seg_durs are parallel lists. seg_durs are the EXTENDED
    durations (slot + FADE_DUR except the run's last segment), so the
    post-xfade part duration equals the plain sum of slot durations and
    every transition starts exactly on a slot boundary.
    """
    in_args, filters = [], []
    for j, (slot, mp, dur) in enumerate(zip(slots, resolved, seg_durs)):
        ia, f = _segment_chain(j, slot, mp, dur)
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
        in_args += ["-loop", "1", "-framerate", str(FPS),
                    "-t", "%.3f" % part_dur, "-i", logo_path]
        filters.append("[%d:v]scale=%d:-1,format=yuv420p[lg]" % (n, LOGO_WIDTH))
        filters.append("%s[lg]overlay=W-w-%d:%d:format=yuv420[vout]"
                       % (vlabel, LOGO_MARGIN, LOGO_MARGIN))
        vlabel = "[vout]"
    cmd = [ffmpeg, "-y", "-hide_banner"] + in_args + [
        "-filter_complex", ";".join(filters),
        "-map", vlabel, "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
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


def _concat_filter(ffmpeg, parts, out_path, log_path):
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
           "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-x264-params", "open-gop=0", out_path], log_path)


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

def _ms(sec):
    return int(round(sec * 1000))


def _render_audio(ffmpeg, narration, bgm, sfx_events, media_dir, total_dur,
                  out_path, log_path):
    in_args, filters, labels = [], [], []
    idx = 0
    n_pad = None
    if narration:
        in_args += ["-i", narration]
        norm = ("[%d:a]aresample=48000:async=1,aformat=sample_rates=48000:"
                "channel_layouts=stereo,volume=1.8" % idx)
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
                       "volume=0.4[b0]" % idx)
        if n_pad:
            filters.append("[b0]%s sidechaincompress=threshold=0.03:ratio=6:"
                           "attack=300:release=800[b]" % n_pad)
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
        filters.append("[%d:a]aresample=48000:async=1,aformat=sample_rates=48000:channel_layouts=stereo,"
                       "volume=%.2f,adelay=%d|%d[s%d]"
                       % (idx, ev.get("volume", 0.7), ms, ms, k))
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

    chunks = _split_chunks(slots)
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    # Invalidate the cache if the project changed (slots/grade/duration).
    proj_hash = "%s|%d|%.2f|%s" % (project.name, len(slots), total_dur,
                                   project.grade_preset)
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
            # split chunk into xfade-connected runs (cut/fadeblack breaks the chain;
            # fadeblack is rendered as xfade in Phase 1)
            runs, cur_run = [], [0]
            for j in range(1, len(ch)):
                if ch[j].transition == "xfade":
                    cur_run.append(j)
                else:
                    runs.append(cur_run)
                    cur_run = [j]
            runs.append(cur_run)
            # map chunk-local indices back to global slot/resolved lists
            base = offset
            part_files = []
            for ri, run in enumerate(runs):
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
                                   logo, part, log_path,
                                   grade_preset=project.grade_preset)
                part_files.append(part)
            if len(part_files) == 1:
                os.replace(part_files[0], cfile)
            else:
                # intra-chunk joins: concat filter (re-encode, always safe)
                _concat_filter(ffmpeg, part_files, cfile, log_path)
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
                      audio_full, log_path)
        progress("audio", 1, 1)

        # ---- mux ----
        check_cancel()
        progress("mux", 0, 1)
        _run([ffmpeg, "-y", "-hide_banner", "-i", full_video, "-i", audio_full,
              "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
              "-b:a", "192k", "-shortest", "-movflags", "+faststart", out_mp4],
             log_path)
        progress("mux", 1, 1)
    return out_mp4
