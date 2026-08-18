# FTP Package Reorg Design

> **Temporary spec.** This doc exists to drive the implementation plan for this one task. Delete it
> once the reorg lands and passes review — it is not meant to be a lasting architecture record like
> the other docs in this directory.

## Goal

`src/aeth_ext/ftp/adapter.py` (1469 lines) mixes three distinct concerns — raw connection-opening,
per-session transfer logic, and pool bookkeeping — and `sftp_pool.py` can't reach `adapter.py`'s
`_SFTPConnector` for real without a cycle, so it duplicates its shape as a local `_ChannelConnector`
Protocol. That duplicate drifts silently if `_SFTPConnector`'s signature ever changes, and costs
goto-def/find-references. Split `ftp/` into single-responsibility modules with a strictly acyclic
import graph, and replace every internal-only Protocol duplicate with the real concrete type it stood
in for.

**Rule driving every Protocol decision below:** a Protocol is only justified where the concrete type
genuinely isn't known at that point (real, open polymorphism) or where the surface crosses into
consumer-supplied code. Anywhere the concrete type is always one specific in-package class, reference
that class directly, even if it takes restructuring to make the import legal.

## Target layout

```
ftp/
  __init__.py                 # public re-export surface (currently 0 lines -- an outlier vs. the
                               # rest of the codebase; mirrors aeth_ext/errors/__init__.py's pattern)
  errors.py                   # unchanged: ServerNotAvailableError
  types.py                    # HandleProvider, ListDirResult, aliases. TransportProvider deleted.
  credentials.py               # unchanged: FTPCredentials, SFTPCredentials
  connectors.py                 # NEW: _FTPConnector, _SFTPConnector (moved from adapter.py)
  session.py                     # NEW: _AdaptedSessionBase, _AdapterBase (renamed from
                                  #   AdapterProtocol, Protocol -> plain base), AdaptedFTP, AdaptedSFTP
  factory.py                       # NEW: create_ftp_adapter + its overloads
  pool/
    __init__.py                     # empty -- internal subpackage, no public surface of its own
    base.py                          # NEW: _PooledAdapterBase, _TransportDialer
    ftp_adapter.py                     # NEW: FTPAdapter
    sftp_adapter.py                      # NEW: SFTPAdapter
    sftp_channel_pool.py                   # renamed from ftp/sftp_pool.py: TransportState, Channel,
                                            #   _LockedDict/_LockedList (renamed, privatized),
                                            #   ChannelLedger, SFTPChannelPool
```

`adapter.py` and the old `sftp_pool.py` are deleted outright. No compatibility re-export shim --
this branch has no merged consumers yet, and the project convention (`.claude/CLAUDE.md`) is against
back-compat shims for removed/moved code.

## Dependency graph

Every edge below is a real (non-`TYPE_CHECKING`) import unless marked `[T]`. There are no cycles,
including in the type-only graph (verified against this project's `reportImportCycles = true`
pyright setting, which flags `TYPE_CHECKING`-only cycles too, not just runtime ones).

```
errors.py                (leaf)
types.py                 (leaf: HandleProvider, ListDirResult, aliases)
credentials.py            (leaf)
connectors.py               -> credentials.py
session.py                   -> types.py, aeth_ext.settings
pool/base.py                    -> session.py [T: _AdapterBase bound], aeth_ext.errors.shutdown
pool/sftp_channel_pool.py          -> connectors.py [T: _SFTPConnector], pool/base.py [T: _TransportDialer]
pool/ftp_adapter.py                  -> pool/base.py, connectors.py, session.py, credentials.py [T]
pool/sftp_adapter.py                   -> pool/base.py, connectors.py, pool/sftp_channel_pool.py,
                                          session.py, credentials.py [T]
factory.py                               -> credentials.py, pool/ftp_adapter.py, pool/sftp_adapter.py
__init__.py                                -> re-exports from all of the above
```

## Protocol audit

| Protocol today | Verdict | Rationale |
|---|---|---|
| `_ChannelConnector` (local to `sftp_pool.py`) | **Delete.** Use concrete `_SFTPConnector`. | Only ever one implementer. `SFTPChannelPool` never constructs one itself -- always receives it from outside -- so it can sit below `connectors.py` with zero cycle. |
| `TransportProvider` (`types.py`) | **Delete.** Use concrete `_TransportDialer`. | Only ever `SFTPAdapter`'s slot-bookkeeping, never consumer-supplied. See "Breaking the `ChannelLedger`/`SFTPAdapter` mutual reference" below for how the cycle this created is actually eliminated, not just suppressed. |
| `AdapterProtocol` (local to `adapter.py`) | **Convert to a plain base class, rename `_AdapterBase`.** | Already only ever `AdaptedFTP`/`AdaptedSFTP`; already nominally inherited (`class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterProtocol)`), never structurally duck-typed elsewhere; method bodies already `raise NotImplementedError` rather than Protocol's usual `...`. Converting preserves `@override` (a straight deletion would break it, since `@override` needs a real base method to point at). Not in `__all__` today either -- the rename to a leading underscore just makes that already-true fact honest. |
| `HandleProvider` (`types.py`) | **Keep as Protocol.** | The one genuine case the rule carves out: `AdaptedFTP`/`AdaptedSFTP` are explicitly designed (per the 2026-08-14 credentials/adapter-split design) to accept *any* conforming provider, including consumer-authored ones that bypass `FTPAdapter`/`SFTPAdapter`/`create_ftp_adapter` entirely. Real open polymorphism at the actual entrypoint surface. |

## Breaking the `ChannelLedger`/`SFTPAdapter` mutual reference

Naively typing `ChannelLedger.transports` as concrete `SFTPAdapter` creates a real cycle:
`sftp_adapter.py` imports `sftp_channel_pool.py` for real (it constructs `ChannelLedger`/
`SFTPChannelPool`), and `sftp_channel_pool.py` would need `SFTPAdapter`'s type back.

`ChannelLedger.transports` only ever calls two methods -- `open_transport()`/`transport_dropped()` --
and `SFTPAdapter`'s current implementations of both are pure `_PooledAdapterBase` slot bookkeeping
(`_open_new_slot`, `_size_lock`, `_current_size`), nothing SFTP-specific. Extract that pair into a new
concrete class, `_TransportDialer`, defined in `pool/base.py`.

**Verified with a standalone pyright probe:** `reportPrivateUsage` is scoped per-*class*, not
per-*module* -- a second class in the same file reaching into `_PooledAdapterBase`'s single-underscore
members still trips it ("protected and used outside of the class in which it is declared"). So
`_TransportDialer` must not hold a `_PooledAdapterBase` reference and reach into it itself. Instead,
`_PooledAdapterBase` gets a new concrete (non-abstract) method that builds the two closures *inside its
own method body* (same-class access, no violation at all) and hands back a `_TransportDialer` that is
just a plain holder of two callables -- it never needs to know `_PooledAdapterBase`, `SFTPAdapter`, or
`pool.sftp_channel_pool` exist:

```python
# pool/base.py

class _TransportDialer:
  """Dials new `Transport`s within a shared ceiling and records when one dies. Built via
  `_PooledAdapterBase._make_transport_dialer` and handed to `ChannelLedger` so the ledger depends on a
  real, narrow, navigable type instead of `SFTPAdapter` itself -- `SFTPAdapter` constructs
  `pool.sftp_channel_pool`'s classes for real, so typing the ledger's dependency back on `SFTPAdapter`
  would cycle; this doesn't, because `_TransportDialer` holds two plain callables and has no dependency
  on `SFTPAdapter` or `pool.sftp_channel_pool` at all.
  """

  __slots__ = ("open_transport", "transport_dropped")

  def __init__(self, *, open_transport: Callable[[], Transport | None], transport_dropped: Callable[[], None]) -> None:
    self.open_transport = open_transport
    self.transport_dropped = transport_dropped


class _PooledAdapterBase[SessionT: _AdapterBase, HandleT](ABC):
  ...

  def _make_transport_dialer(self, dial: Callable[[], Transport]) -> _TransportDialer:
    """Builds a `_TransportDialer` bound to this pool's own ceiling/size bookkeeping.

    Args:
      dial: Opens one new `Transport` (e.g. `_SFTPConnector.get_transport`).

    Returns:
      The dialer, for `SFTPAdapter` to hand to `ChannelLedger`.
    """

    def _open() -> Transport | None:
      return self._open_new_slot(dial)

    def _drop() -> None:
      with self._size_lock:
        self._current_size -= 1

    return _TransportDialer(open_transport=_open, transport_dropped=_drop)
```

`SFTPAdapter.__init__` becomes:

```python
self._connector = _SFTPConnector(credentials)
self._ledger = ChannelLedger(transports=self._make_transport_dialer(self._connector.get_transport))
pool = SFTPChannelPool(self._ledger, self._connector, channels_per_transport)
self._ledger.pool = pool
```

`SFTPAdapter.open_transport`/`transport_dropped` (today's Protocol-satisfying methods) are deleted
entirely -- `_TransportDialer` is now the sole owner of that responsibility, which also shrinks
`SFTPAdapter`'s own public surface to just what it needs. `pool/sftp_channel_pool.py` imports
`_TransportDialer` from `pool/base.py` (`TYPE_CHECKING` is enough -- it's never constructed there, only
received) -- a genuine leaf relationship, since `pool/base.py` has zero knowledge of
`sftp_channel_pool.py` in either direction. Zero `# pyright: ignore` suppressions anywhere in this
reorg.

`tests/ftp/test_adapter_sftp.py::TestPoolWiring::test_transport_provider_methods_delegate_to_the_adapters_slot_bookkeeping`
currently calls `adapter.open_transport()`/`adapter.transport_dropped()` directly -- rewrite it to
exercise `adapter._ledger.transports.open_transport()`/`.transport_dropped()` (or test
`_TransportDialer` directly against a bare `_PooledAdapterBase`-shaped fixture) instead.

## `LockedDict`/`LockedList`

Rename `_LockedDict`/`_LockedList` (leading underscore), drop both from `__all__`. They stay in
`pool/sftp_channel_pool.py` -- niche, single-consumer (`ChannelLedger`), not worth a public export.
`tests/ftp/test_sftp_pool.py` (renamed `test_sftp_channel_pool.py`) keeps its dedicated
`TestLockedDict`/`TestLockedList` concurrency-correctness tests, with
`# pyright: ignore[reportPrivateUsage]` added to the import line.

## Public API surface (`ftp/__init__.py`)

Re-exports, matching `aeth_ext/errors/__init__.py`'s existing pattern (import + explicit `__all__`,
no logic of its own):

```python
__all__ = [
  "AdaptedFTP", "AdaptedSFTP", "FTPAdapter", "SFTPAdapter", "create_ftp_adapter",
  "FTPCredentials", "SFTPCredentials", "HandleProvider", "ListDirResult", "ServerNotAvailableError",
]
```

`README.md`'s `from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP` becomes
`from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP`.

## Migration notes

- All `tests/ftp/*.py` import paths update mechanically to the new module locations.
- `tests/test_public_api_exports.py` gains an entry for `pool/sftp_channel_pool.py` (currently missing
  even for the pre-reorg `sftp_pool.py` -- pick this up as part of the move).
- The `docs/superpowers/plans|specs/2026-08-1{3,4,5}-*.md` files are historical record of how the
  current (pre-reorg) shape came to be -- leave them untouched, do not update them to reflect the
  new layout.
- No behavior change anywhere: this is a pure structural move plus the type-vs-Protocol swaps above.
  The multiplexing algorithm, pooling/ceiling logic, and every public method's observable behavior are
  unchanged.

## Constraints for the implementation plan

- Do not commit anything mid-plan; leave the working tree for review at the end (matches this
  session's existing uncommitted FTP fixes already sitting in the working tree from PR #14 review).
- Preserve the "avoid overeager extraction" convention (`.claude/CLAUDE.md`) for anything **not**
  called out explicitly above -- this spec's moves are justified by the cycle/Protocol problem, not a
  general invitation to split further.
- Run `uv run ruff check` / `uv run pyright` per-module as each is created, not just at the end --
  this reorg touches enough files that batching all verification to the end would make failures hard
  to localize.
- Full-suite `uv run pytest` once, at the end, per this project's testing-workflow convention for
  feature branches.
