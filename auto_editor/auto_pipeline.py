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
from .ai_helper import AIError
from .characters import Character, attach_character
from .editing import apply_editing_decisions
from .jobs import JobStore, JobPaused, record_stage_error, raise_if_paused
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

# (M5) Slot motion/transitions are NOT cycled from a fixed preset list —
# editing.apply_editing_decisions() derives them from each scene's content
# (brief §6: no repetitive zoom presets).


def _progress(cb, stage, done, total, msg=""):
    if cb:
        try:
            cb(stage, done, total, msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# M4: character-aware scene visual generation
# ---------------------------------------------------------------------------

def build_scene_prompt(project, slot):
    """Build (prompt_text, reference_image_path) for one scene's visual.

    The first attached library character (in slot order) supplies its
    primary reference image plus the locked visual-description block, so
    the character stays consistent across scenes. With no attached
    character the plain visual prompt is used and no reference image is
    passed.
    """
    from .characters import build_character_lock
    char = None
    for cid in slot.characters:
        c = project.characters.get(cid)
        if c is not None:
            char = c
            break
    scene = (slot.visual_prompt or "").strip() or (slot.label or "")
    if char is None:
        return scene, ""
    prompt = "%s\n\nScene: %s" % (build_character_lock(char), scene)
    return prompt, char.primary_reference


def generate_scene_visual(project, slot_id, work_dir, api_key=None,
                          progress_cb=None):
    """Regenerate ONE scene's visual without touching other scenes.

    Makes exactly one ai_helper.generate_image call, passing the attached
    character's primary reference image when one exists. On success the
    slot's visual_path is updated. Returns (ok, out_path | error).
    """
    from .timeline import find_slot
    slot = find_slot(project, slot_id)
    if slot is None:
        return False, "Slot nahi mila: %s" % slot_id
    prompt, ref_path = build_scene_prompt(project, slot)
    if slot.visual_path:
        out_path = slot.visual_path
    elif work_dir:
        media_dir = os.path.join(work_dir, "media")
        os.makedirs(media_dir, exist_ok=True)
        out_path = os.path.join(media_dir, "scene_%s.png" % slot.id)
    else:
        return False, ("Slot me koi visual path nahi hai — pehle Inspector me "
                       "visual lagao, phir regenerate karo.")
    _progress(progress_cb, "photo", 0, 1, "Scene visual ban raha hai...")
    ok, res = ai_helper.generate_image(prompt, out_path, api_key=api_key,
                                       reference_image_path=ref_path or None)
    if not ok:
        return False, res
    slot.visual_path = out_path
    _progress(progress_cb, "photo", 1, 1, "Scene visual tayar.")
    return True, out_path


def _make_main_character(project, character_brief, ref_path):
    """Register the auto pipeline's reference sheet as a library Character.

    Returns the Character, or None when there is no brief/sheet (the
    pipeline then behaves exactly as before — plain prompts, no reference).
    """
    if not (character_brief or "").strip() or not ref_path:
        return None
    char = Character("Main character", description=character_brief.strip())
    char.add_reference(os.path.abspath(ref_path), make_primary=True)
    project.characters.add(char)
    return char


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
                        api_key=None, job=None, pause_event=None):
    """SCRIPT-FIRST pipeline: script -> AI voiceover -> exact slots -> photos.

    Slot boundaries come from the TTS chunk durations (exact) — no
    transcription needed. Returns (ok, Project|error).

    M7: job (jobs.JobStore, created when None) persists per-stage
    checkpoints; pause_event (threading.Event) pauses between units.
    Raises jobs.JobPaused when the job pauses (manifest already updated) —
    the caller shows Resume, never a failure.
    """
    os.makedirs(work_dir, exist_ok=True)
    job = job or JobStore(work_dir)
    try:
        return _run_script_pipeline(job, pause_event, script_text,
                                    character_brief, tone_name, work_dir,
                                    progress_cb, ffmpeg, skip_images, api_key)
    except JobPaused as e:
        job.pause_job(e.user_message, e.resume_hint)
        raise


def _run_script_pipeline(job, pause_event, script_text, character_brief,
                         tone_name, work_dir, progress_cb=None, ffmpeg="ffmpeg",
                         skip_images=False, api_key=None):
    """Inner body of run_script_pipeline (job/pause owned by the wrapper)."""

    def _paused():
        if pause_event is not None and pause_event.is_set():
            raise JobPaused("User ne pause kiya.",
                            "Resume dabao — kaam wahin se shuru hoga.")

    from . import voiceover_studio as vs
    media_dir = os.path.join(work_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    if not settings_store.has_api_key():
        return False, "Koi API key nahi mili. Settings me key dalo phir auto mode chalao."

    _paused()
    if job.is_stage_done("voiceover"):
        # Resume: timings already exact in the manifest — no re-synthesis.
        cp = job.checkpoint_of("voiceover")
        vo_mp3, chunks = cp["mp3"], cp["chunks"]
        _progress(progress_cb, "voiceover", 0, 7,
                  "Voiceover pehle se tayar (%d chunks)." % len(chunks))
    else:
        _progress(progress_cb, "voiceover", 0, 7, "Voiceover ban rahi hai...")
        ok, res = vs.generate_voiceover(script_text, tone_name, work_dir,
                                         progress_cb=progress_cb, api_key=api_key,
                                         ffmpeg=ffmpeg, job=job,
                                         pause_event=pause_event)
        if not ok:
            raise_if_paused(job)  # paused -> JobPaused (manifest updated)
            return False, "Voiceover fail: %s" % res
        vo_mp3, chunks = res

    if job.is_stage_done("slots"):
        slots = job.checkpoint_of("slots")["slots"]
        _progress(progress_cb, "slots", 1, 7,
                  "Slots pehle se tayar (%d)." % len(slots))
    else:
        _progress(progress_cb, "slots", 1, 7, "Exact slots ban rahe hain...")
        job.start_stage("slots")
        slots = [{"start_sec": c["start"], "end_sec": c["end"],
                  "label": c["text"][:80], "text": c["text"]} for c in chunks]
        job.checkpoint("slots", {"slots": slots})
        job.complete_stage("slots")

    ref_path = os.path.join(media_dir, "reference_sheet.png")
    _progress(progress_cb, "reference", 2, 7, "Character reference sheet...")
    if not skip_images:
        if job.is_stage_done("reference") and os.path.exists(
                job.checkpoint_of("reference").get("path") or ref_path):
            ref_path = job.checkpoint_of("reference")["path"]
            _progress(progress_cb, "reference", 2, 7,
                      "Reference sheet pehle se tayar.")
        else:
            _paused()
            job.start_stage("reference")
            ok, res = ai_helper.generate_reference_sheet(character_brief, ref_path,
                                                         api_key=api_key)
            if not ok:
                if record_stage_error(job, "reference", AIError(res)) == "paused":
                    raise JobPaused(job.pause_reason, job.pause_hint)
                return False, "Reference sheet fail: %s" % res
            job.checkpoint("reference", {"path": ref_path})
            job.complete_stage("reference")
    else:
        ref_path = ""
        # M7: skipped stages still complete in the manifest (nothing to do).
        if not job.is_stage_done("reference"):
            job.start_stage("reference")
            job.checkpoint("reference", {"path": "", "skipped": True})
            job.complete_stage("reference")

    project = Project(name="Auto video (%s)" % tone_name)
    project.tracks["narration"].media_path = os.path.abspath(vo_mp3)

    # M4: register the reference sheet as a real library character and
    # attach it to every scene — photos then carry the locked look.
    main_char = _make_main_character(project, character_brief, ref_path)

    if not job.is_stage_done("photos"):
        job.start_stage("photos")   # preserves checkpoint (resume-safe)
    job.set_total("photos", len(slots))

    for i, s in enumerate(slots):
        _paused()
        unit = "slot_%03d" % (i + 1)
        img_path = os.path.join(media_dir, "slot_%03d.png" % (i + 1))
        # Resume: completed unit + file on disk -> rebuild slot, no API call.
        done_already = job.is_unit_done("photos", unit) and os.path.exists(img_path)
        if done_already:
            prompt = (job.unit_extra("photos", unit) or {}).get("prompt") or s["label"]
            _progress(progress_cb, "photos", 3, 7,
                      "Photo %d/%d (pehle se tayar)..." % (i + 1, len(slots)))
        else:
            _progress(progress_cb, "photos", 3, 7,
                      "Photo %d/%d..." % (i + 1, len(slots)))
            _paused()
            label = s["label"]
            ok, prompt = ai_helper.suggest_visual_prompt(label, api_key=api_key)
            prompt = prompt if ok else label
        slot = Slot(s["start_sec"], s["end_sec"], visual_path=img_path,
                    visual_type="photo",
                    visual_prompt=prompt, label=s["label"],
                    narration_segment=s.get("text", ""))
        project.add_slot(slot)
        if main_char is not None:
            attach_character(project, slot.id, main_char.id)
        if skip_images or done_already:
            continue
        ok, res = generate_scene_visual(project, slot.id, work_dir,
                                        api_key=api_key)
        if not ok:
            if record_stage_error(job, "photos", AIError(res)) == "paused":
                raise JobPaused(job.pause_reason, job.pause_hint)
            return False, "Photo %d fail: %s" % (i + 1, res)
        job.mark_unit_done("photos", unit, {"path": img_path, "prompt": prompt})
    job.complete_stage("photos")

    job.start_stage("assemble")
    _progress(progress_cb, "editing", 5, 7, "Editing decisions (transitions/motion)...")
    apply_editing_decisions(project)

    _progress(progress_cb, "sfx", 4, 7, "SFX lag rahe hain...")
    sfx_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "sfx"))
    auto_place_sfx(project, script_text, sfx_dir)
    job.checkpoint("assemble", {"slots": len(slots)})
    job.complete_stage("assemble")

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
                      ffmpeg="ffmpeg", skip_images=False, job=None,
                      pause_event=None):
    """Run the full-auto pipeline. Returns (ok, Project|error).

    M7: same job/pause contract as run_script_pipeline — raises
    jobs.JobPaused when the job pauses (manifest already updated).
    """
    os.makedirs(work_dir, exist_ok=True)
    job = job or JobStore(work_dir)
    try:
        return _run_auto_pipeline(job, pause_event, vo_mp3, character_brief,
                                  work_dir, progress_cb, ffmpeg, skip_images)
    except JobPaused as e:
        job.pause_job(e.user_message, e.resume_hint)
        raise


def _run_auto_pipeline(job, pause_event, vo_mp3, character_brief, work_dir,
                       progress_cb=None, ffmpeg="ffmpeg", skip_images=False):
    """Inner body of run_auto_pipeline (job/pause owned by the wrapper)."""

    def _paused():
        if pause_event is not None and pause_event.is_set():
            raise JobPaused("User ne pause kiya.",
                            "Resume dabao — kaam wahin se shuru hoga.")

    os.makedirs(work_dir, exist_ok=True)
    media_dir = os.path.join(work_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    if not settings_store.has_api_key():
        return False, "Koi API key nahi mili. Settings me key dalo phir auto mode chalao."

    _paused()
    if job.is_stage_done("voiceover"):
        # voiceover stage = transcription here; reuse cached transcript.
        transcript = job.checkpoint_of("voiceover").get("transcript", "")
        _progress(progress_cb, "transcribe", 0, 6, "Transcript pehle se tayar.")
    else:
        _progress(progress_cb, "transcribe", 0, 6, "Voiceover transcribe ho rahi hai...")
        job.start_stage("voiceover")
        ok, transcript = ai_helper.transcribe_audio(vo_mp3)
        if not ok:
            if record_stage_error(job, "voiceover", AIError(transcript)) == "paused":
                raise JobPaused(job.pause_reason, job.pause_hint)
            return False, "Transcribe fail: %s" % transcript
        job.checkpoint("voiceover", {"transcript": transcript})
        job.complete_stage("voiceover")

    _paused()
    if job.is_stage_done("slots"):
        slots = job.checkpoint_of("slots")["slots"]
        _progress(progress_cb, "slots", 1, 6,
                  "Slots pehle se tayar (%d)." % len(slots))
    else:
        _progress(progress_cb, "slots", 1, 6, "Slots ban rahe hain...")
        job.start_stage("slots")
        ok, slots = ai_helper.suggest_slot_splits(transcript)
        if not ok:
            if record_stage_error(job, "slots", AIError(slots)) == "paused":
                raise JobPaused(job.pause_reason, job.pause_hint)
            return False, "Slot split fail: %s" % slots
        silences = detect_silences(vo_mp3, ffmpeg)
        if silences:
            slots = snap_slots_to_silence(slots, silences)
        job.checkpoint("slots", {"slots": slots})
        job.complete_stage("slots")

    ref_path = os.path.join(media_dir, "reference_sheet.png")
    _progress(progress_cb, "reference", 2, 6, "Character reference sheet ban rahi hai...")
    if not skip_images:
        if job.is_stage_done("reference") and os.path.exists(
                job.checkpoint_of("reference").get("path") or ref_path):
            ref_path = job.checkpoint_of("reference")["path"]
            _progress(progress_cb, "reference", 2, 6,
                      "Reference sheet pehle se tayar.")
        else:
            _paused()
            job.start_stage("reference")
            ok, res = ai_helper.generate_reference_sheet(character_brief, ref_path)
            if not ok:
                if record_stage_error(job, "reference", AIError(res)) == "paused":
                    raise JobPaused(job.pause_reason, job.pause_hint)
                return False, "Reference sheet fail: %s" % res
            job.checkpoint("reference", {"path": ref_path})
            job.complete_stage("reference")
    else:
        ref_path = ""
        # M7: skipped stages still complete in the manifest (nothing to do).
        if not job.is_stage_done("reference"):
            job.start_stage("reference")
            job.checkpoint("reference", {"path": "", "skipped": True})
            job.complete_stage("reference")

    project = Project(name=os.path.splitext(os.path.basename(vo_mp3))[0] + " (auto)")
    project.tracks["narration"].media_path = os.path.abspath(vo_mp3)

    # M4: same character wiring as the script-first pipeline.
    main_char = _make_main_character(project, character_brief, ref_path)

    _progress(progress_cb, "photos", 3, 6, "Slot photos generate ho rahi hain...")
    if not job.is_stage_done("photos"):
        job.start_stage("photos")   # preserves checkpoint (resume-safe)
    job.set_total("photos", len(slots))
    for i, s in enumerate(slots):
        _paused()
        unit = "slot_%03d" % (i + 1)
        img_path = os.path.join(media_dir, "slot_%03d.png" % (i + 1))
        # Resume: completed unit + file on disk -> rebuild slot, no API call.
        done_already = job.is_unit_done("photos", unit) and os.path.exists(img_path)
        if done_already:
            prompt = (job.unit_extra("photos", unit) or {}).get("prompt") \
                or s.get("label", "")
            _progress(progress_cb, "photos", 3, 6,
                      "Photo %d/%d (pehle se tayar)..." % (i + 1, len(slots)))
        else:
            _progress(progress_cb, "photos", 3, 6,
                      "Photo %d/%d..." % (i + 1, len(slots)))
            _paused()
            label = s.get("label", "")
            ok, prompt = ai_helper.suggest_visual_prompt(
                "%s. Context: %s" % (label, transcript[:600]))
            prompt = prompt if ok else label
        slot = Slot(s["start_sec"], s["end_sec"],
                    visual_path=img_path, visual_type="photo",
                    visual_prompt=prompt, label=s.get("label", ""),
                    narration_segment=s.get("label", ""))
        project.add_slot(slot)
        if main_char is not None:
            attach_character(project, slot.id, main_char.id)
        if skip_images or done_already:
            continue
        ok, res = generate_scene_visual(project, slot.id, work_dir)
        if not ok:
            if record_stage_error(job, "photos", AIError(res)) == "paused":
                raise JobPaused(job.pause_reason, job.pause_hint)
            return False, "Photo %d fail: %s" % (i + 1, res)
        job.mark_unit_done("photos", unit, {"path": img_path, "prompt": prompt})
    job.complete_stage("photos")

    job.start_stage("assemble")
    _progress(progress_cb, "editing", 5, 6, "Editing decisions (transitions/motion)...")
    apply_editing_decisions(project)

    _progress(progress_cb, "sfx", 4, 6, "SFX lag rahe hain...")
    sfx_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "assets", "sfx")
    auto_place_sfx(project, transcript, os.path.normpath(sfx_dir))
    job.checkpoint("assemble", {"slots": len(slots)})
    job.complete_stage("assemble")

    _progress(progress_cb, "done", 6, 6, "Tayar — timeline me check karo, phir Render dabao.")
    return True, project
