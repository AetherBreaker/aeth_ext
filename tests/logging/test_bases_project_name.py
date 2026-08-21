"""End-to-end test for `TaggedLogRecord`'s `PROJECT_NAME` discovery.

`TaggedLogRecord._PROJECT_NAME` is resolved exactly once, at `bases.py` module
import time, via `parse_and_grab_constants` walking from `bases.py` up to the
process entrypoint looking for a `PROJECT_NAME` constant in `__main__.py`/
`__init__.py`. That resolution can't be exercised by importing `bases` inside
the pytest process itself -- it has already happened by the time any test
runs, and pytest's own entrypoint never defines `PROJECT_NAME` anyway (see the
`_project_name_set` fixture in `conftest.py`, which stands in for a real
consumer for every other test in this package).

These tests instead spawn a real subprocess with its own fake "consuming
program" -- a package with a `__main__.py` that sets `PROJECT_NAME` the way a
real application would -- and observe what `TaggedLogRecord` resolves to when
imported fresh in that process.
"""

# Standard library imports
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_PYTHON = sys.executable


def _write(path: Path, text: str) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text, encoding="utf-8")
  return path


def _run_fake_app(tmp_path: Path, main_body: str) -> subprocess.CompletedProcess[str]:
  """Write a fake `app` package with the given `__main__.py` body and run it.

  `main_body` runs as the process entrypoint (`python -m app` with `tmp_path`
  as the working directory), so `get_entrypoint_root()` resolves to the fake
  app's own directory exactly like it would for a real consumer.
  """
  _write(tmp_path / "app" / "__init__.py", "")
  _write(tmp_path / "app" / "__main__.py", main_body)
  return subprocess.run(
    [_PYTHON, "-m", "app"],
    cwd=tmp_path,
    capture_output=True,
    text=True,
    check=False,
  )


class TestTaggedLogRecordProjectNameDiscovery:
  def test_picks_up_project_name_set_by_consuming_program(self, tmp_path: Path) -> None:
    result = _run_fake_app(
      tmp_path,
      'PROJECT_NAME = "widgetco"\n'
      "\n"
      "from aeth_ext.logging.bases import TaggedLogRecord\n"
      "\n"
      'record = TaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n'
      "print(record.project_name)\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "widgetco"

  def test_raises_when_no_consuming_program_sets_project_name(self, tmp_path: Path) -> None:
    result = _run_fake_app(
      tmp_path,
      "from aeth_ext.logging.bases import TaggedLogRecord\n"
      "\n"
      'TaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n',
    )

    assert result.returncode != 0
    assert "Expected project name to be set, but got 'FIX_ME'" in result.stderr
