# Migration Guide: Two-Phase Logging Config & Shutdown Signalling

**Branch:** `feat/two-phase-logging-config`
**Audience:** downstream consumers of `aeth_ext` upgrading past this PR.

This PR reworks three interlocking systems: logging config application (now an explicit
validate/apply split), process-wide shutdown (`aeth_ext.errors.shutdown` replaces the old
`FATAL_EVENT` flag), and the central log server's client/server handshake (a new post-ack
apply-confirmation message).

This doc only lists what you need to *do*: things that will break on upgrade (§1), and new
capabilities you can deliberately adopt (§2). It omits internal refactors and automatic behavior
changes that require no action on your part — those live in the PR diff/commit history, not here.

---

## 1. Required Changes (breaking — fix before upgrading)

- [ ] **Replace every `from aeth_ext.errors import FATAL_EVENT`** with
  `from aeth_ext.errors.shutdown import SHUTDOWN`. API is compatible (`is_set()`,
  `wait(timeout=...)`, `await SHUTDOWN`); `SHUTDOWN` additionally has `.kind` and `.request(kind)`.
  Note the import path changed — `SHUTDOWN` is **not** re-exported from `aeth_ext.errors`.
- [ ] **Replace `FATAL_EVENT.set()`** with `SHUTDOWN.request(ShutdownKind.GRACEFUL)` (or `.FATAL`/
  `.FORCED` as appropriate, from `aeth_ext.errors.shutdown.ShutdownKind`) — `SHUTDOWN` has no bare
  `.set()`.
- [ ] **Drop any import of removed module-level functions** from `aeth_ext.logging.config`:
  `dict_config`, `json_config`, `toml_config`, `toml_to_json`, `yaml_config`, `yaml_to_json`,
  `listen`, `stop_listening`. Replace `dict_config(config, log_dir=...)` with
  `DictConfigurator(config, log_dir=...).apply()`. The socket config-listener (`listen`/
  `stop_listening`) and the `json_config`/`toml_config`/`yaml_config` source-parsing helpers have no
  direct replacement in this module — parse the source yourself and call
  `DictConfigurator(parsed).apply()`.
- [ ] **Drop any direct call to `DictConfigurator(config, manager=..., root=...)`** — the `manager`/
  `root` constructor kwargs are gone. Use `manager, root = DictConfigurator(config).apply(private=True)`
  instead (it now builds and returns its own isolated `logging.Manager`/root rather than accepting
  externally-constructed ones).
- [ ] **Drop any import of `aeth_ext.central_log_server.server.dispatch.build_hierarchy`** (removed,
  was in `__all__`) — replace with `DictConfigurator(config, log_dir=...).validate()` then
  `.apply(private=True)`.
- [ ] **If you construct `aeth_ext.central_log_server.client.history.RecordHistoryBuffer` directly**,
  add the new required first positional argument: `RecordHistoryBuffer(program_name, max_records=...,
  ...)`.
- [ ] **If any tooling reads client log-history files off disk directly** (backup scripts, log
  shippers, monitoring), update the path: history moved from a flat
  `<persisted_dir>/client_log_history/YYYY-MM-DD.jsonl` (all programs interleaved) to per-program
  `<persisted_dir>/client_log_history/<program_name>/<program_name>_YYYY-MM-DD.jsonl`. Pre-upgrade history files
  need to be moved into the new subdirectory layout manually if you need them still resumable.
- [ ] **If you have a hand-rolled central-log-server client** (i.e. you talk the wire protocol
  yourself instead of using `HandshakeSocketHandler`/`AsyncioQueueDrainer`/`ThreadedQueueDrainer`):
  it must be upgraded to expect up to one extra message after `HandshakeAck` before the first log
  record is safe to send. Use `aeth_ext.central_log_server.protocol.decode_server_message(payload)
  -> HandshakeAck | ApplySuccess | ApplyFailure | None` to dispatch on what arrives. A client that
  blindly reads exactly one JSON frame per handshake and then starts streaming records will desync
  its length-prefixed framing or misparse the extra frame as a corrupt record. **Client and server
  must be upgraded together** — this is a wire-protocol version-skew concern, not just a
  library-version concern. (If you use the built-in transport classes, this is already handled —
  no action needed.)
- [ ] **If any code constructs `central_log_server._types.RegisterClient` directly** (unlikely
  outside this package's own reader/writer): its constructor changed from
  `(program_name, manager, root, connection_id)` to
  `(program_name, configurator: DictConfigurator, connection_id, apply_result=...)`.
- [ ] **If you already call `aeth_ext.errors.shutdown.register_for_shutdown`**: every callback must now
  accept exactly one positional argument — the fatal `aeth_ext.errors.exception_trail.ExceptionTrail`
  tuple accumulated so far (empty if none yet), not zero arguments. Add the parameter:
  `def _my_teardown(self, trails: tuple[ExceptionTrail, ...]) -> None: ...`. A zero-argument
  registrant raises `TypeError` at shutdown and its teardown work is skipped.
- [ ] **If any code calls `handler.flush()` on a `HandshakeSocketHandler` expecting it to persist
  buffered records**: it doesn't (MRO resolves to `logging.Handler`'s no-op) and never has — only
  `close()` or the automatic shutdown-triggered flush does. If your code relies on manual `flush()`
  for durability, switch to `close()` or let the automatic shutdown integration handle it.
- [ ] **Audit fatal-exception handling if your production processes run under `python -O`.**
  `report_exc`/`handle_fatal_exc_sync`/`handle_fatal_exc_async` now actively drive
  `run_shutdown(ShutdownKind.FATAL)` (attempting to unwind and exit the process) instead of only
  setting a flag. This — and nearly every other alert/shutdown-triggering behavior added in this
  PR, including `aeth_ext.initialize()` installing signal handlers by default — is a no-op under the
  plain interpreter and **only activates under `python -O`** (`__debug__ is False`). If production
  doesn't run under `-O`, none of this activates yet; if it does, confirm you're ready for a fatal
  exception or a rejected/failed logging config to actively terminate the process. Opt out of the
  signal-handler installation specifically with `aeth_ext.initialize(..., install_signal_handlers=False)`.

---

## 2. Opt-In Features (adopt deliberately)

These require you to write/call something new — they don't happen automatically.

- [ ] **Register your own graceful-shutdown teardown logic**, instead of `atexit` or your own signal
  handling:
  ```python
  from aeth_ext.errors.exception_trail import ExceptionTrail
  from aeth_ext.errors.shutdown import register_for_shutdown, ShutdownPhase, LOGGING_TRANSPORT_PRIORITY

  class MyService:
      def __init__(self):
          # INTERRUPT: runs inline in the signal handler — must be instant, lock-free, no logging
          register_for_shutdown(self._arm_write_through, phase=ShutdownPhase.INTERRUPT)
          # THREADED: may block; required=True means it still runs even under FORCED's tight budget
          register_for_shutdown(self._close_and_flush, phase=ShutdownPhase.THREADED, required=True)

      # Every callback takes the fatal-trail tuple accumulated so far (empty if none yet) —
      # see the required change above if you're upgrading an existing zero-argument registrant.
      def _arm_write_through(self, trails: tuple[ExceptionTrail, ...]) -> None: ...
      def _close_and_flush(self, trails: tuple[ExceptionTrail, ...]) -> None: ...
  ```
  Use `priority=` relative to `LOGGING_TRANSPORT_PRIORITY` (1000) if your teardown needs to run
  strictly before or after `aeth_ext`'s own logging-transport shutdown (which runs after
  default-priority callbacks, so logging still works while other things tear down).
- [ ] **Call `aeth_ext.errors.alert(reason, details, *, priority=0, force=False)` directly** for
  custom out-of-band notifications (email/Pushover) not tied to a live exception. No-op under the
  default interpreter unless you pass `force=True`.
- [ ] **Call `aeth_ext.errors.trigger_shutdown(reason, details, *, kind=ShutdownKind.FATAL)`** to
  alert + drive a shutdown for a plain error condition that has no exception to raise (e.g. a
  rejected remote config, a failed health check). Pass `kind=ShutdownKind.GRACEFUL` for an orderly
  windown instead of `FATAL`.
- [ ] **Call `run_shutdown(kind)` directly** (`aeth_ext.errors.shutdown`) for any custom shutdown
  trigger not covered by the helpers above (e.g. an operator-initiated admin-endpoint shutdown).
- [ ] **Use `DictConfigurator`'s explicit two phases** if you want to validate a config (cheap, no
  I/O — e.g. to fail fast on a bad user-supplied config file) separately from applying it: call
  `.validate()` alone first, surface any validation error, then call `.apply()` only once you're
  ready to commit to it.
- [ ] **Use `protocol.decode_server_message()`** in any custom client code to react to
  `ApplyFailure` distinctly from a rejected handshake (e.g. surface "config parsed but failed to
  apply on the server: disk full" vs. "config was invalid") rather than treating both as the same
  failure mode.

---

## 3. Compatibility Notes

- Nearly every new alert/shutdown-triggering behavior in this PR is gated on `__debug__ is False`,
  i.e. **only active under `python -O`**. Keep this in mind when reading §1 and §2 above — in
  normal dev/test runs, these are silent no-ops.
- `SHUTDOWN` is process-global and one-shot (mirrors old `FATAL_EVENT` semantics: once requested it
  can't be cleared or lowered). Relevant if you test your own `register_for_shutdown`/`run_shutdown`
  integration — trip it for real only in an isolated subprocess.
