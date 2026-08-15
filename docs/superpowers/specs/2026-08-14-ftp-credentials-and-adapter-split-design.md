# FTP/SFTP Credentials & Adapter Split Design

## Problem

`FTPAdapter` currently takes a consumer-authored `type[FTPProtocol | SFTPProtocol]` class. The
consumer is responsible for writing a class that implements `get_transport()`/`request_handler()`/
`close_conn_handler()` and pre-bakes its own connection credentials. `FTPAdapter.__init__` then does
an `issubclass()` check to decide whether it's building an FTP or SFTP pool, and stores the winning
type on `self.protocol_handler`.

This has two problems:

1. **Boilerplate pushed onto every consumer.** Every consumer that wants to talk to an FTP/SFTP
   server has to hand-write a protocol-conforming class just to carry credentials.
2. **The FTP/SFTP identity isn't statically known.** `FTPAdapter[HandlerType_T: AdaptedFTP |
   AdaptedSFTP]` requires the consumer to manually parametrize the generic (`FTPAdapter[AdaptedSFTP](...)`)
   for `start_session()` to resolve to a concrete return type. Left unparametrized, it collapses to the
   `AdaptedFTP | AdaptedSFTP` bound, and "go to definition" on a method call resolves ambiguously
   across both classes instead of jumping to the real implementation. The `issubclass()` dispatch also
   means construction-time branching leaks into runtime (`self.protocol_handler is AdaptedSFTP` checks)
   instead of being resolved once, statically.

## Goals

- Consumers supply credentials, not a protocol-conforming class.
- The FTP-vs-SFTP identity of an adapter (and everything downstream of it — the session object, its
  methods) must be statically resolvable, so "go to definition" on a session method lands on the real
  `AdaptedFTP`/`AdaptedSFTP` method, never a shared protocol stub.
- The FTP-vs-SFTP determination happens exactly once, at adapter construction. No per-session-creation
  branching (`isinstance`/`issubclass` checks) anywhere in the hot path.

## Non-goals

- No backward-compatibility shim for the old `FTPProtocol`/`SFTPProtocol` consumer pattern — this is a
  breaking API change, accepted by the project owner. External consumers migrate to the new entry
  point.
- `container_cls`/`container_cvar` (the logging-label mechanism) is unchanged and out of scope.

## Design

### 1. Credentials dataclasses

Two new pydantic dataclasses in `aeth_ext/ftp/credentials.py`, both inheriting `IsPydantic` per project
convention. These fully replace consumer-authored protocol classes — `aeth_ext.ftp` owns all
connection-opening logic internally, driven by these fields.

```python
from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from pydantic.dataclasses import dataclass
from aeth_ext.types import IsPydantic

@dataclass(config=..., frozen=True)
class FTPCredentials(IsPydantic):
  host: str
  username: str
  password: SecretStr
  port: int = Field(default=21, gt=0, le=65535)
  use_tls: bool = False
  passive_mode: bool = True
  connect_timeout: float | None = None

@dataclass(config=..., frozen=True)
class SFTPCredentials(IsPydantic):
  host: str
  username: str
  port: int = Field(default=22, gt=0, le=65535)
  password: SecretStr | None = None
  private_key_path: Path | None = None
  private_key_passphrase: SecretStr | None = None
  host_key_policy: Literal["auto_add", "reject"] = "reject"
  known_hosts_path: Path | None = None
  connect_timeout: float | None = None

  @model_validator(mode="after")
  def _require_an_auth_method(self) -> Self:
    if self.password is None and self.private_key_path is None:
      raise ValueError("SFTPCredentials requires either password or private_key_path")
    return self
```

`frozen=True` because these are meant to be built once (typically as a module-level constant) and
handed around to `create_ftp_adapter`/one-off `HandleProvider`s — immutability keeps a shared constant
from being mutated out from under every adapter built from it. `password`/`private_key_passphrase` use
`SecretStr` rather than plain `str` so a credentials object doesn't leak its secrets into `repr()`,
logs, or an accidental exception message — consistent with this project's existing secret-handling
discipline (see the `.env` handling rule in `CLAUDE.md`). `known_hosts_path` is `None` by default,
falling back to the OS's `~/.ssh/known_hosts` (Section 2); set it explicitly for a deterministic trust
source that doesn't depend on whichever account the process happens to run as.

Consumers instantiate one of these (typically a module-level constant) and pass it to
`create_ftp_adapter`.

### 2. Private connectors

Two small, non-exported classes inside `adapter.py` replace what consumer-authored `FTPProtocol`/
`SFTPProtocol` implementations used to do. They are internal plumbing, not a public extension point.

```python
class _FTPConnector:
  def __init__(self, credentials: FTPCredentials) -> None:
    self._credentials = credentials

  def get_transport(self) -> None:
    return None  # no-op: FTP has no separate transport/channel tiers

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    conn = FTP_TLS() if self._credentials.use_tls else FTP()
    conn.connect(self._credentials.host, self._credentials.port, timeout=self._credentials.connect_timeout)
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    conn.set_pasv(self._credentials.passive_mode)
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    try:
      handle.quit()
    except OSError:
      handle.close()

class _SFTPConnector:
  def __init__(self, credentials: SFTPCredentials) -> None:
    self._credentials = credentials

  def get_transport(self) -> Transport:
    client = SSHClient()
    if self._credentials.known_hosts_path is not None:
      client.load_host_keys(str(self._credentials.known_hosts_path))
    else:
      client.load_system_host_keys()
    client.set_missing_host_key_policy(
      AutoAddPolicy() if self._credentials.host_key_policy == "auto_add" else RejectPolicy()
    )
    client.connect(
      self._credentials.host,
      port=self._credentials.port,
      username=self._credentials.username,
      password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
      key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
      passphrase=(
        self._credentials.private_key_passphrase.get_secret_value()
        if self._credentials.private_key_passphrase is not None
        else None
      ),
      timeout=self._credentials.connect_timeout,
    )
    transport = client.get_transport()
    assert transport is not None
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    return SFTPClient.from_transport(transport)

  def close_conn_handler(self, handle: Transport) -> None:
    handle.close()
```

`get_transport()` builds the connection through `SSHClient` rather than a bare `Transport()`, purely as
an internal implementation detail — `AutoAddPolicy`/`RejectPolicy` are `SSHClient`-only concepts, and a
raw `Transport.connect()` has no host-key-policy mechanism at all. Only `client.get_transport()`'s
result is returned; the `SSHClient` wrapper itself is never stored, since closing the extracted
`Transport` later (via `close_conn_handler`) closes the same underlying connection `SSHClient.close()`
would. `key_filename=` also lets paramiko auto-detect the private key's type (RSA/Ed25519/ECDSA) instead
of the connector having to guess which `paramiko.PKey` subclass to instantiate.

`close_conn_handler` on both connectors is what `release()`'s discard branch (Section 3) calls when
returning a handle that shouldn't go back into the pool -- for `FTPAdapter`, this is what gives discard
a graceful `.quit()` attempt instead of a bare `.close()`. For `SFTPAdapter`, per-channel discard never
calls this (closing one `SFTPClient` channel is just `channel.handle.close()`, no credentials/connector
involvement needed) -- `_SFTPConnector.close_conn_handler` is only ever invoked on whole `Transport`s,
from `_teardown_idle` (Section 3) and transport-death cascade cleanup (see the multiplexing design).

Note the FTP/SFTP asymmetry: for SFTP, `get_transport()` does the real work (dials the `Transport`,
which is the expensive, reusable-across-channels resource) and `request_handler(transport)` is cheap
(opens one more channel). For FTP there is no such two-tier structure, so the real work is placed in
`request_handler` instead (variadic signature, ignores whatever it's given) and `get_transport` is an
inert no-op. The shared base class's `_open_new_slot()` helper (Section 3) takes a `dial` callable from
the subclass and wraps *whatever that callable does* in one try/except — `FTPAdapter` passes a `dial`
that calls both connector methods (since that's where its real work lives), `SFTPAdapter` passes a
`dial` that calls only `get_transport()` (matching today's behavior, where only Transport-dialing is
ceiling-checked; opening a channel from an already-open Transport happens afterward, outside the
ceiling check). This is what makes `_open_new_slot()` fully protocol-agnostic despite the two
connectors putting their real work in different places.

### 3. Adapter class hierarchy

A generic abstract base, never exposed to consumers with an explicit type parameter — `FTPAdapter`/
`SFTPAdapter` pin `SessionT`/`HandleT` via inheritance, not via a caller-supplied generic argument.

```python
class _PooledAdapterBase[SessionT: AdapterProtocol, HandleT]:
  _REPROBE_INTERVAL: ClassVar[float] = 300.0

  def __init__(self, *, max_connections: int, chunk_size: int, pbar: Progress | None, tzinfo: ZoneInfo | None,
               container_cls: str | None, container_cvar: ContextVar[str] | None, keepalive_interval: float | None) -> None:
    ...  # protocol-agnostic fields + _current_size/_size_lock/_discovered_max/_keepalive_*/_registered_for_shutdown

  # --- concrete, shared ---
  def _effective_ceiling(self) -> int: ...

  def _open_new_slot[T](self, dial: Callable[[], T]) -> T | None:
    # The ONLY piece of connection-establishment that's genuinely identical between FTP and SFTP:
    # ceiling-checked size-lock bookkeeping around opening a brand new low-level connection (a new FTP
    # object, or a new Transport). Reserves a slot, calls `dial`, and on ConnectionRefusedError/
    # TimeoutError/OSError releases the slot and updates _discovered_max before re-raising; on success
    # updates _discovered_max if this probe exceeded it. Returns None if already at the effective
    # ceiling (dial is not called in that case).
    # NOTE: does NOT decide *whether* a new connection needs to be opened at all -- that's still
    # subclass-specific, because SFTP has a branch this can't represent (opening a new channel on an
    # *existing* under-cap Transport needs no new slot and no ceiling check). See acquire().

  def start_session(self) -> SessionT:
    # resolve container_cls from container_cvar/container_cls, call _build_session(container_cls).
    # No pool I/O happens here -- that's deferred to acquire(), called lazily by the session's own
    # __enter__ (self is passed to the session as its HandleProvider -- see Section 4).

  def _keepalive_loop(self) -> None: ...       # calls abstract _keepalive_check_one() each tick
  def _ensure_keepalive_started(self) -> None: ...
  def _shutdown_teardown(self) -> None: ...     # calls abstract _teardown_idle()
  def _ensure_registered_for_shutdown(self) -> None: ...

  # --- abstract, subclass-owned (no runtime type checks -- each subclass statically knows its own types) ---
  # acquire/release are public: FTPAdapter/SFTPAdapter structurally satisfy HandleProvider[HandleT]
  # (Section 4) through these two methods, so `self` can be passed directly as a session's provider.
  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]: raise NotImplementedError
  def release(self, handle: HandleT, is_fatal: bool) -> None: raise NotImplementedError
  def _instrumentation_callbacks(self, handle: HandleT) -> Sequence[Callable[[bytes], Any]]: raise NotImplementedError
  def _build_session(self, container_cls: str | None) -> SessionT: raise NotImplementedError
  def _validate(self, handle: HandleT) -> bool: raise NotImplementedError
  def _keepalive_check_one(self) -> None: raise NotImplementedError
  def _teardown_idle(self) -> None: raise NotImplementedError
```

`acquire` moves from concrete/shared back to abstract: the old spec draft assumed a uniform
`_checkout_idle() -> _grow() -> _block_until_available()` shape shared by both subclasses, but
`SFTPAdapter`'s real acquire sequence has a branch `FTPAdapter` has no equivalent of -- opening a new
channel on an *existing* under-cap `Transport` (no new connection slot, no ceiling check, since it
isn't a new connection). Forcing that through a shared, ceiling-checked `_grow()` would either drop the
branch or leak SFTP-specific logic into the "shared" base. Each subclass now owns its full acquire
sequence and calls the shared `_open_new_slot()` helper only for the sub-step that's genuinely
identical: reserving a ceiling-checked slot and dialing a brand-new low-level connection.

- `FTPAdapter.acquire`: `_checkout_idle()` (pop+validate from the flat queue), else
  `_open_new_slot(self._dial)` where `_dial` calls the connector, else block on the idle queue. Once a
  handle is in hand (whichever branch produced it), calls `_ensure_keepalive_started()` and returns
  `(handle, self._instrumentation_callbacks(handle))` -- always `(handle, ())` for `FTPAdapter`.
- `SFTPAdapter.acquire`: `_checkout_idle()` (pop+validate from `SFTPPool`), else
  `SFTPPool.pick_growth_target()` or -- if no target -- `_open_new_slot(self._connector.get_transport)`
  to open a new `Transport`, then always `self._connector.request_handler(transport)` to open the
  channel and register it with `SFTPPool`, else block via `SFTPPool.checkout_channel_blocking()`. Same
  final step as `FTPAdapter`: `_ensure_keepalive_started()`, then returns
  `(handle, self._instrumentation_callbacks(handle))`, which for `SFTPAdapter` looks up the handle's
  `TransportState` and returns `(self._make_instrument(state),)`.

`release` also stays fully subclass-owned rather than decomposed into shared release/discard
helpers, since `SFTPAdapter`'s version carries saturation-pop and transport-death-cascade logic (see
the multiplexing design) that doesn't cleanly factor out into protocol-agnostic pieces.

`_keepalive_check_one` reuses `release()` rather than reimplementing its own discard-or-requeue logic:
it pops one idle handle (bypassing the blocking/waiting semantics `_checkout_idle`/`acquire` have to
respect -- a keepalive tick should never contend with a real caller for a handle), then calls
`self.release(handle, is_fatal=not self._validate(handle))`. This keeps exactly one place per subclass
that knows how to route a handle back to idle vs. discard it, instead of two.

**`FTPAdapter(_PooledAdapterBase[AdaptedFTP, FTP])`**
- `__init__(self, credentials: FTPCredentials, *, max_connections=16, chunk_size=8192, pbar=None, tzinfo=..., container_cls=None, container_cvar=None, keepalive_interval=None)`
- Owns a `_FTPConnector` and the existing flat `Queue[FTP]` idle pool — pool mechanics unchanged from
  today's FTP path.
- `_instrumentation_callbacks` always returns `()`.
- `_keepalive_check_one`/`_teardown_idle` — same logic as today's `_keepalive_loop`/`_shutdown_teardown`,
  just no longer needing an `isinstance` check since this class only ever touches `FTP` handles.
- Because `acquire`/`release` are public and match `HandleProvider[FTP]`, `FTPAdapter` can be used
  directly as a connection pool by a consumer who only cares about one protocol and wants to skip the
  `AdaptedFTP`/`AdaptedSFTP` transparency layer entirely — they can call `adapter.acquire()`/
  `adapter.release(handle, is_fatal)` themselves instead of going through `start_session()`.

**`SFTPAdapter(_PooledAdapterBase[AdaptedSFTP, SFTPClient])`**
- `__init__(self, credentials: SFTPCredentials, *, ..., channels_per_transport: int = 4)` — this
  parameter no longer exists on `FTPAdapter` at all, since it's meaningless for plain FTP.
- Owns a `_SFTPConnector` and the existing `SFTPPool` (two-tier transport/channel pooling, saturation
  detection, cross-wave memory — unchanged from the multiplexing design).
- `_instrumentation_callbacks(handle)` looks up the `TransportState` via `self._sftp_pool.state_for_handle(handle)`
  and returns `(self._make_instrument(state),)`.
- **Fixes a latent gap**: today, `_keepalive_loop`/`_shutdown_teardown` operate only on the flat
  `_idle` queue, which the SFTP path never uses — meaning SFTP pooled channels currently get no
  periodic keepalive pings and no cleanup on shutdown at all. `SFTPAdapter._keepalive_check_one`
  validates one idle channel per tick (checkout/validate/release-or-discard, mirroring FTP's per-tick
  semantics); `_teardown_idle` closes every tracked `Transport` (via a new `SFTPPool.drain_transports()`
  method), which transitively closes their channels.

### 4. `HandleProvider` protocol, shared session base, and the acquire/release symmetry

Rather than injecting two separate callbacks, `AdaptedFTP`/`AdaptedSFTP` take a single provider object
bundling both. This is a deliberately narrow, public extension point — much narrower than the old
`FTPProtocol`/`SFTPProtocol` (which made consumers handle connect/login themselves): it says nothing
about *how* a handle is obtained, only that something can hand one out and take it back.

```python
# aeth_ext/ftp/types.py
class HandleProvider[HandleT](Protocol):
  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]: ...
  def release(self, handle: HandleT, is_fatal: bool) -> None: ...
```

`FTPAdapter`/`SFTPAdapter` structurally satisfy `HandleProvider[FTP]`/`HandleProvider[SFTPClient]`
through their own public `acquire`/`release` methods (Section 3) — no adapter needs to wrap itself in a
separate facade object.

A new shared generic base, `_AdaptedSessionBase[HandleT]`, holds everything that's identical between
`AdaptedFTP` and `AdaptedSFTP` today: `__slots__` (`handler`, `_provider`, `_callbacks`, `chunk_size`,
`container_cls`, `pbar`, `tzinfo`), `__init__`, `__enter__`, `__exit__`. Neither concrete class
redefines any of these — they only define the transfer-protocol methods (`upload_file`, `download_file`,
`transfer_file`/`_x_to_y` variants, `get_size`, `rename`, `remove`, `listdir`, `test_connection`,
`makedir`), so goto-definition on those still lands directly on the concrete implementation.

```python
class _AdaptedSessionBase[HandleT]:
  def __init__(self, provider: HandleProvider[HandleT], *, container_cls: str | None, pbar: Progress | None,
               tzinfo: ZoneInfo | None, chunk_size: int = 8192, callbacks: Sequence[Callable[[bytes], Any]] = ()) -> None:
    self.handler: HandleT | None = None
    self._provider = provider
    self._callbacks = tuple(callbacks)
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.chunk_size = chunk_size

  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler, callbacks = self._provider.acquire()
      self._callbacks = (*self._callbacks, *callbacks)
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self._provider.release(self.handler, _is_connection_fatal(exc_val))

class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterProtocol):
  ...  # upload_file/download_file/transfer_file/_ftp_to_ftp/_ftp_to_sftp/get_size/rename/remove/listdir/test_connection/makedir

class AdaptedSFTP(_AdaptedSessionBase[SFTPClient], AdapterProtocol):
  ...  # same set, SFTP-flavored
```

`provider` is always required — there's no optional fallback branch in `__exit__`, since every session
now always has something that knows how to release its handle.

**Standalone (one-shot, non-pooled) usage** becomes possible for the first time in this design: a
consumer who wants a single connection without any pooling writes their own minimal provider and
constructs the session directly, bypassing `FTPAdapter`/`create_ftp_adapter` entirely:

```python
class OneShotFTPProvider:
  def __init__(self, credentials: FTPCredentials) -> None:
    self._credentials = credentials

  def acquire(self) -> tuple[FTP, Sequence[Callable[[bytes], Any]]]:
    conn = FTP()
    conn.connect(self._credentials.host, self._credentials.port)
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    return conn, ()

  def release(self, handle: FTP, is_fatal: bool) -> None:
    try:
      handle.quit()
    except OSError:
      handle.close()

with AdaptedFTP(OneShotFTPProvider(MY_FTP_CREDS), container_cls="my_script", pbar=None, tzinfo=None) as ftp:
  ftp.download_file(...)
```

No pool I/O happens at `start_session()` time; the actual connection acquisition only happens once the
`with` block is entered, regardless of which provider is behind it.

### 5. Public entry point

```python
@overload
def create_ftp_adapter(credentials: FTPCredentials, *, max_connections: int = 16, chunk_size: int = 8192,
                        pbar: Progress | None = None, tzinfo: ZoneInfo | None = ..., container_cls: str | None = None,
                        container_cvar: ContextVar[str] | None = None, keepalive_interval: float | None = None) -> FTPAdapter: ...
@overload
def create_ftp_adapter(credentials: SFTPCredentials, *, max_connections: int = 16, chunk_size: int = 8192,
                        pbar: Progress | None = None, tzinfo: ZoneInfo | None = ..., container_cls: str | None = None,
                        container_cvar: ContextVar[str] | None = None, keepalive_interval: float | None = None,
                        channels_per_transport: int = 4) -> SFTPAdapter: ...
def create_ftp_adapter(credentials, **kwargs):
  if isinstance(credentials, FTPCredentials):
    return FTPAdapter(credentials, **kwargs)
  return SFTPAdapter(credentials, **kwargs)
```

Example consumer usage:

```python
MY_SFTP_CREDS = SFTPCredentials(host="ftp.example.com", username="svc", private_key_path=Path("/etc/keys/svc"))

adapter = create_ftp_adapter(MY_SFTP_CREDS, max_connections=8)  # adapter: SFTPAdapter

with adapter.start_session() as session:  # session: AdaptedSFTP -- goto-definition resolves directly
    session.download_file(...)
```

### 6. Changes to `types.py`

`FTPProtocolBase`, `FTPProtocol`, `SFTPProtocol`, `ProtocolEnum` are deleted — they were purely the
old consumer-facing connection-opening extension point being removed. `HandleProvider` (Section 4) is
added as the new, narrower public extension point. `AdapterProtocol`, `ListDirResult`, `BufferSize`,
`TransferSuccess` are unchanged.

## Testing

- `tests/ftp/conftest.py`'s `_TestFTPProtocol`/`_TestSFTPProtocol` test doubles and the protocol-class
  plumbing in `_FTPTestEnv`/`_SFTPTestEnv` are replaced with real `FTPCredentials`/`SFTPCredentials`
  instances pointed at the existing loopback test servers (host/port already known at fixture setup) —
  no mocking, consistent with the project's existing real-server test convention.
- `tests/ftp/test_ftp_adapter_factory.py` is substantially rewritten: `type[FTPProtocol]`-based
  construction becomes `create_ftp_adapter(credentials, ...)`; assertions keyed on `protocol_handler`/
  `ftp_protocol` are replaced with assertions against the concrete `FTPAdapter`/`SFTPAdapter` classes.
- `tests/ftp/test_transfer.py` mostly stays as-is (it already goes through `make_ftp_adapter`/
  `make_sftp_adapter` fixtures) — only the fixtures themselves change.
- `tests/ftp/test_sftp_pool.py` gains coverage for the new `SFTPPool.drain_transports()` method.
- New tests needed: `create_ftp_adapter` overload dispatch (FTP creds → `FTPAdapter`, SFTP creds →
  `SFTPAdapter`), `SFTPAdapter` keepalive/shutdown now actually touching the channel pool (closing the
  latent gap from Section 3), `FTPCredentials`/`SFTPCredentials` validation (`SFTPCredentials`
  rejecting neither-password-nor-key), and a standalone-provider test (a hand-written `HandleProvider`
  constructing `AdaptedFTP`/`AdaptedSFTP` directly, without `FTPAdapter`/`SFTPAdapter`, confirming the
  one-shot usage path from Section 4 actually works end-to-end against the loopback test servers).
