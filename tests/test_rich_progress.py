"""Tests for `aeth_ext.rich.progress.Progress`/`TaskID`."""

# Standard library imports
import copy

# Third party imports
from rich.console import Console

# First party imports
from aeth_ext.rich.progress import Progress

_ADVANCE_AMOUNT = 5
_TOTAL_STEPS = 200.0
_COMPLETED_STEPS = 10


def _progress() -> Progress:
  """A `Progress` with a real, non-terminal `Console` and auto-refresh off (no background thread)."""
  return Progress(console=Console(file=None, force_terminal=False), auto_refresh=False)


class TestConsoleDefaulting:
  def test_none_console_falls_back_to_get_console(self) -> None:
    # Third party imports
    from rich import get_console

    progress = Progress(console=None, auto_refresh=False)

    assert progress.console is get_console()

  def test_explicit_console_is_used_directly(self) -> None:
    console = Console(file=None, force_terminal=False)

    progress = Progress(console=console, auto_refresh=False)

    assert progress.console is console


class TestAddTask:
  def test_returns_an_incrementing_task_id_starting_at_zero(self) -> None:
    progress = _progress()

    first = progress.add_task("first")
    second = progress.add_task("second")

    assert int(first) == 0
    assert int(second) == 1

  def test_task_is_registered_with_given_total_and_completed(self) -> None:
    progress = _progress()

    task_id = progress.add_task("work", total=_TOTAL_STEPS, completed=_COMPLETED_STEPS)

    task = progress.tasks[0]
    assert task.id == task_id
    assert task.total == _TOTAL_STEPS
    assert task.completed == _COMPLETED_STEPS

  def test_start_false_leaves_the_task_unstarted(self) -> None:
    progress = _progress()

    task_id = progress.add_task("lazy", start=False)

    assert progress._tasks[task_id].start_time is None  # pyright: ignore[reportPrivateUsage]

  def test_start_true_is_the_default_and_starts_the_task(self) -> None:
    progress = _progress()

    task_id = progress.add_task("eager")

    assert progress._tasks[task_id].start_time is not None  # pyright: ignore[reportPrivateUsage]


class TestUpdateAndRemove:
  def test_update_advances_completed(self) -> None:
    progress = _progress()
    task_id = progress.add_task("work", total=100.0)

    progress.update(task_id, advance=_ADVANCE_AMOUNT)

    assert progress.tasks[0].completed == _ADVANCE_AMOUNT

  def test_remove_task_drops_it_from_tasks(self) -> None:
    progress = _progress()
    task_id = progress.add_task("work")

    progress.remove_task(task_id)

    assert progress.tasks == []


class TestTaskIDContextManager:
  def test_exiting_the_with_block_removes_the_task_by_default(self) -> None:
    progress = _progress()

    with progress.add_task("scoped"):
      assert len(progress.tasks) == 1

    assert progress.tasks == []

  def test_task_is_removed_even_when_the_block_raises(self) -> None:
    progress = _progress()

    try:
      with progress.add_task("scoped"):
        raise ValueError("boom")
    except ValueError:
      pass

    assert progress.tasks == []

  def test_remove_when_finished_false_keeps_the_task_after_the_block(self) -> None:
    progress = _progress()

    with progress.add_task("persistent", remove_when_finished=False):
      pass

    assert len(progress.tasks) == 1

  def test_task_id_survives_copy_and_deepcopy_as_the_same_object(self) -> None:
    progress = _progress()
    task_id = progress.add_task("work")

    assert copy.copy(task_id) is task_id
    assert copy.deepcopy(task_id) is task_id
