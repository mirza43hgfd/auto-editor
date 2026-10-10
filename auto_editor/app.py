"""Auto-Editor — PySide6 main window.

Dark premium UI (assets/premium.qss). Two views:
  Workspace (M3, primary) — editing-first: storyboard, preview monitor,
    properties inspector, multitrack timeline with undo/redo.
  Classic — Phase 1 layout: media bin, pipeline panel, slot table, render bar.
Media bin with drag-drop, embedded browser, settings with the API key pool,
full-auto pipeline entry, background render with progress.
"""

import os
import sys
import traceback

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QTableWidget, QTableWidgetItem, QPushButton, QProgressBar,
    QTextEdit, QFileDialog, QMenuBar, QDialog, QFormLayout, QLineEdit,
    QLabel, QDialogButtonBox, QMessageBox, QInputDialog, QHeaderView, QTabWidget,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices

from . import settings_store
from .timeline import Project, Slot
from .media import import_files, scan_watch_folder, export_slot_prompts
from .render_worker import render_project, RenderCancelled
from . import ai_helper
from .pipeline_panel import PipelinePanel
from .workspace import MainWorkspace

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))


# --------------------------------------------------------------------------
# background render thread
# --------------------------------------------------------------------------

class RenderThread(QThread):
    progress = Signal(str, int, int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, proj_dict, media_dir, out_mp4, parent=None):
        super().__init__(parent)
        self.proj_dict = proj_dict
        self.media_dir = media_dir
        self.out_mp4 = out_mp4
        self._cancel = False

    def request_cancel(self):
        """Ask the render to stop at the next chunk boundary (resume-safe)."""
        self._cancel = True

    def run(self):
        try:
            render_project(self.proj_dict, self.media_dir, self.out_mp4,
                           progress_cb=lambda ph, d, t: self.progress.emit(ph, d, t),
                           cancel_cb=lambda: self._cancel)
            if self._cancel:
                self.failed.emit("Render cancelled — finished chunks cached, dobara chalao to continue hoga.")
            else:
                self.finished_ok.emit(self.out_mp4)
        except RenderCancelled:
            self.failed.emit("Render cancelled — finished chunks cached, dobara chalao to continue hoga.")
        except Exception as e:
            self.failed.emit("%s\n%s" % (e, traceback.format_exc(limit=3)))


class AutoThread(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, vo_mp3, brief, work_dir, parent=None):
        super().__init__(parent)
        self.vo_mp3 = vo_mp3
        self.brief = brief
        self.work_dir = work_dir

    def run(self):
        try:
            from .auto_pipeline import run_auto_pipeline
            ok, res = run_auto_pipeline(
                self.vo_mp3, self.brief, self.work_dir,
                progress_cb=lambda st, d, t, m: None)
            if ok:
                self.finished_ok.emit(res)
            else:
                self.failed.emit(res)
        except Exception as e:
            self.failed.emit("%s\n%s" % (e, traceback.format_exc(limit=3)))


# --------------------------------------------------------------------------
# settings dialog — API key pool with status dots
# --------------------------------------------------------------------------

DOT = {"ok": "🟢", "limited": "🟠", "failed": "🔴"}


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings — Gemini API key pool")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Apni Gemini API keys (AI Studio se). "
                             "Quota khatam ho to agali key khud lag jayegi."))
        form = QFormLayout()
        self.edits = []
        slots = settings_store.get_api_key_slots()
        for i in range(settings_store.MAX_KEYS):
            row = QHBoxLayout()
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setText(slots[i])
            edit.setPlaceholderText("Key %d — khaali chhor do agar nahi hai" % (i + 1))
            st = settings_store.get_key_status(i)["status"]
            dot = QLabel(DOT.get(st, "🟢"))
            row.addWidget(edit, 1)
            row.addWidget(dot)
            wrap = QWidget()
            wrap.setLayout(row)
            form.addRow("Key %d:" % (i + 1), wrap)
            self.edits.append(edit)
        lay.addLayout(form)
        note = QLabel("Keys sirf is PC par obfuscated save hoti hain — "
                      "kabhi log ya print me nahi aatin.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def accept(self):
        settings_store.set_api_key_slots([e.text().strip() for e in self.edits])
        super().accept()


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto-Editor — documentary assembler")
        self.resize(1280, 800)
        self.project = Project("Untitled")
        self.project_path = ""
        self.media_files = []  # list of (path, type)

        self._build_ui()
        self._build_menus()
        self._load_theme()

        # auto-save every 2 minutes
        self._autosave = QTimer(self)
        self._autosave.timeout.connect(self._auto_save)
        self._autosave.start(120_000)

        # M7: crash recovery — offer Resume for unfinished jobs (never auto-start).
        QTimer.singleShot(800, self._check_job_recovery)

    # -- M7 job recovery ------------------------------------------------------
    def _check_job_recovery(self):
        """Scan recent work dirs for unfinished job manifests; offer Resume."""
        try:
            from . import jobs
            dirs = settings_store.get("recent_work_dirs", []) or []
            dirs = [d for d in dirs if d and os.path.isdir(d)]
            found = jobs.scan_incomplete(dirs)
        except Exception:
            return
        if not found:
            return
        lines = []
        for f in found[:5]:
            s = f["summary"]
            done = sum(1 for v in s.values() if v["status"] == "done")
            stale = " [stale — lagta hai app crash hui thi]" if f["stale"] else ""
            lines.append("• %s\n    %s — %d/%d stages%s"
                         % (f["work_dir"], f["job_status"], done, len(s), stale))
        box = QMessageBox(self)
        box.setWindowTitle("Adhoora kaam mila")
        box.setText("Pichli dafa ye kaam adhoora reh gaya tha:\n\n"
                    + "\n".join(lines)
                    + "\n\nResume karna hai? (khud start nahi hoga — "
                      "tum Pipeline me 'Resume' dabaoge)")
        btn_resume = box.addButton("Resume", QMessageBox.AcceptRole)
        box.addButton("Baad me", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == btn_resume:
            self.pipeline.work_edit.setText(found[0]["work_dir"])
            self._log("Recovery: work folder set — Pipeline tab me stage ka "
                      "'Resume' dabao; progress manifest se wapas ayegi.")

    # -- UI ---------------------------------------------------------------
    def _load_theme(self):
        qss = os.path.join(ASSETS, "premium.qss")
        if os.path.isfile(qss):
            with open(qss, encoding="utf-8") as f:
                self.setStyleSheet(f.read())

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)

        title = QLabel("Auto-Editor <span style='color:#c9a227'>workspace</span>")
        title.setObjectName("heading")
        lay.addWidget(title)

        self.view_tabs = QTabWidget()
        lay.addWidget(self.view_tabs, 1)

        # M3 editing workspace — the primary view
        self.workspace = MainWorkspace()
        self.workspace.project_changed.connect(self._workspace_changed)
        self.workspace.set_project(self.project)
        self.view_tabs.addTab(self.workspace, "Workspace")

        # Classic view — Phase 1 layout, kept fully working
        classic = QWidget()
        clay = QVBoxLayout(classic)
        clay.setContentsMargins(0, 0, 0, 0)

        split = QSplitter(Qt.Orientation.Horizontal)

        # left tabs: media bin | pipeline control
        tabs = QTabWidget()

        # media bin (drag-drop)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Media bin — files yahan drag-drop karo"))
        self.bin = QListWidget()
        self.bin.setAcceptDrops(True)
        self.bin.dragEnterEvent = self._drag_enter
        self.bin.dropEvent = self._drop
        ll.addWidget(self.bin, 1)
        row = QHBoxLayout()
        b_imp = QPushButton("Import files")
        b_imp.clicked.connect(self._import_dialog)
        b_scan = QPushButton("Scan watch folder")
        b_scan.clicked.connect(self._scan_watch)
        row.addWidget(b_imp)
        row.addWidget(b_scan)
        ll.addLayout(row)
        tabs.addTab(left, "Media")

        # pipeline control panel (script -> video stages)
        self.pipeline = PipelinePanel()
        self.pipeline.log.connect(self._log)
        self.pipeline.project_ready.connect(self._pipeline_project)
        self.pipeline.render_asked.connect(self._render_project_obj)
        tabs.addTab(self.pipeline, "Pipeline")
        split.addWidget(tabs)

        # slot table (simple list; graphical timeline = Phase 2)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("Slots — har slot = 1 visual (narration line par)"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Start", "End", "Visual", "Type", "Motion"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        rl.addWidget(self.table, 1)

        brow = QHBoxLayout()
        b_add = QPushButton("Add slot")
        b_add.clicked.connect(self._add_slot)
        b_del = QPushButton("Remove slot")
        b_del.clicked.connect(self._del_slot)
        brow.addWidget(b_add)
        brow.addWidget(b_del)
        rl.addLayout(brow)

        # render bar
        rrow = QHBoxLayout()
        self.btn_render = QPushButton("RENDER 1080p")
        self.btn_render.setObjectName("accent")
        self.btn_render.clicked.connect(self._render)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_render)
        self.prog = QProgressBar()
        rrow.addWidget(self.btn_render)
        rrow.addWidget(self.btn_cancel)
        rrow.addWidget(self.prog, 1)
        rl.addLayout(rrow)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        rl.addWidget(self.log)
        split.addWidget(right)
        split.setSizes([320, 960])
        clay.addWidget(split, 1)
        self.view_tabs.addTab(classic, "Classic")
        self.statusBar().showMessage("Ready")

    def _workspace_changed(self):
        # a workspace edit mutated self.project in place — keep the
        # classic slot table in sync (rebind is a no-op for same object).
        self._refresh_table()

    def _build_menus(self):
        mb = self.menuBar()
        m_file = mb.addMenu("File")
        for label, fn in (("New project", self._new),
                          ("Open .deproj", self._open),
                          ("Save .deproj", self._save)):
            a = m_file.addAction(label)
            a.triggered.connect(fn)
        m_tools = mb.addMenu("Tools")
        for label, fn in (("Embedded browser", self._open_browser),
                          ("Export slot prompts (md)", self._export_prompts),
                          ("AI: suggest slot splits", self._ai_slots),
                          ("AI: suggest visual prompt", self._ai_prompt)):
            a = m_tools.addAction(label)
            a.triggered.connect(fn)
        m_auto = mb.addMenu("Auto mode")
        a = m_auto.addAction("Full-auto from voiceover MP3…")
        a.triggered.connect(self._auto_mode)
        m_set = mb.addMenu("Settings")
        a = m_set.addAction("API keys…")
        a.triggered.connect(lambda: SettingsDialog(self).exec())

    # -- media bin ----------------------------------------------------------
    def _drag_enter(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def _drop(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls()]
        self._add_media(paths)

    def _import_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import media", "",
            "Media (*.mp3 *.wav *.m4a *.jpg *.jpeg *.png *.webp *.mp4 *.mov *.mkv)")
        self._add_media(paths)

    def _add_media(self, paths):
        for p, t in import_files(paths):
            if p not in [m[0] for m in self.media_files]:
                self.media_files.append((p, t))
                self.bin.addItem("[%s] %s" % (t, os.path.basename(p)))
        self._log("%d file(s) imported." % len(paths))

    def _scan_watch(self):
        folder = self.project.settings.get("watch_folder") or \
            QFileDialog.getExistingDirectory(self, "Watch folder (Flow output)")
        if not folder:
            return
        self.project.settings["watch_folder"] = folder
        known = [m[0] for m in self.media_files]
        new = scan_watch_folder(folder, known)
        self._add_media(new)
        self._log("Watch folder: %d nayi file(s)." % len(new))

    # -- slots ---------------------------------------------------------------
    def _refresh_table(self):
        slots = self.project.slots
        self.table.setRowCount(len(slots))
        for r, s in enumerate(slots):
            vals = ["%.1f" % s.start_sec, "%.1f" % s.end_sec,
                    os.path.basename(s.visual_path) or "—",
                    s.visual_type, s.motion_preset]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        # keep the workspace bound to the current project object
        # (_new/_open/pipeline replace it; workspace edits mutate in place)
        self.workspace.set_project(self.project)

    def _add_slot(self):
        start, ok = QInputDialog.getDouble(self, "Slot", "Start (sec):", 0, 0, 99999, 1)
        if not ok:
            return
        end, ok = QInputDialog.getDouble(self, "Slot", "End (sec):", start + 10, 0, 99999, 1)
        if not ok:
            return
        self.project.add_slot(Slot(start, end))
        self._refresh_table()

    def _del_slot(self):
        r = self.table.currentRow()
        if r >= 0:
            self.project.tracks["visuals"].slots.remove(self.project.slots[r])
            self._refresh_table()

    # -- project file ---------------------------------------------------------
    def _new(self):
        self.project = Project("Untitled")
        self.project_path = ""
        self._refresh_table()

    def _open(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open project", "", "*.deproj")
        if p:
            self.project = Project.load(p)
            self.project_path = p
            self._refresh_table()
            self._log("Opened " + p)

    def _save(self):
        p = self.project_path or QFileDialog.getSaveFileName(
            self, "Save project", "", "*.deproj")[0]
        if p:
            if not p.endswith(".deproj"):
                p += ".deproj"
            self.project.save(p)
            self.project_path = p
            self._log("Saved " + p)

    def _auto_save(self):
        if self.project_path:
            try:
                self.project.save(self.project_path)
                self.statusBar().showMessage("Auto-saved", 3000)
            except Exception as e:
                self._log("Auto-save fail: %s" % e)

    # -- tools ------------------------------------------------------------------
    def _open_browser(self):
        from .browser import BrowserWindow, WEBENGINE_AVAILABLE
        if not WEBENGINE_AVAILABLE:
            QMessageBox.information(self, "Browser",
                                    "QtWebEngine installed nahi hai — baqi app kaam karegi.")
            return
        old = getattr(self, "_browser", None)
        if old is not None:
            try:
                old.show()
                old.raise_()
                old.activateWindow()
                return
            except RuntimeError:
                pass  # window was closed; make a new one
        self._browser = BrowserWindow(self)
        self._browser.show()

    def _export_prompts(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export prompts", "", "*.md")
        if p:
            export_slot_prompts(self.project, p)
            self._log("Prompts exported: " + p)

    def _need_key(self):
        if not settings_store.has_api_key():
            QMessageBox.information(self, "API key",
                                    "Pehle Settings me Gemini API key dalo.")
            return False
        return True

    def _ai_slots(self):
        if not self._need_key():
            return
        text, ok = QInputDialog.getMultiLineText(self, "Script", "Script text:")
        if not ok or not text.strip():
            return
        self._log("AI slot suggestions…")
        ok, res = ai_helper.suggest_slot_splits(text)
        if not ok:
            QMessageBox.warning(self, "AI", res)
            return
        for s in res:
            self.project.add_slot(Slot(s["start_sec"], s["end_sec"], label=s["label"]))
        self._refresh_table()
        self._log("%d slots add hue." % len(res))

    def _ai_prompt(self):
        if not self._need_key():
            return
        text, ok = QInputDialog.getMultiLineText(self, "Scene", "Scene describe karo:")
        if not ok or not text.strip():
            return
        ok, res = ai_helper.suggest_visual_prompt(text)
        QMessageBox.information(self, "Visual prompt", res if ok else ("Fail: " + res))

    def _auto_mode(self):
        if getattr(self, "_athread", None) is not None and self._athread.isRunning():
            QMessageBox.information(self, "Auto", "Auto pipeline pehle se chal raha hai.")
            return
        vo, _ = QFileDialog.getOpenFileName(self, "Voiceover MP3", "", "*.mp3 *.wav")
        if not vo:
            return
        brief, ok = QInputDialog.getMultiLineText(
            self, "Character", "Main character ka brief (armor, cloak, horse…):")
        if not ok:
            return
        work = QFileDialog.getExistingDirectory(self, "Project work folder")
        if not work:
            return
        self._log("FULL AUTO start — thora waqt lagega (API quota tumhara lagega)…")
        self._athread = AutoThread(vo, brief, work, self)
        self._athread.finished_ok.connect(self._auto_done)
        self._athread.failed.connect(lambda m: self._log("AUTO FAIL: " + m))
        self._athread.start()

    def _auto_done(self, project):
        self.project = project
        self._refresh_table()
        self._log("Auto complete — timeline check karo, phir RENDER dabao.")

    # -- pipeline ---------------------------------------------------------------
    def _pipeline_project(self, project):
        self.project = project
        self._refresh_table()
        self._log("Pipeline project timeline me khul gaya — check/edit karo.")

    def _render_project_obj(self, project):
        # FULL AUTO path: no questions, output goes next to the project.
        self.project = project
        self._refresh_table()
        base = os.path.dirname(self.project_path) if self.project_path else os.getcwd()
        # M7: the pipeline manifest already has render=running (panel stage);
        # a direct main-window render also records it when a work dir is known.
        try:
            from .jobs import JobStore
            w = self.pipeline.work_edit.text().strip()
            if w and os.path.isdir(w):
                JobStore(w).start_stage("render")
        except Exception:
            pass
        self._render(out_path=os.path.join(base, "final.mp4"))

    # -- render -------------------------------------------------------------------
    def _render(self, out_path=None):
        if not self.project.slots:
            QMessageBox.information(self, "Render", "Pehle slots banao.")
            return
        if getattr(self, "_rthread", None) is not None and self._rthread.isRunning():
            QMessageBox.information(self, "Render", "Render pehle se chal raha hai.")
            return
        out = out_path
        if out is None:
            out, _ = QFileDialog.getSaveFileName(self, "Output MP4", "", "*.mp4")
            if not out:
                return
        if not out.endswith(".mp4"):
            out += ".mp4"
        media_dir = os.path.dirname(self.project_path) if self.project_path else os.getcwd()
        self.btn_render.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._rthread = RenderThread(self.project.to_dict(), media_dir, out, self)
        self._rthread.progress.connect(self._on_progress)
        self._rthread.finished_ok.connect(self._on_done)
        self._rthread.failed.connect(self._on_fail)
        self._rthread.start()
        self._log("Render start…")

    def _cancel_render(self):
        if getattr(self, "_rthread", None) is not None and self._rthread.isRunning():
            self._rthread.request_cancel()
            self._log("Cancel manga — current chunk ke baad rukega…")
            self.btn_cancel.setEnabled(False)

    def _on_progress(self, phase, done, total):
        pct = int(100 * done / max(total, 1))
        self.prog.setValue(pct)
        self.statusBar().showMessage("%s %d/%d" % (phase, done, total))

    def _on_done(self, path):
        self.btn_render.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.prog.setValue(100)
        self._log("DONE: " + path)
        # M7: render stage complete in the pipeline manifest (if any).
        try:
            from .jobs import JobStore
            w = self.pipeline.work_edit.text().strip()
            if w and os.path.isdir(w):
                JobStore(w).complete_stage("render")
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def _on_fail(self, msg):
        self.btn_render.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._log("RENDER FAIL:\n" + msg)
        # M7: user-cancelled render -> paused (chunks are resume-safe);
        # real render errors -> failed with the message.
        try:
            from .jobs import JobStore
            w = self.pipeline.work_edit.text().strip()
            if w and os.path.isdir(w):
                store = JobStore(w)
                if "cancel" in msg.lower():
                    store.pause_job("Render cancel kiya gaya.",
                                    "Render dobara chalao — chunks resume-safe hain.")
                else:
                    store.fail_stage("render", "transient", str(msg)[:300],
                                     "Render dobara chalao — chunks wahin se resume honge.",
                                     retryable=True)
        except Exception:
            pass

    def _log(self, msg):
        self.log.append(msg)

    def closeEvent(self, event):
        # Never destroy a running worker — wait for it first.
        try:
            if not self.pipeline.shutdown(15000):
                self._log("Stage worker still running; close forced.")
        except Exception:
            pass
        # Render/auto threads: ask, then stop cleanly (render cancel is
        # resume-safe; auto pipeline is terminated mid-call).
        rt = getattr(self, "_rthread", None)
        at = getattr(self, "_athread", None)
        busy = ((rt is not None and rt.isRunning()) or
                (at is not None and at.isRunning()))
        if busy:
            ans = QMessageBox.question(
                self, "Band karein?",
                "Kaam chal raha hai — band karne se ye ruk jayega. Phir bhi band karein?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                event.ignore()
                return
            try:
                if rt is not None and rt.isRunning():
                    rt.request_cancel()
                    rt.wait(45000)
                if at is not None and at.isRunning():
                    at.terminate()
                    at.wait(5000)
            except Exception:
                pass
        super().closeEvent(event)


def main():
    if "--smoke" in sys.argv:
        # headless import + model test (used by CI smoke test)
        p = Project("smoke")
        p.add_slot(Slot(0, 5, visual_path="x.png"))
        assert p.total_duration == 5
        from . import ai_helper, auto_pipeline, browser  # noqa
        print("SMOKE OK")
        return
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
