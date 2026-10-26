# aeth_ext changes for the timeclock (10.0.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking. **Implementers run at high effort** (dispatch each task's subagent with `effort: "high"`):
> aeth_ext is shared by other projects, so a wrong guess here breaks more than the timeclock.

**Goal:** Land the seven aeth_ext changes the timeclock needs (bounded alert sends, `INSTANCE_ID`, optional
log server at startup, non-blocking socket logging, config rejection without shutdown, global crash hooks,
the log server as a WireGuard spoke) and release them as aeth_ext 10.0.0.

**Architecture:** Each change is local to one or two modules: `errors/` for alerts and crash hooks,
`settings.py` and `logging/setup.py` for the new switches, `central_log_server/client/` for the socket
handler and its history buffer. The socket handler keeps its history buffer as the queue: `emit` records
and rings a doorbell, and a daemon sender thread owns the socket.

**Tech Stack:** Python 3.14, uv, pydantic-settings, stdlib `logging`/`socket`/`threading`/`queue`, Rich,
pytest. Release through devkit's `poe release`.

**Spec:** `D:\SFT Software Projects\SFT Workspace\timeclock-system\docs\superpowers\specs\2026-10-26-timeclock-design.md`
§10 (and R16 in `2026-10-23-timeclock-requirements.md` beside it). Read §10 before any task; this plan
argues from it and doesn't repeat its reasoning.

## Global Constraints

- Work on a branch `feat/timeclock-changes` off `main`. The first commit adds this plan file. Stage explicit
  paths only, never `git add -A`.
- Read `AGENTS.md` and `.claude/CLAUDE.md` first. Binding: run everything under `uv run`; 2-space indent,
  line length 135; Google-style docstrings with no RST roles (`errors/shutdown.py` is the reference); dense
  comments that carry only reasoning; no `from __future__ import annotations`; pydantic dataclasses inherit
  `aeth_ext.types.IsPydantic`; PEP 758 `except A, B:` is valid; no helper of 4 lines or fewer unless reused
  enough to earn it; Conventional Commits with the module as scope.
- Tests: on the branch run only the task's targeted tests (`uv run pytest <path>::<name> -p no:cacheprovider`
  is fine), plus `uv run ruff check <files>` and `uv run pyright <files>` for what the task touched. One full
  `uv run pytest` runs in Task 9 only. Tests don't define intent: rewrite or delete any test a change makes
  obsolete.
- Anything that trips the one-shot global `SHUTDOWN` or needs `__debug__ == False` is tested through an `-O`
  subprocess, as `tests/errors/_optimized_scenarios.py` (+ `test_err_handling.py::_run_optimized`) already
  does. Don't trip `SHUTDOWN` in-process.
- New settings are read from the environment only: aeth_ext's module-level settings objects are built at
  import, before an app's Settings subclass exists (TODO #18).
- Socket-client parameters come from ALL-CAPS constants in the app's `__main__.py` (`logging/init.py:69-130`),
  so every new parameter of `configure_shared_socket_logging_client` has a default. A new
  `socket_client.toml` `runtime://` key is registered in **both** `configure_shared_socket_logging_client`
  (`logging/setup.py:616-619`) and `get_default_socket_logging_configs` (`:507-510`), or `resolve` raises.
- Nothing reachable from `HandshakeSocketHandler`'s delivery path logs through `logging`; transport failures
  go to `emergency_diagnostic` (class docstring of `HandshakeSocketHandler`, `client/__init__.py:157-225`).
- One release for everything: 10.0.0 (`major`), because the crash hooks are on by default for every
  consumer (R16.6).

## Review Focus

1. **A log-server outage of several days.** On reconnect, every record retained in history (14 days on the
   clock) reaches the server exactly once, in id order. Pinned in Task 4
   (`test_find_after_spans_a_multi_day_outage`) and Task 5
   (`test_reconnect_delivers_every_record_once_in_order`).
2. **Logging while another log call on the same thread is mid-`emit`** (a signal handler that logs, or a
   record logged from inside the transport). No deadlock, nothing lost. Pinned in Task 5
   (`test_reentrant_emit_on_the_same_thread_neither_blocks_nor_sends`).
3. **An alert send whose network call hangs, DNS included.** `alert()` returns by its deadline. Pinned in
   Task 1 (`test_alert_returns_by_the_deadline_when_a_sender_hangs`).
4. **A log server that rejects the config on every reconnect, or accepts it and then fails to apply it.**
   With the opt-out, one alert per excursion, never one per reconnect. Pinned in Task 6
   (`test_apply_failure_loop_alerts_once`).
5. **Shutting down while the log server is stalled** (accepts the connection, never reads). `close()` and
   the shutdown pass return within their bounds. Pinned in Task 5
   (`test_close_is_bounded_when_the_server_stalls`).

## File Structure

| File | Change |
| --- | --- |
| `src/aeth_ext/settings.py` | `alerts_send_timeout_secs`, `instance_id` |
| `src/aeth_ext/utils.py` | `batch_send_emails(timeout=...)` |
| `src/aeth_ext/errors/send_alert_push.py` | timeout from the setting |
| `src/aeth_ext/errors/err_handling.py` | bounded `alert()`, `wait`/`exc` params, instance suffix, exception-explicit traceback |
| `src/aeth_ext/errors/traceback_image.py` | `render_exception_image(exc)` |
| `src/aeth_ext/errors/crash_hooks.py` (new) | `install_crash_hooks()`, `install_loop_exception_handler()` |
| `src/aeth_ext/errors/__init__.py`, `src/aeth_ext/__init__.py` | export, `initialize(install_crash_hooks=True)` |
| `src/aeth_ext/logging/setup.py` | `require_log_server`, `shutdown_on_log_rejection`, `program_name` registrations |
| `src/aeth_ext/logging/config/defaults/socket_client.toml` | `program_name`, `shutdown_on_rejection` keys |
| `src/aeth_ext/central_log_server/client/config_provider.py` | instance suffix on `program_name` |
| `src/aeth_ext/central_log_server/client/history.py` | `find_after` / `_search_disk` fixes |
| `src/aeth_ext/central_log_server/client/__init__.py` | sender thread, rejection opt-out, `read_server_message_sync` returns `ApplySuccess` |
| `pyproject.toml`, `docker/compose.yaml`, `docker/Dockerfile` | the log server as a spoke |
| Tests | `tests/errors/`, `tests/logging/test_setup.py`, `tests/central_log_server/test_client*.py`, `tests/test_settings.py`, `tests/test_public_api_exports.py` |

---

### Task 1: Bounded alert sends (§10.1, R16.1)

**Files:**
- Modify: `src/aeth_ext/settings.py` (beside the alerts settings, `:58-75`)
- Modify: `src/aeth_ext/utils.py:196-244` (`batch_send_emails`)
- Modify: `src/aeth_ext/errors/send_alert_push.py:24,100`
- Modify: `src/aeth_ext/errors/err_handling.py:88-109` (`alert`)
- Modify: `src/aeth_ext/central_log_server/client/history.py` (`_maintain`'s over-budget `alert(...)` call)
- Test: `tests/errors/test_err_handling.py`, `tests/errors/test_send_alert_push.py`, `tests/test_utils.py`,
  `tests/central_log_server/test_client_history.py`

**Interfaces:**
- Produces: `BaseSettings.alerts_send_timeout_secs: float`, alias `ALERTS_SEND_TIMEOUT_SECS`, default `10.0`,
  `gt=0`.
- Produces: `batch_send_emails(email_messages, smtp_server=None, smtp_port=None, smtp_user=None,
  smtp_password=None, timeout: float | None = None) -> None`; `None` means the setting. Passed as
  `SMTP(smtp_server, smtp_port, timeout=...)`.
- Produces: `alert(reason, details, *, priority=0, in_except_block=True, force=False, wait: bool = True) -> None`.
  The subject/body and the traceback image are built on the caller's thread (the image needs the caller's
  `sys.exc_info()`); both sends then run on one daemon thread named `aeth-ext-alert`. `wait=True` joins it
  for at most `2 * alerts_send_timeout_secs + 1` seconds and returns either way; `wait=False` returns at once.
- No pre-send network check (R16.1, dropped by the owner).

- [ ] **Step 1: Write the failing tests**
  - `test_alert_returns_by_the_deadline_when_a_sender_hangs`: monkeypatch `err_handling.SETTINGS.alerts_send_timeout_secs`
    to `0.2` and `err_handling.send_alert_email` to block on a `threading.Event` that the test never sets;
    `alert("r", "d", in_except_block=False, force=True)` returns in under `0.2 * 2 + 1 + 0.5` s.
  - `test_alert_wait_false_returns_immediately`: same hanging sender, `wait=False`, returns in under 0.2 s, and the
    sender was called (poll briefly for the call).
  - `test_alert_still_sends_both_channels`: with fast fake senders, both are called once with the same subject.
  - `test_batch_send_emails_passes_the_timeout_to_smtp`: monkeypatch `utils.SMTP` with a fake context manager
    recording its kwargs; default call → `timeout == SETTINGS.alerts_send_timeout_secs`; `timeout=3` → `3`.
  - `test_push_uses_the_send_timeout_setting`: monkeypatch `send_alert_push.urlopen`, set the Pushover
    credentials; the recorded `timeout` equals the setting.
  - `test_over_budget_alert_does_not_wait`: drive `_maintain` over budget (existing tests in
    `test_client_history.py` show how) with `history.alert` monkeypatched; it was called with `wait=False`.
- [ ] **Step 2: Run them; they fail** (`uv run pytest tests/errors/test_err_handling.py tests/errors/test_send_alert_push.py tests/test_utils.py tests/central_log_server/test_client_history.py -k "deadline or wait_false or both_channels or passes_the_timeout or send_timeout or over_budget_alert_does_not_wait"`).
- [ ] **Step 3: Implement** the setting, the `timeout` parameter, the Pushover change (drop `_REQUEST_TIMEOUT_SECS`),
  `alert(wait=...)` with the daemon thread, and `wait=False` in `_maintain`. `_maintain` runs under the
  history lock inside `emit`, so it must never wait on the network.
- [ ] **Step 4: Run the tests; they pass**, plus `ruff check` and `pyright` on the touched files.
- [ ] **Step 5: Commit** `feat(errors): bound alert sends with a timeout and a send deadline`.

### Task 2: `INSTANCE_ID` (§10.2, R16.2)

**Files:**
- Modify: `src/aeth_ext/settings.py`
- Modify: `src/aeth_ext/errors/err_handling.py:55-79` (`_resolve_program_name`)
- Modify: `src/aeth_ext/logging/setup.py:507` and `:616` (registrations), `src/aeth_ext/logging/config/defaults/socket_client.toml:14`
- Modify: `src/aeth_ext/central_log_server/client/config_provider.py:246`
- Test: `tests/test_settings.py`, `tests/errors/test_err_handling.py`, `tests/logging/test_setup.py`,
  `tests/central_log_server/test_client_config_provider.py`

**Interfaces:**
- Produces: `BaseSettings.instance_id: str | None`, alias `INSTANCE_ID`, `Field(pattern=r"^[A-Za-z0-9_-]+$")`,
  default `None`. Empty is unset (`env_ignore_empty=True` already). A bad value fails at `import aeth_ext`
  with pydantic's error.
- The program name, everywhere below: `f"{name}-{settings.instance_id}" if settings.instance_id else name`.
  Written inline at each site (a one-line expression doesn't earn a helper, `AGENTS.md`).
- Produces: runtime-registry key `program_name` (the suffixed name), registered next to `project_name` in
  both `get_default_socket_logging_configs` and `configure_shared_socket_logging_client`.
  `socket_client.toml`'s handler reads `program_name = "runtime://program_name"`.
- Unchanged: the registered `project_name` (console fragment), `cls.logging_file_name` and remote log file
  names (they sit inside the per-instance server folder), `TaggedLogRecord.project_name` (its `libname`
  matching compares it to path parts, `logging/bases.py:244`).

- [ ] **Step 1: Write the failing tests**
  - `test_instance_id_rejects_a_path_separator`: env `INSTANCE_ID=a/b` → `BaseSettings()` raises `ValidationError`
    (follow `tests/test_settings.py`'s existing env setup for the required `ALERTS_EMAIL_PWD`).
  - `test_instance_id_empty_is_unset`: `INSTANCE_ID=""` → `instance_id is None`.
  - `test_alert_subject_carries_the_instance_suffix`: monkeypatch `err_handling.SETTINGS.instance_id = "3"`,
    `_resolve_program_name.cache_clear()`, fake senders; `alert(..., force=True)` subject contains `[<project>-3]`.
  - `test_socket_program_name_carries_the_instance_suffix`: with `settings.instance_id = "front-desk"`, after
    `get_default_socket_logging_configs(...)`, `runtime_registry.resolve("program_name") == "<project>-front-desk"`
    and `resolve("project_name") == "<project>"`.
  - `test_query_configs_program_name_carries_the_instance_suffix` (config provider).
  - `test_tagged_record_project_name_is_never_suffixed`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement** the setting and the four sites.
- [ ] **Step 4: Run the tests; they pass**, plus `ruff check` and `pyright` on the touched files.
- [ ] **Step 5: Commit** `feat(settings): suffix the program name with INSTANCE_ID`.

### Task 3: Optional log server at startup (§10.3, R16.3)

**Files:**
- Modify: `src/aeth_ext/logging/setup.py:585-614` (`configure_shared_socket_logging_client`)
- Test: `tests/logging/test_setup.py`

**Interfaces:**
- Produces: `configure_shared_socket_logging_client(cls, project_name, rich_console, host=None, port=None,
  remote_config=None, testing=False, require_log_server: bool = True)`. `False` skips the `RuntimeError`
  after a failed `_probe_socket_connection`; everything after it (registry, `apply()`, `connect_and_verify()`)
  runs as normal and the handler buffers. Apps opt out with `REQUIRE_LOG_SERVER = False` in `__main__.py`.
- No retry and no default flip (TODO #14's extras aren't approved).

- [ ] **Step 1: Write the failing tests** with `_probe_socket_connection` monkeypatched to return `False`
  (and the handler's connect mocked the way the module's existing tests do):
  `test_unreachable_log_server_is_fatal_by_default` (raises `RuntimeError`, as today) and
  `test_unreachable_log_server_is_allowed_when_not_required` (no raise; `logging.getHandlerByName("socket")`
  is a `HandshakeSocketHandler`).
- [ ] **Step 2: Run them; the second fails.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests; they pass**, plus `ruff check` and `pyright`.
- [ ] **Step 5: Commit** `feat(logging): let a client start without the log server`.

### Task 4: History lookup fixes (§10.4, R16.4)

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/history.py:486-539` (`find_after`, `_search_disk`)
- Test: `tests/central_log_server/test_client_history.py`

**Interfaces:**
- Produces: `RecordHistoryBuffer.find_after(last_id: int | None, hint_created: float | None) ->
  tuple[HistoryEntry, ...] | None`, same signature and contract as today, callable from any thread while
  other threads `append`. Task 5's sender calls it on every doorbell ring.
- Ids are contiguous per buffer (`RecordDurability.record` assigns `_next_id += 1` and appends every entry),
  which the fast path relies on.

Algorithm (the one place this plan fixes the body, because the locking is the point):

1. Under `self._lock`, take `snapshot = tuple(self._entries)` and release the lock. Never hold `_lock` across
   file IO: `append` runs inside `emit`, and Task 5 promises `emit` never blocks.
2. `last_id` is not `None` and `snapshot` is non-empty:
   - `snapshot[0].id <= last_id + 1` → return the snapshot entries with `id > last_id`, with no disk read
     (the common case right after a spill);
   - `last_id >= snapshot[-1].id` → return `()`.
3. Otherwise scan disk without the lock: `_search_disk(last_id, hint_created, stop_before=snapshot[0].id if
   snapshot else None)`, returning `None` (an unrecoverable gap) exactly as today when `last_id` is a real id
   it can't place; then return `(*disk, *snapshot entries with id > last_id)`.
4. `_search_disk(last_id: int | None, hint_created: float | None, stop_before: int | None)` walks every
   managed history file (`_managed_history_files(self.history_dir)`) dated from the hint's date minus one day
   onward, in date order; with no hint, or `last_id is None`, every retained file. It stops at the first entry
   with `id >= stop_before`, which is what keeps a spill racing between steps 1 and 3 from duplicating
   entries. For `last_id is None` it returns every entry before `stop_before`; otherwise it keeps today's
   resume rule (verbatim match, or the first newer entry). A partial line from a concurrent append is skipped
   by `iter_entries` already.

- [ ] **Step 1: Write the failing tests**
  - `test_find_after_spans_a_multi_day_outage`: entries written to day files five days apart (create
    `HistoryEntry`s with `created` on those days and flush them, as the module's tests already build
    entries); `find_after(id_on_day_1, created_on_day_1)` returns every later entry across all five files,
    in id order. Fails today (±1-day window).
  - `test_find_after_none_returns_disk_and_memory`: spilled entries plus newer in-memory ones; `find_after(None, None)`
    returns all of them, in order, without duplicates.
  - `test_contiguous_memory_skips_the_disk`: spill, append two more, monkeypatch `_search_disk` to raise;
    `find_after(last_spilled_id, ...)` returns the two.
  - `test_find_after_does_not_hold_the_lock_during_disk_io`: monkeypatch the module's `iter_entries` to block on
    an `Event` during a `find_after` that needs the disk (run it on a worker thread); `buffer.append(entry)`
    on the test thread completes within 1 s; then set the event.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement** as above. Update the `find_after` / `_search_disk` docstrings (Google style; the
  "bounded practical window" wording goes).
- [ ] **Step 4: Run the module's tests** (`uv run pytest tests/central_log_server/test_client_history.py`); they
  pass, plus `ruff check` and `pyright`.
- [ ] **Step 5: Commit** `fix(central_log_server): replay every retained day and never block appends on disk reads`
  with a body naming the bug (outages over two days skipped days; the lookup ran unlocked), its cause (the
  ±1-day probe; serialization only through `emit`), and the fix.

### Task 5: Non-blocking socket logging (§10.4, R16.4)

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py:157-557` (`HandshakeSocketHandler`)
- Test: `tests/central_log_server/test_client.py`, `test_client_server_integration.py`,
  `test_client_transport_reentrancy.py`, `tests/logging/test_setup.py`

**Interfaces:**
- Consumes: Task 4's `find_after`.
- Produces (all on `HandshakeSocketHandler`):
  - `emit(record)`: the re-entrancy guard and reachability filter as today, `entry = self.record(...)`, then
    `self._doorbell.put(None)`. Nothing else: no socket, no `record_success` / `record_failure`.
    `_transmit` is removed.
  - `self._doorbell: queue.SimpleQueue[None]`: reentrant-safe; `queue.Queue.put` and `Event.set` take locks a
    signal handler could interrupt.
  - `self._sock_lock: threading.Lock`: guards `self.sock` and connection setup, shared by the sender and
    `connect_and_verify`. `emit` never takes it.
  - `self._stop: threading.Event`, and `self._sender`: a daemon `threading.Thread(target=self._run,
    name=f"log-sender-{program_name}")`, started at the end of `__init__`, so a handler that never sees
    `connect_and_verify()` still delivers.
  - `self._last_sent_created: float | None`: the hint for `find_after`, updated wherever an entry is marked
    sent, and seeded from the ack's `last_received_at`.
- `_run()`, per wake. It waits on `self._doorbell.get(timeout=1.0)`, then drains extra tokens with
  `get_nowait`, then under `_sock_lock`:
  1. `createSocket()` when `self.sock is None`. stdlib's `retryTime` gating supplies the 1→30 s backoff, so
     waking every second never spins. A fresh socket runs `_send_handshake` and its replay from the server
     ack's `last_record_id`, as today.
  2. Still no socket and records are waiting: `record_failure()`, `maybe_enter()`, next wake.
  3. Send `history.find_after(self.last_sent_id, self._last_sent_created)` in order: `sendall(makePickle(...))`,
     then `mark_sent`. On `OSError`, close and drop the socket, `emergency_diagnostic`, `record_failure()`,
     `maybe_enter()`, next wake. Anything sent: `record_success()`.
  4. After `self._stop` is set, one final pass like step 3, then return.
  - Any other exception in an iteration: `emergency_diagnostic`, sleep 1 s, continue. A logging bug must not
    become fatal through Task 7's thread hook.
- Resume after a handshake whose ack couldn't be read: if nothing was sent yet this process
  (`last_sent_id == 0`), mark everything recorded so far as sent and continue live. That's today's
  "resume-by-id skipped" behavior: replaying all of history to a server that may already hold it would
  duplicate lines, since the server doesn't dedupe (`server/id_registry.py:100-116`). Otherwise continue
  from `last_sent_id`.
- The ack-read-failure alert (`:393-400`) fires once per outage: latch `self._ack_failure_alerted`, cleared by
  the next ack that reads.
- `connect_and_verify()`: under `_sock_lock`, connects if `self.sock is None` (whichever of it and the sender
  comes first connects; the other finds the socket), then raises on a rejection as today (Task 6 adds the
  opt-out).
- `close()`, idempotent and bounded, in this order: `RecordDurability.flush(self)` (durability before network);
  `self._stop.set()`; `self._doorbell.put(None)`; `self._sender.join(timeout=_LOOP_TEARDOWN_TIMEOUT)`
  (3.0 s, `:63`); then `RecordDurability.close`, `EmergencyModeTracker.close`, `SocketHandler.close`. A
  second call finds the thread gone and every close a no-op. `_finish_shutdown` still calls it; the shutdown
  runner abandons it if it overruns the budget.
- The sender reports its own failures with `emergency_diagnostic`, never `logging` (records it logged would
  feed straight back into this handler).

- [ ] **Step 1: Write the failing tests** (`make_handler` fixture in `test_client.py`; a small in-test TCP server
  speaking the handshake protocol, like `test_client_server_integration.py`'s):
  - `test_emit_never_touches_the_socket`: monkeypatch `createSocket` to record the calling thread; after `emit`
    and a short wait, no call came from the test thread.
  - `test_emit_returns_fast_when_the_server_stalls`: a server that accepts and never reads or acks; 200 `emit`s
    take under 0.5 s in total.
  - `test_reconnect_delivers_every_record_once_in_order`: the server records received ids and drops the
    connection after 50. On the second handshake it acks `last_record_id` = the highest id it received. After
    300 records and a settle wait, its ids are exactly `1..300`.
  - `test_reentrant_emit_on_the_same_thread_neither_blocks_nor_sends`: a filter or handler hook that emits
    again from inside `emit` returns, and no send happens on the emitting thread.
  - `test_close_is_bounded_when_the_server_stalls`: `close()` returns in under 4 s with the stalled server.
  - `test_close_twice_is_safe`.
  - `test_ack_read_failure_alerts_once_per_outage`: three reconnects whose ack read fails produce one call to
    the module's `alert` (monkeypatched).
  - `test_first_connect_without_an_ack_skips_the_backlog`: records made before the first connect aren't sent
    when that connect's ack fails; later ones are.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement**, following `ThreadedQueueDrainer` (`:907-1211`) for the thread's shape and
  reusing `_send_handshake` / `_replay_backlog`. Update the class docstring where it describes delivery from
  `emit`.
- [ ] **Step 4: Fix the existing tests the design obsoletes.** `TestTransmit` goes; `TestEmitPreFilter`'s
  delivery assertion becomes "id, history and a doorbell ring"; tests that expect synchronous delivery wait for
  the sender (poll with a deadline, never a bare sleep). Then run the four test files; they pass. Run
  `ruff check` and `pyright` on the module.
- [ ] **Step 5: Commit** `feat(central_log_server): deliver socket logs from a background sender thread`.

### Task 6: Config rejection without shutdown (§10.5, R16.5)

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py` (`read_server_message_sync` `:77-106`,
  `_send_handshake`, `_watch_apply_result` `:430-447`, `connect_and_verify`, `__init__`)
- Modify: `src/aeth_ext/logging/setup.py` (both registration sites), `socket_client.toml`
- Test: `tests/central_log_server/test_client.py`, `tests/logging/test_setup.py`

**Interfaces:**
- Produces: `HandshakeSocketHandler(..., shutdown_on_rejection: bool = True)`.
- Produces: `configure_shared_socket_logging_client(..., shutdown_on_log_rejection: bool = True)` (app constant
  `SHUTDOWN_ON_LOG_REJECTION = False`), registered as `shutdown_on_log_rejection`. Also registered by
  `get_default_socket_logging_configs` from that same constant through its `parse_and_grab_constants` call,
  default `True`. `socket_client.toml` gains
  `shutdown_on_rejection = "runtime://shutdown_on_log_rejection"`.
- Produces: `read_server_message_sync(...) -> HandshakeAck | ApplySuccess | ApplyFailure | None`, so the
  watcher can see an `ApplySuccess`. Its other callers only `isinstance`-check `HandshakeAck` / `ApplyFailure`
  and are unaffected.
- Opted out, a `HandshakeAck(ok=False)` in `_send_handshake` or an `ApplyFailure` in `_watch_apply_result`:
  - sends `alert(...)` with the reason, `in_except_block=False`, only while `self._rejection_alerted` is
    false, then sets it;
  - no `trigger_shutdown`;
  - drops the connection: the watcher runs on its own thread, so it calls `sock.shutdown(SHUT_RDWR)` and lets
    the sender's next send fail into its normal reconnect;
  - leaves records in history.
- An `ApplySuccess` clears `_rejection_alerted`, not an ok ack: an ok-ack-then-`ApplyFailure` loop would
  otherwise alert on every reconnect.
- `connect_and_verify()` raises its `RuntimeError` on a rejection only when `shutdown_on_rejection` is true.
- Default (`True`): behavior unchanged. The drainers' rejection paths are unchanged.

- [ ] **Step 1: Write the failing tests** (the module's `alert` and `trigger_shutdown` monkeypatched to record calls):
  - `test_rejection_opted_out_alerts_once_and_keeps_running`: three rejecting handshakes → one alert, no
    `trigger_shutdown`, the records still in history.
  - `test_apply_failure_loop_alerts_once`: ok ack then `ApplyFailure`, three times → one alert.
  - `test_apply_success_rearms_the_rejection_alert`: rejection, then `ApplySuccess`, then rejection → two alerts.
  - `test_rejection_still_shuts_down_by_default`: `trigger_shutdown` called, as today.
  - `test_connect_and_verify_does_not_raise_when_opted_out`.
  - `test_read_server_message_sync_returns_apply_success`.
  - `test_shutdown_on_log_rejection_reaches_the_handler`: after `get_default_socket_logging_configs` with the
    constant `False`, `runtime_registry.resolve("shutdown_on_log_rejection") is False`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests; they pass**, plus `ruff check` and `pyright`.
- [ ] **Step 5: Commit** `feat(central_log_server): opt out of shutting down on a rejected logging config`.

### Task 7: Global crash hooks (§10.6, R16.6)

**Files:**
- Create: `src/aeth_ext/errors/crash_hooks.py`
- Modify: `src/aeth_ext/errors/err_handling.py:88-151` (`alert`, `_extract_rich_traceback`, `_handle_fatal`,
  `alert_exception`)
- Modify: `src/aeth_ext/errors/traceback_image.py:86-120` (`render_exception_image`)
- Modify: `src/aeth_ext/errors/__init__.py`, `src/aeth_ext/__init__.py:63-134` (both overloads and the body)
- Test: `tests/errors/_optimized_scenarios.py`, `tests/errors/test_err_handling.py`,
  `tests/errors/test_traceback_image.py`, `tests/test_init.py`, `tests/test_public_api_exports.py`

**Interfaces:**
- Produces: `_extract_rich_traceback(exc: BaseException) -> str` and `render_exception_image(exc: BaseException |
  None = None) -> bytes | None`. Both render from the given exception, with Rich's
  `Traceback.from_exception(type(exc), exc, exc.__traceback__, show_locals=True, ...)`. `None` means
  `sys.exception()`, and no exception means `None` for the image. Inside `sys.excepthook`, and for a task
  exception reported from `__del__`, `sys.exc_info()` is empty, which is why the exception is passed.
- Produces: `alert(..., exc: BaseException | None = None)`, which renders the image from `exc` when given.
  `_handle_fatal` and `alert_exception` pass their `exc` through.
- Produces, in `crash_hooks.py`:
  - `install_crash_hooks() -> None`: a no-op under `__debug__`; sets `sys.excepthook` and
    `threading.excepthook`.
  - `install_loop_exception_handler() -> None`: a no-op under `__debug__`; calls
    `asyncio.get_running_loop().set_exception_handler(...)`, since uvicorn creates its own loop.
  - Hook rules:
    - `KeyboardInterrupt` returns silently (aeth_ext's exit nudge).
    - `threading.excepthook` also ignores `SystemExit`.
    - Everything is quiet once `SHUTDOWN.is_set()`. That also covers exceptions a decorator already reported,
      because `_handle_fatal` always starts the shutdown (`err_handling.py:151`).
    - Anything else goes to `_handle_fatal(label, exc)`, with labels `"uncaught exception in the main thread"`
      and `f"uncaught exception in thread {name}"`.
    - The loop handler: a context with `exception` → `_handle_fatal(f"unhandled exception in the event loop:
      {message}", exc)`; without one → `logger.error` with the context's message.
    - No chaining to Python's default hooks; nothing written to stderr.
- Produces: `initialize(..., install_crash_hooks: bool = True)` in both overloads and the body. It installs
  after the logging step (Rich's `install()` replaces `sys.excepthook`, `logging/setup.py:625`), calling
  `crash_hooks.install_crash_hooks()` through the module so the parameter doesn't shadow the function.
  `aeth_ext.errors` exports `install_loop_exception_handler`.

- [ ] **Step 1: Write the failing tests.** New `-O` scenarios in `_optimized_scenarios.py`, run through
  `_run_optimized`. Update the harness stub to `err_handling.render_exception_image = lambda exc=None: None`.
  - `uncaught_main_thread_exception_alerts_and_shuts_down`: `install_crash_hooks()`, then call `sys.excepthook`
    with a caught exception's triple → one email, one push, `SHUTDOWN.kind == "FATAL"`.
  - `uncaught_thread_exception_alerts_and_shuts_down`: a thread that raises → same counts.
  - `thread_system_exit_is_ignored` and `keyboard_interrupt_is_silent` → zero alerts, `SHUTDOWN` unset.
  - `hooks_are_quiet_once_shutdown_started`: `run_shutdown(GRACEFUL)` first, then a thread exception → zero alerts.
  - `loop_exception_context_is_fatal` and `loop_message_only_context_logs_an_error` (the second asserts no alert,
    `SHUTDOWN` unset).
  - In `test_err_handling.py`, the main-thread scenario's subprocess `stderr` is empty.
  - In `test_traceback_image.py`: `render_exception_image(exc)` outside any `except` block returns PNG bytes.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.** `crash_hooks.py` imports `_handle_fatal` with
  `# pyright: ignore[reportPrivateUsage]`, as `err_handling.py` does for `shutdown`.
- [ ] **Step 4: Run the touched test files; they pass**, plus `ruff check` and `pyright`.
- [ ] **Step 5: Commit** `feat(errors): make uncaught exceptions anywhere fatal through global crash hooks`.

### Task 8: The log server as a WireGuard spoke (§10.7, R16.7, R22)

**Depends on:** the hub row for the log server (the timeclock's wireguard-hub plan, spec §12) and, ideally,
the devkit-container release with spec §11.1–§11.2. Without §11.2, the log server's Docker health shows
unhealthy while the hub is down; that's cosmetic, since nothing restarts on unhealthy.

**Files:**
- Modify: `pyproject.toml:115` (`wireguard = true`)
- Re-rendered by setup-project: `docker/Dockerfile`, `docker/compose.yaml`
- Modify by hand after the render: `docker/compose.yaml`

- [ ] **Step 1:** Bump the locked devkit-container to the release with §11.1–§11.2 if it exists
  (`uv lock --upgrade-package devkit-container`), set `wireguard = true`, and commit
  `chore(docker): run the log server as a WireGuard spoke` (setup-project refuses uncommitted `[tool.docker]` changes).
- [ ] **Step 2:** `uv run poe setup-project --dry-run`; read the proposed changes. Expect: the Dockerfile
  replaced by the current template (today's is the outdated 2.0.1 one), compose gaining the `WG_PRIVATE_KEY`,
  `WG_HUB_URL`, `WG_HUB_REPO`, `WG_HUB_TOKEN` entries, `cap_add: NET_ADMIN`, and the two-file healthcheck.
  Then `uv run poe setup-project -y` (it commits).
- [ ] **Step 3:** By hand in `docker/compose.yaml`: add `WG_TOLERATE_DISCONNECTED=1` (R22) and change
  `restart: on-failure:3` to `restart: unless-stopped`. Without both, a hub outage ends in devkit-container's
  give-up and the log server stays down for good, with every VPS consumer requiring it. Keep the `coolify`
  network alias `central-log-server`. Commit `chore(docker): tolerate hub outages and always restart the log server`.
- [ ] **Step 4 (owner only):** generate the log server's WireGuard key pair. The public key goes in its hub row
  (§12); `WG_PRIVATE_KEY`, `WG_HUB_URL`, `WG_HUB_REPO` and `WG_HUB_TOKEN` go in the log server's Coolify
  environment. The private key never goes in a file in any repo.
- [ ] **Step 5 (owner only, after Task 9's release):** redeploy the log server in Coolify. Its log should show
  the tunnel connected (devkit-container's boot lines), and the clocks reach it at its tunnel address.

### Task 9: Final checks, merge and release 10.0.0

- [ ] **Step 1:** Full suite once: `uv run pytest`; `uv run ruff check .`; `uv run pyright`. Fix what fails, with
  each fix committed in its task's style.
- [ ] **Step 2:** In `TODO.md`, mark #16 and #20 resolved, and #14's require-log-server and rejection halves,
  following the file's own convention for resolved items; commit (`docs(todo): ...`).
- [ ] **Step 3:** Push the branch and open a PR against `main`, its body listing §10.1–§10.7 and the breaking
  change: crash hooks on by default. End it with the attribution line the session gives.
- [ ] **Step 4 (owner only):** review, run the full suite by hand, merge.
- [ ] **Step 5:** On `main` after the merge: `uv run poe release-and-pin major`. This releases 10.0.0 to GitHub
  and SFTPyPI and pins `docker/compose.yaml`'s `GIT_TAG` to `v10.0.0`. Confirm the release workflow
  finished and that `+simple/aeth-ext/` on SFTPyPI lists 10.0.0.
