"""Pure-Python timeline data model. No Qt dependency — headless-safe.

Tracks (5): narration, visuals, bgm, sfx, logo.
The visuals track holds Slot objects; narration/bgm/logo hold a single media
path each; sfx holds a list of SfxEvent objects.
Serializes to the .deproj JSON format.
"""

import json
import os
import uuid

TRACK_KINDS = ("narration", "visuals", "bgm", "sfx", "logo")
VISUAL_TYPES = ("photo", "clip")
MOTION_PRESETS = ("zoom-in", "zoom-out", "pan-left", "pan-right", "none")
TRANSITIONS = ("xfade", "cut", "fadeblack")

DEFAULT_SETTINGS = {
    "max_clips": 30,            # scene-video limit (user rule); photos unlimited
    "max_clip_duration_sec": 600,
    "watch_folder": "",         # Flow workflow: drop photos here -> auto-import
    "hook_clip_seconds": 0,     # "first N seconds all video clips" (0 = off)
}


class Slot:
    """One narration slot -> one visual."""

    def __init__(self, start_sec, end_sec, visual_path="", visual_type="photo",
                 motion_preset="zoom-in", transition="xfade",
                 clip_in=0.0, clip_out=0.0, prompt="", label=""):
        self.id = uuid.uuid4().hex[:8]
        self.start_sec = float(start_sec)
        self.end_sec = float(end_sec)
        self.visual_path = visual_path
        self.visual_type = visual_type if visual_type in VISUAL_TYPES else "photo"
        self.motion_preset = motion_preset if motion_preset in MOTION_PRESETS else "zoom-in"
        self.transition = transition if transition in TRANSITIONS else "xfade"
        self.clip_in = float(clip_in)
        self.clip_out = float(clip_out)
        self.prompt = prompt      # visual prompt text (Flow copy-paste workflow)
        self.label = label        # short narration label for this slot

    @property
    def duration(self):
        return max(0.0, self.end_sec - self.start_sec)

    def to_dict(self):
        return {
            "id": self.id, "start_sec": self.start_sec, "end_sec": self.end_sec,
            "visual_path": self.visual_path, "visual_type": self.visual_type,
            "motion_preset": self.motion_preset, "transition": self.transition,
            "clip_in": self.clip_in, "clip_out": self.clip_out,
            "prompt": self.prompt, "label": self.label,
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
        s.prompt = d.get("prompt", "")
        s.label = d.get("label", "")
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
            } for s in self.slots],
            "bgm": rel(self.bgm_path),
            "sfx_events": [{
                "time_sec": e.time_sec, "path": rel(e.path), "volume": e.volume,
            } for e in self.tracks["sfx"].sfx_events],
            "logo_path": rel(self.logo_path),
            "grade_preset": self.grade_preset,
            "settings": self.settings,
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
                        prompt=s.get("prompt", ""), label=s.get("text", ""))
            slot.id = s.get("id", slot.id)
            p.add_slot(slot)
        for e in d.get("sfx_events", []):
            p.tracks["sfx"].sfx_events.append(
                SfxEvent(e.get("time_sec", 0), abs_p(e.get("path", "")),
                         e.get("volume", 0.7)))
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
