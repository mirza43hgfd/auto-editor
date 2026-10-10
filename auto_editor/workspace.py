"""M3 editing workspace — the "NOT A DASHBOARD" main view.

MainWorkspace layout:
  left    storyboard/scene panel (slot list with thumbnails)
  center  preview monitor (selected slot's visual) + transport row
          (play = steps through slot stills on a QTimer; there is deliberately
          NO full video playback engine yet — the transport says so honestly)
  right   properties inspector (timings, prompt, transition, motion — all
          editable, written back to the Project model with undo support)
  bottom  multitrack TimelineWidget + zoom + reorder + undo/redo

All timeline mutations go through the Qt-free helpers in timeline.py, so the
model logic is unit-testable. This widget only wires controls to them.
"""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QLabel, QPushButton, QSlider, QScrollArea, QFormLayout, QDoubleSpinBox,
    QLineEdit, QComboBox, QPlainTextEdit, QFileDialog, QAbstractItemView,
    QInputDialog, QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QSpinBox, QCheckBox, QColorDialog,
)
from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QSize, QThread
from PySide6.QtGui import QPixmap, QIcon, QColor

from .timeline import (
    Project, TRANSITIONS, MOTION_PRESETS, VISUAL_TYPES,
    find_slot, reorder_slot, trim_slot, set_slot_field, TimelineHistory,
    DEFAULT_MIXER,
)
from .timeline_widget import TimelineWidget, fmt_time
from . import captions as _cap
from .captions import CaptionStyle, TEMPLATES
from .characters import (
    Character, attach_character, detach_character,
    detach_character_everywhere, check_continuity,
)
from . import settings_store

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm")

MEDIA_FILTER = ("Media (*.jpg *.jpeg *.png *.webp *.bmp "
                "*.mp4 *.mov *.mkv *.avi *.webm)")

IMAGE_FILTER = "Images (*.jpg *.jpeg *.png *.webp *.bmp)"


class _SceneRegenThread(QThread):
    """Runs generate_scene_visual off the UI thread. Emits done(ok, message).

    The Project model is plain Python (Qt-free), so the worker only touches
    model data; all widget updates happen in the done() slot on the UI
    thread. Honest limit: edits made mid-regen are not blocked (only the
    regen button is disabled) — regen only overwrites the scene's own file.
    """

    done = Signal(bool, str)

    def __init__(self, project, slot_id, parent=None):
        super().__init__(parent)
        self._project = project
        self._slot_id = slot_id

    def run(self):
        try:
            from . import auto_pipeline
            ok, res = auto_pipeline.generate_scene_visual(
                self._project, self._slot_id, "")
        except Exception as e:  # never die silently off-thread
            ok, res = False, "Regenerate fail: %s" % e
        self.done.emit(ok, res)


def _is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def _guess_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "clip"
    return "photo"


class MainWorkspace(QWidget):
    """Editing-first workspace. Emits project_changed after every mutation."""

    project_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.selected_id = ""
        self.history = TimelineHistory()
        self._loading = False   # guard: populating inspector, not user edits
        self._play_idx = 0
        self._regen_thread = None  # active _SceneRegenThread, else None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        self.vsplit = QSplitter(Qt.Orientation.Vertical)

        # ---- top: storyboard | monitor | inspector --------------------
        top = QSplitter(Qt.Orientation.Horizontal)

        # storyboard
        sb_wrap = QWidget()
        sb_lay = QVBoxLayout(sb_wrap)
        sb_lay.setContentsMargins(0, 0, 0, 0)
        sb_head = QHBoxLayout()
        sb_head.setContentsMargins(0, 0, 0, 0)
        sb_head.addWidget(QLabel("Storyboard — scenes"))
        self.sb_warn = QLabel("")
        self.sb_warn.setObjectName("warnbadge")
        self.sb_warn.setToolTip("Continuity warnings")
        sb_head.addWidget(self.sb_warn)
        sb_head.addStretch(1)
        sb_lay.addLayout(sb_head)
        self.story = QListWidget()
        self.story.setIconSize(QSize(96, 60))
        self.story.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.story.itemClicked.connect(self._story_clicked)
        sb_lay.addWidget(self.story, 1)

        # characters (M4) — library panel under the storyboard
        ch_head = QHBoxLayout()
        ch_head.setContentsMargins(0, 0, 0, 0)
        ch_head.addWidget(QLabel("Characters"))
        ch_head.addStretch(1)
        sb_lay.addLayout(ch_head)
        self.char_list = QListWidget()
        self.char_list.setMaximumHeight(110)
        self.char_list.setToolTip(
            "Project ke characters — scene se jorne ke liye select karo")
        self.char_list.itemClicked.connect(lambda _it: self._update_char_buttons())
        sb_lay.addWidget(self.char_list)

        ch_btns = QHBoxLayout()
        ch_btns.setContentsMargins(0, 0, 0, 0)
        self.btn_char_add = QPushButton("＋ Add")
        self.btn_char_add.setToolTip("Naya character banao (naam + description)")
        self.btn_char_add.clicked.connect(self._char_add)
        self.btn_char_attach = QPushButton("→ Scene")
        self.btn_char_attach.setToolTip("Selected character ko selected scene se joro")
        self.btn_char_attach.clicked.connect(self._char_attach)
        self.btn_char_detach = QPushButton("✕ Unlink")
        self.btn_char_detach.setToolTip("Character ko selected scene se hatao")
        self.btn_char_detach.clicked.connect(self._char_detach)
        ch_btns.addWidget(self.btn_char_add)
        ch_btns.addWidget(self.btn_char_attach)
        ch_btns.addWidget(self.btn_char_detach)
        sb_lay.addLayout(ch_btns)

        ch_btns2 = QHBoxLayout()
        ch_btns2.setContentsMargins(0, 0, 0, 0)
        self.btn_char_image = QPushButton("🖼 Set image…")
        self.btn_char_image.setToolTip(
            "Character ki primary reference image lagao ya badlo")
        self.btn_char_image.clicked.connect(self._char_set_image)
        self.btn_char_remove = QPushButton("🗑 Remove")
        self.btn_char_remove.setToolTip(
            "Character delete karo (sab scenes se unlink ho jayega)")
        self.btn_char_remove.clicked.connect(self._char_remove)
        ch_btns2.addWidget(self.btn_char_image)
        ch_btns2.addWidget(self.btn_char_remove)
        sb_lay.addLayout(ch_btns2)
        top.addWidget(sb_wrap)

        # monitor + transport
        mon_wrap = QWidget()
        mon_lay = QVBoxLayout(mon_wrap)
        mon_lay.setContentsMargins(0, 0, 0, 0)
        mon_lay.addWidget(QLabel("Preview monitor"))
        self.monitor = QLabel()
        self.monitor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.monitor.setMinimumHeight(280)
        self.monitor.setStyleSheet("background:#050508; border:1px solid #FFFFFF22;")
        self.monitor.setWordWrap(True)
        mon_lay.addWidget(self.monitor, 1)

        trow = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play stills")
        self.btn_play.setToolTip(
            "Slot stills step-through preview (1.2s har slot).\n"
            "Full video playback engine abhi nahi hai — render karke dekho.")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.clicked.connect(self._stop_play)
        self.time_label = QLabel("0:00.0 / 0:00.0")
        self.time_label.setObjectName("muted")
        trow.addWidget(self.btn_play)
        trow.addWidget(self.btn_stop)
        trow.addWidget(self.time_label, 1)
        mon_lay.addLayout(trow)

        # ---- M6: audio mixer row (wired to the render mix) ----
        mrow = QHBoxLayout()
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.addWidget(QLabel("Mixer:"))
        self.mix_sliders = {}
        self.mix_labels = {}
        for key, label, lo, hi, default, scale, fmt in (
                ("narration_vol", "Narra", 0, 300, 180, 100.0, "%.1f"),
                ("music_vol", "Music", 0, 300, 40, 100.0, "%.1f"),
                ("sfx_vol", "SFX", 0, 200, 100, 100.0, "%.1f"),
                ("ducking", "Duck", 0, 100, 100, 100.0, "%d%%")):
            mrow.addWidget(QLabel(label))
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(default)
            sl.setMaximumWidth(90)
            sl.setToolTip({
                "narration_vol": "Narration volume (render mix me lagta hai)",
                "music_vol": "Background music volume",
                "sfx_vol": "Sound-effects volume (per-event volume se guna hota hai)",
                "ducking": "Music ducking jab narration bole (0 = band)",
            }[key])
            lab = QLabel("")
            lab.setMinimumWidth(38)
            sl.valueChanged.connect(
                lambda _v, k=key, s=scale, f=fmt: self._mixer_changed(k, s, f))
            self.mix_sliders[key] = (sl, scale)
            self.mix_labels[key] = (lab, fmt)
            mrow.addWidget(sl)
            mrow.addWidget(lab)
        mrow.addStretch(1)
        mon_lay.addLayout(mrow)
        top.addWidget(mon_wrap)

        # inspector
        insp_wrap = QWidget()
        insp_lay = QVBoxLayout(insp_wrap)
        insp_lay.setContentsMargins(0, 0, 0, 0)
        self.insp_title = QLabel("Inspector")
        self.insp_title.setObjectName("heading")
        insp_lay.addWidget(self.insp_title)

        form = QFormLayout()
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0, 100000)
        self.start_spin.setDecimals(1)
        self.start_spin.setSuffix(" s")
        self.start_spin.setToolTip("Slot start — parosi slot se takra nahi sakta (auto-clamp).")
        self.start_spin.editingFinished.connect(self._apply_trim)
        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0, 100000)
        self.end_spin.setDecimals(1)
        self.end_spin.setSuffix(" s")
        self.end_spin.setToolTip("Slot end — parosi slot se takra nahi sakta (auto-clamp).")
        self.end_spin.editingFinished.connect(self._apply_trim)
        form.addRow("Start:", self.start_spin)
        form.addRow("End:", self.end_spin)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Narration line / caption")
        self.label_edit.editingFinished.connect(
            lambda: self._set_field("label", self.label_edit.text()))
        form.addRow("Label:", self.label_edit)

        vrow = QHBoxLayout()
        self.visual_edit = QLineEdit()
        self.visual_edit.setPlaceholderText("Visual file path")
        self.visual_edit.editingFinished.connect(
            lambda: self._set_visual_path(self.visual_edit.text().strip()))
        self.btn_browse = QPushButton("…")
        self.btn_browse.setFixedWidth(36)
        self.btn_browse.setToolTip("Visual file chuno (tasveer ya clip)")
        self.btn_browse.clicked.connect(self._browse_visual)
        self.btn_clear_visual = QPushButton("✕")
        self.btn_clear_visual.setFixedWidth(36)
        self.btn_clear_visual.setToolTip("Visual hatao")
        self.btn_clear_visual.clicked.connect(lambda: self._set_visual_path(""))
        vrow.addWidget(self.visual_edit, 1)
        vrow.addWidget(self.btn_browse)
        vrow.addWidget(self.btn_clear_visual)
        form.addRow("Visual:", vrow)

        self.type_combo = QComboBox()
        self.type_combo.addItems(list(VISUAL_TYPES))
        self.type_combo.currentTextChanged.connect(
            lambda t: self._set_field("visual_type", t))
        form.addRow("Type:", self.type_combo)

        self.trans_combo = QComboBox()
        self.trans_combo.addItems(list(TRANSITIONS))
        self.trans_combo.setToolTip("Render me WAHI transition lagegi jo yahan select hai.")
        self.trans_combo.currentTextChanged.connect(
            lambda t: self._set_field("transition", t))
        form.addRow("Transition:", self.trans_combo)

        # M5: honest "why" — shows the editing decision's reasoning (or the
        # user's manual choice note) for the selected slot.
        self.edit_why_label = QLabel("—")
        self.edit_why_label.setWordWrap(True)
        self.edit_why_label.setStyleSheet("color: #b89a5e; font-size: 11px;")
        self.edit_why_label.setToolTip(
            "Editing decision ki wajah — auto ya tumhari manual choice.")
        form.addRow("Kyun:", self.edit_why_label)

        self.motion_combo = QComboBox()
        self.motion_combo.addItems(list(MOTION_PRESETS))
        self.motion_combo.currentTextChanged.connect(
            lambda t: self._set_field("motion_preset", t))
        form.addRow("Motion:", self.motion_combo)

        self.clip_in_spin = QDoubleSpinBox()
        self.clip_in_spin.setRange(0, 100000)
        self.clip_in_spin.setDecimals(1)
        self.clip_in_spin.setSuffix(" s")
        self.clip_in_spin.setToolTip("Clip ka hissa yahan se shuru hoga.")
        self.clip_in_spin.editingFinished.connect(self._apply_clip_range)
        self.clip_out_spin = QDoubleSpinBox()
        self.clip_out_spin.setRange(0, 100000)
        self.clip_out_spin.setDecimals(1)
        self.clip_out_spin.setSuffix(" s (0 = poora)")
        self.clip_out_spin.setToolTip("Clip ka hissa yahan khatam (0 = clip ka end tak).")
        self.clip_out_spin.editingFinished.connect(self._apply_clip_range)
        form.addRow("Clip in:", self.clip_in_spin)
        form.addRow("Clip out:", self.clip_out_spin)

        insp_lay.addLayout(form)
        insp_lay.addWidget(QLabel("Visual prompt:"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText("Is slot ka visual prompt (Flow copy-paste)…")
        self.prompt_edit.setMaximumHeight(110)
        self.prompt_edit.setToolTip(
            "Type karte hi save hota hai (undo ke liye focus par checkpoint banta hai).")
        self.prompt_edit.textChanged.connect(self._prompt_typed)
        self.prompt_edit.installEventFilter(self)
        insp_lay.addWidget(self.prompt_edit, 1)
        insp_lay.addWidget(QLabel("Scene characters:"))
        self.slot_chars_label = QLabel("—")
        self.slot_chars_label.setWordWrap(True)
        self.slot_chars_label.setObjectName("muted")
        self.slot_chars_label.setToolTip(
            "Is scene se jure characters — Characters panel se joro/hatao")
        insp_lay.addWidget(self.slot_chars_label)
        self.btn_regen_scene = QPushButton("⟳ Regenerate scene visual")
        self.btn_regen_scene.clicked.connect(self._regen_scene)
        insp_lay.addWidget(self.btn_regen_scene)
        top.addWidget(insp_wrap)
        top.setSizes([240, 560, 300])

        self.vsplit.addWidget(top)

        # ---- bottom: timeline + captions tabs --------------------------
        tl_wrap = QWidget()
        tl_lay = QVBoxLayout(tl_wrap)
        tl_lay.setContentsMargins(0, 0, 0, 0)
        tl_bar = QHBoxLayout()
        tl_bar.addWidget(QLabel("Timeline"))
        tl_bar.addWidget(QLabel("Zoom:"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(8, 240)
        self.zoom.setValue(40)
        self.zoom.setMaximumWidth(160)
        self.zoom.setToolTip("Timeline zoom (pixels per second)")
        self.zoom.valueChanged.connect(self._on_zoom)
        tl_bar.addWidget(self.zoom)
        self.btn_left = QPushButton("◀ Move left")
        self.btn_left.setToolTip("Selected slot ko ek step peeche karo")
        self.btn_left.clicked.connect(lambda: self._reorder(-1))
        self.btn_right = QPushButton("Move right ▶")
        self.btn_right.setToolTip("Selected slot ko ek step aage karo")
        self.btn_right.clicked.connect(lambda: self._reorder(1))
        self.btn_undo = QPushButton("↩ Undo")
        self.btn_undo.setToolTip("Timeline edit wapas lo (Ctrl+Z)")
        self.btn_undo.setShortcut("Ctrl+Z")
        self.btn_undo.clicked.connect(self._undo)
        self.btn_redo = QPushButton("↪ Redo")
        self.btn_redo.setToolTip("Undo wapas lao (Ctrl+Shift+Z)")
        self.btn_redo.setShortcut("Ctrl+Shift+Z")
        self.btn_redo.clicked.connect(self._redo)
        tl_bar.addWidget(self.btn_left)
        tl_bar.addWidget(self.btn_right)
        tl_bar.addWidget(self.btn_undo)
        tl_bar.addWidget(self.btn_redo)
        tl_bar.addStretch(1)
        tl_lay.addLayout(tl_bar)

        self.timeline = TimelineWidget()
        self.timeline.slot_selected.connect(self._timeline_selected)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setWidget(self.timeline)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tl_lay.addWidget(scroll, 1)

        # ---- M6: captions tab (cue list + style) ----
        cap_tab = QWidget()
        self._build_captions_tab(cap_tab)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(tl_wrap, "Timeline")
        self.bottom_tabs.addTab(cap_tab, "Captions")
        self.vsplit.addWidget(self.bottom_tabs)
        self.vsplit.setSizes([520, 260])
        lay.addWidget(self.vsplit, 1)

        # stills-stepping timer
        self._timer = QTimer(self)
        self._timer.setInterval(1200)
        self._timer.timeout.connect(self._step)

        self._set_inspector_enabled(False)
        self._update_transport()

    # ------------------------------------------------------------------
    # M6: captions tab + audio mixer
    # ------------------------------------------------------------------
    def _build_captions_tab(self, tab):
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.cap_generate = QPushButton("🔤 Generate from narration")
        self.cap_generate.setToolTip(
            "Har slot ki narration se phrase-level cues banao.\n"
            "Timing slot window me characters ke hisaab se taqseem hoti hai "
            "(andaza hai — table me edit kar sakte ho).")
        self.cap_generate.clicked.connect(self._cap_generate)
        self.cap_validate = QPushButton("✓ Validate")
        self.cap_validate.setToolTip(
            "Cues ki timing check karo (overlap / gap / bounds)")
        self.cap_validate.clicked.connect(self._cap_validate)
        self.cap_export = QPushButton("⬇ Export SRT")
        self.cap_export.setToolTip("Cues ko .srt file me save karo")
        self.cap_export.clicked.connect(self._cap_export)
        self.cap_add = QPushButton("+ Add")
        self.cap_add.setToolTip("Naya cue joro")
        self.cap_add.clicked.connect(self._cap_add)
        self.cap_del = QPushButton("− Delete")
        self.cap_del.setToolTip("Selected cues hatao")
        self.cap_del.clicked.connect(self._cap_del)
        bar.addWidget(self.cap_generate)
        bar.addWidget(self.cap_validate)
        bar.addWidget(self.cap_export)
        bar.addWidget(self.cap_add)
        bar.addWidget(self.cap_del)
        bar.addWidget(QLabel("Template:"))
        self.cap_template = QComboBox()
        self.cap_template.addItems(list(TEMPLATES.keys()) + ["custom"])
        self.cap_template.setToolTip("Caption style template")
        self.cap_template.currentTextChanged.connect(self._cap_template_changed)
        bar.addWidget(self.cap_template)
        bar.addStretch(1)
        self.cap_status = QLabel("")
        self.cap_status.setObjectName("muted")
        bar.addWidget(self.cap_status)
        lay.addLayout(bar)

        self.cap_table = QTableWidget(0, 3)
        self.cap_table.setHorizontalHeaderLabels(["Start (s)", "End (s)", "Text"])
        self.cap_table.horizontalHeader().setStretchLastSection(True)
        self.cap_table.setColumnWidth(0, 90)
        self.cap_table.setColumnWidth(1, 90)
        self.cap_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.cap_table.setToolTip(
            "Cue list — cells edit kar sakte ho (timing + text)")
        self.cap_table.cellChanged.connect(self._cap_cell_changed)
        lay.addWidget(self.cap_table, 1)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Font:"))
        self.cap_font = QComboBox()
        self.cap_font.setEditable(True)
        self.cap_font.addItems(["Arial", "Georgia", "Verdana", "Tahoma",
                                "Times New Roman", "Trebuchet MS"])
        self.cap_font.setToolTip("Burn-in caption font")
        self.cap_font.currentTextChanged.connect(
            lambda _t: self._cap_style_changed())
        srow.addWidget(self.cap_font)
        srow.addWidget(QLabel("Size:"))
        self.cap_size = QSpinBox()
        self.cap_size.setRange(16, 160)
        self.cap_size.setToolTip("Font size (project resolution ke pixels me)")
        self.cap_size.valueChanged.connect(lambda _v: self._cap_style_changed())
        srow.addWidget(self.cap_size)
        self.cap_bold = QCheckBox("Bold")
        self.cap_bold.toggled.connect(lambda _v: self._cap_style_changed())
        srow.addWidget(self.cap_bold)
        self._cap_btn_color = "#FFFFFF"
        self.cap_color_btn = QPushButton("Color")
        self.cap_color_btn.setToolTip("Caption text color")
        self.cap_color_btn.clicked.connect(self._cap_pick_color)
        srow.addWidget(self.cap_color_btn)
        srow.addWidget(QLabel("Outline:"))
        self.cap_outline = QSpinBox()
        self.cap_outline.setRange(0, 8)
        self.cap_outline.setToolTip("Text outline motai")
        self.cap_outline.valueChanged.connect(
            lambda _v: self._cap_style_changed())
        srow.addWidget(self.cap_outline)
        srow.addWidget(QLabel("Pos:"))
        self.cap_pos = QComboBox()
        self.cap_pos.addItems(["bottom", "top"])
        self.cap_pos.setToolTip("Caption position")
        self.cap_pos.currentTextChanged.connect(
            lambda _t: self._cap_style_changed())
        srow.addWidget(self.cap_pos)
        srow.addWidget(QLabel("Margin:"))
        self.cap_margin = QSpinBox()
        self.cap_margin.setRange(0, 400)
        self.cap_margin.setToolTip("Kinare se fasla (pixels)")
        self.cap_margin.valueChanged.connect(
            lambda _v: self._cap_style_changed())
        srow.addWidget(self.cap_margin)
        self.cap_bg = QCheckBox("Box")
        self.cap_bg.setToolTip("Text ke peeche background box")
        self.cap_bg.toggled.connect(lambda _v: self._cap_style_changed())
        srow.addWidget(self.cap_bg)
        srow.addWidget(QLabel("Highlight:"))
        self.cap_hl = QLineEdit()
        self.cap_hl.setPlaceholderText("comma,separated,words")
        self.cap_hl.setToolTip(
            "Ye lafz alag rang me highlight honge (bold-pop template me)")
        self.cap_hl.editingFinished.connect(
            lambda: self._cap_style_changed())
        srow.addWidget(self.cap_hl, 1)
        lay.addLayout(srow)

        note = QLabel(
            "Cues render me jalte hain (burn-in) aur MP4 ke sath .srt bhi banti "
            "hai. Timing andaza hai (characters ke hisaab se taqseem) — ghalat "
            "lage to table me edit karo. Undo sirf timeline edits par hai.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._paint_color_btn()

    def _paint_color_btn(self):
        self.cap_color_btn.setStyleSheet(
            "QPushButton { background-color: %s; }" % self._cap_btn_color)

    def _cap_pick_color(self):
        col = QColorDialog.getColor(QColor(self._cap_btn_color), self,
                                    "Caption text color")
        if col.isValid():
            self._cap_btn_color = col.name()
            self._cap_style_changed()

    def _apply_style_to_fields(self, d):
        st = CaptionStyle.from_dict(d or {})
        self._loading = True
        try:
            self.cap_font.setCurrentText(st.font)
            self.cap_size.setValue(st.size)
            self.cap_bold.setChecked(st.bold)
            self._cap_btn_color = st.color
            self._paint_color_btn()
            self.cap_outline.setValue(st.outline_width)
            self.cap_pos.setCurrentText(st.position)
            self.cap_margin.setValue(st.margin_v)
            self.cap_bg.setChecked(st.bg)
            self.cap_hl.setText(", ".join(st.highlight_words))
            self.cap_template.blockSignals(True)
            self.cap_template.setCurrentText(
                st.name if st.name in TEMPLATES else "custom")
            self.cap_template.blockSignals(False)
        finally:
            self._loading = False

    def _refresh_captions(self):
        self._loading = True
        try:
            cues = (sorted(self.project.caption_cues,
                           key=lambda c: c.start_sec)
                    if self.project else [])
            tbl = self.cap_table
            tbl.setRowCount(len(cues))
            for r, c in enumerate(cues):
                for col, val in ((0, "%.2f" % c.start_sec),
                                 (1, "%.2f" % c.end_sec),
                                 (2, c.text)):
                    it = QTableWidgetItem(val)
                    it.setData(Qt.ItemDataRole.UserRole, c.id)
                    tbl.setItem(r, col, it)
            self._apply_style_to_fields(
                self.project.caption_style if self.project else {})
            self._refresh_mixer()
            has_slots = bool(self.project and self.project.slots)
            self.cap_generate.setEnabled(has_slots)
            self.cap_validate.setEnabled(bool(cues))
            self.cap_export.setEnabled(bool(cues))
            self.cap_add.setEnabled(has_slots)
            self.cap_del.setEnabled(bool(cues))
            self.cap_status.setText(
                "%d cues" % len(cues) if cues
                else "koi cue nahi — Generate dabao")
        finally:
            self._loading = False

    def _refresh_mixer(self):
        m = dict(DEFAULT_MIXER)
        if self.project:
            m.update(self.project.mixer or {})
        for key, scale, fmt in (("narration_vol", 100.0, "%.1f"),
                                ("music_vol", 100.0, "%.1f"),
                                ("sfx_vol", 100.0, "%.1f"),
                                ("ducking", 100.0, "%d%%")):
            sl, _s = self.mix_sliders[key]
            lab, _f = self.mix_labels[key]
            v = float(m.get(key, 0))
            sl.blockSignals(True)
            sl.setValue(int(round(v * scale)))
            sl.blockSignals(False)
            lab.setText(fmt % (v * 100 if key == "ducking" else v))

    def _mixer_changed(self, key, scale, fmt):
        if not self.project:
            return
        sl, _s = self.mix_sliders[key]
        val = sl.value() / scale
        self.project.mixer[key] = val
        lab, _f = self.mix_labels[key]
        lab.setText(fmt % (val * 100 if key == "ducking" else val))
        self.project_changed.emit()

    def _cap_generate(self):
        if not self.project or not self.project.slots:
            return
        cues = _cap.generate_cues(self.project)
        if not cues:
            QMessageBox.information(
                self, "Captions",
                "Kisi slot me narration text nahi mila — cues nahi bane.\n"
                "Pehle slots me narration_segment ya label likho.")
            return
        self.project.caption_cues = cues
        self.cap_status.setText("✓ %d cues bane" % len(cues))
        self._refresh_captions()
        self.timeline.set_project(self.project)
        self.project_changed.emit()

    def _cap_validate(self):
        if not self.project:
            return
        issues = _cap.validate_cues(self.project, self.project.caption_cues)
        errs = [i for i in issues if i["severity"] == "error"]
        warns = [i for i in issues if i["severity"] == "warning"]
        if not issues:
            self.cap_status.setText(
                "✓ %d cues OK" % len(self.project.caption_cues))
            QMessageBox.information(self, "Validate", "Sab cues theek hain ✓")
            return
        self.cap_status.setText(
            "⚠ %d errors, %d warnings" % (len(errs), len(warns)))
        lines = ["%s: %s" % ("ERROR" if i["severity"] == "error" else "WARN",
                             i["message"]) for i in issues[:20]]
        if len(issues) > 20:
            lines.append("... aur %d" % (len(issues) - 20))
        QMessageBox.warning(self, "Validate", "\n".join(lines))

    def _cap_export(self):
        if not self.project or not self.project.caption_cues:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SRT", "captions.srt", "SubRip (*.srt)")
        if not path:
            return
        if not path.lower().endswith(".srt"):
            path += ".srt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(_cap.to_srt(self.project.caption_cues))

    def _cap_add(self):
        if not self.project or not self.project.slots:
            return
        cues = sorted(self.project.caption_cues, key=lambda c: c.start_sec)
        if cues:
            start = cues[-1].end_sec + 0.2
            hard_end = self.project.total_duration
        else:
            start = self.project.slots[0].start_sec
            hard_end = self.project.slots[0].end_sec
        end = min(start + 2.0, hard_end)
        cue = _cap.CaptionCue(start, max(start + 0.5, end), "Naya caption")
        self.project.caption_cues.append(cue)
        self._refresh_captions()
        self.timeline.set_project(self.project)
        self.project_changed.emit()

    def _cap_del(self):
        if not self.project:
            return
        rows = sorted({i.row() for i in self.cap_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "Delete",
                                    "Pehle table me koi row select karo.")
            return
        ids = set()
        for r in rows:
            it = self.cap_table.item(r, 2)
            if it is not None:
                ids.add(it.data(Qt.ItemDataRole.UserRole))
        self.project.caption_cues = [
            c for c in self.project.caption_cues if c.id not in ids]
        self._refresh_captions()
        self.timeline.set_project(self.project)
        self.project_changed.emit()

    def _cap_cell_changed(self, row, col):
        if self._loading or not self.project:
            return
        tbl = self.cap_table
        it = tbl.item(row, col)
        if it is None:
            return
        cue_id = it.data(Qt.ItemDataRole.UserRole)
        cue = next((c for c in self.project.caption_cues if c.id == cue_id),
                   None)
        if cue is None:
            return
        try:
            if col == 0:
                new = _cap.edit_cue(cue, start=float(it.text()))
            elif col == 1:
                new = _cap.edit_cue(cue, end=float(it.text()))
            else:
                new = _cap.edit_cue(cue, text=it.text())
        except (TypeError, ValueError):
            self._refresh_captions()  # revert invalid input honestly
            return
        self.project.caption_cues[
            self.project.caption_cues.index(cue)] = new
        self._loading = True
        try:
            tbl.item(row, 0).setText("%.2f" % new.start_sec)
            tbl.item(row, 1).setText("%.2f" % new.end_sec)
        finally:
            self._loading = False
        self.timeline.set_project(self.project)
        self.project_changed.emit()

    def _cap_template_changed(self, name):
        if self._loading or not self.project:
            return
        if name == "custom":
            st = dict(self.project.caption_style)
            st["name"] = "custom"
            self.project.caption_style = st
        else:
            self.project.caption_style = CaptionStyle.from_template(
                name).to_dict()
            self._apply_style_to_fields(self.project.caption_style)
        self.project_changed.emit()

    def _cap_style_changed(self):
        if self._loading or not self.project:
            return
        st = {
            "name": "custom",
            "font": self.cap_font.currentText(),
            "size": self.cap_size.value(),
            "bold": self.cap_bold.isChecked(),
            "color": self._cap_btn_color,
            "outline_color": "#000000",
            "outline_width": self.cap_outline.value(),
            "shadow": 1,
            "bg": self.cap_bg.isChecked(),
            "bg_color": "#000000",
            "bg_alpha": 0.6,
            "position": self.cap_pos.currentText(),
            "margin_v": self.cap_margin.value(),
            "alignment": "center",
            "highlight_words": [w.strip() for w in self.cap_hl.text().split(",")
                                if w.strip()],
            "highlight_color": "#FFE135",
        }
        self.project.caption_style = CaptionStyle.from_dict(st).to_dict()
        self._cap_btn_color = self.project.caption_style["color"]
        self._paint_color_btn()
        self.cap_template.blockSignals(True)
        self.cap_template.setCurrentText("custom")
        self.cap_template.blockSignals(False)
        self.project_changed.emit()

    # ------------------------------------------------------------------
    # project binding
    # ------------------------------------------------------------------
    def set_project(self, project):
        self._stop_play()
        if project is not self.project:
            # new project object -> fresh undo stack + fresh selection
            self.project = project
            self.history = TimelineHistory()
            if project is not None:
                ids = {s.id for s in project.tracks["visuals"].slots}
                if self.selected_id not in ids:
                    self.selected_id = project.slots[0].id if project.slots else ""
            else:
                self.selected_id = ""
        self.refresh()

    def refresh(self):
        """Rebuild storyboard, inspector, timeline from the Project model."""
        self._refresh_storyboard()
        self._refresh_inspector()
        self._refresh_characters()
        self.timeline.set_project(self.project)
        self._refresh_captions()
        self._update_transport()
        self._update_edit_buttons()

    # ------------------------------------------------------------------
    # storyboard
    # ------------------------------------------------------------------
    def _refresh_storyboard(self):
        self._loading = True
        try:
            self.story.clear()
            if not self.project or not self.project.slots:
                it = QListWidgetItem("Koi slot nahi — Pipeline tab se banao")
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.story.addItem(it)
                return
            for i, s in enumerate(self.project.slots):
                rng = "%s–%s" % (fmt_time(s.start_sec), fmt_time(s.end_sec))
                text = "#%d %s  %s" % (i + 1, rng, s.label.strip() or "(no label)")
                it = QListWidgetItem(text)
                it.setData(Qt.ItemDataRole.UserRole, s.id)
                if s.visual_path and os.path.isfile(s.visual_path) and _is_image(s.visual_path):
                    pm = QPixmap(s.visual_path).scaled(
                        96, 60, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    if not pm.isNull():
                        it.setIcon(QIcon(pm))
                if s.id == self.selected_id:
                    self.story.setCurrentItem(it)
        finally:
            self._loading = False

    def _story_clicked(self, item):
        sid = item.data(Qt.ItemDataRole.UserRole)
        if sid:
            self._select(sid)

    # ------------------------------------------------------------------
    # selection + monitor
    # ------------------------------------------------------------------
    def _select(self, slot_id):
        self._stop_play()
        self.selected_id = slot_id
        self.timeline.select(slot_id)
        self._refresh_storyboard()
        self._refresh_inspector()
        self._show_selected_in_monitor()
        self._update_edit_buttons()
        self._update_char_buttons()

    def _timeline_selected(self, slot_id):
        self._select(slot_id)

    def _selected_slot(self):
        if not self.project or not self.selected_id:
            return None
        return find_slot(self.project, self.selected_id)

    def _show_selected_in_monitor(self):
        s = self._selected_slot()
        if s is None:
            self.monitor.setText("Koi slot select nahi")
            self.monitor.setPixmap(QPixmap())
            return
        vp = s.visual_path
        if vp and os.path.isfile(vp) and _is_image(vp):
            pm = QPixmap(vp)
            if not pm.isNull():
                self.monitor.setPixmap(pm.scaled(
                    self.monitor.size() * 0.96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                self.monitor.setText("")
                return
        self.monitor.setPixmap(QPixmap())
        if vp and os.path.isfile(vp):
            self.monitor.setText("🎬 clip: %s\n(still preview nahi — render karke dekho)"
                                 % os.path.basename(vp))
        elif vp:
            self.monitor.setText("Visual file nahi mili:\n%s" % vp)
        else:
            self.monitor.setText("No visual assigned — Inspector me lagao")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # keep the monitor pixmap fitted after resizes
        if self.monitor.pixmap() is not None and not self.monitor.pixmap().isNull():
            self._show_selected_in_monitor()

    def eventFilter(self, obj, ev):
        # prompt editor: checkpoint on focus-in (one undo step per edit
        # session), write-through while typing, notify on focus-out.
        if obj is self.prompt_edit:
            if ev.type() == QEvent.Type.FocusIn:
                if not self._loading and self.selected_id and self.project:
                    self.history.checkpoint(self.project)
            elif ev.type() == QEvent.Type.FocusOut:
                if self.project:
                    self._update_edit_buttons()
                    self.project_changed.emit()
        return super().eventFilter(obj, ev)

    def _prompt_typed(self):
        if self._loading or not self.project or not self.selected_id:
            return
        set_slot_field(self.project, self.selected_id, "prompt",
                       self.prompt_edit.toPlainText())

    # ------------------------------------------------------------------
    # characters (M4) — library panel + continuity badge + scene regen
    # ------------------------------------------------------------------
    def _selected_character(self):
        it = self.char_list.currentItem()
        if it is None or not self.project:
            return None
        return self.project.characters.get(it.data(Qt.ItemDataRole.UserRole))

    def _mutate_chars(self, fn):
        """Checkpoint history, run a cast mutation, refresh, notify.

        Unlike _mutate this does not require a selected slot — character
        edits are project-level.
        """
        if not self.project:
            return
        self.history.checkpoint(self.project)
        fn()
        self.refresh()
        self.project_changed.emit()

    def _refresh_characters(self):
        self.char_list.clear()
        if not self.project:
            self.sb_warn.setText("")
            self.sb_warn.setToolTip("")
            self._update_char_buttons()
            return
        for c in self.project.characters.characters:
            n = len(c.scene_appearances)
            it = QListWidgetItem("%s  (%d scene%s)%s" % (
                c.name, n, "" if n == 1 else "s",
                "" if c.reference_paths else " — no image"))
            it.setData(Qt.ItemDataRole.UserRole, c.id)
            it.setToolTip("Ref images: %d%s" % (
                len(c.reference_paths),
                (" | primary: " + os.path.basename(c.primary_reference))
                if c.primary_reference else ""))
            self.char_list.addItem(it)
        # continuity warnings -> honest badge, never blocking
        warns = [w for w in check_continuity(self.project)
                 if w["level"] == "warning"]
        if warns:
            self.sb_warn.setText("⚠ %d" % len(warns))
            tip = "\n".join("• " + w["message"] for w in warns[:8])
            if len(warns) > 8:
                tip += "\n… +%d aur" % (len(warns) - 8)
            self.sb_warn.setToolTip(tip)
        else:
            self.sb_warn.setText("")
            self.sb_warn.setToolTip("Continuity theek hai — koi warning nahi")
        self._update_char_buttons()

    def _update_char_buttons(self):
        has_proj = self.project is not None
        char = self._selected_character()
        slot = self._selected_slot()
        attached = (has_proj and char is not None and slot is not None
                    and char.id in slot.characters)
        self.btn_char_add.setEnabled(has_proj)
        self.btn_char_attach.setEnabled(
            has_proj and char is not None and slot is not None and not attached)
        self.btn_char_detach.setEnabled(bool(attached))
        self.btn_char_image.setEnabled(has_proj and char is not None)
        self.btn_char_remove.setEnabled(has_proj and char is not None)
        if not has_proj or char is None or slot is None:
            self.btn_char_attach.setToolTip(
                "Pehle ek character aur ek scene select karo")
        elif attached:
            self.btn_char_attach.setToolTip(
                "Ye character is scene se pehle se jura hai")
        else:
            self.btn_char_attach.setToolTip(
                "Selected character ko selected scene se joro")

    def _char_add(self):
        if not self.project:
            return
        name, ok = QInputDialog.getText(
            self, "Naya character", "Character ka naam:")
        if not ok or not name.strip():
            return
        desc, ok = QInputDialog.getText(
            self, "Naya character", "Description (look, kapre, etc.):")
        if not ok:
            return
        char = Character(name.strip(), description=desc.strip())
        self._mutate_chars(lambda: self.project.characters.add(char))

    def _char_attach(self):
        char, slot = self._selected_character(), self._selected_slot()
        if char is None or slot is None or not self.project:
            return
        self._mutate_chars(
            lambda: attach_character(self.project, slot.id, char.id))

    def _char_detach(self):
        char, slot = self._selected_character(), self._selected_slot()
        if char is None or slot is None or not self.project:
            return
        self._mutate_chars(
            lambda: detach_character(self.project, slot.id, char.id))

    def _char_set_image(self):
        char = self._selected_character()
        if char is None or not self.project:
            return
        p, _ = QFileDialog.getOpenFileName(
            self, "Primary reference image", "", IMAGE_FILTER)
        if not p:
            return
        # replaces the primary: newest choice goes first
        self._mutate_chars(lambda: char.add_reference(p, make_primary=True))

    def _char_remove(self):
        char = self._selected_character()
        if char is None or not self.project:
            return
        ans = QMessageBox.question(
            self, "Character delete karo?",
            "'%s' delete ho jayega aur sab scenes se unlink hoga. Pakka?"
            % char.name)
        if ans != QMessageBox.StandardButton.Yes:
            return
        cid = char.id

        def _do():
            detach_character_everywhere(self.project, cid)
            self.project.characters.remove(cid)
        self._mutate_chars(_do)

    # -- per-scene visual regenerate --------------------------------------
    def _update_regen_button(self):
        s = self._selected_slot()
        running = (self._regen_thread is not None
                   and self._regen_thread.isRunning())
        key_ok = settings_store.has_api_key()
        # enabled purely on honest state, never a fake affordance
        self.btn_regen_scene.setEnabled(bool(s is not None and s.visual_path
                                             and key_ok and not running))
        if s is None:
            tip = "Pehle koi scene select karo"
        elif running:
            tip = "Regenerate chal rahi hai — thahro"
        elif not key_ok:
            tip = "API key nahi hai — Settings me key dalo"
        elif not s.visual_path:
            tip = ("Is scene me visual path nahi — regenerate ke liye pehle "
                   "Inspector me visual lagao")
        else:
            tip = ("Sirf is scene ki tasveer dobara banao (attached character "
                   "ki reference image ke sath). Baqi scenes nahi chherenge.")
        self.btn_regen_scene.setToolTip(tip)

    def _regen_scene(self):
        s = self._selected_slot()
        if s is None or not settings_store.has_api_key():
            return
        if not s.visual_path:
            QMessageBox.information(
                self, "Visual nahi hai",
                "Is scene me visual path nahi hai — pehle Inspector me "
                "visual lagao.")
            return
        self.btn_regen_scene.setEnabled(False)
        self.btn_regen_scene.setText("⟳ Ban rahi hai…")
        self._regen_thread = _SceneRegenThread(self.project, s.id, self)
        self._regen_thread.done.connect(self._regen_done)
        self._regen_thread.start()

    def _regen_done(self, ok, msg):
        self._regen_thread = None
        self.btn_regen_scene.setText("⟳ Regenerate scene visual")
        if ok:
            self.refresh()
            self._show_selected_in_monitor()
            self.project_changed.emit()
        else:
            QMessageBox.warning(self, "Regenerate fail", msg)
        self._update_regen_button()

    # ------------------------------------------------------------------
    # inspector
    # ------------------------------------------------------------------
    def _set_inspector_enabled(self, on):
        for w in (self.start_spin, self.end_spin, self.label_edit,
                  self.visual_edit, self.btn_browse, self.btn_clear_visual,
                  self.type_combo, self.trans_combo, self.motion_combo,
                  self.clip_in_spin, self.clip_out_spin, self.prompt_edit,
                  self.slot_chars_label):
            w.setEnabled(on)
        self._update_regen_button()

    def _refresh_inspector(self):
        s = self._selected_slot()
        self._loading = True
        try:
            if s is None:
                self.insp_title.setText("Inspector")
                self._set_inspector_enabled(False)
                self.slot_chars_label.setText("—")
                self.edit_why_label.setText("—")
                return
            idx = next((i for i, x in enumerate(self.project.slots) if x.id == s.id), 0)
            self.insp_title.setText("Inspector — slot #%d (%s)" % (idx + 1, s.id))
            self._set_inspector_enabled(True)
            self.start_spin.setValue(s.start_sec)
            self.end_spin.setValue(s.end_sec)
            self.label_edit.setText(s.label)
            self.visual_edit.setText(s.visual_path)
            self.type_combo.setCurrentText(s.visual_type)
            self.trans_combo.setCurrentText(s.transition)
            self.edit_why_label.setText(s.edit_notes or "—")
            self.motion_combo.setCurrentText(s.motion_preset)
            self.clip_in_spin.setValue(s.clip_in)
            self.clip_out_spin.setValue(s.clip_out)
            is_clip = (s.visual_type == "clip")
            self.clip_in_spin.setEnabled(is_clip)
            self.clip_out_spin.setEnabled(is_clip)
            if self.prompt_edit.toPlainText() != s.prompt:
                self.prompt_edit.setPlainText(s.prompt)
            names = [self.project.characters.get(c).name
                     for c in s.characters if self.project.characters.get(c)]
            self.slot_chars_label.setText(", ".join(names) if names else "—")
        finally:
            self._loading = False

    def _mutate(self, fn):
        """Checkpoint history, run a model mutation, refresh, notify."""
        if not self.project or not self.selected_id:
            return
        self.history.checkpoint(self.project)
        fn()
        self.refresh()
        self._show_selected_in_monitor()
        self.project_changed.emit()

    def _set_field(self, field, value):
        if self._loading:
            return

        def do():
            ok = set_slot_field(self.project, self.selected_id, field, value)
            # M5 honesty: a manual transition/motion override is recorded in
            # the "why" line so the Inspector never shows stale auto reasoning.
            if ok and field in ("transition", "motion_preset"):
                s = find_slot(self.project, self.selected_id)
                if s is not None:
                    s.edit_notes = "Manual choice: %s = %s (auto decision overridden)." % (field, value)
        self._mutate(do)

    def _set_visual_path(self, path):
        if self._loading:
            return

        def _do():
            set_slot_field(self.project, self.selected_id, "visual_path", path)
            if path:
                set_slot_field(self.project, self.selected_id, "visual_type",
                               _guess_type(path))
        self._mutate(_do)

    def _browse_visual(self):
        if self._loading or not self.selected_id:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Visual chuno", "", MEDIA_FILTER)
        if p:
            self._set_visual_path(p)

    def _apply_trim(self):
        if self._loading or not self.selected_id:
            return
        applied = [None]

        def _do():
            applied[0] = trim_slot(self.project, self.selected_id,
                                   self.start_spin.value(), self.end_spin.value())
        self._mutate(_do)
        # show the clamped values honestly
        if applied[0]:
            self._loading = True
            try:
                self.start_spin.setValue(applied[0][0])
                self.end_spin.setValue(applied[0][1])
            finally:
                self._loading = False

    def _apply_clip_range(self):
        if self._loading:
            return
        s = self._selected_slot()
        if s is None:
            return
        self.history.checkpoint(self.project)
        s.clip_in = max(0.0, self.clip_in_spin.value())
        out = self.clip_out_spin.value()
        s.clip_out = max(0.0, out)
        if s.clip_out and s.clip_out < s.clip_in:
            s.clip_out = s.clip_in  # clamp: out < in ka koi matlab nahi
            self._loading = True
            try:
                self.clip_out_spin.setValue(s.clip_out)
            finally:
                self._loading = False
        self.project_changed.emit()

    # ------------------------------------------------------------------
    # reorder / undo / redo / zoom
    # ------------------------------------------------------------------
    def _reorder(self, direction):
        if not self.selected_id:
            return
        self._mutate(lambda: reorder_slot(self.project, self.selected_id, direction))

    def _undo(self):
        if self.project and self.history.undo(self.project):
            # selection may point at a restored slot — ids survive, revalidate
            ids = {s.id for s in self.project.tracks["visuals"].slots}
            if self.selected_id not in ids:
                self.selected_id = ""
            self.refresh()
            self._show_selected_in_monitor()
            self.project_changed.emit()

    def _redo(self):
        if self.project and self.history.redo(self.project):
            ids = {s.id for s in self.project.tracks["visuals"].slots}
            if self.selected_id not in ids:
                self.selected_id = ""
            self.refresh()
            self._show_selected_in_monitor()
            self.project_changed.emit()

    def _on_zoom(self, val):
        self.timeline.set_zoom(float(val))

    def _update_edit_buttons(self):
        slots = self.project.slots if self.project else []
        idx = next((i for i, s in enumerate(slots) if s.id == self.selected_id), -1)
        has = idx >= 0
        self.btn_left.setEnabled(has and idx > 0)
        self.btn_right.setEnabled(has and idx < len(slots) - 1)
        self.btn_undo.setEnabled(self.history.can_undo())
        self.btn_redo.setEnabled(self.history.can_redo())
        if not has:
            self.btn_left.setToolTip("Pehle koi slot select karo")
            self.btn_right.setToolTip("Pehle koi slot select karo")
        else:
            self.btn_left.setToolTip("Selected slot ko ek step peeche karo")
            self.btn_right.setToolTip("Selected slot ko ek step aage karo")

    # ------------------------------------------------------------------
    # transport (stills step-through — NOT a video player)
    # ------------------------------------------------------------------
    def _update_transport(self):
        n = len(self.project.slots) if self.project else 0
        self.btn_play.setEnabled(n > 0)
        self.btn_stop.setEnabled(self._timer.isActive())
        total = self.project.total_duration if self.project else 0.0
        self.time_label.setText("%s / %s  •  %d slots" %
                                (fmt_time(self.timeline.playhead_sec),
                                 fmt_time(total), n))
        if self._timer.isActive():
            self.btn_play.setText("⏸ Pause")
            self.btn_play.setEnabled(True)
        else:
            self.btn_play.setText("▶ Play stills")

    def _toggle_play(self):
        if self._timer.isActive():
            self._timer.stop()
        else:
            if not self.project or not self.project.slots:
                return
            slots = self.project.slots
            # resume from selected slot if any
            idx = next((i for i, s in enumerate(slots) if s.id == self.selected_id), 0)
            self._play_idx = idx
            self._timer.start()
            self._step()
        self._update_transport()

    def _stop_play(self):
        if self._timer.isActive():
            self._timer.stop()
            self._update_transport()

    def _step(self):
        if not self.project or not self.project.slots:
            self._stop_play()
            return
        slots = self.project.slots
        s = slots[self._play_idx % len(slots)]
        self.selected_id = s.id
        self.timeline.select(s.id)
        self.timeline.set_playhead(s.start_sec)
        self._refresh_storyboard()
        self._refresh_inspector()
        self._show_selected_in_monitor()
        self._play_idx += 1
        self._update_transport()
