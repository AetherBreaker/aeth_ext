# Design notes — pre-warm windows (TODO-ftp-optimization item 7)

Working notes for the pre-warm feature on `perf/ftp-optimization`. Records what has been decided,
what was measured, and what is still open. Not a specification — the code and the live conversation
are authoritative where they disagree with this.

## Problem

`ScheduledInvoiceProcessor` runs 24/7 but transfers in short scheduled bursts (~1% of the time).
Between waves every pooled connection dies, so each wave pays a full cold start. Measured cold
connect-to-usable: **SFT holding FTP 3.0 s** (Pure-FTPd stalls ~2.5 s on `PASS`; 7 concurrent logins
= 3.8 s wall), **RYO vendor SFTP 0.72 s**, **SAS vendor SFTP 0.54 s**.

The consumer knows its schedule, so it can warm the pool shortly before a wave and release it after,
rather than holding server slots around the clock.

## Decided

- **A method on the adapter**, not a registry. An earlier sketch had adapters self-register under a
  name, with a lookup returning a factory that returns a prewarm callable. Dropped: the pool already
  holds the state needed to refcount overlapping holds locally, and the registry added a module-level
  mutable singleton, a naming scheme, and a deregistration lifecycle to solve that same problem
  remotely. APScheduler can call a bound method through a `partial` as easily as a lookup.
- **`prewarm()` blocks while dialing, then holds in the background.** It returns once connections are
  open; the keepalive worker holds them until the event or the deadline fires. It must not block for
  the hold's full duration — that would pin an APScheduler worker for the whole window.
- **The hold ends on either an event or a deadline.** The deadline always exists as a safety ceiling
  so a crashed job cannot leave connections warm indefinitely; the event is the early release.
- **Event contract**: a `Protocol` requiring `set` / `wait` / `is_set`. `clear` is probed with
  `getattr` and used when present.
  - `prewarm()` clears the event if it can, then **raises if `is_set()` is still true**. A spent
    one-shot (or a stale `set` from a previous wave) would otherwise start a hold that ends on its
    first tick — a pre-warm that silently does nothing, wave after wave. Raising makes that a loud
    error at the call site instead.
  - Supports `threading.Event` and `aiologic.REvent` (both have `clear`, reusable across waves), and
    `aiologic.Event` (no `clear`, single-use — reusing one raises).
- **Keepalive during a hold must be real protocol traffic**, sweeping **all** idle handles per tick
  (the current keepalive loop probes one per tick, so with N idle handles the effective per-handle
  cadence is N × interval). `stat(".")` or `realpath(".")` for SFTP, `NOOP` for FTP.

- **Split the interval from the on/off decision.** Today `keepalive_interval` is both: the guard in
  `_ensure_keepalive_started` is `if self._keepalive_interval is None: return`, so setting an
  interval is what turns keepalive on, and there is no way to express "no passive keepalive, but
  sweep every 45 s while pre-warmed". Replace with two parameters:
  - **`keepalive_interval: float | None`** — a knob. Seconds between sweeps, used by *both* the
    passive keepalive thread and the pre-warm hold's sweeper. No longer carries the on/off meaning.
  - **`keepalive: bool`** — a switch. Whether the passive keepalive thread runs at all outside a
    pre-warm hold. A hold starts sweeping regardless of this switch; it governs only the ambient
    behaviour.
  - **`keepalive=True` with `keepalive_interval=None` raises** at construction, next to the existing
    `max_connections`/`keepalive_interval <= 0` guards. Asking for keepalive without saying how often
    is a contradiction, and the alternative — silently substituting a default cadence — is exactly
    the courtesy problem this split exists to solve, since no default is right for both a 120 s and a
    >12 min server. Failing at construction means a consumer fixes it once, at the call site, rather
    than discovering months later that a pool was hammering (or never reaching) a vendor.

- **`prewarm()` takes its own `keepalive` / `keepalive_interval` overrides**, defaulting to the
  adapter's. So a consumer can leave ambient keepalive off and still sweep during holds — which is
  `ScheduledInvoiceProcessor`'s exact shape: bursty scheduled waves, nothing between them.

- **No interval configured means dial and leave.** With no keepalive resolved from either the call or
  the adapter, `prewarm()` opens the connections and does nothing further; they live until the server
  or `_EMPTY_TRANSPORT_TTL` reclaims them. This is not a degraded mode — pre-warm's contract is
  *connections ready at wave start*, and persistence is a separate concern. Against Bitvise, which
  left an untouched idle connection alive across three runs (12 min, 7.8 min, 16 min), sweeping would
  be pure noise. Raising here would force every consumer to configure a cadence for servers that
  demonstrably do not need one.

  This exists because the servers demand wildly different cadences and the courteous interval is a
  per-server fact the consumer knows and the library cannot: Files.com disconnects after **120 s** of
  no SFTP command, while Bitvise held an idle connection for **>12 min untouched** in two separate
  runs. One default cannot serve both — too short spams a server that never needed it, too long
  fails to hold the one that does. Consumers already tune `max_connections`/`channels_per_transport`
  per server for the same reason (see `ScheduledInvoiceProcessor`'s `PROBED-SERVER-LIMITS.md`).
- **Raise `_EMPTY_TRANSPORT_TTL` from 30 s to 60 s** (`sftp_channel_pool.py` line 281). Independent of
  pre-warm: a Transport costs 0.72 s (RYO) / 0.54 s (SAS) to redial, so reaping one after 30 s idle
  discards real work on a pool whose whole purpose is reuse. 60 s still bounds how long an unused
  Transport occupies a server slot — well inside RYO's 19-Transport ceiling — while surviving the
  ordinary gaps between operations within a single wave.

- **Pre-warmed Transports are exempted from the TTL prune individually, not by suspending it
  globally.** The reaper closes any Transport with no checked-out channel after its TTL, independent
  of keepalive, so a pre-warm would otherwise be undone within a minute of the dial — including in
  the dial-and-leave mode above, which means "no server-facing traffic", not "no protection from our
  own reaper".

  Exempt per Transport rather than suspending the reaper wholesale: a global suspension also spares
  Transports the hold never dialed, and Transports a concurrent unrelated caller opened, which is
  both wider than the hold's responsibility and wrong once the hold ends. A `pinned: bool = False`
  field on `TransportState` (a `slots=True` dataclass, so it sits naturally beside `channel_cap` and
  `last_released`) is read by the single filter in the prune loop; cool-down clears the flag, at
  which point the ordinary TTL applies and reclaims them on the normal schedule.

  This also keeps the reaper doing its job throughout a hold for everything the hold does not own,
  rather than pausing pool-wide lifecycle management for the whole window.

  Overlapping holds mean the flag cannot be a bare `bool`: the first hold to cool down would unpin a
  Transport a second hold still wants. A counter (or a set of owning hold ids) is needed, the same
  shape as the refcounting the warm count already requires.

  **An exempt connection must not cause a pruner thread to exist.** Deliberate goal, not an
  optimisation to add later: when everything alive is pinned, there is nothing for a pruner to do,
  and starting a thread to discover that repeatedly is pure cost — a wasted thread plus its wakeups
  for the entire hold, which is the common case for a pre-warmed pool sitting idle before a wave.
  Concretely: `_ensure_pruner_running()` inspects no state — it only checks whether a thread already
  exists — so the filtering belongs at its call sites and in the loop. `_discard` (and the other
  caller) must skip starting a pruner when the state it just emptied is pinned, and `_prune_loop`'s
  `idle_states` computation must exclude pinned states, so a running pruner retires through its
  existing "nothing left to wait on" path once the last *unpinned* Transport is reaped rather than
  spinning on a set it can never act on. Unpinning at cool-down is then what starts a pruner, and the
  exempt-only case costs nothing at all.

- **Give the FTP pool the same idle reaping, and the same pin.** Today it has none: `_idle` is a
  plain `Queue[FTP]` with no timestamps, no TTL and no background thread, and handles leave it only
  via `acquire()`, `_keepalive_check_one()` or `_teardown_idle()`. An idle FTP connection therefore
  lives until the server drops it — 15 min on the SFT Pure-FTPd host, per its banner.

  The original asymmetry has a cause rather than being an oversight: a `Transport` is a two-tier
  resource, and an *empty* one still occupies a server connection slot while serving nothing, which
  is what the reaper exists to release. An idle FTP handle is single-tier — it *is* the connection,
  immediately reusable by the next `acquire()`, with no equivalent "holding a slot for nothing"
  state. So the FTP pool was never wrong so much as it never learned to release idle capacity.

  It should, for the same courtesy reason driving the rest of this branch: a wave that dials 16
  connections and then holds all 16 open for 15 idle minutes is occupying slots for nothing, on a
  host with a global 50-user cap and **no per-IP limit** (48 concurrent logins accepted from one IP —
  a single client can starve every sibling program). Reaping idle FTP handles on the same TTL bounds
  that, and gives pre-warm's pin something to apply to on both sides.

  The FTP pool stores bare `FTP` handles in a `Queue`, so there is nowhere to record an idle
  timestamp or a pin. The fix is small: a `slots=True` dataclass mirroring `TransportState`, holding
  the `FTP` object as an attribute alongside `last_released` and the pin count, and queue *that*
  instead of the raw handle. Everywhere that currently takes a handle off `_idle` gains one attribute
  access; nothing else about `acquire`/`release`/`_teardown_idle` changes shape. The prune loop then
  mirrors the SFTP one.
- **Scope for the first pass**: `aeth_ext` only, with tests. The consumer-side scheduling in
  `ScheduledInvoiceProcessor` is a separate pass.

## Measured

| | RYO (Bitvise 9.66) | SAS (Files.com) | SFT holding (Pure-FTPd) |
| --- | --- | --- | --- |
| cold connect → usable | 0.72 s | 0.54 s | 3.0 s |
| round trip | 108 ms | 45 ms | 125 ms |
| max channels / Transport | 10 (11th refused) | ≥20, no refusal | n/a |
| max concurrent Transports | 19 (20th refused) | ≥24, no refusal | 50 global, no per-IP cap |
| idle survival, no keepalive | **>12 min** | **died ~2–2.5 min** | 15 min server-stated |
| idle survival, SSH keepalive 60 s | >12 min | **died ~2–2.5 min** | — |

`listdir(".")` costs ~4x `stat(".")` on both SFTP servers (RYO 165 ms vs 38 ms; SAS 173 ms vs 43 ms),
and both login roots hold only 4–5 entries — so the penalty is `listdir`'s fixed multi-round-trip
sequence, not directory size. This is item 1's saving.

**Probe primitive**: `stat(".")` and `realpath(".")` are interchangeable — both single round trips,
both return successfully on either server, and their costs are indistinguishable and rank in opposite
directions (RYO 29.5 vs 30.7 ms favouring `stat`; SAS 48.9 vs 44.9 ms favouring `realpath`). An
earlier guess that `realpath` should be cheaper — pure path canonicalisation, no metadata access —
is not borne out. Prefer `stat(".")` for both keepalive and item 1's checkout validation, so there is
one probe primitive rather than two.

**Bitvise has no idle timeout worth defeating.** Four separate runs (12 min, 7.8 min, 16 min, and a
full 20 min with both arms surviving and a final `stat` at 32 ms) left a no-keepalive control alive
throughout. A survival test cannot prove a keepalive works there, because the control survives too;
all that is provable is that the probe command executes correctly. Any courtesy interval configured
for RYO is therefore a precaution, not a measured requirement.

## The SSH-keepalive finding

`Transport.set_keepalive()` **did not keep a Files.com connection alive**. A keepalived Transport and
a control with no keepalive died at the same checkpoint, ~2–2.5 minutes in. `is_active()` correctly
reported `False` afterwards, and a real `stat` confirmed both dead (`OSError: Socket is closed`), so
passive detection is *accurate* — it just cannot *prevent* the drop.

Mechanism, from paramiko's source: `set_keepalive` sends
`global_request("keepalive@lag.net", wait=False)`. The `name@domain` form is RFC 4254 §4.9's
extension namespace — nothing is sent to lag.net; it is just the conventional identifier, also used
by OpenSSH. Note `global_request` serializes its `wait` argument into the packet's `want_reply`
field, so `wait=False` does not merely skip reading the reply — it tells the server not to send one.

**This is not the cause, and paramiko is not at fault.** Tested directly against Files.com with three
idle connections in parallel: paramiko's own `set_keepalive`, an explicit
`global_request("keepalive@lag.net", wait=True)` every 30 s, and a no-keepalive control. The
`wait=True` connection received **4 successful replies** (`SSH_MSG_REQUEST_SUCCESS` — Files.com
implements the extension rather than failing it), the last ~20 s before the drop. All three died
together at **2.3 minutes**.

So Files.com's idle timer counts **SFTP-subsystem activity only** and ignores transport-layer traffic
entirely, answered or not. That is an application-layer policy, not a protocol quirk, and there is no
SSH-level shortcut around it.

Consequences for this design: SFTP keepalive has to be an SFTP-level sweep — this is now confirmed
necessary, not merely the conservative option — and the interval has to sit under the shortest
server timeout.

- **One process-wide pruner, shared by every adapter of either protocol.** Today the pruner is
  per-`SFTPChannelPool`, so N SFTP adapters cost N threads; adding FTP reaping naively would add one
  per FTP adapter on top. A process running several adapters (`ScheduledInvoiceProcessor` has four:
  RYO, SAS, Coremark, and the shared SFT holding pool) pays a thread each for what is one periodic
  sweep over a uniform predicate.

  The criteria are already identical across both protocols once FTP has per-handle state: *is this
  connection idle*, *how long has it been idle* (`last_released`), *is it pinned*, and *is its TTL
  elapsed*. So the shared loop needs a small interface each pool registers itself against — something
  like "give me your currently-prunable entries" and "close this one" — and it can then reason over
  plain data without knowing whether it is looking at a `Transport` or an `FTP`.

  **Locking — make the ledger copy-on-write for reads, keep a small writer lock.** Mutating `states`
  in place is what forces the pruner to iterate under `_ledger.lock` today (unsynchronised iteration
  risks `dictionary changed size during iteration`, not merely a stale read). If writers instead build
  a new dict and swap the reference, any reader can take the current reference and iterate it
  lock-free; a stale snapshot is harmless here because the TTL decision tolerates staleness. That is
  what makes a shared pruner sweeping every registered pool cheap.

  Writers still need mutual exclusion among themselves. The GIL does not provide it: read-copy-swap
  spans multiple bytecodes with switch points between them (CPython releases the GIL every
  ~5 ms and at call boundaries), so two writers can copy from the same base and have the second swap
  silently discard the first — the same reason `i += 1` is not thread-safe. Free-threaded builds
  remove the GIL entirely. A lost update here is not cosmetic: prune removal and `acquire` insertion
  both touch `_current_size` accounting via `transport_dropped()`, so dropping one either leaks
  capacity or lets the pool exceed `max_connections`, which matters against a hard server cap (RYO
  refuses the 20th Transport). Keep a lock on the write path, which is cheap and rare — once per TTL
  over a handful of entries — while the constant read traffic goes lock-free.

  **The decide→act gap needs no atomicity.** An earlier version of this design treated "pruner selects
  a Transport that `acquire` then hands out" as requiring atomic check-and-remove. It does not: the
  pruner is a janitor, not a safety mechanism, and an old-but-idle Transport is perfectly fine to use.
  Handle it by reconciliation instead — on release, a handle that is no longer tracked in the ledger
  is **closed**, not reinstated. Reinstating would mean re-acquiring capacity that `transport_dropped()`
  already released and another caller may since have taken, so it needs exactly the contended
  bookkeeping this avoids, and racing it against a full pool would breach `max_connections`. Closing
  needs no accounting at all and can only ever cost a redial.

  Do that close **explicitly and idempotently**, not by dropping references and leaving it to garbage
  collection. `ftplib.FTP` has no `__del__` at all (see `ftp_connector.py`'s own note on this — it is
  why the connector closes the socket by hand on a failed dial), so an unreferenced handle leaks its
  socket until process exit; paramiko's `Transport` is a live thread holding a socket and is no better.
  Adding finalizers instead would mean doing network I/O — and, for SFTP, joining a thread — from
  `__del__`, which can run on any thread at an arbitrary moment including interpreter shutdown. GC
  timing is also unpredictable for exactly these objects (`TransportState` → `Transport` plus
  paramiko's own back-references are cycle-prone, deferring to a gc pass), which would trade "slot
  released at a known instant" for "eventually" — undermining the courtesy goal that motivates reaping
  at all against a server with a hard cap. An explicit close is observable, loggable and testable.

  Keep the reconciliation itself trivial: if the handle is not in the ledger at release time, close it
  unconditionally. No logic to distinguish "pruned while I held it" from "pool was torn down" —
  idempotent close absorbs the overlap (`ftplib`'s `close()` already is, per `close_conn_handler`).

  What also moves is the `_prune_thread`/`_prune_lock` ownership handshake, which becomes a single
  registry-level concern instead of one per pool.

  Registration must be weak (or explicitly unregistered on teardown) so a shared, process-lived
  thread cannot keep a closed adapter alive; `_teardown_idle`/`close()` is the natural unregistration
  point. The retire-when-idle behaviour generalises unchanged: the shared thread retires when *no*
  registered pool has an unpinned prunable entry, and any pool's next `_discard` restarts it.

## Sequencing

This design has outgrown one branch. Before writing code, finish speccing the remaining
`TODO-ftp-optimization.md` items (1, 3, 6) so the full shape is known, then split the work into
independently testable and mergeable branches rather than landing it as one change. A first cut at
the seams, smallest and least entangled first:

1. **Cheaper checkout probe** (item 1) — `listdir(".")` → `stat(".")`, ~125 ms/file on both SFTP
   servers. Self-contained, no new state, measurable on its own.
2. **FTP per-handle state** — the dataclass-in-the-queue change, no behaviour change. Pure
   refactor, and the precondition for anything else on the FTP side.
3. **Shared pruner + FTP idle reaping** — lift the per-pool prune loop into one process-wide thread
   both protocols register against, then give the FTP pool reaping through it, plus the TTL bump to
   60 s. Do the lift *before* the FTP side rather than mirroring the SFTP loop and merging two
   copies afterwards. Touches `acquire`/`release`/`_teardown_idle`, whose comments document real
   shutdown races and lock ordering, so it deserves its own review rather than riding along with a
   feature.
4. **Keepalive knob/switch split** — API change, no new subsystem.
5. **Pre-warm** (item 7) — holds, pinning, sweeps, deadline/event. Builds on all of the above.

Item 6 (SFTP request pipelining) stays last and separate; the TODO already notes it is not worth
starting until the cheaper items have landed and the wave is re-profiled.

## Open

- ~~Files.com's real idle timeout~~ — **settled: 120 s**, documented ("Files.com gracefully
  disconnects SFTP sessions after 120 seconds with no new commands or data transfer") and matching
  every control connection's death at 2.3 min on 15 s checkpoints.
- ~~Whether an SFTP-level sweep actually holds a Files.com connection~~ — **settled: yes.** Both
  `stat(".")` (45 s cadence, survived 7 min, 9 sweeps, final stat OK at 49 ms) and `realpath(".")`
  (45 s cadence, survived 8 min, 10 pings, final call OK at 46 ms) held a connection open while a
  no-keepalive control on the same server died at 2.3 min in both runs.
- **Whether SFTP pre-warm is worth building at all.** Pre-warm pays overwhelmingly on the FTP pool
  (3.0 s cold). RYO survives >12 min idle untouched, and SAS — the only server that aggressively
  drops connections — is also the cheapest to reconnect to at 0.54 s. If that holds, the SFTP half
  (TTL suspension, channel sweeps, parallel `_dial_lock` dialing) buys ~0.5 s on a server that
  reconnects in ~0.5 s, and the item shrinks to the FTP pool alone.
- Whether `prewarm` reports the warmed count by return value or by log line only (APScheduler
  discards return values).
- Exact interval and deadline defaults.

## Implementation constraints found in the code

- Keepalive is currently **all-or-nothing and start-only**: `_ensure_keepalive_started` returns early
  when `_keepalive_interval is None`, and nothing stops the thread except `_shutdown_teardown`, which
  is terminal. A hold needs keepalive that starts and stops on demand, on pools that today are built
  with `keepalive_interval=None` — which is how both `ScheduledInvoiceProcessor` adapters are
  constructed. This is a structural change, not a new call site, and it is what the knob/switch split
  above resolves.
- **Migration**: `keepalive_interval` keeps its `float | None` type, and `keepalive` defaults to
  `False`, so every existing call site stays valid and silent — including both
  `ScheduledInvoiceProcessor` adapters, which pass `keepalive_interval=None` today and get the same
  behaviour they have now (no passive keepalive). Only a consumer that opts in with `keepalive=True`
  has to supply an interval, and it is told so immediately.
- `PooledAdapterBase` defines `__slots__`, so any hold state needs explicit slots entries.
- `_EMPTY_TRANSPORT_TTL` is a `ClassVar` on `SFTPChannelPool`, reaped by a dedicated prune thread.
