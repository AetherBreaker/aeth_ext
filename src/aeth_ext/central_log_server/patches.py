# Standard library imports
import logging

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher


class CentralLogServerPatches(MonkeyPatcher):
  """Monkey patches applied to the central log server process at startup.

  Each method is forced static by ``MonkeyPatcherMeta`` and called once by
  ``MonkeyPatcher.apply_monkey_patches()`` during ``initialize()``.
  """

  def patch_handler_emit_separator() -> None:  # type: ignore[misc]
    """Add ``Handler.emit_separator(message)`` for printing connect/disconnect rules.

    Writes a dash-padded rule line (e.g. ``"--- myapp connected ---"``) straight to
    the handler's stream, bypassing the usual ``Formatter.format()``/filter
    pipeline entirely, since a separator is not a real log record.

    When the handler's formatter is a
    :class:`~aeth_ext.logging.bases.SmartColumnFormatter`, the rule is padded to
    match that handler's tracked column width (via
    :meth:`~aeth_ext.logging.bases.SmartColumnFormatter.format_separator`) so it
    stays visually aligned with the columns it separates. Handlers without a
    writable ``stream`` (e.g. ``NullHandler``, ``SocketHandler``) are silently
    skipped.
    """
    # Deferred to avoid a hard import-time dependency from this lightweight
    # patches module onto the rest of the logging package.
    # First party imports
    from aeth_ext.logging.bases import SmartColumnFormatter

    def emit_separator(self: logging.Handler, message: str) -> None:
      if not hasattr(self, "stream"):
        # Not a stream-backed handler (e.g. NullHandler, SocketHandler,
        # MemoryHandler); there is nowhere sensible to write a rule.
        return

      formatter = self.formatter
      if isinstance(formatter, SmartColumnFormatter):
        handler_name = getattr(self, "_smart_col_handler_name", None)
        line = formatter.format_separator(message, handler_name)
      else:
        line = message.center(60, "-")

      self.acquire()
      try:
        stream = getattr(self, "stream", None)
        if stream is None:
          opener = getattr(self, "_open", None)
          if opener is None:
            return
          stream = self.stream = opener()  # type: ignore[attr-defined]
        terminator = getattr(self, "terminator", "\n")
        stream.write(line + terminator)
        self.flush()
      except OSError, ValueError:
        # Mirrors logging's own defensive posture (e.g. a closed stream);
        # a failed separator write is not worth tearing anything down for.
        pass
      finally:
        self.release()

    logging.Handler.emit_separator = emit_separator  # type: ignore[attr-defined]
