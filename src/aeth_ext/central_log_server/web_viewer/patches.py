# Standard library imports
import logging as _stdlib_logging
import sys as _sys
from pathlib import Path as _Path

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher


def _is_web_viewer_entrypoint() -> bool:
  """Return True only when the current process entrypoint is the web_viewer __main__."""
  main = _sys.modules.get("__main__")
  main_file = getattr(main, "__file__", None)
  if main_file is None:
    return False
  return _Path(main_file).resolve() == (_Path(__file__).parent / "__main__.py").resolve()


class WebViewerPatches(MonkeyPatcher):
  """Monkey patches applied to every web_viewer subprocess at startup.

  Each method is forced static by ``MonkeyPatcherMeta`` and called once by
  ``MonkeyPatcher.apply_monkey_patches()`` during ``initialize()``.

  ``apply_monkey_patches()`` searches upward from the caller's own directory
  ancestry and never descends into subdirectories, so a separate process (e.g.
  the central log server, whose own entrypoint search never descends into
  ``web_viewer/``) will not discover this subclass at all. Each patch still
  guards itself with ``_is_web_viewer_entrypoint()`` as cheap defense-in-depth
  so Textual is never imported outside the web_viewer subprocess even if that
  search boundary ever changes.
  """

  def patch_textual_logger() -> None:  # type: ignore[misc]
    """Route Textual's ``log()`` calls through Python standard logging.

    Textual 8.x has no ``logging.getLogger`` calls anywhere in its source —
    its ``Logger.__call__`` only writes to ``TEXTUAL_LOG`` or the devtools
    socket.  Wrapping it here makes every ``app.log(...)`` / ``self.log.*``
    call also emit a record on the ``textual`` stdlib logger so that the
    ``SocketHandler`` forwards it to the central log server.
    """
    if not _is_web_viewer_entrypoint():
      return

    # Deferred so Textual is never imported into a parent process.
    # Third party imports
    from textual import Logger as _TextualLogger
    from textual._log import LogGroup

    _textual_pylog = _stdlib_logging.getLogger("textual")

    _group_to_level: dict[LogGroup, int] = {
      LogGroup.UNDEFINED: _stdlib_logging.DEBUG,
      LogGroup.EVENT: _stdlib_logging.DEBUG,
      LogGroup.DEBUG: _stdlib_logging.DEBUG,
      LogGroup.INFO: _stdlib_logging.INFO,
      LogGroup.WARNING: _stdlib_logging.WARNING,
      LogGroup.ERROR: _stdlib_logging.ERROR,
      LogGroup.PRINT: _stdlib_logging.INFO,
      LogGroup.SYSTEM: _stdlib_logging.DEBUG,
      LogGroup.LOGGING: _stdlib_logging.DEBUG,
      LogGroup.WORKER: _stdlib_logging.DEBUG,
    }

    # Captured here (inside the guard) so we always wrap the true original,
    # and the reference is never created in a non-web_viewer process.
    _orig_call = _TextualLogger.__call__

    def _patched_call(self: _TextualLogger, *args: object, **kwargs: object) -> None:
      if args or kwargs:
        parts = [str(arg) for arg in args]
        if kwargs:
          parts += [f"{k}={v!r}" for k, v in kwargs.items()]
        level = _group_to_level.get(self._group, _stdlib_logging.DEBUG)  # pyright: ignore[reportPrivateUsage]
        _textual_pylog.log(level, " ".join(parts))
      _orig_call(self, *args, **kwargs)

    _TextualLogger.__call__ = _patched_call
