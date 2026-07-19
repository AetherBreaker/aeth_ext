# Standard library imports
import os
from typing import Any, override

# First party imports
from aeth_ext.logging.setup import BaseLoggingConfig


class LoggingConfig(BaseLoggingConfig):
  """Project logging configuration.

  All customization lives in the TOML override files shipped next to this
  module (discovered via the directory of ``__main__``):

  - ``logging_config.toml`` - local-mode file split.
  - ``remote_logging_config.toml`` - the same split applied server-side by the
    central log server (merged into the remote config sent in the socket
    handshake).

  ``override_mode = "merge"`` merges those files onto the packaged aeth_ext
  defaults instead of replacing them.
  """

  override_mode = "merge"

  @override
  @classmethod
  def get_default_remote_config(cls, logging_file_name: str) -> dict[str, Any]:
    """Build the remote config using the session UUID for all filenames.

    When ``AETH_WEB_SESSION_ID`` is set (injected by ``SessionAppService``),
    the session UUID is used as the filename prefix so that every session's
    log files land inside the single shared
    ``aeth_ext.central-log-web-viewer/`` directory rather than each session
    getting its own subdirectory.  Falls back to ``logging_file_name`` in
    standalone (no env var) mode, preserving original behaviour.
    """
    session_id = os.environ.get("AETH_WEB_SESSION_ID", logging_file_name)
    config = super().get_default_remote_config(session_id)
    # The textual handler filenames in remote_logging_config.toml are static
    # (logdir://textual_debug.log etc.).  Replace them with session-specific
    # names now that pre_resolve has already run - logdir:// values are kept
    # as strings by pre_resolve and resolved server-side, so we can still
    # rewrite them here.
    handlers: dict[str, Any] = config.get("handlers", {})
    if "textual_debug" in handlers:
      handlers["textual_debug"]["filename"] = f"logdir://{session_id}_textual_debug.log"
    if "textual_info" in handlers:
      handlers["textual_info"]["filename"] = f"logdir://{session_id}_textual.log"
    return config
