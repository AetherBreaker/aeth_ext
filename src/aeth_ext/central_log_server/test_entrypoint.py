"""A lightweight, embeddable `central_log_server` entrypoint for other programs' test suites.

Unlike ``run-app-central-log-server`` (`central_log_server/__main__.py`), which
is the real production entrypoint, this script is meant to be spawned as a
short-lived subprocess by another project's own test fixtures: it binds to
ephemeral ports by default, reports the ports it actually bound as a single
JSON line on stdout once ready, and shuts down gracefully as soon as its
stdin pipe is closed (the parent's signal to stop).

The boot/shutdown sequence below is deliberately its own, dedicated,
minimal implementation -- it does *not* call the production
`central_log_server.startup.main`. Wiring the lower-level server/writer/
registry pieces directly here keeps this entrypoint lightweight and lets it
evolve independently (e.g. new ports, new defaults) without risking any
change to the real production boot path, which changes rarely and should
stay untouched by test-only concerns.

There is deliberately no in-process Python API here -- only this CLI. A
subprocess-per-instance design sidesteps :data:`aeth_ext.errors.FATAL_EVENT`
being a one-shot, process-global event: since each invocation gets its own
fresh interpreter, there is no risk of one test's server "using up" the event
and leaving a second, same-process instance unable to start.

Typical usage from a caller's own test fixture::

    proc = subprocess.Popen(
        [sys.executable, "-m", "aeth_ext.central_log_server.test_entrypoint", "run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    ready = json.loads(proc.stdout.readline())
    log_port = ready["log_port"]
    ...
    proc.stdin.close()  # request graceful shutdown
    proc.wait(timeout=10)

If ``--log-dir`` is omitted, ``run`` creates its own temp directory and
reports it back in the ready line (``ready["log_dir"]``). By default that
directory is left behind after shutdown so a caller can inspect it -- e.g.
after a failing test -- without racing the server's own exit. A caller that
doesn't need the logs at all can either pass ``run --auto-cleanup`` (deletes
its own temp dir, but never a caller-supplied ``--log-dir``, on shutdown) or
invoke the separate ``cleanup <log_dir>`` command once it knows the logs are
no longer needed.
"""

# Standard library imports
import asyncio
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from sys import platform
from typing import TYPE_CHECKING, Annotated

# Third party imports
import typer
from aiologic import Queue

# First party imports
from aeth_ext import initialize
from aeth_ext.central_log_server import web_viewer
from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
from aeth_ext.central_log_server.server.reader_server import LogRecordServer
from aeth_ext.central_log_server.server.writer_thread import LogWriterThread
from aeth_ext.central_log_server.settings import Settings
from aeth_ext.central_log_server.web_viewer.server import InLoopServer
from aeth_ext.errors import FATAL_EVENT
from aeth_ext.logging.setup import BaseLoggingConfig

if TYPE_CHECKING:
  # Third party imports
  from aiohttp import web

  # First party imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem

app = typer.Typer()

_FAVICON_PATH = Path(__file__).parent / "web_viewer" / "favicon.ico"
_TEMPLATES_PATH = Path(__file__).parent / "web_viewer" / "templates"


def _watch_for_shutdown_signal() -> None:
  """Block until the parent process closes our stdin, then set `FATAL_EVENT`.

  Closing stdin (rather than sending an OS signal) is the shutdown request
  mechanism here, since Windows has no reliable, uniform way for a parent to
  send POSIX-style signals to a subprocess. This runs in a plain background
  thread rather than via `asyncio.connect_read_pipe`: on Windows,
  `ProactorEventLoop` can only treat a pipe as async-readable if it was
  opened in overlapped mode, which an inherited stdin handle typically is
  not -- attempting it silently hangs forever instead of erroring, whereas a
  blocking read in its own thread works portably. `FATAL_EVENT` is an
  `aiologic.Event`, which is thread/task safe by design, so setting it here
  correctly wakes up the `await FATAL_EVENT` waiter on the event loop.
  """
  sys.stdin.buffer.read()  # blocks until the parent closes its end (EOF)
  FATAL_EVENT.set()


def _report_ready(log_port: int, web_viewer_port: int | None, log_dir: Path) -> None:
  """Print the actual bound port(s) and resolved log dir as a single JSON line, then flush immediately."""
  print(json.dumps({"log_port": log_port, "web_viewer_port": web_viewer_port, "log_dir": str(log_dir)}), flush=True)


def _report_startup_error(exc: BaseException) -> None:
  """Print a startup failure as a single JSON line on stdout, mirroring `_report_ready`'s protocol.

  Lets a caller blocked on `proc.stdout.readline()` distinguish "server
  failed to start, here's why" from an unexplained EOF, without having to go
  parse `stderr`'s traceback itself.
  """
  print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)


async def _run(  # noqa: PLR0917
  log_queue: Queue[WriterItem],
  host: str,
  port: int,
  log_dir: Path,
  with_web_viewer: bool,
  web_viewer_host: str,
  web_viewer_port: int,
) -> None:
  """Boot a minimal log server (and, optionally, the web viewer), then run until shutdown.

  A from-scratch, purpose-built lifecycle for this lightweight test
  entrypoint: it wires the same lower-level pieces the production entrypoint
  uses (`LogWriterThread`, `LogRecordServer`, `ClientIdRegistry`), but skips
  everything about `central_log_server.startup.main` that only matters for
  the real deployed process (heartbeat file, periodic heartbeat task).
  """
  writer: LogWriterThread | None = None
  tcp_server: asyncio.Server | None = None
  runner: web.AppRunner | None = None
  try:
    id_registry = ClientIdRegistry.load()

    writer = LogWriterThread(log_queue, id_registry, server_config=BaseLoggingConfig._configure_logserver(log_queue))  # pyright: ignore[reportPrivateUsage]
    writer.start()

    server = LogRecordServer(queue=log_queue, id_registry=id_registry, host=host, port=port, log_dir=log_dir)
    tcp_server = await server.start_server()
    actual_log_port: int = tcp_server.sockets[0].getsockname()[1]

    actual_web_viewer_port: int | None = None
    if with_web_viewer:
      settings = Settings.get_settings()
      textual_server = InLoopServer(
        command=f"python -m {web_viewer.__name__}",
        host=web_viewer_host,
        port=web_viewer_port,
        title="aeth_ext test log server",
        public_url=(settings.web_viewer_public_url if platform != "win32" else f"http://localhost:{web_viewer_port}"),
        favicon_path=_FAVICON_PATH,
        templates_path=_TEMPLATES_PATH,
      )
      runner = await textual_server.serve_in_loop(__debug__)
      actual_web_viewer_port = runner.addresses[0][1]
  except Exception as exc:
    # Anything above can fail after `writer` is already running; since its
    # shutdown is driven solely by `FATAL_EVENT` (see `LogWriterThread`) and
    # it is not a daemon thread, leaving that unset here would hang the
    # process on that thread forever instead of exiting on this exception.
    _report_startup_error(exc)
    FATAL_EVENT.set()
    if tcp_server is not None:
      tcp_server.close()
      await tcp_server.wait_closed()
    if runner is not None:
      # Reachable at runtime (e.g. IndexError/AttributeError from the line
      # below): pyright doesn't model subscript exprs as raising, so with
      # nothing after `actual_web_viewer_port = runner.addresses[0][1]`
      # inside `try`, it can't see this branch as reachable.
      await runner.cleanup()  # pyright: ignore[reportUnreachable]
    if writer is not None:
      writer.join()
    raise

  _report_ready(actual_log_port, actual_web_viewer_port, log_dir)

  # daemon=True so this thread (blocked in a synchronous read) never keeps the
  # process alive on its own -- FATAL_EVENT getting set some other way (e.g. an
  # unhandled exception) still lets the process exit without waiting on it.
  threading.Thread(target=_watch_for_shutdown_signal, daemon=True).start()
  async with tcp_server:
    try:
      await FATAL_EVENT
    finally:
      FATAL_EVENT.set()
      tcp_server.close()
      await tcp_server.wait_closed()
      if runner is not None:
        await runner.cleanup()
      writer.join()


@app.command("cleanup")
def cleanup(log_dir: Annotated[Path, typer.Argument()]) -> None:
  """Remove a log directory previously reported by `run`.

  Standalone from `run` so a caller that wants to inspect a server's logs
  after a failing test -- rather than have the server guess at whether
  they're still needed at its own shutdown time -- can defer deletion until
  it actually knows the outcome, by simply not calling this when it wants to
  keep them.
  """
  shutil.rmtree(log_dir, ignore_errors=True)


@app.command("run")
def run(  # noqa: PLR0917
  host: Annotated[str, typer.Option()] = "127.0.0.1",
  port: Annotated[int, typer.Option()] = 0,
  log_dir: Annotated[Path | None, typer.Option()] = None,
  auto_cleanup: Annotated[
    bool,
    typer.Option(
      help="Delete the auto-created temp log dir on shutdown. Ignored if --log-dir was given explicitly "
      "-- a caller-supplied directory is always the caller's to clean up."
    ),
  ] = False,
  with_web_viewer: Annotated[bool, typer.Option()] = False,
  web_viewer_host: Annotated[str, typer.Option()] = "127.0.0.1",
  web_viewer_port: Annotated[int, typer.Option()] = 0,
) -> None:
  """Run a lightweight `central_log_server` instance for another program's test suite.

  Defaults to binding both the log server and (if enabled) the web viewer on
  OS-assigned ephemeral ports; the actual bound port(s) and resolved log dir
  are reported as a single JSON line on stdout once ready. The web viewer is
  off by default.
  """
  log_queue: Queue[WriterItem] = Queue()
  initialize(asyncio=True, logging=False)

  owns_log_dir = log_dir is None
  resolved_log_dir = log_dir if log_dir is not None else Path(tempfile.mkdtemp(prefix="aeth_ext_test_log_server_"))

  try:
    asyncio.run(
      _run(
        log_queue=log_queue,
        host=host,
        port=port,
        log_dir=resolved_log_dir,
        with_web_viewer=with_web_viewer,
        web_viewer_host=web_viewer_host,
        web_viewer_port=web_viewer_port,
      )
    )
  finally:
    if owns_log_dir and auto_cleanup:
      shutil.rmtree(resolved_log_dir, ignore_errors=True)


if __name__ == "__main__":
  app()
