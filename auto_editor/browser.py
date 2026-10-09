"""Embedded browser with a persistent profile.

One-time login (Google/Flow): cookies + storage live in <app-data>/browser_profile
and survive restarts, so the user is not asked to log in again.
Phase 1: window + profile scaffolding + session persistence + logout button.
No automation yet.

If QtWebEngine is unavailable (PySide6-WebEngine not installed), the window
shows a clear message instead of crashing.

Qt6 notes (verified against Qt 6 docs):
- QWebEngineProfile("name", parent): the name must be unique per process, so
  the profile is created ONCE per process (module-level singleton). Opening
  the browser window twice reuses it instead of creating a conflicting twin.
- Persistence = setPersistentStoragePath + setCachePath + setHttpCacheType(
  DiskHttpCache) + setPersistentCookiesPolicy(ForcePersistentCookies).
  ForcePersistentCookies keeps even session cookies across restarts, which is
  what makes "stay logged in" actually work.
- Page is built as QWebEnginePage(profile, view) + view.setPage(page), the
  documented pattern for attaching a custom profile to a view.
- There is no QWebEngineSettings.WebAttribute.PersistentStorageEnabled in Qt6
  (that name belongs to the old QtWebKit API) — persistence comes from the
  profile calls above, not from a settings attribute.
"""

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    WEBENGINE_AVAILABLE = True
except ImportError:
    WEBENGINE_AVAILABLE = False

from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget, QLabel, QLineEdit, QToolBar, QMessageBox,
)
from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction

from . import settings_store

PROFILE_NAME = "autoeditor"

# One profile per process — Qt requires unique profile names and a profile's
# storage path must not be shared by two live profiles.
_SHARED_PROFILE = None


def get_shared_profile():
    """Process-wide persistent profile, created on first use."""
    global _SHARED_PROFILE
    if _SHARED_PROFILE is None:
        _SHARED_PROFILE = QWebEngineProfile(PROFILE_NAME)
        pdir = settings_store.browser_profile_dir()
        _SHARED_PROFILE.setPersistentStoragePath(pdir)
        _SHARED_PROFILE.setCachePath(pdir + "/cache")
        _SHARED_PROFILE.setHttpCacheType(
            QWebEngineProfile.HttpCacheType.DiskHttpCache)
        _SHARED_PROFILE.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    return _SHARED_PROFILE


class BrowserWindow(QMainWindow):
    def __init__(self, parent=None, start_url="https://www.google.com"):
        super().__init__(parent)
        self.setWindowTitle("Auto-Editor Browser — session stays logged in")
        self.resize(1100, 750)
        self._start_url = start_url
        self.view = None

        if not WEBENGINE_AVAILABLE:
            msg = QLabel(
                "QtWebEngine is not installed.\n"
                "The embedded browser needs the PySide6-WebEngine package.\n"
                "Everything else in the app works without it.")
            msg.setObjectName("heading")
            msg.setWordWrap(True)
            self.setCentralWidget(msg)
            return

        self.profile = get_shared_profile()

        self.view = QWebEngineView(self)
        page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(page)

        self.urlbar = QLineEdit(self)
        self.urlbar.setPlaceholderText("URL likho, Enter dabao…")
        self.urlbar.returnPressed.connect(self._go_urlbar)

        toolbar = QToolBar("nav", self)
        for label, slot in (("Back", self.view.back), ("Fwd", self.view.forward),
                            ("Reload", self.view.reload)):
            a = QAction(label, self)
            a.triggered.connect(slot)
            toolbar.addAction(a)
        toolbar.addSeparator()
        logout = QAction("Logout (session saaf karo)", self)
        logout.triggered.connect(self._clear_session)
        toolbar.addAction(logout)
        self.addToolBar(toolbar)

        wrap = QWidget(self)
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.urlbar)
        lay.addWidget(self.view)
        self.setCentralWidget(wrap)

        self.view.urlChanged.connect(
            lambda u: self.urlbar.setText(u.toString()))
        self.view.setUrl(QUrl(self._start_url))

    def _go_urlbar(self):
        if self.view is None:
            return
        text = self.urlbar.text().strip()
        if text and "://" not in text:
            text = "https://" + text
        if text:
            self.view.setUrl(QUrl(text))

    def _clear_session(self):
        """Genuine logout: wipe cookies + HTTP cache + visited links."""
        if self.view is None:
            return
        ans = QMessageBox.question(
            self, "Logout",
            "Browser ki sari login session saaf ho jayegi (cookies, cache).\n"
            "Dobara login karna parega. Pakka?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self.profile.clearAllVisitedLinks()
        self.view.setUrl(QUrl(self._start_url))
        self.urlbar.setText(self._start_url)

    def open_url(self, url):
        if self.view is not None:
            self.view.setUrl(QUrl(url))
