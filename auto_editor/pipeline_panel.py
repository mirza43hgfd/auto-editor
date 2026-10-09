"""Stage-wise pipeline control panel.

Stages: [Voiceover (+Preview)] -> [Auto Slots] -> [Reference Sheet] ->
[Photos] -> [Auto Assemble] -> [Render].
After each stage: Approve / Regenerate (Edit = manual tweaks in main window).
Mode toggle: FULL AUTO (script -> video, no stops) vs STEP-BY-STEP (approval
after every stage). Premium theme via assets/premium.qss.

Threading rules (hardened):
- Only ONE stage worker runs at a time (self._busy guard). All Start /
  Regenerate / Preview buttons are disabled while busy, so a second worker
  can never overwrite the first and a "zombie" thread can never deliver
  stale results.
- Every run gets a sequence number; _stage_done ignores results from an
  older run (late-finishing race after invalidation).
- Stage functions receive plain snapshot arguments (script text, tone, paths,
  previous results). They NEVER touch widgets or panel state from the worker
  thread — Qt widgets are GUI-thread only.
- Finished workers deleteLater() themselves (no C++ thread leak).
- Re-running an earlier stage discards later results + approvals (no stale
  state), and hides their Approve/Regenerate buttons.
- shutdown() waits for a running worker before the app closes; the main
  window should call it from closeEvent (see note in MainWindow).
"""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QTextEdit, QLineEdit, QComboBox, QFileDialog, QMessageBox, QProgressBar,
)
from PySide6.QtCore import QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices

from . import settings_store, ai_helper
from . import voiceover_studio as vs
from .timeline import Project, Slot
from .auto_pipeline import MOTION_CYCLE, auto_place_sfx


class StageWorker(QThread):
    """Runs one stage function off the GUI thread.

    done -> (ok: bool, payload: tuple) where payload is (run_seq, result|error).
    """

    done = Signal(bool, object)   # ok, (run_seq, result|error)
    tick = Signal(str)

    def __init__(self, fn, args=(), run_seq=0, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.args = args
        self.run_seq = run_seq

    def run(self):
        try:
            res = self.fn(self.tick.emit, *self.args)
            self.done.emit(True, (self.run_seq, res))
        except Exception as e:
            self.done.emit(False, (self.run_seq, str(e)[:500]))


STAGES = ["voiceover", "slots", "reference", "photos", "assemble", "render"]
STAGE_TITLES = {
    "voiceover": "1. Voiceover",
    "slots": "2. Auto Slots",
    "reference": "3. Reference Sheet",
    "photos": "4. Photos",
    "assemble": "5. Auto Assemble",
    "render": "6. Render",
}


class PipelinePanel(QWidget):
    project_ready = Signal(object)   # Project
    render_asked = Signal(object)    # Project -> main window renders
    log = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = {}          # stage results (fresh only)
        self.approved = set()    # stages the user approved
        self._busy = False       # a stage worker is running
        self._run_seq = 0        # generation counter for stale-result guard
        self._worker = None
        self._build()
        self._refresh_buttons()

    # -- UI -----------------------------------------------------------------
    def _build(self):
        lay = QVBoxLayout(self)

        head = QLabel("PIPELINE CONTROL")
        head.setObjectName("heading")
        lay.addWidget(head)

        form = QFormLayout()
        self.script_edit = QTextEdit()
        self.script_edit.setPlaceholderText("Script yahan likho/paste karo (Urdu/English)…")
        self.script_edit.setMaximumHeight(110)
        form.addRow("Script:", self.script_edit)

        self.tone_combo = QComboBox()
        self.tone_combo.addItems(list(vs.TONES.keys()))
        form.addRow("Tone:", self.tone_combo)

        self.brief_edit = QLineEdit()
        self.brief_edit.setPlaceholderText("Character brief: armor, cloak, horse, banner…")
        form.addRow("Character:", self.brief_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["STEP-BY-STEP", "FULL AUTO"])
        form.addRow("Mode:", self.mode_combo)

        workrow = QHBoxLayout()
        self.work_edit = QLineEdit()
        self.work_edit.setPlaceholderText("Project work folder…")
        wb = QPushButton("…")
        wb.setMaximumWidth(40)
        wb.clicked.connect(self._pick_work)
        workrow.addWidget(self.work_edit, 1)
        workrow.addWidget(wb)
        wrap = QWidget()
        wrap.setLayout(workrow)
        form.addRow("Folder:", wrap)
        lay.addLayout(form)

        # stage rows
        self.rows = {}
        for st in STAGES:
            row = QHBoxLayout()
            title = QLabel(STAGE_TITLES[st])
            title.setMinimumWidth(130)
            btn = QPushButton("Start")
            btn.setMaximumWidth(90)
            status = QLabel("—")
            status.setObjectName("muted")
            status.setWordWrap(True)
            approve = QPushButton("Approve")
            approve.setMaximumWidth(80)
            regen = QPushButton("Regenerate")
            regen.setMaximumWidth(100)
            approve.hide()
            regen.hide()
            row.addWidget(title)
            row.addWidget(btn)
            row.addWidget(status, 1)
            row.addWidget(approve)
            row.addWidget(regen)
            wrap = QWidget()
            wrap.setLayout(row)
            lay.addWidget(wrap)
            btn.clicked.connect(lambda _=False, s=st: self._run_stage(s))
            approve.clicked.connect(lambda _=False, s=st: self._approve(s))
            regen.clicked.connect(lambda _=False, s=st: self._run_stage(s))
            self.rows[st] = {"btn": btn, "status": status,
                             "approve": approve, "regen": regen}

        # preview tone button
        prow = QHBoxLayout()
        self.btn_preview = QPushButton("Preview tone (pehla chunk suno)")
        self.btn_preview.clicked.connect(self._preview_tone)
        prow.addWidget(self.btn_preview)
        prow.addStretch(1)
        lay.addLayout(prow)

        self.bar = QProgressBar()
        self.bar.setMaximum(0)   # busy indicator
        self.bar.hide()
        lay.addWidget(self.bar)
        lay.addStretch(1)

    def _pick_work(self):
        d = QFileDialog.getExistingDirectory(self, "Project work folder")
        if d:
            self.work_edit.setText(d)

    # -- inputs (GUI thread only; results are plain snapshots) ----------------
    def _inputs_ok(self):
        """Validate; returns snapshot dict or None (with a dialog)."""
        script = self.script_edit.toPlainText().strip()
        if not script:
            QMessageBox.information(self, "Script", "Pehle script likho.")
            return None
        work = self.work_edit.text().strip()
        if not work:
            QMessageBox.information(self, "Folder", "Work folder chuno.")
            return None
        if not settings_store.has_api_key():
            QMessageBox.information(self, "API key", "Settings me Gemini API key dalo.")
            return None
        return {
            "script": script,
            "tone": self.tone_combo.currentText(),
            "brief": self.brief_edit.text().strip(),
            "work": work,
        }

    def _set_status(self, st, text):
        self.rows[st]["status"].setText(text)

    def _set_all_buttons_enabled(self, enabled):
        for st in STAGES:
            self.rows[st]["btn"].setEnabled(enabled)
            self.rows[st]["regen"].setEnabled(enabled)
        self.btn_preview.setEnabled(enabled)

    def _refresh_buttons(self):
        order = STAGES
        for i, st in enumerate(order):
            can = (i == 0) or (order[i - 1] in self.approved)
            locked = st in self.approved
            # Start: only when previous stage approved, not approved itself,
            # and nothing running. Approve/Regen visible only with a fresh result.
            self.rows[st]["btn"].setEnabled(can and not locked and not self._busy)
            has_result = st in self.state and st not in self.approved
            self.rows[st]["approve"].setVisible(has_result)
            self.rows[st]["regen"].setVisible(has_result and not self._busy)
        self.btn_preview.setEnabled(not self._busy)

    # -- stage flow -----------------------------------------------------------
    def _invalidate_from(self, idx):
        """Re-running stage idx: drop later results + approvals (no stale state)."""
        for st in STAGES[idx:]:
            self.state.pop(st, None)
            self.approved.discard(st)
            self._set_status(st, "—")

    def _run_stage(self, st):
        if self._busy:
            self.log.emit("Ek stage pehle se chal raha hai — ruko.")
            return
        snap = self._inputs_ok()
        if snap is None:
            return
        idx = STAGES.index(st)
        # a fresh run of an earlier stage invalidates everything from it on
        self._invalidate_from(idx)

        # previous-stage results are passed as plain snapshot args
        # (worker thread must never touch widgets/panel state)
        try:
            args = self._stage_args(st, snap)
        except KeyError as e:
            self._set_status(st, "Pehle previous stage complete karo.")
            self.log.emit("Stage %s: missing input %s" % (st, e))
            return

        self._busy = True
        self._run_seq += 1
        seq = self._run_seq
        self._set_status(st, "Chal raha hai…")
        self._set_all_buttons_enabled(False)
        self.bar.show()
        fn = getattr(self, "_stage_" + st)
        self._worker = StageWorker(fn, args, run_seq=seq, parent=None)
        self._worker.tick.connect(lambda m: self._set_status(st, m))
        self._worker.done.connect(
            lambda ok, payload, s=st: self._stage_done(s, ok, payload))
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _stage_args(self, st, snap):
        """Snapshot arguments for each stage (GUI thread)."""
        if st == "voiceover":
            return (snap["script"], snap["tone"], snap["work"])
        if st == "slots":
            return (self.state["voiceover"],)
        if st == "reference":
            return (snap["brief"], snap["work"])
        if st == "photos":
            return (self.state["slots"], self.state.get("reference", ""),
                    snap["work"])
        if st == "assemble":
            return (snap["script"], snap["tone"], self.state["voiceover"],
                    self.state["slots"], self.state["photos"])
        if st == "render":
            return (self.state["assemble"],)
        raise KeyError(st)

    def _stage_done(self, st, ok, payload):
        seq, res = payload
        if seq != self._run_seq:
            return  # stale result from an invalidated run — ignore
        self._busy = False
        self._worker = None
        self.bar.hide()
        if not ok:
            self._set_status(st, "FAIL: %s" % str(res)[:120])
            self.log.emit("Stage %s fail: %s" % (st, res))
            self._refresh_buttons()
            return
        self.state[st] = res
        self._set_status(st, "Ho gaya ✓")
        self.log.emit("Stage '%s' complete." % st)
        self._refresh_buttons()
        if self.mode_combo.currentText() == "FULL AUTO" and st not in self.approved:
            self._approve(st)  # auto-approve, chain forward

    def _approve(self, st):
        self.approved.add(st)
        self._set_status(st, "Approved ✓")
        self._refresh_buttons()
        nxt = STAGES.index(st) + 1
        if self.mode_combo.currentText() == "FULL AUTO" and nxt < len(STAGES):
            self._run_stage(STAGES[nxt])
        elif st == "assemble":
            self.project_ready.emit(self.state["assemble"])

    def shutdown(self, timeout_ms=15000):
        """Wait for a running stage worker before the app closes.

        The main window should call this from closeEvent and only then accept
        the close — destroying a running QThread aborts the process.
        Returns True when no worker is left running.
        """
        w = self._worker
        if w is not None and w.isRunning():
            self.log.emit("Stage khatam hone ka wait…")
            w.wait(timeout_ms)
            return not w.isRunning()
        return True

    # -- stages (run on the WORKER thread; args are plain snapshots) ----------
    def _stage_voiceover(self, tick, script, tone, work):
        from . import voiceover_studio as vsm
        tick("Voiceover ban rahi hai…")
        ok, res = vsm.generate_voiceover(
            script, tone, work,
            progress_cb=lambda ph, d, t, m: tick(m))
        if not ok:
            raise RuntimeError(res)
        mp3, chunks = res
        return {"mp3": mp3, "chunks": chunks}

    def _stage_slots(self, tick, voiceover):
        slots = []
        for ch in voiceover["chunks"]:
            slots.append({"start_sec": ch["start"], "end_sec": ch["end"],
                          "label": ch["text"][:80]})
        tick("%d exact slots." % len(slots))
        return slots

    def _stage_reference(self, tick, brief, work):
        media = os.path.join(work, "media")
        os.makedirs(media, exist_ok=True)
        ref = os.path.join(media, "reference_sheet.png")
        tick("Reference sheet ban rahi hai…")
        ok, res = ai_helper.generate_reference_sheet(brief or "epic warrior", ref)
        if not ok:
            raise RuntimeError(res)
        return ref

    def _stage_photos(self, tick, slots, reference, work):
        media = os.path.join(work, "media")
        os.makedirs(media, exist_ok=True)
        photos = []
        for i, s in enumerate(slots):
            tick("Photo %d/%d…" % (i + 1, len(slots)))
            ok, prompt = ai_helper.suggest_visual_prompt(s["label"])
            prompt = prompt if ok else s["label"]
            img = os.path.join(media, "slot_%03d.png" % (i + 1))
            ok, res = ai_helper.generate_image(prompt, img,
                                               reference_image_path=reference or None)
            if not ok:
                raise RuntimeError("Photo %d: %s" % (i + 1, res))
            photos.append({"path": img, "prompt": prompt})
        return photos

    def _stage_assemble(self, tick, script, tone, voiceover, slots, photos):
        project = Project("Auto (%s)" % tone)
        project.tracks["narration"].media_path = os.path.abspath(voiceover["mp3"])
        for i, (s, ph) in enumerate(zip(slots, photos)):
            project.add_slot(Slot(
                s["start_sec"], s["end_sec"], visual_path=ph["path"],
                visual_type="photo", motion_preset=MOTION_CYCLE[i % len(MOTION_CYCLE)],
                transition="xfade", prompt=ph["prompt"], label=s["label"]))
        sfx_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "assets", "sfx"))
        n = auto_place_sfx(project, script, sfx_dir)
        tick("Assemble ho gaya (%d SFX)." % n)
        return project

    def _stage_render(self, tick, project):
        self.render_asked.emit(project)
        return True

    # -- tone preview -----------------------------------------------------------
    def _preview_tone(self):
        if self._busy:
            self.log.emit("Ek stage pehle se chal raha hai — ruko.")
            return
        snap = self._inputs_ok()
        if snap is None:
            return
        self._busy = True
        self._run_seq += 1
        seq = self._run_seq
        self._set_all_buttons_enabled(False)
        self._set_status("voiceover", "Preview ban raha hai…")

        def fn(tick):
            ok, res = vs.preview_chunk(snap["script"], snap["tone"], snap["work"])
            if not ok:
                raise RuntimeError(res)
            return res

        self._worker = StageWorker(fn, (), run_seq=seq, parent=None)
        self._worker.done.connect(
            lambda ok, payload: self._preview_done(ok, payload))
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _preview_done(self, ok, payload):
        seq, res = payload
        if seq != self._run_seq:
            return  # stale preview — ignore
        self._busy = False
        self._worker = None
        self._refresh_buttons()
        if ok:
            self._set_status("voiceover", "Preview tayar — sun lo.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(res))
        else:
            self._set_status("voiceover", "Preview fail.")
            QMessageBox.warning(self, "Preview", str(res)[:300])
