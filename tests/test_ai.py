"""WS2 offline tests (no quota burn): AI-service plumbing without the network.

1. split_script chunking rules
2. TONES + TTS_MODELS structure
3. call_gemini with a mocked _post_model_pool (text extraction + inline media parts)
4. generate_voiceover contract chunk shape [{text,start,end}] with a mocked
   synthesize_chunk (real WAV writing, real chunk_duration, real concat)
5. OPTIONAL live test: one tiny call_gemini("reply OK") via the key pool —
   runs only if a key exists; failure (503/no key) marks the test DEFERRED
   and never fails the suite.
"""

import os
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor import ai_helper
from auto_editor import voiceover_studio as vs
from auto_editor import settings_store

OUT = os.path.join(HERE, "out_ai")
os.makedirs(OUT, exist_ok=True)

passed, failed, deferred = 0, 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        print("FAIL  %s  %s" % (name, detail))


# ---------------------------------------------------------------- 1. split_script
script = (
    "Para one is a short intro line about the warrior.\n\n"
    + ("This is a long paragraph that goes on and on about the battle scene, "
       "the desert wind, the clash of swords and the thunder of hooves. " * 12)
    + "\n\nShort tail."
)
chunks = vs.split_script(script)
check("split_script: non-empty", len(chunks) >= 2, str(chunks))
check("split_script: each chunk <= ~750 chars", all(len(c) <= 750 for c in chunks),
      str([len(c) for c in chunks]))
check("split_script: tiny tail merged, not standalone",
      not any(c.strip() == "Short tail." for c in chunks))
check("split_script: empty script -> []", vs.split_script("   \n  ") == [])

# ---------------------------------------------------------------- 2. TONES
check("TONES: 4 tones", set(vs.TONES) == {"Deep Dramatic", "Calm Storyteller",
                                          "Warm", "Energetic"}, str(vs.TONES.keys()))
check("TONES: voice+style non-empty",
      all(t.get("voice") and t.get("style") for t in vs.TONES.values()))
check("TONES: Deep Dramatic -> Charon (default)",
      vs.TONES["Deep Dramatic"]["voice"] == "Charon")
check("TTS_MODELS: order as specced",
      vs.TTS_MODELS == ["gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts",
                        "gemini-flash-latest"], str(vs.TTS_MODELS))

# ------------------------------------------------- 3. call_gemini (mocked pool)
captured = {}


def fake_post_model_pool(model, payload, api_key=None, timeout=90):
    captured["model"] = model
    captured["payload"] = payload
    parts = payload["contents"][0]["parts"]
    if any(p.get("inline_data") for p in parts):
        return {"candidates": [{"content": {"parts": [{"text": "got-media"}]}}]}
    return {"candidates": [{"content": {"parts": [{"text": "hello"}, {"text": " world"}]}}]}


ai_helper._post_model_pool = fake_post_model_pool
text = ai_helper.call_gemini("say hi")
check("call_gemini: text concat from parts", text == "hello world", repr(text))
check("call_gemini: default model first", captured["model"] == "gemini-3.5-flash",
      captured.get("model"))
check("call_gemini: prompt in payload",
      captured["payload"]["contents"][0]["parts"][0]["text"] == "say hi")

# explicit model + system instruction
ai_helper.call_gemini("p", system="sys", model="my-model")
check("call_gemini: explicit model used", captured["model"] == "my-model")
check("call_gemini: systemInstruction set",
      captured["payload"].get("systemInstruction", {}).get("parts") == [{"text": "sys"}])

# media_paths -> inline_data part
png = os.path.join(OUT, "tiny.png")
with open(png, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)  # not a real image; only plumbing is tested
check("call_gemini: media part returned", ai_helper.call_gemini("x", media_paths=[png]) == "got-media")
parts = captured["payload"]["contents"][0]["parts"]
check("call_gemini: inline_data mime image/png",
      parts[-1].get("inline_data", {}).get("mime_type") == "image/png")

# model fallback on transient failure
calls = {"n": 0}


def flaky(model, payload, api_key=None, timeout=90):
    calls["n"] += 1
    if calls["n"] == 1:
        raise ai_helper.AIError("HTTP_503 unavailable")
    return {"candidates": [{"content": {"parts": [{"text": "fallback-ok"}]}}]}


ai_helper._post_model_pool = flaky
check("call_gemini: falls back to next model on 503",
      ai_helper.call_gemini("x") == "fallback-ok" and calls["n"] == 2,
      str(calls))
ai_helper._post_model_pool = fake_post_model_pool

# -------------------------------- 4. generate_voiceover chunk shape (mocked TTS)
RATE = 24000


def fake_synth(text, tone_name, out_wav, api_key=None):
    with wave.open(out_wav, "wb") as w:  # 1.25s of silence
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00" * int(RATE * 1.25 * 2))
    return True, out_wav


vs.synthesize_chunk = fake_synth
script2 = "First chunk text about the battle.\n\nSecond chunk text about the hero's sword.\n\nThird chunk text about the final victory march."
ok, res = vs.generate_voiceover(script2, "Deep Dramatic", os.path.join(OUT, "vo"),
                                 api_key="DUMMY-KEY")
check("generate_voiceover: ok", ok, str(res)[:150])
if ok:
    mp3, chunks = res
    check("generate_voiceover: mp3 exists", os.path.exists(mp3) and os.path.getsize(mp3) > 1000)
    keys_ok = all(set(c.keys()) == {"text", "start", "end"} for c in chunks)
    check("generate_voiceover: chunk keys {text,start,end}", keys_ok)
    types_ok = all(isinstance(c["text"], str) and isinstance(c["start"], float)
                   and isinstance(c["end"], float) for c in chunks)
    check("generate_voiceover: types str/float/float", types_ok)
    check("generate_voiceover: first start == 0.0", chunks[0]["start"] == 0.0)
    contiguous = all(abs(chunks[i]["start"] - chunks[i - 1]["end"]) < 1e-6
                     for i in range(1, len(chunks)))
    check("generate_voiceover: start[i] == end[i-1] (contiguous)", contiguous)
    check("generate_voiceover: chunk durations ~1.25s",
          all(abs((c["end"] - c["start"]) - 1.25) < 0.05 for c in chunks),
          str([(c["end"] - c["start"]) for c in chunks]))
    check("generate_voiceover: text matches split_script",
          [c["text"] for c in chunks] == vs.split_script(script2))

# contract conversion smoke test on the chunk shape
from auto_editor.timeline import Project, Slot
proj = Project(name="ai-test")
for c in chunks:
    proj.add_slot(Slot(c["start"], c["end"], visual_path="/proj/media/slot.png",
                       visual_type="photo"))
cd = proj.to_contract_dict("/proj")
check("to_contract_dict: slot text/start/end present",
      all("text" in s and isinstance(s["start"], float) for s in cd["slots"]))
check("to_contract_dict: paths relative", cd["slots"][0]["visual_path"] == "media/slot.png")

# ------------------------------------------- 5. optional live tiny call
if settings_store.has_api_key():
    try:
        reply = ai_helper.call_gemini("Reply with exactly: OK", timeout=60)
        check("LIVE: pool path works", "OK" in reply.upper(), repr(reply)[:80])
    except ai_helper.AIError as e:
        deferred += 1
        print("DEFER  LIVE: %s" % str(e)[:100])
else:
    deferred += 1
    print("DEFER  LIVE: no API key in settings store — skipped (no quota burn)")

print("\n%d passed, %d failed, %d deferred" % (passed, failed, deferred))
sys.exit(1 if failed else 0)
