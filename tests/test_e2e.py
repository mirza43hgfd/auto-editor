"""End-to-end test: Urdu script -> real TTS voiceover -> slots -> assemble -> render.

Covers the full pipeline EXCEPT AI photo generation (image quota is 429;
see BUG_LOG). Photos are synthetic stand-ins; the AI-photo code path shares
the validated inlineData pattern. When image quota recovers, re-run with
AI photos by swapping _synth_photo() for ai_helper.generate_image().

TEST-ONLY key bridge: the user's vault key is used via the surrogate URL
(dynamic_credentials), exactly like the gemini-ask CLI. The key is never
written to disk or printed. settings_store pool logic is unit-tested
offline in tests/test_ai.py.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import url_with_surrogate_query_param, read_json_response

from auto_editor.timeline import Project, Slot
from auto_editor.render_worker import render_project
from auto_editor import voiceover_studio as vs

HOSTS = ["generativelanguage.googleapis.com"]
TTS_MODEL = "gemini-2.5-flash-preview-tts"
OUT = os.path.join(HERE, "out_e2e")
os.makedirs(OUT, exist_ok=True)

# ~60s Urdu script (6 chunks x ~10s)
SCRIPT = """Hazrat Khalid bin Waleed, jinhain Saifullah ka laqab mila, Islam ke
azeem tareen sipah salar thay. Unhon ne apni zindagi me jo laraaiyan larin,
un me kabhi shikast nahi khai.

Yarmouk ki laraai me unhon ne apni danishmandi se dushman ke lashkar ko
shikast di. Ye laraai tareekh ka rukh morrne wali sabit hui.

Mutah ki laraai me jab teenon sipah salar shaheed ho gaye, to Khalid ne
alam sanbhala aur lashkar ko mehfooz wapas le aaye. Isi din unhain
Saifullah, yani Allah ki talwar ka khitab mila.

Aap ki zindagi ka aakhri waqt aaya to unhon ne farmaya: mere jism par aisa
koi hissa nahi jahan talwar ya nezay ka zakhm na ho, phir bhi me bistar par
mar raha hun. Buzdilon ki ankh na khulay.

Allah un se raazi ho. Un ki bahaduri aur qurbaniyan rehti duniya tak yaad
rakhi jayengi. Allah Hafiz."""

COLORS = ["0x1a2b4a", "0x4a1a2b", "0x1a4a2b", "0x2b1a4a", "0x4a3a1a", "0x1a3a4a"]


def tts_chunk(text, out_wav):
    """One chunk -> WAV via vault key (surrogate URL). Returns duration."""
    payload = {
        "contents": [{"parts": [{"text":
            "Speak as a deep, dramatic documentary narrator. Slow, weighty, cinematic.\n\n"
            "Text to speak:\n" + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}},
        },
    }
    url = url_with_surrogate_query_param(
        f"https://generativelanguage.googleapis.com/v1beta/models/{TTS_MODEL}:generateContent",
        "custom.gemini", allowed_hosts=HOSTS)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=180)
    data = read_json_response(resp)
    for p in data["candidates"][0]["content"]["parts"]:
        inl = p.get("inlineData") or p.get("inline_data")
        if inl and inl.get("data"):
            vs._save_tts_audio(inl["data"], out_wav)
            return vs.chunk_duration(out_wav)
    raise RuntimeError("no audio in TTS response")


def main():
    chunks_text = vs.split_script(SCRIPT)
    print("script chunks: %d" % len(chunks_text))

    # 1. TTS voiceover (real API)
    wavs, chunks = [], []
    t = 0.0
    for i, ch in enumerate(chunks_text):
        out = os.path.join(OUT, "vo_%02d.wav" % i)
        dur = tts_chunk(ch, out)
        print("  chunk %d: %.1fs" % (i, dur))
        wavs.append(out)
        chunks.append({"text": ch, "start": t, "end": t + dur})
        t += dur
    ok, mp3 = vs.concat_chunks(wavs, os.path.join(OUT, "voiceover.mp3"))
    assert ok, mp3
    total = t
    print("voiceover: %.1fs total" % total)

    # 2. Synthetic photos (stand-ins for AI photos — quota 429)
    photos = []
    for i in range(len(chunks)):
        ph = os.path.join(OUT, "photo_%02d.png" % i)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=%s:s=1920x1080:d=1" % COLORS[i % len(COLORS)],
                        "-frames:v", "1", ph], check=True)
        photos.append(ph)

    # 3. Assemble contract project
    proj = {
        "version": 1, "name": "e2e-urdu",
        "narration_mp3": "voiceover.mp3",
        "slots": [{
            "id": "s%02d" % i, "start": c["start"], "end": c["end"],
            "text": c["text"][:60], "visual_path": os.path.basename(photos[i]),
            "visual_type": "photo", "motion": "zoom-in", "transition": "xfade",
        } for i, c in enumerate(chunks)],
        "bgm": "", "sfx_events": [], "logo_path": "",
        "grade_preset": "cinematic", "settings": {},
    }

    # 4. Render
    out_mp4 = os.path.join(OUT, "e2e_final.mp4")
    render_project(proj, OUT, out_mp4,
                   progress_cb=lambda ph, d, x: print("  render %s %d/%d" % (ph, d, x)),
                   work_dir=os.path.join(OUT, ".cache"))

    # 5. Verify
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=width,height,r_frame_rate,codec_type:format=duration",
                        "-of", "json", out_mp4],
                       stdout=subprocess.PIPE, text=True)
    info = json.loads(r.stdout)
    dur = float(info["format"]["duration"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    fps = eval(v["r_frame_rate"])
    print("E2E result: %.1fs (script %.1fs), %dx%d, %.1ffps, audio=%s"
          % (dur, total, v["width"], v["height"], fps, bool(a)))
    assert abs(dur - total) < 2.0, "duration drift"
    assert (v["width"], v["height"]) == (1920, 1080)
    assert abs(fps - 30) < 0.5 and a
    # audio start-to-end: check volume in first and last 2s
    # NOTE: volumedetect prints at info level; do NOT use -v error here.
    for ss, tag in (("0", "head"), (str(max(total - 3, 0)), "tail")):
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", ss, "-t", "2", "-i", out_mp4,
             "-af", "volumedetect", "-f", "null", "-"],
            stderr=subprocess.PIPE, text=True)
        import re
        m = re.search(r"mean_volume: ([-\d.]+) dB", p.stderr)
        mv = float(m.group(1)) if m else -99
        print("  audio %s mean_volume=%.1f dB %s" % (tag, mv, "OK" if mv > -60 else "SILENT!"))
        assert mv > -60, "audio silent at " + tag
    # no black frames
    for tt in [1, total / 2, total - 1]:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(tt), "-i", out_mp4,
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
                        "/tmp/e2e.raw"], check=True)
        d = open("/tmp/e2e.raw", "rb").read()
        assert sum(d) / len(d) > 5, "black frame at %s" % tt
    print("E2E PASSED")


if __name__ == "__main__":
    import sys as _sys
    if "--verify-only" in _sys.argv:
        # Re-verify the existing render without spending TTS quota.
        out = os.path.join(OUT, "e2e_final.mp4")
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", out],
                           stdout=subprocess.PIPE, text=True)
        total = float(r.stdout.strip())
        for ss, tag in (("0", "head"), (str(max(total - 3, 0)), "tail")):
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-ss", ss, "-t", "2", "-i", out,
                 "-af", "volumedetect", "-f", "null", "-"],
                stderr=subprocess.PIPE, text=True)
            import re
            m = re.search(r"mean_volume: ([-\d.]+) dB", p.stderr)
            mv = float(m.group(1)) if m else -99
            print("  audio %s mean_volume=%.1f dB %s"
                  % (tag, mv, "OK" if mv > -60 else "SILENT!"))
            assert mv > -60, "audio silent at " + tag
        print("E2E VERIFY-ONLY PASSED")
    else:
        main()
