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
`self.config` in place), so it can never be re-applied or retried after a failed `apply()`.

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

### Fallback — RETRACTED

D-D1–D-D5 (a rotation-style-matched fallback config, built from scratch rather than the packaged
fragment loaders, applied when a well-formed config failed to apply) shipped, then was retracted
once the graceful-shutdown system gave the client a clean alert-then-shut-down path for an
unusable config. See [PLAN-retract-fallback-config.md](PLAN-retract-fallback-config.md) for the
retraction and why. Nothing below should reference the fallback as present.

### Client notification

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

**D-E6 — a rejected/failed config on the client side is confirmed to shut down the host
application**, always paired with an alert e-mail and Pushover notification — the operator is
notified immediately and responds within minutes, which is what makes the aggressive shutdown
acceptable. (The specific function owning this has moved and been renamed more than once since;
see [PLAN-retract-fallback-config.md](PLAN-retract-fallback-config.md) for the current shape —
don't trust a function name cited here.)

**D-E7 — failing to receive the ack raises an alert but does not shut down.** Today `_read_ack`
returns `None` silently on timeout/malformed/dead-connection, so resume-by-id is skipped with nobody
noticing. That silent degradation becomes an alert.

### Graceful shutdown

> **Fully superseded by [PLAN-graceful-shutdown.md](PLAN-graceful-shutdown.md) (the D-I series),
> which shipped.** The design that used to be here (D-G1–D-H7: a bare `SHUTDOWN_EVENT`, a
> `SupportsGracefulShutdown` registry) turned out to be unimplementable as written — nothing ever
> terminated the process, the event had zero readers, the registry's teardown ordering was
> inverted. None of it was built. Read the other doc for what actually exists
> (`errors/shutdown.py`); nothing here is worth re-deriving.

### Process

**D-F1 — one PR.**

**D-F2 → TODO.md.** `iter_unique_handlers` has no direct test despite being in `__all__` and used at
[writer_thread.py:313](src/aeth_ext/central_log_server/server/writer_thread.py#L313).

---

## Implementation order

All steps below shipped. Kept as a terse record of what happened, not as instructions to follow —
see each step's git history for the actual changes.

1. **[DONE]** Configurator core — `validate()`/`apply(private)` split (D-A1–D-A7).
2. **[DONE]** Chaff cut — deleted `_apply_config`/`build_hierarchy`; callers construct
   `DictConfigurator` directly (D-F3/D-F4).
3. **[DONE, later retracted]** New config fields — the fallback opt-out flag and rotation-style
   field. Both deleted again by [PLAN-retract-fallback-config.md](PLAN-retract-fallback-config.md).
4. **[DONE]** Writer-thread apply — `RegisterClient` carries the configurator; `_register_client`
   awaits `asyncio.to_thread(...)` inline (D-C2).
5. **[DONE, later retracted]** Fallback config on apply failure — deleted by
   [PLAN-retract-fallback-config.md](PLAN-retract-fallback-config.md).
6. **[DONE]** Result signalling — the `ApplyResultEvent` wrapper (D-E3), reader-side
   `asyncio.wait(..., FIRST_COMPLETED)` (D-E2), `ApplySuccess`/`ApplyFailure` plus discriminator (D-E5).
7. **[DONE]** Client receive paths (D-E5) and the alert-on-rejection/ack-failure handling (D-E6/D-E7)
   — current shape is in [PLAN-retract-fallback-config.md](PLAN-retract-fallback-config.md), not here.
8. **[DONE]** Docs — stale "validated and applied by the reader" claims corrected.
9. **[DONE]** Graceful shutdown signal — superseded in full by
   [PLAN-graceful-shutdown.md](PLAN-graceful-shutdown.md); see that doc.

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
