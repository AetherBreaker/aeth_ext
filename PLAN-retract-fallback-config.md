# Retract Fallback Config; Consolidate err_handling.py

**Branch:** `feat/two-phase-logging-config` — retracts the "Fallback" section (D-D1–D-D5) of
[PLAN-two-phase-logging-config.md](PLAN-two-phase-logging-config.md) and cleans up the
`errors/err_handling.py` surface along the way.

**Breaking changes are fine.** This branch is unmerged; every consumer updates when it releases.
No back-compat shims, no deprecated aliases, no dual code paths.

---

## Context

### Why the fallback is being retracted

The graceful-shutdown system (`errors/shutdown.py`) now gives the client a clean way to react to
an unusable logging config: alert the operator, then shut the host application down for manual
fixing. Given that path exists, building a *second*, degraded config server-side when the first
one fails to apply is no longer buying anything — it just delays the operator finding out, and
does so by re-implementing config construction from scratch instead of using the packaged
fragment loaders already in `aeth_ext.logging.config`. This was flagged as unresolved on the PR
(`server/dispatch.py:113`) at the time it landed: *"This should instead be done using the config
fragment loaders... This needs further discussion before changes, but absolutely must be
handled."* The discussion concluded: don't build a second config at all.

The client side of "alert then shut down" already exists and needs no new work —
`report_config_rejected`/`report_ack_read_failure` (being consolidated below) already alert and,
for a rejected/failed config, drive a fatal shutdown. Removing the fallback is almost entirely a
server-side deletion.

### Why err_handling.py is being touched in the same pass

While tracing the client-side alert/shutdown path, two more things came up:

1. Two more unresolved PR threads (`errors/err_handling.py:167`, `:205`) flagged
   `handle_config_rejected`/`handle_ack_read_failure` for living in the generic `errors` package
   and for `handle_config_rejected` calling `run_shutdown` directly instead of raising into
   existing machinery. That relocation already happened on this branch (uncommitted) —
   `report_config_rejected`/`report_ack_read_failure` now live in `client/__init__.py` and raise
   synthetically into `report_exc`/`alert_exception`. This plan finishes that job properly instead
   of leaving the synthetic-raise workaround in place.
2. A review pass of `err_handling.py` for functionality overlap (requested mid-design) found real
   duplication worth removing while everything else in the file is already being touched.

---

## Settled decisions

### F1 — Delete the fallback machinery outright, hand-reverted against the commit that added it

The fallback landed in one commit (`02b1702` / `b97d837` via the merge) bundled with unrelated
apply/shutdown-signalling work, so this is a hand-revert of just the fallback-specific hunks, not
a `git revert`. Verified against that commit's diff, file by file:

| File | Change |
| --- | --- |
| `server/dispatch.py` | Delete `build_fallback_config()`; drop it from `__all__`; drop now-unused `Literal`/`Any` TYPE_CHECKING imports if nothing else needs them |
| `server/writer_thread.py` | Delete `_apply_fallback()`. In `_register_client()`, an `apply()` exception goes straight to `event.apply_result.set_failure()` — no retry attempt, no fallback branch |
| `logging/config/models.py` | Delete `logging_type` and `disable_fallback` from `LoggingConfigModel` — both existed solely to drive fallback matching/opt-out |
| `logging/config/defaults/logging_config_schema.json` | Delete the corresponding two schema entries |
| `logging/config/defaults/remote_daily.toml` / `remote_per_run.toml` | Delete the `logging_type = "..."` line each gained |
| `protocol.py` | `ApplySuccess`/`ApplyFailure` **stay** — apply can still fail (EACCES, disk full) with no fallback attempt, so the signalling channel is still needed. Reword their docstrings to drop "or via the D-D2 fallback" / "neither it nor its D-D2 fallback" |
| `TODO.md` | Delete item 3 (fallback-degradation-not-surfaced-in-viewer) — moot with no fallback to degrade to |
| `tests/central_log_server/test_writer_thread.py` | Delete the fallback-specific test classes (daily/per_run fallback, disable_fallback opt-out, missing-logging_type, fallback-also-fails). Add/adjust one case asserting a bare `apply()` exception sets `apply_result` to failure directly, with no retry |

`log_server_root.toml`'s change in that same commit is an unrelated comment fix
(`build_hierarchy` → `DictConfigurator` wording) and is not touched.

### F2 — `err_handling.py`: dedup the fatal-path body

`report_exc`'s except-block, `handle_fatal_exc_sync`'s, and `handle_fatal_exc_async`'s each
independently do: log critical with `exc_info`, render the traceback, `_send_alerts(...,
priority=_FATAL_PUSH_PRIORITY)`, `run_shutdown(FATAL, ...)`. Extract one private helper:

```python
def _handle_fatal(label: str, exc: BaseException, *, exit_when_done: bool = False) -> None:
  """Log, alert, and drive a fatal shutdown for exc, currently being handled as label's failure."""
```

- `report_exc`'s except-block becomes: call `_handle_fatal(label, e, exit_when_done=exit_when_done)`, then `if reraise: raise`.
- `handle_fatal_exc_sync`/`_async`'s except-blocks keep their own `extract_details_callable`
  handling (unique to the decorators), then call `_handle_fatal(func.__qualname__, e)`.

No public API or behavior change — internal-only.

### F3 — `err_handling.py`: make `_send_alerts` public instead of adding a thin wrapper around it

`report_config_rejected`-style fatal handling and `report_ack_read_failure`-style non-fatal
handling currently reach `err_handling`'s machinery by synthesizing a `RuntimeError` purely to
`raise`/`except` it — `report_exc`/`alert_exception` both render from `sys.exc_info()` by
contract, so a plain condition (not a real exception) has no other way in today.

The first draft of this plan added a new `alert()` function to close that gap — but `_send_alerts`
*already* does exactly what that would do (it already takes `with_traceback`), so a second function
that only calls it would be a pure pass-through: bloat, not a fix. Instead:

- **Drop the leading underscore.** `_send_alerts` becomes the public `alert(reason, details, *, priority=0, with_traceback=True)` — same signature, same behavior, no new function. This is also the resolution to the stale unresolved-PR suggestion that `_send_alerts` be made public (`err_handling.py:205`).
- `alert_exception` and the new `_handle_fatal` (F2) call `alert(...)` internally instead of `_send_alerts(...)` — a rename at their call sites, nothing else changes.
- One new function is still needed for the fatal+shutdown case, since nothing today bundles alert-with-no-exception together with `run_shutdown`:

```python
def trigger_shutdown(reason: str, details: str, *, kind: ShutdownKind = ShutdownKind.FATAL, exit_when_done: bool = True) -> None:
  """Alert then drive a shutdown for a condition with no live exception to report."""
```

  Body: `alert(reason, details, priority=_FATAL_PUSH_PRIORITY, with_traceback=False)` then
  `run_shutdown(kind, exit_when_done=exit_when_done)`. Export `alert` and `trigger_shutdown` from
  `errors/__init__.py`.

For the non-fatal ack-read-failure case in F4, the call site uses `errors.alert(reason, details,
with_traceback=False)` directly — no dedicated wrapper for that path at all.

### F4 — Consolidate `report_config_rejected`/`report_ack_read_failure` into one function

Fold the two into a single function in `client/__init__.py`, calling the new `err_handling`
primitives directly instead of synthesizing exceptions:

```python
def report_central_log_server_failure(program_name: str, reason: str, *, fatal: bool) -> None:
  """Report a problem with the central log server connection for program_name.

  fatal=True covers a rejected handshake or an ApplyFailure: this program's
  logging can no longer be trusted, so this alerts and drives a fatal shutdown
  via errors.trigger_shutdown.

  fatal=False covers a handshake-ack read failure: alerts via errors.alert
  without shutting down, since resume-by-id degrading to live streaming is
  not fatal.
  """
```

All six call sites across `HandshakeSocketHandler`/`AsyncioQueueDrainer`/`ThreadedQueueDrainer`
(rejected-ack, `ApplyFailure`, ack-read-failure — one of each per class) switch to this one
function with the appropriate `fatal=`. No synthetic `raise` remains anywhere in this file.

### F5 — Trim the two prior PLAN-*.md docs; nothing in them is trusted as still-true

Every claim re-verified against current code before being kept.

**`PLAN-two-phase-logging-config.md`:**

- Delete the "Fallback" (D-D1–D-D5) section outright, replaced with a one-line pointer to this doc.
- Delete D-E1 (the opt-out flag) — dead now that `disable_fallback` is gone.
- Delete the entire D-G/D-H graceful-shutdown section — already marked "SUPERSEDED... do not
  implement from it" in the doc's own text; `PLAN-graceful-shutdown.md` is authoritative and this
  is pure noise.
- Collapse the "[DONE]" implementation-order list to a short completed-summary.

**`PLAN-graceful-shutdown.md`:**

- Fix D-I1: `ShutdownState` does not live in `err_handling.py` — it lives in its own
  `errors/shutdown.py` module.
- Every reference to `handle_config_rejected` is stale (renamed once already, renamed again by
  F4) — generalize to "the client-side config-rejection path" rather than naming a function that
  keeps moving.
- Collapse the implementation-order/verification sections — everything in this doc has shipped
  and matches `shutdown.py`.

---

## Implementation order

1. **Fallback deletion** (F1) — server/dispatch.py, writer_thread.py, models.py, schema json, the
   two remote TOML fragments, TODO.md item 3, test_writer_thread.py.
2. **err_handling.py dedup** (F2) — extract `_handle_fatal`, rewire the three existing callers.
3. **New primitives** (F3) — `trigger_shutdown`, `alert`, exported from `errors/__init__.py`.
4. **Client consolidation** (F4) — `report_central_log_server_failure`, rewire the six call sites,
   delete `report_config_rejected`/`report_ack_read_failure`.
5. **Plan doc trims** (F5).
6. **Test sweep** — beyond the fallback-test deletions in step 1: update
   `tests/errors/test_err_handling.py`/`_optimized_scenarios.py` for the new/renamed primitives,
   and `tests/central_log_server/test_client.py`/`_optimized_scenarios.py` for the consolidated
   client function.

## Verification

```bash
uv run pytest tests/errors tests/central_log_server
uv run ruff check .
uv run pyright
```

Per this repo's testing-cadence convention, this is a feature branch: run only the targeted
suites above while iterating, and the full `uv run pytest` once at the end, immediately before
stopping.
