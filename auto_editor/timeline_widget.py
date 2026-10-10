"""M3 editing workspace — custom multitrack timeline widget.

Layout (top to bottom): time ruler, VIDEO lane (one block per slot),
NARRATION lane, MUSIC lane, SFX lane (event ticks), CAPTION lane (real
caption cues when the project has them — M6 — otherwise one block per
slot from the slot label, drawn dimmer as an honest fallback).

The widget paints only. ALL mutation logic lives in the Qt-free helpers in
timeline.py (reorder_slot, trim_slot, set_slot_field, TimelineHistory);
this module adds no mutation logic of its own.
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QPainter, QColor, QFont, QPen

from .timeline import TRANSITIONS

# Midnight Cinema palette (matches assets/premium.qss intent)
BG = QColor("#0A0A0F")
PANEL = QColor("#101018")
BORDER = QColor("#FFFFFF22")
GOLD = QColor("#D9A441")
GOLD_DIM = QColor("#8a6a2a")
TEXT = QColor("#e8e6e1")
MUTED = QColor("#8b8b96")
EMERALD = QColor("#3FA46A")
BLOCK = QColor("#1b1b26")
LANE_LABEL_W = 92

LANES = (
    ("ruler", "TIME", 30),
    ("video", "VIDEO", 58),
    ("narration", "NARR", 30),
    ("music", "MUSIC", 30),
    ("sfx", "SFX", 30),
    ("caption", "CAPTION", 36),
)


def fmt_time(sec):
    sec = max(0.0, float(sec))
    m = int(sec // 60)
    s = sec - m * 60
    return "%d:%04.1f" % (m, s)


class TimelineWidget(QWidget):
    """Painted multitrack timeline. Emits slot_selected(slot_id) on clicks."""

    slot_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.selected_id = ""
        self.playhead_sec = 0.0
        self.px_per_sec = 40.0
        self._hover_id = ""
        self.setMouseTracking(True)
        self.setMinimumHeight(sum(h for _, _, h in LANES) + 8)
        self.setMinimumWidth(400)

    # -- data -----------------------------------------------------------------
    def set_project(self, project):
        self.project = project
        if project is not None:
            ids = {s.id for s in project.tracks["visuals"].slots}
            if self.selected_id not in ids:
                self.selected_id = ""
        self._fit_width()
        self.update()

    def set_zoom(self, px_per_sec):
        self.px_per_sec = max(8.0, min(240.0, float(px_per_sec)))
        self._fit_width()
        self.update()

    def set_playhead(self, sec):
        self.playhead_sec = max(0.0, float(sec))
        self.update()

    def select(self, slot_id):
        self.selected_id = slot_id or ""
        self.update()

    def _fit_width(self):
        dur = self.project.total_duration if self.project else 60.0
        self.setMinimumWidth(int(LANE_LABEL_W + max(dur, 1.0) * self.px_per_sec + 40))

    # -- geometry ---------------------------------------------------------------
    def _lane_y(self, lane_key):
        y = 4
        for key, _, h in LANES:
            if key == lane_key:
                return y, h
            y += h
        return 4, 30

    def _x(self, sec):
        return LANE_LABEL_W + sec * self.px_per_sec

    def _sec(self, x):
        return max(0.0, (x - LANE_LABEL_W) / self.px_per_sec)

    def _slot_at(self, x, y):
        """Return slot id under (x, y) in the video/caption lanes, else ''."""
        if not self.project:
            return ""
        vy, vh = self._lane_y("video")
        cy, ch = self._lane_y("caption")
        if not ((vy <= y <= vy + vh) or (cy <= y <= cy + ch)):
            return ""
        sec = self._sec(x)
        for s in self.project.slots:
            if s.start_sec <= sec <= s.end_sec:
                return s.id
        return ""

    # -- events -------------------------------------------------------------------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            sid = self._slot_at(ev.position().x(), ev.position().y())
            if sid:
                self.selected_id = sid
                self.update()
                self.slot_selected.emit(sid)

    def mouseMoveEvent(self, ev):
        sid = self._slot_at(ev.position().x(), ev.position().y())
        if sid != self._hover_id:
            self._hover_id = sid
            self.setCursor(Qt.CursorShape.PointingHandCursor if sid
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    # -- painting -------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        p.setFont(QFont("Sans", 8))
        self._draw_ruler(p)
        if not self.project or not self.project.slots:
            p.setPen(MUTED)
            vy, vh = self._lane_y("video")
            p.drawText(QRect(LANE_LABEL_W + 12, vy, self.width(), vh),
                       Qt.AlignmentFlag.AlignVCenter,
                       "Koi slot nahi — Pipeline tab se slots banao, phir yahan edit karo.")
            for key, label, _ in LANES[2:]:
                self._lane_label(p, key, label)
            p.end()
            return
        self._draw_video_lane(p)
        self._draw_single_lane(p, "narration", "NARR",
                               self.project.narration_path, "No narration")
        self._draw_single_lane(p, "music", "MUSIC",
                               self.project.bgm_path, "No music")
        self._draw_sfx_lane(p)
        self._draw_caption_lane(p)
        self._draw_playhead(p)
        p.end()

    def _lane_label(self, p, key, label):
        y, h = self._lane_y(key)
        p.setPen(MUTED)
        p.drawText(QRect(6, y, LANE_LABEL_W - 12, h),
                   Qt.AlignmentFlag.AlignVCenter, label)

    def _draw_ruler(self, p):
        y, h = self._lane_y("ruler")
        self._lane_label(p, "ruler", "TIME")
        # adaptive tick step: at least ~64 px between major ticks
        for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
            if step * self.px_per_sec >= 64:
                break
        dur = self.project.total_duration if self.project else 0.0
        p.setPen(QPen(BORDER))
        t = 0.0
        while t <= dur + 0.001:
            x = int(self._x(t))
            p.drawLine(x, y + h - 8, x, y + h)
            p.setPen(MUTED)
            p.drawText(x + 3, y + 11, fmt_time(t))
            p.setPen(QPen(BORDER))
            t += step

    def _draw_video_lane(self, p):
        import os
        y, h = self._lane_y("video")
        self._lane_label(p, "video", "VIDEO")
        for s in self.project.slots:
            x0 = int(self._x(s.start_sec))
            x1 = int(self._x(s.end_sec))
            r = QRect(x0 + 1, y + 4, max(6, x1 - x0 - 2), h - 8)
            sel = (s.id == self.selected_id)
            hov = (s.id == self._hover_id)
            p.fillRect(r, BLOCK if not hov else QColor("#23232f"))
            pen = QPen(GOLD if sel else BORDER)
            pen.setWidth(2 if sel else 1)
            p.setPen(pen)
            p.drawRect(r)
            # transition marker: small gold tick at block start (except first)
            if s.transition != "cut":
                p.fillRect(QRect(x0 + 1, y + 4, 3, h - 8),
                           GOLD if s.transition == "xfade" else GOLD_DIM)
            p.setPen(TEXT if sel else MUTED)
            name = s.label.strip() or os.path.basename(s.visual_path) or "slot"
            if len(name) > 26:
                name = name[:25] + "…"
            p.drawText(r.adjusted(6, 0, -4, 0),
                       Qt.AlignmentFlag.AlignVCenter, name)

    def _draw_single_lane(self, p, key, label, media_path, empty_text):
        import os
        y, h = self._lane_y(key)
        self._lane_label(p, key, label)
        dur = self.project.total_duration
        if not media_path or dur <= 0:
            p.setPen(MUTED)
            p.drawText(QRect(LANE_LABEL_W + 12, y, 300, h),
                       Qt.AlignmentFlag.AlignVCenter, empty_text)
            return
        x0 = int(self._x(0))
        x1 = int(self._x(dur))
        r = QRect(x0 + 1, y + 6, max(6, x1 - x0 - 2), h - 12)
        p.fillRect(r, QColor("#14231b"))
        p.setPen(QPen(EMERALD))
        p.drawRect(r)
        p.setPen(MUTED)
        name = os.path.basename(media_path)
        if len(name) > 30:
            name = name[:29] + "…"
        p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, name)

    def _draw_sfx_lane(self, p):
        y, h = self._lane_y("sfx")
        self._lane_label(p, "sfx", "SFX")
        events = self.project.tracks["sfx"].sfx_events
        if not events:
            p.setPen(MUTED)
            p.drawText(QRect(LANE_LABEL_W + 12, y, 300, h),
                       Qt.AlignmentFlag.AlignVCenter, "No SFX events")
            return
        for e in events:
            x = int(self._x(e.time_sec))
            p.setPen(QPen(GOLD))
            p.drawLine(x, y + 6, x, y + h - 6)
            p.fillRect(QRect(x - 2, y + h // 2 - 2, 5, 5), GOLD)

    def _draw_caption_lane(self, p):
        y, h = self._lane_y("caption")
        self._lane_label(p, "caption", "CAPTION")
        # M6: real caption cues when the project has them; otherwise the
        # honest fallback (one block per slot from the slot label).
        cues = [c for c in (getattr(self.project, "caption_cues", None) or [])]
        if cues:
            for c in cues:
                try:
                    s0 = float(c["start"] if isinstance(c, dict) else c.start_sec)
                    s1 = float(c["end"] if isinstance(c, dict) else c.end_sec)
                    txt = str(c["text"] if isinstance(c, dict) else c.text).strip()
                except (TypeError, ValueError, KeyError, AttributeError):
                    continue
                x0 = int(self._x(s0))
                x1 = int(self._x(s1))
                r = QRect(x0 + 1, y + 5, max(6, x1 - x0 - 2), h - 10)
                p.fillRect(r, QColor("#2a2416"))
                p.setPen(QPen(GOLD_DIM))
                p.drawRect(r)
                p.setPen(TEXT)
                if len(txt) > 26:
                    txt = txt[:25] + "…"
                p.drawText(r.adjusted(6, 0, -4, 0),
                           Qt.AlignmentFlag.AlignVCenter, txt or "(khaali)")
            return
        for s in self.project.slots:
            x0 = int(self._x(s.start_sec))
            x1 = int(self._x(s.end_sec))
            r = QRect(x0 + 1, y + 5, max(6, x1 - x0 - 2), h - 10)
            sel = (s.id == self.selected_id)
            p.fillRect(r, QColor("#1a2030"))
            p.setPen(QPen(GOLD_DIM if not sel else GOLD))
            p.drawRect(r)
            p.setPen(MUTED)
            cap = s.label.strip() or "(caption nahi)"
            if len(cap) > 30:
                cap = cap[:29] + "…"
            p.drawText(r.adjusted(6, 0, -4, 0),
                       Qt.AlignmentFlag.AlignVCenter, cap)

    def _draw_playhead(self, p):
        x = int(self._x(self.playhead_sec))
        p.setPen(QPen(GOLD, 2))
        p.drawLine(x, 0, x, self.height())
