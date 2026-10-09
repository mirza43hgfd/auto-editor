"""FULL AUTO MODE (Phase 1 skeleton, polish in Phase 2).

User gives ONLY the voiceover MP3. The pipeline:
  1. transcribe_audio      -> script text (Gemini, user's key)
  2. suggest_slot_splits   -> slot boundaries (Gemini) x-checked w/ silence detect
  3. generate_reference_sheet -> character reference board (Gemini image)
  4. per slot: suggest_visual_prompt + generate_image (ref image as input)
  5. build Project: narration + photo slots (Ken Burns, xfade) -> timeline
  6. auto_place_sfx        -> keyword-matched SFX events
  7. caller opens the Project in the timeline for MANUAL OVERRIDE, then renders.

All Gemini calls use the user's own key pool (their quota).
"""

import os
import re
import subprocess

from . import ai_helper, settings_store
from .timeline import Project, Slot, SfxEvent

# keyword -> sfx filename (built-in library under assets/sfx/). Both English
# and Urdu/Hindi-transliterated keywords; matched case-insensitively.
SFX_KEYWORDS = {
    "sword.mp3": ["sword", "talwar", "talvar", "shamsheer", "blade"],
    "horse.mp3": ["horse", "ghora", "ghoda", "cavalry", "sawar", "horseback"],
    "crowd.mp3": ["crowd", "bheer", "bheed", "army", "lashkar", "soldiers", "fauj"],
    "wind.mp3": ["wind", "hawa", "desert", "sehra", "storm", "aandhi"],
    "drum.mp3": ["drum", "dhol", "naqqara", "war cry", "naara", "battle cry"],
    "arrow.mp3": ["arrow", "teer", "archer", "kamaan"],
    "bell.mp3": ["bell", "ghanti", "azan", "call to prayer"],
    "fire.mp3": ["fire", "aag", "burn", "jal"],
    "door.mp3": ["door", "darwaza", "gate", "darwaza"],
    "whoosh.mp3": ["whoosh", "suddenly", "achanak"],
}

MOTION_CYCLE = ["zoom-in", "zoom-out", "pan-left", "pan-right"]


def _progress(cb, stage, done, total, msg=""):
    if cb:
        try:
            cb(stage, done, total, msg)
        except Exception:
            pass


def detect_silences(audio_path, ffmpeg="ffmpeg", noise_db=-35, min_dur=0.7):
    """ffmpeg silencedetect -> list of (start, end) silence spans. Best effort."""
    try:
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", audio_path,
             "-af", "silencedetect=noise=%ddB:d=%s" % (noise_db, min_dur),
             "-f", "null", "-"],
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
            text=True, timeout=300)
    except Exception:
        return []
    spans, cur = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            spans.append((cur, float(m.group(1))))
            cur = None
    return spans


def snap_slots_to_silence(slots, silences, tol=1.5):
    """Nudge slot boundaries to nearby silence midpoints (cross-check)."""
    mids = [(a + b) / 2 for a, b in silences]
    out = []
    for s in slots:
        start, end = s["start_sec"], s["end_sec"]
        for m in mids:
            if abs(start - m) <= tol:
                start = m
                break
        for m in mids:
            if abs(end - m) <= tol:
                end = m
                break
        if end > start:
            out.append({"start_sec": start, "end_sec": end,
                        "label": s.get("label", "")})
    return out or slots


def auto_place_sfx(project, transcript, sfx_dir):
    """Keyword-match transcript per slot -> SfxEvent at slot start."""
    if not os.path.isdir(sfx_dir):
        return 0
    available = {f.lower(): os.path.join(sfx_dir, f) for f in os.listdir(sfx_dir)}
    placed = 0
    low_full = transcript.lower()
    for slot in project.slots:
        seg = low_full  # match against whole transcript segment window below
        start_i = max(0, int(len(low_full) * slot.start_sec / max(project.total_duration, 1)) - 400)
        window = low_full[start_i:start_i + 800]
        for fname, keywords in SFX_KEYWORDS.items():
            if fname.lower() not in available:
                continue
            if any(k in window for k in keywords):
                project.tracks["sfx"].sfx_events.append(
                    SfxEvent(slot.start_sec, available[fname.lower()], 0.8))
                placed += 1
                break  # one SFX per slot max in auto mode
    return placed


def run_script_pipeline(script_text, character_brief, tone_name, work_dir,
                        progress_cb=None, ffmpeg="ffmpeg", skip_images=False,
                        api_key=None):
    """SCRIPT-FIRST pipeline: script -> AI voiceover -> exact slots -> photos.

    Slot boundaries come from the TTS chunk durations (exact) — no
    transcription needed. Returns (ok, Project|error).
    """
    from . import voiceover_studio as vs
    os.makedirs(work_dir, exist_ok=True)
    media_dir = os.path.join(work_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    if not settings_store.has_api_key():
        return False, "Koi API key nahi mili. Settings me key dalo phir auto mode chalao."

    _progress(progress_cb, "voiceover", 0, 7, "Voiceover ban rahi hai...")
    ok, res = vs.generate_voiceover(script_text, tone_name, work_dir,
                                     progress_cb=progress_cb, api_key=api_key,
                                     ffmpeg=ffmpeg)
    if not ok:
        return False, "Voiceover fail: %s" % res
    vo_mp3, chunks = res

    _progress(progress_cb, "slots", 1, 7, "Exact slots ban rahe hain...")
    slots = [{"start_sec": c["start"], "end_sec": c["end"],
              "label": c["text"][:80], "text": c["text"]} for c in chunks]

    ref_path = os.path.join(media_dir, "reference_sheet.png")
    _progress(progress_cb, "reference", 2, 7, "Character reference sheet...")
    if not skip_images:
        ok, res = ai_helper.generate_reference_sheet(character_brief, ref_path,
                                                     api_key=api_key)
        if not ok:
            return False, "Reference sheet fail: %s" % res
    else:
        ref_path = ""

    project = Project(name="Auto video (%s)" % tone_name)
    project.tracks["narration"].media_path = os.path.abspath(vo_mp3)

    for i, s in enumerate(slots):
        _progress(progress_cb, "photos", 3, 7, "Photo %d/%d..." % (i + 1, len(slots)))
        label = s["label"]
        ok, prompt = ai_helper.suggest_visual_prompt(label, api_key=api_key)
        prompt = prompt if ok else label
        img_path = os.path.join(media_dir, "slot_%03d.png" % (i + 1))
        if not skip_images:
            ok, res = ai_helper.generate_image(prompt, img_path, api_key=api_key,
                                               reference_image_path=ref_path or None)
            if not ok:
                return False, "Photo %d fail: %s" % (i + 1, res)
        project.add_slot(Slot(s["start_sec"], s["end_sec"], visual_path=img_path,
                              visual_type="photo",
                              motion_preset=MOTION_CYCLE[i % len(MOTION_CYCLE)],
                              transition="xfade", prompt=prompt, label=label))

    _progress(progress_cb, "sfx", 4, 7, "SFX lag rahe hain...")
    sfx_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "sfx"))
    auto_place_sfx(project, script_text, sfx_dir)

    _progress(progress_cb, "done", 7, 7, "Tayar — timeline check karo, phir Render dabao.")
    return True, project


def run(script, project_dir, progress_cb=None, tone="Deep Dramatic",
        character_brief="", skip_images=False, api_key=None, ffmpeg="ffmpeg"):
    """Contract wrapper around run_script_pipeline.

    Returns (ok, contract_dict | error) where contract_dict is the canonical
    pipeline JSON (timeline.to_contract_dict) with all media paths relative
    to project_dir — ready for render_worker.render_project.

    The Project object is discarded after conversion; callers that need it
    (timeline UI with manual override) should use run_script_pipeline instead.
    """
    os.makedirs(project_dir, exist_ok=True)
    ok, res = run_script_pipeline(
        script, character_brief, tone, project_dir,
        progress_cb=progress_cb, ffmpeg=ffmpeg,
        skip_images=skip_images, api_key=api_key)
    if not ok:
        return False, res
    project = res
    return True, project.to_contract_dict(project_dir)


def run_auto_pipeline(vo_mp3, character_brief, work_dir, progress_cb=None,
                      ffmpeg="ffmpeg", skip_images=False):
    """Run the full-auto pipeline. Returns (ok, Project|error)."""
    os.makedirs(work_dir, exist_ok=True)
    media_dir = os.path.join(work_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    if not settings_store.has_api_key():
        return False, "Koi API key nahi mili. Settings me key dalo phir auto mode chalao."

    _progress(progress_cb, "transcribe", 0, 6, "Voiceover transcribe ho rahi hai...")
    ok, transcript = ai_helper.transcribe_audio(vo_mp3)
    if not ok:
        return False, "Transcribe fail: %s" % transcript

    _progress(progress_cb, "slots", 1, 6, "Slots ban rahe hain...")
    ok, slots = ai_helper.suggest_slot_splits(transcript)
    if not ok:
        return False, "Slot split fail: %s" % slots
    silences = detect_silences(vo_mp3, ffmpeg)
    if silences:
        slots = snap_slots_to_silence(slots, silences)

    ref_path = os.path.join(media_dir, "reference_sheet.png")
    _progress(progress_cb, "reference", 2, 6, "Character reference sheet ban rahi hai...")
    if not skip_images:
        ok, res = ai_helper.generate_reference_sheet(character_brief, ref_path)
        if not ok:
            return False, "Reference sheet fail: %s" % res
    else:
        ref_path = ""

    project = Project(name=os.path.splitext(os.path.basename(vo_mp3))[0] + " (auto)")
    project.tracks["narration"].media_path = os.path.abspath(vo_mp3)

    _progress(progress_cb, "photos", 3, 6, "Slot photos generate ho rahi hain...")
    for i, s in enumerate(slots):
        _progress(progress_cb, "photos", 3, 6,
                  "Photo %d/%d..." % (i + 1, len(slots)))
        label = s.get("label", "")
        ok, prompt = ai_helper.suggest_visual_prompt(
            "%s. Context: %s" % (label, transcript[:600]))
        prompt = prompt if ok else label
        img_path = os.path.join(media_dir, "slot_%03d.png" % (i + 1))
        if not skip_images:
            ok, res = ai_helper.generate_image(prompt, img_path,
                                               reference_image_path=ref_path or None)
            if not ok:
                return False, "Photo %d fail: %s" % (i + 1, res)
        slot = Slot(s["start_sec"], s["end_sec"],
                    visual_path=img_path, visual_type="photo",
                    motion_preset=MOTION_CYCLE[i % len(MOTION_CYCLE)],
                    transition="xfade", prompt=prompt, label=label)
        project.add_slot(slot)

    _progress(progress_cb, "sfx", 4, 6, "SFX lag rahe hain...")
    sfx_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "assets", "sfx")
    auto_place_sfx(project, transcript, os.path.normpath(sfx_dir))

    _progress(progress_cb, "done", 6, 6, "Tayar — timeline me check karo, phir Render dabao.")
    return True, project
