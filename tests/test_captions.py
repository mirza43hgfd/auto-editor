"""M6 captions + audio mixer tests. Qt-free, no network.

Part 1: captions.py unit tests — cue generation timing math, phrase
        splitting (incl. Urdu punctuation), SRT round-trip, validation
        (overlap/gap/out-of-bounds/empty), edit ops (shift/split/merge/
        edit), style templates round-trip, ASS generation sanity,
        cue_hash stability, Project serialization round-trip, mixer
        cleaning/clamping.
Part 2: mixer unit tests — _render_audio honors mixer volumes (filter
        string inspection via a stubbed _run).
Part 3: RENDER PROOF (synthetic ffmpeg) — a 2-slot 1280x720 project with
        cues renders: ffprobe duration OK, .srt exported next to the MP4
        with correct timings, and a bottom-strip frame from the captioned
        render differs from the same render without cues (proving burn-in
        actually drew text). If this ffmpeg lacks the libass subtitles
        filter, the honest fallback is asserted instead: SRT still exports,
        the render succeeds, and render.log carries the skip warning.

Run:  .venv/bin/python tests/test_captions.py
Exit 0 = all pass; non-zero = real failure.
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import Project, Slot, DEFAULT_MIXER, _clean_mixer
from auto_editor import captions as cap
from auto_editor.captions import (
    CaptionCue, CaptionStyle, TEMPLATES, generate_cues, validate_cues,
    to_srt, parse_srt, to_ass, cue_hash, shift_cue, split_cue, merge_cues,
    edit_cue, split_into_phrases)
from auto_editor import render_worker as rw

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        print("FAIL  %s  [%s]" % (name, detail))


def sh(cmd):
    pr = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    if pr.returncode != 0:
        raise RuntimeError("command failed: %s" % " ".join(cmd[:4]))


# --------------------------------------------------------------------------
# Part 1: caption engine units
# --------------------------------------------------------------------------

def _proj_two_slots():
    p = Project("cap-units")
    p.add_slot(Slot(0, 6, narration_segment=(
        "Pehla jumla yahan hai. Dusra jumla wahan hai! Teesra sawal hai?")))
    p.add_slot(Slot(6, 12, narration_segment=(
        "Chautha jumla. Paanchwan jumla, lamba wala, comma ke saath.")))
    return p


p = _proj_two_slots()
cues = generate_cues(p)
check("generate: 5 cues from 2 slots", len(cues) == 5, len(cues))

# timing math: cues are contiguous and cover each slot window
by_slot = {}
for c in cues:
    key = 0 if c.start_sec < 6 else 1
    by_slot.setdefault(key, []).append(c)
for key, win in ((0, 6.0), (1, 6.0)):
    total = sum(c.duration for c in by_slot[key])
    check("slot %d: cue durations sum ~= window (%.2f vs %.2f)"
          % (key, total, win), abs(total - win) < 0.05, total)
    check("slot %d: last cue snaps to slot end" % key,
          abs(by_slot[key][-1].end_sec - (key + 1) * 6.0) < 0.01,
          by_slot[key][-1].end_sec)
    for a, b in zip(by_slot[key], by_slot[key][1:]):
        check("slot %d: cues contiguous, no overlap" % key,
              abs(a.end_sec - b.start_sec) < 0.01, (a.end_sec, b.start_sec))

check("validate: generated cues are clean",
      validate_cues(p, cues) == [], validate_cues(p, cues))

# Urdu punctuation splitting (tiny trailing fragment merges backward by design)
ph = split_into_phrases("Ye pehla jumla hai۔ Ye dusra hai؟ Teesra!")
check("split: Urdu punctuation splits phrases",
      ph == ["Ye pehla jumla hai۔", "Ye dusra hai؟ Teesra!"], ph)
check("split: empty text -> []", split_into_phrases("   ") == [])
check("split: tiny fragments merge",
      len(split_into_phrases("Aur phir. Bas.")) <= 2,
      split_into_phrases("Aur phir. Bas."))

# slot with no narration -> no cues (honest, not fake)
p_empty = Project("x")
p_empty.add_slot(Slot(0, 4, label=""))
check("generate: slot without narration text -> no cues",
      generate_cues(p_empty) == [])

# SRT round-trip
srt = to_srt(cues)
check("srt: starts with index 1", srt.startswith("1\n"))
check("srt: timestamp format",
      bool(re.search(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", srt)))
back = parse_srt(srt)
check("srt: round-trip count", len(back) == len(cues), len(back))
ok_rt = all(abs(a.start_sec - b.start_sec) < 0.002 and
            abs(a.end_sec - b.end_sec) < 0.002 and
            a.text.strip() == b.text.strip()
            for a, b in zip(sorted(cues, key=lambda c: c.start_sec), back))
check("srt: round-trip timings+text preserved", ok_rt)

# validation catches problems
bad = [CaptionCue(0, 2, "ok"), CaptionCue(1.5, 3, "overlap"),
       CaptionCue(5, 9, "   "), CaptionCue(20, 21, "oob")]
issues = validate_cues(p, bad)
kinds = {i["kind"] for i in issues}
check("validate: overlap caught", "overlap" in kinds, kinds)
check("validate: empty text caught", "empty" in kinds, kinds)
check("validate: out_of_bounds caught", "out_of_bounds" in kinds, kinds)
gap_issues = validate_cues(p, [CaptionCue(0, 1, "a"), CaptionCue(3, 4, "b")])
check("validate: gap flagged as warning",
      any(i["kind"] == "gap" and i["severity"] == "warning" for i in gap_issues),
      gap_issues)

# edit ops
c0 = CaptionCue(2, 5, "hello world test")
moved = shift_cue(c0, -5)
check("shift: clamps at 0", moved.start_sec == 0.0 and moved.duration == 3.0,
      (moved.start_sec, moved.end_sec))
moved2 = shift_cue(c0, 1.5)
check("shift: preserves duration+id",
      abs(moved2.duration - 3.0) < 1e-9 and moved2.id == c0.id)
f, s = split_cue(c0, 3.0)
check("split: two cues cover original",
      f is not None and s is not None and
      abs(f.start_sec - 2.0) < 1e-9 and abs(s.end_sec - 5.0) < 1e-9 and
      abs(f.end_sec - s.start_sec) < 1e-9, (f, s))
check("split: text divided at word",
      (f.text + " " + s.text) == "hello world test", (f.text, s.text))
f2, s2 = split_cue(c0, 99)
check("split: invalid point -> (cue, None)", s2 is None and f2 is c0)
m = merge_cues(CaptionCue(0, 2, "ek"), CaptionCue(2.5, 4, "do"))
check("merge: spans both, joins text",
      m.start_sec == 0 and m.end_sec == 4 and m.text == "ek do", m)
e = edit_cue(c0, text="  naya  ", start=10, end=5)
check("edit: clamps inverted range",
      e.text == "  naya  " and e.end_sec > e.start_sec,
      (e.start_sec, e.end_sec))

# templates
check("templates: 3 built-ins",
      set(TEMPLATES) == {"cinematic", "clean", "bold-pop"}, set(TEMPLATES))
for name in TEMPLATES:
    st = CaptionStyle.from_template(name)
    rt = CaptionStyle.from_dict(json.loads(json.dumps(st.to_dict())))
    check("template %s: dict round-trip" % name,
          rt.to_dict() == st.to_dict())
garbage = CaptionStyle.from_dict({"color": "notacolor", "position": "sideways",
                                  "size": 9999, "name": "x"})
check("style: garbage sanitized",
      garbage.color == "#FFFFFF" and garbage.position == "bottom" and
      garbage.size == 160, garbage.to_dict())

# ASS sanity
ass = to_ass(cues, CaptionStyle.from_template("cinematic"), 1920, 1080)
check("ass: Script Info + PlayRes",
      "[Script Info]" in ass and "PlayResX: 1920" in ass and
      "PlayResY: 1080" in ass)
check("ass: style line carries font",
      "Style: Cap,Georgia," in ass)
check("ass: one Dialogue per cue",
      sum(1 for ln in ass.splitlines() if ln.startswith("Dialogue")) == len(cues),
      len(cues))
check("ass: ASS timestamps",
      "Dialogue: 0,0:00:00.00,0:00:02.16," in ass)
check("ass: no raw braces leak into text",
      all("{" not in ln and "}" not in ln
          for ln in ass.splitlines() if ln.startswith("Dialogue")))
hl_style = CaptionStyle.from_template("bold-pop")
hl_style.highlight_words = ["jumla"]
hl_style.highlight_color = "#FFE135"
ass_hl = to_ass([CaptionCue(0, 2, "Pehla jumla yahan")], hl_style, 1920, 1080)
check("ass: highlight override tags",
      r"{\1c&H0035E1FF}" in ass_hl and r"{\1c}" in ass_hl, ass_hl.splitlines()[-1])

# cue_hash
h1 = cue_hash(cues, p.caption_style)
h2 = cue_hash(cues, p.caption_style)
check("cue_hash: stable", h1 == h2, (h1, h2))
cues_mod = [edit_cue(cues[0], text="badla hua")]
check("cue_hash: changes with content",
      cue_hash(cues_mod, p.caption_style) != h1)

# Project serialization round-trip
p.caption_cues = cues
p.mixer = {"narration_vol": 2.0, "music_vol": 0.1, "sfx_vol": 1.5, "ducking": 0.0}
d = json.loads(json.dumps(p.to_dict()))
p2 = Project.from_dict(d)
check("project: cues survive round-trip", len(p2.caption_cues) == len(cues),
      len(p2.caption_cues))
check("project: mixer survives round-trip",
      p2.mixer == {"narration_vol": 2.0, "music_vol": 0.1,
                   "sfx_vol": 1.5, "ducking": 0.0}, p2.mixer)
check("project: style survives round-trip",
      p2.caption_style.get("name") == "cinematic", p2.caption_style.get("name"))
c0b, c1b = p2.caption_cues[0], cues[0]
check("project: cue fields intact",
      abs(c0b.start_sec - c1b.start_sec) < 1e-9 and c0b.text == c1b.text)

# old project without M6 keys -> defaults (backward compat)
p3 = Project.from_dict({"name": "old", "tracks": {}})
check("project: pre-M6 file -> empty cues + default mixer",
      p3.caption_cues == [] and p3.mixer == DEFAULT_MIXER, p3.mixer)

# mixer cleaning
check("mixer: garbage -> defaults", _clean_mixer(None) == DEFAULT_MIXER)
check("mixer: clamps ranges",
      _clean_mixer({"narration_vol": 99, "ducking": -5,
                    "music_vol": "xx"}) == {"narration_vol": 3.0,
                                            "music_vol": 0.4, "sfx_vol": 1.0,
                                            "ducking": 0.0},
      _clean_mixer({"narration_vol": 99, "ducking": -5}))

# --------------------------------------------------------------------------
# Part 2: mixer wiring in _render_audio (no real render; stub _run)
# --------------------------------------------------------------------------

captured = {}


def fake_run(cmd, log_path=None):
    captured["cmd"] = cmd
    captured["log"] = log_path
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("FAKE-RUN\n")


real_run = rw._run
rw._run = fake_run
try:
    rw._render_audio("ffmpeg", "/tmp/nonexistent_n.mp3", None, [],
                     "/tmp", 4.0, "/tmp/o.m4a", "/tmp/l.log")
    cmd = " ".join(captured["cmd"])
    check("audio: default mixer == legacy (narration 1.8)",
          "volume=1.80" in cmd, cmd[:200])

    rw._render_audio("ffmpeg", "/tmp/nonexistent_n.mp3", "/tmp/nonexistent_b.mp3",
                     [], "/tmp", 4.0, "/tmp/o.m4a", "/tmp/l.log",
                     mixer={"narration_vol": 2.5, "music_vol": 0.1,
                            "sfx_vol": 1.0, "ducking": 0.0})
    cmd = " ".join(captured["cmd"])
    check("audio: mixer volumes applied",
          "volume=2.50" in cmd and "volume=0.10" in cmd, cmd[:300])
    check("audio: ducking=0 disables sidechain",
          "sidechaincompress" not in cmd, cmd[:300])

    rw._render_audio("ffmpeg", "/tmp/nonexistent_n.mp3", "/tmp/nonexistent_b.mp3",
                     [], "/tmp", 4.0, "/tmp/o.m4a", "/tmp/l.log",
                     mixer={"ducking": 1.0})
    cmd = " ".join(captured["cmd"])
    check("audio: ducking=1 keeps legacy ratio=6",
          "sidechaincompress=threshold=0.03:ratio=6.0" in cmd, cmd[:300])
finally:
    rw._run = real_run

check("subs filter detection returns bool",
      isinstance(rw._has_subtitles_filter("ffmpeg"), bool))
check("subs path escaping",
      rw._escape_subs_path("/tmp/a'b/c:d.ass") == "/tmp/a\\'b/c\\:d.ass",
      rw._escape_subs_path("/tmp/a'b/c:d.ass"))

# --------------------------------------------------------------------------
# Part 3: RENDER PROOF (synthetic)
# --------------------------------------------------------------------------

T = os.path.join(HERE, "test_media_captions")
OUT = os.path.join(HERE, "out_captions")
os.makedirs(T, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

print("generating synthetic media...")
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=navy:s=1280x720:d=1",
    "-frames:v", "1", "%s/a.png" % T])
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=maroon:s=1280x720:d=1",
    "-frames:v", "1", "%s/b.png" % T])
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
    "-c:a", "libmp3lame", "%s/narration.mp3" % T])

pr = Project("m6-render-proof")
pr.width, pr.height, pr.fps = 1280, 720, 30
pr.tracks["narration"].media_path = "%s/narration.mp3" % T
pr.add_slot(Slot(0, 4, "%s/a.png" % T, "photo", "none", "cut",
                 narration_segment="Pehla caption yahan aayega. Dusra bhi.",
                 label="s1"))
pr.add_slot(Slot(4, 8, "%s/b.png" % T, "photo", "none", "cut",
                 narration_segment="Teesra caption idhar hai.",
                 label="s2"))
pr.caption_cues = generate_cues(pr)
pr.caption_style = CaptionStyle.from_template("bold-pop").to_dict()
check("proof: cues generated", len(pr.caption_cues) == 2, len(pr.caption_cues))

has_libass = rw._has_subtitles_filter("ffmpeg")
print("libass subtitles filter present: %s" % has_libass)

out_cap = os.path.join(OUT, "m6_captioned.mp4")
print("rendering WITH captions...")
rw.render_project(pr.to_dict(), T, out_cap, progress_cb=lambda *a: None)

srt_path = os.path.splitext(out_cap)[0] + ".srt"
check("proof: SRT exported next to MP4", os.path.isfile(srt_path), srt_path)
srt_text = open(srt_path, encoding="utf-8").read() if os.path.isfile(srt_path) else ""
check("proof: SRT has cue text + timings",
      "Pehla caption" in srt_text and "00:00:04,000" in srt_text,
      srt_text[:200])
check("proof: SRT parses back to 2 cues", len(parse_srt(srt_text)) == 2)


def probe_dur(path):
    pr2 = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return float(pr2.stdout.strip())


def bottom_strip_mean(path, t, w=1280, h=720, strip=160):
    """Mean abs pixel value of the bottom strip (where captions burn)."""
    pr2 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-ss", "%.2f" % t,
         "-frames:v", "1", "-vf", "crop=%d:%d:0:%d" % (w, strip, h - strip),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw = pr2.stdout
    assert len(raw) == w * strip * 3, "short frame (%d bytes)" % len(raw)
    return raw


dur = probe_dur(out_cap)
check("proof: duration ~8s", abs(dur - 8) < 1.0, dur)

# same project WITHOUT cues -> captionless render for frame comparison
pr2 = Project.from_dict(json.loads(json.dumps(pr.to_dict())))
pr2.caption_cues = []
out_plain = os.path.join(OUT, "m6_plain.mp4")
print("rendering WITHOUT captions (comparison)...")
rw.render_project(pr2.to_dict(), T, out_plain, progress_cb=lambda *a: None)
check("proof: no stale SRT beside cueless render",
      not os.path.isfile(os.path.splitext(out_plain)[0] + ".srt"))

log_text = open(os.path.join(OUT, ".render_cache", "render.log"),
                encoding="utf-8", errors="replace").read() \
    if os.path.isfile(os.path.join(OUT, ".render_cache", "render.log")) else ""

if has_libass:
    # bottom strip at t=1.5 (a cue is on screen): captioned must differ
    a = bottom_strip_mean(out_cap, 1.5)
    b = bottom_strip_mean(out_plain, 1.5)
    diff = sum(abs(x - y) for x, y in zip(a[::16], b[::16])) / (len(a) / 16)
    check("proof: burn-in drew pixels (bottom strip differs)",
          diff > 2.0, round(diff, 2))
    # top strip (no captions there) should be ~identical: proves the ONLY
    # difference is the caption text, not a re-encode artifact
    def top_strip(path, t, w=1280, h=720, strip=160):
        pr2 = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-ss", "%.2f" % t,
             "-frames:v", "1", "-vf", "crop=%d:%d:0:0" % (w, strip),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return pr2.stdout
    ta, tb = top_strip(out_cap, 1.5), top_strip(out_plain, 1.5)
    tdiff = sum(abs(x - y) for x, y in zip(ta[::16], tb[::16])) / (len(ta) / 16)
    check("proof: top strip ~identical (only captions differ)",
          tdiff < 8.0, round(tdiff, 2))
else:
    check("proof: honest fallback — warn in render.log",
          "subtitles filter" in log_text and "SRT" in log_text,
          log_text[-200:])
    check("proof: fallback still exported SRT", os.path.isfile(srt_path))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
