"""Phase 1 proof test (no network, no API key needed).

1. Generates synthetic media with ffmpeg.
2. Builds a ~20s project: photo motion + clip segment + xfade + logo + ducked BGM + SFX.
3. Renders via render_worker.render_project.
4. Verifies with ffprobe: duration ~20s, 1920x1080, 30fps, audio stream present.
5. Re-runs to prove restart-safe resume (chunks skipped).
6. Unit-checks: split_script chunking, key-pool obfuscation, clip-limit warnings.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import Project, Slot, SfxEvent
from auto_editor.render_worker import render_project
from auto_editor import voiceover_studio as vs
from auto_editor import settings_store

FF = "ffmpeg"
FP = "ffprobe"
T = os.path.join(HERE, "test_media")
OUT = os.path.join(HERE, "out")
os.makedirs(T, exist_ok=True)
os.makedirs(OUT, exist_ok=True)


def sh(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    assert p.returncode == 0, "CMD failed: %s\n%s" % (" ".join(cmd), p.stderr[-800:])


def gen_media():
    print("generating synthetic media...")
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=0x1a2b4a:s=1920x1080:d=1",
        "-frames:v", "1", f"{T}/photo1.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=0x4a1a2b:s=1920x1080:d=1",
        "-frames:v", "1", f"{T}/photo2.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", f"{T}/clip1.mp4"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:a", "libmp3lame", f"{T}/narration.mp3"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=20",
        "-c:a", "libmp3lame", f"{T}/bgm.mp3"])
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=white:s=400x200:d=1",
        "-frames:v", "1", f"{T}/logo.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-c:a", "libmp3lame", f"{T}/sfx.mp3"])
    print("media OK")


def build_project():
    p = Project("phase1-test")
    p.tracks["narration"].media_path = f"{T}/narration.mp3"
    p.tracks["bgm"].media_path = f"{T}/bgm.mp3"
    p.tracks["logo"].media_path = f"{T}/logo.png"
    p.add_slot(Slot(0, 5, f"{T}/photo1.png", "photo", "zoom-in", "xfade", label="s1"))
    s2 = Slot(5, 10, f"{T}/clip1.mp4", "clip", "none", "xfade", label="s2")
    s2.clip_in, s2.clip_out = 1.0, 6.0
    p.add_slot(s2)
    p.add_slot(Slot(10, 15, f"{T}/photo2.png", "photo", "pan-left", "xfade", label="s3"))
    p.add_slot(Slot(15, 20, f"{T}/photo1.png", "photo", "zoom-out", "cut", label="s4"))
    p.tracks["sfx"].sfx_events.append(SfxEvent(7.0, f"{T}/sfx.mp3", 0.7))
    return p


def probe(path):
    p = subprocess.run(
        [FP, "-v", "error", "-show_entries",
         "stream=width,height,r_frame_rate,codec_type:format=duration",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60)
    return json.loads(p.stdout)


def verify(path, label):
    info = probe(path)
    dur = float(info["format"]["duration"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    fps = eval(v["r_frame_rate"])
    print("%s: duration=%.2fs %dx%d %.1ffps audio=%s"
          % (label, dur, v["width"], v["height"], fps, bool(a)))
    assert abs(dur - 20) < 1.5, "duration off: %s" % dur
    assert (v["width"], v["height"]) == (1920, 1080), "resolution off"
    assert abs(fps - 30) < 0.5, "fps off"
    assert a, "no audio stream"
    print("verify OK")


def unit_checks():
    # split_script: paragraph-wise ~300-500 chars
    chunks = vs.split_script("Para one. " * 60 + "\n\n" + "Para two. " * 60)
    assert chunks and all(len(c) <= 900 for c in chunks), "chunking bad"
    print("split_script OK (%d chunks)" % len(chunks))
    # key pool: obfuscated at rest, round-trips, never plaintext
    settings_store.set_api_key_slots(["k1", "", "k3", "", "", "", ""])
    raw = open(settings_store._config_path()).read()
    assert "k1" not in raw and "k3" not in raw, "key stored in plaintext!"
    assert settings_store.get_api_key_slots()[0] == "k1"
    assert settings_store.get_api_key_slots()[2] == "k3"
    assert settings_store.has_api_key()
    settings_store.set_api_key_slots([""] * 7)  # clean up
    print("key-pool obfuscation OK")
    # clip limits
    p = Project("limits")
    for i in range(31):
        p.add_slot(Slot(i * 20, i * 20 + 20, "x.mp4", "clip"))
    w = p.check_clip_limits()
    assert len(w) == 2, w
    print("clip-limit warnings OK")


def main():
    gen_media()
    unit_checks()
    proj = build_project()
    assert not proj.check_clip_limits()
    out = os.path.join(OUT, "test_render.mp4")
    work = os.path.join(OUT, ".render_cache")

    print("render pass 1...")
    render_project(proj.to_dict(), T, out,
                   progress_cb=lambda ph, d, t: print("  %s %d/%d" % (ph, d, t)))
    verify(out, "pass1")

    print("render pass 2 (resume — chunks should skip)...")
    import time
    t0 = time.time()
    render_project(proj.to_dict(), T, out, progress_cb=lambda *a: None)
    dt = time.time() - t0
    print("pass2 took %.1fs" % dt)
    assert dt < 60, "resume did not skip chunks!"
    verify(out, "pass2")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
