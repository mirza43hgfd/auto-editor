"""Pure-Python timeline data model. No Qt dependency — headless-safe.

Tracks (5): narration, visuals, bgm, sfx, logo.
The visuals track holds Slot objects; narration/bgm/logo hold a single media
path each; sfx holds a list of SfxEvent objects.
Serializes to the .deproj JSON format.
"""

import json
import os
import uuid

from .captions import CaptionCue, CaptionStyle
from .characters import CharacterLibrary

TRACK_KINDS = ("narration", "visuals", "bgm", "sfx", "logo")
VISUAL_TYPES = ("photo", "clip")
MOTION_PRESETS = ("zoom-in", "zoom-out", "pan-left", "pan-right", "none")
# M5: renderer genuinely implements every one of these (render_worker).
# "fadeblack" is the Phase-1 legacy name for "dip-to-black" (kept so old
# .deproj files still load; the renderer treats them identically).
TRANSITIONS = ("xfade", "cut", "fadeblack", "fade-in", "fade-out",
               "dip-to-black")

DEFAULT_SETTINGS = {
    "max_clips": 30,            # scene-video limit (user rule); photos unlimited
    "max_clip_duration_sec": 600,
    "watch_folder": "",         # Flow workflow: drop photos here -> auto-import
    "hook_clip_seconds": 0,     # "first N seconds all video clips" (0 = off)
}

# M6: audio mixer defaults. narration_vol/music_vol are LINEAR multipliers on
# the legacy hardcoded levels (1.8 / 0.4) so old projects render bit-identical;
# sfx_vol is a neutral multiplier (1.0); ducking 1.0 == legacy ratio 6.
DEFAULT_MIXER = {
    "narration_vol": 1.8,
    "music_vol": 0.4,
    "sfx_vol": 1.0,
    "ducking": 1.0,
}


def _clean_mixer(d):
    m = dict(DEFAULT_MIXER)
    for k in m:
        try:
            v = float((d or {}).get(k, m[k]))
        except (TypeError, ValueError):
            continue
        if k == "ducking":
            m[k] = min(1.0, max(0.0, v))
        else:
            m[k] = min(3.0, max(0.0, v))
    return m


class Slot:
    """One narration slot -> one visual.

    Structured scene metadata (M4): purpose, narration_segment, characters
    (character ids into Project.characters), visual_prompt, assets,
    transition_intent, continuity_notes. visual_prompt and the legacy
    `prompt` attribute are the same value (two names, one storage) so old
    projects and old code keep working.
    """

    def __init__(self, start_sec, end_sec, visual_path="", visual_type="photo",
                 motion_preset="zoom-in", transition="xfade",
                 clip_in=0.0, clip_out=0.0, prompt="", label="",
                 purpose="", narration_segment="", characters=None,
                 visual_prompt=None, assets=None, transition_intent="",
                 continuity_notes="", hold_factor=1.0, grade_hint="",
                 edit_notes=""):
        self.id = uuid.uuid4().hex[:8]
        self.start_sec = float(start_sec)
        self.end_sec = float(end_sec)
        self.visual_path = visual_path
        self.visual_type = visual_type if visual_type in VISUAL_TYPES else "photo"
        self.motion_preset = motion_preset if motion_preset in MOTION_PRESETS else "zoom-in"
        self.transition = transition if transition in TRANSITIONS else "xfade"
        self.clip_in = float(clip_in)
        self.clip_out = float(clip_out)
        self._visual_prompt = ""
        # visual_prompt wins when both are given; legacy `prompt` otherwise
        self.visual_prompt = visual_prompt if visual_prompt is not None else prompt
        self.label = label        # short narration label for this slot
        # -- M4 structured scene metadata ----------------------------------
        self.purpose = str(purpose or "")                 # why this scene exists
        self.narration_segment = str(narration_segment or "")  # full narration text
        self.characters = [str(c) for c in (characters or [])]  # character ids
        self.assets = [str(a) for a in (assets or [])]   # extra asset paths
        self.transition_intent = str(transition_intent or "")  # e.g. "gentle fade"
        self.continuity_notes = str(continuity_notes or "")     # wardrobe/props notes
        # -- M5 editing-decision outputs -----------------------------------
        try:
            hf = float(hold_factor)
        except (TypeError, ValueError):
            hf = 1.0
        self.hold_factor = min(2.0, max(0.5, hf))  # pacing multiplier
        self.grade_hint = str(grade_hint or "")    # advisory (see editing.py)
        self.edit_notes = str(edit_notes or "")    # "why" line for the Inspector

    @property
    def prompt(self):
        """Legacy alias of visual_prompt (same storage)."""
        return self._visual_prompt

    @prompt.setter
    def prompt(self, value):
        self._visual_prompt = str(value or "")

    @property
    def visual_prompt(self):
        return self._visual_prompt

    @visual_prompt.setter
    def visual_prompt(self, value):
        self._visual_prompt = str(value or "")

    @property
    def duration(self):
        return max(0.0, self.end_sec - self.start_sec)

    def to_dict(self):
        return {
            "id": self.id, "start_sec": self.start_sec, "end_sec": self.end_sec,
            "visual_path": self.visual_path, "visual_type": self.visual_type,
            "motion_preset": self.motion_preset, "transition": self.transition,
            "clip_in": self.clip_in, "clip_out": self.clip_out,
            "prompt": self._visual_prompt, "visual_prompt": self._visual_prompt,
            "label": self.label,
            "purpose": self.purpose, "narration_segment": self.narration_segment,
            "characters": list(self.characters), "assets": list(self.assets),
            "transition_intent": self.transition_intent,
            "continuity_notes": self.continuity_notes,
            "hold_factor": self.hold_factor, "grade_hint": self.grade_hint,
            "edit_notes": self.edit_notes,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls(d.get("start_sec", 0), d.get("end_sec", 0))
        s.id = d.get("id", s.id)
        s.visual_path = d.get("visual_path", "")
        s.visual_type = d.get("visual_type", "photo")
        s.motion_preset = d.get("motion_preset", "zoom-in")
        s.transition = d.get("transition", "xfade")
        s.clip_in = float(d.get("clip_in", 0.0))
        s.clip_out = float(d.get("clip_out", 0.0))
        # prefer the new key, fall back to the legacy one (backward compat)
        s.visual_prompt = d.get("visual_prompt", d.get("prompt", ""))
        s.label = d.get("label", "")
        s.purpose = d.get("purpose", "")
        s.narration_segment = d.get("narration_segment", "")
        s.characters = [str(c) for c in d.get("characters", [])]
        s.assets = [str(a) for a in d.get("assets", [])]
        s.transition_intent = d.get("transition_intent", "")
        s.continuity_notes = d.get("continuity_notes", "")
        try:
            hf = float(d.get("hold_factor", 1.0))
        except (TypeError, ValueError):
            hf = 1.0
        s.hold_factor = min(2.0, max(0.5, hf))
        s.grade_hint = d.get("grade_hint", "")
        s.edit_notes = d.get("edit_notes", "")
        return s


class SfxEvent:
    def __init__(self, time_sec, path, volume=1.0):
        self.time_sec = float(time_sec)
        self.path = path
        self.volume = float(volume)

    def to_dict(self):
        return {"time_sec": self.time_sec, "path": self.path, "volume": self.volume}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("time_sec", 0), d.get("path", ""), d.get("volume", 1.0))


class Track:
    def __init__(self, kind):
        assert kind in TRACK_KINDS, kind
        self.kind = kind
        self.media_path = ""      # narration / bgm / logo
        self.slots = []           # visuals track
        self.sfx_events = []      # sfx track

    def to_dict(self):
        return {
            "kind": self.kind,
            "media_path": self.media_path,
            "slots": [s.to_dict() for s in self.slots],
            "sfx_events": [e.to_dict() for e in self.sfx_events],
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d.get("kind", "visuals"))
        t.media_path = d.get("media_path", "")
        t.slots = [Slot.from_dict(s) for s in d.get("slots", [])]
        t.sfx_events = [SfxEvent.from_dict(e) for e in d.get("sfx_events", [])]
        return t


class Project:
    def __init__(self, name="Untitled"):
        self.name = name
        self.width = 1920
        self.height = 1080
        self.fps = 30
        self.grade_preset = "cinematic"
        self.settings = dict(DEFAULT_SETTINGS)
        self.tracks = {k: Track(k) for k in TRACK_KINDS}
        self.characters = CharacterLibrary()  # M4: project cast
        # M6: captions + audio mixer (persisted with the project)
        self.caption_cues = []  # list[CaptionCue]
        self.caption_style = CaptionStyle.from_template("cinematic").to_dict()
        self.mixer = dict(DEFAULT_MIXER)

    # -- convenience accessors -------------------------------------------------
    @property
    def slots(self):
        return sorted(self.tracks["visuals"].slots, key=lambda s: s.start_sec)

    @property
    def narration_path(self):
        return self.tracks["narration"].media_path

    @property
    def bgm_path(self):
        return self.tracks["bgm"].media_path

    @property
    def logo_path(self):
        return self.tracks["logo"].media_path

    @property
    def total_duration(self):
        slots = self.slots
        if slots:
            return max(s.end_sec for s in slots)
        return 0.0

    def add_slot(self, slot):
        self.tracks["visuals"].slots.append(slot)

    # -- clip-limit rule (user requirement) ------------------------------------
    def clip_stats(self):
        clips = [s for s in self.slots if s.visual_type == "clip"]
        total = sum(s.duration for s in clips)
        return len(clips), total

    def check_clip_limits(self):
        """Return list of warning strings; empty means within limits."""
        warnings = []
        n, total = self.clip_stats()
        if n > self.settings.get("max_clips", 30):
            warnings.append(
                "Clip limit exceeded: %d clips (max %d). Extra clips will still render; "
                "raise the limit in project settings or replace some with photos."
                % (n, self.settings.get("max_clips", 30)))
        if total > self.settings.get("max_clip_duration_sec", 600):
            warnings.append(
                "Clip duration exceeded: %.0fs total (max %ds)."
                % (total, self.settings.get("max_clip_duration_sec", 600)))
        return warnings

    # -- serialization ----------------------------------------------------------
    def to_dict(self):
        return {
            "app": "auto-editor", "version": 1, "name": self.name,
            "width": self.width, "height": self.height, "fps": self.fps,
            "grade_preset": self.grade_preset,
            "settings": self.settings,
            "tracks": {k: t.to_dict() for k, t in self.tracks.items()},
            "characters": self.characters.to_dict(),
            # M6 (additive; old readers ignore)
            "caption_cues": [c.to_dict() for c in self.caption_cues],
            "caption_style": dict(self.caption_style),
            "mixer": dict(self.mixer),
        }

    @classmethod
    def from_dict(cls, d):
        p = cls(d.get("name", "Untitled"))
        p.width = d.get("width", 1920)
        p.height = d.get("height", 1080)
        p.fps = d.get("fps", 30)
        p.grade_preset = d.get("grade_preset", "cinematic")
        merged = dict(DEFAULT_SETTINGS)
        merged.update(d.get("settings", {}))
        p.settings = merged
        p.tracks = {k: Track.from_dict(t) for k, t in d.get("tracks", {}).items()}
        for k in TRACK_KINDS:
            p.tracks.setdefault(k, Track(k))
        # missing "characters" (pre-M4 projects) -> empty library
        p.characters = CharacterLibrary.from_dict(d.get("characters", {}))
        # M6: captions + mixer (pre-M6 projects -> defaults; garbage -> defaults)
        try:
            p.caption_cues = [CaptionCue.from_dict(c)
                              for c in (d.get("caption_cues") or [])]
        except (TypeError, ValueError, AttributeError):
            p.caption_cues = []
        try:
            p.caption_style = CaptionStyle.from_dict(
                d.get("caption_style") or {}).to_dict()
        except (TypeError, ValueError, AttributeError):
            p.caption_style = CaptionStyle.from_template("cinematic").to_dict()
        p.mixer = _clean_mixer(d.get("mixer"))
        return p

    # -- interface-contract format (flat, shared across workstreams) ---------
    # {version, narration_mp3, slots[{id,start,end,text,visual_path,visual_type,
    #  motion,transition}], bgm, sfx_events[], logo_path, grade_preset}
    # Media paths are relative to the project dir.
    def to_contract_dict(self, project_dir=""):
        def rel(p):
            if not p:
                return ""
            try:
                return os.path.relpath(p, project_dir) if project_dir else p
            except ValueError:
                return p
        return {
            "version": 1,
            "name": self.name,
            "narration_mp3": rel(self.narration_path),
            "slots": [{
                "id": s.id, "start": s.start_sec, "end": s.end_sec,
                "text": s.label, "visual_path": rel(s.visual_path),
                "visual_type": s.visual_type, "motion": s.motion_preset,
                "transition": s.transition, "clip_in": s.clip_in,
                "clip_out": s.clip_out, "prompt": s.prompt,
                # M4 structured scene metadata (additive; old readers ignore)
                "visual_prompt": s.visual_prompt, "purpose": s.purpose,
                "narration_segment": s.narration_segment,
                "characters": list(s.characters), "assets": list(s.assets),
                "transition_intent": s.transition_intent,
                "continuity_notes": s.continuity_notes,
                # M5 editing decisions (additive; old readers ignore)
                "hold_factor": s.hold_factor, "grade_hint": s.grade_hint,
                "edit_notes": s.edit_notes,
            } for s in self.slots],
            "bgm": rel(self.bgm_path),
            "sfx_events": [{
                "time_sec": e.time_sec, "path": rel(e.path), "volume": e.volume,
            } for e in self.tracks["sfx"].sfx_events],
            "logo_path": rel(self.logo_path),
            "grade_preset": self.grade_preset,
            "settings": self.settings,
            "characters": self.characters.to_dict(),
            # M6 (additive; old readers ignore)
            "caption_cues": [c.to_dict() for c in self.caption_cues],
            "caption_style": dict(self.caption_style),
            "mixer": dict(self.mixer),
        }

    @classmethod
    def from_contract_dict(cls, d, project_dir=""):
        def abs_p(p):
            if not p:
                return ""
            if os.path.isabs(p):
                return p
            return os.path.normpath(os.path.join(project_dir, p)) if project_dir else p
        p = cls(d.get("name", "Untitled"))
        p.grade_preset = d.get("grade_preset", "cinematic")
        merged = dict(DEFAULT_SETTINGS)
        merged.update(d.get("settings", {}))
        p.settings = merged
        p.tracks["narration"].media_path = abs_p(d.get("narration_mp3", ""))
        p.tracks["bgm"].media_path = abs_p(d.get("bgm", ""))
        p.tracks["logo"].media_path = abs_p(d.get("logo_path", ""))
        for s in d.get("slots", []):
            slot = Slot(s.get("start", 0), s.get("end", 0),
                        visual_path=abs_p(s.get("visual_path", "")),
                        visual_type=s.get("visual_type", "photo"),
                        motion_preset=s.get("motion", "zoom-in"),
                        transition=s.get("transition", "xfade"),
                        clip_in=s.get("clip_in", 0.0),
                        clip_out=s.get("clip_out", 0.0),
                        prompt=s.get("prompt", ""), label=s.get("text", ""),
                        purpose=s.get("purpose", ""),
                        narration_segment=s.get("narration_segment", ""),
                        characters=s.get("characters", []),
                        visual_prompt=s.get("visual_prompt") or None,
                        assets=s.get("assets", []),
                        transition_intent=s.get("transition_intent", ""),
                        continuity_notes=s.get("continuity_notes", ""))
            slot.id = s.get("id", slot.id)
            slot.hold_factor = min(2.0, max(0.5, float(s.get("hold_factor", 1.0) or 1.0)))
            slot.grade_hint = s.get("grade_hint", "")
            slot.edit_notes = s.get("edit_notes", "")
            p.add_slot(slot)
        p.characters = CharacterLibrary.from_dict(d.get("characters", {}))
        for e in d.get("sfx_events", []):
            p.tracks["sfx"].sfx_events.append(
                SfxEvent(e.get("time_sec", 0), abs_p(e.get("path", "")),
                         e.get("volume", 0.7)))
        # M6 (additive; absent -> defaults)
        try:
            p.caption_cues = [CaptionCue.from_dict(c)
                              for c in (d.get("caption_cues") or [])]
        except (TypeError, ValueError, AttributeError):
            p.caption_cues = []
        try:
            p.caption_style = CaptionStyle.from_dict(
                d.get("caption_style") or {}).to_dict()
        except (TypeError, ValueError, AttributeError):
            p.caption_style = CaptionStyle.from_template("cinematic").to_dict()
        p.mixer = _clean_mixer(d.get("mixer"))
        return p

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def resolve(self, media_dir, path):
        """Resolve a media path: absolute stays, relative resolves under media_dir."""
        if not path:
            return ""
        if os.path.isabs(path) and os.path.exists(path):
            return path
        cand = os.path.join(media_dir, os.path.basename(path))
        if os.path.exists(cand):
            return cand
        if os.path.exists(path):
            return path
        return ""


# ---------------------------------------------------------------------------
# Qt-free timeline mutation logic (M3 editing workspace).
# All functions below operate on plain Project/Slot data — no Qt imports —
# so they are unit-testable headless. The QWidget timeline in
# timeline_widget.py calls these; it adds no mutation logic of its own.
# ---------------------------------------------------------------------------

MIN_TRIM_DUR = 0.5  # seconds; a slot can never be trimmed shorter


def _sorted_slots(project):
    return project.slots  # already sorted by start_sec


def find_slot(project, slot_id):
    for s in project.tracks["visuals"].slots:
        if s.id == slot_id:
            return s
    return None


def reorder_slot(project, slot_id, direction):
    """Move a slot one step left (-1) or right (+1) in timeline order.

    Implemented by swapping the (start_sec, end_sec) timing windows of the
    slot and its neighbour — everything else (visual, prompt, transition)
    stays attached to the slot. Returns True if moved, False at the ends.
    """
    if direction not in (-1, 1):
        return False
    slots = _sorted_slots(project)
    idx = next((i for i, s in enumerate(slots) if s.id == slot_id), None)
    if idx is None:
        return False
    j = idx + direction
    if j < 0 or j >= len(slots):
        return False
    a, b = slots[idx], slots[j]
    (a.start_sec, a.end_sec), (b.start_sec, b.end_sec) = \
        (b.start_sec, b.end_sec), (a.start_sec, a.end_sec)
    return True


def trim_slot(project, slot_id, new_start, new_end):
    """Trim a slot, clamped so it can never go negative or overlap neighbours.

    Returns the (start_sec, end_sec) actually applied. If there is no room
    to honour MIN_TRIM_DUR without overlapping, the slot is left unchanged
    and its current timings are returned.
    """
    slots = _sorted_slots(project)
    idx = next((i for i, s in enumerate(slots) if s.id == slot_id), None)
    if idx is None:
        return None
    s = slots[idx]
    lo = slots[idx - 1].end_sec if idx > 0 else 0.0
    hi = slots[idx + 1].start_sec if idx < len(slots) - 1 else float("inf")
    ns = max(float(new_start), lo, 0.0)
    ne = min(float(new_end), hi)
    if ne - ns < MIN_TRIM_DUR:
        ne = ns + MIN_TRIM_DUR
        if ne > hi:
            ne = hi
            ns = ne - MIN_TRIM_DUR
            if ns < lo or ns < 0.0:
                return (s.start_sec, s.end_sec)  # no room: unchanged
    s.start_sec, s.end_sec = ns, ne
    return (ns, ne)


def set_slot_field(project, slot_id, field, value):
    """Set an inspector-editable field on a slot. Returns True on success.

    Allowed fields: label, prompt, visual_path, visual_type, motion_preset,
    transition, purpose, narration_segment, transition_intent,
    continuity_notes, hold_factor, grade_hint, edit_notes. Enum fields are
    validated (invalid values rejected, not stored). (prompt/visual_prompt
    are the same value — see Slot.)
    """
    s = find_slot(project, slot_id)
    if s is None:
        return False
    if field == "label":
        s.label = str(value)
    elif field in ("prompt", "visual_prompt"):
        s.visual_prompt = str(value)
    elif field == "visual_path":
        s.visual_path = str(value or "")
    elif field == "visual_type":
        if value not in VISUAL_TYPES:
            return False
        s.visual_type = value
    elif field == "motion_preset":
        if value not in MOTION_PRESETS:
            return False
        s.motion_preset = value
    elif field == "transition":
        if value not in TRANSITIONS:
            return False
        s.transition = value
    elif field in ("purpose", "narration_segment", "transition_intent",
                   "continuity_notes", "grade_hint", "edit_notes"):
        setattr(s, field, str(value or ""))
    elif field == "hold_factor":
        try:
            hf = float(value)
        except (TypeError, ValueError):
            return False
        s.hold_factor = min(2.0, max(0.5, hf))
    else:
        return False
    return True


def capture_timeline_state(project):
    """Snapshot the visuals track as plain dicts (for undo/redo)."""
    return [s.to_dict() for s in project.tracks["visuals"].slots]


def restore_timeline_state(project, state):
    """Restore the visuals track from a snapshot. Unknown ids are dropped."""
    project.tracks["visuals"].slots = [Slot.from_dict(d) for d in state]


class TimelineHistory:
    """Bounded undo/redo stack over timeline snapshots. Qt-free."""

    def __init__(self, limit=50):
        self._undo = []
        self._redo = []
        self._limit = max(1, limit)

    def checkpoint(self, project):
        """Call BEFORE mutating the project."""
        self._undo.append(capture_timeline_state(project))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self, project):
        if not self._undo:
            return False
        self._redo.append(capture_timeline_state(project))
        restore_timeline_state(project, self._undo.pop())
        return True

    def redo(self, project):
        if not self._redo:
            return False
        self._undo.append(capture_timeline_state(project))
        restore_timeline_state(project, self._redo.pop())
        return True
