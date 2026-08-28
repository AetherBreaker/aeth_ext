"""Emergency diagnostics for places where going through ``logging`` is unsafe.

Some code runs *inside* the logging machinery -- a handler's ``emit``, a transport's delivery path,
the history/emergency writers behind it -- where a ``logger.*`` call is delivered by the very
component that is failing. In the central-log-server client that was unbounded recursion on a dead
socket (the 2026-08-27 production hang), a self-sustaining loop in the emergency writer, and one
alert plus one fatal shutdown per recursion frame. ``emergency_diagnostic`` is the channel such code uses
instead: it never touches ``logging``, never raises, and reports the way stdlib
``logging.Handler.handleError`` does -- to ``stderr`` -- while also appending to a file under the
persisted data directory, because the containers this runs in are ephemeral and their console
output is gone with them.

Not a log: a run writes a handful of entries here, only when nothing else can be trusted. They are
rendered with ``SmartColumnFormatter`` so the file reads like the project's normal log files, but
with its own columns: time, thread, the call site that reported, and the message (with
any traceback under it).
"""

# Standard library imports
import inspect
import logging
import sys
import threading
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.logging.bases import SmartColumnFormatter
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

__all__ = ["emergency_diagnostic", "emergency_diagnostics_path"]

settings = BaseSettings.get_settings()

emergency_diagnostics_path: Path = settings.persisted_dir_loc / "emergency_diagnostics.txt"
"""Where every entry is appended. Module-level so tests (and an operator) can point it elsewhere."""

# Above this size the file is rotated once to ``<name>.1`` (replacing any previous ``.1``), so a
# component stuck in a failure loop can't fill the persisted volume. Two files, bounded.
_MAX_BYTES = 5 * 1024 * 1024

# Widths are not persisted (``persist_path=None``): the shared width file is written from inside
# ``format`` and this channel must stay free of anything that can fail loudly or contend with the
# regular formatters. Alignment simply restarts with the process.
_FORMATTER = SmartColumnFormatter(
  ["{asctime}", "{threadName}", "{module}.{funcName}:{lineno}", "{message}"],
  persist_path=None,
)

_file_lock = threading.Lock()


def emergency_diagnostic(message: str, *, exc: BaseException | None = None) -> None:
  """Write *message* (and *exc*'s traceback, if given) to ``stderr`` and ``emergency_diagnostics_path``.

  Safe to call from anywhere, including inside a ``logging.Handler`` -- it never goes through
  ``logging`` and never raises. The reporting call site is recorded as the entry's origin column.

  Args:
    message: What happened, already formatted -- there is no lazy ``%`` formatting here.
    exc: The exception being reported; its traceback is rendered under the message.
  """
  try:
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None  # the reporting call site, for the origin column
    record = logging.LogRecord(
      __name__,
      logging.WARNING,
      caller.f_code.co_filename if caller is not None else "?",
      caller.f_lineno if caller is not None else 0,
      message,
      None,
      (type(exc), exc, exc.__traceback__) if exc is not None else None,
      func=caller.f_code.co_name if caller is not None else None,
    )
    text = _FORMATTER.format(record) + "\n"  # pyright: ignore[reportArgumentType] -- only LogRecord attributes are read
  except Exception:  # noqa: BLE001 -- formatting must not fail the caller; fall back to the bare message
    text = f"emergency diagnostic: {message}\n"
  try:
    stream = sys.stderr
    if stream is not None:
      stream.write(text)
      stream.flush()
  except Exception:  # noqa: BLE001, S110 -- a broken stderr must not take the caller down with it
    pass
  try:
    with _file_lock:
      path = emergency_diagnostics_path
      path.parent.mkdir(parents=True, exist_ok=True)
      if path.exists() and path.stat().st_size > _MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
      with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
  except Exception:  # noqa: BLE001, S110 -- an unwritable volume must not either; stderr already has the line
    pass
