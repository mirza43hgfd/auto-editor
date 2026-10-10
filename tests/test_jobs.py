"""M7 job persistence / pause / resume / recovery tests.

ALL MOCKED — no network, no real API keys, no Qt.

Covers: manifest save/reopen round-trip; photo-loop checkpoint -> simulated
crash -> resume skips completed units; AllKeysExhausted -> paused (not
failed); permanent error -> failed with message; classify_error table;
stale running job detected on scan; resume_job ordering; voiceover chunk
resume with a stubbed TTS synthesizer (real WAVs via wave module, real
ffmpeg concat); pause-attribute AIError -> paused.

Run:  .venv/bin/python tests/test_jobs.py
Exit 0 = all pass; non-zero = real failure.
"""

import json
import os
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor import jobs
from auto_editor.jobs import (JobStore, JobPaused, classify_error,
                              record_stage_error, raise_if_paused,
                              scan_incomplete, STALE_AFTER)
from auto_editor.ai_helper import AIError
from auto_editor import voiceover_studio as vs

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        print("FAIL  %s  %s" % (name, detail))


def fresh_dir():
    return tempfile.mkdtemp(prefix="ae-job-")


# ---------------------------------------------------------------- 1. round-trip
d = fresh_dir()
s = JobStore(d)
s.start_stage("photos")
s.set_total("photos", 3)
s.mark_unit_done("photos", "slot_001", {"path": "/x/a.png", "prompt": "p1"})
s.mark_unit_done("photos", "slot_002", {"path": "/x/b.png", "prompt": "p2"})
s.checkpoint("voiceover", {"mp3": "/x/vo.mp3", "chunks": [{"text": "hi"}]})
s.complete_stage("voiceover")

s2 = JobStore(d)  # reopen = simulated restart
check("roundtrip: photos running kept", s2.stage_status("photos") == "running")
check("roundtrip: voiceover done", s2.is_stage_done("voiceover"))
check("roundtrip: done_units kept",
      s2.is_unit_done("photos", "slot_001")
      and s2.is_unit_done("photos", "slot_002")
      and not s2.is_unit_done("photos", "slot_003"))
check("roundtrip: unit extra kept",
      s2.unit_extra("photos", "slot_001") == {"path": "/x/a.png", "prompt": "p1"})
check("roundtrip: checkpoint kept",
      s2.checkpoint_of("voiceover")["mp3"] == "/x/vo.mp3")
summ = s2.job_summary()
check("roundtrip: summary honest",
      summ["photos"] == {"status": "running", "done": 2, "total": 3,
                         "error": None},
      str(summ["photos"]))

# ------------------------------------------------- 2. crash -> resume skips done
d = fresh_dir()
gen_calls = []


def fake_generate(i):
    gen_calls.append(i)
    return "img%d.png" % i


def photo_loop(store):
    """Mirrors pipeline_panel._stage_photos unit logic."""
    store.start_stage("photos")
    store.set_total("photos", 3)
    for i in range(3):
        unit = "slot_%03d" % (i + 1)
        if store.is_unit_done("photos", unit):
            continue
        fake_generate(i)
        store.mark_unit_done("photos", unit, {"path": "img%d.png" % i})


st = JobStore(d)
st.start_stage("photos")
st.set_total("photos", 3)
for i in range(2):  # two units, then "crash" (no complete_stage)
    unit = "slot_%03d" % (i + 1)
    fake_generate(i)
    st.mark_unit_done("photos", unit, {"path": "img%d.png" % i})
del st  # process dies here

st2 = JobStore(d)  # restart
photo_loop(st2)
check("resume: generate called only for remaining",
      gen_calls == [0, 1, 2], str(gen_calls))
check("resume: manifest complete after resume",
      st2.job_summary()["photos"]["done"] == 3)

# ------------------------------------------------- 3. AllKeysExhausted -> paused
d = fresh_dir()
st = JobStore(d)
st.start_stage("photos")
exhausted = AIError("Saari keys is waqt available nahi hain "
                    "(rate limit / quota). Thori der baad khud retry hoga.")
res = record_stage_error(st, "photos", exhausted)
check("exhausted: paused not failed",
      res == "paused" and st.job_status == "paused"
      and st.stage_status("photos") == "paused",
      "%s %s" % (res, st.job_status))
check("exhausted: pause reason kept", "Limit lag gayi" in st.pause_reason,
      st.pause_reason)

# ------------------------------------------------- 4. permanent error -> failed
d = fresh_dir()
st = JobStore(d)
st.start_stage("photos")
perm = AIError("HTTP_401 Unauthorized: API key not valid.")
res = record_stage_error(st, "photos", perm)
err = st.stage_error("photos")
check("permanent: failed", res == "failed" and st.job_status == "failed"
      and st.stage_status("photos") == "failed")
check("permanent: category+message",
      err and err["category"] == "invalid_key"
      and not err["retryable"] and "API key galat hai" in err["message"]
      and err["what_to_do"], str(err))

# ------------------------------------------------- 5. classify_error table
cases = [
    (AIError("HTTP_400 bad things"), "bad_request", False),
    (AIError("HTTP_401 nope"), "invalid_key", False),
    (AIError("HTTP_403 forbidden"), "invalid_key", False),
    (AIError("HTTP_429 slow down"), "rate_limit", True),
    (AIError("HTTP_500 boom"), "transient", True),
    (AIError("HTTP_0 network: timed out"), "transient", True),
    (AIError("network: connection reset"), "transient", True),
    (AIError("Koi API key nahi mili. Settings me dalo."), "no_keys", True),
    (AIError("API key galat hai — Google ne reject kar di (401/403). x"),
     "invalid_key", False),
    (ValueError("weird"), "unknown", True),
    (JobPaused("User ne pause kiya.", "Resume dabao"), "paused", True),
]
ok_all = True
for err, cat, retr in cases:
    info = classify_error(err)
    good = (info["category"] == cat and info["retryable"] == retr
            and info["message"] and info["what_to_do"])
    if not good:
        ok_all = False
        print("   case FAIL: %r -> %s" % (str(err)[:40], info))
check("classify_error table", ok_all)

# paused-attribute path (ai_helper._post_pool attaches these)
attr_err = AIError("Saari keys is waqt available nahi hain.")
attr_err.paused = True
attr_err.resume_hint = "5 min me retry"
info = classify_error(attr_err)
check("classify: paused attribute",
      info["category"] == "rate_limit" and info["retryable"]
      and info["what_to_do"] == "5 min me retry", str(info))

# ------------------------------------------------- 6. stale scan
d = fresh_dir()
st = JobStore(d)
st.start_stage("photos")
# backdate the manifest -> looks crashed
with open(st.path, "r", encoding="utf-8") as f:
    raw = json.load(f)
raw["updated_at"] = time.time() - STALE_AFTER - 100
raw["stages"]["photos"]["updated_at"] = time.time() - STALE_AFTER - 100
with open(st.path, "w", encoding="utf-8") as f:
    json.dump(raw, f)

d2 = fresh_dir()
st2 = JobStore(d2)
st2.pause_job("quota", "wait")

found = scan_incomplete([d, d2, "/nonexistent-dir"])
by_dir = {f["work_dir"]: f for f in found}
check("scan: finds both", d in by_dir and d2 in by_dir,
      str([f["work_dir"] for f in found]))
check("scan: stale running flagged",
      by_dir[d]["stale"] is True and by_dir[d]["job_status"] == "running")
check("scan: paused not stale",
      by_dir[d2]["stale"] is False and by_dir[d2]["job_status"] == "paused")
check("scan: missing dir skipped", len(found) == 2, str(len(found)))

# empty dir -> nothing found
check("scan: no manifest -> empty", scan_incomplete([fresh_dir()]) == [])

# ------------------------------------------------- 7. resume_job ordering
d = fresh_dir()
st = JobStore(d)
st.start_stage("voiceover")
st.complete_stage("voiceover")
st.start_stage("photos")
st.mark_unit_done("photos", "slot_001")
st.pause_job("quota lagi", "kal try karo")
got = st.resume_job()
check("resume_job: next pending unit",
      got is not None and got[0] == "photos"
      and got[1].get("done_units") == ["slot_001"]
      and st.job_status == "running", str(got))
st.complete_stage("photos")
for name in ("slots", "reference", "assemble", "render"):
    st.start_stage(name)
    st.complete_stage(name)
check("resume_job: nothing left -> None", st.resume_job() is None)
check("all done -> job done", st.job_status == "done", st.job_status)

# ------------------------------------------------- 8. raise_if_paused
d = fresh_dir()
st = JobStore(d)
st.pause_job("quota", "wait karo")
try:
    raise_if_paused(st)
    check("raise_if_paused: raises", False)
except JobPaused as e:
    check("raise_if_paused: raises JobPaused",
          "PAUSED" in str(e) and e.user_message == "quota")
st2 = JobStore(fresh_dir())
check("raise_if_paused: quiet when idle", raise_if_paused(st2) is False)
check("raise_if_paused: None ok", raise_if_paused(None) is False)

# ------------------------------------------------- 9. voiceover resume (stubbed TTS)
d = fresh_dir()
calls = []
real_synth = vs.synthesize_chunk


def fake_synth(text, tone_name, out_wav, api_key=None):
    calls.append(text)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 12000)  # 0.5 s of silence
    return True, out_wav


vs.synthesize_chunk = fake_synth
try:
    script = ("Pehla paragraph. " * 30 + "\n\n"
              + "Doosra paragraph. " * 30 + "\n\n"
              + "Teesra paragraph. " * 30)
    n_chunks = len(vs.split_script(script))
    assert n_chunks >= 3, "need >=3 chunks, got %d" % n_chunks

    st = JobStore(d)
    ok, res = vs.generate_voiceover(script, "Deep Dramatic", d, job=st)
    assert ok, res
    mp3, chunks = res
    first_calls = len(calls)
    assert first_calls == n_chunks, (first_calls, n_chunks)
    assert os.path.isfile(mp3), "mp3 missing"
    check("voiceover: full run ok", True)

    # simulated crash mid-way is covered by manifest; now "restart"
    calls.clear()
    st2 = JobStore(d)
    ok2, res2 = vs.generate_voiceover(script, "Deep Dramatic", d, job=st2)
    check("voiceover resume: ok", ok2, str(res2)[:200] if not ok2 else "")
    check("voiceover resume: zero TTS calls on 2nd run", calls == [], str(calls))
    mp3b, chunks2 = res2
    check("voiceover resume: same timings",
          [c["start"] for c in chunks2] == [c["start"] for c in chunks])

    # pause_event mid-run
    import threading
    d3 = fresh_dir()
    st3 = JobStore(d3)
    calls.clear()
    ev = threading.Event()
    orig = vs.synthesize_chunk

    def pausing_synth(text, tone_name, out_wav, api_key=None):
        r = orig(text, tone_name, out_wav, api_key)
        ev.set()  # pause after the first chunk completes
        return r

    vs.synthesize_chunk = pausing_synth
    try:
        vs.generate_voiceover(script, "Deep Dramatic", d3, job=st3,
                              pause_event=ev)
        check("voiceover pause: raises", False)
    except JobPaused:
        check("voiceover pause: JobPaused raised", True)
    check("voiceover pause: partial checkpoint kept",
          len(st3.checkpoint_of("voiceover").get("done_units", [])) >= 1)
finally:
    vs.synthesize_chunk = real_synth

# ------------------------------------------------- 10. run_script_pipeline stages (stubbed net)
from auto_editor import auto_pipeline as ap

d = fresh_dir()
real_vo = vs.generate_voiceover
real_svp = ap.ai_helper.suggest_visual_prompt
real_haskey = ap.settings_store.has_api_key
ap.settings_store.has_api_key = lambda: True


def fake_vo_full(script_text, tone_name, work_dir, progress_cb=None,
                 api_key=None, ffmpeg="ffmpeg", job=None, pause_event=None):
    chunks = [{"text": "chunk %d" % i, "start": float(i), "end": float(i + 1)}
              for i in range(3)]
    mp3 = os.path.join(work_dir, "voiceover_final.mp3")
    with open(mp3, "wb") as f:
        f.write(b"fake-mp3")
    if job is not None:
        job.start_stage("voiceover")
        job.set_total("voiceover", 3)
        for i in range(3):
            if pause_event is not None and pause_event.is_set():
                raise JobPaused("User ne pause kiya.", "Resume dabao.")
            job.mark_unit_done("voiceover", "chunk_%03d" % i)
        job.checkpoint("voiceover", {"mp3": mp3, "chunks": chunks})
        job.complete_stage("voiceover")
    return True, (mp3, chunks)


vs.generate_voiceover = fake_vo_full
ap.ai_helper.suggest_visual_prompt = lambda label, api_key=None: (True, "a still")
try:
    import threading
    # user pause before anything starts -> JobPaused, nothing recorded
    ev = threading.Event()
    ev.set()
    try:
        ap.run_script_pipeline("script", "brief", "Deep Dramatic", d,
                               skip_images=True, pause_event=ev)
        check("pipeline: pre-start pause raises", False)
    except JobPaused:
        check("pipeline: pre-start pause raises", True)
    st = JobStore(d)
    check("pipeline: pre-start pause recorded as paused",
          st.job_status == "paused" and "User ne pause kiya" in st.pause_reason,
          st.job_status)

    # full run, no network (skip_images)
    ok, proj = ap.run_script_pipeline("script", "brief", "Deep Dramatic", d,
                                      skip_images=True)
    check("pipeline: full run ok", ok, str(proj)[:200] if not ok else "")
    check("pipeline: 3 slots built", ok and len(proj.slots) == 3,
          str(len(proj.slots)) if ok else "")
    st = JobStore(d)
    summ = st.job_summary()
    check("pipeline: stages done",
          all(summ[s]["status"] == "done"
              for s in ("voiceover", "slots", "photos", "assemble")),
          str({k: v["status"] for k, v in summ.items()}))
    check("pipeline: job done", st.job_status == "done", st.job_status)

    # idempotent re-run (resume path, no API)
    ok2, proj2 = ap.run_script_pipeline("script", "brief", "Deep Dramatic", d,
                                        skip_images=True)
    check("pipeline: re-run ok", ok2 and len(proj2.slots) == 3)
finally:
    vs.generate_voiceover = real_vo
    ap.ai_helper.suggest_visual_prompt = real_svp
    ap.settings_store.has_api_key = real_haskey

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
