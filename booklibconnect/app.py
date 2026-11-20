from __future__ import annotations

import sys
import time


# The console launcher and -m use the package. Also support opening app.py
# directly from a checkout without catching (and masking) dependency errors.
if not __package__:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booklibconnect import __version__
from booklibconnect import diagnostics


def main() -> int:
    if sys.argv[1:] in (["--version"], ["-V"]):
        print(f"Audiobook Shelf Manager {__version__}")
        return 0
    if sys.argv[1:] == ["--diagnostics"]:
        # Also works when the Qt platform plugin or the GUI cannot start.
        sys.stdout.write(diagnostics.report())
        return 0

    started = time.perf_counter()
    diagnostics.start()
    from booklibconnect.debug_tools import dbg, install_excepthook
    install_excepthook()
    diagnostics.record("startup", "start")
    from PySide6.QtCore import QTimer, qVersion
    from PySide6.QtWidgets import QApplication
    from booklibconnect.qt_compat import ensure_qt_platform, log_qt_env
    from booklibconnect.ui_mainwindow import MainWindow

    ensure_qt_platform()
    log_qt_env()
    dbg("main(): starting QApplication")
    app = QApplication(sys.argv)
    app.setApplicationName("Audiobook Shelf Manager")
    app.setOrganizationName("Audiobook Shelf Manager")
    app.setApplicationVersion(__version__)
    dbg("QApplication created")

    screen = app.primaryScreen()
    size = screen.size() if screen is not None else None
    diagnostics.record("app", "ok", backend=app.platformName(), qt=qVersion(),
                       width=size.width() if size else 0, height=size.height() if size else 0)
    # Record unusually late callbacks only. A delay can also mean the system slept.
    heartbeat = QTimer(app)
    heartbeat.setInterval(1000)
    last_tick = time.perf_counter()

    def tick():
        nonlocal last_tick
        now = time.perf_counter()
        late_ms = round((now - last_tick) * 1000) - heartbeat.interval()
        last_tick = now
        if late_ms >= 1000:
            diagnostics.record('ui', 'delay', ms=late_ms)

    heartbeat.timeout.connect(tick)
    heartbeat.start()
    w = MainWindow()
    w.show()
    diagnostics.record("startup", "ok", ms=diagnostics.elapsed_ms(started))
    dbg("MainWindow shown")

    rc = app.exec()
    diagnostics.stop(rc)
    dbg("QApplication exited with code %s", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
