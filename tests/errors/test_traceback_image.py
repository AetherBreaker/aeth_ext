# Standard library imports
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Third party imports
  import pytest

# First party imports
from aeth_ext.errors import traceback_image as ti_mod

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestRenderExceptionImage:
  def test_returns_png_bytes_for_the_currently_handled_exception(self) -> None:
    try:
      raise ValueError("boom")
    except ValueError:
      result = ti_mod.render_exception_image()

    assert result is not None
    assert result.startswith(_PNG_MAGIC)

  def test_uses_the_configured_width_and_max_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    original_print_exception = ti_mod.Console.print_exception

    def spy_print_exception(self: ti_mod.Console, **kwargs: object) -> None:
      calls.append(kwargs)
      original_print_exception(self, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(ti_mod.Console, "print_exception", spy_print_exception)

    try:
      raise ValueError("boom")
    except ValueError:
      ti_mod.render_exception_image()

    (kwargs,) = calls
    assert kwargs["show_locals"] is True
    assert kwargs["max_frames"] == ti_mod._TRACEBACK_IMAGE_MAX_FRAMES  # pyright: ignore[reportPrivateUsage]

  def test_returns_none_and_logs_when_rendering_fails(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def raising_svg_to_bytes(**kwargs: object) -> bytes:
      raise RuntimeError("rasterizer exploded")

    monkeypatch.setattr(ti_mod.resvg_py, "svg_to_bytes", raising_svg_to_bytes)

    try:
      raise ValueError("boom")
    except ValueError:
      with caplog.at_level("ERROR"):
        result = ti_mod.render_exception_image()

    assert result is None
    assert any("Failed to render traceback image" in record.message for record in caplog.records)
