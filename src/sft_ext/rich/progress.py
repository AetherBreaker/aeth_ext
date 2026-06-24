# Standard library imports
from functools import partial
from logging import getLogger
from threading import RLock
from typing import TYPE_CHECKING, cast, override

# Third party imports
from rich import get_console
from rich.live import Live
from rich.progress import (
  BarColumn,
  MofNCompleteColumn,
  Progress as _Progress,
  ProgressColumn,
  Task,
  TaskProgressColumn,
  TextColumn,
  TimeRemainingColumn,
)

if TYPE_CHECKING:
  # Standard library imports
  from types import TracebackType
  from typing import Any, Self

  # Third party imports
  from rich.console import Console
  from rich.progress import GetTimeCallable, TaskID as _TaskID

logger = getLogger(__name__)


__all__ = ["Progress", "TaskID"]


class TaskID(int):
  def __new__(cls, task_id: int, prog_instance: _Progress | type[_Progress], remove: bool = True):
    return super().__new__(cls, task_id)

  @override
  def __init__(self, task_id: int, prog_instance: _Progress | type[_Progress], remove: bool = True):
    self.prog_instance = prog_instance
    self.remove = remove
    self.remove_func = partial(prog_instance.remove_task, self)
    super().__init__()

  def __enter__(self) -> Self:
    return self

  def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    traceback: TracebackType | None,
  ):
    remove = self.remove
    if remove:
      self.remove_func()

  def __copy__(self) -> Self:
    return self

  def __deepcopy__(self, memo: dict[int, Any]) -> Self:
    return self


class Progress(_Progress):
  @override
  def __init__(  # pyright: ignore[reportMissingSuperCall]
    self,
    *columns: str | ProgressColumn,
    console: Console | None,
    auto_refresh: bool = True,
    refresh_per_second: float = 4,
    speed_estimate_period: float = 30.0,
    transient: bool = False,
    redirect_stdout: bool = True,
    redirect_stderr: bool = True,
    get_time: GetTimeCallable | None = None,
    disable: bool = False,
    expand: bool = False,
    live: Live | None = None,
  ) -> None:
    assert refresh_per_second > 0, "refresh_per_second must be > 0"
    if console is None:
      console = get_console()
    self._lock = RLock()
    self.columns = (
      BarColumn(),
      TaskProgressColumn(),
      MofNCompleteColumn(),
      TimeRemainingColumn(),
      TextColumn("[progress.description]{task.description}"),
      *columns,
    )
    self.speed_estimate_period = speed_estimate_period

    self.disable = disable
    self.expand = expand
    self._tasks: dict[TaskID, Task] = {}  # type: ignore
    self._task_index: TaskID = TaskID(0, self)
    self.live = live or Live(
      console=console,
      auto_refresh=auto_refresh,
      refresh_per_second=refresh_per_second,
      transient=transient,
      redirect_stdout=redirect_stdout,
      redirect_stderr=redirect_stderr,
      get_renderable=self.get_renderable,
    )
    self.get_time = get_time or self.console.get_time
    self.print = self.console.print
    self.log = self.console.log

  def add_task(  # type: ignore
    self,
    description: str,
    start: bool = True,
    total: float | None = 100.0,
    completed: int = 0,
    visible: bool = True,
    remove_when_finished: bool = True,
    **fields: Any,
  ) -> TaskID:
    """Add a new 'task' to the Progress display.

    Args:
        description (str): A description of the task.
        start (bool, optional): Start the task immediately (to calculate elapsed time). If set to False,
            you will need to call `start` manually. Defaults to True.
        total (float, optional): Number of total steps in the progress if known.
            Set to None to render a pulsing animation. Defaults to 100.
        completed (int, optional): Number of steps completed so far. Defaults to 0.
        visible (bool, optional): Enable display of the task. Defaults to True.
        **fields (str): Additional data fields required for rendering.

    Returns:
        TaskID: An ID you can use when calling `update`.
    """
    with self._lock:
      task = Task(
        self._task_index,  # type: ignore
        description,
        total,
        completed,
        visible=visible,
        fields=fields,
        _get_time=self.get_time,
        _lock=self._lock,
      )
      self._tasks[self._task_index] = task
      if start:
        self.start_task(self._task_index)  # type: ignore

      new_task_index = self._task_index
      new_task_index.remove = remove_when_finished
      self._task_index = TaskID(int(self._task_index) + 1, self)  # type: ignore
    # self.refresh()
    return new_task_index

  def update(  # type: ignore
    self,
    task_id: TaskID,
    *,
    total: float | None = None,
    completed: float | None = None,
    advance: float | None = None,
    description: str | None = None,
    visible: bool | None = None,
    refresh: bool = False,
    **fields: Any,
  ) -> None:
    return super().update(
      cast("_TaskID", task_id),
      total=total,
      completed=completed,
      advance=advance,
      description=description,
      visible=visible,
      refresh=refresh,
      **fields,
    )

  def remove_task(self, task_id: TaskID) -> None:  # type: ignore
    return super().remove_task(cast("_TaskID", task_id))
