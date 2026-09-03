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
  def __init__(self, screen: Screen[Any]) -> None:
    super().__init__()
    self._screen = screen

  def on_mount(self) -> None:
    self.push_screen(self._screen)


def write_log_file(directory: Path, name: str, lines: Sequence[str]) -> Path:
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / name
  path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
  return path


async def expand_and_get_child(pilot: Pilot[Any], dir_node: TreeNode[DirEntry], child_name: str) -> TreeNode[DirEntry]:
  dir_node.expand()
  for _ in range(_EXPAND_ATTEMPTS):
    await pilot.pause(_EXPAND_POLL)
    for child in dir_node.children:
      if child.data is not None and child.data.path.name == child_name:
        return child
  raise AssertionError(f"child {child_name!r} never appeared under {dir_node!r}")
