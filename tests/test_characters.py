"""M4 character-library + scene-continuity tests. Qt-free.

Covers: Character/Library CRUD, reference ordering, attach/detach sync,
export/import round-trip, scene metadata save/reopen, backward-compatible
load of pre-M4 projects, continuity rules (each), per-scene regenerate with
a MOCKED ai_helper.generate_image (no network, no API key).

Run:  .venv/bin/python tests/test_characters.py
Exit 0 = all pass; non-zero = real failure.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import Project, Slot, find_slot, set_slot_field
from auto_editor.characters import (
    Character, CharacterLibrary, build_character_lock,
    attach_character, detach_character, detach_character_everywhere,
    check_continuity,
)
from auto_editor import auto_pipeline
from auto_editor import ai_helper

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  %s" % name)
    else:
        failed += 1
        print("  FAIL %s  %s" % (name, detail))


def make_cast_project():
    """Project with 3 scenes and 2 characters attached to scenes 1-2."""
    p = Project("cast")
    p.add_slot(Slot(0, 10, label="Khalid rides out", visual_prompt="desert dawn",
                    narration_segment="Khalid rides out with Zara at his side."))
    p.add_slot(Slot(10, 20, label="The battle", visual_prompt="battle clash"))
    p.add_slot(Slot(20, 30, label="Quiet aftermath", visual_prompt="sunset field"))
    khalid = Character("Khalid", description="Arab commander",
                       clothing="dark steel armor")
    khalid.add_reference("/ref/khalid_front.png", make_primary=True)
    khalid.add_reference("/ref/khalid_side.png")
    zara = Character("Zara", description="Scout")
    zara.add_reference("/ref/zara.png", make_primary=True)
    p.characters.add(khalid)
    p.characters.add(zara)
    slots = p.slots
    attach_character(p, slots[0].id, khalid.id)
    attach_character(p, slots[1].id, khalid.id)
    attach_character(p, slots[1].id, zara.id)
    return p, khalid, zara


# ---------------------------------------------------------------- character

c = Character("Khalid", description="commander", clothing="steel armor")
check("character stable id", len(c.id) == 8)
check("character defaults empty", c.face == "" and c.reference_paths == []
      and c.scene_appearances == [])

lib = CharacterLibrary()
lib.add(c)
check("library add/get", lib.get(c.id) is c)
check("library get unknown -> None", lib.get("nope") is None)
check("library remove unknown -> False", lib.remove("nope") is False)
check("library remove", lib.remove(c.id) is True and len(lib) == 0)

# reference ordering
r = Character("R")
check("add_reference empty -> False", r.add_reference("") is False)
r.add_reference("/a.png")
r.add_reference("/b.png")
check("reference order append", r.reference_paths == ["/a.png", "/b.png"])
check("primary is first", r.primary_reference == "/a.png")
r.add_reference("/c.png", make_primary=True)
check("make_primary inserts first",
      r.reference_paths == ["/c.png", "/a.png", "/b.png"])
check("re-add moves, no duplicates",
      r.add_reference("/a.png") is True
      and r.reference_paths == ["/c.png", "/b.png", "/a.png"])
check("set_primary existing", r.set_primary("/b.png") is True
      and r.primary_reference == "/b.png")
check("set_primary unknown -> False, unchanged",
      r.set_primary("/zzz.png") is False
      and r.primary_reference == "/b.png")
check("remove_reference", r.remove_reference("/b.png") is True
      and "/b.png" not in r.reference_paths)
check("remove_reference unknown -> False", r.remove_reference("/zzz") is False)
check("primary empty when no refs", Character("E").primary_reference == "")

# ---------------------------------------------------------------- attach

p = Project("a")
p.add_slot(Slot(0, 5, label="s1", visual_prompt="vp1"))
p.add_slot(Slot(5, 10, label="s2", visual_prompt="vp2"))
ch = Character("K", clothing="armor")
ch.add_reference("/ref/k.png", make_primary=True)
p.characters.add(ch)
s1, s2 = p.slots

check("attach ok", attach_character(p, s1.id, ch.id) is True)
check("attach syncs slot", s1.characters == [ch.id])
check("attach syncs appearances", ch.scene_appearances == [s1.id])
check("double attach no dup",
      attach_character(p, s1.id, ch.id) is True
      and s1.characters == [ch.id] and ch.scene_appearances == [s1.id])
check("attach unknown slot -> False", attach_character(p, "nope", ch.id) is False)
check("attach unknown char -> False", attach_character(p, s1.id, "nope") is False)
check("detach ok", detach_character(p, s1.id, ch.id) is True)
check("detach syncs both", s1.characters == [] and ch.scene_appearances == [])
check("detach not-attached -> False",
      detach_character(p, s1.id, ch.id) is False)
attach_character(p, s1.id, ch.id)
attach_character(p, s2.id, ch.id)
check("detach everywhere",
      detach_character_everywhere(p, ch.id) == 2
      and s1.characters == [] and s2.characters == []
      and ch.scene_appearances == [])

# ---------------------------------------------------------------- lock block

lock = build_character_lock(ch)
check("lock has name", "K" in lock)
check("lock has filled trait", "armor" in lock)
check("lock skips empty traits", "Face:" not in lock and "Hair:" not in lock)
check("lock instructs consistency", "reference image" in lock.lower())
full = Character("F", description="d", face="scarred", hairstyle="black",
                 age_range="40s", clothing="robe",
                 distinctive_features="gold ring")
lock2 = build_character_lock(full)
check("lock includes all filled traits",
      all(k in lock2 for k in ("scarred", "black", "40s", "robe", "gold ring")))

# ---------------------------------------------------------------- export/import

with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "cast.json")
    lib2 = CharacterLibrary()
    lib2.add(Character("A", description="da", clothing="ca"))
    lib2.characters[0].add_reference("/r/a.png", make_primary=True)
    lib2.export_json(fp)
    lib3 = CharacterLibrary.import_json(fp)
    check("export/import round-trip count", len(lib3) == 1)
    rc = lib3.characters[0]
    check("export/import keeps fields",
          rc.name == "A" and rc.description == "da" and rc.clothing == "ca"
          and rc.primary_reference == "/r/a.png")
    check("export/import keeps id", rc.id == lib2.characters[0].id)

# fresh in-memory collision test
l1, l2 = CharacterLibrary(), CharacterLibrary()
ca = Character("Same")
l1.add(ca)
cb = Character("Same")
cb.id = ca.id  # force collision
l2.add(cb)
added = l1.merge(l2)
check("merge regenerates colliding id",
      len(added) == 1 and added[0].id != ca.id and len(l1) == 2)

# ---------------------------------------------------------------- save/reopen

p, khalid, zara = make_cast_project()
s0 = p.slots[0]
s0.purpose = "establish hero"
s0.narration_segment = "Khalid rides out at dawn."
s0.assets = ["/assets/banner.png"]
s0.transition_intent = "gentle fade"
s0.continuity_notes = "same armor as reference"
with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "t.deproj")
    p.save(fp)
    q = Project.load(fp)
check("save/reopen keeps character count", len(q.characters) == 2)
qk = q.characters.get(khalid.id)
check("save/reopen keeps character fields",
      qk is not None and qk.name == "Khalid"
      and qk.primary_reference == "/ref/khalid_front.png"
      and qk.scene_appearances == khalid.scene_appearances)
qs0 = q.slots[0]
check("save/reopen keeps slot characters", qs0.characters == [khalid.id])
check("save/reopen keeps scene metadata",
      qs0.purpose == "establish hero"
      and qs0.narration_segment == "Khalid rides out at dawn."
      and qs0.assets == ["/assets/banner.png"]
      and qs0.transition_intent == "gentle fade"
      and qs0.continuity_notes == "same armor as reference"
      and qs0.visual_prompt == "desert dawn" and qs0.prompt == "desert dawn")

# prompt / visual_prompt alias
sa = Slot(0, 1, prompt="legacy text")
check("prompt kwarg fills visual_prompt", sa.visual_prompt == "legacy text")
sb = Slot(0, 1, prompt="legacy", visual_prompt="new wins")
check("visual_prompt wins over prompt", sb.visual_prompt == "new wins"
      and sb.prompt == "new wins")
set_slot_field(p, s0.id, "prompt", "typed")
check("set_slot_field prompt syncs alias",
      s0.visual_prompt == "typed" and s0.prompt == "typed")
set_slot_field(p, s0.id, "purpose", "p2")
check("set_slot_field new metadata field", s0.purpose == "p2")

# backward compat: pre-M4 project files
old_slot = {"id": "abc", "start_sec": 0, "end_sec": 5, "prompt": "old prompt",
            "label": "l"}
so = Slot.from_dict(old_slot)
check("old slot dict loads", so.visual_prompt == "old prompt"
      and so.purpose == "" and so.characters == [] and so.assets == [])
old_proj = {"name": "old", "tracks": {"visuals": {"kind": "visuals",
            "slots": [old_slot], "sfx_events": []}}}
po = Project.from_dict(old_proj)
check("old project dict -> empty library", len(po.characters) == 0
      and po.slots[0].visual_prompt == "old prompt")

# contract round-trip keeps characters + metadata
cd = p.to_contract_dict()
p3 = Project.from_contract_dict(cd)
check("contract keeps characters", len(p3.characters) == 2
      and p3.characters.get(khalid.id).name == "Khalid")
check("contract keeps slot metadata",
      p3.slots[0].characters == [khalid.id]
      and p3.slots[0].continuity_notes == "same armor as reference")

# ---------------------------------------------------------------- continuity

def rules(warns):
    return [w["rule"] for w in warns]

p, khalid, zara = make_cast_project()
check("clean project: no warnings", check_continuity(p) == [],
      "got %s" % rules(check_continuity(p)))

# no-reference: strip zara's refs (attached to scene 2)
zara.reference_paths = []
w = check_continuity(p)
check("no-reference warning",
      "no-reference" in rules(w)
      and any("Zara" in x["message"] for x in w if x["rule"] == "no-reference"))

# unknown character id on a slot
p.slots[2].characters.append("ghost-id")
w = check_continuity(p)
check("unknown-character warning", "unknown-character" in rules(w))

# conflicting continuity notes
p.slots[0].continuity_notes = "steel armor"
p.slots[1].continuity_notes = "leather armor"
w = check_continuity(p)
check("conflicting-notes warning",
      "conflicting-notes" in rules(w)
      and any("Khalid" in x["message"] for x in w
              if x["rule"] == "conflicting-notes"))
# same notes -> no warning
p.slots[1].continuity_notes = "steel armor"
w = check_continuity(p)
check("identical notes: no conflict",
      "conflicting-notes" not in rules(w))

# missing visual prompt
p.slots[2].visual_prompt = ""
w = check_continuity(p)
check("missing-prompt warning",
      any(x["rule"] == "missing-prompt" and x["scene"] == 3 for x in w))

# no prior introduction: newcomer first appears in scene 3, never named before
newc = Character("Rashid", description="villager")
newc.add_reference("/ref/r.png", make_primary=True)
p.characters.add(newc)
attach_character(p, p.slots[2].id, newc.id)
w = check_continuity(p)
check("no-introduction warning",
      any(x["rule"] == "no-introduction" and x["character_name"] == "Rashid"
          for x in w))
# negative: name mentioned in an earlier scene -> no warning
p.slots[0].narration_segment += " Rashid watches from the hills."
w = check_continuity(p)
check("named earlier: no no-introduction",
      not any(x["rule"] == "no-introduction" for x in w))

# unused character -> info, not warning
extra = Character("Extra")
p.characters.add(extra)
w = check_continuity(p)
infos = [x for x in w if x["level"] == "info"]
check("unused character is info-level",
      any(x["rule"] == "unused-character" and x["character_name"] == "Extra"
          for x in infos))

# ---------------------------------------------------------------- per-scene regen (mocked)

calls = []
real_gen = ai_helper.generate_image


def fake_generate_image(prompt, out_path, api_key=None,
                        reference_image_path=None, use_cache=True):
    calls.append({"prompt": prompt, "out_path": out_path,
                  "api_key": api_key,
                  "reference_image_path": reference_image_path})
    with open(out_path, "wb") as f:
        f.write(b"fake-png-bytes")
    return True, out_path


ai_helper.generate_image = fake_generate_image
try:
    with tempfile.TemporaryDirectory() as td:
        media = os.path.join(td, "media")
        os.makedirs(media)
        p, khalid, zara = make_cast_project()
        target = p.slots[1]
        target.visual_path = os.path.join(media, "slot_002.png")
        before = p.slots[0].visual_path  # "" — untouched

        ok, res = auto_pipeline.generate_scene_visual(p, target.id, td)
        check("regen ok", ok is True, res)
        check("regen calls generate_image exactly once", len(calls) == 1,
              "calls=%d" % len(calls))
        call = calls[0]
        check("regen passes primary reference",
              call["reference_image_path"] == "/ref/khalid_front.png",
              "got %r" % call["reference_image_path"])
        check("regen prompt has character lock",
              "CHARACTER LOCK" in call["prompt"] and "Khalid" in call["prompt"])
        check("regen prompt has scene prompt", "battle clash" in call["prompt"])
        check("regen updates slot visual_path",
              target.visual_path == os.path.join(media, "slot_002.png"))
        check("regen writes file", os.path.isfile(target.visual_path))
        check("regen leaves other scenes alone",
              p.slots[0].visual_path == before and len(calls) == 1)

        # no character attached -> no reference image passed
        calls.clear()
        plain = p.slots[2]
        plain.visual_path = os.path.join(media, "slot_003.png")
        ok, _ = auto_pipeline.generate_scene_visual(p, plain.id, td)
        check("regen without character: no reference",
              ok and calls[0]["reference_image_path"] is None
              and calls[0]["prompt"] == "sunset field",
              "got %r" % (calls[0],))

        # unknown slot -> honest failure, zero calls
        calls.clear()
        ok, msg = auto_pipeline.generate_scene_visual(p, "nope", td)
        check("regen unknown slot fails cleanly",
              ok is False and len(calls) == 0, msg)

        # build_scene_prompt skips dangling ids, uses first real character
        p.slots[0].characters.insert(0, "ghost")
        prompt, ref = auto_pipeline.build_scene_prompt(p, p.slots[0])
        check("scene prompt skips dangling character",
              ref == "/ref/khalid_front.png" and "Khalid" in prompt)
finally:
    ai_helper.generate_image = real_gen

check("mock restored", ai_helper.generate_image is real_gen)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
