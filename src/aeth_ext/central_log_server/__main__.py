"""Typer entrypoint for the central log server process."""

# Standard library imports
from asyncio import run
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Annotated

# Third party imports
import typer
from aiologic import SimpleQueue

# First party imports
from aeth_ext import initialize
from aeth_ext.central_log_server.startup import main
from aeth_ext.logging.setup import BaseLoggingConfig

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem

app = typer.Typer()


@app.command()
def cli(
  host: Annotated[str, typer.Argument()] = "0.0.0.0",
  port: Annotated[int, typer.Argument()] = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Annotated[Path | None, typer.Argument()] = None,
) -> None:
  """Boot the server on *host*:*port*, filing logs beneath *log_dir* (the settings default when omitted)."""
  # Must be a SimpleQueue, not a Queue: the root QueueForwardHandler puts onto
  # this from the asyncio event loop thread synchronously, which a mutex-based
  # aiologic.Queue can deadlock. See QueueForwardHandler's docstring.
  log_queue: SimpleQueue[WriterItem] = SimpleQueue()
  initialize(asyncio=True, logging=False)

  server_config = BaseLoggingConfig._configure_logserver(log_queue)  # pyright: ignore[reportPrivateUsage]

  kwargs = {
    "log_queue": log_queue,
    "host": host,
    "port": port,
    "server_config": server_config,
  }
  if log_dir is not None:
    kwargs["log_dir"] = log_dir

  run(main(**kwargs))


if __name__ == "__main__":
  app()
