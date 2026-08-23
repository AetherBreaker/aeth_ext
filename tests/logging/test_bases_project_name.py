"""End-to-end test for `TaggedLogRecord`'s `PROJECT_NAME` discovery.

`TaggedLogRecord._resolve_project_name()` resolves and caches `PROJECT_NAME` lazily, on first
real record construction (not at `bases.py` module import time -- a `python -m pkg.submodule`
invocation can import `bases.py` during runpy's dotted-path resolution, before
`sys.modules["__main__"]` points at the real entry module, which would permanently poison an
eager, class-definition-time lookup), via `parse_and_grab_constants` walking from `bases.py` up to
the process entrypoint looking for a `PROJECT_NAME` constant in `__main__.py`/`__init__.py`. That
resolution can't be exercised by importing `bases` inside the pytest process itself -- pytest's own
entrypoint never defines `PROJECT_NAME` anyway (see the `_project_name_set` fixture in
`conftest.py`, which stands in for a real consumer for every other test in this package).

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

  def test_picks_up_project_name_for_a_deep_dotted_submodule_entrypoint(self, tmp_path: Path) -> None:
    """`python -m app.sub.leaf` resolves the dotted path by importing `app`, then `app.sub`
    (executing their `__init__.py`s under their real names) to locate `leaf`'s spec -- only
    *after* that does `sys.modules["__main__"]` get swapped to point at `leaf`, and `leaf.py`'s
    own top-level code run as `__main__`. If some parent package's `__init__.py` imports
    `bases.py` as a side effect (as `aeth_ext/__init__.py` does for `aeth_ext.logging.bases` in
    the real package -- simulated here via `app/sub/__init__.py`), an eager, class-definition-time
    PROJECT_NAME lookup would run while `__main__` is still a bogus placeholder (no `__file__`)
    and permanently cache `"FIX_ME"`, even though the real app package defines PROJECT_NAME and
    `leaf.py` itself runs correctly as `__main__` afterward (D-copilot-adjacent regression:
    production code first surfaced this via `aeth_ext.central_log_server.test_entrypoint`, a real
    three-level dotted entrypoint whose own `__init__.py` chain imports `bases.py` early)."""
    _write(tmp_path / "app" / "__init__.py", 'PROJECT_NAME = "widgetco"\n')
    _write(tmp_path / "app" / "sub" / "__init__.py", "from aeth_ext.logging.bases import TaggedLogRecord\n")
    _write(
      tmp_path / "app" / "sub" / "leaf.py",
      "from app.sub import TaggedLogRecord\n"
      "\n"
      'record = TaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n'
      "print(record.project_name)\n",
    )

    result = subprocess.run(
      [_PYTHON, "-m", "app.sub.leaf"],
      cwd=tmp_path,
      capture_output=True,
      text=True,
      check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "widgetco"

  def test_a_record_built_during_parent_package_import_does_not_poison_later_resolution(self, tmp_path: Path) -> None:
    """Unlike the previous test (which only *imports* `bases.py` from the parent package), this
    parent package's `__init__.py` actually constructs a `TaggedLogRecord` as it loads -- e.g. a
    diagnostic it logs on its own. `sys.modules["__main__"]` is still the runpy bootstrap
    placeholder at that point (no `__file__`), so this first, premature resolution attempt must
    neither cache `"FIX_ME"` permanently nor raise; `leaf.py`'s own later record, once it's
    genuinely `__main__`, must still resolve the real `PROJECT_NAME` (D-copilot regression)."""
    _write(tmp_path / "app" / "__init__.py", 'PROJECT_NAME = "widgetco"\n')
    _write(
      tmp_path / "app" / "sub" / "__init__.py",
      "from aeth_ext.logging.bases import TaggedLogRecord\n"
      "\n"
      '_bootstrap_record = TaggedLogRecord("test", 20, __file__, 1, "loading", (), None)\n',
    )
    _write(
      tmp_path / "app" / "sub" / "leaf.py",
      "from app.sub import TaggedLogRecord\n"
      "\n"
      'record = TaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n'
      "print(record.project_name)\n",
    )

    result = subprocess.run(
      [_PYTHON, "-m", "app.sub.leaf"],
      cwd=tmp_path,
      capture_output=True,
      text=True,
      check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "widgetco"

  def test_does_not_recurse_when_the_process_log_record_factory_is_already_tagged(self, tmp_path: Path) -> None:
    """Once `logging.setLogRecordFactory(TaggedLogRecord)` is in effect, *every* logger call --
    including `parse_and_grab_constants`'s own diagnostic `logger.debug(...)` -- constructs a
    `TaggedLogRecord`. The first real record triggers `_resolve_project_name()`, which is still
    unresolved (`_project_name` is `None`) while that nested diagnostic call runs, so without a
    reentrancy guard it recurses back into `_resolve_project_name()` and blows the stack with
    `RecursionError` instead of resolving normally (production surfaced this via
    `central_log_server.test_entrypoint`'s handshake path, which sets the factory in
    `LogWriterThread.__init__` via `_configure_logserver` before the first client connects)."""
    _write(tmp_path / "app" / "__init__.py", 'PROJECT_NAME = "widgetco"\n')
    _write(
      tmp_path / "app" / "__main__.py",
      "import logging\n"
      "from aeth_ext.logging.bases import TaggedLogRecord\n"
      "\n"
      "logging.setLogRecordFactory(TaggedLogRecord)\n"
      "logging.getLogger().setLevel(logging.DEBUG)\n"
      'logging.getLogger("aeth_ext.static_eval").setLevel(logging.DEBUG)\n'
      "\n"
      'record = TaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n'
      "print(record.project_name)\n",
    )

    result = subprocess.run(
      [_PYTHON, "-m", "app"],
      cwd=tmp_path,
      capture_output=True,
      text=True,
      check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "widgetco"
