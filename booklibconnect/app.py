from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

# Support both:
# 1) python app.py              -> absolute imports
# 2) python -m booklibconnect.app -> relative imports
try:
    from .debug_tools import dbg, debug_call, install_excepthook
    from .qt_compat import ensure_qt_platform, log_qt_env
    from .ui_mainwindow import MainWindow
except ImportError:
    from debug_tools import dbg, debug_call, install_excepthook
    from qt_compat import ensure_qt_platform, log_qt_env
    from ui_mainwindow import MainWindow


@debug_call()
def main() -> int:
    install_excepthook()
    ensure_qt_platform()
    log_qt_env()

    dbg("main(): starting QApplication; argv=%s", sys.argv)
    app = QApplication(sys.argv)
    dbg("QApplication created")

    w = MainWindow()
    w.show()
    dbg("MainWindow shown")

    rc = app.exec()
    dbg("QApplication exited with code %s", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
