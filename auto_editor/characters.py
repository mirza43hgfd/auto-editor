"""Character reference library + scene continuity checks.

Qt-free / headless-safe: pure data model plus continuity analysis. The
workspace panel (workspace.py) only wires buttons to these helpers.

A Character is one consistent person/creature in a project:
  - stable id, name, description
  - ordered reference image paths (index 0 = primary)
  - visual traits (face, hairstyle, age_range, clothing, distinctive_features)
  - scene_appearances: slot ids this character is attached to

CharacterLibrary owns one project's characters and serializes inside
Project.to_dict/from_dict. It can also be exported/imported as standalone
JSON so characters are reusable across projects.

Honest limit: reference-image conditioning improves consistency a lot but
does not guarantee perfect identity — check_continuity() surfaces problems
instead of hiding them.
"""

import json
import os
import uuid


def _new_id():
    return uuid.uuid4().hex[:8]


class Character:
    """One consistent character with reference images and visual traits."""

    def __init__(self, name, description="", face="", hairstyle="",
                 age_range="", clothing="", distinctive_features=""):
        self.id = _new_id()
        self.name = str(name or "Unnamed")
        self.description = str(description or "")
        self.face = str(face or "")
        self.hairstyle = str(hairstyle or "")
        self.age_range = str(age_range or "")
        self.clothing = str(clothing or "")
        self.distinctive_features = str(distinctive_features or "")
        self.reference_paths = []    # ordered; [0] is the primary reference
        self.scene_appearances = []  # slot ids this character appears in

    @property
    def primary_reference(self):
        """Primary reference image path, or '' when none set."""
        return self.reference_paths[0] if self.reference_paths else ""

    def add_reference(self, path, make_primary=False):
        """Add a reference image. make_primary=True puts it first.

        Adding a path that is already present moves it (no duplicates).
        Returns True on success, False for an empty path.
        """
        path = str(path or "")
        if not path:
            return False
        if path in self.reference_paths:
            self.reference_paths.remove(path)
        if make_primary:
            self.reference_paths.insert(0, path)
        else:
            self.reference_paths.append(path)
        return True

    def remove_reference(self, path):
        """Remove a reference image. Returns False if it was not present."""
        if path in self.reference_paths:
            self.reference_paths.remove(path)
            return True
        return False

    def set_primary(self, path):
        """Make an already-added reference the primary one.

        Returns False (and changes nothing) when the path is not a known
        reference — the UI disables the button in that case.
        """
        if path not in self.reference_paths:
            return False
        self.reference_paths.remove(path)
        self.reference_paths.insert(0, path)
        return True

    # -- serialization ----------------------------------------------------
    def to_dict(self):
        return {
            "id": self.id, "name": self.name,
            "description": self.description, "face": self.face,
            "hairstyle": self.hairstyle, "age_range": self.age_range,
            "clothing": self.clothing,
            "distinctive_features": self.distinctive_features,
            "reference_paths": list(self.reference_paths),
            "scene_appearances": list(self.scene_appearances),
        }

    @classmethod
    def from_dict(cls, d):
        c = cls(d.get("name", "Unnamed"),
                description=d.get("description", ""),
                face=d.get("face", ""), hairstyle=d.get("hairstyle", ""),
                age_range=d.get("age_range", ""),
                clothing=d.get("clothing", ""),
                distinctive_features=d.get("distinctive_features", ""))
        c.id = d.get("id", c.id)
        c.reference_paths = [str(p) for p in d.get("reference_paths", [])]
        c.scene_appearances = [str(s) for s in d.get("scene_appearances", [])]
        return c


class CharacterLibrary:
    """The character cast of one project."""

    def __init__(self):
        self.characters = []  # Character, in insertion order

    # -- CRUD ---------------------------------------------------------------
    def add(self, character):
        self.characters.append(character)
        return character

    def remove(self, character_id):
        for i, c in enumerate(self.characters):
            if c.id == character_id:
                del self.characters[i]
                return True
        return False

    def get(self, character_id):
        for c in self.characters:
            if c.id == character_id:
                return c
        return None

    def names(self):
        return [c.name for c in self.characters]

    def __len__(self):
        return len(self.characters)

    # -- cross-project reuse --------------------------------------------------
    def merge(self, other):
        """Import another library's characters. Id collisions get fresh ids.

        Returns the list of added Character objects.
        """
        added = []
        known = {c.id for c in self.characters}
        for c in other.characters:
            if c.id in known:
                c.id = _new_id()
            known.add(c.id)
            self.characters.append(c)
            added.append(c)
        return added

    # -- serialization --------------------------------------------------------
    def to_dict(self):
        return {"characters": [c.to_dict() for c in self.characters]}

    @classmethod
    def from_dict(cls, d):
        lib = cls()
        for cd in (d or {}).get("characters", []):
            lib.characters.append(Character.from_dict(cd))
        return lib

    def export_json(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def import_json(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def build_character_lock(character):
    """Locked visual-description block for image prompts.

    Concise, stable trait list — passed alongside the primary reference
    image so every generation of this character stays close to the same
    look. Empty traits are skipped, never rendered as blank lines.
    """
    bits = []
    if character.description:
        bits.append(character.description)
    for label, val in (("Face", character.face),
                       ("Hair", character.hairstyle),
                       ("Age", character.age_range),
                       ("Clothing", character.clothing),
                       ("Distinctive features", character.distinctive_features)):
        if val:
            bits.append("%s: %s" % (label, val))
    detail = "; ".join(bits)
    lock = "CHARACTER LOCK — %s: %s." % (character.name, detail) if detail \
        else "CHARACTER LOCK — %s." % character.name
    return (lock + " Keep this exact look in every shot: same face, same "
            "clothing, same proportions. Match the reference image.")


# ---------------------------------------------------------------------------
# Slot <-> character attachment (Qt-free; operates on a timeline.Project)
# ---------------------------------------------------------------------------

def attach_character(project, slot_id, character_id):
    """Attach a library character to a slot. Syncs both sides.

    Returns True on success; False when the slot or character does not
    exist. Attaching twice is a no-op (still True, no duplicates).
    """
    from .timeline import find_slot  # local import: avoids a hard cycle
    char = project.characters.get(character_id)
    slot = find_slot(project, slot_id)
    if char is None or slot is None:
        return False
    if character_id not in slot.characters:
        slot.characters.append(character_id)
    if slot_id not in char.scene_appearances:
        char.scene_appearances.append(slot_id)
    return True


def detach_character(project, slot_id, character_id):
    """Detach a character from a slot. Syncs both sides.

    Returns True when something was detached, False when the slot/character
    does not exist or they were not attached.
    """
    from .timeline import find_slot
    char = project.characters.get(character_id)
    slot = find_slot(project, slot_id)
    if char is None or slot is None:
        return False
    changed = False
    if character_id in slot.characters:
        slot.characters.remove(character_id)
        changed = True
    if slot_id in char.scene_appearances:
        char.scene_appearances.remove(slot_id)
        changed = True
    return changed


def detach_character_everywhere(project, character_id):
    """Remove a character from every slot (used when deleting a character)."""
    from .timeline import find_slot
    char = project.characters.get(character_id)
    if char is None:
        return 0
    n = 0
    for sid in list(char.scene_appearances):
        slot = find_slot(project, sid)
        if slot is not None and character_id in slot.characters:
            slot.characters.remove(character_id)
            n += 1
    char.scene_appearances = []
    return n


# ---------------------------------------------------------------------------
# Continuity checks — honest, non-blocking warnings for the storyboard panel
# ---------------------------------------------------------------------------

def check_continuity(project):
    """Analyse character/scene consistency. Returns a list of dicts:

        {"level": "warning"|"info", "rule": str, "scene": int|None,
         "slot_id": str, "character_id": str, "character_name": str,
         "message": str}

    Rules:
      unknown-character  — slot references a character id not in the library
      no-reference       — attached character has no reference images
      conflicting-notes  — same character, differing continuity_notes across scenes
      missing-prompt     — scene has no visual prompt
      no-introduction    — character first appears mid-story without being
                           named in any earlier scene's narration/label
      unused-character   — (info) library character appears in no scene
    """
    out = []
    slots = project.slots  # sorted by start_sec
    lib = project.characters

    def warn(rule, message, scene=None, slot_id="", character_id="",
             character_name=""):
        out.append({"level": "warning", "rule": rule, "scene": scene,
                    "slot_id": slot_id, "character_id": character_id,
                    "character_name": character_name, "message": message})

    # per-scene checks
    for i, s in enumerate(slots, start=1):
        if not (s.visual_prompt or "").strip():
            warn("missing-prompt",
                 "Scene #%d has no visual prompt — generation would be unguided." % i,
                 scene=i, slot_id=s.id)
        for cid in s.characters:
            char = lib.get(cid)
            if char is None:
                warn("unknown-character",
                     "Scene #%d references unknown character id '%s' — attach a "
                     "real library character instead." % (i, cid),
                     scene=i, slot_id=s.id, character_id=cid)
                continue
            if not char.reference_paths:
                warn("no-reference",
                     "Scene #%d: character '%s' has no reference images — "
                     "generated look will drift." % (i, char.name),
                     scene=i, slot_id=s.id, character_id=cid,
                     character_name=char.name)

    # conflicting continuity notes per character
    for char in lib.characters:
        notes = {}  # note text -> first scene index
        for i, s in enumerate(slots, start=1):
            if char.id in s.characters and (s.continuity_notes or "").strip():
                notes.setdefault(s.continuity_notes.strip(), i)
        if len(notes) > 1:
            scenes = sorted(notes.values())
            warn("conflicting-notes",
                 "Character '%s' has conflicting continuity notes across scenes "
                 "%s — pick one look." % (char.name, ", ".join("#%d" % n for n in scenes)),
                 scene=scenes[0], slot_id="", character_id=char.id,
                 character_name=char.name)

    # no prior introduction: first appearance mid-story, never named earlier
    for char in lib.characters:
        first = None
        for i, s in enumerate(slots, start=1):
            if char.id in s.characters:
                first = i
                break
        if first is not None and first > 1:
            earlier_text = " ".join(
                "%s %s" % (s.narration_segment or "", s.label or "")
                for s in slots[:first - 1]).lower()
            if char.name.lower() not in earlier_text:
                warn("no-introduction",
                     "Character '%s' first appears in scene #%d but is not named "
                     "in any earlier scene — consider an introduction." % (char.name, first),
                     scene=first, slot_id=slots[first - 1].id,
                     character_id=char.id, character_name=char.name)

    # unused characters (info, not a warning)
    for char in lib.characters:
        if not any(char.id in s.characters for s in slots):
            out.append({"level": "info", "rule": "unused-character",
                        "scene": None, "slot_id": "", "character_id": char.id,
                        "character_name": char.name,
                        "message": "Character '%s' is in the library but appears "
                                   "in no scene." % char.name})
    return out


def scene_summary(project):
    """One-line-per-scene continuity overview (for logs / tooltips)."""
    lines = []
    for i, s in enumerate(project.slots, start=1):
        names = [project.characters.get(c).name
                 for c in s.characters if project.characters.get(c)]
        lines.append("#%d %s chars=[%s] prompt=%s" % (
            i, s.label.strip()[:40] or "(no label)", ", ".join(names) or "-",
            "yes" if (s.visual_prompt or "").strip() else "MISSING"))
    return lines
