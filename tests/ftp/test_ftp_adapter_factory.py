"""Tests for `aeth_ext.ftp.create_ftp_adapter`/`FTPAdapter`/`SFTPAdapter`."""

# Standard library imports
import socket
import threading
from contextvars import ContextVar
from ftplib import FTP
from time import monotonic, sleep
from typing import TYPE_CHECKING

# Third party imports
import paramiko
import pytest
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.errors import HandleReleasedError, PoolClosedError, PoolTimeoutError, ServerCapacityError
from aeth_ext.ftp.factory import create_ftp_adapter
from aeth_ext.ftp.ftp_connector import FTPConnector
from aeth_ext.ftp.pool.base import PooledAdapterBase, WakeupGate
from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter
from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter
from aeth_ext.ftp.session import AdaptedSFTP
from tests.ftp.conftest import _make_stub_sftp_server, _StubServerInterface, wait_until  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from pathlib import Path

  # First party imports
  from aeth_ext.ftp.types import InstrumentCallable
  from aeth_ext.types import SizedBuffer
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
    held.__exit__(None, ConnectionError("simulated dead socket"), None)

    assert unblocked.wait(timeout=2), "fatal release of the last slot must wake a blocked checkout, not hang forever"
    t.join(timeout=2)
    assert len(got_second) == 1
    got_second[0].__exit__(None, None, None)  # pyright: ignore[reportAttributeAccessIssue]


class TestConnectionFatalReleaseIsDiscarded:
  def test_connection_error_during_session_discards_the_handler(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    first_handler: FTP | None = None
    with pytest.raises(ConnectionError), adapter.start_session() as session:
      first_handler = session.handler
      raise ConnectionError("simulated dead socket")

    # A fatal exception must not return the handler to the pool -- the next
    # checkout should get a freshly opened one, not the poisoned one.
    with adapter.start_session() as second:
      assert second.handler is not first_handler
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]

  def test_non_fatal_exception_still_returns_handler_to_pool(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    first_handler: FTP | None = None
    with pytest.raises(FileNotFoundError), adapter.start_session() as session:
      first_handler = session.handler
      raise FileNotFoundError("no such remote file")

    with adapter.start_session() as second:
      assert second.handler is first_handler

  def test_fatal_error_caught_inside_a_nested_with_still_discards_at_depth_zero(self, ftp_env: _FTPTestEnv) -> None:
    """An inner `with session:` that raises a connection-fatal error which the outer block then
    catches must still poison the handle: the inner exit owns no release, and the clean outer exit
    would otherwise hand the broken connection straight back to the idle queue."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    first_handler: FTP | None = None
    with adapter.start_session() as outer:
      first_handler = outer.handler
      try:
        with outer:
          raise ConnectionError("simulated dead socket")
      except ConnectionError:
        pass

    with adapter.start_session() as second:
      assert second.handler is not first_handler

  def test_discard_closes_the_handler_directly(self, ftp_env: _FTPTestEnv) -> None:
    """Regression test for a bug where discard tried to invoke a protocol's
    `close_conn_handler` as an unbound method on the handler
    (`close_conn_handler.__func__(handler)`), which silently no-ops against
    any real implementation instead of actually closing the connection.
    Discard must call the handler's own `.close()`/`.quit()` instead."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    handler: FTP | None = None
    with pytest.raises(ConnectionError), adapter.start_session() as session:
      handler = session.handler
      assert handler is not None
      raise ConnectionError("simulated dead socket")
    assert handler is not None

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
        raise ServerCapacityError("server connection limit reached")
      open_count += 1

    monkeypatch.setattr("aeth_ext.ftp.ftp_connector.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    sessions = [adapter.start_session() for _ in range(allowed_connections)]
    for s in sessions:
      s.__enter__()

    # A refusal while other connections are still live blocks the caller instead of raising (see
    # _open_new_slot's ServerCapacityError handling) -- it still pins _discovered_max before blocking,
    # so this has to observe that through a background waiter rather than an immediate exception.
    waiter_started = threading.Event()

    def _blocked_checkout() -> None:
      waiter_started.set()
      with adapter.start_session():
        pass

    waiter = threading.Thread(target=_blocked_checkout, daemon=True)
    waiter.start()
    assert waiter_started.wait(timeout=5)

    assert wait_until(lambda: adapter._discovered_max == allowed_connections)  # pyright: ignore[reportPrivateUsage]

    for s in sessions:
      s.__exit__(None, None, None)  # frees a slot -- the waiter can now proceed
    waiter.join(timeout=5)
    assert not waiter.is_alive()

  def test_subsequent_checkouts_respect_discovered_max_without_reattempting(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ServerCapacityError("server connection limit reached")

    monkeypatch.setattr("aeth_ext.ftp.ftp_connector.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()  # succeeds, open_attempts == 1

    # A refusal while `held` is still live blocks the caller instead of raising (see _open_new_slot's
    # ServerCapacityError handling), but still discovers/pins the ceiling (open_attempts == 2) before
    # blocking. Releasing `held` here and immediately reacquiring would come straight from _idle
    # regardless of whether the discovered ceiling is honored at all -- idle checkout happens before
    # any growth decision runs. Keep the sole connection checked out instead, and prove the waiter
    # actually blocks on the ceiling (no additional dial) rather than merely finding nothing idle.
    got_second = threading.Event()
    waiter_started = threading.Event()

    def _blocked_checkout() -> None:
      waiter_started.set()
      with adapter.start_session():
        got_second.set()

    waiter = threading.Thread(target=_blocked_checkout, daemon=True)
    waiter.start()
    assert waiter_started.wait(timeout=5)
    assert wait_until(lambda: open_attempts == expected_open_attempts)  # discovers max=1, then blocks

    sleep(0.2)  # give the waiter time to actually reach the blocking retry, not just the failed dial
    assert not got_second.is_set(), "checkout beyond the discovered ceiling must block, not retry growth"
    assert open_attempts == expected_open_attempts  # blocked, not repeatedly re-dialing

    held.__exit__(None, None, None)  # frees the sole slot -- the waiter can now proceed
    assert wait_until(got_second.is_set)
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert open_attempts == expected_open_attempts  # proceeded via idle reuse, no additional dial

  def test_transient_os_error_does_not_pin_discovered_max(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: a bare OSError (timeout, reset, DNS failure, ...) is not evidence of a real
    server-side connection ceiling and must not cap the pool the way a ServerCapacityError does --
    only a connector that explicitly classifies the failure as a capacity refusal should do that."""
    failing_attempt = 2
    open_count = 0

    def _flaky_get_transport(self: object) -> None:
      nonlocal open_count
      open_count += 1
      if open_count == failing_attempt:
        raise ConnectionResetError("simulated transient network failure")

    monkeypatch.setattr("aeth_ext.ftp.ftp_connector.FTPConnector.get_transport", _flaky_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()  # succeeds, open_count == 1

    with pytest.raises(ConnectionResetError):
      adapter.start_session().__enter__()  # open_count == 2, transient failure

    assert adapter._discovered_max is None  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

    # A later attempt must still be free to grow past where the transient error occurred.
    with adapter.start_session() as third:
      assert third.handler is not None


class TestRecoveringADiscoveredCeiling:
  def test_reprobe_after_interval_raises_discovered_max_on_success(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    allow_growth = False
    open_count = 0

    def _gated_get_transport(self: object) -> None:
      nonlocal open_count
      if open_count >= 1 and not allow_growth:
        raise ServerCapacityError("server connection limit reached")
      open_count += 1

    monkeypatch.setattr("aeth_ext.ftp.ftp_connector.FTPConnector.get_transport", _gated_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()

    # A refusal while `held` is still live blocks the caller instead of raising (see _open_new_slot's
    # ServerCapacityError handling) -- it still pins _discovered_max before blocking, so this has to
    # observe that through a background waiter rather than an immediate exception. The waiter stays
    # blocked (nothing frees a slot or elapses the reprobe window yet) until the signal() call below.
    waiter_started = threading.Event()
    second_succeeded = threading.Event()

    def _blocked_checkout() -> None:
      waiter_started.set()
      with adapter.start_session():
        second_succeeded.set()

    waiter = threading.Thread(target=_blocked_checkout, daemon=True)
    waiter.start()
    assert waiter_started.wait(timeout=5)
    assert wait_until(lambda: adapter._discovered_max == 1)  # pyright: ignore[reportPrivateUsage]

    # Simulate the server now allowing more connections, and the reprobe window elapsing.
    allow_growth = True
    recovered_ceiling = 2
    # Standard library imports
    from time import monotonic

    # Comfortably past the adapter's re-probe interval (300s per the design doc) --
    # a fixed large offset avoids depending on the private _REPROBE_INTERVAL value.
    monkeypatch.setattr("aeth_ext.ftp.pool.base.monotonic", lambda: monotonic() + 10_000)
    # The waiter is already parked in a real (up to _REPROBE_INTERVAL-long) wait() call whose timeout
    # was computed before the monkeypatch above took effect -- waiting it out for real would make this
    # test itself take minutes. signal() forces retry_until's next iteration to run immediately instead,
    # which is where the patched clock actually gets consulted.
    adapter._wakeup.signal()  # pyright: ignore[reportPrivateUsage]

    assert wait_until(second_succeeded.is_set)
    waiter.join(timeout=5)
    assert not waiter.is_alive()

    assert adapter._discovered_max is None or adapter._discovered_max >= recovered_ceiling  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

  def test_reprobe_within_interval_does_not_reattempt(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    # ConnectionRefusedError follows _open_new_slot's transient-error branch, which deliberately never
    # sets _discovered_max (see that method's comments) -- only a connector-classified
    # ServerCapacityError does. Using it here would mean this test never actually discovers a ceiling,
    # so it wouldn't be exercising the reprobe-interval policy it's named for at all.
    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ServerCapacityError("server connection limit reached")

    expected_open_attempts = 2
    open_attempts = 0
    monkeypatch.setattr("aeth_ext.ftp.ftp_connector.FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()

    # A refusal while `held` is still live blocks the caller instead of raising (see _open_new_slot's
    # ServerCapacityError handling), but still discovers/pins the ceiling (open_attempts == 2) before
    # blocking. The sole discovered slot stays occupied and we're still within _REPROBE_INTERVAL, so
    # the blocked waiter itself is the proof a further checkout respects the ceiling rather than
    # attempting a new dial -- releasing and immediately reacquiring `held` would instead come straight
    # from _idle regardless of whether the ceiling is honored at all, proving nothing about the reprobe
    # policy.
    got_second = threading.Event()
    waiter_started = threading.Event()

    def _blocked_checkout() -> None:
      waiter_started.set()
      with adapter.start_session():
        got_second.set()

    waiter = threading.Thread(target=_blocked_checkout, daemon=True)
    waiter.start()
    assert waiter_started.wait(timeout=5)
    assert wait_until(lambda: open_attempts == expected_open_attempts)  # discovers max=1, then blocks
    assert adapter._discovered_max == 1  # pyright: ignore[reportPrivateUsage]

    sleep(0.2)  # give the waiter time to actually reach the blocking retry, not just the failed dial
    assert not got_second.is_set(), "checkout beyond the discovered ceiling must block, not reattempt"
    assert open_attempts == expected_open_attempts  # no additional dial was attempted while blocked

    held.__exit__(None, None, None)  # frees the sole slot -- the waiter can now proceed
    assert wait_until(got_second.is_set)
    waiter.join(timeout=5)
    assert not waiter.is_alive()


class TestReprobeDeadlineAvoidsBusySpin:
  """`_time_until_reprobe` is pure bookkeeping over `_discovered_max`/`max_connections` -- no real
  server needed, same style as `test_wakeup_gate.py`'s direct-construction unit tests.

  `_discovered_max_last_probe` is always set relative to a real `monotonic()` reading here, never a
  hardcoded epoch like `0.0` -- `monotonic()` has no fixed epoch (often since-boot on Windows), so on a
  freshly booted CI runner `monotonic()` itself can be well under `_REPROBE_INTERVAL`, making `0.0`
  indistinguishable from "just probed" instead of "long ago" (caught live: a Windows CI runner with
  ~291s of uptime made a `0.0`-anchored elapsed-time assertion fail outright).
  """

  def test_returns_none_once_discovered_max_reaches_max_connections(self) -> None:
    # Once the discovered ceiling has already reached max_connections, _effective_ceiling() can never
    # return anything past max_connections either way -- a reprobe can't accomplish anything. Returning
    # a real (even zero) deadline forever past this point would make every saturated retry_until()
    # waiter spin on a zero-timeout wait instead of blocking for a real signal() (a release).
    adapter = FTPAdapter(FTPCredentials(host="unused", username="unused", password="unused"), max_connections=4)  # pyright: ignore[reportArgumentType]
    adapter._discovered_max = 4  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max_last_probe = monotonic() - 10_000  # pyright: ignore[reportPrivateUsage] -- interval long elapsed

    assert adapter._time_until_reprobe() is None  # pyright: ignore[reportPrivateUsage]

  def test_still_returns_a_deadline_when_discovered_max_is_below_max_connections(self) -> None:
    # Sanity check the fix didn't just always return None -- growth is still genuinely possible here,
    # so a real deadline must still come back right after a probe (interval not yet elapsed).
    adapter = FTPAdapter(FTPCredentials(host="unused", username="unused", password="unused"), max_connections=4)  # pyright: ignore[reportArgumentType]
    adapter._discovered_max = 2  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max_last_probe = monotonic()  # pyright: ignore[reportPrivateUsage] -- just probed

    deadline = adapter._time_until_reprobe()  # pyright: ignore[reportPrivateUsage]
    assert deadline is not None and deadline > 0

  def test_returns_none_while_a_probe_still_holds_the_reopened_window(self) -> None:
    # The window has elapsed, but the probe past the discovered ceiling holds the one extra slot
    # _effective_ceiling() opened, so every other waiter's growth attempt comes back empty at once.
    # Handing them 0.0 (the window is elapsed, and _discovered_max_last_probe is only refreshed when
    # the probe resolves) would spin retry_until on a zero-timeout wait for the whole dial -- which
    # is unbounded, since credentials default connect_timeout to None.
    adapter = FTPAdapter(FTPCredentials(host="unused", username="unused", password="unused"), max_connections=4)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 2  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max = 2  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max_last_probe = monotonic() - 10_000  # pyright: ignore[reportPrivateUsage] -- interval long elapsed
    assert adapter._time_until_reprobe() == 0.0  # pyright: ignore[reportPrivateUsage] -- the window really is open

    dialing = threading.Event()
    finish_dial = threading.Event()

    def _dial() -> object:
      dialing.set()
      assert finish_dial.wait(timeout=5)
      return object()

    probe = threading.Thread(target=lambda: adapter._open_new_slot(_dial), daemon=True)  # pyright: ignore[reportPrivateUsage]
    probe.start()
    try:
      assert dialing.wait(timeout=5)

      assert adapter._time_until_reprobe() is None  # pyright: ignore[reportPrivateUsage]
    finally:
      finish_dial.set()
      probe.join(timeout=5)

    # The probe succeeded, raising _discovered_max to 3 and refreshing the timestamp, so waiters that
    # parked on the None above get a real deadline again the moment the probe's signal() wakes them.
    deadline = adapter._time_until_reprobe()  # pyright: ignore[reportPrivateUsage]
    assert deadline is not None and deadline > 0

  def test_probe_signals_on_success_so_parked_waiters_re_evaluate(self) -> None:
    # A successful probe frees no capacity, so nothing else would wake the waiters parked on the
    # probe-in-flight None above -- they would sleep straight through the next re-probe window, since
    # the probe's own connection may never be released. Asserted on the gate's signal count, since a
    # retry_until() waiter always runs one attempt() on entry regardless of any signal.
    adapter = FTPAdapter(FTPCredentials(host="unused", username="unused", password="unused"), max_connections=4)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 1  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max = 1  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max_last_probe = monotonic() - 10_000  # pyright: ignore[reportPrivateUsage] -- interval long elapsed
    before = adapter._wakeup._epoch  # pyright: ignore[reportPrivateUsage]

    assert adapter._open_new_slot(object) is not None  # pyright: ignore[reportPrivateUsage]

    assert adapter._wakeup._epoch > before  # pyright: ignore[reportPrivateUsage]
    assert adapter._discovered_max == 2  # noqa: PLR2004 # pyright: ignore[reportPrivateUsage] -- the probe raised the ceiling


class TestOptInKeepAlive:
  def test_disabled_by_default_spawns_no_thread(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))

    with adapter.start_session():
      pass

    assert adapter._keepalive_thread is None  # pyright: ignore[reportPrivateUsage]

  def test_keepalive_pings_idle_connection_without_touching_checked_out_one(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    # A second start_session() right after the first releases would just reuse that same connection
    # back out of _idle, leaving nothing genuinely idle for keepalive to ping -- proving nothing about
    # keepalive actually leaving a checked-out connection alone. Hold two distinct connections
    # concurrently instead, release only one, and track which handler(s) actually get NOOP'd.
    pinged: list[int] = []
    original_voidcmd = FTP.voidcmd

    def _tracking_voidcmd(self: FTP, cmd: str) -> object:
      pinged.append(id(self))
      return original_voidcmd(self, cmd)

    monkeypatch.setattr(FTP, "voidcmd", _tracking_voidcmd)

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4, keepalive_interval=0.05)

    idle_session = adapter.start_session()
    idle_session.__enter__()
    checked_out = adapter.start_session()
    checked_out.__enter__()  # a second, distinct connection stays checked out throughout
    checked_out_handler = checked_out.handler

    idle_handler = idle_session.handler
    idle_session.__exit__(None, None, None)  # released -- now genuinely idle
    assert idle_handler is not None

    assert wait_until(lambda: id(idle_handler) in pinged)
    sleep(0.1)  # let a couple more keepalive ticks pass

    assert checked_out.handler is checked_out_handler
    assert checked_out.handler is not None
    assert id(checked_out.handler) not in pinged, "keepalive must not touch a checked-out connection"
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

    # A second start_session() right after the first releases would just reuse that same connection
    # back out of _idle, leaving _idle already empty before teardown ever runs -- the assertion below
    # would then pass even if teardown didn't close idle connections at all. Hold two distinct
    # connections concurrently instead, and release only one so it's genuinely idle at teardown time.
    idle_session = adapter.start_session()
    idle_session.__enter__()
    checked_out = adapter.start_session()
    checked_out.__enter__()  # a second, distinct connection stays checked out throughout

    idle_handler = idle_session.handler
    idle_session.__exit__(None, None, None)  # released -- now genuinely idle

    adapter._shutdown_teardown(())  # pyright: ignore[reportPrivateUsage]

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    assert idle_handler is not None
    with pytest.raises((OSError, AttributeError)):
      # Teardown must have actually closed the idle connection, not just left the queue looking empty.
      idle_handler.voidcmd("NOOP")

    # The checked-out connection must be untouched by teardown.
    assert checked_out.handler is not None
    checked_out.handler.voidcmd("NOOP")
    handler = checked_out.handler
    checked_out.__exit__(None, None, None)

    # A clean release after teardown must close the connection outright, not queue it back into
    # _idle -- nothing ever drains that queue again post-teardown, which would leak it forever.
    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    assert adapter._current_size == 0  # pyright: ignore[reportPrivateUsage]
    with pytest.raises((OSError, AttributeError)):
      # A closed connection can no longer respond -- see test_discard_closes_the_handler_directly's
      # comment on why AttributeError, not just OSError, is expected here on Python 3.14's ftplib.
      handler.voidcmd("NOOP")

  def test_shutdown_observed_mid_reservation_never_dials_and_rolls_back_the_slot(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """A caller reserving a fresh connection slot who then discovers shutdown was already requested
    must never dial -- dial has no timeout (FTPCredentials.connect_timeout defaults to None), so
    starting one into a pool that just tore itself down could block the caller indefinitely on a
    connection nothing will ever use or close. The reserved slot must also be rolled back, not left
    permanently inflated for a connection that was never opened."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    def _fail_if_called(self: FTPConnector, *_args: object, **_kwargs: object) -> FTP:
      pytest.fail("dial must not be called once shutdown is observed inside _open_new_slot")

    monkeypatch.setattr(FTPConnector, "request_handler", _fail_if_called)
    monkeypatch.setattr(PooledAdapterBase, "_ensure_registered_for_shutdown", lambda self: True)

    with pytest.raises(PoolClosedError):
      adapter.start_session().__enter__()

    assert adapter._current_size == 0  # pyright: ignore[reportPrivateUsage]

  def test_close_tears_down_the_pool_deterministically(self, ftp_env: _FTPTestEnv) -> None:
    """A pool must be closable on demand, not only via process shutdown -- `close()`'s registered
    `WeakMethod` callback dies silently if the pool itself is dropped first (D-copilot regression)."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)
    with adapter.start_session():
      pass  # released back to idle

    adapter.close()

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(PoolClosedError):
      adapter.start_session().__enter__()

    adapter.close()  # idempotent -- must not raise a second time

  def test_context_manager_closes_the_pool_on_exit(self, ftp_env: _FTPTestEnv) -> None:
    with create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4) as adapter, adapter.start_session():
      pass

    with pytest.raises(PoolClosedError):
      adapter.start_session().__enter__()


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
    seen_by_ctor_cb: list[SizedBuffer] = []
    seen_by_call_cb: list[bytes] = []
    adapter = ftp_env.make_adapter(callbacks=(seen_by_ctor_cb.append,))

    with adapter as ftp:
      assert ftp.handler is not None
      (tmp_path / "src").write_bytes(b"hello world")
      with open(tmp_path / "src", "rb") as f:
        ftp.handler.storbinary("STOR probe", f)
      ftp.download_file("probe", lambda chunk: seen_by_call_cb.append(bytes(chunk)))

    assert b"".join(seen_by_ctor_cb) == b"hello world"
    assert b"".join(seen_by_call_cb) == b"hello world"

  def test_upload_file_taps_constructor_callbacks_with_the_pulled_bytes(self, ftp_env: _FTPTestEnv) -> None:
    seen: list[SizedBuffer] = []
    adapter = ftp_env.make_adapter(callbacks=(seen.append,))
    chunks = [b"abc", b"def", b""]

    def _source(_size: int) -> bytes:
      return chunks.pop(0)

    with adapter as ftp:
      ftp.upload_file("probe2", _source, file_size=6)

    assert b"".join(seen) == b"abcdef"


class _ReCheckoutProvider:
  """`HandleProvider[SFTPClient]`-shaped double that hands out a *fresh* recording observer on every
  `acquire()`, so the callbacks of one checkout can be told apart from the next one's."""

  def __init__(self) -> None:
    self.checkouts: list[list[bytes]] = []
    # Never touched for I/O -- the session only ever stores it and hands it back to release().
    self._handle = SFTPClient.__new__(SFTPClient)

  def acquire(self) -> tuple[SFTPClient, Sequence[InstrumentCallable]]:
    seen: list[bytes] = []
    self.checkouts.append(seen)
    return self._handle, (lambda data: seen.append(bytes(data)),)

  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    pass


class TestPerCheckoutCallbackScoping:
  def test_provider_observers_do_not_accumulate_across_session_re_entry(self) -> None:
    """A provider's observers are bound to the handle being checked out (for SFTP, to its
    `Transport`'s throughput state), so `__enter__` must rebuild the combined tuple from the
    constructor-supplied callbacks rather than appending to the previous checkout's. Appending would
    keep every earlier checkout's observers alive, reporting each later chunk to stale state once
    more per reuse."""
    provider = _ReCheckoutProvider()
    ctor_seen: list[bytes] = []
    session = AdaptedSFTP(provider, container_cls="test", callbacks=(lambda data: ctor_seen.append(bytes(data)),))

    with session:
      session._notify(b"a")  # pyright: ignore[reportPrivateUsage]
    with session:
      session._notify(b"b")  # pyright: ignore[reportPrivateUsage]

    assert provider.checkouts == [[b"a"], [b"b"]]
    assert ctor_seen == [b"a", b"b"]  # session-lifetime, so they see every checkout's chunks


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

    # Several chunks' worth (chunk_size defaults to 8192): the first observer call only establishes the
    # timing baseline, so a single-chunk transfer records no throughput sample at all by design.
    (tmp_path / "probe_source").write_bytes(b"x" * 65536)
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


class TestAcquireTimeout:
  """`acquire_timeout` bounds how long a blocked `acquire()` waits for capacity. Without it a pool
  whose holders never release wedges the caller permanently, with no diagnostic."""

  def test_defaults_to_a_finite_budget(self, ftp_env: _FTPTestEnv) -> None:
    # A hung acquire should always surface eventually, even for a caller that configured nothing.
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))

    assert adapter._acquire_timeout == 30.0  # noqa: PLR2004 # pyright: ignore[reportPrivateUsage]

  def test_raises_once_the_budget_elapses_at_capacity(self, ftp_env: _FTPTestEnv) -> None:
    budget = 0.2
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1, acquire_timeout=budget)

    with adapter.start_session():  # holds the pool's only slot for the duration
      started = monotonic()
      with pytest.raises(PoolTimeoutError):
        adapter.start_session().__enter__()
      elapsed = monotonic() - started

    assert budget <= elapsed < budget * 25, f"waited {elapsed}s, expected to give up at ~{budget}s"

  def test_a_freed_connection_still_wins_the_race(self, ftp_env: _FTPTestEnv) -> None:
    # The budget must not pre-empt a release that lands inside it -- only a genuinely stuck pool.
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1, acquire_timeout=10.0)
    released = threading.Event()

    def hold_briefly() -> None:
      with adapter.start_session():
        sleep(0.15)
      released.set()

    holder = threading.Thread(target=hold_briefly, daemon=True)
    holder.start()
    sleep(0.02)  # let the holder take the only slot first
    with adapter.start_session() as waiter:
      assert waiter.handler is not None
    holder.join(timeout=5)

    assert released.is_set()

  def test_none_disables_the_budget(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), acquire_timeout=None)

    assert adapter._acquire_timeout is None  # pyright: ignore[reportPrivateUsage]

  @pytest.mark.parametrize("bad", [0, -1.0])
  def test_rejects_a_non_positive_budget(self, ftp_env: _FTPTestEnv, bad: float) -> None:
    with pytest.raises(ValueError, match="acquire_timeout must be positive or None"):
      create_ftp_adapter(_ftp_credentials(ftp_env), acquire_timeout=bad)

  def test_an_expired_budget_still_gets_one_attempt(self) -> None:
    # retry_until runs attempt() before consulting the clock, so a handle that is already sitting
    # there is handed over rather than refused on a technicality.
    gate = WakeupGate()

    assert gate.retry_until(lambda: "handle", timeout=0.000001) == "handle"


class TestConcurrentProbesEachHoldTheWindow:
  """Two probes can be in flight at once: a release landing mid-probe drops `_current_size` back
  below the reopened ceiling, letting a second caller reserve and dial too. Tracking that with a
  flag rather than a count lets whichever probe finishes first hand `0.0` back to every waiter while
  the other dial is still running -- reopening the zero-timeout spin the block exists to prevent."""

  def test_the_window_stays_blocked_until_the_last_probe_finishes(self) -> None:
    adapter = FTPAdapter(FTPCredentials(host="unused", username="unused", password="unused"), max_connections=8)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 1  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max = 1  # pyright: ignore[reportPrivateUsage]
    adapter._discovered_max_last_probe = monotonic() - 10_000  # pyright: ignore[reportPrivateUsage] -- window long open

    dialing = threading.Barrier(3, timeout=5)  # both probers plus this thread
    finish_first = threading.Event()
    finish_second = threading.Event()

    def probe(release_on_entry: bool, done: threading.Event) -> None:
      def _dial() -> object:
        if release_on_entry:
          # What a concurrent release does: frees the slot the second prober needs to get in.
          with adapter._size_lock:  # pyright: ignore[reportPrivateUsage]
            adapter._current_size -= 1  # pyright: ignore[reportPrivateUsage]
        dialing.wait()
        assert done.wait(timeout=5)
        return object()

      adapter._open_new_slot(_dial)  # pyright: ignore[reportPrivateUsage]

    first = threading.Thread(target=probe, args=(True, finish_first), daemon=True)
    first.start()
    # The first prober frees a slot inside its dial, so this second reservation also lands at the
    # discovered ceiling and counts as a probe.
    second = threading.Thread(target=lambda: None, daemon=True)
    while adapter._current_size != 1:  # pyright: ignore[reportPrivateUsage] -- wait for that release
      sleep(0.005)
    second = threading.Thread(target=probe, args=(False, finish_second), daemon=True)
    second.start()
    dialing.wait()
    try:
      assert adapter._probes_in_flight == 2, "both reservations should count as probes"  # noqa: PLR2004 # pyright: ignore[reportPrivateUsage]

      finish_first.set()
      first.join(timeout=5)

      # The surviving probe still holds the reopened window; handing 0.0 back now would spin every
      # other waiter for as long as its dial runs.
      assert adapter._probes_in_flight == 1  # pyright: ignore[reportPrivateUsage]
      assert adapter._time_until_reprobe() is None  # pyright: ignore[reportPrivateUsage]
    finally:
      finish_first.set()
      finish_second.set()
      first.join(timeout=5)
      second.join(timeout=5)

    assert adapter._probes_in_flight == 0  # pyright: ignore[reportPrivateUsage]
    assert adapter._time_until_reprobe() is not None, "with no probe left the window must reopen"  # pyright: ignore[reportPrivateUsage]


class TestBudgetSurvivesASignallingPool:
  """The epoch re-check short-circuits straight back to `attempt()`. With the budget checked after
  it, a pool signalling faster than a caller can win a handle keeps that caller looping past its
  `acquire_timeout` indefinitely."""

  def test_timeout_still_fires_while_signals_keep_landing(self) -> None:
    gate = WakeupGate()
    outcome: list[BaseException | None] = []

    def attempt_and_signal() -> None:
      # retry_until samples the epoch just before calling attempt(), so signalling from inside it
      # guarantees the epoch differs by the time the re-check runs -- the `continue` path taken on
      # every single pass, deterministically, instead of only when a racing thread happens to land.
      gate.signal()

    def wait_for_nothing() -> None:
      try:
        gate.retry_until(attempt_and_signal, timeout=0.2)
      except BaseException as exc:  # noqa: BLE001 -- recorded and asserted on below
        outcome.append(exc)
      else:
        outcome.append(None)

    waiter = threading.Thread(target=wait_for_nothing, daemon=True)
    waiter.start()
    waiter.join(timeout=15)  # joined rather than waited inline so a regression fails instead of hanging

    assert not waiter.is_alive(), "retry_until never gave up despite its budget elapsing"
    assert isinstance(outcome[0], PoolTimeoutError)


class TestListdirGuardSurvivesHandleReuse:
  """A pool with few connections routinely hands the very same handle object back on the next
  checkout, so `self.handler is handler` cannot tell "still my checkout" from "released, someone
  else used it, and now I have it again". The guard keys on a checkout counter instead."""

  def test_re_entering_the_same_session_invalidates_an_open_iterator(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1)
    session = adapter.start_session()
    with session as ftp:
      for name in ("a.txt", "b.txt"):
        chunks = iter([b"x", b""])
        ftp.upload_file(name, lambda _n, c=chunks: next(c), file_size=1)
      entries = ftp.listdir(".")
      next(iter(entries))  # binds the iterator to *this* checkout; an unstarted one binds lazily
      first_handler = ftp.handler

    with session as ftp:
      # Same object back out of a one-connection pool: identity alone would wave the stale iterator
      # through, even though anyone could have used the connection in between.
      assert ftp.handler is first_handler
      with pytest.raises(HandleReleasedError):
        next(iter(entries))
