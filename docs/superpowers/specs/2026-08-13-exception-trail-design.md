# Design: standardized fatal-exception-origin API (`ExceptionTrail`)

Design agreed 2026-08-13. Sub-project #1 of the
[shutdown-and-ftp master plan](2026-08-13-shutdown-and-ftp-master-plan.md). Independent of every
other sub-project in that plan; no blocking dependency in either direction. Implements
[TODO.md item #6](../../TODO.md). Lands on its own branch, separate from sub-project #2's branch,
until complete.

## Context

`handle_fatal_exc_sync`/`handle_fatal_exc_async` (`src/aeth_ext/errors/err_handling.py`) take an
`extract_details_callable: Callable[[BaseException], Any] | None` parameter — an arbitrary escape
hatch added for exactly one anticipated consumer, with no real production caller until
`scheduled-invoice-processor`'s `err_handling.py::extract_exc_details`. That function hand-rolls its
own traceback walk: it manually iterates `exc.__traceback__` (via `traceback.extract_tb`) and the
`__cause__`/`__context__` chain (with `id()`-based cycle detection), matching each frame's *file path*
against a tuple of hardcoded path-string markers
(`_DATABASE_FATAL_PATH_MARKERS = ("\\src\\database\\", "/gspread/", ...)`) to answer one question:
"did this exception touch the database layer?" — used to decide whether a final Sheets flush is safe
to attempt during fatal shutdown.

`extract_details_callable` doesn't generalize: every consumer would have to reimplement the same
frame-walking, path-matching, cycle-detection logic. This design replaces it with a shared, richer
primitive that computes an exception's full origin trail once and answers a small fixed set of
standardized questions about it, plus wires that trail through `aeth_ext`'s existing shutdown
machinery so both registered-callback consumers and ad hoc cleanup code can retrieve it.

## Non-goals

- **Not building a general-purpose state-storage/history primitive.** `_last_fatal_details`-style
  "keep the most recent N fatal exceptions" is app-owned plumbing (`scheduled-invoice-processor`
  keeps its own module-level dict); `aeth_ext` exposes only the *current* in-flight fatal trail via a
  narrow getter, not a history or general key-value store.
- **Not attempting to infer "did this touch mutated state" automatically.** The `matches()` query is a
  coarse, caller-supplied-prefix proxy for that question — genuinely inferring which frames mutate
  state is acknowledged as impractical to do empirically and is out of scope entirely.
- **Not changing `run_shutdown()`'s signature.** It stays `run_shutdown(kind: ShutdownKind = ...)`.
  Only `_handle_fatal` (the one call site with a live exception) ever has a trail to report, so the
  trail is threaded through a separate settable slot rather than a parameter every caller must reason
  about.

## The trail: `build_exception_trail` and `ExceptionTrail`

New module, `src/aeth_ext/errors/exception_trail.py`.

**`OriginCategory`** (`StrEnum`): `FIRST_PARTY`, `THIRD_PARTY`, `STDLIB`, `UNPACKAGED`.

**`TrailEntry`** (`NamedTuple`): `module: str`, `category: OriginCategory`, `file: str`.

**`build_exception_trail(exc: BaseException, *, walk_chain: bool = True) -> ExceptionTrail`**:

1. Walk `exc.__traceback__` frame by frame, **innermost (origin) first** — i.e. reverse of
   `traceback.extract_tb`'s natural outer-to-inner order, since "origin" must be `entries[0]`.
2. When `walk_chain=True` (default, matches today's `_is_database_origin_exception` behavior), also
   walk `exc.__cause__` and `exc.__context__`, recursively, with `id()`-based cycle detection —
   ported directly from the existing app-side implementation's stack-and-seen-set approach.
3. For each frame, resolve its module via `frame.f_globals.get("__name__")`. Deduplicate consecutive
   frames belonging to the same module (a tight loop within one function produces many frames in the
   same module; the trail records distinct module transitions, not raw frame count).
4. Categorize each resolved module:
   - **`STDLIB`**: the module's top-level component (e.g. `"asyncio"` from `"asyncio.tasks"`) is in
     `sys.stdlib_module_names` (Python 3.10+, always available under this repo's `requires-python
     >=3.14` floor). Checked first — a stdlib frame has no meaningful package root to compute.
   - **`FIRST_PARTY`** / **`THIRD_PARTY`**: otherwise, resolved via
     `aeth_ext.static_eval.get_package_root(frame's file)` — reuse its existing `site-packages`
     short-circuit: a file under a `site-packages` directory categorizes `THIRD_PARTY`; anything else
     resolves to a package root, categorized `FIRST_PARTY` **only if** that root equals
     `get_entrypoint_root()` (called once, uncached, per `build_exception_trail` call — cheap relative
     to the walk itself, and the entrypoint doesn't change mid-process so per-call recomputation is
     just simplicity over a micro-optimization). This is a different call than
     `_resolve_program_name()` in `err_handling.py`, which returns a display *name* string (used for
     alert attribution) rather than a comparable path — the two are conceptually related (both
     identify "the host application") but are separate calls returning different types, not a shared
     cached value. A resolvable package root that is *not* the host application's own (e.g. a
     first-party-looking local package that isn't the app itself — rare but possible in a monorepo)
     still categorizes `THIRD_PARTY`, since "first party" means "belongs to the application that's
     running," not merely "not in site-packages."
   - **`UNPACKAGED`**: no resolvable package root at all (a loose script, `__main__`, code typed at an
     interactive prompt, or `frame.f_globals["__name__"]` unavailable).
5. Build the immutable `ExceptionTrail`, computing all derived attributes eagerly (see below) — the
   walk is already small (a handful to a few dozen frames per real exception), so eager computation
   costs nothing measurable relative to the walk itself.

**`ExceptionTrail`** (frozen dataclass or similar immutable container):

- `.entries: tuple[TrailEntry, ...]` — origin-first, deduplicated, as built above.
- `.origin: TrailEntry` — `entries[0]`. `build_exception_trail` never returns an empty trail (an
  exception always has at least one frame — the one that raised it), so this is never `None`.
- `.first_party_entry: TrailEntry | None` — the first entry (scanning origin → outward) categorized
  `FIRST_PARTY`; `None` if the trail never touches the host application's own code (e.g. a failure
  entirely inside a dependency, never propagated up into caller code — unusual but possible for a
  `report_exc`/`handle_fatal_exc_*`-wrapped callback that's itself third-party).
- `.matches(*patterns: str) -> tuple[TrailEntry, ...]` — every entry whose `module` matches any given
  glob pattern (see below). Empty tuple is falsy, so `if trail.matches(...):` reads as a boolean check
  while still handing back full matching detail (module, category, file, position via index into
  `.entries`) to a caller that wants it — this is the one query this design commits to as "the"
  standardized way to ask "did this trail touch marked module(s)," replacing every consumer's own
  hand-rolled membership check.

## Glob pattern matching

Patterns are dot-segment strings, matched against a module's full dotted name, **fully anchored**
(start-to-end — a bare `"database"` matches only a module literally named `database`, not as a suffix
of `scheduled_invoice_processor.database`; matching a suffix requires an explicit `**.database`).

- A plain segment (e.g. `scheduled_invoice_processor`) matches that literal segment.
- `*` matches exactly one segment.
- `**` matches zero or more segments (standard glob/gitignore convention — `a.**.b` matches `a.b`,
  `a.x.b`, and `a.x.y.b` alike; `**` matching zero segments is what makes `a.**.b` also cover the
  directly-adjacent case without a second pattern).

Implementation: compile each pattern to a `re.Pattern` once (patterns are typically supplied as static
constants at a call site, so compiling at `matches()`-call time is fine — no caching needed unless
profiling later shows otherwise), translating each dot-separated pattern segment to the equivalent
regex fragment (`re.escape` for literal segments, `[^.]+` for `*`, `(?:[^.]+\.)*[^.]*` — or equivalent
— for `**`), joined by literal `\.` between segments and fully anchored with `^`/`$`.

Examples:
- `"scheduled_invoice_processor.database"` — matches only that exact module.
- `"scheduled_invoice_processor.*.database"` — matches `scheduled_invoice_processor.suppliers.database`
  but not `scheduled_invoice_processor.database` (no segment in between) or
  `scheduled_invoice_processor.a.b.database` (two segments in between).
- `"scheduled_invoice_processor.**.database"` — matches all three of the above.
- `"**.gspread.**"` — matches any module with `gspread` as any segment, anywhere.

## Wiring into `aeth_ext.errors.shutdown`

New module-level state in `src/aeth_ext/errors/shutdown.py`, alongside the existing `_t0`/
`_exit_nudge_sent`/`_drive_released` pattern (plain reference swap, safe to touch from interrupt
context — no lock, matching every other piece of shutdown-adjacent module state):

```python
_current_fatal_trail: ExceptionTrail | None = None


def get_current_fatal_trail() -> ExceptionTrail | None:
  """The ExceptionTrail for the exception currently driving a fatal shutdown, if any.

  None when no shutdown is underway, or the current shutdown was not triggered by a
  live exception (e.g. a signal-driven GRACEFUL shutdown, or trigger_shutdown() for a
  plain error condition with nothing to raise). Set by _handle_fatal
  (aeth_ext.errors.err_handling) immediately before it calls run_shutdown(FATAL); never
  cleared afterward, matching SHUTDOWN's own one-shot semantics -- once a fatal trail
  exists, it describes the shutdown for the rest of the process's life.

  This is the retrieval mechanism for cleanup code that runs behind the shutdown event
  (e.g. `await SHUTDOWN` in an application's main loop) rather than as a registered
  register_for_shutdown callback -- those receive the trail as a call argument instead
  (see register_for_shutdown).
  """
  return _current_fatal_trail


def _set_current_fatal_trail(trail: ExceptionTrail) -> None:
  """Private setter, called only by aeth_ext.errors.err_handling._handle_fatal."""
  global _current_fatal_trail
  _current_fatal_trail = trail
```

**`register_for_shutdown` signature detection**: at registration time, `inspect.signature(callback)` is
inspected once. If the callback accepts one positional parameter (bound method or plain function —
matching the existing `WeakMethod`-vs-strong-reference distinction `register_for_shutdown` already
makes based on `__self__`), it is recorded as "wants the trail." Every existing registrant in `aeth_ext`
itself (the log-transport drainers, `central_log_server`'s arm/close callbacks) is zero-arg and
continues to work unmodified — this is purely additive.

**Callback invocation** (`_run_interrupt_pass`, `_run_threaded_pass`): a "wants the trail" callback is
invoked as `callback(_current_fatal_trail)` instead of `callback()`. `_current_fatal_trail` may be
`None` (shutdown wasn't exception-triggered) — a callback that wants the trail must handle `None`
itself; this is a normal `ExceptionTrail | None` parameter, not a guarantee of non-`None`.

## Wiring into `aeth_ext.errors.err_handling`

**`_handle_fatal`** (the single shared fatal-path helper used by `report_exc`,
`handle_fatal_exc_sync`, `handle_fatal_exc_async`) gains one step, before its existing
`run_shutdown(ShutdownKind.FATAL)` call:

```python
def _handle_fatal(label: str, exc: BaseException) -> None:
  logger.critical("Fatal exception in %s", label, exc_info=exc)
  traceback_text = _extract_rich_traceback()
  alert(f"Fatal exception in {label}", f"{exc}:\n\n{traceback_text}", priority=_FATAL_PUSH_PRIORITY)
  # Standard library imports
  from aeth_ext.errors.exception_trail import build_exception_trail
  from aeth_ext.errors.shutdown import _set_current_fatal_trail

  _set_current_fatal_trail(build_exception_trail(exc))
  run_shutdown(ShutdownKind.FATAL)
```

**`extract_details_callable` is renamed and retyped** on `handle_fatal_exc_sync`/
`handle_fatal_exc_async`: `extract_trail_callable: Callable[[ExceptionTrail], Any] | None = None`.
Same side-effecting-callback contract as today (the return value is still discarded; the callback is
still wrapped in its own `try/except Exception` so a broken extractor can't itself crash the fatal
path) — the only change is what gets passed in: the standardized `ExceptionTrail` for `exc` (built via
the same `build_exception_trail(exc)` call used above, computed once and reused — not rebuilt a second
time for the callback) instead of the raw exception.

```python
def handle_fatal_exc_sync[**Params_T, Return_T](
  func: Callable[Params_T, Return_T] | None = None,
  *,
  extract_trail_callable: Callable[[ExceptionTrail], Any] | None = None,
) -> ...:
  def decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
      try:
        return func(*args, **kwargs)
      except CancelledError:
        raise
      except BaseException as e:
        if extract_trail_callable is not None:
          try:
            extract_trail_callable(build_exception_trail(e))
          except Exception as extract_exc:
            logger.exception("Error in extract_trail_callable for exception", exc_info=extract_exc)
        _handle_fatal(func.__qualname__, e)
        return None
    return func if __debug__ and __name__ != "__main__" else wrapper
  ...
```

Identical change to `handle_fatal_exc_async`.

## Consumer migration (not part of this spec)

`scheduled-invoice-processor`'s adoption of this API is sub-project #5 in the master plan, specced
separately once this ships. Sketch only, to confirm the new API actually satisfies the motivating use
case: `_is_database_origin_exception`'s hand-rolled walk is deleted; `extract_exc_details` becomes
`extract_trail_callable=lambda trail: _last_fatal_details.update(is_database_origin=bool(trail.matches(
"scheduled_invoice_processor.database", "**.gspread.**", "**.google.oauth2.**"
)), ...)` — same three real-world markers as today's `_DATABASE_FATAL_PATH_MARKERS`, translated from
path fragments to module-glob patterns.

## Testing

- **Trail construction**: a synthetic exception raised through several nested functions across at
  least two real modules — assert `.entries` is ordered origin-first, deduplicates consecutive
  same-module frames, and each entry's category is correct (a stdlib call in the mix, e.g. via
  `json.loads` raising, categorized `STDLIB`; the test module itself categorized `FIRST_PARTY`; an
  installed third-party dependency in the call path categorized `THIRD_PARTY`).
- **Chain walking**: `raise NewError(...) from original` — assert the trail includes frames from both
  `original` and the wrapping exception, matching `walk_chain=True`'s default; assert `walk_chain=False`
  excludes the cause chain. A cyclic `__context__` (implicit chaining loop) does not infinite-loop.
- **`UNPACKAGED` categorization**: a frame from a file with no resolvable package root (e.g. a script
  executed via `exec()` with a synthetic `__name__`/no real file) categorizes `UNPACKAGED`.
- **`first_party_entry`**: `None` when a trail never touches the host application's own package;
  correctly finds the first (origin-outward) first-party frame when one exists partway through the
  trail.
- **Glob matcher**: table-driven tests covering — literal exact match; `*` matching exactly one
  segment and rejecting zero or two; `**` matching zero, one, and multiple segments; full-string
  anchoring (a pattern that would match as a substring/suffix without anchoring must not match).
- **`matches()`**: returns all matching entries (not just the first), in trail order; empty tuple
  (falsy) when nothing matches.
- **`register_for_shutdown` signature detection**: a zero-arg callback is invoked with no arguments; a
  one-positional-arg callback is invoked with `get_current_fatal_trail()`'s current value (both `None`
  and a real trail, in separate test cases) — reuse the existing shutdown test harness's `-O` subprocess
  pattern (`tests/errors/_optimized_scenarios.py`) since `register_for_shutdown`'s invocation only
  happens during a real driven shutdown.
- **`get_current_fatal_trail`**: `None` before any fatal shutdown; returns the trail `_handle_fatal`
  built, after one fires, in an isolated `-O` subprocess (per `ShutdownState`'s existing one-shot,
  can't-be-cleared testing convention).
- **`extract_trail_callable`**: a broken callback (raises inside itself) does not prevent
  `_handle_fatal`'s subsequent logging/alerting/shutdown from running — mirrors the existing
  `extract_details_callable` error-handling test.
