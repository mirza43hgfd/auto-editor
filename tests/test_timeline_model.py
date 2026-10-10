"""M3 timeline-model tests. Qt-free: exercises the pure mutation logic in
auto_editor/timeline.py (reorder_slot, trim_slot, set_slot_field,
TimelineHistory) plus save/reopen round-trip of timeline edits.

Run:  .venv/bin/python tests/test_timeline_model.py
Exit 0 = all pass; non-zero = real failure.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from auto_editor.timeline import (
    Project, Slot, reorder_slot, trim_slot, set_slot_field,
    TimelineHistory, capture_timeline_state, restore_timeline_state,
    MIN_TRIM_DUR,
)

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  %s" % name)
    else:
        failed += 1
        print("  FAIL %s  %s" % (name, detail))


def make_project():
    p = Project("t")
    p.add_slot(Slot(0, 10, label="a", prompt="pa", transition="xfade"))
    p.add_slot(Slot(10, 25, label="b", prompt="pb", transition="cut"))
    p.add_slot(Slot(25, 40, label="c", prompt="pc", transition="fadeblack"))
    return p


def ids(p):
    return [s.id for s in p.slots]


# --- reorder ---------------------------------------------------------------
p = make_project()
a, b, c = ids(p)
check("reorder right moves middle slot one step",
      reorder_slot(p, b, 1) is True)
slots = p.slots
check("reorder right: slot b now last in time order",
      [s.id for s in slots] == [a, c, b], str([s.label for s in slots]))
sb = next(s for s in slots if s.id == b)
sc = next(s for s in slots if s.id == c)
check("reorder swaps timing windows, keeps fields attached",
      (sb.start_sec, sb.end_sec) == (25, 40) and sb.label == "b"
      and (sc.start_sec, sc.end_sec) == (10, 25) and sc.label == "c")

check("reorder at right end returns False", reorder_slot(p, b, 1) is False)
check("reorder at left end returns False", reorder_slot(p, a, -1) is False)
check("reorder unknown id returns False", reorder_slot(p, "nope", 1) is False)
check("reorder bad direction returns False", reorder_slot(p, a, 0) is False)

# --- trim clamping ----------------------------------------------------------
p = make_project()
a, b, c = ids(p)
ns, ne = trim_slot(p, b, -5, 20)          # negative start -> clamp to prev end (10)
check("trim clamps negative start to 0/prev-end", (ns, ne) == (10, 20),
      "got (%s, %s)" % (ns, ne))
ns, ne = trim_slot(p, b, 10, 99)          # end past next start -> clamp to 25
check("trim clamps end to next start (no overlap)", (ns, ne) == (10, 25),
      "got (%s, %s)" % (ns, ne))
ns, ne = trim_slot(p, a, 0, 0.1)          # below MIN_TRIM_DUR -> enforced
check("trim enforces min duration", ne - ns >= MIN_TRIM_DUR - 1e-9,
      "got (%s, %s)" % (ns, ne))
ns, ne = trim_slot(p, c, 25, 25.4)        # last slot, no next -> grows freely
check("trim last slot end unconstrained", (ns, ne) == (25, 25.5),
      "got (%s, %s)" % (ns, ne))
check("trim unknown id returns None", trim_slot(p, "nope", 0, 5) is None)

# no-room case: squeeze b between a and c so < MIN_TRIM_DUR fits
p2 = Project("tight")
p2.add_slot(Slot(0, 10, label="a"))
p2.add_slot(Slot(10, 10.2, label="b"))   # 0.2s window
p2.add_slot(Slot(10.2, 20, label="c"))
tb = ids(p2)[1]
before = (10.0, 10.2)
got = trim_slot(p2, tb, 10.05, 10.1)    # 0.05s requested < MIN_TRIM_DUR, no room
check("trim with no room leaves slot unchanged", got == before,
      "got %s" % (got,))

# --- set_slot_field ----------------------------------------------------------
def find_label(p, sid):
    return next(s.label for s in p.tracks["visuals"].slots if s.id == sid)


def find_trans(p, sid):
    return next(s.transition for s in p.tracks["visuals"].slots if s.id == sid)


p = make_project()
a = ids(p)[0]
check("set label", set_slot_field(p, a, "label", "hello")
      and find_label(p, a) == "hello")
check("set prompt", set_slot_field(p, a, "prompt", "p2")
      and p.tracks["visuals"].slots[0].prompt == "p2")
check("set valid transition", set_slot_field(p, a, "transition", "cut"))
check("reject invalid transition",
      set_slot_field(p, a, "transition", "star-wipe") is False
      and find_trans(p, a) == "cut")
check("reject invalid motion",
      set_slot_field(p, a, "motion_preset", "spin") is False)
check("reject unknown field", set_slot_field(p, a, "nope", "x") is False)
check("reject unknown id", set_slot_field(p, "nope", "label", "x") is False)


# --- undo / redo --------------------------------------------------------------
p = make_project()
a, b, c = ids(p)
h = TimelineHistory()
check("undo on empty stack is False", h.undo(p) is False)
check("redo on empty stack is False", h.redo(p) is False)

h.checkpoint(p)
reorder_slot(p, b, 1)
check("after reorder, order changed", ids(p) == [a, c, b])
check("undo restores order", h.undo(p) is True and ids(p) == [a, b, c])
check("redo re-applies", h.redo(p) is True and ids(p) == [a, c, b])
check("second undo works", h.undo(p) is True and ids(p) == [a, b, c])

h.checkpoint(p)
trim_slot(p, b, 12, 22)
sb = next(s for s in p.slots if s.id == b)
check("trim applied", (sb.start_sec, sb.end_sec) == (12, 22))
check("undo restores trim", h.undo(p) is True)
sb = next(s for s in p.slots if s.id == b)
check("trim undone", (sb.start_sec, sb.end_sec) == (10, 25),
      "got (%s, %s)" % (sb.start_sec, sb.end_sec))
# new mutation clears redo
h.checkpoint(p)
set_slot_field(p, a, "label", "zzz")
check("redo cleared after new checkpoint", h.redo(p) is False and h.can_redo() is False)
check("undo stack bounded", TimelineHistory(limit=2)._limit == 2)

# snapshot helpers round-trip
p = make_project()
st = capture_timeline_state(p)
p.tracks["visuals"].slots.pop()
check("capture/restore round-trip", len(p.slots) == 2)
restore_timeline_state(p, st)
check("restore brings back 3 slots with ids", ids(p) == [s["id"] for s in st])

# --- save / reopen round-trip ----------------------------------------------------
p = make_project()
a, b = ids(p)[:2]
hh = TimelineHistory()
hh.checkpoint(p)
reorder_slot(p, b, 1)
trim_slot(p, a, 0, 8)
set_slot_field(p, a, "transition", "fadeblack")
set_slot_field(p, a, "prompt", "desert at dusk")
with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "t.deproj")
    p.save(fp)
    q = Project.load(fp)
check("save/reopen keeps slot count", len(q.slots) == 3)
qa = next(s for s in q.slots if s.id == a)
check("save/reopen keeps trim", (qa.start_sec, qa.end_sec) == (0, 8))
check("save/reopen keeps transition+prompt",
      qa.transition == "fadeblack" and qa.prompt == "desert at dusk")
qb = next(s for s in q.slots if s.id == b)
check("save/reopen keeps reorder (timings)",
      (qb.start_sec, qb.end_sec) == (25, 40),
      "got (%s, %s)" % (qb.start_sec, qb.end_sec))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
