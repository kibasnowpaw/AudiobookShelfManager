from __future__ import annotations

import logging
import os
import sys
import time
import threading
import traceback
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import diagnostics
from typing import Any, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def _truthy_env(name: str, default: str = "0") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


# Detailed debug tracing is disabled during normal use.
# It can be enabled manually with:
# BOOKLIBCONNECT_DEBUG=1
DEBUG_ENABLED = _truthy_env("BOOKLIBCONNECT_DEBUG", "0")


def _debug_log_path() -> Path:
    base = Path(
        os.environ.get(
            "XDG_CACHE_HOME",
            str(Path.home() / ".cache"),
        )
    )
    directory = base / "booklibconnect"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "debug.log"


LOGGER = logging.getLogger("booklibconnect")

if not LOGGER.handlers:
    LOGGER.setLevel(logging.DEBUG if DEBUG_ENABLED else logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(
        logging.DEBUG if DEBUG_ENABLED else logging.INFO
    )
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    try:
        file_handler = RotatingFileHandler(
            _debug_log_path(),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(
            logging.DEBUG if DEBUG_ENABLED else logging.INFO
        )
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
    except Exception:
        # Logging must never prevent the program from starting.
        pass


def dbg(message: str, *args: Any) -> None:
    if DEBUG_ENABLED:
        LOGGER.debug(message, *args)


def info(message: str, *args: Any) -> None:
    LOGGER.info(message, *args)


def warn(message: str, *args: Any) -> None:
    LOGGER.warning(message, *args)


def err(message: str, *args: Any) -> None:
    LOGGER.error(message, *args)


def dbg_exc(prefix: str = "Exception") -> None:
    exc = sys.exc_info()[1]
    if exc is not None:
        # The support report deliberately never receives the raw debug text.
        diagnostics.record('python', 'warning', exc=exc, once=True)
    LOGGER.error(
        "%s:\n%s",
        prefix,
        traceback.format_exc(),
    )


def _safe_repr(value: Any, max_len: int = 160) -> str:
    try:
        result = repr(value)
    except Exception:
        result = f"<unrepr {type(value).__name__}>"

    if len(result) > max_len:
        result = result[: max_len - 3] + "..."

    return result


def _safe_repr_args(
    args: tuple[Any, ...],
    limit: int = 5,
) -> str:
    output: list[str] = []

    for index, argument in enumerate(args[:limit]):
        if (
            index == 0
            and hasattr(argument, "__class__")
            and argument.__class__.__name__ == "MainWindow"
        ):
            output.append("<MainWindow>")
        else:
            output.append(_safe_repr(argument))

    if len(args) > limit:
        output.append("...")

    return "(" + ", ".join(output) + ")"


def _safe_repr_kwargs(
    kwargs: dict[str, Any],
    limit: int = 6,
) -> str:
    items = list(kwargs.items())[:limit]
    parts = [
        f"{key}={_safe_repr(value)}"
        for key, value in items
    ]

    if len(kwargs) > limit:
        parts.append("...")

    return "{" + ", ".join(parts) + "}"


def debug_call(
    name: str | None = None,
) -> Callable[[F], F]:
    def decorator(function: F) -> F:
        # During normal use, return the original function directly.
        # This avoids timing, argument conversion and debug-log writes.
        if not DEBUG_ENABLED:
            return function

        function_name = name or function.__qualname__

        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()

            dbg(
                "ENTER %s args=%s kwargs=%s",
                function_name,
                _safe_repr_args(args),
                _safe_repr_kwargs(kwargs),
            )

            try:
                result = function(*args, **kwargs)

                elapsed_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                dbg(
                    "EXIT  %s (%.1f ms)",
                    function_name,
                    elapsed_ms,
                )

                return result

            except Exception:
                elapsed_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                LOGGER.error(
                    "FAIL  %s (%.1f ms)\n%s",
                    function_name,
                    elapsed_ms,
                    traceback.format_exc(),
                )
                raise

        return cast(F, wrapper)

    return decorator


def install_excepthook() -> None:
    def _hook(exc_type, exc_value, exc_tb) -> None:
        diagnostics.record('python', 'error', exc=exc_value)
        text = "".join(
            traceback.format_exception(
                exc_type,
                exc_value,
                exc_tb,
            )
        )

        LOGGER.critical(
            "UNCAUGHT EXCEPTION\n%s",
            text,
        )

        sys.__excepthook__(
            exc_type,
            exc_value,
            exc_tb,
        )

    def _thread_hook(args):
        if args.exc_type is not SystemExit:
            diagnostics.record('python.thread', 'error', exc=args.exc_value)
        threading.__excepthook__(args)

    threading.excepthook = _thread_hook
    sys.excepthook = _hook
    dbg("Installed sys.excepthook")
