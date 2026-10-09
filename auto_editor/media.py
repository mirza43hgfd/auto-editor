"""Media import helpers: type detection, watch-folder scan (Flow workflow),
per-slot visual-prompt export."""

import os

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
CLIP_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def detect_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in CLIP_EXTS:
        return "clip"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"


def import_files(paths):
    """Filter to supported media files. Returns list of (path, type)."""
    out = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        t = detect_type(p)
        if t != "unknown":
            out.append((p, t))
    return out


def scan_watch_folder(folder, known_paths):
    """Return supported files in folder not already in known_paths.

    Flow workflow: user generates photos in Flow, saves them into the watch
    folder; the app picks them up automatically (called on a timer / on focus).
    """
    known = {os.path.abspath(p) for p in known_paths}
    new_files = []
    if not folder or not os.path.isdir(folder):
        return new_files
    for name in sorted(os.listdir(folder)):
        full = os.path.abspath(os.path.join(folder, name))
        if full in known or not os.path.isfile(full):
            continue
        if detect_type(full) != "unknown":
            new_files.append(full)
    return new_files


def export_slot_prompts(project, out_md):
    """Write per-slot copy-paste visual prompts (Flow workflow) to markdown."""
    lines = ["# Visual prompts — %s" % project.name, ""]
    for i, slot in enumerate(project.slots, 1):
        lines.append("## Slot %d — %.1fs to %.1fs" % (i, slot.start_sec, slot.end_sec))
        if slot.label:
            lines.append("Narration: %s" % slot.label)
        lines.append("")
        lines.append("```")
        lines.append(slot.prompt or "(no prompt yet — use AI suggest or type one)")
        lines.append("```")
        lines.append("")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_md
