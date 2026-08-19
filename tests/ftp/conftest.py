"""Shared pytest fixtures for the FTP/SFTP adapter test suite.

Both fixtures run a real local server (a real `pyftpdlib` FTP server, a real
loopback `paramiko` SSH/SFTP server) rather than faking sockets, so the tests
exercise the actual `ftplib`/`paramiko` wire protocols `aeth_ext.ftp`
talks to.
"""

# Standard library imports
import contextlib
import os
import socket
import threading
import uuid
from typing import TYPE_CHECKING, override

# Third party imports
import paramiko
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

# First party imports
from aeth_ext.ftp.session import AdaptedFTP, AdaptedSFTP

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Sequence
  from ftplib import FTP
  from pathlib import Path

  # First party imports
  from aeth_ext.ftp.types import IntrumentCallable


# ---------------------------------------------------------------------------
# FTP: one shared pyftpdlib server per test, isolated via a unique user+homedir.
# ---------------------------------------------------------------------------


class _OneShotFTPProvider:
  """Minimal `HandleProvider[FTP]`-shaped test double: connects/disconnects a real `ftplib.FTP`
  directly against a known host/port/user, without going through `FTPAdapter`/`create_ftp_adapter` --
  exercises the standalone (non-pooled) usage path `AdaptedFTP` supports."""

  def __init__(self, port: int, username: str, password: str) -> None:
    self._port = port
    self._username = username
    self._password = password
    self._conn: FTP | None = None

  def acquire(self) -> tuple[FTP, Sequence[IntrumentCallable]]:
    # Standard library imports
    from ftplib import FTP

    conn = FTP()
    conn.connect("127.0.0.1", self._port)
    conn.login(self._username, self._password)
    self._conn = conn
    return conn, ()

  def release(self, handle: FTP, is_fatal: bool) -> None:
    try:
      handle.quit()
    except OSError:
      handle.close()
    self._conn = None


class _FTPTestEnv:
  def __init__(self, port: int, authorizer: DummyAuthorizer, root: Path) -> None:
    self._port = port
    self._authorizer = authorizer
    self._root = root

  def make_adapter(self, container_cls: str = "test", callbacks: Sequence[IntrumentCallable] = ()) -> AdaptedFTP:
    name = uuid.uuid4().hex
    homedir = self._root / name
    homedir.mkdir()
    username, password = f"user_{name}", "password"
    self._authorizer.add_user(username, password, str(homedir), perm="elradfmwMT")
    provider = _OneShotFTPProvider(self._port, username, password)
    return AdaptedFTP(provider, container_cls=container_cls, callbacks=callbacks)


@pytest.fixture
def ftp_env(tmp_path: Path) -> Iterator[_FTPTestEnv]:
  root = tmp_path / "ftp_root"
  root.mkdir()

  authorizer = DummyAuthorizer()

  class _Handler(FTPHandler):
    pass

  _Handler.authorizer = authorizer

  server = FTPServer(("127.0.0.1", 0), _Handler)
  port = server.address[1]
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()

  try:
    yield _FTPTestEnv(port, authorizer, root)
  finally:
    server.close_all()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# SFTP: a fresh loopback paramiko server per adapter, rooted at a real directory.
# ---------------------------------------------------------------------------

_SFTP_OK: int = paramiko.SFTP_OK  # pyright: ignore[reportAttributeAccessIssue]


class _StubServerInterface(paramiko.ServerInterface):
  @override
  def check_auth_password(self, username: str, password: str) -> int:
    return paramiko.AUTH_SUCCESSFUL  # pyright: ignore[reportAttributeAccessIssue]

  @override
  def check_channel_request(self, kind: str, chanid: int) -> int:
    return paramiko.OPEN_SUCCEEDED  # pyright: ignore[reportAttributeAccessIssue]

  @override
  def get_allowed_auths(self, username: str) -> str:
    return "password"


class _StubSFTPHandle(paramiko.SFTPHandle):
  """An `SFTPHandle` that answers FSTAT requests via the real open file descriptor."""

  @override
  def stat(self) -> paramiko.SFTPAttributes:
    return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))  # pyright: ignore[reportAttributeAccessIssue]


def _make_stub_sftp_server(root: str) -> type[paramiko.SFTPServerInterface]:  # noqa: C901
  """Build an `SFTPServerInterface` rooted at `root` on the real filesystem."""

  class _StubSFTPServer(paramiko.SFTPServerInterface):
    def _realpath(self, path: str) -> str:
      return root + self.canonicalize(path)

    @override
    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes]:
      p = self._realpath(path)
      out: list[paramiko.SFTPAttributes] = []
      for fname in os.listdir(p):
        attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(p, fname)))
        attr.filename = fname
        out.append(attr)
      return out

    @override
    def stat(self, path: str) -> paramiko.SFTPAttributes:
      try:
        return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))
      except OSError as e:
        return paramiko.SFTPServer.convert_errno(e.errno or 0)  # pyright: ignore[reportReturnType]

    @override
    def lstat(self, path: str) -> paramiko.SFTPAttributes:
      return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))

    @override
    def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes) -> paramiko.SFTPHandle:
      p = self._realpath(path)
      flags |= getattr(os, "O_BINARY", 0)
      mode = getattr(attr, "st_mode", None) or 0o666
      fd = os.open(p, flags, mode)
      f = os.fdopen(fd, "rb" if (flags & os.O_WRONLY) == 0 and (flags & os.O_RDWR) == 0 else "wb")
      handle = _StubSFTPHandle(flags)
      handle.readfile = f  # pyright: ignore[reportAttributeAccessIssue]
      handle.writefile = f  # pyright: ignore[reportAttributeAccessIssue]
      return handle

    @override
    def remove(self, path: str) -> int:
      os.remove(self._realpath(path))
      return _SFTP_OK

    @override
    def rename(self, oldpath: str, newpath: str) -> int:
      os.rename(self._realpath(oldpath), self._realpath(newpath))
      return _SFTP_OK

    @override
    def mkdir(self, path: str, attr: paramiko.SFTPAttributes) -> int:
      os.mkdir(self._realpath(path))
      return _SFTP_OK

  return _StubSFTPServer


class _OneShotSFTPProvider:
  """Minimal `HandleProvider[SFTPClient]`-shaped test double: connects/disconnects a real
  `paramiko.SFTPClient` directly against a known port, without going through
  `SFTPAdapter`/`create_ftp_adapter`."""

  def __init__(self, port: int) -> None:
    self._port = port
    self._transport: paramiko.Transport | None = None

  def acquire(self) -> tuple[paramiko.SFTPClient, Sequence[IntrumentCallable]]:
    transport = paramiko.Transport(("127.0.0.1", self._port))
    transport.connect(username="anyone", password="anything")
    self._transport = transport
    return paramiko.SFTPClient.from_transport(transport), ()  # pyright: ignore[reportReturnType]

  def release(self, handle: paramiko.SFTPClient, is_fatal: bool) -> None:
    handle.close()
    if self._transport is not None:
      self._transport.close()
      self._transport = None


class _SFTPTestEnv:
  def __init__(self, root: Path) -> None:
    self._root = root
    self._servers: list[paramiko.Transport] = []
    self._listeners: list[socket.socket] = []

  def make_adapter(self, container_cls: str = "test") -> AdaptedSFTP:
    name = uuid.uuid4().hex
    homedir = self._root / name
    homedir.mkdir()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    self._listeners.append(listener)

    host_key = paramiko.RSAKey.generate(2048)
    sftp_si = _make_stub_sftp_server(str(homedir))
    servers = self._servers

    def _serve() -> None:
      try:
        conn, _addr = listener.accept()
      except OSError:
        # The listener was closed during test teardown before a client ever
        # connected (e.g. a test that intentionally never opens this adapter).
        return
      transport = paramiko.Transport(conn)
      servers.append(transport)
      transport.add_server_key(host_key)
      transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_si=sftp_si)
      transport.start_server(server=_StubServerInterface())

    threading.Thread(target=_serve, daemon=True).start()

    provider = _OneShotSFTPProvider(port)
    return AdaptedSFTP(provider, container_cls=container_cls)

  def close(self) -> None:
    for server in self._servers:
      server.close()
    for listener in self._listeners:
      listener.close()


@pytest.fixture
def sftp_env(tmp_path: Path) -> Iterator[_SFTPTestEnv]:
  root = tmp_path / "sftp_root"
  root.mkdir()

  env = _SFTPTestEnv(root)
  try:
    yield env
  finally:
    env.close()


@pytest.fixture
def make_ftp_adapter(ftp_env: _FTPTestEnv) -> Callable[[], AdaptedFTP]:
  return ftp_env.make_adapter


@pytest.fixture
def make_sftp_adapter(sftp_env: _SFTPTestEnv) -> Callable[[], AdaptedSFTP]:
  return sftp_env.make_adapter


# ---------------------------------------------------------------------------
# Progress-bar spy, matching the `pbar.add_task(...)` / `pbar.update(...)` shape
# `AdaptedFTP`/`AdaptedSFTP` call against `aeth_ext.rich.progress.Progress`.
# ---------------------------------------------------------------------------


class FakeProgress:
  def __init__(self) -> None:
    self.tasks: list[tuple[str, int | None]] = []
    self.updates: list[tuple[int, int]] = []

  def add_task(self, description: str, total: int | None = None) -> contextlib.AbstractContextManager[int]:
    task_id = len(self.tasks)
    self.tasks.append((description, total))
    return contextlib.nullcontext(task_id)

  def update(self, task_id: int, advance: int) -> None:
    self.updates.append((task_id, advance))


@pytest.fixture
def fake_progress() -> FakeProgress:
  return FakeProgress()
