from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QStandardPaths


def app_data_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def covers_dir() -> Path:
    p = app_data_dir() / "covers"
    p.mkdir(parents=True, exist_ok=True)
    return p