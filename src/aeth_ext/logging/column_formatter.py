"""``SmartColumnFormatter``: the column-aligned formatter used by the packaged file configs and ``emergency_diagnostics``.

Kept apart from ``bases`` because ``bases`` imports ``aeth_ext.errors`` (for its rotating handler's alerts) and
``aeth_ext.errors`` reaches ``emergency_diagnostics``, which needs this formatter; nothing here may import
``aeth_ext.errors``.
"""

# Standard library imports
import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, override

# First party imports
from aeth_ext.settings import BaseSettings

__all__ = ["SmartColumnFormatter"]

_tz = BaseSettings.get_settings().tz

_UNSET: object = object()


class _DefaultMap:
  """Mapping that returns ``""`` for any key absent from the wrapped dict.

  Used with :func:`str.format_map` so column templates referencing attributes
  not present on a :class:`~logging.LogRecord` produce ``""`` instead of
  raising :exc:`KeyError`.
  """

  __slots__ = ("_d",)

  def __init__(self, d: dict[str, Any]) -> None:
    self._d = d

  def __getitem__(self, key: str) -> Any:
    return self._d.get(key, "")


class SmartColumnFormatter(logging.Formatter):
  """A :class:`~logging.Formatter` that renders records as dynamically aligned columns.

  Each column is defined by a ``{}``-style template string (e.g. ``"{asctime}"``,
  ``"{levelname: >8}"``, ``"{message}"``) referencing any attribute of a
  :class:`~logging.LogRecord`.

  **Alignment**: the formatter tracks the widest rendered value seen for each
  tracked column and pads all values with spaces so that columns stay aligned
  as new records arrive.  Tracked widths can be persisted to a JSON file so
  alignment is preserved across restarts.

  **Per-handler tracking**: when the formatter is attached to a handler via
  :meth:`logging.Handler.setFormatter`, it wraps that handler's ``format``
  method so each handler maintains an **independent** width tally.  A single
  formatter instance can therefore be shared across multiple handlers (e.g.
  ``debug_file`` and ``info_file``) and each log file stays as narrow as its
  own content allows.

  **Tracked columns**: all columns except the last are tracked by default.
  If *right_align_last* is :data:`True` the last column is also tracked and
  its content is right-justified to the running maximum width.

  **Multiline values**: when any column value contains newline characters the
  output spans multiple rows.  All columns' lines are zipped together
  row-by-row; columns that exhaust their lines early show blank padding on
  continuation rows.

  Args:
    columns: Ordered list of ``{}``-style format strings, one per column.
    separator: String inserted between adjacent columns (default ``" | "``).
    persist_path: JSON file used to persist maximum column widths across
      process restarts.  The default (sentinel :data:`_UNSET`) resolves at
      construction time to
      ``settings.persisted_dir_loc / "logging_column_widths.json"``.
      Pass :data:`None` to disable persistence.
    right_align_last: When :data:`True` the last column is also tracked and
      its content is right-justified to the running maximum width.
    datefmt: Date/time format string forwarded to :meth:`formatTime`.
  """

  default_msec_format = None
  _file_lock: ClassVar[threading.Lock] = threading.Lock()

  def __init__(
    self,
    columns: list[str],
    separator: str = " | ",
    persist_path: Path | None = _UNSET,  # type: ignore[assignment]
    *,
    right_align_last: bool = False,
    datefmt: str | None = None,
  ) -> None:
    """Set up column state and load any persisted widths (arguments are documented on the class)."""
    super().__init__(datefmt=datefmt)
    if persist_path is _UNSET:
      persist_path = BaseSettings.get_settings().persisted_dir_loc / "logging_column_widths.json"
    self._columns = columns
    self._separator = separator
    self._persist_path: Path | None = persist_path
    self._right_align_last = right_align_last
    self._n_tracked = len(columns) if right_align_last else len(columns) - 1
    self._widths_default: list[int] = [0] * len(columns)
    self._widths_by_handler: dict[str, list[int]] = {}
    self._lock = threading.Lock()
    self._local = threading.local()
    key_src = separator + "||" + "||".join(columns)
    self._key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
    self._load_all_widths()

  @override
  def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
    dt = datetime.fromtimestamp(record.created, tz=_tz)
    if datefmt:
      s = dt.strftime(datefmt)
    else:
      s = dt.strftime(self.default_time_format)
      if self.default_msec_format:
        s = self.default_msec_format % (s, record.msecs)
    return s

  def _register_handler(self, handler: logging.Handler) -> None:
    """Attach this formatter to *handler*, giving it a dedicated width tally.

    Called automatically by the :func:`logging.Handler.setFormatter` patch
    installed at module level.  Wraps ``handler.format`` with a closure that
    sets a thread-local ``handler_name`` before delegating to the original
    method, allowing :meth:`_current_widths` to select the right tally.
    """
    if isinstance(handler, logging.FileHandler):
      hname: str = str(Path(handler.baseFilename).resolve())
    else:
      hname: str = handler.name or f"_anon_{id(handler)}"
    with self._lock:
      if hname not in self._widths_by_handler:
        self._widths_by_handler[hname] = [0] * len(self._columns)

    # Stashed directly on the handler (rather than only reachable via the
    # thread-local set inside ``_format_with_context``) so code outside of a
    # ``format()`` call -- e.g. the ``emit_separator`` monkey patch -- can
    # still look up which width tally belongs to this handler.
    handler._smart_col_handler_name = hname  # type: ignore[attr-defined]

    local = self._local
    original_format = handler.format

    def _format_with_context(record: logging.LogRecord) -> str:
      local.handler_name = hname
      try:
        return original_format(record)
      finally:
        local.handler_name = None

    handler.format = _format_with_context

  def _current_widths(self) -> list[int]:
    hname: str | None = getattr(self._local, "handler_name", None)
    if hname is not None:
      return self._widths_by_handler.get(hname, self._widths_default)
    return self._widths_default

  def format_separator(self, message: str, handler_name: str | None = None) -> str:
    """Render *message* centered in dashes, padded to match this handler's column width.

    Used by the ``Handler.emit_separator`` monkey patch (see
    ``aeth_ext.central_log_server.patches``) to print connect/disconnect rules
    that stay visually aligned with the surrounding columnar output. *handler_name*
    should be the same key ``_register_handler`` computed for the target handler
    (stashed as ``handler._smart_col_handler_name``); when omitted or unknown the
    shared default tally is used instead.

    Falls back to a fixed width of 60 when nothing has been tracked yet (e.g. the
    very first record a handler ever sees is a separator).
    """
    widths = self._widths_by_handler.get(handler_name, self._widths_default) if handler_name is not None else self._widths_default
    total_width = sum(widths[: self._n_tracked]) + len(self._separator) * self._n_tracked
    if total_width <= 0:
      total_width = 60
    return message.center(total_width, "-")

  def _load_all_widths(self) -> None:
    if self._persist_path is None:
      return
    try:
      with SmartColumnFormatter._file_lock:
        raw = self._persist_path.read_text(encoding="utf-8")
        data: dict[str, dict[str, list[int]]] = json.loads(raw)
      section = data.get(self._key, {})
      n = len(self._columns)
      for hname, saved in section.items():
        widths = [0] * n
        for i, w in enumerate(saved):
          if i < n:
            widths[i] = max(0, w)
        if hname == "__default__":
          self._widths_default = widths
        else:
          self._widths_by_handler[hname] = widths
    except OSError, json.JSONDecodeError, ValueError, TypeError:
      pass

  def _save_widths(self) -> None:
    if self._persist_path is None:
      return
    with self._lock:
      snapshot: dict[str, list[int]] = {
        "__default__": list(self._widths_default),
        **{k: list(v) for k, v in self._widths_by_handler.items()},
      }
    try:
      with SmartColumnFormatter._file_lock:
        try:
          data: dict[str, dict[str, list[int]]] = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
          data = {}
        data[self._key] = snapshot
        self._persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
      pass

  def _update_width(self, col_idx: int, width: int) -> bool:
    widths = self._current_widths()
    with self._lock:
      if width > widths[col_idx]:
        widths[col_idx] = width
        return True
    return False

  def _render_columns(self, record: logging.LogRecord) -> list[str]:
    dm = _DefaultMap(record.__dict__)
    rendered = [col.format_map(dm) for col in self._columns]
    if record.exc_text:
      rendered[-1] += ("\n" if rendered[-1] else "") + record.exc_text.rstrip()
    if record.stack_info:
      rendered[-1] += ("\n" if rendered[-1] else "") + self.formatStack(record.stack_info)
    return rendered

  def _track_widths(self, col_lines: list[list[str]]) -> None:
    changed = False
    for i in range(self._n_tracked):
      max_w = max(len(line) for line in col_lines[i])
      if self._update_width(i, max_w):
        changed = True
    if self._right_align_last:
      last = len(self._columns) - 1
      max_w = max(len(line) for line in col_lines[last])
      if self._update_width(last, max_w):
        changed = True
    if changed:
      self._save_widths()

  def _pad_cell(self, c: int, cell: str, widths: list[int]) -> str:
    if c == len(self._columns) - 1:
      if self._right_align_last:
        return cell.rjust(widths[c])
      return cell
    if c < self._n_tracked:
      return cell.ljust(widths[c])
    return cell

  @override
  def format(self, record: logging.LogRecord) -> str:
    record.message = record.getMessage()
    record.asctime = self.formatTime(record, self.datefmt)
    if record.exc_info and not record.exc_text:
      record.exc_text = self.formatException(record.exc_info)

    col_lines: list[list[str]] = [r.split("\n") for r in self._render_columns(record)]
    self._track_widths(col_lines)
    widths = self._current_widths()

    max_rows = max(len(lines) for lines in col_lines)
    output_rows: list[str] = []
    for row_idx in range(max_rows):
      parts = [self._pad_cell(c, lines[row_idx] if row_idx < len(lines) else "", widths) for c, lines in enumerate(col_lines)]
      output_rows.append(self._separator.join(parts))
    return "\n".join(output_rows)


_orig_handler_set_formatter = logging.Handler.setFormatter


def _patched_set_formatter(self: logging.Handler, fmt: logging.Formatter | None) -> None:
  _orig_handler_set_formatter(self, fmt)
  if fmt is not None and hasattr(fmt, "_register_handler"):
    fmt._register_handler(self)  # type: ignore[union-attr]


logging.Handler.setFormatter = _patched_set_formatter
