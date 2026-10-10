"""Intelligent editing decisions (M5). Qt-free, deterministic.

decide_edit(slot, prev_slot, next_slot, project) -> EditDecision

The decision layer kills the repetitive-preset slideshow feel: every
transition / motion / pacing choice is DERIVED FROM the scene's content
(purpose, narration text, emotional keywords, neighbours) — never from a
fixed cycling order. The same inputs always produce the same decision
(stable md5 tie-breaks, no randomness), so decisions are unit-testable.

RULES (priority order, tunable via the module constants below):
  1. END      purpose mentions end/conclusion/epilogue -> dip-to-black,
              longer hold, no motion (let the moment land).
  2. ACTION   battle/scream/charge... keywords -> hard cut, push-in motion,
              slightly tighter pacing (hold < 1).
  3. QUIET    dawn/prayer/silence... keywords -> gentle crossfade (fade-in
              when it opens the video), slow pull-back or still, longer hold.
  4. CHANGE   new characters on screen, or "meanwhile"/"new location" cues
              -> gentle crossfade into the new moment.
  5. OPEN     first slot of the project (neutral content) -> fade-in from
              black. (Quiet/action openings are decided by rules 2-3.)
  6. CONTINUE next scene carries the same moment (same characters, no change
              markers) -> hard cut, restrained motion.
  7. DEFAULT  gentle crossfade, mild push-in, neutral pacing.

grade_hint is advisory metadata (a key of render_worker's GRADE_PRESETS);
the renderer currently applies the project-level grade preset, so a
per-slot grade_hint is stored, shown in the Inspector, and documented as a
known limitation (see ARCHITECTURE.md M5).
"""

import hashlib

from .timeline import MOTION_PRESETS, TRANSITIONS

# ---------------------------------------------------------------------------
# Tunable keyword sets. English + Urdu-script + transliterated forms, because
# narration segments are usually Urdu. Matched case-insensitively against the
# scene's combined text (purpose + narration + label + intents + notes).
# ---------------------------------------------------------------------------

ACTION_KW = frozenset((
    # English
    "battle", "fight", "war", "clash", "charge", "attack", "strike", "assault",
    "scream", "shout", "roar", "explosion", "chase", "flee", "kill", "slain",
    "blood", "sword", "arrow", "siege", "ambush", "martyr", "onslaught",
    # Urdu script
    "جنگ", "لڑائی", "حملہ", "چیخ", "تلوار", "لشکر", "شہید", "خون",
    # Transliterated
    "jang", "larai", "hamla", "cheekh", "talwar", "lashkar", "shaheed",
    "qatal", "khoon", "maar dhaar",
))

QUIET_KW = frozenset((
    # English
    "quiet", "silence", "silent", "calm", "peaceful", "dawn", "dusk",
    "night", "prayer", "pray", "dua", "mourn", "grave", "tear", "weep",
    "lonely", "solitude", "stars", "moon", "gentle", "soft", "whisper",
    "stillness", "contemplat",
    # Urdu script
    "خاموش", "دعا", "صبح", "فجر", "رات", "آنسو", "پرسکون", "نماز",
    "تنہا", "چاند",
    # Transliterated
    "khamosh", "khamoshi", "subah", "fajr", "raat", "aansu", "pursukoon",
    "namaz", "tanha", "chand",
))

END_KW = frozenset((
    "end", "ending", "conclusion", "conclude", "closing", "epilogue",
    "finale", "farewell", "last scene", "final",
    "اختتام", "خاتمہ", "ikhtitam", "khatma", "aakhri",
))

CHANGE_KW = frozenset((
    "meanwhile", "elsewhere", "new location", "scene change", "cut to",
    "montage", "journey", "travel", "years later", "next morning",
    "دریں اثنا",
))

NIGHT_KW = frozenset((
    "night", "dusk", "moon", "stars", "midnight", "رات", "چاند", "raat",
))


def _signal_text(slot):
    """Combined searchable text for one scene."""
    parts = (slot.purpose, slot.narration_segment, slot.label,
             slot.transition_intent, slot.continuity_notes)
    return " ".join(p for p in parts if p).lower()


def _hits(text, keywords):
    return any(k in text for k in keywords)


def _pick(options, key):
    """Deterministic choice from options (stable md5, no randomness)."""
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return options[int(h, 16) % len(options)]


class EditDecision:
    """One scene's editing decision. Immutable, Qt-free."""

    __slots__ = ("transition", "motion_preset", "hold_factor",
                 "grade_hint", "notes")

    def __init__(self, transition, motion_preset, hold_factor,
                 grade_hint, notes):
        assert transition in TRANSITIONS, transition
        assert motion_preset in MOTION_PRESETS, motion_preset
        self.transition = transition
        self.motion_preset = motion_preset
        self.hold_factor = float(hold_factor)
        self.grade_hint = str(grade_hint or "")
        self.notes = str(notes or "")

    def __eq__(self, other):
        return (isinstance(other, EditDecision)
                and (self.transition, self.motion_preset, self.hold_factor,
                     self.grade_hint, self.notes)
                == (other.transition, other.motion_preset, other.hold_factor,
                    other.grade_hint, other.notes))

    def __repr__(self):
        return ("EditDecision(transition=%r, motion_preset=%r, hold_factor=%.2f, "
                "grade_hint=%r)" % (self.transition, self.motion_preset,
                                   self.hold_factor, self.grade_hint))


def decide_edit(slot, prev_slot, next_slot, project=None):
    """Derive this scene's editing decision from its content + neighbours.

    slot/prev_slot/next_slot are timeline.Slot (neighbours may be None).
    project is accepted for future use (settings/grade presets) and ignored
    for now. Never raises on missing metadata — empty text falls through to
    the default rule.
    """
    text = _signal_text(slot)
    is_action = _hits(text, ACTION_KW)
    is_quiet = _hits(text, QUIET_KW) and not is_action  # action wins ties
    is_end = _hits(text, END_KW)
    is_first = prev_slot is None
    is_night = _hits(text, NIGHT_KW)
    key = slot.id + "|" + text[:200]

    # -- Rule 1: section end ---------------------------------------------
    if is_end:
        return EditDecision(
            "dip-to-black", "none", 1.2,
            "night-cold" if is_night else "cinematic",
            "Section end (%s) -> dip-to-black, hold 1.2x, no motion: "
            "let the moment land." % _matched(text, END_KW))

    # -- Rule 2: action ---------------------------------------------------
    if is_action:
        motion = "zoom-in"  # push-in energy
        return EditDecision(
            "cut", motion, 0.9, "war-desat",
            "Action beat (%s) -> hard cut, push-in, tighter pacing "
            "(hold 0.9x)." % _matched(text, ACTION_KW))

    # -- Rule 3: quiet / emotional ----------------------------------------
    if is_quiet:
        trans = "fade-in" if is_first else "xfade"
        motion = _pick(("none", "zoom-out"), "quiet|" + key)
        return EditDecision(
            trans, motion, 1.25,
            "night-cold" if is_night else "cinematic",
            "Quiet scene (%s) -> gentle %s, slow %s, longer hold "
            "(1.25x)." % (_matched(text, QUIET_KW), trans, motion))

    # -- Rule 4: scene/character change ------------------------------------
    changed_chars = (
        next_slot is not None
        and set(slot.characters) != set(next_slot.characters)
        and (slot.characters or next_slot.characters))
    if changed_chars or _hits(text, CHANGE_KW):
        why = "character change" if changed_chars else "location/time cue"
        return EditDecision(
            "xfade", "zoom-out", 1.0, "cinematic",
            "New moment (%s) -> gentle crossfade in." % why)

    # -- Rule 5: opening slot ----------------------------------------------
    if is_first:
        return EditDecision(
            "fade-in", "zoom-in", 1.0, "cinematic",
            "Opening scene -> fade in from black.")

    # -- Rule 6: continuation of the same moment ---------------------------
    if next_slot is not None:
        return EditDecision(
            "cut", "none", 1.0, "cinematic",
            "Same moment continues -> hard cut, restrained motion.")

    # -- Rule 7: default ----------------------------------------------------
    return EditDecision(
        "xfade", "zoom-in", 1.0, "cinematic",
        "Default -> gentle crossfade, mild push-in.")


def _matched(text, keywords):
    """First matching keyword (for human-readable notes)."""
    for k in sorted(keywords, key=len, reverse=True):
        if k in text:
            return k
    return "?"


def apply_editing_decisions(project):
    """Run decide_edit for every slot and store the results on the slots.

    Sets slot.transition, slot.motion_preset, slot.hold_factor,
    slot.grade_hint and slot.edit_notes (the Inspector's "why" line).
    Returns the list of (slot_id, EditDecision).
    """
    out = []
    slots = project.slots  # sorted by start_sec
    for i, slot in enumerate(slots):
        prev_slot = slots[i - 1] if i > 0 else None
        next_slot = slots[i + 1] if i + 1 < len(slots) else None
        d = decide_edit(slot, prev_slot, next_slot, project)
        slot.transition = d.transition
        slot.motion_preset = d.motion_preset
        slot.hold_factor = d.hold_factor
        slot.grade_hint = d.grade_hint
        slot.edit_notes = d.notes
        out.append((slot.id, d))
    return out
