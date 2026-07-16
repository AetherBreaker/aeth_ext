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
  log_queue: Queue[WriterItem] = Queue()
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
