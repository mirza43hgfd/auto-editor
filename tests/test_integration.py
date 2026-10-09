"""WS4 cross-workstream integration tests (no network, no real API keys).

Covers:
  (a) contract-dict -> render_project end-to-end (~10s, ffprobe verified)
  (b) timeline contract round-trip incl. grade_preset + settings
  (c) media.py import / watch-folder scan / prompt export
  (d) settings_store key-pool round-trip + status/cooldown (dummy keys only)
  (e) render resume: delete one chunk file, re-run, assert ONLY it re-renders
  (f) app --smoke headless (Qt-dependent; env-skip if Qt system libs missing)
  (g) ai_helper pool path with zero keys -> clean AIError, no network touched

Run:  .venv/bin/python tests/test_integration.py
Exit 0 = all pass (or env-skipped with reason); non-zero = real failure.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import Project, Slot, SfxEvent
from auto_editor import render_worker
from auto_editor import media as media_mod
from auto_editor import settings_store

FF = "ffmpeg"
FP = "ffprobe"
T = os.path.join(HERE, "integ_media")
OUT = os.path.join(HERE, "out_integration")
os.makedirs(T, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

RESULTS = []


def check(name, fn):
    t0 = time.time()
    try:
        note = fn() or ""
        RESULTS.append((name, "PASS", note, time.time() - t0))
        print("PASS  %-28s %s (%.1fs)" % (name, note, time.time() - t0))
    except Exception as e:
        RESULTS.append((name, "FAIL", "%s: %s" % (type(e).__name__, e), time.time() - t0))
        print("FAIL  %-28s %s: %s" % (name, type(e).__name__, str(e)[:300]))


def sh(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    assert p.returncode == 0, "CMD failed: %s\n%s" % (" ".join(cmd), p.stderr[-600:])


def gen_media():
    if os.path.isfile(os.path.join(T, "narration.mp3")):
        return
    print("generating integration media...")
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=0x1a2b4a:s=1920x1080:d=1",
        "-frames:v", "1", T + "/photo1.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=0x4a2b1a:s=1920x1080:d=1",
        "-frames:v", "1", T + "/photo2.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", T + "/clip1.mp4"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-c:a", "libmp3lame", T + "/narration.mp3"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=10",
        "-c:a", "libmp3lame", T + "/bgm.mp3"])
    sh([FF, "-y", "-f", "lavfi", "-i", "color=c=white:s=400x200:d=1",
        "-frames:v", "1", T + "/logo.png"])
    sh([FF, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-c:a", "libmp3lame", T + "/sfx.mp3"])
    print("media OK")


def probe(path):
    p = subprocess.run(
        [FP, "-v", "error", "-show_entries",
         "stream=width,height,r_frame_rate,codec_type:format=duration",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60)
    return json.loads(p.stdout)


def build_10s_project():
    p = Project("integ-10s")
    p.grade_preset = "teal-orange"
    p.tracks["narration"].media_path = T + "/narration.mp3"
    p.tracks["bgm"].media_path = T + "/bgm.mp3"
    p.tracks["logo"].media_path = T + "/logo.png"
    p.add_slot(Slot(0, 4, T + "/photo1.png", "photo", "zoom-in", "xfade",
                    prompt="desert caravan at dawn, cinematic", label="s1"))
    s2 = Slot(4, 7, T + "/clip1.mp4", "clip", "none", "xfade", label="s2")
    s2.clip_in, s2.clip_out = 1.0, 4.0
    p.add_slot(s2)
    p.add_slot(Slot(7, 10, T + "/photo2.png", "photo", "pan-right", "cut",
                    prompt="fortress silhouette at dusk", label="s3"))
    p.tracks["sfx"].sfx_events.append(SfxEvent(5.0, T + "/sfx.mp3", 0.7))
    return p


# ---------------------------------------------------------------- (a)
def t_contract_render_e2e():
    proj = build_10s_project()
    contract = proj.to_contract_dict()  # flat interface-contract format
    assert contract["version"] == 1 and len(contract["slots"]) == 3
    out = os.path.join(OUT, "contract_e2e.mp4")
    work = os.path.join(OUT, ".cache_a")
    render_worker.render_project(contract, T, out, work_dir=work,
                                 progress_cb=lambda *a: None)
    info = probe(out)
    dur = float(info["format"]["duration"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    fps = eval(v["r_frame_rate"])
    assert abs(dur - 10) < 1.5, "duration %.2f" % dur
    assert (v["width"], v["height"]) == (1920, 1080)
    assert abs(fps - 30) < 0.5, "fps %.2f" % fps
    assert a, "no audio"
    return "dur=%.1fs 1920x1080 30fps audio=ok" % dur


# ---------------------------------------------------------------- (b)
def t_contract_roundtrip():
    p = Project("rt")
    p.grade_preset = "war-desat"
    p.settings["max_clips"] = 12
    p.settings["watch_folder"] = "/tmp/watch"
    p.settings["hook_clip_seconds"] = 5
    p.tracks["narration"].media_path = "/media/narr.mp3"
    p.tracks["bgm"].media_path = "/media/bgm.mp3"
    p.tracks["logo"].media_path = "/media/logo.png"
    s = Slot(2.5, 8.0, "/media/v.mp4", "clip", "pan-left", "fadeblack",
             prompt="P1", label="hello world")
    s.clip_in, s.clip_out = 3.0, 8.5
    sid = s.id
    p.add_slot(s)
    p.tracks["sfx"].sfx_events.append(SfxEvent(4.25, "/media/s.mp3", 0.55))

    d = p.to_contract_dict(project_dir="/proj")
    assert d["narration_mp3"] == os.path.join("..", "media", "narr.mp3"), d["narration_mp3"]
    assert d["grade_preset"] == "war-desat"
    assert d["settings"]["max_clips"] == 12
    sl = d["slots"][0]
    assert sl["id"] == sid and sl["text"] == "hello world" and sl["prompt"] == "P1"
    assert sl["clip_in"] == 3.0 and sl["clip_out"] == 8.5
    assert sl["motion"] == "pan-left" and sl["transition"] == "fadeblack"

    p2 = Project.from_contract_dict(d, project_dir="/proj")
    assert p2.grade_preset == "war-desat"
    assert p2.settings["max_clips"] == 12
    assert p2.settings["watch_folder"] == "/tmp/watch"
    assert p2.settings["hook_clip_seconds"] == 5
    assert p2.narration_path == "/media/narr.mp3"
    assert p2.bgm_path == "/media/bgm.mp3"
    assert p2.logo_path == "/media/logo.png"
    s2 = p2.slots[0]
    assert s2.id == sid and abs(s2.start_sec - 2.5) < 1e-6
    assert s2.visual_path == "/media/v.mp4" and s2.visual_type == "clip"
    assert s2.prompt == "P1" and s2.label == "hello world"
    assert s2.clip_in == 3.0 and s2.clip_out == 8.5
    e2 = p2.tracks["sfx"].sfx_events[0]
    assert e2.path == "/media/s.mp3" and abs(e2.time_sec - 4.25) < 1e-6
    assert abs(e2.volume - 0.55) < 1e-6
    # native .deproj round-trip keeps grade too
    p3 = Project.from_dict(p.to_dict())
    assert p3.grade_preset == "war-desat" and p3.settings["max_clips"] == 12
    return "grade+settings+slots+sfx all round-tripped"


# ---------------------------------------------------------------- (c)
def t_media_helpers():
    assert media_mod.detect_type("x.JPG") == "photo"
    assert media_mod.detect_type("x.MP4") == "clip"
    assert media_mod.detect_type("x.mp3") == "audio"
    assert media_mod.detect_type("x.xyz") == "unknown"
    got = media_mod.import_files([T + "/photo1.png", T + "/clip1.mp4",
                                  T + "/nope.mp4", T + "/photo1.png".replace("png", "xyz")])
    assert len(got) == 2 and got[0][1] == "photo" and got[1][1] == "clip", got
    with tempfile.TemporaryDirectory() as wd:
        open(os.path.join(wd, "new1.png"), "wb").write(b"\x89PNG")
        open(os.path.join(wd, "old.mp4"), "wb").write(b"\x00")
        open(os.path.join(wd, "note.txt"), "w").write("x")
        known = [os.path.join(wd, "old.mp4")]
        new = media_mod.scan_watch_folder(wd, known)
        assert new == [os.path.abspath(os.path.join(wd, "new1.png"))], new
        assert media_mod.scan_watch_folder("/no/such/dir", []) == []
    p = build_10s_project()
    md = os.path.join(OUT, "prompts.md")
    media_mod.export_slot_prompts(p, md)
    txt = open(md, encoding="utf-8").read()
    assert "desert caravan at dawn" in txt and "fortress silhouette" in txt
    assert "Slot 1" in txt and "0.0s to 4.0s" in txt
    return "detect/import/scan/export ok"


# ---------------------------------------------------------------- (d)
def t_keypool():
    with tempfile.TemporaryDirectory() as cfg:
        real = settings_store._config_dir
        settings_store._config_dir = lambda: cfg
        try:
            settings_store.set_api_key_slots(["DUMMY-KEY-AAA", "", "DUMMY-KEY-CCC"])
            raw = open(settings_store._config_path(), encoding="utf-8").read()
            assert "DUMMY-KEY" not in raw, "key leaked in plaintext!"
            slots = settings_store.get_api_key_slots()
            assert slots[0] == "DUMMY-KEY-AAA" and slots[1] == "" and slots[2] == "DUMMY-KEY-CCC"
            assert len(slots) == settings_store.MAX_KEYS == 7
            assert settings_store.has_api_key()
            assert settings_store.usable_key_indices() == [0, 2]
            # cooldown logic
            settings_store.set_key_status(2, settings_store.STATUS_LIMITED, 600)
            assert settings_store.usable_key_indices() == [0]
            st = settings_store.get_key_status(2)
            assert st["status"] == "limited" and st["cooldown_until"] > time.time()
            settings_store.set_key_status(0, settings_store.STATUS_FAILED)
            assert settings_store.usable_key_indices() == []
            # legacy single-key path -> slot 0
            settings_store.set_api_key_slots([""] * 7)
            settings_store.set_api_key("DUMMY-KEY-Z")
            assert settings_store.get_api_key() == "DUMMY-KEY-Z"
            assert settings_store.get_api_key_slots()[0] == "DUMMY-KEY-Z"
            settings_store.set_api_key_slots([""] * 7)
            assert not settings_store.has_api_key()
        finally:
            settings_store._config_dir = real
    return "obfuscation + failover/cooldown logic ok (dummy keys only)"


# ---------------------------------------------------------------- (e)
def t_render_resume_selective():
    old_target = render_worker.CHUNK_TARGET
    render_worker.CHUNK_TARGET = 4.0  # 10s project -> 3 chunks
    try:
        proj = build_10s_project()
        out = os.path.join(OUT, "resume.mp4")
        work = os.path.join(OUT, ".cache_e")
        chunk_dir = os.path.join(work, "chunks")
        render_worker.render_project(proj.to_dict(), T, out, work_dir=work,
                                     progress_cb=lambda *a: None)
        cfiles = [os.path.join(chunk_dir, "chunk_%03d.mp4" % i) for i in range(3)]
        assert all(os.path.isfile(c) for c in cfiles), os.listdir(chunk_dir)
        m0 = {c: os.path.getmtime(c) for c in cfiles}
        time.sleep(1.1)  # mtime granularity
        os.remove(cfiles[1])  # simulate a lost/interrupted chunk
        render_worker.render_project(proj.to_dict(), T, out, work_dir=work,
                                     progress_cb=lambda *a: None)
        assert os.path.isfile(cfiles[1]), "chunk 1 not re-rendered"
        assert os.path.getmtime(cfiles[0]) == m0[cfiles[0]], "chunk 0 re-rendered (should skip)"
        assert os.path.getmtime(cfiles[2]) == m0[cfiles[2]], "chunk 2 re-rendered (should skip)"
        assert os.path.getmtime(cfiles[1]) > m0[cfiles[1]], "chunk 1 mtime not newer"
        man = json.load(open(os.path.join(work, "manifest.json"), encoding="utf-8"))
        assert man["chunks"]["1"]["done"] is True
        dur = float(probe(out)["format"]["duration"])
        assert abs(dur - 10) < 1.5, "duration %.2f after resume" % dur
    finally:
        render_worker.CHUNK_TARGET = old_target
    return "only the deleted chunk re-rendered; output still 10s"


# ---------------------------------------------------------------- (f)
def t_app_smoke():
    # 1) every non-Qt module must import cleanly (catches real regressions here)
    import importlib
    for m in ("auto_editor.timeline", "auto_editor.render_worker",
              "auto_editor.settings_store", "auto_editor.media",
              "auto_editor.ai_helper", "auto_editor.auto_pipeline",
              "auto_editor.voiceover_studio"):
        importlib.import_module(m)
    # 2) the real GUI smoke needs Qt platform libs
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, "-m", "auto_editor.app", "--smoke"],
                       cwd=ROOT, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, timeout=120)
    if p.returncode == 0 and "SMOKE OK" in p.stdout:
        return "python -m auto_editor.app --smoke -> SMOKE OK"
    err = (p.stderr or "") + (p.stdout or "")
    qt_env_markers = ("libEGL", "libGL", "libxkbcommon", "undefined symbol",
                      "could not load the Qt platform plugin", "xcb")
    if any(mk in err for mk in qt_env_markers):
        return ("ENV-SKIP: Qt system libs missing on this Linux VM "
                "(%s). Non-Qt imports verified; real smoke runs on the "
                "Windows CI runner." % err.strip().splitlines()[-1][:100])
    raise AssertionError("smoke failed for a REAL reason:\n%s" % err[-800:])


# ---------------------------------------------------------------- (g)
def t_ai_pool_no_key():
    from auto_editor import ai_helper
    with tempfile.TemporaryDirectory() as cfg:
        real = settings_store._config_dir
        settings_store._config_dir = lambda: cfg
        try:
            try:
                ai_helper._generate_text("ping", timeout=5)
                raise AssertionError("expected AIError with no keys")
            except ai_helper.AIError as e:
                msg = str(e)
                assert "key" in msg.lower(), msg
                assert "sk-" not in msg and "AIza" not in msg
        finally:
            settings_store._config_dir = real
    return "no-key pool path raises clean AIError (no network, no quota)"


# ---------------------------------------------------------------- (h)
def t_ai_pool_failover():
    """Real _post_pool code path: key0 429s -> marked limited+cooldown, key1 wins.
    _post_single is stubbed so zero network/quota is touched."""
    from auto_editor import ai_helper
    with tempfile.TemporaryDirectory() as cfg:
        real_cfg = settings_store._config_dir
        real_post = ai_helper._post_single
        settings_store._config_dir = lambda: cfg
        calls = []

        def fake_post(model, payload, api_key, timeout=90):
            calls.append(api_key)
            if api_key == "DUMMY-K0":
                raise ai_helper.AIError("HTTP_429 quota exceeded for the day")
            return ({"candidates": [{"content": "ok"}]}, 200)

        ai_helper._post_single = fake_post
        try:
            settings_store.set_api_key_slots(["DUMMY-K0", "DUMMY-K1"])
            resp = ai_helper._post_pool("gemini-3.5-flash", {"x": 1}, timeout=5)
            assert resp["candidates"][0]["content"] == "ok"  # _post_pool returns the response dict
            assert calls == ["DUMMY-K0", "DUMMY-K1"], calls
            st0 = settings_store.get_key_status(0)
            assert st0["status"] == settings_store.STATUS_LIMITED, st0
            assert st0["cooldown_until"] > time.time()
            assert settings_store.get_key_status(1)["status"] == settings_store.STATUS_OK
            assert settings_store.usable_key_indices() == [1]
        finally:
            ai_helper._post_single = real_post
            settings_store._config_dir = real_cfg
    return "429 -> failover to key1, key0 cooling down (no network)"


def main():
    gen_media()
    check("(a) contract->render e2e", t_contract_render_e2e)
    check("(b) contract round-trip", t_contract_roundtrip)
    check("(c) media helpers", t_media_helpers)
    check("(d) key-pool logic", t_keypool)
    check("(e) selective resume", t_render_resume_selective)
    check("(f) app --smoke", t_app_smoke)
    check("(g) ai pool no-key", t_ai_pool_no_key)
    check("(h) ai pool failover", t_ai_pool_failover)
    print("\n==== SUMMARY ====")
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    for name, status, note, dt in RESULTS:
        print("%-4s %-28s %s" % (status, name, note))
    if fails:
        print("%d FAILED" % len(fails))
        sys.exit(1)
    print("ALL INTEGRATION TESTS PASSED (or env-skipped with reason)")


if __name__ == "__main__":
    main()
