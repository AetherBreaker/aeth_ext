# FTP/SFTP Connection Pooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `FTPAdapter` reuse live FTP/SFTP connections across calls to `start_session()` instead of opening a brand-new connection (full TCP + login/SSH handshake) every time, cutting the dominant per-file cost in `scheduled-invoice-processor`'s batch transfers.

**Architecture:** All pooling state and logic lives directly on `FTPAdapter` (no new classes). `start_session()` becomes a checkout that first tries an idle-connection queue, then opens a new connection if under a discovered/configured ceiling, then blocks. `AdaptedFTP`/`AdaptedSFTP.__exit__` return the handler to the pool instead of closing it, unless the session raised a connection-fatal exception. Optional lazy validation (cheap no-op) guards against handing out a stale pooled connection, optional keep-alive pings idle connections on a timer, and the adapter registers itself for shutdown to drain the pool cleanly.

**Tech Stack:** Python 3.14, `queue.Queue`, `threading.Lock`/`Thread`, `ftplib`, `paramiko`, `aeth_ext.errors.shutdown.register_for_shutdown`, pytest with real local `pyftpdlib`/`paramiko` loopback servers (existing `tests/ftp/conftest.py` fixtures).

**Spec:** [2026-08-13-ftp-connection-pooling-design.md](2026-08-13-ftp-connection-pooling-design.md)

## Global Constraints

- No changes to `AdapterProtocol`, `AdaptedFTP`, or `AdaptedSFTP`'s public surface — pooling is invisible above `FTPAdapter`. (Their `__exit__` internals do change — see Task 2.)
- No new top-level classes. All new state and logic lives on `FTPAdapter` itself.
- No changes required in `scheduled-invoice-processor` or any other consumer — every existing `with adapter.start_session() as client:` call site must keep working unmodified.
- New constructor parameters (`max_connections`, `keepalive_interval`) must be optional with defaults that preserve today's `1` fresh-connection-per-call behavior being replaced by pooling transparently (i.e. no consumer opt-in required to benefit).
- `FTPAdapter` is `__slots__`-based (`src/aeth_ext/ftp/adapter.py:526`) — every new instance attribute must be added to `__slots__`.
- `max_connections` default is `16`, matching `to_thread`'s default executor worker count.
- Re-probe interval for a discovered ceiling (`_REPROBE_INTERVAL`) is `300` seconds.

---

### Task 1: Pooled checkout/release — core contract

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.__init__`, `FTPAdapter.start_session`, new `FTPAdapter._release`, new `FTPAdapter._open_new`)
- Modify: `src/aeth_ext/ftp/adapter.py` (`AdaptedFTP.__exit__`, `AdaptedSFTP.__exit__`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: existing `FTPProtocol`/`SFTPProtocol.get_conn_handler()`/`close_conn_handler()` (`src/aeth_ext/ftp/types.py`), existing `AdaptedFTP.__init__(ftp_protocol, container_cls, pbar, tzinfo)` (unchanged signature).
- Produces: `FTPAdapter.__init__(..., max_connections: int = 16)`, `FTPAdapter._current_size: int`, `FTPAdapter._idle: queue.Queue`, `FTPAdapter._size_lock: threading.Lock`. Later tasks (2-5) build on this checkout/release pair.

This task lands the minimal pool: fixed ceiling (`max_connections`), no validation, no ramp-up/backoff, no keep-alive, no shutdown registration — just "reuse connections up to a fixed cap, block past it." Later tasks layer behavior on top without changing this shape.

- [x] **Step 1: Write the failing test for basic reuse**

Add to `tests/ftp/test_ftp_adapter_factory.py`:

```python
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
```

`ftp_env` is the existing fixture from `tests/ftp/conftest.py`. `_TestFTPProtocolFactory` is a new small test helper (add it near the top of `tests/ftp/test_ftp_adapter_factory.py`) that wraps `ftp_env.make_adapter()`'s underlying `_TestFTPProtocol` construction so `FTPAdapter` can be given a real `type[FTPProtocol]`-shaped factory instead of a pre-built adapter — since `FTPAdapter.__init__` takes a *protocol class*, not an instance:

```python
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

    class _Protocol:
      KIND = ProtocolEnum.FTP

      def get_conn_handler(self) -> FTP:
        conn = FTP()
        conn.connect("127.0.0.1", port)
        conn.login(username, password)
        return conn

      def close_conn_handler(self) -> None:
        pass  # handler closed by caller in these tests

    return _Protocol
```

Add the needed imports at the top of the test file:

```python
from aeth_ext.ftp.types import ProtocolEnum

if TYPE_CHECKING:
  from tests.ftp.conftest import _FTPTestEnv
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestConnectionPooling -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'max_connections'` (or similar), since `FTPAdapter` doesn't accept it yet.

- [x] **Step 3: Implement the pooled checkout/release**

In `src/aeth_ext/ftp/adapter.py`, update `FTPAdapter`:

```python
class FTPAdapter[HandlerType_T: AdaptedFTP | AdaptedSFTP]:
  __slots__ = (
    "_current_size",
    "_idle",
    "_size_lock",
    "container_cls",
    "container_cvar",
    "ftp_protocol",
    "max_connections",
    "pbar",
    "protocol_handler",
    "tzinfo",
  )

  def __init__(
    self,
    ftp_protocol: type[FTPProtocol | SFTPProtocol],
    container_cls: str | None = None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cvar: ContextVar[str] | None = None,
    max_connections: int = 16,
  ):
    self.container_cvar = container_cvar
    self.container_cls = container_cls
    self.ftp_protocol = ftp_protocol
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.max_connections = max_connections

    if issubclass(ftp_protocol, FTPProtocol):
      self.protocol_handler = AdaptedFTP
      self.ftp_protocol = ftp_protocol
    elif issubclass(ftp_protocol, SFTPProtocol):  # pyright: ignore[reportUnnecessaryIsInstance]
      self.protocol_handler = AdaptedSFTP
      self.ftp_protocol = ftp_protocol
    else:
      raise TypeError(f"Unsupported protocol type: {ftp_protocol}")  # pyright: ignore[reportUnreachable]

    # Standard library imports
    from queue import Queue
    from threading import Lock

    self._idle = Queue(maxsize=max_connections)
    self._current_size = 0
    self._size_lock = Lock()

    super().__init__()

  def _resolve_container_cls(self) -> str | None:
    try:
      if self.container_cvar is not None:
        return self.container_cvar.get()
      return self.container_cls
    except LookupError:
      return self.container_cls

  def _open_new(self):
    return self.ftp_protocol().get_conn_handler()

  def start_session(self) -> HandlerType_T:
    container_cls = self._resolve_container_cls()

    handler = None
    try:
      handler = self._idle.get_nowait()
    except Exception:  # noqa: BLE001 -- queue.Empty, narrowed in Task 3's validation pass
      pass

    if handler is None:
      with self._size_lock:
        if self._current_size < self.max_connections:
          self._current_size += 1
          handler = self._open_new()

    if handler is None:
      handler = self._idle.get()

    session = self.protocol_handler(self.ftp_protocol(), container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo)  # type: ignore
    session.handler = handler
    session._pool = self  # pyright: ignore[reportAttributeAccessIssue]
    return session  # pyright: ignore[reportReturnType]

  def _release(self, handler) -> None:
    self._idle.put(handler)

  def test_connection(self, logit: bool = False) -> bool:
    return self.start_session().test_connection(logit)
```

Note: `session.handler = handler` after construction works because `AdaptedFTP.__enter__`/`AdaptedSFTP.__enter__` currently *set* `self.handler` themselves via `get_conn_handler()` — this task changes that flow so the handler comes from the pool instead. Update `__enter__` on both classes to no-op if `self.handler` is already set by the factory, and only call `get_conn_handler()` when constructed the old way (kept for the test doubles in `tests/ftp/conftest.py` that construct `AdaptedFTP`/`AdaptedSFTP` directly, bypassing `FTPAdapter`):

```python
# AdaptedFTP and AdaptedSFTP, identical change to each:
__slots__ = (..., "_pool")  # add to both classes' existing __slots__ tuples

def __init__(self, ftp_protocol, container_cls, pbar=None, tzinfo=SETTINGS.tz):
  ...
  self._pool = None  # set by FTPAdapter.start_session() when pool-backed

def __enter__(self) -> Self:
  if self.handler is None:
    self.handler = self.proto_instance.get_conn_handler()
  return self

def __exit__(self, exc_type, exc_val, exc_tb) -> None:
  if self._pool is not None:
    self._pool._release(self.handler)  # pyright: ignore[reportPrivateUsage]
  else:
    self.proto_instance.close_conn_handler()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestConnectionPooling -v`
Expected: PASS (all 3 new tests)

- [x] **Step 5: Run the full existing FTP/SFTP suite to check for regressions**

Run: `pytest tests/ftp/ -v`
Expected: PASS — the `__enter__`/`__exit__` change must not break `tests/ftp/test_adapter_ftp.py`, `test_adapter_sftp.py`, or `test_transfer.py`, which construct `AdaptedFTP`/`AdaptedSFTP` directly via the `make_ftp_adapter`/`make_sftp_adapter` fixtures (the `self._pool is None` fallback path).

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): pool and reuse connections across start_session() calls"
```

---

### Task 2: Connection-fatal classification on release

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`AdaptedFTP.__exit__`, `AdaptedSFTP.__exit__`, new module-level `_is_connection_fatal`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter._release(handler)` from Task 1, `FTPAdapter._current_size`/`_size_lock`.
- Produces: `FTPAdapter._discard(handler)` — closes a handler and decrements `_current_size` instead of returning it to `_idle`. Task 3 (validation) and Task 4 (ramp-up/backoff) both call this.

A session that raised a connection-fatal exception (dead socket, broken pipe, SSH failure) must not be returned to the pool — the next checkout would get a handler that's already broken.

- [x] **Step 1: Write the failing test**

```python
class TestConnectionFatalReleaseIsDiscarded:
  def test_connection_error_during_session_discards_the_handler(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with pytest.raises(ConnectionError):
      with adapter.start_session() as session:
        first_handler = session.handler
        raise ConnectionError("simulated dead socket")

    # A fatal exception must not return the handler to the pool -- the next
    # checkout should get a freshly opened one, not the poisoned one.
    with adapter.start_session() as second:
      assert second.handler is not first_handler
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]

  def test_non_fatal_exception_still_returns_handler_to_pool(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with pytest.raises(FileNotFoundError):
      with adapter.start_session() as session:
        first_handler = session.handler
        raise FileNotFoundError("no such remote file")

    with adapter.start_session() as second:
      assert second.handler is first_handler
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestConnectionFatalReleaseIsDiscarded -v`
Expected: FAIL — both tests currently see `second.handler is not first_handler`/`is first_handler` inverted, since Task 1's `__exit__` unconditionally releases regardless of the exception.

- [x] **Step 3: Implement the classification**

Add near the top of `src/aeth_ext/ftp/adapter.py` (module level, after imports):

```python
_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError, OSError)


def _is_connection_fatal(exc: BaseException | None) -> bool:
  if exc is None:
    return False
  if isinstance(exc, _CONNECTION_FATAL_TYPES):
    return True
  # Standard library imports
  from paramiko import SSHException

  return isinstance(exc, SSHException)
```

Add `_discard` to `FTPAdapter`:

```python
  def _discard(self, handler) -> None:
    with self._size_lock:
      self._current_size -= 1
    try:
      self.ftp_protocol().close_conn_handler.__func__(handler)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- best-effort close of an already-broken connection
      pass
```

`OSError` is broader than "connection fatal" (e.g. a local disk error would also match) — narrow this to what `AdapterProtocol`'s real call sites actually raise for network trouble, matching `_is_transient_transfer_error`'s shape from `scheduled-invoice-processor`. Since the discard path only needs "is the *connection* still good," not "is this exact error retryable," this coarser check is intentional and documented as such in the design (see the design doc's "Connection-fatal classification" section) — leave as-is.

Update `AdaptedFTP.__exit__`/`AdaptedSFTP.__exit__` from Task 1:

```python
def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
  if self._pool is not None:
    if _is_connection_fatal(exc_val):
      self._pool._discard(self.handler)  # pyright: ignore[reportPrivateUsage]
    else:
      self._pool._release(self.handler)  # pyright: ignore[reportPrivateUsage]
  else:
    self.proto_instance.close_conn_handler()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k "TestConnectionPooling or TestConnectionFatalReleaseIsDiscarded" -v`
Expected: PASS

- [x] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): discard connection-fatal sessions instead of pooling them"
```

---

### Task 3: Lazy validation on checkout

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.start_session`, new `FTPAdapter._validate`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter._discard(handler)` from Task 2.
- Produces: `FTPAdapter._validate(handler) -> bool`. Task 6 (keep-alive) reuses this directly.

A pooled connection may have gone stale (server-side idle timeout, network blip) between release and the next checkout. Validate before handing it out; discard and open fresh on failure.

- [x] **Step 1: Write the failing test**

```python
class TestLazyValidationOnCheckout:
  def test_stale_pooled_connection_is_discarded_and_replaced(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      stale_handler = first.handler

    # Simulate the server having dropped the connection while it sat idle.
    stale_handler.close()

    with adapter.start_session() as second:
      assert second.handler is not stale_handler
      # The replacement must be a live, working connection.
      second.handler.voidcmd("NOOP")

  def test_freshly_opened_connection_skips_validation(self, ftp_env: "_FTPTestEnv", monkeypatch: pytest.MonkeyPatch):
    """A connection that was just opened (not popped from the idle queue)
    must not pay the extra validation round trip -- it was already proven
    live by successfully completing its handshake."""
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)
    calls: list[object] = []
    original = FTPAdapter._validate
    monkeypatch.setattr(FTPAdapter, "_validate", lambda self, h: (calls.append(h), original(self, h))[1])

    with adapter.start_session():
      pass

    assert calls == []
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestLazyValidationOnCheckout -v`
Expected: FAIL — `AttributeError: FTPAdapter has no attribute '_validate'`, and the stale-connection test fails because `NOOP` on a closed socket raises instead of the pool having recovered transparently.

- [x] **Step 3: Implement validation**

Add to `FTPAdapter`:

```python
  def _validate(self, handler) -> bool:
    try:
      if self.protocol_handler is AdaptedFTP:
        handler.voidcmd("NOOP")
      else:
        handler.listdir(".")
      return True
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False
```

Update `start_session()`'s checkout-from-idle branch:

```python
  def start_session(self) -> HandlerType_T:
    container_cls = self._resolve_container_cls()

    handler = None
    try:
      candidate = self._idle.get_nowait()
    except Exception:  # noqa: BLE001 -- queue.Empty
      pass
    else:
      if self._validate(candidate):
        handler = candidate
      else:
        self._discard(candidate)

    if handler is None:
      with self._size_lock:
        if self._current_size < self.max_connections:
          self._current_size += 1
          handler = self._open_new()

    if handler is None:
      handler = self._idle.get()

    session = self.protocol_handler(self.ftp_protocol(), container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo)  # type: ignore
    session.handler = handler
    session._pool = self  # pyright: ignore[reportAttributeAccessIssue]
    return session  # pyright: ignore[reportReturnType]
```

Note `_discard` already decrements `_current_size` (Task 2), so the fall-through to "open new" correctly sees room to grow.

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestLazyValidationOnCheckout -v`
Expected: PASS

- [x] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): validate pooled connections before reuse"
```

---

### Task 4: Ramp-up / backoff — discovering the real ceiling

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.__init__`, `FTPAdapter.start_session`, `FTPAdapter._open_new`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter._current_size`/`_size_lock` from Task 1.
- Produces: `FTPAdapter._discovered_max: int | None`. Task 5 (recovery) reads and updates this.

If opening a new connection fails with a connection-refused-style error while under `max_connections`, that's a server-side limit being hit, not a transient failure — pin the effective ceiling there instead of retrying the same failure on every future checkout attempt to grow.

- [x] **Step 1: Write the failing test**

```python
class TestRampUpDiscoversRealCeiling:
  def test_refused_growth_pins_discovered_max(self, ftp_env: "_FTPTestEnv"):
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    open_count = 0

    def _limited_get(self):
      nonlocal open_count
      if open_count >= 2:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1
      return real_get(self)

    protocol_cls.get_conn_handler = _limited_get
    adapter = FTPAdapter(protocol_cls, max_connections=16)

    sessions = [adapter.start_session() for _ in range(2)]
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session()

    assert adapter._discovered_max == 2  # pyright: ignore[reportPrivateUsage]
    for s in sessions:
      s.__exit__(None, None, None)

  def test_subsequent_checkouts_respect_discovered_max_without_reattempting(self, ftp_env: "_FTPTestEnv"):
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    open_attempts = 0

    def _limited_get(self):
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

    assert open_attempts == 2
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestRampUpDiscoversRealCeiling -v`
Expected: FAIL — `AttributeError: FTPAdapter has no attribute '_discovered_max'`.

- [x] **Step 3: Implement ramp-up/backoff**

Add `_discovered_max` and `_discovered_max_last_probe` to the existing `__slots__` tuple (do not
replace the whole tuple — append these two names to whatever Task 1/2/3 already left in place):

```python
    "_discovered_max",
    "_discovered_max_last_probe",
```

Add to `__init__`, alongside the other instance state from Task 1:

```python
    self._discovered_max: int | None = None
    self._discovered_max_last_probe: float = 0.0
```

Update `start_session()`'s growth branch — this replaces only the "if handler is None: with
self._size_lock: ..." block from Task 3's version, the validated-idle-checkout branch above it is
unchanged:

```python
    if handler is None:
      with self._size_lock:
        effective_ceiling = self.max_connections if self._discovered_max is None else min(self.max_connections, self._discovered_max)
        if self._current_size < effective_ceiling:
          self._current_size += 1
          try:
            handler = self._open_new()
          except (ConnectionRefusedError, TimeoutError, OSError):
            self._current_size -= 1
            # Standard library imports
            from time import monotonic

            self._discovered_max = self._current_size
            self._discovered_max_last_probe = monotonic()
            raise
```

Note this re-raises on discovery (matching the test's `pytest.raises`) rather than silently falling through to blocking — a caller whose growth attempt was refused should see the failure immediately rather than hang, since a pool that's already at its *discovered* ceiling still has other callers' connections to wait on via the existing `handler = self._idle.get()` fallback on the *next* call. Verify this against the second test (`held.__exit__` must run before the third checkout, which it does).

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestRampUpDiscoversRealCeiling -v`
Expected: PASS

- [x] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): discover and pin the server's real connection ceiling on refusal"
```

---

### Task 5: Recovering a discovered ceiling

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.start_session`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter._discovered_max`/`_discovered_max_last_probe` from Task 4.
- Produces: `FTPAdapter._REPROBE_INTERVAL` (module-level constant, `300.0`).

A server's real limit can rise later (admin raises quota without an app restart). Re-probe past the discovered ceiling at most once per `_REPROBE_INTERVAL`, on an ordinary checkout rather than a background thread.

- [x] **Step 1: Write the failing test**

```python
class TestRecoveringADiscoveredCeiling:
  def test_reprobe_after_interval_raises_discovered_max_on_success(
    self, ftp_env: "_FTPTestEnv", monkeypatch: pytest.MonkeyPatch
  ):
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    allow_growth = False
    open_count = 0

    def _gated_get(self):
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
    # Standard library imports
    from time import monotonic

    monkeypatch.setattr(
      "aeth_ext.ftp.adapter.monotonic", lambda: monotonic() + FTPAdapter._REPROBE_INTERVAL + 1
    )

    with adapter.start_session() as second:
      assert second.handler is not None

    assert adapter._discovered_max is None or adapter._discovered_max >= 2  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

  def test_reprobe_within_interval_does_not_reattempt(self, ftp_env: "_FTPTestEnv"):
    protocol_cls = _TestFTPProtocolFactory(ftp_env)
    real_get = protocol_cls.get_conn_handler
    open_attempts = 0

    def _limited_get(self):
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
    assert open_attempts == 2

    held.__exit__(None, None, None)
    with adapter.start_session():
      pass

    # Still within _REPROBE_INTERVAL -- must come from _idle, no new open attempt.
    assert open_attempts == 2
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestRecoveringADiscoveredCeiling -v`
Expected: FAIL — `AttributeError: FTPAdapter has no attribute '_REPROBE_INTERVAL'`, and no reprobe logic exists yet.

- [x] **Step 3: Implement recovery**

Add near the top of `src/aeth_ext/ftp/adapter.py`:

```python
from time import monotonic
```

Add the class attribute (no `__slots__` change needed — `_REPROBE_INTERVAL` is a class attribute, not
an instance attribute, so it does not go in `__slots__`):

```python
class FTPAdapter[HandlerType_T: AdaptedFTP | AdaptedSFTP]:
  _REPROBE_INTERVAL: ClassVar[float] = 300.0
```

Update the growth branch in `start_session()` — this replaces the block Task 4 just added:

```python
    if handler is None:
      with self._size_lock:
        if self._discovered_max is None:
          effective_ceiling = self.max_connections
        elif monotonic() - self._discovered_max_last_probe >= self._REPROBE_INTERVAL:
          effective_ceiling = self.max_connections  # allow one probe past the discovered ceiling
        else:
          effective_ceiling = min(self.max_connections, self._discovered_max)

        if self._current_size < effective_ceiling:
          self._current_size += 1
          try:
            handler = self._open_new()
          except (ConnectionRefusedError, TimeoutError, OSError):
            self._current_size -= 1
            self._discovered_max = self._current_size
            self._discovered_max_last_probe = monotonic()
            raise
          else:
            if self._discovered_max is not None and self._current_size > self._discovered_max:
              self._discovered_max = self._current_size
```

Add `ClassVar` to the existing `typing` import at the top of the file if not already present.

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestRecoveringADiscoveredCeiling -v`
Expected: PASS

- [x] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): periodically re-probe a discovered connection ceiling"
```

---

### Task 6: Opt-in keep-alive

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.__init__`, new `FTPAdapter._keepalive_loop`, new `FTPAdapter._ensure_keepalive_started`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter._validate(handler)` from Task 3, `FTPAdapter._discard(handler)`/`_release(handler)` from Tasks 1-2.
- Produces: `FTPAdapter._keepalive_thread: Thread | None`. Task 8 (shutdown integration) stops this thread.

When `keepalive_interval` is set, periodically ping idle connections to prevent server-side idle timeouts. Off by default, and must never touch a connection that's currently checked out.

- [x] **Step 1: Write the failing test**

```python
class TestOptInKeepAlive:
  def test_disabled_by_default_spawns_no_thread(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env))

    with adapter.start_session():
      pass

    assert adapter._keepalive_thread is None  # pyright: ignore[reportPrivateUsage]

  def test_keepalive_pings_idle_connection_without_touching_checked_out_one(self, ftp_env: "_FTPTestEnv"):
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestOptInKeepAlive -v`
Expected: FAIL — `AttributeError: FTPAdapter has no attribute 'keepalive_interval'`.

- [x] **Step 3: Implement keep-alive**

Add `keepalive_interval` param and `_keepalive_thread`/`_keepalive_stop`/`_keepalive_interval` to the
existing `__slots__` tuple (append these three names to whatever Tasks 1-4 already left in place —
`_discovered_max_last_probe` does not need a further change here):

```python
    "_keepalive_interval",
    "_keepalive_stop",
    "_keepalive_thread",
```

Add the new constructor parameter and instance state:

```python
  def __init__(
    self,
    ftp_protocol: type[FTPProtocol | SFTPProtocol],
    container_cls: str | None = None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cvar: ContextVar[str] | None = None,
    max_connections: int = 16,
    keepalive_interval: float | None = None,
  ):
    self.container_cvar = container_cvar
    self.container_cls = container_cls
    self.ftp_protocol = ftp_protocol
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.max_connections = max_connections

    if issubclass(ftp_protocol, FTPProtocol):
      self.protocol_handler = AdaptedFTP
      self.ftp_protocol = ftp_protocol
    elif issubclass(ftp_protocol, SFTPProtocol):  # pyright: ignore[reportUnnecessaryIsInstance]
      self.protocol_handler = AdaptedSFTP
      self.ftp_protocol = ftp_protocol
    else:
      raise TypeError(f"Unsupported protocol type: {ftp_protocol}")  # pyright: ignore[reportUnreachable]

    # Standard library imports
    from queue import Queue
    from threading import Lock

    self._idle = Queue(maxsize=max_connections)
    self._current_size = 0
    self._size_lock = Lock()
    self._discovered_max: int | None = None
    self._discovered_max_last_probe: float = 0.0
    self._keepalive_interval = keepalive_interval
    self._keepalive_thread = None
    self._keepalive_stop = None

    super().__init__()
```

Add the loop and lazy-start helper:

```python
  def _keepalive_loop(self) -> None:
    # Standard library imports
    from queue import Empty

    while not self._keepalive_stop.wait(timeout=self._keepalive_interval):  # pyright: ignore[reportOptionalMemberAccess]
      try:
        handler = self._idle.get_nowait()
      except Empty:
        continue
      if self._validate(handler):
        self._idle.put(handler)
      else:
        self._discard(handler)

  def _ensure_keepalive_started(self) -> None:
    if self._keepalive_interval is None or self._keepalive_thread is not None:
      return
    # Standard library imports
    from threading import Event, Thread

    self._keepalive_stop = Event()
    self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
    self._keepalive_thread.start()
```

Call `self._ensure_keepalive_started()` at the end of `start_session()`, right before `return session`.

Note `_discard` in the keep-alive path bypasses `_size_lock`'s growth-ceiling bookkeeping check (it only decrements) — that's correct here: a keep-alive discard just shrinks the pool by one, exactly like any other discard, and the next checkout's growth logic naturally accounts for the smaller `_current_size`.

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestOptInKeepAlive -v`
Expected: PASS

- [x] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): add opt-in keep-alive for pooled idle connections"
```

---

### Task 7: `test_connection()` routes through the pool

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.test_connection`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `FTPAdapter.start_session()` from Tasks 1-6 (already pool-backed).
- Produces: none new — this task changes `test_connection`'s behavior, not its signature.

Currently `test_connection()` opens a session, tests it, and (via `__exit__`) already returns it to the pool as of Task 1 — so functionally this may already be correct. This task exists to add an explicit regression test proving `test_connection()` pre-warms the pool, since that behavior is easy to silently break in a future refactor of `start_session()`.

- [ ] **Step 1: Write the failing test**

```python
class TestConnectionPrewarmsPool:
  def test_test_connection_leaves_a_reusable_connection_pooled(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    assert adapter.test_connection() is True

    with adapter.start_session():
      pass

    # If test_connection()'s session had been closed instead of pooled, this
    # would open a second connection instead of reusing the first.
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]
```

- [ ] **Step 2: Run the test to verify it fails or passes**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestConnectionPrewarmsPool -v`
Expected: This may already PASS given Task 1's implementation (since `test_connection()` delegates to `start_session().test_connection(logit)`, and the returned session's `__exit__` already pools it via the normal `with`-less call path — check whether `test_connection()`'s internal call releases properly since it doesn't use a `with` block). If it fails, the fix is in Step 3; if it already passes, skip to Step 4 and note in the commit message that this is a regression-test-only change.

- [ ] **Step 3: Fix if needed**

`FTPAdapter.test_connection` currently is:

```python
  def test_connection(self, logit: bool = False) -> bool:
    return self.start_session().test_connection(logit)
```

This never calls `__exit__` on the session it opens, so the connection is never released back to `_idle` — it leaks as checked-out forever. Fix:

```python
  def test_connection(self, logit: bool = False) -> bool:
    session = self.start_session()
    try:
      return session.test_connection(logit)
    finally:
      session.__exit__(None, None, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestConnectionPrewarmsPool -v`
Expected: PASS

- [ ] **Step 5: Run the full FTP/SFTP suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "fix(ftp): release test_connection()'s session back to the pool"
```

---

### Task 8: Shutdown integration

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (`FTPAdapter.start_session`, new `FTPAdapter._shutdown_teardown`)
- Test: `tests/ftp/test_ftp_adapter_factory.py`

**Interfaces:**
- Consumes: `aeth_ext.errors.shutdown.register_for_shutdown(callback, *, phase, priority=0, required=False)`, `ShutdownPhase.THREADED` (`src/aeth_ext/errors/shutdown.py:371`). `FTPAdapter._idle`, `_keepalive_stop`, `_keepalive_thread` from prior tasks.
- Produces: `FTPAdapter._registered_for_shutdown: bool`. Nothing downstream consumes this — it's the terminal task for this plan.

Register once (lazily, on first connection open — matching the "register only once there's real state" convention `central_log_server`'s drainers use) to drain idle connections and stop the keep-alive thread at shutdown, without touching checked-out connections.

- [ ] **Step 1: Write the failing test**

```python
class TestShutdownIntegration:
  def test_registers_for_shutdown_on_first_connection(self, ftp_env: "_FTPTestEnv", monkeypatch: pytest.MonkeyPatch):
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

  def test_shutdown_teardown_closes_idle_connections_only(self, ftp_env: "_FTPTestEnv"):
    adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

    with adapter.start_session():
      pass  # released -- now idle
    checked_out = adapter.start_session()  # stays checked out

    adapter._shutdown_teardown()  # pyright: ignore[reportPrivateUsage]

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    # The checked-out connection must be untouched by teardown.
    checked_out.handler.voidcmd("NOOP")
    checked_out.__exit__(None, None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestShutdownIntegration -v`
Expected: FAIL — `AttributeError: FTPAdapter has no attribute '_shutdown_teardown'`, and no `register_for_shutdown` import exists in `adapter.py` yet.

- [ ] **Step 3: Implement shutdown integration**

Add the import near the top of `src/aeth_ext/ftp/adapter.py`:

```python
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
```

Add `_registered_for_shutdown` to `__slots__` and `__init__` (default `False`):

```python
    self._registered_for_shutdown = False
```

Add the teardown method and registration call:

```python
  def _shutdown_teardown(self) -> None:
    # Standard library imports
    from queue import Empty

    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      if self._keepalive_thread is not None:
        self._keepalive_thread.join(timeout=2.0)

    while True:
      try:
        handler = self._idle.get_nowait()
      except Empty:
        break
      try:
        self.ftp_protocol().close_conn_handler.__func__(handler)  # type: ignore[attr-defined]
      except Exception:  # noqa: BLE001 -- best-effort close during teardown
        pass

  def _ensure_registered_for_shutdown(self) -> None:
    if self._registered_for_shutdown:
      return
    self._registered_for_shutdown = True
    register_for_shutdown(self._shutdown_teardown, phase=ShutdownPhase.THREADED)
```

Call `self._ensure_registered_for_shutdown()` at the top of the growth branch in `start_session()` (i.e. right before or after the first `self._current_size += 1`), so registration happens exactly once, on first real connection open.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ftp/test_ftp_adapter_factory.py -k TestShutdownIntegration -v`
Expected: PASS

- [ ] **Step 5: Run the full FTP/SFTP suite and the full test suite**

Run: `pytest tests/ftp/ -v`
Expected: PASS

Run: `pytest -v`
Expected: PASS — full-repo regression check, since `register_for_shutdown` mutates module-global state in `aeth_ext.errors.shutdown` (`_registrations`) that other test modules may also touch.

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/ftp/adapter.py tests/ftp/test_ftp_adapter_factory.py
git commit -m "feat(ftp): register FTPAdapter's idle pool for shutdown teardown"
```

---

## Post-implementation

Once all 8 tasks are complete and committed, the design doc's "Testing" section is fully covered:
Task 1 covers the "Concurrency regression" bullet, Task 2 covers "Release classification," Task 3
covers "Lazy validation," Tasks 4-5 cover "Ramp-up" and "Recovery," Task 6 covers "Keep-alive," and
Task 8 covers "Shutdown." No spec requirement is left unimplemented.

Run the full suite once more (`pytest -v`) and confirm no regressions in `tests/ftp/` or elsewhere in
the repo before considering this sub-project done. `scheduled-invoice-processor` needs no changes to
benefit — it will automatically get pooled connections the next time it upgrades its `aeth-ext` pin
past whatever version this ships in.
