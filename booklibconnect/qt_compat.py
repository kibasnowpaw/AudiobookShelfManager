from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

from .debug_tools import dbg, dbg_exc

try:
    from PySide6.QtCore import QSignalBlocker
    _HAS_QSIGNALBLOCKER = True
except Exception:
    QSignalBlocker = None  # type: ignore[assignment]
    _HAS_QSIGNALBLOCKER = False


class QObjectLike(Protocol):
    def blockSignals(self, b: bool) -> bool: ...


def ensure_qt_platform() -> None:
    if "QT_QPA_PLATFORM" not in os.environ:
        if os.environ.get("WAYLAND_DISPLAY"):
            os.environ["QT_QPA_PLATFORM"] = "wayland"
        elif os.environ.get("DISPLAY"):
            os.environ["QT_QPA_PLATFORM"] = "xcb"


def log_qt_env() -> None:
    dbg(
        "Startup env: QT_QPA_PLATFORM=%s WAYLAND_DISPLAY=%s DISPLAY=%s HAS_QSIGNALBLOCKER=%s",
        os.environ.get("QT_QPA_PLATFORM"),
        bool(os.environ.get("WAYLAND_DISPLAY")),
        bool(os.environ.get("DISPLAY")),
        _HAS_QSIGNALBLOCKER,
    )


@contextmanager
def signal_blocker(widget: QObjectLike) -> Iterator[Any]:
    """
    Safer wrapper around QSignalBlocker.
    Falls back to blockSignals(True) if QSignalBlocker is missing.
    """
    if _HAS_QSIGNALBLOCKER and QSignalBlocker is not None:
        blocker = QSignalBlocker(widget)  # type: ignore[arg-type]
        try:
            yield blocker
        finally:
            pass
        return

    old = False
    try:
        old = bool(widget.blockSignals(True))
        dbg("signal_blocker fallback: blockSignals(True) old=%s on %s", old, type(widget).__name__)
        yield None
    finally:
        try:
            widget.blockSignals(old)
            dbg("signal_blocker fallback: restored old=%s on %s", old, type(widget).__name__)
        except Exception:
            dbg_exc("signal_blocker fallback restore failed")
