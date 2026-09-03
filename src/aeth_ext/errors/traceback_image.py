"""Render the currently-handled exception as a syntax-highlighted PNG for alert attachments."""

# Standard library imports
from functools import cache
from importlib.resources import files
from io import StringIO
from logging import getLogger

# Third party imports
import resvg_py
from rich._export_format import CONSOLE_SVG_FORMAT
from rich.console import Console
from rich.terminal_theme import MONOKAI

logger = getLogger(__name__)

__all__ = ["render_exception_image"]

# Static width tuned for legibility when the image is viewed at full-width on
# a phone screen. Deliberately NOT computed dynamically per exception: a
# width chosen to hit a target aspect ratio ends up *wider* for longer
# tracebacks (to avoid an absurdly tall image), which shrinks the text --
# exactly backwards from the goal of staying readable.
_TRACEBACK_IMAGE_WIDTH = 65

# Caps image height for pathologically deep tracebacks (e.g. runaway
# recursion, which can be hundreds of frames deep). The plain-text alert/
# attachment sent alongside this image is never truncated -- this cap only
# bounds the image, which exists for a quick visual glance, not forensics.
_TRACEBACK_IMAGE_MAX_FRAMES = 20

# Rich's default SVG template pulls "Fira Code" from a CDN via @font-face,
# which the offline rasterizer below never fetches anyway. JetBrains Mono is
# bundled with this package (see _resolve_font_path) and passed to resvg_py
# directly, rather than relying on it being installed on the host -- so the
# fallbacks after it only matter for the (never-taken) case where resvg_py's
# font loading is disabled some other way.
_FONT_STACK = '"JetBrains Mono", Consolas, "Cascadia Code", Menlo, monospace'
_CODE_FORMAT = (
  CONSOLE_SVG_FORMAT.split("<style>")[0]
  + "<style>\n\n"
  + CONSOLE_SVG_FORMAT.split("</style>")[0].split("<style>")[1].split("@font-face {{")[0]  # drop both @font-face blocks entirely
  + f"""
    .{{unique_id}}-matrix {{{{
        font-family: {_FONT_STACK};
        font-size: {{char_height}}px;
        line-height: {{line_height}}px;
        font-variant-east-asian: full-width;
    }}}}

    .{{unique_id}}-title {{{{
        font-size: 18px;
        font-weight: bold;
        font-family: arial;
    }}}}

    {{styles}}
    </style>
"""
  + CONSOLE_SVG_FORMAT.split("</style>")[1]
)

_BACKGROUND_COLOR = f"#{MONOKAI.background_color.hex.lstrip('#')}"


@cache
def _resolve_font_path() -> str:
  """Locate the JetBrains Mono font bundled with this package, as a real filesystem path.

  resvg_py has no access to fonts installed on the *host* unless told about
  them explicitly -- deployment targets (e.g. a `python:slim` container image)
  routinely have zero fonts installed, which makes every glyph in the
  traceback image silently disappear while the surrounding SVG chrome (the
  background panel, the title-bar dots) still renders fine, since those are
  drawn as plain shapes rather than text. Bundling the font with the package
  and passing it to resvg_py directly (see `render_exception_image`) removes
  that host dependency entirely.

  Deferred and cached rather than resolved at import time, matching
  `aeth_ext.errors.err_handling._resolve_program_name` -- most aeth_ext
  consumers never hit a fatal exception, so they shouldn't pay for this.
  """
  return str(files("aeth_ext.errors.fonts") / "JetBrainsMono-Regular.ttf")


def render_exception_image() -> bytes | None:
  """Render the currently-handled exception as a syntax-highlighted PNG image.

  Must be called from inside the ``except`` block for the exception being
  rendered, since Rich pulls it from ``sys.exc_info()`` -- same requirement
  as ``aeth_ext.errors.err_handling._extract_rich_traceback``.

  Best-effort: returns ``None`` instead of raising if rendering fails for any
  reason, so a rendering bug can never prevent the underlying text-based
  alert from going out.
  """
  try:
    console = Console(record=True, width=_TRACEBACK_IMAGE_WIDTH, file=StringIO())
    console.print_exception(show_locals=True, max_frames=_TRACEBACK_IMAGE_MAX_FRAMES)
    svg = console.export_svg(title="Exception", theme=MONOKAI, code_format=_CODE_FORMAT)
    return bytes(
      resvg_py.svg_to_bytes(
        svg_string=svg,
        background=_BACKGROUND_COLOR,
        font_files=[_resolve_font_path()],
        skip_system_fonts=True,
      )
    )
  except Exception:
    logger.exception("Failed to render traceback image")
    return None
