"""Tests for `aeth_ext.ftp.create_ftp_adapter`/`FTPAdapter`/`SFTPAdapter`."""

# Standard library imports
import socket
import threading
from contextvars import ContextVar
from ftplib import FTP
from typing import TYPE_CHECKING

# Third party imports
import paramiko
import pytest
from paramiko import Transport

# First party imports
from aeth_ext.ftp import create_ftp_adapter
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter
from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter
from aeth_ext.ftp.session import AdaptedSFTP
from tests.ftp.conftest import _make_stub_sftp_server, _StubServerInterface  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # First party imports
  from tests.ftp.conftest import _FTPTestEnv  # pyright: ignore[reportPrivateUsage]


class TestFactoryDispatch:
  def test_ftp_credentials_produce_an_ftp_adapter(self) -> None:
    creds = FTPCredentials(host="127.0.0.1", username="anyone", password="anything", port=1)  # pyright: ignore[reportArgumentType]
    adapter = create_ftp_adapter(creds)

    assert isinstance(adapter, FTPAdapter)

  def test_sftp_credentials_produce_an_sftp_adapter(self) -> None:
    creds = SFTPCredentials(host="127.0.0.1", username="anyone", password="anything", port=1)  # pyright: ignore[reportArgumentType]
    adapter = create_ftp_adapter(creds)

    assert isinstance(adapter, SFTPAdapter)


class TestContainerClsResolution:
  def test_plain_string_is_used_directly(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="explicit-name")

    session = adapter.start_session()

    assert session.container_cls == "explicit-name"

  def test_contextvar_is_preferred_when_set(self, ftp_env: _FTPTestEnv) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar")
    cvar.set("from-contextvar")
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "from-contextvar"

  def test_falls_back_to_plain_string_when_contextvar_is_unset(self, ftp_env: _FTPTestEnv) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar_unset")
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "fallback-name"


class TestTestConnectionDelegation:
  def test_delegates_to_a_fresh_sessions_test_connection(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """`FTPAdapter.test_connection` should be a thin delegate to
    `start_session().test_connection(logit)` -- exercised here via a spy
    rather than a real connection, since the real-connection path is already
    covered end-to-end by `AdaptedFTP`/`AdaptedSFTP`'s own test suites."""
    calls: list[bool] = []

    class _FakeSession:
      def test_connection(self, logit: bool = False) -> bool:
        calls.append(logit)
        return True

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))
    # FTPAdapter is __slots__-based, so an instance can't shadow a class method --
    # patch the class itself instead.
    monkeypatch.setattr(FTPAdapter, "start_session", lambda self: _FakeSession())

    result = adapter.test_connection(logit=True)

    assert result is True
    assert calls == [True]


def _ftp_credentials(ftp_env: _FTPTestEnv) -> FTPCredentials:
  """Adapts `_FTPTestEnv` (which builds ready-made `AdaptedFTP`s) into `FTPCredentials` for a fresh
  user+homedir on the same running pyftpdlib server, so these tests can exercise
  `create_ftp_adapter`/`FTPAdapter` itself rather than a pre-built adapter."""
  # Standard library imports
  import uuid

  port = ftp_env._port  # pyright: ignore[reportPrivateUsage]
  authorizer = ftp_env._authorizer  # pyright: ignore[reportPrivateUsage]
  root = ftp_env._root  # pyright: ignore[reportPrivateUsage]

  name = uuid.uuid4().hex
  homedir = root / name
  homedir.mkdir()
  username, password = f"user_{name}", "password"
  authorizer.add_user(username, password, str(homedir), perm="elradfmwMT")

  return FTPCredentials(host="127.0.0.1", username=username, password=password, port=port)  # pyright: ignore[reportArgumentType]


class TestConnectionPooling:
  def test_release_returns_connection_for_reuse(self, ftp_env: _FTPTestEnv) -> None:
    """Releasing a session should make the underlying connection available to
    the next start_session() call, not close it."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      first_handler = first.handler

    with adapter.start_session() as second:
      second_handler = second.handler

    assert second_handler is first_handler

  def test_concurrent_checkouts_up_to_max_connections_do_not_block(self, ftp_env: _FTPTestEnv) -> None:
    max_connections = 3
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=max_connections)

    sessions = [adapter.start_session() for _ in range(max_connections)]
    for s in sessions:
      s.__enter__()

    assert len({s.handler for s in sessions}) == max_connections
    for s in sessions:
      s.__exit__(None, None, None)

  def test_checkout_past_max_connections_blocks_until_release(self, ftp_env: _FTPTestEnv) -> None:
    # Standard library imports
    from threading import Event, Thread

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1)
    held = adapter.start_session()
    held.__enter__()
    got_second: list[object] = []
    unblocked = Event()

    def _checkout_second() -> None:
      session = adapter.start_session()
      session.__enter__()
      got_second.append(session)
      unblocked.set()

    t = Thread(target=_checkout_second, daemon=True)
    t.start()
    assert not unblocked.wait(timeout=0.3), "second checkout should still be blocked"

    held.__exit__(None, None, None)
    assert unblocked.wait(timeout=2), "second checkout should unblock after release"
    t.join(timeout=2)
    assert len(got_second) == 1
    got_second[0].__exit__(None, None, None)  # pyright: ignore[reportAttributeAccessIssue]

  def test_fatal_release_of_the_last_slot_unblocks_a_waiting_checkout(self, ftp_env: _FTPTestEnv) -> None:
    # Standard library imports
    from threading import Event, Thread

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1)
    held = adapter.start_session()
    held.__enter__()
    got_second: list[object] = []
    unblocked = Event()

    def _checkout_second() -> None:
      session = adapter.start_session()
      session.__enter__()
      got_second.append(session)
      unblocked.set()

    t = Thread(target=_checkout_second, daemon=True)
    t.start()
    assert not unblocked.wait(timeout=0.3), "second checkout should still be blocked"

    # A fatal release (not a clean __exit__) frees capacity without ever putting a handle into the
    # idle queue -- before the fix, a waiter blocked on Queue.get() would never learn about it.
    with pytest.raises(ConnectionError), held:
      raise ConnectionError("simulated dead socket")

    assert unblocked.wait(timeout=2), "fatal release of the last slot must wake a blocked checkout, not hang forever"
    t.join(timeout=2)
    assert len(got_second) == 1
    got_second[0].__exit__(None, None, None)  # pyright: ignore[reportAttributeAccessIssue]


class TestConnectionFatalReleaseIsDiscarded:
  def test_connection_error_during_session_discards_the_handler(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    first_handler = session.handler
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    # A fatal exception must not return the handler to the pool -- the next
    # checkout should get a freshly opened one, not the poisoned one.
    with adapter.start_session() as second:
      assert second.handler is not first_handler
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]

  def test_non_fatal_exception_still_returns_handler_to_pool(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    first_handler = session.handler
    with pytest.raises(FileNotFoundError), session:
      raise FileNotFoundError("no such remote file")

    with adapter.start_session() as second:
      assert second.handler is first_handler

  def test_discard_closes_the_handler_directly(self, ftp_env: _FTPTestEnv) -> None:
    """Regression test for a bug where discard tried to invoke a protocol's
    `close_conn_handler` as an unbound method on the handler
    (`close_conn_handler.__func__(handler)`), which silently no-ops against
    any real implementation instead of actually closing the connection.
    Discard must call the handler's own `.close()`/`.quit()` instead."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    handler = session.handler
    assert handler is not None
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    with pytest.raises((OSError, AttributeError)):
      # A closed connection can no longer respond -- `quit()` leaves `self.sock` as `None`, so
      # ftplib raises `AttributeError` here rather than `OSError` (Python 3.14's `ftplib`).
      handler.voidcmd("NOOP")


class TestLazyValidationOnCheckout:
  def test_stale_pooled_connection_is_discarded_and_replaced(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      stale_handler = first.handler
    assert stale_handler is not None

    # Simulate the server having dropped the connection while it sat idle.
    stale_handler.close()

    with adapter.start_session() as second:
      assert second.handler is not None
      assert second.handler is not stale_handler
      # The replacement must be a live, working connection.
      second.handler.voidcmd("NOOP")

  def test_freshly_opened_connection_skips_validation(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that was just opened (not popped from the idle queue)
    must not pay the extra validation round trip -- it was already proven
    live by successfully completing its handshake. Asserted behaviorally
    (no NOOP round trip sent) rather than by spying on a private validation
    method directly, so this doesn't couple to an implementation detail."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)
    calls: list[str] = []
    original_voidcmd = FTP.voidcmd
    monkeypatch.setattr(FTP, "voidcmd", lambda self, cmd: (calls.append(cmd), original_voidcmd(self, cmd))[1])

    with adapter.start_session():
      pass

    assert calls == []


class TestRampUpDiscoversRealCeiling:
  def test_refused_growth_pins_discovered_max(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_connections = 2
    open_count = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_count
      if open_count >= allowed_connections:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1

    monkeypatch.setattr("aeth_ext.ftp.connectors.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    sessions = [adapter.start_session() for _ in range(allowed_connections)]
    for s in sessions:
      s.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()

    assert adapter._discovered_max == allowed_connections  # pyright: ignore[reportPrivateUsage]
    for s in sessions:
      s.__exit__(None, None, None)

  def test_subsequent_checkouts_respect_discovered_max_without_reattempting(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")

    monkeypatch.setattr("aeth_ext.ftp.connectors.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()  # succeeds, open_attempts == 1

    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()  # open_attempts == 2, fails, discovers max=1

    held.__exit__(None, None, None)  # returns the one connection to _idle

    # This checkout must come from _idle (no new open attempt), not retry growth.
    with adapter.start_session():
      pass

    assert open_attempts == expected_open_attempts


class TestRecoveringADiscoveredCeiling:
  def test_reprobe_after_interval_raises_discovered_max_on_success(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    allow_growth = False
    open_count = 0

    def _gated_get_transport(self: object) -> None:
      nonlocal open_count
      if open_count >= 1 and not allow_growth:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1

    monkeypatch.setattr("aeth_ext.ftp.connectors.FTPConnector.get_transport", _gated_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()
    assert adapter._discovered_max == 1  # pyright: ignore[reportPrivateUsage]

    # Simulate the server now allowing more connections, and the reprobe window elapsing.
    allow_growth = True
    recovered_ceiling = 2
    # Standard library imports
    from time import monotonic

    # Comfortably past the adapter's re-probe interval (300s per the design doc) --
    # a fixed large offset avoids depending on the private _REPROBE_INTERVAL value.
    monkeypatch.setattr("aeth_ext.ftp.pool.base.monotonic", lambda: monotonic() + 10_000)

    with adapter.start_session() as second:
      assert second.handler is not None

    assert adapter._discovered_max is None or adapter._discovered_max >= recovered_ceiling  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

  def test_reprobe_within_interval_does_not_reattempt(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")

    monkeypatch.setattr("aeth_ext.ftp.connectors.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()
    assert open_attempts == expected_open_attempts

    held.__exit__(None, None, None)
    with adapter.start_session():
      pass

    # Still within _REPROBE_INTERVAL -- must come from _idle, no new open attempt.
    assert open_attempts == expected_open_attempts


class TestOptInKeepAlive:
  def test_disabled_by_default_spawns_no_thread(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))

    with adapter.start_session():
      pass

    assert adapter._keepalive_thread is None  # pyright: ignore[reportPrivateUsage]

  def test_keepalive_pings_idle_connection_without_touching_checked_out_one(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4, keepalive_interval=0.05)

    with adapter.start_session():
      pass  # released back to _idle

    checked_out = adapter.start_session()
    checked_out.__enter__()  # not released -- must stay untouched
    checked_out_handler = checked_out.handler

    # Standard library imports
    from time import sleep

    sleep(0.2)  # let the keepalive loop tick a few times

    assert checked_out.handler is checked_out_handler
    assert checked_out.handler is not None
    checked_out.handler.voidcmd("NOOP")  # still alive, unpinged connection wasn't broken by concurrent use
    checked_out.__exit__(None, None, None)


class TestConnectionPrewarmsPool:
  def test_test_connection_leaves_a_reusable_connection_pooled(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

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
      "aeth_ext.ftp.pool.base.register_for_shutdown",
      lambda callback, *, phase, priority=0, required=False: registered.append((callback, phase.name)),
    )
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    assert registered == []
    with adapter.start_session():
      pass
    assert len(registered) == 1
    assert registered[0][1] == "THREADED"

    with adapter.start_session():
      pass
    assert len(registered) == 1  # still only once

  def test_shutdown_teardown_closes_idle_connections_only(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session():
      pass  # released -- now idle
    checked_out = adapter.start_session()
    checked_out.__enter__()  # stays checked out

    adapter._shutdown_teardown()  # pyright: ignore[reportPrivateUsage]

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    # The checked-out connection must be untouched by teardown.
    assert checked_out.handler is not None
    checked_out.handler.voidcmd("NOOP")
    checked_out.__exit__(None, None, None)


class TestChunkSizeThreading:
  def test_custom_chunk_size_reaches_the_session(self, ftp_env: _FTPTestEnv) -> None:
    custom_chunk_size = 4096
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4, chunk_size=custom_chunk_size)

    with adapter.start_session() as session:
      assert session.chunk_size == custom_chunk_size

  def test_default_chunk_size_is_8192(self, ftp_env: _FTPTestEnv) -> None:
    default_chunk_size = 8192
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as session:
      assert session.chunk_size == default_chunk_size


class TestConstructorCallbacks:
  def test_download_file_invokes_constructor_callbacks_alongside_the_call_time_one(self, ftp_env: _FTPTestEnv, tmp_path: Path) -> None:
    adapter = ftp_env.make_adapter()
    seen_by_ctor_cb: list[bytes] = []
    seen_by_call_cb: list[bytes] = []
    adapter._callbacks = (seen_by_ctor_cb.append,)  # pyright: ignore[reportPrivateUsage]

    with adapter as ftp:
      assert ftp.handler is not None
      (tmp_path / "src").write_bytes(b"hello world")
      with open(tmp_path / "src", "rb") as f:
        ftp.handler.storbinary("STOR probe", f)
      ftp.download_file("probe", lambda chunk: seen_by_call_cb.append(bytes(chunk)))

    assert b"".join(seen_by_ctor_cb) == b"hello world"
    assert b"".join(seen_by_call_cb) == b"hello world"

  def test_upload_file_taps_constructor_callbacks_with_the_pulled_bytes(self, ftp_env: _FTPTestEnv) -> None:
    adapter = ftp_env.make_adapter()
    seen: list[bytes] = []
    adapter._callbacks = (seen.append,)  # pyright: ignore[reportPrivateUsage]
    chunks = [b"abc", b"def", b""]

    def _source(_size: int) -> bytes:
      return chunks.pop(0)

    with adapter as ftp:
      ftp.upload_file("probe2", _source, file_size=6)

    assert b"".join(seen) == b"abcdef"


class TestStandaloneProviderUsage:
  """Confirms the one-shot, non-pooled usage path (spec Section 4) actually works end-to-end: a
  hand-written `HandleProvider` used to construct `AdaptedFTP` directly, without `FTPAdapter`/
  `create_ftp_adapter` in the picture at all. `ftp_env.make_adapter()` already exercises this same path
  implicitly (see `tests/ftp/conftest.py`'s `_OneShotFTPProvider`) -- this test asserts it explicitly as
  a first-class scenario rather than only incidentally through every other test in this file."""

  def test_upload_then_download_round_trips(self, ftp_env: _FTPTestEnv) -> None:
    session = ftp_env.make_adapter()
    data = b"standalone provider payload"
    chunks = iter([data, b""])

    with session as ftp:
      ftp.upload_file("probe.bin", lambda _size: next(chunks), file_size=len(data))
      received = bytearray()
      ftp.download_file("probe.bin", lambda chunk: received.extend(bytes(chunk)))

    assert bytes(received) == data


class _TestSFTPServer:
  """Runs a persistent, multi-accept loopback paramiko SFTP server for the lifetime of a test, so
  multiplexing tests can dial the same port repeatedly to open more than one `Transport`. Unlike
  `_SFTPTestEnv.make_adapter` (which spins up a single-accept listener per adapter, sufficient for the
  non-pooling adapter tests), the multiplexing tests below need a server that keeps accepting new
  connections for the lifetime of the test."""

  def __init__(self, tmp_path: Path) -> None:
    homedir = tmp_path / "sftp_pool_root"
    homedir.mkdir()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    self.port = listener.getsockname()[1]

    host_key = paramiko.RSAKey.generate(2048)
    sftp_si = _make_stub_sftp_server(str(homedir))

    def _serve_forever() -> None:
      while True:
        try:
          conn, _addr = listener.accept()
        except OSError:
          return
        transport = Transport(conn)
        transport.add_server_key(host_key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_si=sftp_si)
        transport.start_server(server=_StubServerInterface())

    threading.Thread(target=_serve_forever, daemon=True).start()

  def credentials(self) -> SFTPCredentials:
    # host_key_policy="auto_add": this server generates a fresh ephemeral host key per test run with
    # no fixed known_hosts entry, so the default "reject" policy would always fail the handshake.
    return SFTPCredentials(
      host="127.0.0.1",
      username="anyone",
      password="anything",  # pyright: ignore[reportArgumentType]
      port=self.port,
      host_key_policy="auto_add",
    )


class TestSFTPChannelMultiplexing:
  def test_two_checkouts_share_one_transports_channel_cap(self, tmp_path: Path) -> None:
    """Two concurrently-checked-out SFTP sessions should multiplex over the same `Transport`
    instead of each dialing a fresh TCP connection, as long as both fit under
    `channels_per_transport`. Asserted via the pool's own bookkeeping (not a `.transport` attribute
    on the session -- the Adapted classes don't expose one) by checking both handlers resolve to the
    same `TransportState`."""
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()

    state_first = adapter._ledger.handle_states.get(id(first.handler))  # pyright: ignore[reportPrivateUsage]
    state_second = adapter._ledger.handle_states.get(id(second.handler))  # pyright: ignore[reportPrivateUsage]
    assert state_first is state_second
    assert first.handler is not second.handler
    first.__exit__(None, None, None)
    second.__exit__(None, None, None)

  def test_channel_cap_forces_a_second_transport(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=1)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()

    state_first = adapter._ledger.handle_states.get(id(first.handler))  # pyright: ignore[reportPrivateUsage]
    state_second = adapter._ledger.handle_states.get(id(second.handler))  # pyright: ignore[reportPrivateUsage]
    assert state_first is not state_second
    first.__exit__(None, None, None)
    second.__exit__(None, None, None)


class TestThroughputInstrumentation:
  def test_download_updates_the_owning_transports_throughput(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    (tmp_path / "probe_source").write_bytes(b"x" * 4096)
    with adapter.start_session() as session:
      assert isinstance(session, AdaptedSFTP)
      assert session.handler is not None
      session.handler.put(str(tmp_path / "probe_source"), "probe_remote")
      received = bytearray()
      session.download_file("probe_remote", lambda chunk: received.extend(bytes(chunk)))
      state = adapter._ledger.handle_states.get(id(session.handler))  # pyright: ignore[reportPrivateUsage]

    assert state is not None
    assert state.sample_count >= 1
    assert state.ewma_throughput is not None and state.ewma_throughput > 0


class TestSFTPTransportDeathCascade:
  def test_dead_transport_discards_its_idle_channels_too(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()
    state = adapter._ledger.handle_states.get(id(first.handler))  # pyright: ignore[reportPrivateUsage]
    assert state is not None
    assert state is adapter._ledger.handle_states.get(id(second.handler))  # pyright: ignore[reportPrivateUsage]
    first.__exit__(None, None, None)  # released -- now idle on the shared Transport

    dead_transport = state.transport
    with pytest.raises(ConnectionError), second:
      dead_transport.close()  # kill the Transport out from under the still-open session
      raise ConnectionError("simulated transport death")

    assert adapter._ledger.handle_states.get(id(first.handler)) is None  # pyright: ignore[reportPrivateUsage]

    # A fresh checkout must not reuse the now-dead idle channel from `first`.
    third = adapter.start_session()
    third.__enter__()
    third_state = adapter._ledger.handle_states.get(id(third.handler))  # pyright: ignore[reportPrivateUsage]
    assert third_state is not None
    assert third_state.transport is not dead_transport
    third.__exit__(None, None, None)
