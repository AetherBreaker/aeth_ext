# SFTP Transport Multiplexing — Design

> **Amendment (2026-08-15):** the algorithm below (growth/shrink, saturation, cross-wave memory, error
> handling) is unchanged and still authoritative. Everything this document says about *which class* holds
> which state or method (`SFTPPool`, `_grow()`, `_pool_return()`, `_size_lock` as the in-flight guard) is
> superseded by `2026-08-15-sftp-channel-pool-separation-design.md`, which splits that single pool object
> into `SFTPChannelPool` (decisions) + `ChannelLedger` (shared state) + `SFTPAdapter` (transport
> dial/ceiling only). Read this document for *what* the pool does; read the separation design for *which
> object* does it.

## Motivation

`FTPAdapter`'s pool (`src/aeth_ext/ftp/adapter.py`) currently treats every pooled SFTP handle as its own
TCP connection: `_grow()` calls `self.ftp_protocol().get_conn_handler()`, which dials a fresh
`paramiko.Transport` and re-authenticates per handle. This has two costs a shared library shouldn't
impose on every downstream consumer: full SSH re-auth overhead per pooled connection, and consuming one
of the server's concurrent-connection slots per handle even when the workload doesn't need the bandwidth
of a dedicated TCP stream.

`paramiko.SFTPClient.from_transport(transport)` can mint additional multiplexed channels over an
already-authenticated `Transport` far more cheaply than opening a new one. The goal of this design is to
default to that multiplexing, while still growing additional `Transport`s when demand genuinely needs the
throughput a single TCP stream can't give — without requiring bandwidth-tuning knobs downstream consumers
would have to calibrate per deployment.

## Scope

SFTP only. Plain FTP (`ftplib`) has no transport/channel concept and keeps its current
one-handler-per-TCP-connection pooling *behavior* unchanged. API breaks are acceptable — `FTPAdapter`,
`SFTPProtocol`, and `FTPProtocol` are all allowed to change shape. `FTPProtocol`'s methods are renamed to
match `SFTPProtocol`'s for call-site parity (see below), not because FTP gains multiplexing.

## `FTPProtocolBase`/`FTPProtocol`/`SFTPProtocol` interface change

Replace the single opaque `get_conn_handler()` with two primitives, so the pool can hold onto a
`Transport` and mint further channels from it without knowing how the downstream consumer authenticates.
`FTPProtocol` moves to the same two-method shape as `SFTPProtocol` — not because plain FTP gains
multiplexing (it doesn't; see Scope), but because `FTPAdapter._grow()`/`start_session()` call
`self.ftp_protocol().get_conn_handler()` generically today, with no FTP-vs-SFTP branch. Keeping that
call site protocol-agnostic requires both concrete protocols to expose the same method names:

```python
class FTPProtocolBase(Protocol):
  KIND: ProtocolEnum

  @abstractmethod
  def get_transport(self) -> Any:
    """Dials and authenticates a new transport-level connection. Called once per pooled slot."""

  @abstractmethod
  def open_channel(self, transport: Any) -> Any:
    """Obtains a usable handler from an already-dialed transport."""

  @abstractmethod
  def close_conn_handler(self) -> None:
    raise NotImplementedError


class FTPProtocol(FTPProtocolBase):
  KIND = ProtocolEnum.FTP

  @abstractmethod
  def get_transport(self) -> FTP:
    """Dials and logs in a new ftplib.FTP connection — identical to the old get_conn_handler()."""

  @abstractmethod
  def open_channel(self, transport: FTP) -> FTP:
    """Identity passthrough: ftplib has no channel concept, so 'opening a channel' on an FTP
    connection is just returning the same connection. Keeps FTPAdapter's pool checkout path
    calling the same two methods regardless of which protocol it holds."""


class SFTPProtocol(FTPProtocolBase):
  KIND = ProtocolEnum.SFTP

  @abstractmethod
  def get_transport(self) -> Transport:
    """Dials and authenticates a new Transport. Called once per Transport, not per channel."""

  @abstractmethod
  def open_channel(self, transport: Transport) -> SFTPClient:
    """Opens an additional multiplexed channel on an already-authenticated Transport."""
```

This is a pure interface reshape on the FTP side: `FTPAdapter`'s FTP pooling behavior (one connection per
pooled slot, no multiplexing, existing `_grow()` probing) is unchanged — only the method names/split
change, so generic pool code doesn't need an `isinstance`/`KIND` branch to call it. `_TestFTPProtocol` and
`_TestSFTPProtocol` in `tests/ftp/conftest.py`, and any other downstream implementer of either protocol,
need updating to this shape as part of implementation.

## Pool architecture: two-tier, fixed-cap fallback

`FTPAdapter`'s SFTP pooling gains a second dimension on top of its existing `_idle` queue /
`_current_size` / `_effective_ceiling()` / `_discovered_max` machinery, which is otherwise reused as-is:

- **Transport tier**: bounded by `max_connections`, using the *existing* connection-refused probing
  (`_grow()`'s `ConnectionRefusedError, TimeoutError, OSError` handling) — this is what the server's real
  concurrent-connection limit actually constrains.
- **Channel tier**: each live `Transport` can hold up to `channels_per_transport` (new constructor param,
  default 4) concurrently-checked-out channels.

`_grow()`'s SFTP path becomes: prefer opening a new channel on an existing `Transport` that's under both
its channel cap and not flagged saturated (see below); only open a brand-new `Transport` when every
existing one is at cap or saturated, subject to the existing `max_connections`/probing ceiling; otherwise
fall back to the existing blocking `_idle.get()` wait.

A `Transport` with no throughput data yet (freshly opened, or before its first chunk has been measured)
is exempt from saturation checks and governed purely by `channels_per_transport` — the fixed cap is the
permanent floor/fallback this whole design sits on top of, not a discarded alternative.

## Chunk size

Replace the ~6 hardcoded `8192` literals across `upload_file`/`download_file`/`_ftp_to_sftp`/
`_ftp_to_ftp`/`_sftp_to_ftp` with a value threaded from a new `FTPAdapter(..., chunk_size: int = 8192)`
constructor param, passed down to `AdaptedFTP`/`AdaptedSFTP` at session construction.

## Instrumentation via existing callbacks

No new callback plumbing — extend what `upload_file`/`download_file`/`transfer_file` already accept:

- **Sink-style callbacks** (`download_file`'s `callback: Callable[[Buffer], Any]`, `transfer_file`'s
  observer `callback`) already receive each chunk's data. These become a list of callbacks, each invoked
  once per chunk, so the pool's instrumentation observer can coexist with a consumer-supplied callback.
- **`upload_file`'s callback is a pull source** (`Callable[[BufferSize], bytes]` — the adapter calls it to
  *obtain* each chunk), not a sink, so it cannot become a list of peers. Instead, after each successful
  pull (`buffer := callback(chunk_size)`), the adapter internally taps the returned bytes to the pool's
  instrumentation observer. This is not part of the public multi-callback API.

The instrumentation observer itself is bound into a session as a closure tied to that specific channel's
owning `Transport`, decided once at checkout (`start_session()`/`_grow()`). This preserves the existing
design priority that FTP-vs-SFTP path selection happens at adapter instantiation, not per call: the
transfer loop in `AdaptedSFTP` always just invokes whatever observer list it was constructed with — no
protocol branching inside the hot path.

## Growth/shrink algorithm: cross-sectional peer comparison

Saturation is judged by comparing a `Transport`'s current per-channel throughput against its *live peers*
at decision time, not against its own history:

- Each instrumentation report updates a per-`Transport` recent/EWMA per-channel throughput figure.
- At `_grow()` or release time, a `Transport` is "saturated" if its current per-channel throughput is
  meaningfully below the best currently-live `Transport`'s per-channel throughput (relative comparison,
  with hysteresis/a minimum-sample requirement before acting, to avoid flapping).
- Saturated `Transport`s are excluded from receiving new channels; new demand routes to a non-saturated
  `Transport` or triggers opening a new one.
- **Rebalancing**: when a channel on a saturated `Transport` is released back to the pool, it is closed
  instead of being returned to `_idle`, and that `Transport`'s channel count is decremented. No dedicated
  relocation code is needed — the next demand for a channel is routed by the normal `_grow()` logic, which
  already prefers non-saturated `Transport`s.
- Comparing against live peers (rather than a fixed historical baseline) sidesteps two problems a
  self-baseline approach would have: it needs no isolated "solo channel" measurement window (which a
  workload that only ever arrives in full-concurrency bursts might never produce), and it stays valid
  under real external network slowdowns, since those drag all `Transport`s down together and the relative
  comparison is unaffected.
- The degenerate case — a single `Transport`, under a cold-start burst, with no peer yet to compare
  against — falls back to the fixed-cap state described above until sustained demand forces a second
  `Transport` into existence.

## Cross-wave memory

To avoid every new batch of demand cold-starting back into the fixed-cap fallback state (needing a second
`Transport` to exist before saturation detection has anything to compare against):

- The adapter keeps a single scalar, `_last_wave_best_throughput: float | None` (bytes/sec per channel),
  not scoped to any one `Transport` — it stands in for "what this network path can do when unsaturated."
- During a wave, alongside the moment-to-moment peer comparison, a running max of every per-channel
  throughput reading is tracked. This running max — not the latest reading — is what gets persisted,
  since `Transport`s are typically winding down (lower throughput) by the time a wave ends.
- **Wave boundary**: detected when the count of checked-out (in-flight) channels drops to zero, i.e.
  every live channel is back in `_idle`. This requires an explicit in-flight counter (the pool currently
  only tracks total live count via `_current_size` plus the `_idle` queue's contents), decremented under
  the existing `_size_lock` on release so the exact zero-crossing is detected cleanly under concurrent
  releases. On that crossing: snapshot the running max into `_last_wave_best_throughput`, then reset the
  running max for the next wave.
- **Using it**: when a new wave's first channel has no live peer yet to compare against, it compares
  against `_last_wave_best_throughput` instead of falling back to the fixed-cap/ungoverned state — so
  saturation detection is available from the first channel of every wave except the very first the
  adapter ever serves.

## Error handling

- `_is_connection_fatal` continues to classify exceptions as today.
- A fatal error on a channel does not necessarily mean its `Transport` is dead — channels are
  independent. On a fatal error, check `transport.is_active()`; if the `Transport` is still active, only
  the one channel is discarded (existing behavior). If the `Transport` is no longer active, every *idle*
  channel currently attributed to it is discarded and its slot counts decremented.
- Channels from a dead `Transport` that are still checked out (in-flight in another thread) are **not**
  eagerly invalidated cross-thread. They fail naturally on their next I/O and go through the existing
  `_pool_return(is_fatal=True)` path when that thread releases them — consistent with keeping
  instrumentation/error handling out of the hot transfer path.

## Testing

- `tests/ftp/conftest.py`'s `_TestSFTPProtocol` updates to implement `get_transport`/`open_channel`
  instead of `get_conn_handler`.
- New coverage needed for: channel reuse on an existing `Transport` (no new TCP connection observed),
  `channels_per_transport` cap triggering a new `Transport`, cascading discard when a `Transport` dies
  with idle channels attached, pop-on-release when a `Transport` is judged saturated, and wave-boundary
  detection persisting/reusing `_last_wave_best_throughput` across a simulated idle gap.
- Existing FTP tests are unaffected — no interface or pooling behavior changes on that path.

## Out of scope

- Adaptive/measured growth was chosen over a purely fixed `channels_per_transport` cap; the fixed cap
  remains as the permanent fallback for Transports without enough data to judge, rather than being a
  separate mode.
- No proactive shrink-on-idle for `Transport`s themselves (matches the pool's existing philosophy — idle
  handles already aren't closed early today). Only the saturation-driven channel pop closes anything
  before shutdown teardown.
