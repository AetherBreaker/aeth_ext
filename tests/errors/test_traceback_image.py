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

  def test_returns_png_bytes_for_a_given_exception_outside_any_except_block(self) -> None:
    try:
      raise ValueError("boom")
    except ValueError as e:
      caught = e

    result = ti_mod.render_exception_image(caught)

    assert result is not None
    assert result.startswith(_PNG_MAGIC)

  def test_returns_none_without_logging_when_there_is_no_exception(self, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR"):
      result = ti_mod.render_exception_image()

    assert result is None
    assert caplog.records == []

  def test_renders_the_given_exception_with_locals_and_the_frame_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_from_exception = ti_mod.Traceback.from_exception

    def spy_from_exception(*args: object, **kwargs: object) -> ti_mod.Traceback:
      calls.append((args, kwargs))
      return original_from_exception(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(ti_mod.Traceback, "from_exception", spy_from_exception)
    try:
      raise ValueError("boom")
    except ValueError as e:
      caught = e

    ti_mod.render_exception_image(caught)

    ((args, kwargs),) = calls
    assert args == (ValueError, caught, caught.__traceback__)
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
