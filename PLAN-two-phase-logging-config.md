# Two-Phase Logging Config (validate / apply)

**Branch:** `feat/two-phase-logging-config` (cut from `main`) — **single PR**

> **Status:** all architectural decisions below are **settled**. Each carries its rationale so it
> does not need re-deriving in a later session without this context.

---

## Context

### The problem

`LogRecordServer._handle_client` builds each connecting program's private logging hierarchy
**synchronously on the asyncio reader thread** ([reader_server.py:136](src/aeth_ext/central_log_server/server/reader_server.py#L136)).
That call chain (`build_hierarchy` → `dict_config` → `DictConfigurator.configure()`) runs its entire
body under `logging._lock` ([config/__init__.py:495](src/aeth_ext/logging/config/__init__.py#L495))
and does real disk work: the `logdir://` converter calls `path.parent.mkdir(...)`
([line 315](src/aeth_ext/logging/config/__init__.py#L315)) and `_instantiate_handler`
([line 869](src/aeth_ext/logging/config/__init__.py#L869)) opens files.

Consequences (TODO.md item 3):

1. Every handshake stalls the reader's event loop for the duration of the disk work.
2. While the reader holds the process-global `logging._lock`, the writer thread blocks on it —
   `_dispatch` calls `entry.manager.getLogger(record.name)` on nearly every record
   ([writer_thread.py:341](src/aeth_ext/central_log_server/server/writer_thread.py#L341)).

### The fix

Split config application into **validate** (pure, cheap, no I/O, no lock) and **apply** (I/O, lock).
The reader validates and acks; the writer thread applies. The long lock hold moves onto the writer,
which is single-threaded and processes queue items sequentially, so it cannot contend with itself.

This is also the opportunity to **collapse the layers between config assembly and
`DictConfigurator`** (D-A1), which currently obscure what actually happens during configuration
behind several stack frames.

### Investigation findings that shape the design

- **`HandshakeAck` is the only server→client message** today, and no client reads the socket after
  it (verified across all three client classes).
- **Validation cannot be total.** Filesystem errors only surface at apply time.
- **Import resolution is cheap.** `BaseConfigurator.resolve` ([line 249](src/aeth_ext/logging/config/__init__.py#L249))
  uses `__import__`, which hits `sys.modules` first. Every handler class in the packaged fragments is
  already imported by the time a handshake arrives — validation costs **no disk I/O** in the common
  case, so it runs **inline on the reader's loop** (no `asyncio.to_thread`).
- **`Converting*` wrappers mutate config in place on read** ([lines 148-157](src/aeth_ext/logging/config/__init__.py#L148)) —
  reading a value both fires its converter and permanently replaces the raw string. D-A2 turns this
  into the memoization mechanism rather than fighting it.
- **`LoggingConfigModel` is `extra="forbid"` at the top level**
  ([models.py:248-293](src/aeth_ext/logging/config/models.py)), so new config flags must be real model fields.
- **Only `logdir://` performs real I/O among the six converters.** `ext://` does a cached, idempotent
  import; `cfg://`, `setting://`, `runtime://`, `env://` are pure lookups. This is why D-A2 is small.

---

## Doctrine

**D-A5 — Tests never define intent.** Tests are perpetually mutable and exist to serve the
codebase's intent, not to define it. No guarantee is preserved *merely* because a test asserts it,
and no existing test is evidence of intended behaviour. Where this plan notes a test will change,
that is a heads-up for the implementer, never a constraint on the design.

---

## Settled decisions

### Configurator interface

**D-A1 — `DictConfigurator` becomes the unit of currency; the layers above it are removed.**

| Member | Behaviour |
|---|---|
| `__init__(config, ...)` | Stores the config dict. **No validation.** Cheap, so instances are created and passed around without paying validation cost up front. |
| `validate()` | Pydantic validation + converter resolution. Sets an internal "validated" flag. |
| `apply(private: bool)` | Applies the config. If the validated flag is unset, implicitly calls `validate()` first. |
| `configure()` | Alias for `apply()`. |

This **moves `LoggingConfigModel.model_validate` out of `__init__`** (currently
[line 477](src/aeth_ext/logging/config/__init__.py#L477)) into `validate()`.

**D-F3 — `validate()`/`apply()` are public API and replace the `dict_config()` function.**

**D-B4 / D-A1 chaff cut.** Callers construct a `DictConfigurator` directly and call `.apply()`:

- `BaseLoggingConfig._apply_config` ([setup.py:276](src/aeth_ext/logging/setup.py#L276)) is deleted;
  its four call sites ([369](src/aeth_ext/logging/setup.py#L369),
  [399](src/aeth_ext/logging/setup.py#L399), [437](src/aeth_ext/logging/setup.py#L437),
  [645](src/aeth_ext/logging/setup.py#L645)) become `_build_config(...)` → `DictConfigurator(...)` → `.apply()`.
- `build_hierarchy` ([dispatch.py:76](src/aeth_ext/central_log_server/server/dispatch.py#L76)) is
  deleted. Its three callers ([writer_thread.py:135](src/aeth_ext/central_log_server/server/writer_thread.py#L135),
  [client/__init__.py:498 and :815](src/aeth_ext/central_log_server/client/__init__.py#L498))
  construct a configurator directly.
- **D-B5** — no `prepare_hierarchy()` helper. The reader constructs the configurator from
  `handshake.config`, calls `.validate()`, sends the ack, and puts the configurator into
  `RegisterClient`.

`_build_config` and the `loader.py` fragment-assembly helpers are **kept** — they are the legitimate
"assemble fragments into one dict" layer.

**D-F4 — `json_config` / `toml_config` / `yaml_config` / `_process_socket_config_chunk` are
rewritten onto the new interface**, not deleted.

**D-B1 — the `DictConfigurator` instance itself is what gets passed around** (into `RegisterClient`
and down the queue). No separate prepared/spec wrapper type. **D-B3** — naming N/A as a result.

Ownership contract to document on the class: **the reader is fire-and-forget.** Once a configurator
passes validation and goes down the pipeline, the reader never touches it again — all responsibility
transfers to the writer thread. The object is single-use and self-mutating (`apply()` rewrites
`self.config` in place), so it can never be re-applied or retried; a fallback is always a *fresh*
instance.

**D-B2 — `RootLogger`/`Manager` for a private hierarchy are constructed inside `apply()`.**
`apply()` takes a `private: bool` parameter: when true it returns `(manager, root)`, when false it
returns `None`. Typing handled with `@overload`.

### Validation behaviour

**D-A2 — converters are not split into two paths.** Since only `logdir://` did I/O and D-A3 removes
that, the split is:

- **`validate()` resolves everything** — all converters run, plus structural checks. The
  `Converting*` write-back then acts as memoization, so nothing is resolved twice.
- **`apply()` does the one-pass mkdir, instantiates handlers, and wires the hierarchy.** No
  re-conversion.

**`cfg://` is the one exception**: at validate time it only checks that the referenced name matches a
definition elsewhere in the config. Actual creation and binding happen during `apply()`, because the
target only becomes a live object then.

**D-A3 — `mkdir` leaves the converters.** `logdir://` resolves paths only. Directories needing
creation accumulate on the configurator, and a private method creates them in **one pass** during
`apply()`. Path resolution itself is part of validation.

**D-A4 — validation checks handler kwargs, bounded by cost.** Acceptable only while no more
expensive than what already happens at apply time. If signature binding proves costlier, fall back to
name binding plus simple `isinstance` checks on easily duck-typed values.

**D-A7 — partial-apply cleanup is made deterministic.** Today a mid-apply failure orphans already
instantiated handlers holding open file descriptors; for private hierarchies they are only reclaimed
whenever CPython's refcounting happens to collect the configurator, and repeated
disconnect/reconnect of a bad config leans on that. The configurator tracks every handler it
instantiates, and `apply()` wraps its body so a failure flushes, closes, and drops them before
propagating.

### Writer-thread apply

**D-C2 — apply runs in `asyncio.to_thread`, awaited inline. No race exists.** `_record_loop` does
`await self._process(item)` ([writer_thread.py:219](src/aeth_ext/central_log_server/server/writer_thread.py#L219))
strictly sequentially, so `_process` does not return until apply completes and the first record is
not even dequeued yet. `to_thread` frees the writer's *loop* (keeping the web-viewer push server
responsive) without reordering the *queue*. Invariant: **await inline, never fire-and-forget.**

**D-C3 — out of scope:** `_unregister_client` / `shutdown_hierarchy` / `_emit_connection_separator`
sync I/O on the writer's loop stays as-is (TODO item 4).

**D-C4 — out of scope:** concurrent/per-program registration. Registration stays strictly sequential.

### Fallback

**D-D1 — validation failure and apply failure are different things, deliberately.**
Validation failure = the client's config is structurally broken = **the client's fault** → connection
denial ack, exactly as today, no fallback. Apply failure = the config was well-formed but the
environment refused it (EACCES, disk full) = **the server's problem** → fall back so records are not
lost. The asymmetry is intentional.

**D-D2 — the fallback mirrors the failed config's rotation style, read from an explicit field.**
A new **top-level config field** carries daily-vs-per-run (sniffing handler class names was rejected
as fragile — it breaks the moment a consumer customizes handlers). Its absence means the config is
pervasively malformed, and the connection is rejected as if the client had opted out of fallbacks.

**D-D3 — fallback files are named `{program_name}_fallback.log` and
`{program_name}_fallback_debug.log`** so a degraded program is obvious on disk.

**D-D4 — if the fallback itself fails to apply**, the connection is rejected via the D-E2 failure
event.

**D-D5 → TODO.md.** Surfacing config degradation in the web viewer's `StatsData`/snapshot is a good
idea but out of scope.

### Opt-out and client notification

**D-E1 — the opt-out flag lives in the config itself** (a real field on `LoggingConfigModel`, since
the model forbids extras). Consumers override it in TOML with no new machinery, and it reaches the
server over the wire for free — a handshake-level field would need plumbing to get it into the
socket handler.

**D-E2 — the ack is never delayed; failure is reported out-of-band afterwards.**

1. `RegisterClient` carries a tri-state event (`unset` / `success` / `failure`). The reader sends the
   `HandshakeAck` immediately once validation passes — it never waits on the writer.
2. The writer applies the config and sets the event to `success` or `failure`.
3. The reader's per-connection loop watches that event via
   `asyncio.wait([read, event], return_when=FIRST_COMPLETED)`, so a quiet client is served as
   promptly as a chatty one. Either outcome puts exactly one message on the wire — a success
   notification, or an explicit connection rejection — after which the reader stops watching and
   lets the event be collected.

**D-E2a — the server signals success as well as failure.** Without it a client cannot distinguish
"applied fine, nothing is coming" from "still applying, a rejection may yet arrive", which would
force every client watcher to stay alive for the whole connection. Signalling both means the watcher
always terminates on the first inbound message, which is what keeps D-E5's fallback thread
short-lived. It also gives the client positive confirmation that its config is live.

**D-E3 — the tri-state object is a small wrapper around `aiologic.Event` plus an outcome field** —
settable from the writer thread, awaitable from the reader's loop. No new primitive is invented.

**D-E4 — N/A.** An unresponsive writer means something is badly wrong process-wide; not this code
path's responsibility. Moot anyway now the ack does not wait on the writer.

**D-E5 — the apply-result messages are new types, distinct from `HandshakeAck`**, because the ack is
sent as soon as validation succeeds and must not be held for the writer. Requires a framing
discriminator, since clients currently assume the first inbound packet is an ack.
**Old-client compatibility is explicitly not a concern** — all consumers update once this is tested.

Client-side receive paths, each terminating on the first inbound message (D-E2a):

- `AsyncioQueueDrainer` — a task awaiting `reader.read()` alongside `_drain_until_broken`, cancelled
  on reconnect/close.
- `ThreadedQueueDrainer` — `select.select([sock], [], [], 0)` inside the existing drain loop.
- `HandshakeSocketHandler` — **always a short-lived thread; no event-loop detection.** Loop detection
  was rejected because `asyncio.get_running_loop()` only succeeds when called *from* the loop thread,
  while `emit()` runs on whatever thread called `logger.info(...)` — commonly a worker. Detection
  would therefore be non-deterministic, with the chosen path depending on which thread happened to
  emit the record that triggered the reconnect. Because the server always replies (D-E2a), the thread
  exits within milliseconds, so a uniform thread costs almost nothing and removes the heuristic
  entirely.

**D-E6 — client-side `FATAL_EVENT` on rejection is confirmed**, including that this shuts down the
host application. A handler is added to [err_handling.py](src/aeth_ext/errors/err_handling.py) that
owns the setting of `FATAL_EVENT` so it is always paired with an alert email and Pushover
notification — the operator is notified immediately and responds within minutes, which is what makes
the aggressive shutdown acceptable.

**D-E7 — failing to receive the ack raises an alert but does not shut down.** Today `_read_ack`
returns `None` silently on timeout/malformed/dead-connection, so resume-by-id is skipped with nobody
noticing. That silent degradation becomes an alert.

### Graceful shutdown

> **SUPERSEDED — D-G1 through D-H7 (everything from here to the "Process" heading) has been replaced
> by [PLAN-graceful-shutdown.md](PLAN-graceful-shutdown.md) (the D-I series).** Reviewing this section
> against the codebase found it unimplementable as written: nothing ever terminated the process,
> `SHUTDOWN_EVENT` had zero readers while every real consumer watched `FATAL_EVENT`, D-H3's ordering
> tore down the logging transport before its own dependents, D-H1's arm callback re-entered
> `_flush_to_disk` in signal context, and D-H7 mistook `EmergencyHistoryWriter`'s queue for "no
> buffering". Retained below for the reasoning history only — **do not implement from it.** Steps 1-8
> of the implementation order (D-A through D-F) are unaffected and remain accurate.

**D-G1 — a process-wide `SHUTDOWN_EVENT` is added, mirroring `FATAL_EVENT`.** Config-failure paths
(D-E6/D-D4) need a way to signal the whole program to wind down, not just the logging subsystem, so
the primitive belongs next to `FATAL_EVENT` in [err_handling.py](src/aeth_ext/errors/err_handling.py)
rather than inside the logging package. Same `aiologic.Event` type, same reasoning as D-E3: no new
primitive invented. Unlike `FATAL_EVENT` it carries no outcome/alerting semantics of its own — it is a
plain "start winding down" signal that consumers subscribe to; setting `FATAL_EVENT` and setting
`SHUTDOWN_EVENT` remain independent actions; the D-E6 handler sets both.

**D-G2 — the OS signal handlers are registered inside `initialize()`**, not left to each entrypoint to
wire up individually. `initialize()` already owns process-wide setup (monkey patches, event loop
policy, logging), so this is one more thing it configures once per process, guarded the same way as
the existing `run_monkey_patches`/`asyncio` flags. Registration sets `SHUTDOWN_EVENT` and returns
control to whatever is running — it does not itself block or perform shutdown work.

**D-G3 — platform dispatch is explicit, following the existing `platform in (...)` branch used for the
event loop policy choice (line 76).** POSIX registers `SIGINT` and `SIGTERM` via `signal.signal`.
Windows registers `SIGINT` and, where available, `SIGBREAK` — Windows has no `SIGTERM` delivery
mechanism from the OS, so nothing is lost by not registering it there. No `pywin32`/console-control-handler
dependency is introduced; this stays within the standard-library `signal` module.

**D-G4 — consumers own their own drain/flush logic behind the event; this plan only adds the signal.**
The concrete guarantee this unblocks is that log-writing clients (D-E5/D-E2's `AsyncioQueueDrainer` /
`ThreadedQueueDrainer` / `HandshakeSocketHandler`, and the central log server's writer thread) flush
buffered records to their handlers before the process exits — but wiring each consumer's shutdown
sequence is separate follow-up work, not part of this PR. This plan's scope ends at: the event exists,
is importable, and is set reliably by both a caught OS signal and the D-E6 fatal path.

### Graceful shutdown for log-server clients (D-H series — D-G4 follow-up)

**Why this is needed now:** D-G2's signal handler intercepts `SIGINT`/`SIGTERM` and *only* sets
`SHUTDOWN_EVENT` — unlike Python's default handlers, it does not raise `KeyboardInterrupt`, call
`sys.exit()`, or otherwise unwind the process. Nothing else currently reacts to that event. Under
Docker, a container receives `SIGTERM`, is given a finite grace period, then is `SIGKILL`ed — so unless
something actively uses that grace period, it is wasted. Investigation for this follow-up also found
that none of `HandshakeSocketHandler` / `AsyncioQueueDrainer` / `ThreadedQueueDrainer`
(`src/aeth_ext/central_log_server/client/__init__.py`) currently flush their
`RecordHistoryBuffer` (`src/aeth_ext/central_log_server/client/history.py`) on close/shutdown at all —
whatever is still sitting in memory, unflushed, is simply lost.

**Priority, per explicit direction:** getting already-buffered *and* still-arriving records durably
onto the on-disk JSONL history file during the grace period is what matters. Attempting network
delivery to the central log server during shutdown is explicitly out of scope ("gravy") — it is not
attempted here, and nothing in this section should be read as implying the drain loops, sockets, or
`_stop_event`s of the three client classes are touched. (Forcing those loops to stop as part of this
would risk `AsyncioQueueDrainer.__aexit__`'s `await self._queue.join()` hanging forever on a queue
nothing is draining anymore — a real hazard identified and deliberately avoided.)

**D-H1 — a project-wide registry, not a new watcher thread; zero new threads anywhere in this
section.** Two call sites already run synchronously in direct reaction to a shutdown trigger, and both
become the sole triggers of a new `run_graceful_shutdown()` function — nothing new ever blocks on
`SHUTDOWN_EVENT.wait()`:

- `_handle_shutdown_signal` in [`src/aeth_ext/__init__.py`](src/aeth_ext/__init__.py) — an OS signal
  callback. CPython signal callbacks registered via `signal.signal()` run on the main thread *between*
  bytecode instructions, not as raw async-C handlers, so ordinary Python calls (including file I/O) are
  an established-safe pattern here — this is exactly why the existing D-G2 code already installs plain
  `signal.signal()` handlers around a running asyncio loop without special precautions, and it already
  works.
- `handle_config_rejected` in [`src/aeth_ext/errors/err_handling.py`](src/aeth_ext/errors/err_handling.py)
  (D-E6) — an ordinary function call from whatever background thread detected the client-side
  rejection. No signal-safety concerns apply here at all.

**D-H2 — the registry lives in `err_handling.py`**, next to `SHUTDOWN_EVENT`/`FATAL_EVENT`, since it is
general process-shutdown infrastructure, not specific to logging or the central log server.

- `SupportsGracefulShutdown` — a `typing.Protocol` (structural, **not** a base class to inherit from)
  with exactly one method: `graceful_shutdown(self) -> None`.
- `register_for_graceful_shutdown(obj: SupportsGracefulShutdown, *, priority: int = 0) -> None` — public
  API, exported from `err_handling.__all__` and `aeth_ext.errors.__init__` alongside `SHUTDOWN_EVENT`.
  Stores `(weakref.ref(obj), package, priority)` — as a `_Registration(NamedTuple)`, mirroring the
  existing `_Hierarchy(NamedTuple)` pattern in `writer_thread.py` — in a module-level `list` guarded by a
  plain (non-reentrant) `threading.Lock`.
- **`package` is auto-detected, not passed in.** Resolved via
  `sys._getframe(1).f_globals.get("__name__", "__main__").split(".", 1)[0]` — the *caller's own*
  top-level package, with zero extra argument required. `aeth_ext`'s own internal registrations (e.g.
  `RecordHistoryBuffer.__init__`, D-H5) therefore land in the `"aeth_ext"` bucket automatically, purely
  because of *where in the source tree* the registration call lives; any downstream consumer's own
  registrations land under their own top-level package name the exact same way, with no risk of a
  typo'd or hardcoded namespace string and no possibility of accidentally colliding with `"aeth_ext"`'s
  own bucket.
- Dead (garbage-collected) entries are pruned opportunistically at registration time — a cheap O(n)
  scan over what is expected to be a small, slow-growing list — rather than via a `weakref` finalize
  callback, to keep the mechanism simple. No explicit unregister function is needed or provided.

**D-H3 — run order: all of `aeth_ext`'s own registrations first (priority ascending), then every other
package's own registrations (each sorted by its own priority ascending; no ordering guarantee between
two different non-`aeth_ext` packages beyond first-registration-encountered order).** Lower priority
number shuts down earlier — priority `0` runs before priority `10` within the same package's group,
matching the common "priority 0 = goes first" convention.

```python
def run_graceful_shutdown() -> None:
  if SHUTDOWN_EVENT.is_set():
    return  # fast-path only, not a strict mutex — see rationale below
  SHUTDOWN_EVENT.set()
  with _registry_lock:
    _prune_dead_registrations()
    snapshot = list(_registry)
  aeth_ext_group = sorted((r for r in snapshot if r.package == "aeth_ext"), key=lambda r: r.priority)
  other_groups: dict[str, list[_Registration]] = {}
  for r in snapshot:
    if r.package != "aeth_ext":
      other_groups.setdefault(r.package, []).append(r)
  ordered = [*aeth_ext_group]
  for regs in other_groups.values():
    ordered.extend(sorted(regs, key=lambda r: r.priority))
  for reg in ordered:
    obj = reg.ref()
    if obj is None:
      continue
    try:
      obj.graceful_shutdown()
    except Exception:
      logger.exception("graceful_shutdown() failed for %r (package=%r, priority=%r)", obj, reg.package, reg.priority)
```

- The `if SHUTDOWN_EVENT.is_set(): return` guard is a cheap common-case check, **not** a strict mutex: a
  rare race between the OS-signal path and the `handle_config_rejected` path firing at nearly the same
  instant could run the full registry twice. Accepted deliberately rather than adding lock machinery
  around the whole function, because every `graceful_shutdown()` implementation is expected to be
  idempotent/cheap (flushing an already-empty buffer is a no-op), and a broader lock here risks a
  subtle deadlock if some future implementation ever logs through a path that re-enters this same code.
- **No ordering conflict exists yet to actually resolve.** At the time of writing, `RecordHistoryBuffer`
  (D-H5) is the *only* thing that self-registers, so there is nothing today competing for order. This
  section is explicitly building the *infrastructure* — correctly used — so that future additions (e.g.
  socket teardown, id-checkpoint persistence, anything that must not go offline before something else
  that depends on it) have a safe, pre-existing place to plug into without a redesign.

**D-H4 — the two existing `SHUTDOWN_EVENT.set()` call sites are replaced with `run_graceful_shutdown()`.**

- `_handle_shutdown_signal` (`src/aeth_ext/__init__.py`) — currently just `SHUTDOWN_EVENT.set()`;
  becomes `run_graceful_shutdown()` (imported from `aeth_ext.errors`). Docstring updated to drop the
  "does not itself perform shutdown work" claim, since now it does (by design).
- `handle_config_rejected` (`src/aeth_ext/errors/err_handling.py`) — currently
  `FATAL_EVENT.set(); SHUTDOWN_EVENT.set()`; becomes `FATAL_EVENT.set(); run_graceful_shutdown()`.
  `FATAL_EVENT` stays a separate, independent call — it is not part of the registry, it remains its own
  alerting primitive per D-G1.

**D-H5 — `RecordHistoryBuffer` (`src/aeth_ext/central_log_server/client/history.py`) gains a public
`flush()`, a `graceful_shutdown()`, and an always-flush-immediately mode once shutdown has started.**

- `flush()` — forces `self._flush_to_disk()` now, bypassing the count/byte/age thresholds
  `_maybe_flush()` normally gates on, then resets `self._last_flush_monotonic` the same way
  `_maybe_flush()` does after its own call to `_flush_to_disk()`.
- `graceful_shutdown()` — sets a new `self._shutting_down: bool` instance flag (default `False`, set in
  `__init__`) to `True`, then calls `self.flush()`. This is what makes `RecordHistoryBuffer` satisfy
  `SupportsGracefulShutdown` structurally — no inheritance needed, nothing else about the class changes.
- `append()` checks `self._shutting_down`: once `True`, every subsequent `append()` calls
  `self._flush_to_disk()` unconditionally afterward, **instead of** the normal threshold-gated
  `self._maybe_flush()`. This is the concrete mechanism that satisfies "ensure records get flushed to
  their history file" for anything logged *during* the shutdown grace period — not just what was
  already buffered at the moment shutdown began, but everything that continues to arrive up until the
  process actually dies.
- `__init__` calls `register_for_graceful_shutdown(self)` (default `priority=0` — nothing else in
  `aeth_ext` registers yet, so this value is a placeholder, not a considered choice against another
  registrant; revisit if/when a second `aeth_ext`-internal registrant is added).
- **The three client classes (`HandshakeSocketHandler`, `AsyncioQueueDrainer`, `ThreadedQueueDrainer`)
  need no code changes for this part.** Each already constructs `self._history = RecordHistoryBuffer(...)`
  in its own `__init__`; the buffer now handles its own shutdown registration internally, so all three
  get this wiring for free at their existing construction sites.

**D-H6 — startup repair of a truncated trailing JSONL record**, added to `RecordHistoryBuffer.__init__`,
addressing the case where a *previous* run's grace period ran out mid-write.

- Checked file: only the **current day's** history file
  (`_history_file_for_date(self.history_dir, <today>)`) — that is the only file a fresh instance is
  about to append to; other days' files are read-only lookup targets for `find_after`/`_search_disk`
  and are not touched by this check.
- Detection: read the file's raw bytes. If it does not end in `b"\n"`, the segment after the last
  `b"\n"` (or the whole file, if there is no newline at all) is *suspect* — every successful write
  always appends a full JSON line plus a trailing `\n` (see `_format_entry_line`/`_flush_to_disk`), so a
  missing final newline is sufficient evidence that something is off by construction. A well-formed
  file (ends in `\n`, or is empty) is left untouched with no further work.
- Attempt to parse that suspect trailing segment as JSON to determine which of two outcomes applies:
  - It fails to parse as JSON (`orjson.loads` raises `JSONDecodeError`) → a genuine partial write,
    nothing recoverable. Log a warning (report the byte length, not the raw content — a partial write
    may not even be valid UTF-8) and truncate the file at the position right after the last complete
    `\n` (open with `"r+b"`, then `fh.truncate(<pos>)`).
  - It parses successfully → a complete, valid record that is merely missing its own trailing newline
    (should not happen given the write pattern, but cheap to handle defensively rather than assume it
    can't). Append the single missing `b"\n"` (open with `"ab"`) rather than deleting a valid record.
- Both branches wrap their file operations in `try`/`except OSError`, logging and returning rather than
  raising — this is best-effort hygiene, not a hard dependency of construction succeeding.

**D-H7 — `EmergencyHistoryWriter` needs no shutdown-registry participation; its stale TODO is corrected,
not acted on.** The `# TODO needs a detector for FATAL_EVENT being set so it can drain the queue and
exit promptly` comment inside `EmergencyHistoryWriter._run` predates `SHUTDOWN_EVENT`'s existence and,
on inspection, describes a problem this class does not actually have: every record it receives via
`submit()` is written **and flushed** to disk synchronously inside `_run()`'s loop, the same iteration
it is dequeued (`current_fh.write(...); current_fh.flush()`) — so there is no in-memory buffering here
that could be lost; durability is already immediate, per-record. The writer's own thread is
`daemon=True` and so does not block process exit either way. Replace the comment with a short note
recording this conclusion; do not add any new behaviour to this class.

### Process

**D-F1 — one PR.**

**D-F2 → TODO.md.** `iter_unique_handlers` has no direct test despite being in `__all__` and used at
[writer_thread.py:313](src/aeth_ext/central_log_server/server/writer_thread.py#L313).

---

## Implementation order

Single PR, but this is the dependency order to build in:

1. **[DONE] Configurator core** — `__init__` / `validate()` / `apply(private)` / `configure()`, the
   validated flag, the mkdir list (D-A3), handler tracking + failure cleanup (D-A7), `cfg://`
   deferral (D-A2).
2. **[DONE] Chaff cut** — delete `_apply_config` and `build_hierarchy`; rewrite their call sites plus
   `json_config` / `toml_config` / `yaml_config` / `_process_socket_config_chunk` (D-F4).
   After this step behaviour is unchanged and the suite should be green.
3. **[DONE] New config fields** — the opt-out flag (D-E1) and the rotation-style field (D-D2) on
   `LoggingConfigModel`, plus the packaged remote fragments.
4. **[DONE] Writer-thread apply** — `RegisterClient` carries the configurator; `_register_client` becomes
   async and awaits `asyncio.to_thread(...)` inline (D-C2).
5. **[DONE] Fallback** — rotation-style-matched fallback config with `_fallback` filenames (D-D2/D-D3),
   rejection when unextractable or when the fallback itself fails (D-D4).
6. **[DONE] Result signalling** — the `aiologic.Event` wrapper (D-E3), reader-side
   `asyncio.wait(..., FIRST_COMPLETED)` (D-E2), the two new wire messages plus discriminator (D-E5).
7. **[DONE] Client receive paths** (D-E5) and the `FATAL_EVENT` alert handler in `err_handling` (D-E6),
   plus the ack-failure alert (D-E7).
8. **[DONE] Docs** — update the stale "validated *and applied* by the reader" claims at
   [_types.py:23](src/aeth_ext/central_log_server/_types.py#L23),
   [reader_server.py:44-53](src/aeth_ext/central_log_server/server/reader_server.py#L44), and
   [writer_thread.py:269-270](src/aeth_ext/central_log_server/server/writer_thread.py#L269);
   mark TODO.md item 3 resolved; add the D-D5 and D-F2 deferrals to TODO.md.
9. **[DONE] Graceful shutdown signal** — `SHUTDOWN_EVENT` alongside `FATAL_EVENT` in `err_handling.py`
   (D-G1); OS signal handler registration inside `initialize()` for POSIX (`SIGINT`/`SIGTERM`) and
   Windows (`SIGINT`/`SIGBREAK`) (D-G2/D-G3); wire the D-E6 fatal-shutdown path to also set
   `SHUTDOWN_EVENT` (D-G4). No consumer drain/flush logic yet — just the signal and its two triggers.
10. **[SUPERSEDED - see PLAN-graceful-shutdown.md]** **Registry core** — `SupportsGracefulShutdown` Protocol, `register_for_graceful_shutdown()`,
    `run_graceful_shutdown()` in `err_handling.py` (D-H1/D-H2/D-H3). Export `run_graceful_shutdown`,
    `register_for_graceful_shutdown`, and `SupportsGracefulShutdown` from `err_handling.__all__` and
    `aeth_ext.errors.__init__`, matching the existing export pattern for `SHUTDOWN_EVENT`/`FATAL_EVENT` —
    consumers need `register_for_graceful_shutdown` to register their own objects, so it must be public.
11. **[SUPERSEDED - see PLAN-graceful-shutdown.md]** **Wire the two trigger call sites onto the registry** (D-H4): `_handle_shutdown_signal`
    (`src/aeth_ext/__init__.py`) and `handle_config_rejected` (`err_handling.py`) both call
    `run_graceful_shutdown()` instead of a bare `SHUTDOWN_EVENT.set()`.
12. **[SUPERSEDED - see PLAN-graceful-shutdown.md]** **`RecordHistoryBuffer` changes** (D-H5): public `flush()`, `graceful_shutdown()`, the
    `_shutting_down`-gated immediate flush inside `append()`, and self-registration in `__init__`. No
    changes needed to `HandshakeSocketHandler` / `AsyncioQueueDrainer` / `ThreadedQueueDrainer`
    themselves.
13. **[SUPERSEDED - see PLAN-graceful-shutdown.md]** **Startup repair** (D-H6): truncated-trailing-record detection/repair added to
    `RecordHistoryBuffer.__init__`, scoped to the current day's history file only.
14. **[SUPERSEDED - see PLAN-graceful-shutdown.md]** **`EmergencyHistoryWriter` comment cleanup** (D-H7) — replace the stale
    `# TODO needs a detector for FATAL_EVENT...` comment with a short note recording why this class
    needs no shutdown-registry participation. No behavioural change to this class.

---

## Verification

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

End-to-end confidence comes from
[test_client_server_integration.py](tests/central_log_server/test_client_server_integration.py),
which spawns a real server subprocess and a real client (full round trip, server outage with
id-resume, abrupt disconnect). Per D-A5, expect to rewrite tests freely to match the new intent.

Manual check after the writer-thread move: confirm handshake latency no longer scales with config
size, and that a reconnect storm no longer stalls record flow.

**For the D-H series specifically:** `SHUTDOWN_EVENT` is one-shot and process-global, exactly like
`FATAL_EVENT` — any test that actually calls `run_graceful_shutdown()` (or otherwise sets the event for
real) must run in an isolated subprocess, following the existing pattern in
`tests/errors/_optimized_scenarios.py` + `tests/errors/test_err_handling.py`, to avoid poisoning the
rest of the pytest session. Tests that only need to exercise *registration*/*ordering* logic (package
auto-detection, priority sorting across the two tiers) can do so in-process without ever setting the
event, by calling the grouping/sorting logic directly or by monkeypatching a throwaway
`aiologic.Event()` in place of the real `SHUTDOWN_EVENT` for the duration of one test. Manual/functional
check: construct a `RecordHistoryBuffer`, `append()` a few entries without tripping the normal
thresholds, send the process `SIGTERM` (POSIX) or invoke the registered handler directly (Windows), and
confirm the JSONL history file contains those entries afterward; separately, hard-kill a process
mid-write (or hand-craft a file with a truncated final line) and confirm the next `RecordHistoryBuffer`
construction repairs it per D-H6.
