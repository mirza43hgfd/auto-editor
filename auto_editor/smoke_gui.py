"""Windows CI smoke — imports every Qt-bearing module (v2.0.1 regression gate).

The v2.0 EXE shipped with an ImportError (pipeline_panel importing the deleted
MOTION_CYCLE) because the headless smoke test never imported the Qt modules.
This module imports them all — no widgets are constructed, no display needed
beyond what PySide6 bundles on Windows (QT_QPA_PLATFORM=offscreen).

Run on Windows CI:  python -m auto_editor.smoke_gui
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import pipeline_panel  # noqa: F401  (v2.0 ImportError regression)
from . import app  # noqa: F401
from . import workspace  # noqa: F401
from . import timeline_widget  # noqa: F401
from . import voiceover_studio  # noqa: F401


def main():
    assert hasattr(pipeline_panel, "PipelinePanel")
    assert hasattr(app, "MainWindow")
    assert hasattr(workspace, "MainWorkspace")
    print("SMOKE GUI OK")


if __name__ == "__main__":
    main()
