"""M5 editing-decision + genuine-transition tests. Qt-free, no network.

Part 1: decide_edit() unit tests — quiet/action/end/continuation/change,
        determinism, no fixed preset cycling, apply_editing_decisions().
Part 2: renderer unit tests — _split_runs() boundary fades,
        _check_transitions() loud failure, _motion_filter() intensity.
Part 3: RENDER PROOF (synthetic ffmpeg) — a 3-slot 1280x720 project with
        transitions [fade-in, dip-to-black, fade-out] is rendered and frames
        are extracted: the dip midpoint must be near-black (proving
        dip-to-black rendered instead of being substituted with xfade) and
        the fade-in must progress dark -> bright.

Run:  .venv/bin/python tests/test_editing.py
Exit 0 = all pass; non-zero = real failure.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import (
    Project, Slot, set_slot_field, TRANSITIONS)
from auto_editor.editing import (
    decide_edit, apply_editing_decisions, EditDecision)
from auto_editor import render_worker as rw

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        print("FAIL  %s  %s" % (name, detail))


def mk(text, purpose="", label="", chars=None, start=0, end=4):
    return Slot(start, end, "v.png", "photo",
                narration_segment=text, purpose=purpose, label=label,
                characters=chars or [])


# ------------------------------------------------- Part 1: decide_edit()

# 1. quiet emotional scene -> gentle fade + longer hold
s = mk("At dawn he prayed alone in silence, tears on his face.")
d = decide_edit(s, mk("x"), None)
check("quiet -> gentle transition", d.transition in ("xfade", "fade-in"),
      d.transition)
check("quiet -> longer hold", d.hold_factor > 1.0, d.hold_factor)
check("quiet -> restrained motion", d.motion_preset in ("none", "zoom-out"),
      d.motion_preset)
check("quiet -> notes explain why", "Quiet" in d.notes, d.notes)

# 2. action scene -> hard cut, tighter pacing
s = mk("The battle began with a scream of swords as the army charged.")
d = decide_edit(s, mk("x"), mk("y"))
check("action -> hard cut", d.transition == "cut", d.transition)
check("action -> tighter hold", d.hold_factor < 1.0, d.hold_factor)
check("action -> energetic motion", d.motion_preset == "zoom-in",
      d.motion_preset)

# 3. section end -> dip-to-black
s = mk("The army marched home.", purpose="Conclusion of the chapter")
d = decide_edit(s, mk("x"), None)
check("section-end -> dip-to-black", d.transition == "dip-to-black",
      d.transition)
check("section-end -> hold", d.hold_factor >= 1.0, d.hold_factor)

# 4. continuation of the same moment -> hard cut
a = mk("He looked at the horizon.", chars=["c1"])
b = mk("The wind moved the sand.", chars=["c1"])
d = decide_edit(a, mk("Earlier that day."), b)
check("continuation -> hard cut", d.transition == "cut", d.transition)
check("continuation -> restrained", d.motion_preset == "none",
      d.motion_preset)

# 5. character change -> gentle crossfade
a = mk("He looked at the horizon.", chars=["c1"])
b = mk("Far away, the enemy camp stirred.", chars=["c2"])
d = decide_edit(a, mk("Earlier that day."), b)
check("character-change -> crossfade", d.transition == "xfade",
      d.transition)

# 6. opening slot, neutral -> fade in from black
s = mk("A man walked across the desert.")
d = decide_edit(s, None, mk("y"))
check("opening -> fade-in", d.transition == "fade-in", d.transition)

# 7. determinism: same inputs -> identical decision
s = mk("At dawn he prayed alone in silence.")
d1 = decide_edit(s, None, None)
d2 = decide_edit(s, None, None)
check("deterministic (same slot twice)", d1 == d2, repr(d1))

# 8. NO fixed cycling: six varied scenes must not all share one transition
varied = [
    mk("At dawn he prayed alone in silence."),                    # quiet
    mk("The battle began with a scream."),                        # action
    mk("He raised his sword.", chars=["c1"]),                     # neutral
    mk("The blade caught the sun.", chars=["c1"]),                # continue
    mk("Far away the enemy camp stirred.", chars=["c2"]),         # change
    mk("And so the story ends.", purpose="Epilogue"),             # end
]
seen = set()
for i, sl in enumerate(varied):
    prev = varied[i - 1] if i > 0 else None
    nxt = varied[i + 1] if i + 1 < len(varied) else None
    seen.add(decide_edit(sl, prev, nxt).transition)
check("no fixed cycling (varied scenes vary)", len(seen) > 1,
      "all -> %s" % seen)

# 9. apply_editing_decisions stores everything on the slots
p = Project("m5")
p.add_slot(mk("At dawn he prayed alone in silence.", start=0, end=4))
p.add_slot(mk("The battle began with a scream.", start=4, end=8))
p.add_slot(mk("The end.", purpose="Conclusion", start=8, end=12))
out = apply_editing_decisions(p)
check("apply returns one decision per slot", len(out) == 3, len(out))
check("decisions stored (transition)", p.slots[1].transition == "cut",
      p.slots[1].transition)
check("decisions stored (hold_factor)",
      abs(p.slots[0].hold_factor - 1.25) < 1e-9, p.slots[0].hold_factor)
check("decisions stored (edit_notes)", bool(p.slots[2].edit_notes),
      p.slots[2].edit_notes[:40])
check("decisions stored (grade_hint)", bool(p.slots[1].grade_hint),
      p.slots[1].grade_hint)

# 10. serialization round-trips the M5 fields
d1 = p.to_dict()
p2 = Project.from_dict(d1)
check("deproj round-trip keeps transition",
      p2.slots[2].transition == "dip-to-black", p2.slots[2].transition)
check("deproj round-trip keeps hold_factor",
      abs(p2.slots[0].hold_factor - 1.25) < 1e-9)
check("deproj round-trip keeps edit_notes",
      p2.slots[1].edit_notes == p.slots[1].edit_notes)
c1 = p.to_contract_dict("/tmp")
p3 = Project.from_contract_dict(c1, "/tmp")
check("contract round-trip keeps decisions",
      p3.slots[0].transition == p.slots[0].transition
      and abs(p3.slots[0].hold_factor - 1.25) < 1e-9)

# 11. inspector field validation
s = mk("x")
p.add_slot(s)
check("set_slot_field rejects bad transition",
      set_slot_field(p, s.id, "transition", "spiral-wipe") is False)
check("set_slot_field accepts dip-to-black",
      set_slot_field(p, s.id, "transition", "dip-to-black") is True
      and s.transition == "dip-to-black")
check("set_slot_field clamps hold_factor",
      set_slot_field(p, s.id, "hold_factor", 9.0) is True
      and s.hold_factor == 2.0, s.hold_factor)
check("set_slot_field stores edit_notes",
      set_slot_field(p, s.id, "edit_notes", "Manual choice") is True
      and s.edit_notes == "Manual choice")

# ------------------------------------------------- Part 2: renderer units

# 12. _split_runs boundary fades
ch = [Slot(0, 1, transition=t) for t in
      ("fade-in", "dip-to-black", "fade-out", "xfade", "cut")]
runs = rw._split_runs(ch)
got = [(r, h, t) for (r, h, t) in runs]
check("split_runs: 4 runs", [r for r, _, _ in got]
      == [[0], [1], [2, 3], [4]], str([r for r, _, _ in got]))
check("split_runs: fade-in head", got[0][1] == rw.FADE_DUR, got[0][1])
check("split_runs: dip boundary tail+head",
      got[0][2] == rw.FADE_DUR and got[1][1] == rw.FADE_DUR,
      (got[0][2], got[1][1]))
check("split_runs: fade-out tail", got[1][2] == rw.FADE_DUR, got[1][2])
check("split_runs: cut = no fades",
      got[2][2] == 0.0 and got[3][1] == 0.0, (got[2][2], got[3][1]))
check("split_runs: xfade chain unbroken", got[2][0] == [2, 3])

# legacy fadeblack behaves exactly like dip-to-black
ch2 = [Slot(0, 1, transition="cut"), Slot(1, 2, transition="fadeblack")]
r2 = rw._split_runs(ch2)
check("fadeblack == dip-to-black (head fade)",
      r2[1][1] == rw.FADE_DUR, r2[1][1])
check("fadeblack == dip-to-black (tail fade)",
      r2[0][2] == rw.FADE_DUR, r2[0][2])

# 13. _check_transitions: loud failure, never silent substitution
s = mk("x")
s.transition = "spiral-wipe"  # bypass model coercion, as raw input could
try:
    rw._check_transitions([s])
    check("check_transitions raises on garbage", False, "no raise")
except RuntimeError as e:
    check("check_transitions raises on garbage", "spiral-wipe" in str(e),
          str(e)[:60])
ok_slots = [Slot(0, 1, transition=t) for t in TRANSITIONS]
try:
    rw._check_transitions(ok_slots)
    check("check_transitions accepts all TRANSITIONS", True)
except RuntimeError as e:
    check("check_transitions accepts all TRANSITIONS", False, str(e)[:80])

# 14. motion intensity from hold_factor
f1 = rw._motion_filter("zoom-in", 90, 1280, 720, 30, intensity=1.0)
f2 = rw._motion_filter("zoom-in", 90, 1280, 720, 30, intensity=0.5)
check("motion intensity changes zoom amplitude",
      f1 != f2 and "0.150" in f1 and "0.075" in f2, f1[:60])
f3 = rw._motion_filter("none", 90, 1280, 720, 30, intensity=0.5)
check("motion 'none' unaffected by intensity", "z='1.0'" in f3, f3[:60])


# ------------------------------------------------- Part 3: RENDER PROOF
def sh(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    assert p.returncode == 0, "CMD failed: %s\n%s" % (
        " ".join(cmd), p.stderr[-600:])


T = os.path.join(HERE, "test_media_editing")
OUT = os.path.join(HERE, "out_editing")
os.makedirs(T, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

print("generating synthetic media...")
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1280x720:d=1",
    "-frames:v", "1", "%s/red.png" % T])
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=1280x720:d=1",
    "-frames:v", "1", "%s/green.png" % T])
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1280x720:d=1",
    "-frames:v", "1", "%s/blue.png" % T])
sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
    "-c:a", "libmp3lame", "%s/narration.mp3" % T])

p = Project("m5-render-proof")
p.width, p.height, p.fps = 1280, 720, 30
p.tracks["narration"].media_path = "%s/narration.mp3" % T
p.add_slot(Slot(0, 4, "%s/red.png" % T, "photo", "none", "fade-in",
                label="s1"))
p.add_slot(Slot(4, 8, "%s/green.png" % T, "photo", "none", "dip-to-black",
                label="s2"))
p.add_slot(Slot(8, 12, "%s/blue.png" % T, "photo", "none", "fade-out",
                label="s3"))

out = os.path.join(OUT, "m5_proof.mp4")
print("rendering proof video...")
rw.render_project(p.to_dict(), T, out, progress_cb=lambda *a: None)


def probe_dur(path):
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return float(pr.stdout.strip())


def frame_mean(path, t, w=1280, h=720):
    """Mean byte value of one accurately-seeked RGB frame."""
    pr = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-ss", "%.2f" % t,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw = pr.stdout
    assert len(raw) == w * h * 3, "short frame at t=%.2f (%d bytes)" % (t, len(raw))
    s = raw[::97]  # sample: fast enough, still ~28k samples
    return sum(s) / len(s)


dur = probe_dur(out)
check("proof duration ~12s", abs(dur - 12) < 1.0, dur)

m_full = frame_mean(out, 2.0)
check("sanity: mid-slot frame is bright red", m_full > 60, m_full)

m_early = frame_mean(out, 0.15)
check("fade-in start is dark", m_early < 30, m_early)
m_late = frame_mean(out, 0.95)
check("fade-in progresses dark -> bright",
      m_late > 50 and m_late > m_early * 2, (m_early, m_late))

m_dip = frame_mean(out, 4.0)
check("dip-to-black midpoint is near-black (mean<15)", m_dip < 15, m_dip)
# ^ If the renderer had substituted xfade, this frame would be a red/green
# blend with mean ~55-85. Near-black PROVES dip-to-black rendered.

m_out = frame_mean(out, 7.9)
check("fade-out tail is near-black", m_out < 30, m_out)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
