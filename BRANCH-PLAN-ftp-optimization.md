# Branch plan — FTP/SFTP optimization work

How the remaining `TODO-ftp-optimization.md` work should be split into independently testable and
mergeable branches. Written at a pause point: the pre-warm design (`DESIGN-prewarm.md`) is specced,
items 1 and 6 are **not** yet specced, and `perf/ftp-optimization` currently holds only the already-
landed work (EOF-read fix, dial-time `TYPE I`, `channels_per_transport`, plus these docs).

## Blocking prerequisite — do this first, on `main`

**`TODO-get-size-exception-contract.md`** — `get_size` leaks protocol-specific exceptions
(`ftplib.error_perm: 550 Can't check for file existence`) to callers, aborting a live
`ScheduledInvoiceProcessor` pickup. This is a production bug on `main`, not a branch concern, and it
must land and release before any of the work below merges — `ScheduledInvoiceProcessor` depends on a
released `aeth_ext`, and this branch is nowhere near ready to be that release.

Note the traceback lands at `session.py:646`, inside `get_size` — one of the seven call sites the
dial-time `TYPE I` change touched on this branch. That change did not introduce the leak (the
exception contract was always this), but the two touch the same method, so whichever lands second
should re-check the other's assumptions.

## What is already on this branch

Landed and pushed: the EOF-read fix (item 5), dial-time `TYPE I` (item 2), `channels_per_transport`
accepted for either protocol and defaulted to 10 (item 4), and the two design docs. Items 5, 2 and 4
are retired from the TODO; item 3 was absorbed into item 7.

**This branch is not mergeable as-is** and should probably not grow further. Everything below is
better as fresh branches off an updated `main`.

## Proposed split

Ordered smallest-and-least-entangled first. Each is independently testable and independently
valuable; none depends on a later one.

### 1. Cheaper checkout probe (TODO item 1) — *not yet specced*

`SFTPChannelPool._validate` probes with `listdir(".")`; make it `stat(".")`. Measured ~4x cheaper on
both servers (RYO 165→38 ms, SAS 173→43 ms), and the login roots hold only 4–5 entries, so the cost is
`listdir`'s fixed multi-round-trip sequence rather than directory size.

Self-contained: one method, no new state, no lifecycle change. The rest of item 1 (skip the probe for
recently-released channels, skip it during a pre-warm hold) depends on later branches and should be
split out rather than bundled here.

Open question carried from the item: it claims a stale channel surfacing as a transfer error is
already handled by `ScheduledInvoiceProcessor`'s `_transfer_file_vend_to_main` retry loop. That is a
consumer-side claim in a plan doc and has not been verified against the actual retry code — check
before relying on it.

### 2. FTP per-handle state — pure refactor

`_idle` is a `Queue[FTP]` holding bare handles, so there is nowhere to record an idle timestamp or a
pin. Introduce a `slots=True` dataclass mirroring `TransportState` that holds the `FTP` object as an
attribute alongside `last_released` and a pin count, and queue that instead.

No behaviour change whatsoever — call sites gain one attribute access. Landing it alone keeps the
diff reviewable and makes branch 3's real changes legible.

### 3. Shared pruner + FTP idle reaping + TTL raise

The substantial one. Lift the per-pool prune loop into a single process-wide thread that both
protocols register against, make the ledger copy-on-write so that loop can iterate lock-free, then
give the FTP pool reaping through it, and raise `_EMPTY_TRANSPORT_TTL` 30 s → 60 s.

Do the lift *before* the FTP side, rather than mirroring the SFTP loop and merging two copies later.

**Highest-risk branch.** It touches `acquire`/`release`/`_teardown_idle`, whose existing comments
document real shutdown races found the hard way — a `release()` landing after `_teardown_idle()`'s
one-shot drain leaks a connection permanently, and `_size_lock` serialisation is what prevents it.
Adding a third party that mutates the idle collection means those invariants need re-checking, not
assuming. It also converts every in-place `states` mutation (`_discard`, `acquire`,
`_drop_transport`) to copy-swap under the writer lock.

`DESIGN-prewarm.md` carries the reasoning: why reads can go lock-free but writers still need a lock
(lost updates are real under CPython's switch interval, and `_current_size` drift breaches
`max_connections` against RYO's hard 19-Transport cap), why the decide→act gap needs no atomicity,
and why a handle found missing from the ledger at release time is closed rather than reinstated.

### 4. Keepalive knob/switch split

`keepalive_interval` currently means both "how often" and "whether" — the guard is literally
`if self._keepalive_interval is None: return`. Split into a `keepalive_interval: float | None` knob
(shared by passive keepalive and pre-warm sweeps) and a `keepalive: bool` switch, raising if the
switch is on with no interval.

API change, no new subsystem. No migration break: `keepalive` defaults to `False` and the interval
keeps its type, so every existing call site — including both `ScheduledInvoiceProcessor` adapters,
which pass `keepalive_interval=None` — behaves exactly as today.

### 5. Pre-warm (TODO item 7)

Holds, per-connection pin, sweeps, event/deadline release. Builds on branches 2–4. Fully specced in
`DESIGN-prewarm.md`; still open there are the deadline default and whether `prewarm` reports its
warmed count by return value or log line.

Worth restating the motivation, which is not primarily wall-clock: shrinking a wave enough that it
reliably completes inside a graceful-shutdown window would let `ScheduledInvoiceProcessor` stop
relying on idempotency to recover from a shutdown mid-wave.

### 6. SFTP request pipelining (TODO item 6) — *not yet specced, do last*

A thin request/response layer over paramiko's `Channel` speaking SFTP v3 directly, so N files can
have requests in flight at once rather than one-at-a-time per channel. Potentially the SFTP side from
~290 ms/file to ~100 ms/file, and a whole wave in ~3 round trips.

The TODO is explicit that this is not worth starting until items 1 and 7 have landed and the wave is
re-profiled — the cheaper items remove more time between them, and the profile afterwards may not
justify this at all. Leave it unspecced until then; specifying it now would be guessing at numbers
that are about to change.

## Sequencing summary

```
main:  get_size exception contract  ──►  release
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
              1. cheaper probe                    2. FTP per-handle state
                        │                                   │
                        │                                   ▼
                        │                     3. shared pruner + FTP reaping + TTL
                        │                                   │
                        │                                   ▼
                        │                     4. keepalive knob/switch
                        └─────────────────┬─────────────────┘
                                          ▼
                                   5. pre-warm (item 7)
                                          │
                                          ▼
                            re-profile, then decide on 6
```

Branch 1 is independent of 2–4 and can go in parallel. Branch 6 is deliberately gated behind a fresh
profile rather than scheduled.
