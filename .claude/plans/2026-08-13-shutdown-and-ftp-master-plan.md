# Master plan: shutdown lifecycle, FTP performance, and fatal-exception-origin API

Written 2026-08-13. This document exists identically in both `scheduled-invoice-processor` and
`aeth_ext` (each repo's copy lives at `.claude/plans/2026-08-13-shutdown-and-ftp-master-plan.md`).
Each repo holds spec files only for the sub-projects that touch it — see "Where things live" below.

## How this started

A routine "upgrade to aeth_ext v7" task turned up two things during investigation that made a
mechanical version bump insufficient:

1. **A real race condition.** `scheduled-invoice-processor`'s `startup.py::main()` drives its own
   fatal-shutdown teardown (pause scheduler, sleep up to 600s, flush Google Sheets writes,
   `sys.exit(1)`) directly after `await SHUTDOWN`. aeth_ext v7's shutdown system
   (`aeth_ext.errors.shutdown`) *also* drives teardown independently, on a daemon thread, and force-exits
   the main thread via `interrupt_main()` once its own (much shorter, ~7s) budget is up. These two
   teardown sequences run uncoordinated on the same process — aeth_ext's finishes first essentially
   always, and can `KeyboardInterrupt` the app's own cleanup mid-flight.
2. **This app predates and inspired aeth_ext's shutdown system**, and has grown considerably in
   sophistication since the original design (e.g. the `sleep(600)` heuristic, per-supplier `.errored`
   circuit breaking, 10-minute-cadence queue backups) — so this isn't a compatibility patch, it's an
   opportunity to redesign the app's shutdown story on top of a shutdown system that didn't exist when
   the original logic was written.

Investigating the shutdown redesign surfaced two more independent, high-value threads:

3. **aeth_ext TODO.md item #6** (`extract_details_callable` standardization) — `scheduled-invoice-processor`
   is the only real production caller of this parameter (`err_handling.py`'s
   `_is_database_origin_exception`), making this the natural moment to design and adopt the
   standardized replacement together.
4. **FTP transfer performance.** While reasoning about how long a shutdown could safely wait for
   in-flight FTP work, a profiling spike (2026-08-13, temporary instrumentation added and removed from
   `suppliers/__init__.py`) confirmed transfers within a batch already run fully concurrently
   (16-way, thread-pool-limited — a batch of 77 files, ~5-7s per file, finished in ~36s wall time, not
   the ~460s serial sum). The real inefficiency found instead: `_transfer_file_vend_to_main` opens a
   **fresh** vendor+waiting connection (TCP + login) *per file*, not once per batch — the single
   biggest fixable cost in that ~5-7s/file figure.

## The five sub-projects

Each gets its own spec, written and implemented independently. Dependencies noted where they exist.

| # | Sub-project | Repo | Depends on | Status |
|---|---|---|---|---|
| 1 | Standardized fatal-exception-origin API (replaces `extract_details_callable`, TODO #6) | `aeth_ext` | — | **Spec written**: `aeth_ext/.claude/plans/2026-08-13-exception-trail-design.md` |
| 2 | FTP connection pooling (reuse connections across a batch instead of per-file) | `aeth_ext` | — | **Spec + implementation plan written**: `aeth_ext/.claude/plans/2026-08-13-ftp-connection-pooling-design.md` and `-plan.md`. Not yet implemented/executed. |
| 3 | Queue backups persist on every change (not just ~10min cron cadence / unreliable `__del__`) | `scheduled-invoice-processor` | — | Not yet specced. See "Sub-project #3" below for carried-forward context. |
| 4 | Shutdown lifecycle redesign (register teardown via `register_for_shutdown`, retire `main()`'s racing block and the `sleep(600)` heuristic) | `scheduled-invoice-processor` | **#3** (budget/durability reasoning assumes on-every-change persistence); benefits from but does not require #2 | Not yet specced. See "Sub-project #4" below for carried-forward context. |
| 5 | Adopt the new fatal-exception-origin API in `err_handling.py` | `scheduled-invoice-processor` | **#1** | Not yet specced. See "Sub-project #5" below for carried-forward context. |

None of #1/#2/#3 depend on each other and can be built in any order or in parallel. #4 should not
start implementation until #3 is done. #5 should not start until #1 ships a release
`scheduled-invoice-processor` can pin to.

**Deliberate choice (2026-08-13): #3, #4, and #5 are not specced yet.** Each sits downstream of #1
and/or #2, which are designed but not yet implemented — writing full specs for the downstream
sub-projects now would risk producing documents that silently drift out of sync as #1/#2's actual
implementations (inevitably) diverge in small ways from their current designs during real
implementation. Instead, this document carries forward everything already investigated for #3/#4/#5,
so a future session can resume brainstorming each one without re-deriving the groundwork — but the
actual approach/design/spec is deferred until its dependencies are real code, not just a design doc.

## Where things live

- **aeth_ext** (`d:\SFT Software Projects\aeth_ext\.claude\plans\`): specs for #1 and #2, each on its
  own branch until complete (per-repo branch, not shared with scheduled-invoice-processor's work).
- **scheduled-invoice-processor** (`d:\SFT Software Projects\ScheduledInvoiceProcessor\.claude\plans\`):
  specs for #3, #4, and #5 (not yet written — see above).
- This master plan file is duplicated verbatim in both locations and kept in sync manually — there is
  no single canonical copy.

## Key findings to carry into sub-project specs (so they aren't re-derived)

- **aeth_ext's shutdown budgets**: GRACEFUL=7s, FATAL=1s, FORCED=0s (`_BUDGETS` in
  `aeth_ext/errors/shutdown.py`), racing against Docker's default ~10s SIGKILL grace period.
- **Checkoff ordering** (`suppliers/__init__.py`): a per-key Google Sheets checkoff
  (`schedule.check_box(...)`) is queued into `DatabaseCache`'s write buffers only *after* all files
  for that key finish transferring (inside the batch's `gather()`). Interrupting mid-batch does not
  corrupt anything — it forfeits the checkoff for whichever keys hadn't finished yet, meaning that
  work is silently redone on next boot. Keys that *did* finish are already durable in
  `DatabaseCache`'s in-memory buffers before any Sheets flush happens.
- **`DatabaseCache.submit_queued_writes_to_pool`** has no persistence of its own — buffered writes
  live only in process memory until flushed to Sheets. A flush must be guaranteed
  (`required=True`-equivalent) during any shutdown that isn't itself database-origin.
- **FTP concurrency is already correct** — do not "fix" concurrency. The 16-worker
  `ThreadPoolExecutor` already runs transfers in parallel; the fixable cost is per-file connection
  setup, addressed by sub-project #2.
- **The original `sleep(600)`** (per the author, 2026-08-13) was a low-effort stand-in for two things
  that should be solved properly instead: (a) queue backups only persisting every ~10 minutes
  (sub-project #3), and (b) no real "is in-flight work done" signal, so a flat timer stood in for one.
  It is not protecting against data corruption — only against redundant re-work on restart.
- **FTP shutdown-wait strategy conclusion (2026-08-13, resolves an earlier open question)**: a
  bounded wait for in-flight FTP work during shutdown is NOT a productive lever, given real per-file
  transfer times (~5-7s each, confirmed by the profiling spike, and this is *before* sub-project #2's
  pooling fix — even after pooling removes the ~1-3s handshake cost, actual data-transfer time will
  still often approach or exceed the GRACEFUL budget for anything but small files). Waiting long
  enough to reliably catch an in-flight transfer eats most/all of the 7s GRACEFUL budget by itself,
  leaving little room for the guaranteed Sheets flush. Conclusion: sub-project #4 should NOT build a
  bounded-wait-on-FTP mechanism as originally sketched — instead lean entirely on sub-project #3
  (durable-on-every-change queue state) to make abandoning in-flight FTP work cheap to recover from,
  and treat the guaranteed DatabaseCache flush as the only thing shutdown actively waits on.

## Sub-project #3: queue backups persist on every change

**Not yet specced — brainstorm from scratch when picked up, using this as a starting point, not a
locked design.**

**Problem**: `SupplierProcessorBase._save_backups()` (`suppliers/__init__.py`) only runs (a) on a
10-minute cron tick (`save_queue_backups_off_thread`, scheduled in `startup.py`) and (b) in `__del__`
(unreliable — CPython doesn't guarantee `__del__` runs at interpreter exit for objects still
referenced by module-level singletons, and it calls the *synchronous* `_save_backups()` directly,
racing the `_lock` — an `aiologic.Lock`, usable cross-thread/cross-loop — against any concurrently
running async holder of that lock). Up to 10 minutes of queue-state changes (new pickups/dropoffs
registered, files matched/transferred, moved between `_file_pickup_queue`/`_file_waiting_queue`/
`_file_preprocess_queue`/`_file_dropoff_queue`) can be lost on an abrupt shutdown.

**Direction discussed but not committed**: persist on every mutation instead of on a timer. The four
queue dicts are mutated at several call sites across `_register_pickup`, `_register_dropoff`,
`_pickup_files`, `_preprocess_files`, `_dropoff_files`, `clean_stale_queue_entries` — all already
under `self._lock` (`async with self._lock` or `with self._lock` depending on sync/async context).
Open questions for whoever picks this up: does "every change" mean a full `_save_backups()` call after
every single dict mutation (simplest, but re-serializes and rewrites all 4 files on every tiny change
— check whether that's cheap enough given `pydantic`'s `dump_json` + full file rewrite), or a debounced/
batched write (more complex, reintroduces a small window of loss); should the four separate JSON files
remain separate or could a single combined write reduce I/O; does this want to move off the `to_thread`
dispatch pattern `save_queue_backups_off_thread` uses today, or keep it.

**Not blocked on anything.** Can be specced and implemented independently of #1/#2/#4/#5.

## Sub-project #4: shutdown lifecycle redesign

**Not yet specced — brainstorm from scratch when picked up.** Depends on #3 being implemented first
(not just specced) since its budget/durability reasoning assumes queue backups are already durable
on every change, not just every 10 minutes.

**Core shape already validated during investigation** (see "Key findings" above and the FTP
shutdown-wait conclusion): register scheduler-pause, queue-backup-flush (now redundant with #3's
always-current backups, so may become a no-op or a thin confirmation step rather than real work),
and `DatabaseCache.submit_queued_writes_to_pool` as `register_for_shutdown` THREADED-phase callbacks
(default priority, running before aeth_ext's own `LOGGING_TRANSPORT_PRIORITY=1000` transport
teardown). `main()`'s current post-`await SHUTDOWN` block (scheduler pause, `sleep(600)`, Sheets
flush, `sys.exit(1)`) is retired — `await SHUTDOWN` becomes a signal to stop scheduling new work, not
a place to drive teardown, since that teardown now lives in registered callbacks that aeth_ext's
ladder times/budgets/coordinates directly instead of racing it on the same thread.

**Do NOT build a bounded-wait-on-FTP mechanism** — see the FTP shutdown-wait conclusion above; this
was seriously considered and rejected based on real transfer-time data.

**The `sleep(600)`/`.errored` heuristic needs its own fresh discussion when this is picked up** — the
original author's reasoning (captured 2026-08-13) was that it existed mainly to give the scheduler
time for other non-errored suppliers' in-flight cron jobs to "trickle down" naturally, as a
low-effort substitute for real per-processor in-flight-work tracking, not because interrupting
mid-transfer is unsafe (it isn't — see the checkoff-ordering finding above). Once #3 lands, most of
the original motivation (protecting unpersisted queue state) evaporates; whether *any* replacement
wait is still warranted (e.g. for a different reason not yet identified) should be re-litigated fresh
rather than assumed away.

**Also unresolved, needs investigation when picked up**: `OrderProcessingScheduler.shutdown(wait=False)`
does not cancel or await in-flight jobs (asyncio tasks / `run_in_executor` futures already dispatched
via `CustomAsyncIOExecutor._do_submit_job`) — they become orphaned if the event loop is then closed.
Whether the shutdown redesign needs to explicitly gather/cancel `_pending_futures` with a bounded
timeout, versus accepting orphaned in-flight jobs as an acceptable cost (consistent with the
FTP-abandonment conclusion above), was flagged during investigation but never resolved.

## Sub-project #5: adopt the new fatal-exception-origin API

**Not yet specced — brainstorm from scratch when picked up.** Depends on #1 shipping a real aeth_ext
release `scheduled-invoice-processor` can pin to (not just a design doc).

**Sketch already validated against #1's design** (see #1's spec, "Consumer migration" section, for
the authoritative version — this is a summary, re-check against whatever #1 actually ships since its
implementation may have shifted details): `scheduled_invoice_processor/err_handling.py`'s
`_is_database_origin_exception` (hand-rolled frame-walking + path-string marker matching against
`_DATABASE_FATAL_PATH_MARKERS = ("\\src\\database\\", "/gspread/", "/google/oauth2/", ...)`) is
deleted entirely. `extract_exc_details` is rewritten to use aeth_ext's new
`extract_trail_callable` parameter (replacing `extract_details_callable`) on
`handle_fatal_exc_sync` (used by `scheduler_config.py`'s `CustomAsyncIOExecutor`), receiving a
standardized `ExceptionTrail` instead of the raw exception, and calling `.matches(...)` with
module-glob patterns translated from the old path markers (e.g.
`"scheduled_invoice_processor.database"`, `"**.gspread.**"`, `"**.google.oauth2.**"`) instead of
manual path-substring checks. The `_last_fatal_details` module-level dict and `get_last_fatal_details()`
stay exactly as they are — that's app-owned plumbing #1 deliberately does not replace (see #1's
"Non-goals").

**Worth checking when picked up**: whether `scheduled-invoice-processor` should also register a
`get_current_fatal_trail()`-based read somewhere in `startup.py`'s shutdown-adjacent code (once #4
lands) as a second, non-callback consumer of the trail, now that aeth_ext exposes that retrieval path
— this wasn't concretely planned during investigation, just noted as newly possible.
