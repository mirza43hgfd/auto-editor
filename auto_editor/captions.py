"""Caption engine — pure Python, no Qt (headless-safe, unit-testable).

Cues are generated from each slot's narration text (narration_segment,
falling back to the slot label). Timing model, stated honestly:

* We do NOT have word-level ASR timestamps. Each slot's narration text is
  split into phrase-level cues at sentence/punctuation boundaries, and the
  slot's time window is distributed across phrases PROPORTIONALLY BY
  CHARACTER COUNT. This is an approximation: it assumes roughly constant
  speaking rate inside one slot. It is good enough for readable captions,
  and every cue is user-editable (shift/split/merge/edit) when the
  approximation is off.

Burn-in uses a generated ASS file (libass `subtitles` filter) so font,
color, outline, background box and position are real styling — not a
hardcoded look. A plain .srt is always exported next to the MP4.
"""

import hashlib
import re
import uuid

# ---------------------------------------------------------------------------
# cue model
# ---------------------------------------------------------------------------

MIN_CUE_DUR = 0.4   # seconds; shorter cues are unreadable
GAP_WARN_SEC = 0.75  # a silence gap longer than this is flagged (warning)
WRAP_CHARS = 42     # caption line width target for ASS output


class CaptionCue:
    """One caption cue: [start, end) seconds + text."""

    def __init__(self, start, end, text="", cue_id=None):
        try:
            start = float(start)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(end)
        except (TypeError, ValueError):
            end = start
        self.id = cue_id or uuid.uuid4().hex[:8]
        self.start_sec = max(0.0, start)
        # end must be after start; keep at least a sliver so a cue never inverts
        self.end_sec = max(self.start_sec + 0.1, end)
        self.text = str(text or "")

    @property
    def duration(self):
        return max(0.0, self.end_sec - self.start_sec)

    def to_dict(self):
        return {"id": self.id, "start": round(self.start_sec, 3),
                "end": round(self.end_sec, 3), "text": self.text}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(d.get("start", 0), d.get("end", 0),
                   d.get("text", ""), cue_id=d.get("id"))

    def __repr__(self):
        return "CaptionCue(%s, %.2f-%.2f, %r)" % (
            self.id, self.start_sec, self.end_sec, self.text[:24])


# ---------------------------------------------------------------------------
# phrase splitting + cue generation
# ---------------------------------------------------------------------------

# Sentence-ish boundaries: Latin + Urdu/Arabic punctuation + newlines.
# The delimiter stays attached to the preceding phrase.
_SPLIT_RE = re.compile(r"(.+?[.!?\u06d4\u061f\u2026\u3002\n]+)(?=\s|$)", re.S)
_WEAK_SPLIT_RE = re.compile(r"(.+?[,;\u060c:\u2014\u2013]+)(?=\s|$)")

# Fragments shorter than this get merged into the next phrase (avoids
# one-word cues like "Aur" floating alone).
_MIN_PHRASE_CHARS = 12


def split_into_phrases(text):
    """Split narration text into caption-sized phrases.

    Returns a list of non-empty strings. Pure function.
    """
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not text:
        return []
    phrases = []
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in _SPLIT_RE.findall(chunk + "\n")]
        rest = _SPLIT_RE.sub("", chunk + "\n").strip()
        if rest:
            parts.append(rest)
        # long phrases: also split at weak boundaries (comma etc.)
        out = []
        for p in parts:
            if len(p) > 90:
                subs = [s.strip() for s in _WEAK_SPLIT_RE.findall(p + " ") if s.strip()]
                tail = _WEAK_SPLIT_RE.sub("", p + " ").strip()
                if tail:
                    subs.append(tail)
                out.extend(subs if subs else [p])
            else:
                out.append(p)
        phrases.extend(x for x in out if x)
    # merge tiny fragments forward (last one merges backward)
    merged = []
    for p in phrases:
        if merged and len(merged[-1]) < _MIN_PHRASE_CHARS:
            merged[-1] = (merged[-1] + " " + p).strip()
        else:
            merged.append(p)
    if len(merged) > 1 and len(merged[-1]) < _MIN_PHRASE_CHARS:
        # NOTE: pop() first — indexing merged[-2] in the same expression as
        # pop() would rebind the index AFTER the list shrank (silent
        # corruption with 3+ items, IndexError with exactly 2).
        last = merged.pop()
        merged[-1] = (merged[-1] + " " + last).strip()
    return merged


def generate_cues(project, max_cues_per_slot=12):
    """Build cues for every slot from its narration text.

    `project` is duck-typed: needs `.slots` with start_sec/end_sec,
    narration_segment and label. Slot windows are distributed across
    phrases proportionally by character count (see module docstring).
    The final cue of each slot snaps exactly to the slot end so the
    captions cover the whole narration window.
    """
    cues = []
    for s in sorted(project.slots, key=lambda x: x.start_sec):
        window = max(0.0, s.end_sec - s.start_sec)
        if window <= 0:
            continue
        text = (getattr(s, "narration_segment", "") or
                getattr(s, "label", "") or "").strip()
        if not text:
            continue
        phrases = split_into_phrases(text)[:max_cues_per_slot] or [text]
        weights = [max(len(p), 1) for p in phrases]
        total_w = float(sum(weights)) or 1.0
        t = s.start_sec
        for i, phrase in enumerate(phrases):
            if i == len(phrases) - 1:
                end = s.end_sec  # snap: covers the full window
            else:
                end = t + max(MIN_CUE_DUR, window * weights[i] / total_w)
                end = min(end, s.end_sec - MIN_CUE_DUR * (len(phrases) - i - 1))
            if end - t < 0.2:
                break  # no room left in this slot; stop honestly
            cues.append(CaptionCue(t, end, phrase))
            t = end
    return cues


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_cues(project, cues):
    """Check cues against the project. Returns a list of issue dicts:

    {"severity": "error"|"warning", "kind": ..., "cue_id": ..., "message": ...}
    Kinds: empty, bad_duration, overlap, gap, out_of_bounds, outside_slots.
    An empty list means clean.
    """
    issues = []
    cues = sorted(cues, key=lambda c: c.start_sec)
    try:
        total = float(project.total_duration)
    except (TypeError, ValueError):
        total = 0.0
    slots = sorted(project.slots, key=lambda s: s.start_sec)
    prev = None
    for c in cues:
        if not c.text.strip():
            issues.append({"severity": "error", "kind": "empty",
                           "cue_id": c.id,
                           "message": "Cue %s ka text khaali hai." % c.id})
        if c.end_sec - c.start_sec < 0.2:
            issues.append({"severity": "error", "kind": "bad_duration",
                           "cue_id": c.id,
                           "message": "Cue %s ki duration ghalat hai (%.2fs)." %
                                      (c.id, c.duration)})
        if c.start_sec < -0.01 or (total > 0 and c.end_sec > total + 0.01):
            issues.append({"severity": "error", "kind": "out_of_bounds",
                           "cue_id": c.id,
                           "message": "Cue %s project duration se bahar hai." % c.id})
        if slots and not any(s.start_sec - 0.01 <= c.start_sec and
                             c.end_sec <= s.end_sec + 0.01 for s in slots):
            issues.append({"severity": "warning", "kind": "outside_slots",
                           "cue_id": c.id,
                           "message": "Cue %s kisi slot ke andar poori tarah nahi hai." % c.id})
        if prev is not None:
            if c.start_sec < prev.end_sec - 0.01:
                issues.append({"severity": "error", "kind": "overlap",
                               "cue_id": c.id,
                               "message": "Cue %s pichle cue se takra raha hai." % c.id})
            elif c.start_sec - prev.end_sec > GAP_WARN_SEC:
                issues.append({"severity": "warning", "kind": "gap",
                               "cue_id": c.id,
                               "message": "Cue %s se pehle %.1fs ka khaali waqfa hai." %
                                          (c.id, c.start_sec - prev.end_sec)})
        prev = c
    return issues


# ---------------------------------------------------------------------------
# edit operations (pure functions)
# ---------------------------------------------------------------------------

def shift_cue(cue, delta):
    """Return a new cue moved by delta seconds.

    Start clamps at 0; duration is preserved (the end moves with the start)
    so a cue never collapses when shifted past zero.
    """
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        delta = 0.0
    dur = cue.duration
    start = max(0.0, cue.start_sec + delta)
    return CaptionCue(start, start + dur, cue.text, cue_id=cue.id)


def split_cue(cue, at_sec):
    """Split a cue into two at at_sec. Text splits at the word nearest the
    time fraction. Returns (first, second) or (cue, None) if invalid."""
    try:
        at = float(at_sec)
    except (TypeError, ValueError):
        return cue, None
    if not (cue.start_sec + 0.2 < at < cue.end_sec - 0.2):
        return cue, None
    words = cue.text.split()
    if len(words) < 2:
        return cue, None
    frac = (at - cue.start_sec) / max(0.01, cue.duration)
    cut = min(len(words) - 1, max(1, int(round(len(words) * frac))))
    first = CaptionCue(cue.start_sec, at, " ".join(words[:cut]))
    second = CaptionCue(at, cue.end_sec, " ".join(words[cut:]))
    return first, second


def merge_cues(a, b):
    """Merge two cues into one spanning both; texts joined with a space."""
    first, second = (a, b) if a.start_sec <= b.start_sec else (b, a)
    text = (first.text.strip() + " " + second.text.strip()).strip()
    return CaptionCue(first.start_sec, max(first.end_sec, second.end_sec), text)


def edit_cue(cue, text=None, start=None, end=None):
    """Return a new cue with edited fields (validated/clamped)."""
    t = cue.text if text is None else str(text)
    s = cue.start_sec if start is None else start
    e = cue.end_sec if end is None else end
    return CaptionCue(s, e, t, cue_id=cue.id)


# ---------------------------------------------------------------------------
# SRT
# ---------------------------------------------------------------------------

def _srt_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def to_srt(cues):
    """Serialize cues to SubRip (.srt) text."""
    out = []
    for i, c in enumerate(sorted(cues, key=lambda x: x.start_sec), 1):
        out.append(str(i))
        out.append("%s --> %s" % (_srt_time(c.start_sec), _srt_time(c.end_sec)))
        out.append(c.text.strip() or " ")
        out.append("")
    return "\n".join(out)


_SRT_TIME_RE = re.compile(r"(\d+):(\d+):(\d+),(\d+)")


def _parse_srt_time(t):
    m = _SRT_TIME_RE.match(t.strip())
    if not m:
        raise ValueError("bad SRT timestamp: %r" % t)
    h, mi, s, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse_srt(text):
    """Parse .srt text back into CaptionCue list (round-trip / import)."""
    cues = []
    blocks = re.split(r"\r?\n\r?\n", str(text or "").strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 2:
            continue
        # first line may be the numeric index; find the timestamp line
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        start_s, end_s = [p.strip() for p in lines[ts_idx].split("-->")]
        body = "\n".join(lines[ts_idx + 1:]).strip()
        cues.append(CaptionCue(_parse_srt_time(start_s), _parse_srt_time(end_s), body))
    return cues


# ---------------------------------------------------------------------------
# styling
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_color(value, fallback):
    v = str(value or "").strip()
    return v if _HEX_RE.match(v) else fallback


class CaptionStyle:
    """Burn-in styling. All fields are plain data (JSON-serializable)."""

    def __init__(self, name="custom", font="Arial", size=56, bold=False,
                 color="#FFFFFF", outline_color="#000000", outline_width=2,
                 shadow=1, bg=False, bg_color="#000000", bg_alpha=0.55,
                 position="bottom", margin_v=60, alignment="center",
                 highlight_words=(), highlight_color="#FFE135"):
        self.name = str(name or "custom")
        self.font = str(font or "Arial")
        try:
            self.size = int(size)
        except (TypeError, ValueError):
            self.size = 56
        self.size = min(160, max(16, self.size))
        self.bold = bool(bold)
        self.color = _clean_color(color, "#FFFFFF")
        self.outline_color = _clean_color(outline_color, "#000000")
        try:
            self.outline_width = int(outline_width)
        except (TypeError, ValueError):
            self.outline_width = 2
        self.outline_width = min(8, max(0, self.outline_width))
        self.shadow = 1 if shadow else 0
        self.bg = bool(bg)
        self.bg_color = _clean_color(bg_color, "#000000")
        try:
            self.bg_alpha = float(bg_alpha)
        except (TypeError, ValueError):
            self.bg_alpha = 0.55
        self.bg_alpha = min(0.95, max(0.0, self.bg_alpha))
        self.position = position if position in ("bottom", "top") else "bottom"
        self.margin_v = int(margin_v) if str(margin_v).lstrip("-").isdigit() else 60
        self.margin_v = min(400, max(0, self.margin_v))
        self.alignment = alignment if alignment in ("center", "left", "right") else "center"
        self.highlight_words = [str(w) for w in (highlight_words or []) if str(w).strip()]
        self.highlight_color = _clean_color(highlight_color, "#FFE135")

    def to_dict(self):
        return {"name": self.name, "font": self.font, "size": self.size,
                "bold": self.bold, "color": self.color,
                "outline_color": self.outline_color,
                "outline_width": self.outline_width, "shadow": self.shadow,
                "bg": self.bg, "bg_color": self.bg_color,
                "bg_alpha": self.bg_alpha, "position": self.position,
                "margin_v": self.margin_v, "alignment": self.alignment,
                "highlight_words": list(self.highlight_words),
                "highlight_color": self.highlight_color}

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        return cls(**{k: d[k] for k in (
            "name", "font", "size", "bold", "color", "outline_color",
            "outline_width", "shadow", "bg", "bg_color", "bg_alpha",
            "position", "margin_v", "alignment", "highlight_words",
            "highlight_color") if k in d})

    @classmethod
    def from_template(cls, name):
        return cls.from_dict(TEMPLATES.get(name, TEMPLATES["cinematic"]))


# Built-in templates — pure data; every field is user-editable in the UI.
TEMPLATES = {
    "cinematic": {
        "name": "cinematic", "font": "Georgia", "size": 60, "bold": False,
        "color": "#F5E6C8", "outline_color": "#000000", "outline_width": 2,
        "shadow": 1, "bg": False, "bg_color": "#000000", "bg_alpha": 0.55,
        "position": "bottom", "margin_v": 70, "alignment": "center",
        "highlight_words": [], "highlight_color": "#FFE135",
    },
    "clean": {
        "name": "clean", "font": "Arial", "size": 54, "bold": False,
        "color": "#FFFFFF", "outline_color": "#000000", "outline_width": 2,
        "shadow": 1, "bg": False, "bg_color": "#000000", "bg_alpha": 0.55,
        "position": "bottom", "margin_v": 60, "alignment": "center",
        "highlight_words": [], "highlight_color": "#FFE135",
    },
    "bold-pop": {
        "name": "bold-pop", "font": "Arial", "size": 64, "bold": True,
        "color": "#FFE135", "outline_color": "#000000", "outline_width": 3,
        "shadow": 1, "bg": True, "bg_color": "#000000", "bg_alpha": 0.60,
        "position": "bottom", "margin_v": 70, "alignment": "center",
        "highlight_words": [], "highlight_color": "#FFFFFF",
    },
}


# ---------------------------------------------------------------------------
# ASS generation (for the ffmpeg subtitles filter)
# ---------------------------------------------------------------------------

def _ass_color(hex_color, alpha=0.0):
    """#RRGGBB -> ASS &HAABBGGRR."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = "%02X" % int(round(min(1.0, max(0.0, alpha)) * 255))
    return "&H%s%s%s%s" % (a, b, g, r)


def _ass_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 3600 % 60
    return "%d:%02d:%05.2f" % (h, m, s)


_ALIGNMENT = {
    "bottom": {"center": 2, "left": 1, "right": 3},
    "top": {"center": 8, "left": 7, "right": 9},
}


def _wrap_text(text, width=WRAP_CHARS):
    """Greedy word wrap to ~width chars per line."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _apply_highlights(text, style):
    """Wrap whole-word highlight matches in ASS color override tags.

    Braces are stripped first (they are ASS override syntax).
    """
    text = text.replace("{", "").replace("}", "")
    words = [w for w in style.highlight_words if w.strip()]
    if not words:
        return text
    pat = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in words),
                     re.IGNORECASE | re.UNICODE)
    on = r"{\1c%s}" % _ass_color(style.highlight_color)
    off = r"{\1c}"
    return pat.sub(lambda m: on + m.group(1) + off, text)


def to_ass(cues, style, width=1920, height=1080):
    """Generate a full ASS subtitle file from cues + style."""
    if isinstance(style, dict):
        style = CaptionStyle.from_dict(style)
    align = _ALIGNMENT[style.position][style.alignment]
    # BorderStyle 3 = opaque box behind text (honest background box);
    # otherwise 1 = outline + shadow.
    border_style = 3 if style.bg else 1
    back = _ass_color(style.bg_color, style.bg_alpha if style.bg else 0.0)
    style_line = (
        "Style: Cap,%s,%d,%s,%s,%s,%s,%d,0,0,0,100,100,0,0,%d,%d,%d,%d,"
        "40,40,%d,1" % (
            style.font, style.size,
            _ass_color(style.color),            # PrimaryColour
            _ass_color(style.color),            # SecondaryColour (karaoke; unused)
            _ass_color(style.outline_color, 0.35),  # OutlineColour
            back,                               # BackColour
            -1 if style.bold else 0,            # Bold
            border_style,                       # BorderStyle
            style.outline_width,                # Outline
            style.shadow,                       # Shadow
            align,                              # Alignment
            style.margin_v,                     # MarginV
        ))
    events = []
    for c in sorted(cues, key=lambda x: x.start_sec):
        body = r"\N".join(_wrap_text(_apply_highlights(c.text.strip(), style)))
        if not body.strip():
            continue
        events.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s" % (
            _ass_time(c.start_sec), _ass_time(c.end_sec), body))
    return "\n".join([
        "[Script Info]",
        "; AutoEditor captions — generated, do not hand-edit lightly",
        "ScriptType: v4.00+",
        "PlayResX: %d" % int(width),
        "PlayResY: %d" % int(height),
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ] + events) + "\n"


def cue_hash(cues, style_dict):
    """Stable hash of cues + style (render cache invalidation)."""
    h = hashlib.sha1()
    for c in sorted(cues, key=lambda x: (x.start_sec, x.end_sec)):
        h.update(("%s|%.3f|%.3f|%s;" % (c.id, c.start_sec, c.end_sec, c.text)
                  ).encode("utf-8"))
    h.update(repr(sorted((style_dict or {}).items())).encode("utf-8"))
    return h.hexdigest()[:16]
