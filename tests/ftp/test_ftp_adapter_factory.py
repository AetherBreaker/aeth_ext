"""Tests for `aeth_ext.ftp.adapter.FTPAdapter` (the protocol-dispatching factory)."""

# Standard library imports
from contextvars import ContextVar
from typing import TYPE_CHECKING, override

# Third party imports
import pytest

# First party imports
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP, FTPAdapter
from aeth_ext.ftp.types import FTPProtocol, ProtocolEnum, SFTPProtocol

if TYPE_CHECKING:
  # Standard library imports
  from ftplib import FTP

  # Third party imports
  from paramiko import SFTPClient

  # First party imports
  from tests.ftp.conftest import _FTPTestEnv


class _NoOpFTPProtocol(FTPProtocol):
  """`FTPProtocol` test double whose `get_conn_handler()` returns a dummy
  handler instead of a real connection -- `FTPAdapter.start_session()` opens
  a connection eagerly (to fill the pool), so this can no longer raise
  `NotImplementedError` the way a lazily-connecting double could."""

  @override
  def get_conn_handler(self) -> FTP:
    return object()  # pyright: ignore[reportReturnType]

  @override
  def close_conn_handler(self) -> None:
    pass


class _NoOpSFTPProtocol(SFTPProtocol):
  """See `_NoOpFTPProtocol` -- same reasoning for the SFTP side."""

  @override
  def get_conn_handler(self) -> SFTPClient:
    return object()  # pyright: ignore[reportReturnType]

  @override
  def close_conn_handler(self) -> None:
    pass


class TestProtocolResolution:
  def test_ftp_protocol_subclass_resolves_to_adapted_ftp(self) -> None:
    adapter = FTPAdapter(_NoOpFTPProtocol)

    assert adapter.protocol_handler is AdaptedFTP

  def test_sftp_protocol_subclass_resolves_to_adapted_sftp(self) -> None:
    adapter = FTPAdapter(_NoOpSFTPProtocol)

    assert adapter.protocol_handler is AdaptedSFTP

  def test_unrelated_type_raises_type_error(self) -> None:
    class NotAProtocol:
      pass

    with pytest.raises(TypeError):
      FTPAdapter(NotAProtocol)  # pyright: ignore[reportArgumentType]


class TestContainerClsResolution:
  def test_plain_string_is_used_directly(self) -> None:
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="explicit-name")

    session = adapter.start_session()

    assert session.container_cls == "explicit-name"

  def test_contextvar_is_preferred_when_set(self) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar")
    cvar.set("from-contextvar")
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "from-contextvar"

  def test_falls_back_to_plain_string_when_contextvar_is_unset(self) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar_unset")
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "fallback-name"


class TestTestConnectionDelegation:
  def test_delegates_to_a_fresh_sessions_test_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """`FTPAdapter.test_connection` should be a thin delegate to
    `start_session().test_connection(logit)` -- exercised here via a spy
    rather than a real connection, since the real-connection path is already
    covered end-to-end by `AdaptedFTP`/`AdaptedSFTP`'s own test suites."""
    calls: list[bool] = []

    class _FakeSession:
      def test_connection(self, logit: bool = False) -> bool:
        calls.append(logit)
        return True

    adapter = FTPAdapter(_NoOpFTPProtocol)
    # FTPAdapter is __slots__-based, so an instance can't shadow a class method --
    # patch the class itself instead.
    monkeypatch.setattr(FTPAdapter, "start_session", lambda self: _FakeSession())

    result = adapter.test_connection(logit=True)

    assert result is True
    assert calls == [True]


class _TestFTPProtocolFactory:
  """Adapts `_FTPTestEnv` (which builds ready-made `AdaptedFTP`s) into a
  `type[FTPProtocol]`-shaped callable `FTPAdapter.__init__` can call directly,
  so these tests can exercise `FTPAdapter` itself rather than a pre-built adapter."""

  def __new__(cls, ftp_env: "_FTPTestEnv"):
    # Standard library imports
    from ftplib import FTP

    port = ftp_env._port  # pyright: ignore[reportPrivateUsage]
    authorizer = ftp_env._authorizer  # pyright: ignore[reportPrivateUsage]
    root = ftp_env._root  # pyright: ignore[reportPrivateUsage]

    # Standard library imports
    import uuid

    name = uuid.uuid4().hex
    homedir = root / name
    homedir.mkdir()
    username, password = f"user_{name}", "password"
    authorizer.add_user(username, password, str(homedir), perm="elradfmwMT")

    class _Protocol(FTPProtocol):
      KIND = ProtocolEnum.FTP

      def get_conn_handler(self) -> FTP:
        conn = FTP()
        conn.connect("127.0.0.1", port)
        conn.login(username, password)
        return conn

      def close_conn_handler(self) -> None:
        pass  # handler closed by caller in these tests

    return _Protocol


class TestConnectionPooling:
  def test_release_returns_connection_for_reuse(self, ftp_env: "_FTPTestEnv"):
    """Releasing a session should make the underlying connection available to
    the next start_session() call, not close it."""
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      first_handler = first.handler

    with adapter.start_session() as second:
      second_handler = second.handler

    assert second_handler is first_handler

  def test_concurrent_checkouts_up_to_max_connections_do_not_block(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=3)

    sessions = [adapter.start_session() for _ in range(3)]

    assert len({s.handler for s in sessions}) == 3
    for s in sessions:
      s.__exit__(None, None, None)

  def test_checkout_past_max_connections_blocks_until_release(self, ftp_env: "_FTPTestEnv"):
    # Standard library imports
    from threading import Event, Thread

    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=1)
    held = adapter.start_session()
    got_second: list[object] = []
    unblocked = Event()

    def _checkout_second():
      session = adapter.start_session()
      got_second.append(session)
      unblocked.set()

    t = Thread(target=_checkout_second, daemon=True)
    t.start()
    assert not unblocked.wait(timeout=0.3), "second checkout should still be blocked"

    held.__exit__(None, None, None)
    assert unblocked.wait(timeout=2), "second checkout should unblock after release"
    t.join(timeout=2)
    assert len(got_second) == 1
