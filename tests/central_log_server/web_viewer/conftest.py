"""Shared fixtures/helpers for Pilot-driven tests of the web viewer's Textual screens."""

# Standard library imports
from typing import TYPE_CHECKING, Any

# Third party imports
from textual.app import App

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from pathlib import Path

  # Third party imports
  from textual.pilot import Pilot
  from textual.screen import Screen
  from textual.widgets._directory_tree import DirEntry
  from textual.widgets._tree import TreeNode

_EXPAND_ATTEMPTS = 30
_EXPAND_POLL = 0.1


class ScreenHostApp(App[None]):
  """Minimal test-only `App` that mounts a single, already-constructed screen on startup.

  `LogStreamScreen` and `LogPickerScreen` both expect to be pushed onto a
  running app rather than run standalone, and each carries real behaviour
  (CSS lookup relative to its own module, `self.app.push_screen_wait`, etc.)
  that a fake/stub host would risk diverging from. This gives every test in
  this package the same minimal, real `App` to mount an individual screen
  into, so tests can drive it with a genuine `Pilot` without going through
  the full `LogWebViewApp` picker-to-stream flow every time. That full flow
  is exercised separately in `test_app.py`.
  """

  def __init__(self, screen: Screen[Any]) -> None:
    super().__init__()
    self._screen = screen

  def on_mount(self) -> None:
    self.push_screen(self._screen)


def write_log_file(directory: Path, name: str, lines: Sequence[str]) -> Path:
  """Write *lines* (newline-joined, trailing newline included) to *directory*/*name*."""
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / name
  path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
  return path


async def expand_and_get_child(
  pilot: Pilot[Any], dir_node: TreeNode[DirEntry], child_name: str
) -> TreeNode[DirEntry]:
  """Expand a `DirectoryTree` node and poll until its named child appears.

  `TreeNode.expand()` only *requests* the expansion -- the actual directory
  listing is loaded asynchronously via `DirectoryTree`'s own load-queue
  worker (see `DirectoryTree._add_to_load_queue`), so the child isn't
  necessarily present on the node yet by the time `expand()` returns.
  """
  dir_node.expand()
  for _ in range(_EXPAND_ATTEMPTS):
    await pilot.pause(_EXPAND_POLL)
    for child in dir_node.children:
      if child.data is not None and child.data.path.name == child_name:
        return child
  raise AssertionError(f"child {child_name!r} never appeared under {dir_node!r}")
