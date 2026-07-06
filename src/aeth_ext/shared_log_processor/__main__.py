# Standard library imports
from asyncio import run
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Annotated

# Third party imports
import typer
from aiologic import Queue

# First party imports
from aeth_ext import initialize
from aeth_ext.logging.config import BaseLoggingConfig
from aeth_ext.shared_log_processor import main

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.shared_log_processor.server.dispatch import WriterItem


def cli(
  host: str = "localhost",
  port: Annotated[int, typer.Argument()] = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Annotated[Path | None, typer.Argument()] = None,
) -> None:
  print("IGNORE EVERYTHING BEFORE THIS LINE AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
  log_queue: Queue[WriterItem] = Queue()
  initialize(asyncio=True, logging=True)

  BaseLoggingConfig._configure_logserver(log_queue)  # pyright: ignore[reportPrivateUsage]

  kwargs = {
    "log_queue": log_queue,
    "host": host,
    "port": port,
  }
  if log_dir is not None:
    kwargs["log_dir"] = log_dir

  run(main(**kwargs))


if __name__ == "__main__":
  typer.run(cli)
