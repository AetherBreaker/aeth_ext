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
  from tests.ftp.conftest import _FTPTestEnv  # pyright: ignore[reportPrivateUsage]


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

  def __new__(cls, ftp_env: _FTPTestEnv) -> type[FTPProtocol]:
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

      @override
      def get_conn_handler(self) -> FTP:
        conn = FTP()
        conn.connect("127.0.0.1", port)
        conn.login(username, password)
        return conn

      @override
      def close_conn_handler(self) -> None:
        pass  # handler closed by caller in these tests

    return _Protocol


class TestConnectionPooling:
  def test_release_returns_connection_for_reuse(self, ftp_env: _FTPTestEnv) -> None:
    """Releasing a session should make the underlying connection available to
    the next start_session() call, not close it."""
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      first_handler = first.handler

    with adapter.start_session() as second:
      second_handler = second.handler

    assert second_handler is first_handler

  def test_concurrent_checkouts_up_to_max_connections_do_not_block(self, ftp_env: _FTPTestEnv) -> None:
    max_connections = 3
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=max_connections)

    sessions = [adapter.start_session() for _ in range(max_connections)]

    assert len({s.handler for s in sessions}) == max_connections
    for s in sessions:
      s.__exit__(None, None, None)

  def test_checkout_past_max_connections_blocks_until_release(self, ftp_env: _FTPTestEnv) -> None:
    # Standard library imports
    from threading import Event, Thread

    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=1)
    held = adapter.start_session()
    got_second: list[object] = []
    unblocked = Event()

    def _checkout_second() -> None:
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


class TestConnectionFatalReleaseIsDiscarded:
  def test_connection_error_during_session_discards_the_handler(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    session = adapter.start_session()
    first_handler = session.handler
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    # A fatal exception must not return the handler to the pool -- the next
    # checkout should get a freshly opened one, not the poisoned one.
    with adapter.start_session() as second:
      assert second.handler is not first_handler
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]

  def test_non_fatal_exception_still_returns_handler_to_pool(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    session = adapter.start_session()
    first_handler = session.handler
    with pytest.raises(FileNotFoundError), session:
      raise FileNotFoundError("no such remote file")

    with adapter.start_session() as second:
      assert second.handler is first_handler

  def test_discard_closes_the_handler_directly(self) -> None:
    """Regression test for a bug where `_discard` tried to invoke the
    protocol's `close_conn_handler` as an unbound method on the handler
    (`close_conn_handler.__func__(handler)`), which silently no-ops against
    any real implementation instead of actually closing the connection.
    `_discard` must call the handler's own `.close()` instead."""

    class _FakeHandler:
      def __init__(self) -> None:
        self.closed = False

      def close(self) -> None:
        self.closed = True

    class _FakeProtocol(FTPProtocol):
      @override
      def get_conn_handler(self) -> FTP:
        return _FakeHandler()  # pyright: ignore[reportReturnType]

      @override
      def close_conn_handler(self) -> None:
        pass  # never expected to be called by _discard

    adapter = FTPAdapter(_FakeProtocol, max_connections=4)

    session = adapter.start_session()
    handler = session.handler
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    assert handler.closed is True


class TestLazyValidationOnCheckout:
  def test_stale_pooled_connection_is_discarded_and_replaced(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      stale_handler = first.handler

    # Simulate the server having dropped the connection while it sat idle.
    stale_handler.close()

    with adapter.start_session() as second:
      assert second.handler is not stale_handler
      # The replacement must be a live, working connection.
      second.handler.voidcmd("NOOP")

  def test_freshly_opened_connection_skips_validation(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that was just opened (not popped from the idle queue)
    must not pay the extra validation round trip -- it was already proven
    live by successfully completing its handshake. Asserted behaviorally
    (no NOOP round trip sent) rather than by spying on `FTPAdapter._validate`
    directly, so this doesn't couple to a private implementation detail."""
    # Standard library imports
    from ftplib import FTP

    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)
    calls: list[str] = []
    original_voidcmd = FTP.voidcmd
    monkeypatch.setattr(FTP, "voidcmd", lambda self, cmd: (calls.append(cmd), original_voidcmd(self, cmd))[1])

    with adapter.start_session():
      pass

    assert calls == []


class TestRampUpDiscoversRealCeiling:
  def test_refused_growth_pins_discovered_max(self, ftp_env: _FTPTestEnv) -> None:
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    allowed_connections = 2
    open_count = 0

    def _limited_get(self: FTPProtocol) -> FTP:
      nonlocal open_count
      if open_count >= allowed_connections:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1
      return real_get(self)

    protocol_cls.get_conn_handler = _limited_get
    adapter = FTPAdapter(protocol_cls, max_connections=16)

    sessions = [adapter.start_session() for _ in range(allowed_connections)]
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session()

    assert adapter._discovered_max == allowed_connections  # pyright: ignore[reportPrivateUsage]
    for s in sessions:
      s.__exit__(None, None, None)

  def test_subsequent_checkouts_respect_discovered_max_without_reattempting(self, ftp_env: _FTPTestEnv) -> None:
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get(self: FTPProtocol) -> FTP:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")
      return real_get(self)

    protocol_cls.get_conn_handler = _limited_get
    adapter = FTPAdapter(protocol_cls, max_connections=16)

    held = adapter.start_session()  # succeeds, open_attempts == 1

    with pytest.raises(ConnectionRefusedError):
      adapter.start_session()  # open_attempts == 2, fails, discovers max=1

    held.__exit__(None, None, None)  # returns the one connection to _idle

    # This checkout must come from _idle (no new open attempt), not retry growth.
    with adapter.start_session():
      pass

    assert open_attempts == expected_open_attempts


class TestRecoveringADiscoveredCeiling:
  def test_reprobe_after_interval_raises_discovered_max_on_success(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    allow_growth = False
    open_count = 0

    def _gated_get(self: FTPProtocol) -> FTP:
      nonlocal open_count
      if open_count >= 1 and not allow_growth:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1
      return real_get(self)

    protocol_cls.get_conn_handler = _gated_get
    adapter = FTPAdapter(protocol_cls, max_connections=16)

    held = adapter.start_session()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session()
    assert adapter._discovered_max == 1  # pyright: ignore[reportPrivateUsage]

    # Simulate the server now allowing more connections, and the reprobe window elapsing.
    allow_growth = True
    recovered_ceiling = 2
    # Standard library imports
    from time import monotonic

    # Comfortably past FTPAdapter's re-probe interval (300s per the design doc) --
    # a fixed large offset avoids depending on the private _REPROBE_INTERVAL value.
    monkeypatch.setattr("aeth_ext.ftp.adapter.monotonic", lambda: monotonic() + 10_000)

    with adapter.start_session() as second:
      assert second.handler is not None

    assert adapter._discovered_max is None or adapter._discovered_max >= recovered_ceiling  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

  def test_reprobe_within_interval_does_not_reattempt(self, ftp_env: _FTPTestEnv) -> None:
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get(self: FTPProtocol) -> FTP:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")
      return real_get(self)

    protocol_cls.get_conn_handler = _limited_get
    adapter = FTPAdapter(protocol_cls, max_connections=16)

    held = adapter.start_session()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session()
    assert open_attempts == expected_open_attempts

    held.__exit__(None, None, None)
    with adapter.start_session():
      pass

    # Still within _REPROBE_INTERVAL -- must come from _idle, no new open attempt.
    assert open_attempts == expected_open_attempts


class TestOptInKeepAlive:
  def test_disabled_by_default_spawns_no_thread(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env))

    with adapter.start_session():
      pass

    assert adapter._keepalive_thread is None  # pyright: ignore[reportPrivateUsage]

  def test_keepalive_pings_idle_connection_without_touching_checked_out_one(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4, keepalive_interval=0.05)

    with adapter.start_session():
      pass  # released back to _idle

    checked_out = adapter.start_session()  # not released -- must stay untouched
    checked_out_handler = checked_out.handler

    # Standard library imports
    from time import sleep

    sleep(0.2)  # let the keepalive loop tick a few times

    assert checked_out.handler is checked_out_handler
    checked_out.handler.voidcmd("NOOP")  # still alive, unpinged connection wasn't broken by concurrent use
    checked_out.__exit__(None, None, None)


class TestConnectionPrewarmsPool:
  def test_test_connection_leaves_a_reusable_connection_pooled(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    assert adapter.test_connection() is True

    with adapter.start_session():
      pass

    # If test_connection()'s session had been closed instead of pooled, this
    # would open a second connection instead of reusing the first.
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]


class TestShutdownIntegration:
  def test_registers_for_shutdown_on_first_connection(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[tuple[object, str]] = []
    monkeypatch.setattr(
      "aeth_ext.ftp.adapter.register_for_shutdown",
      lambda callback, *, phase, priority=0, required=False: registered.append((callback, phase.name)),
    )
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    assert registered == []
    with adapter.start_session():
      pass
    assert len(registered) == 1
    assert registered[0][1] == "THREADED"

    with adapter.start_session():
      pass
    assert len(registered) == 1  # still only once

  def test_shutdown_teardown_closes_idle_connections_only(self, ftp_env: _FTPTestEnv) -> None:
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session():
      pass  # released -- now idle
    checked_out = adapter.start_session()  # stays checked out

    adapter._shutdown_teardown()  # pyright: ignore[reportPrivateUsage]

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    # The checked-out connection must be untouched by teardown.
    checked_out.handler.voidcmd("NOOP")
    checked_out.__exit__(None, None, None)
