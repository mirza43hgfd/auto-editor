"""Voiceover studio — script-first pipeline (no pre-made MP3 needed).

Flow:
  1. split_script(text) -> chunks (~300-500 chars, paragraph-wise).
  2. Preview: synthesize chunk 0 only, user picks tone.
  3. Full generate: every chunk -> Gemini TTS (key pool + model failover),
     concat -> final VO MP3, per-chunk durations recorded for exact slots.
  4. Urdu scripts supported — TTS reads Urdu text directly.

Tone presets (voice + style direction). Charon was the Tariq/Khalid voice,
so it is the Deep Dramatic default.

TTS request shape (verified 2026-10-09 against Google AI Studio docs and the
public gemini TTS guides):
  POST generativelanguage.googleapis.com/v1beta/models/<tts-model>:generateContent
  generationConfig: {responseModalities: ["AUDIO"],
                     speechConfig: {voiceConfig: {prebuiltVoiceConfig: {voiceName}}}}
  -> candidates[0].content.parts[].inlineData.data = base64 audio.
Classic TTS models return raw PCM (24 kHz, 16-bit, mono); newer models
(gemini-3.8-flash-tts) may return WAV-wrapped bytes — _save_tts_audio handles
both via the RIFF header check. Style direction is sent as a text prefix on
the spoken text (the prompt approach that produced the Tariq/Khalid VOs);
the newer enterprise schema's per-part speechMetadata is a Vertex-only shape
and is NOT used on the v1beta REST path.
"""

import base64
import io
import os
import subprocess
import wave

from . import ai_helper
from .ai_helper import AIError
from .jobs import JobPaused, record_stage_error

TONES = {
    "Deep Dramatic": {
        "voice": "Charon",
        "style": "Speak as a deep, dramatic documentary narrator. Slow, weighty, "
                 "cinematic. Full of gravitas.",
    },
    "Calm Storyteller": {
        "voice": "Kore",
        "style": "Speak as a calm, warm storyteller by a fire. Gentle pace, "
                 "clear and soothing.",
    },
    "Warm": {
        "voice": "Aoede",
        "style": "Speak in a warm, friendly, inviting tone, like a trusted friend "
                 "telling a story.",
    },
    "Energetic": {
        "voice": "Fenrir",
        "style": "Speak with high energy and excitement, fast-paced and punchy, "
                 "like a movie trailer voice.",
    },
}

TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts",
              "gemini-flash-latest"]
TARGET_CHARS = (300, 500)


def split_script(text):
    """Split script into ~300-500 char chunks on paragraph boundaries."""
    paras = [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]
    chunks, cur = [], ""
    lo, hi = TARGET_CHARS
    for p in paras:
        if cur and len(cur) + len(p) + 1 > hi:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip() if cur else p
        # hard-split an over-long single paragraph on sentence ends
        while len(cur) > hi * 1.5:
            cut = max(cur.rfind(". ", 0, hi), cur.rfind("؟ ", 0, hi),
                      cur.rfind("! ", 0, hi))
            cut = cut + 2 if cut > 0 else hi
            chunks.append(cur[:cut].strip())
            cur = cur[cut:].strip()
    if cur:
        # merge a tiny tail into the previous chunk
        if chunks and len(cur) < lo // 2:
            chunks[-1] = chunks[-1] + " " + cur
        else:
            chunks.append(cur)
    return chunks


def _pcm_to_wav(pcm_bytes, out_wav, rate=24000):
    """Wrap raw 16-bit mono PCM as WAV (Gemini TTS returns raw PCM)."""
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)


def _save_tts_audio(inline_data_b64, out_wav):
    raw = base64.b64decode(inline_data_b64)
    if raw[:4] == b"RIFF":
        with open(out_wav, "wb") as f:
            f.write(raw)
    else:
        _pcm_to_wav(raw, out_wav)


def synthesize_chunk(text, tone_name, out_wav, api_key=None):
    """One script chunk -> WAV via Gemini TTS. Returns (ok, path|error)."""
    tone = TONES.get(tone_name, TONES["Deep Dramatic"])
    styled = "%s\n\nText to speak:\n%s" % (tone["style"], text[:4000])
    payload = {
        "contents": [{"parts": [{"text": styled}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig":
                                             {"voiceName": tone["voice"]}}},
        },
    }
    last_err = "no models tried"
    for model in TTS_MODELS:
        try:
            if api_key:
                resp, _ = ai_helper._post_single(model, payload, api_key, timeout=180)
            else:
                resp = ai_helper._post_pool(model, payload, timeout=180)
            parts = resp["candidates"][0]["content"]["parts"]
            for p in parts:
                inline = p.get("inlineData") or p.get("inline_data")
                if inline and inline.get("data"):
                    _save_tts_audio(inline["data"], out_wav)
                    return True, out_wav
            last_err = "no audio in response"
        except AIError as e:
            last_err = str(e)
    return False, "TTS fail: %s" % last_err[:200]


def chunk_duration(wav_path, ffmpeg="ffmpeg"):
    """Exact seconds of a WAV via ffprobe (falls back to wave module)."""
    try:
        import shutil
        probe = shutil.which("ffprobe")
        if probe:
            p = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", wav_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=30)
            return float(p.stdout.strip())
    except Exception:
        pass
    with wave.open(wav_path, "rb") as w:
        return w.getnframes() / w.getframerate()


def concat_chunks(wav_files, out_mp3, ffmpeg="ffmpeg"):
    """Concat chunk WAVs -> final VO MP3. Returns (ok, path|error)."""
    lst = out_mp3 + ".lst"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for w in wav_files:
                f.write("file '%s'\n" % w.replace("'", "'\\''"))
        p = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
             "-i", lst, "-c:a", "libmp3lame", "-b:a", "192k", out_mp3],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=600)
        if p.returncode != 0:
            return False, p.stderr[-500:]
        return True, out_mp3
    finally:
        if os.path.exists(lst):
            os.remove(lst)


def preview_chunk(script_text, tone_name, work_dir, api_key=None):
    """Synthesize ONLY the first chunk for tone preview. (ok, wav|error)."""
    chunks = split_script(script_text)
    if not chunks:
        return False, "Script khaali hai."
    out = os.path.join(work_dir, "preview_chunk0.wav")
    return synthesize_chunk(chunks[0], tone_name, out, api_key)


def generate_voiceover(script_text, tone_name, work_dir, progress_cb=None,
                       api_key=None, ffmpeg="ffmpeg", job=None,
                       pause_event=None):
    """Full voiceover: chunks -> TTS -> concat MP3.

    Returns (ok, (mp3_path, chunks) | error) where
      chunks[i] = {"text": str, "start": float_sec, "end": float_sec}
    Starts are cumulative sums of the exact per-chunk durations, so slots
    built from chunks need no transcription.

    M7 job support: with job (a jobs.JobStore) every finished chunk is
    checkpointed and completed chunks are skipped on resume (the wav must
    still exist). pause_event (threading.Event) is checked between chunks
    and raises jobs.JobPaused. Failures are classified + recorded in the
    manifest via record_stage_error and still returned as (False, msg) —
    the caller escalates with jobs.raise_if_paused().
    """
    chunks = split_script(script_text)
    if not chunks:
        return False, "Script khaali hai."
    os.makedirs(work_dir, exist_ok=True)
    if job is not None:
        job.start_stage("voiceover")   # preserves checkpoint (resume-safe)
        job.set_total("voiceover", len(chunks))
    wavs, durations = [], []
    for i, ch in enumerate(chunks):
        unit = "chunk_%03d" % i
        out = os.path.join(work_dir, "vo_chunk_%03d.wav" % i)
        if pause_event is not None and pause_event.is_set():
            raise JobPaused("User ne pause kiya.",
                            "Resume dabao — voiceover wahin se shuru hogi.")
        if job is not None and job.is_unit_done("voiceover", unit) \
                and os.path.exists(out):
            if progress_cb:
                progress_cb("tts", i, len(chunks),
                            "Chunk %d/%d (pehle se tayar)..." % (i + 1, len(chunks)))
            wavs.append(out)
            durations.append(chunk_duration(out, ffmpeg))
            continue
        if progress_cb:
            progress_cb("tts", i, len(chunks), "Chunk %d/%d..." % (i + 1, len(chunks)))
        ok, res = synthesize_chunk(ch, tone_name, out, api_key)
        if not ok:
            if job is not None:
                record_stage_error(job, "voiceover", AIError(res))
            return False, res
        if job is not None:
            job.mark_unit_done("voiceover", unit, {"wav": out})
        wavs.append(out)
        durations.append(chunk_duration(out, ffmpeg))
    mp3 = os.path.join(work_dir, "voiceover_final.mp3")
    ok, res = concat_chunks(wavs, mp3, ffmpeg)
    if not ok:
        if job is not None:
            record_stage_error(job, "voiceover", AIError("Concat fail: %s" % res))
        return False, "Concat fail: %s" % res
    if progress_cb:
        progress_cb("tts", len(chunks), len(chunks), "Voiceover tayar.")
    timed, t = [], 0.0
    for ch, dur in zip(chunks, durations):
        timed.append({"text": ch, "start": t, "end": t + dur})
        t += dur
    if job is not None:
        job.checkpoint("voiceover", {"mp3": mp3, "chunks": timed})
        job.complete_stage("voiceover")
    return True, (mp3, timed)
