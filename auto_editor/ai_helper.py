"""Gemini API helper — AI assistance for planning/prep.

Uses the user's own API key from settings_store (their key, their PC, their
quota). The key is sent ONLY as the x-goog-api-key header to
generativelanguage.googleapis.com and is NEVER logged, printed, or included
in error messages.

call_gemini() is THE primitive (text in, text out, optional inline media);
every helper below builds on it, so key-pool failover and model fallback
live in one place.
"""

import base64
import json
import os
import urllib.request

from . import settings_store

API_HOST = "https://generativelanguage.googleapis.com"
TEXT_MODEL = "gemini-3.5-flash"          # verified working via generateContent
TEXT_MODEL_FALLBACK = "gemini-flash-latest"
IMAGE_MODEL = "gemini-2.5-flash-image"   # listed in models:list for this key
MAX_INLINE_AUDIO_MB = 15                 # inline_data limit is 20MB; stay under
MAX_INLINE_FILE_MB = 19                # hard per-file inline_data guard

# extension -> mime type for inline media parts
_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
}


class AIError(Exception):
    pass


def _post_single(model, payload, api_key, timeout=90):
    """Low-level generateContent POST with one explicit key.

    Raises AIError (key never in the message). Returns (response, http_code).
    """
    url = "%s/v1beta/models/%s:generateContent" % (API_HOST, model)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp), 200
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise AIError("HTTP_%d %s" % (e.code, detail[:200]))


# HTTP codes / phrases that mean "try the next key", not "give up".
_FAILOVER_CODES = {429, 500, 502, 503}
_FAILOVER_WORDS = ("quota", "rate limit", "rate_limit", "exhausted", "too many requests")


def _post_pool(model, payload, timeout=90):
    """POST using the key pool with auto-failover.

    Tries usable keys in slot order; a key that hits quota/rate/transient
    errors is marked limited (cooldown) and the next key is tried.
    Raises AIError when every key is exhausted.
    """
    slots = settings_store.get_api_key_slots()
    order = settings_store.usable_key_indices()
    # also retry keys that are merely cooling down, as a last resort
    import time
    now = time.time()
    for i, k in enumerate(slots):
        if k and i not in order:
            st = settings_store.get_key_status(i)
            if st["status"] != settings_store.STATUS_FAILED:
                order.append(i)
    if not order:
        raise AIError("Koi API key nahi mili. Settings me key dalo.")

    last_err = None
    for i in order:
        key = slots[i]
        try:
            resp, _ = _post_single(model, payload, key, timeout)
            settings_store.set_key_status(i, settings_store.STATUS_OK)
            return resp
        except AIError as e:
            msg = str(e)
            code = None
            if msg.startswith("HTTP_"):
                try:
                    code = int(msg[5:8])
                except ValueError:
                    pass
            low = msg.lower()
            failover = (code in _FAILOVER_CODES) or any(w in low for w in _FAILOVER_WORDS)
            if code == 403 or "api key" in low or "invalid" in low:
                settings_store.set_key_status(i, settings_store.STATUS_FAILED)
            elif failover:
                settings_store.set_key_status(i, settings_store.STATUS_LIMITED,
                                              settings_store.COOLDOWN_SEC)
            last_err = e
            continue
    raise AIError("Sari keys limit par hain, thori der baad try karo ya nayi key dalo. (%s)"
                  % str(last_err)[:120])


def _text_of(response):
    """Extract concatenated text parts from a generateContent response."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected Gemini response shape.")


def _media_part(path):
    """Local file -> inline_data part. Raises AIError on missing/oversize file."""
    if not path or not os.path.exists(path):
        raise AIError("Media file not found: %s" % (path or ""))
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > MAX_INLINE_FILE_MB:
        raise AIError("File %.1f MB exceeds the %d MB inline limit."
                      % (size_mb, MAX_INLINE_FILE_MB))
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME_BY_EXT.get(ext)
    if not mime:
        raise AIError("Unknown mime type for file: %s" % path)
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return {"inline_data": {"mime_type": mime, "data": data}}


def _payload(prompt, media_paths=None, system=None, generation_config=None):
    """Shared request body builder for call_gemini and generate_image."""
    parts = []
    if prompt:
        parts.append({"text": prompt})
    for p in media_paths or []:
        parts.append(_media_part(p))
    if not parts:
        raise AIError("call_gemini: prompt and media_paths are both empty.")
    body = {"contents": [{"parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if generation_config:
        body["generationConfig"] = generation_config
    return body


def _post_model_pool(model, payload, api_key=None, timeout=90):
    """One model: pool failover POST (or one explicit key). Returns response."""
    if api_key:
        resp, _ = _post_single(model, payload, api_key, timeout)
        return resp
    return _post_pool(model, payload, timeout)


def call_gemini(prompt, system=None, model=None, media_paths=None,
                api_key=None, timeout=90):
    """THE primitive: text in, text out.

    prompt: main instruction text. system: optional system instruction.
    model: explicit model id, or None -> TEXT_MODEL with fallback.
    media_paths: list of local image/audio files sent as inline_data.
    Raises AIError on failure (key never in the message). Never logs the key.
    """
    models = (model,) if model else (TEXT_MODEL, TEXT_MODEL_FALLBACK)
    payload = _payload(prompt, media_paths=media_paths, system=system)
    last_err = None
    for m in models:
        try:
            resp = _post_model_pool(m, payload, api_key=api_key, timeout=timeout)
            return _text_of(resp)
        except AIError as e:
            last_err = e
    raise last_err


def _generate_text(prompt, api_key=None, timeout=90):
    """Legacy wrapper kept for internal callers -> call_gemini."""
    return call_gemini(prompt, api_key=api_key, timeout=timeout)


def _extract_json(text):
    """Pull the first {...} or [...] JSON block out of model text."""
    start_candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not start_candidates:
        raise AIError("Model did not return JSON.")
    start = min(start_candidates)
    opener, closer = ("{", "}") if text[start] == "{" else ("[", "]")
    depth, end = 0, -1
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise AIError("Could not parse JSON from model output.")
    return json.loads(text[start:end])


# --------------------------------------------------------------------------
# Phase 1 public calls (each returns (ok: bool, result_or_error: str/list))
# --------------------------------------------------------------------------

def suggest_slot_splits(script_text, api_key=None):
    """Script text -> suggested slot boundaries [{start_sec, end_sec, label}]."""
    api_key = api_key or settings_store.get_api_key()
    prompt = (
        "You are a documentary editor. Split the narration script below into "
        "visual slots. Each slot should cover one idea/scene, roughly 8-20 seconds. "
        "The script may contain [MM:SS] markers — use them if present, else estimate "
        "even pacing (~900 chars/min Urdu/English speech).\n"
        "Return ONLY a JSON array: [{\"start_sec\": 0, \"end_sec\": 14, \"label\": \"short scene label\"}, ...]. "
        "No other text.\n\nSCRIPT:\n" + script_text[:12000]
    )
    try:
        slots = _extract_json(_generate_text(prompt, api_key))
        clean = []
        for s in slots:
            clean.append({
                "start_sec": float(s.get("start_sec", 0)),
                "end_sec": float(s.get("end_sec", 0)),
                "label": str(s.get("label", ""))[:120],
            })
        return True, clean
    except AIError as e:
        return False, str(e)
    except (ValueError, TypeError) as e:
        return False, "Bad slot data from model: %s" % str(e)[:150]


def suggest_visual_prompt(description, api_key=None):
    """Slot description -> cinematic image-generation prompt (no text in image)."""
    api_key = api_key or settings_store.get_api_key()
    prompt = (
        "Write ONE cinematic image-generation prompt for a historical documentary frame. "
        "Rules: photorealistic, dramatic lighting, no text/captions/watermarks in the image, "
        "no faces of religious figures, 16:9 widescreen composition. "
        "Scene: %s\nReturn only the prompt, 2-4 sentences." % description[:2000]
    )
    try:
        return True, _generate_text(prompt, api_key).strip()
    except AIError as e:
        return False, str(e)


def transcribe_audio(mp3_path, api_key=None):
    """MP3 -> full transcript text via Gemini audio input. Size-guarded."""
    if not mp3_path or not os.path.exists(mp3_path):
        return False, "Audio file not found: %s" % (mp3_path or "")
    size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
    if size_mb > MAX_INLINE_AUDIO_MB:
        return False, ("Audio is %.1f MB — over the %d MB inline limit. "
                       "Phase 2 will add chunked transcription." % (size_mb, MAX_INLINE_AUDIO_MB))
    try:
        text = call_gemini(
            "Transcribe this documentary voiceover completely, in its original "
            "language (Urdu/Hindi/English mix as spoken). Return only the transcript.",
            media_paths=[mp3_path], api_key=api_key, timeout=240)
        return True, text.strip()
    except AIError as e:
        return False, str(e)


def generate_image(prompt, out_path, api_key=None, reference_image_path=None):
    """Prompt -> 16:9 PNG via Gemini image model.

    reference_image_path: optional character reference sheet — passed as a
    multimodal input so the character stays consistent across photos.
    Honest limit: consistency improves a lot, but is not 100% guaranteed.
    """
    if reference_image_path and os.path.exists(reference_image_path):
        media = [reference_image_path]
    else:
        media = []
    payload = _payload(
        ("16:9 widescreen cinematic still, photorealistic, no text in image. "
         + ("Match the character look from the reference image. "
            if reference_image_path else "")
         + prompt[:2000]),
        media_paths=media,
        generation_config={"responseModalities": ["TEXT", "IMAGE"]})
    try:
        resp = _post_model_pool(IMAGE_MODEL, payload, api_key=api_key, timeout=240)
        rparts = resp["candidates"][0]["content"]["parts"]
        for p in rparts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(inline["data"]))
                return True, out_path
        return False, "Model returned no image data."
    except AIError as e:
        return False, str(e)
    except (KeyError, IndexError, TypeError):
        return False, "Unexpected image response shape."


def generate_reference_sheet(character_brief, out_path, api_key=None):
    """Character reference board for consistency across auto-generated photos.

    character_brief: e.g. "Arab commander, dark steel armor, deep green cloak,
    black turban with steel band, Arabian horse, black banner with gold calligraphy".
    """
    prompt = (
        "Character reference board, single 16:9 image split into 4 labeled panels: "
        "(1) full-body front view, (2) armor and clothing close-up, (3) headgear/helmet "
        "close-up, (4) horse and banner. Epic documentary cinematic style, photorealistic, "
        "dramatic rim lighting, neutral background, no text watermarks. Character: "
        + character_brief[:1500])
    return generate_image(prompt, out_path, api_key=api_key)
