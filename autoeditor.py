"""Repo-root launcher for the PyInstaller build (Mdola pattern).

Why this file exists: PyInstaller runs its entry script as __main__ with no
parent package. app.py uses relative imports throughout
(`from . import settings_store`, ...), so naming `auto_editor/app.py` as the
PyInstaller entry would crash the shipped EXE at startup with
"ImportError: attempted relative import with no known parent package".
This launcher imports the package properly and starts the GUI, so the frozen
bundle resolves every relative import correctly.

CI entry points:
  python -m auto_editor.app --smoke   (headless import check)
  python autoeditor.py                (GUI, dev runs)
"""

from auto_editor.app import main

if __name__ == "__main__":
    main()
