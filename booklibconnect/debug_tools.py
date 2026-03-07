from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def _truthy_env(name: str, default: str = "1") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v in {"1", "true", "yes", "on", "y"}


DEBUG_ENABLED = _truthy_env("BOOKLIBCONNECT_DEBUG", "1")


def _debug_log_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    d = base / "booklibconnect"
    d.mkdir(parents=True, exist_ok=True)
    return d / "debug.log"


LOGGER = logging.getLogger("booklibconnect")
if not LOGGER.handlers:
    LOGGER.setLevel(logging.DEBUG if DEBUG_ENABLED else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if DEBUG_ENABLED else logging.INFO)
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)

    try:
        fh = logging.FileHandler(_debug_log_path(), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)
    except Exception:
        # Never crash because logging file can't be created
        pass


def dbg(msg: str, *args: Any) -> None:
    if DEBUG_ENABLED:
        LOGGER.debug(msg, *args)


def info(msg: str, *args: Any) -> None:
    LOGGER.info(msg, *args)


def warn(msg: str, *args: Any) -> None:
    LOGGER.warning(msg, *args)


def err(msg: str, *args: Any) -> None:
    LOGGER.error(msg, *args)


def dbg_exc(prefix: str = "Exception") -> None:
    LOGGER.error("%s:\n%s", prefix, traceback.format_exc())


def _safe_repr(v: Any, max_len: int = 160) -> str:
    try:
        s = repr(v)
    except Exception:
        s = f"<unrepr {type(v).__name__}>"
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def _safe_repr_args(args: tuple[Any, ...], limit: int = 5) -> str:
    out = []
    for i, a in enumerate(args[:limit]):
        if i == 0 and hasattr(a, "__class__") and a.__class__.__name__ == "MainWindow":
            out.append("<MainWindow>")
        else:
            out.append(_safe_repr(a))
    if len(args) > limit:
        out.append("...")
    return "(" + ", ".join(out) + ")"


def _safe_repr_kwargs(kwargs: dict[str, Any], limit: int = 6) -> str:
    items = list(kwargs.items())[:limit]
    parts = [f"{k}={_safe_repr(v)}" for k, v in items]
    if len(kwargs) > limit:
        parts.append("...")
    return "{" + ", ".join(parts) + "}"


def debug_call(name: str | None = None) -> Callable[[F], F]:
    def deco(fn: F) -> F:
        fn_name = name or fn.__qualname__

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            dbg("ENTER %s args=%s kwargs=%s", fn_name, _safe_repr_args(args), _safe_repr_kwargs(kwargs))
            try:
                out = fn(*args, **kwargs)
                dt = (time.perf_counter() - t0) * 1000.0
                dbg("EXIT  %s (%.1f ms)", fn_name, dt)
                return out
            except Exception:
                dt = (time.perf_counter() - t0) * 1000.0
                LOGGER.error("FAIL  %s (%.1f ms)\n%s", fn_name, dt, traceback.format_exc())
                raise

        return cast(F, wrapper)

    return deco


def install_excepthook() -> None:
    def _hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        LOGGER.critical("UNCAUGHT EXCEPTION\n%s", text)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
    dbg("Installed sys.excepthook")
