"""Adjacent temporary files, atomic replacement and durable JSON writes."""
import json
import os
import tempfile
from pathlib import Path


def write_bytes_atomic(path: Path, contents: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # Some filesystems do not support syncing the directory itself.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def write_json_atomic(path: Path, data: object) -> None:
    write_bytes_atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
