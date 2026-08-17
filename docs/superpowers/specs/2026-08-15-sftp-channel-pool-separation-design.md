# SFTP Channel Pool Separation — Design

## Motivation

`SFTPPool` (`src/aeth_ext/ftp/sftp_pool.py`) currently mixes two things that don't belong in the same
object: the *decision* of when to reuse/grow/open-new/block, and thin single-use bookkeeping methods
(`register_transport`, `track_handle`, `mark_checked_out`, ...) that only make sense read alongside their
one call site in `SFTPAdapter.acquire()`/`release()` (`src/aeth_ext/ftp/adapter.py`). Understanding either
method requires the other file open at the same time — `register_transport`'s only caller sits four lines
deep in a branch of `acquire()` that also calls three other pool methods in a fixed order to keep
`channel_count`/`_handle_states`/`_in_flight` consistent, and one of those four lines mutated
`TransportState.channel_count` directly from `adapter.py`, outside `SFTPPool._lock` entirely — a latent
race this design also closes.

This document restructures that one class into three collaborators with non-overlapping jobs, changes
nothing about the multiplexing *algorithm* itself (growth/shrink, saturation, cross-wave memory, error
handling — see `2026-08-14-sftp-transport-multiplexing-design.md`, still authoritative for those), and
changes which object plays `AdaptedSFTP`'s `HandleProvider` (see
`2026-08-14-ftp-credentials-and-adapter-split-design.md` Section 4, superseded on this one point — see
that document's amendment note).

## Scope

`src/aeth_ext/ftp/sftp_pool.py`, `src/aeth_ext/ftp/adapter.py` (the `SFTPAdapter` class only —
`FTPAdapter`/`_PooledAdapterBase` are untouched), and `src/aeth_ext/ftp/types.py` (one new `Protocol`).
`AdaptedSFTP`, `AdaptedFTP`, `_AdaptedSessionBase`, and everything FTP-only are unaffected — `AdaptedSFTP`
stays exactly as transfer-only as it is today; only the identity of the object it's handed as its
`HandleProvider` changes, not what it does with it.

## Object model

Four objects replace today's two (`SFTPAdapter` + `SFTPPool`):

```text
AdaptedSFTP ──acquire/release──▶ SFTPChannelPool ──reads/writes──▶ ChannelLedger
                                        │                                │
                                        │ open a channel                │ holds references to
                                        ▼ (direct, own connector)        ▼ (stored, never called by the ledger)
                                  _SFTPConnector              SFTPAdapter (as TransportProvider) , SFTPChannelPool
```

- **`SFTPAdapter`** (existing class, adapter.py) — transport lifecycle only: dials new transports within
  the `max_connections` ceiling (`_open_new_slot`, unchanged), and owns construction/shutdown wiring. No
  longer implements `HandleProvider` itself and no longer holds a direct reference to the pool — it reaches
  it through the ledger.
- **`SFTPChannelPool`** (renamed from `SFTPPool`) — all decision-making: the full `acquire()`/`release()`
  procedure lives here, in one method each, in one file. It *is* `AdaptedSFTP`'s `HandleProvider` now. It
  opens channels directly via its own `_SFTPConnector` reference (a plain, standalone class with no
  reference back to anything — safe to hand out directly, no circularity risk) and asks for new transports
  through the ledger's stored `TransportProvider`.
- **`ChannelLedger`** (new) — the shared, thread-safe state: the transport-state registry, the
  handle→state map, the idle-channel list, and the wave/throughput scalars. Every collection on it behaves
  like its underlying type (`ledger.states[key] = value`, `ledger.idle.append(x)`, `x in ledger.idle`,
  `del ledger.handle_states[key]`) — no `register_x`/`track_y`-style method wrapping a one-line mutation,
  because a method name is less informative than the one line it wraps. It makes **no decisions** — it
  never reads its own data to decide anything, never calls out to the network, never calls `SFTPChannelPool`
  or `SFTPAdapter`. It also just holds two plain attributes, `transports` and `pool`, so
  `SFTPChannelPool`/`SFTPAdapter` can each reach the other without holding a direct reference to each other
  — see "Wiring" below.
- **`TransportProvider`** (new `Protocol`, `types.py`) — the narrow interface `SFTPAdapter` satisfies
  structurally, mirroring how `HandleProvider` already works (`ftp-credentials-and-adapter-split-design.md`
  Section 4): one method, `open_transport() -> Transport | None`.

## `ChannelLedger`

```python
# aeth_ext/ftp/sftp_pool.py

class LockedDict[K, V]:
  """A dict whose mutating/reading operations are individually atomic under one shared lock.

  Snapshotting methods (`values()`) copy under the lock and return a plain list/dict, so callers never
  iterate while holding the lock.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: dict[K, V] = {}
    self._lock = lock

  def __setitem__(self, key: K, value: V) -> None:
    with self._lock:
      self._data[key] = value

  def __getitem__(self, key: K) -> V:
    with self._lock:
      return self._data[key]

  def __delitem__(self, key: K) -> None:
    with self._lock:
      del self._data[key]

  def __contains__(self, key: K) -> bool:
    with self._lock:
      return key in self._data

  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  def get(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.get(key, default)

  def pop(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.pop(key, default)

  def values(self) -> list[V]:
    with self._lock:
      return list(self._data.values())

  def clear(self) -> list[V]:
    """Empties the dict, returning everything it held (for teardown)."""
    with self._lock:
      values = list(self._data.values())
      self._data.clear()
      return values


class LockedList[T]:
  """A list whose mutating/reading operations are individually atomic under one shared lock."""

  def __init__(self, lock: RLock) -> None:
    self._data: list[T] = []
    self._lock = lock

  def append(self, item: T) -> None:
    with self._lock:
      self._data.append(item)

  def pop(self) -> T | None:
    with self._lock:
      return self._data.pop() if self._data else None

  def remove(self, item: T) -> None:
    with self._lock:
      if item in self._data:
        self._data.remove(item)

  def __contains__(self, item: T) -> bool:
    with self._lock:
      return item in self._data

  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  def clear(self) -> list[T]:
    with self._lock:
      values = list(self._data)
      self._data.clear()
      return values


class ChannelLedger:
  """Shared, self-locking books for `SFTPChannelPool`. Holds data only -- every read/write here is a
  single, obvious operation; anything that has to touch more than one of these atomically (e.g. "is this
  transport saturated, and if not, put its channel back in `idle`") is `SFTPChannelPool`'s job, done with
  an explicit `with ledger.lock:` block, not a method on this class.
  """

  def __init__(self, transports: TransportProvider) -> None:
    self.lock = RLock()
    self.transports = transports
    self.pool: SFTPChannelPool | None = None  # filled in by SFTPAdapter once the pool exists -- see Wiring
    self.states: LockedDict[int, TransportState] = LockedDict(self.lock)
    self.handle_states: LockedDict[int, TransportState] = LockedDict(self.lock)
    self.idle: LockedList[Channel] = LockedList(self.lock)
    self.in_flight = 0
    self.wave_running_max = 0.0
    self.last_wave_best_throughput: float | None = None
```

`in_flight`/`wave_running_max`/`last_wave_best_throughput` stay plain `int`/`float` attributes rather than
a locked-counter wrapper: there's no natural dunder for "compare-and-maybe-replace a float," so they're
always mutated inside an explicit `with ledger.lock:` block in `SFTPChannelPool`, same as any other
multi-step update. `RLock` (not `Lock`) is required: `SFTPChannelPool` routinely does
`with ledger.lock: ledger.states[...] = ...; ledger.idle.append(...)`, and each `LockedDict`/`LockedList`
call inside that block re-enters the same lock — a plain `Lock` would deadlock on the second acquisition
from the same thread.

## `SFTPChannelPool`

```python
class SFTPChannelPool:
  def __init__(self, ledger: ChannelLedger, connector: _SFTPConnector, channels_per_transport: int) -> None:
    self._ledger = ledger
    self._connector = connector
    self.channels_per_transport = channels_per_transport
    self._wakeup: Queue[None] = Queue()

  def acquire(self) -> tuple[SFTPClient, Sequence[Callable[[bytes], Any]]]:
    channel = self._checkout_idle()
    if channel is not None and not self._validate(channel.handle):
      self._discard(channel)
      channel = None

    if channel is None:
      target = self._pick_growth_target()
      if target is None:
        transport = self._ledger.transports.open_transport()
        if transport is not None:
          target = TransportState(transport=transport)
          self._ledger.states[id(transport)] = target
      if target is not None:
        handle = self._connector.request_handler(target.transport)
        with self._ledger.lock:
          target.channel_count += 1
          self._ledger.handle_states[id(handle)] = target
          self._ledger.in_flight += 1
        channel = Channel(handle=handle, state=target)

    if channel is None:
      channel = self._checkout_blocking()

    return channel.handle, (self._make_instrument(channel.state),)

  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    state = self._ledger.handle_states.get(id(handle))
    assert state is not None, "handle must have been tracked at checkout"
    channel = Channel(handle=handle, state=state)

    if not is_fatal:
      if self._release_or_pop_saturated(channel):
        self._close_quietly(channel.handle)
      return

    if state.transport.is_active():
      self._discard(channel)
      return

    orphaned = self._drop_transport(state)
    for orphan in (channel, *orphaned):
      self._close_quietly(orphan.handle)
    self._ledger.transports  # (unused here -- see note below)
```

The `release()` sketch above is deliberately incomplete on the "a whole `Transport` died" branch — it needs
to tell `SFTPAdapter` its connection-count ceiling has one fewer live transport, but `SFTPChannelPool`
doesn't hold an adapter reference. `TransportProvider` gains a second method for exactly this:

```python
class TransportProvider[TransportT](Protocol):
  def open_transport(self) -> TransportT | None: ...
  def transport_dropped(self) -> None: ...
```

`SFTPAdapter.transport_dropped()` does what `release()`'s fatal-and-dead branch does today:
`with self._size_lock: self._current_size -= 1`. So the final line of that branch becomes
`self._ledger.transports.transport_dropped()`.

`_pick_growth_target`, `_checkout_idle`, `_checkout_blocking`, `_release_or_pop_saturated`, `_drop_transport`
carry over unchanged in *behavior* from today's `SFTPPool.pick_growth_target`/`checkout_channel`/
`checkout_channel_blocking`/`release_channel`/`discard_transport` — same saturation/EWMA logic from the
multiplexing design, just reading/writing through `ledger.states`/`ledger.idle`/`ledger.handle_states`
instead of the pool's own private dicts, and any step that reads-then-writes across more than one of them
wrapped in `with self._ledger.lock:` rather than being its own ledger method. `_validate` and
`_close_quietly` stay pure `SFTPClient` operations with no external dependency, same as today's
`SFTPAdapter._validate`.

`teardown()` (replaces `_teardown_idle` + `drain_transports`) and `keepalive_check_one()` (replaces
`_keepalive_check_one`) move onto `SFTPChannelPool` too, for the same reason `acquire`/`release` did — they
were already just "pop something from the ledger, then call the connector," split across two files:

```python
  def teardown(self) -> None:
    for state in self._ledger.states.clear():
      self._close_quietly_transport(state.transport)
    self._ledger.handle_states.clear()
    self._ledger.idle.clear()

  def keepalive_check_one(self) -> None:
    channel = self._checkout_idle()
    if channel is None:
      return
    self.release(channel.handle, is_fatal=not self._validate(channel.handle))
```

## Wiring

`SFTPAdapter.__init__` builds the three-object graph in a fixed order, since `ChannelLedger` needs the
adapter (as `TransportProvider`) before the pool exists, and the pool needs the ledger before it exists
itself:

```python
class SFTPAdapter(_PooledAdapterBase[AdaptedSFTP, SFTPClient]):
  def __init__(self, credentials: SFTPCredentials, *, ..., channels_per_transport: int = 4) -> None:
    super().__init__(...)
    self._connector = _SFTPConnector(credentials)
    self._ledger = ChannelLedger(transports=self)
    pool = SFTPChannelPool(self._ledger, self._connector, channels_per_transport)
    self._ledger.pool = pool

  def open_transport(self) -> Transport | None:
    return self._open_new_slot(self._connector.get_transport)

  def transport_dropped(self) -> None:
    with self._size_lock:
      self._current_size -= 1
```

Neither `SFTPChannelPool` nor `SFTPAdapter` stores a reference to the other — `SFTPChannelPool` only ever
holds `self._ledger`/`self._connector`; `SFTPAdapter` only ever holds `self._ledger`/`self._connector`.
Wherever the adapter used to call the pool directly, it now goes through `self._ledger.pool`:

```python
  def _build_session(self, container_cls: str | None) -> AdaptedSFTP:
    assert self._ledger.pool is not None
    return AdaptedSFTP(self._ledger.pool, container_cls=container_cls, ...)  # HandleProvider is the pool now

  def _keepalive_check_one(self) -> None:
    assert self._ledger.pool is not None
    self._ledger.pool.keepalive_check_one()

  def _teardown_idle(self) -> None:
    assert self._ledger.pool is not None
    self._ledger.pool.teardown()
```

`SFTPAdapter` no longer implements `acquire`/`release`/`_validate` at all — those move to
`SFTPChannelPool` (`_validate` becomes a private method there; `acquire`/`release` are its public,
`HandleProvider`-satisfying surface).

**Decision:** `_PooledAdapterBase`'s abstract surface narrows to drop `acquire`/`release`/`_validate` —
they stop being `@abstractmethod`s on the base class entirely, rather than `SFTPAdapter` carrying trivial
delegating stubs just to satisfy a contract nothing calls through it anymore. `FTPAdapter` keeps
implementing all three regardless (unaffected by this document — it's still its own `HandleProvider`,
still passed as `self` to `AdaptedFTP` in `FTPAdapter._build_session`), just as a concrete method rather
than an enforced override. `_PooledAdapterBase`'s required abstract surface becomes exactly
`_build_session`/`_keepalive_check_one`/`_teardown_idle` — both subclasses still implement all three,
`SFTPAdapter`'s versions now delegating to `self._ledger.pool` as shown above.

The comment at `_PooledAdapterBase`'s current abstract-methods boundary (adapter.py:1188-1191, explaining
that `acquire`/`release` are public because both subclasses structurally satisfy `HandleProvider` through
them) needs rewriting too: that's no longer true of `SFTPAdapter`, only `FTPAdapter`.

## Testing

- `tests/ftp/test_sftp_pool.py` splits along the same line as the production code: `ChannelLedger` tests
  exercise `LockedDict`/`LockedList` directly (no fakes needed — pure data structure behavior, plus a
  concurrency test that two threads mutating `ledger.idle`/`ledger.states` concurrently never corrupt
  state) and `SFTPChannelPool` tests exercise `acquire`/`release`/`teardown`/`keepalive_check_one` against
  a fake `TransportProvider` and a fake connector (same style of fakes `_FakeTransport`/`_FakeChannel`
  already use today).
- Every existing test in that file keyed on `SFTPPool.register_transport`/`.track_handle`/
  `.mark_checked_out` needs rewriting against the new shape — `register_transport` becomes a direct
  `ledger.states[id(transport)] = TransportState(transport=transport)` at the call site, `track_handle`/
  `mark_checked_out` are subsumed into `acquire()`'s single `with ledger.lock:` block and have no standalone
  equivalent to test directly; test the observable behavior (`state_for_handle`/`ledger.handle_states.get`)
  instead.
- `tests/ftp/test_adapter_sftp.py` needs a new case confirming `_build_session` hands `AdaptedSFTP` the
  pool (not the adapter) as its provider, and that `adapter.open_transport()`/`transport_dropped()` are
  actually called by the pool at the right moments (growth, and fatal-and-dead-transport release).
- No behavior change to assert beyond the new race-condition fix (`channel_count` now only ever mutated
  under `ledger.lock`) — the multiplexing algorithm's existing test coverage (channel reuse, cap-triggered
  new transport, saturation pop, wave-boundary memory) carries over unchanged in what it asserts, just
  against the new class names.

## Out of scope

- No change to the multiplexing algorithm itself — saturation detection, cross-wave memory, and error
  classification are exactly as specified in `2026-08-14-sftp-transport-multiplexing-design.md`.
- No change to `FTPAdapter`/`AdaptedFTP` — plain FTP has no transport/channel split and isn't touched by
  this document.
- No change to `credentials.py` or the public `create_ftp_adapter` entry point.
