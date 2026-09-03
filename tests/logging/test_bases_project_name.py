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
      'from aeth_ext.logging.bases import TaggedLogRecord\n\nTaggedLogRecord("test", 20, __file__, 1, "hello", (), None)\n',
    )

    assert result.returncode != 0
    assert "Expected project name to be set, but got 'FIX_ME'" in result.stderr

  def test_picks_up_project_name_for_a_deep_dotted_submodule_entrypoint(self, tmp_path: Path) -> None:
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

  def test_raises_immediately_for_a_permanently_file_less_deviation(self, tmp_path: Path) -> None:
    result = subprocess.run(
      [
        _PYTHON,
        "-c",
        ("from aeth_ext.logging.bases import TaggedLogRecord\nTaggedLogRecord('test', 20, 'fake.py', 1, 'hello', (), None)\n"),
      ],
      cwd=tmp_path,
      capture_output=True,
      text=True,
      check=False,
    )

    assert result.returncode != 0
    assert "Expected project name to be set, but got 'FIX_ME'" in result.stderr

  def test_does_not_recurse_when_the_process_log_record_factory_is_already_tagged(self, tmp_path: Path) -> None:
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
