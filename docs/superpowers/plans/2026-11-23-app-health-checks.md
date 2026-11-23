# App-declared health checks (aeth-ext) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apps declare named health checks and ping them through `aeth_ext.monitoring`. aeth-ext
writes the declarations and per-check state for devkit-container. It declares a built-in
`heartbeat` check, and when no supervisor owns the ping, it applies the check rules itself.

**Architecture:** One new module, `aeth_ext/monitoring/health.py`, owns a process-wide registry
(declare, freeze on the first ping, atomic file writes) and an in-memory judgment of every check.
The heartbeat primitive `_send_heartbeat` pings the built-in check and, when unsupervised, uses
that judgment to withhold the regular ping, send stall `/fail`s and (with `__debug__` off) shut
down. `ping_healthcheck` gains an optional body.

**Tech Stack:** Python 3.14, stdlib `threading`/`json`/`urllib`, pytest (`asyncio_mode = "auto"`),
ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-11-23-app-health-checks-design.md`

## Global Constraints

- Work on branch `feat/app-health-checks`. Never merge; the user reviews the PR.
- Commits follow Conventional Commits with the module as scope (`feat(monitoring): …`), ending
  with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Commands: `uv run pytest <path>::<test>` while iterating; once at the end `uv run pytest`,
  `uv run ruff check .`, `uv run pyright`. CI runs pytest on ubuntu-latest and windows-latest.
- Style: 2-space indent, line length 135, Google docstrings with no Sphinx roles, no
  `from __future__ import annotations`, PEP 758 `except A, B:` is valid. Comments are dense and
  carry reasoning. No small single-use helpers (AGENTS.md).
- Catch only exception types this code actually meets (file writes: `OSError`); never `Exception`.
- The file contract is the spec's, verbatim: folder `log_loc_folder / "health"`, `checks.json`
  (`version` 1, `written_at`, `checks` keyed by name with `period_secs`, `grace_secs`,
  `stall_message`, `affects_ping`, `fail_ping`, `shutdown`), `state/<name>.json` (`at`,
  `failure`, `message`). Timestamps are `datetime.now(UTC).isoformat()`. Every write is atomic: a
  sibling temp file, then `Path.replace`.
- Judgment strings must match devkit-container byte for byte:
  `<name>: <stall_message> (no ping for N s)`, `<name>: no ping for N s (period P s + grace G s)`,
  `<name>: <message>`, `<name>: failure ping`.
- Names match `^[a-z0-9][a-z0-9_-]{0,63}$`; `period >= 1`, `grace >= 0`, whole seconds (`int`).
  Messages are collapsed to one line (`" ".join(m.split())`) and capped at 500 characters.
- The version stays 10.1.0 in this PR (the user's `devkit release minor` makes it 10.2.0). The
  `devkit-container>=2.4.0` floor is Task 7, after the user publishes devkit-container 2.4.0.
- `SHUTDOWN` is one-shot and `tests/conftest.py::_clear_shutdown_state` fails any in-process test
  that trips it. Stub `run_shutdown`, and cover the `__debug__`-off path in a `python -O`
  subprocess harness.

## Review Focus

1. Two threads pinging for the first time at once: `checks.json` is written exactly once, and both
   state files land.
2. A message with newlines or 2,000 characters is stored as one line of at most 500 characters.
3. With the built-in check declared, a scheduler whose interval is below a second (tests use
   0.01 s) re-declares a period of 1, not 0, so it doesn't raise.
4. `log_loc_folder` missing (a fresh local run) gets `health/state/` created; an unwritable folder
   is logged, not raised, and `checks.json` is retried on the next ping.
5. Supervised (`DEVKIT_SUPERVISED_PING=1`): the app sends no health-check pings of its own and
   never shuts itself down, but still writes every file.

---

### Task 1: The registry and `declare_health_check`

**Files:**

- Create: `src/aeth_ext/monitoring/health.py`, `tests/monitoring/test_health.py`
- Modify: `src/aeth_ext/monitoring/__init__.py` (re-export), `tests/conftest.py` (autouse
  isolation fixture), `tests/test_public_api_exports.py` (add `test_monitoring_health`)

**Interfaces:**

- Produces, in `aeth_ext.monitoring.health` (`__all__ = ["HealthCheckError", "declare_health_check", "send_health_check"]`):
  - `class HealthCheckError(RuntimeError)`: contract misuse. Its docstring says why it is fatal.
  - `class _Unset` (`__slots__ = ()`) and `_UNSET = _Unset()`, the typed-sentinel pattern of
    `ftp/pool/sftp_channel_pool.py:133-143`.
  - `def declare_health_check(name: str, *, period: int | _Unset = _UNSET, grace: int | _Unset = _UNSET, stall_message: str | None | _Unset = _UNSET, affects_ping: bool | None | _Unset = _UNSET, fail_ping: bool | None | _Unset = _UNSET, shutdown: bool | None | _Unset = _UNSET) -> None`
  - `class _Registry` with:
    - `lock: threading.Lock`
    - `declarations: dict[str, _Declaration]`
    - `frozen: bool`
    - `written_at: datetime | None`
    - `declarations_written: bool`
    - `reports: dict[str, _Report]`, the last ping per name
    - `stalled: dict[str, bool]`

    `_REGISTRY = _Registry()` is module-level. `_Declaration.as_json() -> dict[str, object]`
    returns the check's `checks.json` entry.
  - The isolation fixture `_isolate_health_registry` (autouse, root `tests/conftest.py`)
    monkeypatches `health._REGISTRY` with a fresh `_Registry()`.

- [ ] **Step 1: Write the failing tests** in `tests/monitoring/test_health.py`

```python
from aeth_ext.monitoring import health

def _decl(name: str) -> dict[str, object]:
  return health._REGISTRY.declarations[name].as_json()  # the checks.json entry for name

def test_a_new_check_takes_its_values_and_the_defaults() -> None:
  health.declare_health_check("imap", period=600, grace=120)
  assert _decl("imap") == {"period_secs": 600, "grace_secs": 120, "stall_message": None,
                           "affects_ping": False, "fail_ping": False, "shutdown": False}

def test_redeclaring_overwrites_only_what_is_passed() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP server unresponsive", fail_ping=True)
  health.declare_health_check("imap", grace=30)
  assert _decl("imap") == {"period_secs": 600, "grace_secs": 30, "stall_message": "IMAP server unresponsive",
                           "affects_ping": False, "fail_ping": True, "shutdown": False}

def test_none_voids_to_the_parameters_own_default() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="x", affects_ping=True)
  health.declare_health_check("imap", stall_message=None, affects_ping=None)
  assert (_decl("imap")["stall_message"], _decl("imap")["affects_ping"]) == (None, False)

@pytest.mark.parametrize("kwargs", [{"period": None}, {"grace": None}])
def test_period_and_grace_cannot_be_voided(kwargs: dict[str, None]) -> None:
  health.declare_health_check("imap", period=600, grace=120)
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", **kwargs)  # pyright: ignore[reportArgumentType]

@pytest.mark.parametrize("kwargs", [{}, {"period": 600}, {"grace": 120}, {"period": 0, "grace": 1}, {"period": 1, "grace": -1}])
def test_a_new_check_needs_a_valid_period_and_grace(kwargs: dict[str, int]) -> None:
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", **kwargs)

@pytest.mark.parametrize("name", ["../x", "Imap", "", "a/b", "a" * 65])
def test_invalid_names_are_refused(name: str) -> None:
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check(name, period=1, grace=0)
```

(`test_declaring_after_the_freeze_raises` belongs to Task 2, since the first ping freezes.)

- [ ] **Step 2: Run to verify they fail.** Run: `uv run pytest tests/monitoring/test_health.py`. Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement** the Task 1 interfaces. A declaration is a small frozen dataclass with
`as_json()`. A `bool` or `None` check comes before `isinstance(x, _Unset)`, so the sentinel never
reaches the stored value. Every mutation happens under the registry lock.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/monitoring/health.py src/aeth_ext/monitoring/__init__.py tests/monitoring/test_health.py tests/conftest.py tests/test_public_api_exports.py
git commit -m "feat(monitoring): declare health checks in a freezable registry"
```

### Task 2: `send_health_check` freezes the registry and writes the files

**Files:**

- Modify: `src/aeth_ext/monitoring/health.py`, `tests/monitoring/test_health.py`

**Interfaces:**

- Consumes: Task 1.
- Produces: `def send_health_check(name: str, *, failure: bool = False, message: str | None = None) -> None`
  (this task covers the file side; Task 4 adds the unsupervised `/fail`).

Tests point `BaseSettings.get_settings().log_loc_folder` at `tmp_path / "logs"` with
`monkeypatch.setattr` on the live instance (precedent: `tests/logging/test_setup.py:77-79`).

- [ ] **Step 1: Write the failing tests**
  - `test_pinging_an_undeclared_check_raises`: `pytest.raises(HealthCheckError)`, and no file written.
  - `test_the_first_ping_freezes_and_writes_the_declarations`: declare `imap` and `other`, ping
    `imap`. `checks.json` loads with `version == 1`, both checks with the Task 1 JSON shapes, and a
    `written_at` that `datetime.fromisoformat` parses with a UTC offset. `state/imap.json` has
    `failure is False`, `message is None`, `at >= written_at`. `state/other.json` doesn't exist.
  - `test_declaring_after_the_freeze_raises`, including a re-declaration of an existing name.
  - `test_a_failure_ping_stores_a_single_capped_line` (Review Focus 2): the message
    `"a\n  b\t" + "c" * 2000` is stored as `("a b " + "c" * 2000)[:500]` with `failure is True`.
  - `test_the_folders_are_created_and_write_errors_are_logged_not_raised` (Review Focus 4): with
    `log_loc_folder` missing, the files land. Then, with `Path.replace` patched to raise
    `PermissionError`, the ping doesn't raise, `caplog` holds an error, and `checks.json` is
    written on the next ping once the patch is removed.
  - `test_concurrent_first_pings_write_the_declarations_once` (Review Focus 1): patch the
    declarations writer to count calls, then start two threads that each ping a different check.
    The count is 1 and both state files exist.

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL.

- [ ] **Step 3: Implement.** Everything runs under the registry lock:
  1. Look the name up; raise `HealthCheckError` if it isn't declared.
  2. On the first ping, freeze and stamp `written_at`.
  3. If `checks.json` hasn't been written yet, write it.
  4. Write the state file with `at` taken now, which is never earlier than `written_at`.
  5. Record the report in memory for Task 4.

  Writes go through `tmp = path.with_suffix(path.suffix + ".tmp")`, `tmp.write_text(...)`, then
  `tmp.replace(path)`, under `except OSError: logger.exception(...)`.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/monitoring/health.py tests/monitoring/test_health.py
git commit -m "feat(monitoring): ping health checks and write their files for devkit-container"
```

### Task 3: `ping_healthcheck` carries a reason

**Files:**

- Modify: `src/aeth_ext/monitoring/ping.py:19-66`, `tests/monitoring/test_ping.py`

**Interfaces:**

- Produces: `def ping_healthcheck(url: SecretStr | None, *, failure: bool = False, start: bool = False, autoprovision: bool = False, body: str | None = None) -> None`

- [ ] **Step 1: Write the failing test** `test_a_body_is_posted_as_utf8`: a fake
`urlopen(url, data=None, timeout=...)` records `data`; `ping_healthcheck(SecretStr("https://hc-ping.com/uuid"), failure=True, body="imap: x")`
records `b"imap: x"` and the `/fail` URL.

- [ ] **Step 2: Run to verify it fails.** Run: `uv run pytest tests/monitoring/test_ping.py`. Expected: FAIL (`TypeError`).

- [ ] **Step 3: Implement.** Pass `data=body.encode()` only when `body` is given, so a body-less
call is exactly today's `urlopen(url, timeout=...)` and the existing fakes keep passing. Add one
docstring line: with a body the request is a POST, which healthchecks.io stores with the ping.

- [ ] **Step 4: Run to verify it passes** (the whole file). Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/monitoring/ping.py tests/monitoring/test_ping.py
git commit -m "feat(monitoring): let a healthchecks.io ping carry a body"
```

### Task 4: The rules aeth-ext applies when no supervisor owns the ping

**Files:**

- Modify: `src/aeth_ext/monitoring/health.py`, `src/aeth_ext/monitoring/heartbeat.py:38-100`,
  `src/aeth_ext/monitoring/ping.py`
- Create: `tests/monitoring/_optimized_scenarios.py` (the `python -O` harness, shaped like
  `tests/errors/_optimized_scenarios.py`)
- Test: `tests/monitoring/test_health.py`, `tests/monitoring/test_heartbeat.py`

**Interfaces:**

- Consumes: Tasks 2 and 3; `from aeth_ext.errors.shutdown import ShutdownKind, run_shutdown`.
- **Import direction.** `heartbeat.py` imports `health.py`, and `health.py` never imports
  `heartbeat.py`: pyright runs with `reportImportCycles` and `reportPrivateUsage` on. So
  `_supervised()` and `_resolve_ping_url()` move from `heartbeat.py` into `ping.py` as public
  functions, `supervised_ping() -> bool` and
  `resolve_ping_url(ping_url, pingkey, slug) -> tuple[SecretStr | None, bool]`. Their bodies are
  unchanged, and they stay out of `__all__`. `heartbeat.py` imports them from there. Update any
  test that referenced the old private names.
- Produces, in `health.py`. These are public names kept out of `__all__`, as the internal API
  `heartbeat.py` uses:
  - `class Tick(NamedTuple)`: `withheld: bool`, `alerts: list[str]`, `stop: str | None`.
  - `def heartbeat_tick(now: datetime) -> Tick`:
    1. If `heartbeat` is declared, ping it (Task 5 declares it).
    2. Judge every declared check from the in-memory reports, using the spec's rule and the
       Global Constraints strings.
    3. `withheld`: some `affects_ping` check is not healthy.
    4. `alerts`: the stall reason of each `fail_ping` check that has just become stalled.
    5. `stop`: the stall reason of the first `shutdown` check that has just become stalled.
    6. Before the freeze it returns `Tick(False, [], None)`.
- `send_health_check(failure=True)` also sends a `/fail` from a daemon thread, with the judgment
  string as its body. It does so only when `not supervised_ping()` and a URL resolves from the
  live settings (`alerts_healthcheck_ping_url`, `alerts_healthcheck_pingkey`, `heartbeat_slug`).
- `_send_heartbeat`, after the file write:
  1. Run `heartbeat_tick`.
  2. Unsupervised: skip the regular ping while `withheld`, unless it is a `failure=True` ping.
  3. Unsupervised: send each `alerts` line as a `/fail` with a body.
  4. Unsupervised, `stop` set, `__debug__` off: log an error and call
     `run_shutdown(ShutdownKind.GRACEFUL)`.
  5. Supervised: skip all of the above. The files are still written.

- [ ] **Step 1: Write the failing tests**
  - `test_heartbeat_tick_judges_with_devkit_containers_strings`: declare `imap` (period 600,
    grace 120, `stall_message` "IMAP server unresponsive"), freeze it with a ping at `t0`.
    `heartbeat_tick` at `t0 + 719 s` returns nothing. With `fail_ping` on, `t0 + 720 s` gives
    `alerts == ["imap: IMAP server unresponsive (no ping for 720 s)"]`, and the same again 60 s
    later gives no alert (one per stall episode). A ping, then a new stall, alerts again.
  - `test_affects_ping_withholds_while_failing_or_stalled`.
  - `test_unsupervised_failure_pings_post_the_reason` and
    `test_supervised_failure_pings_send_nothing` (Review Focus 5). Stub
    `health.ping_healthcheck` and wait with `tests.conftest.wait_until`, because it runs on a
    thread.
  - In `test_heartbeat.py`: `test_a_withheld_heartbeat_still_writes_the_file_but_skips_the_ping`,
    `test_a_known_failure_ping_is_never_withheld`, and
    `test_a_stalled_shutdown_check_does_not_stop_a_debug_run` (stub `run_shutdown` and assert it
    was not called).
  - The `-O` harness scenario `stalled_shutdown_check_stops_the_app` replaces `run_shutdown` with
    a recorder, declares a `shutdown` check, freezes it, and drives one `_send_heartbeat` past the
    stall. It prints `{"kinds": [1]}`. The pytest runner in `test_health.py` asserts that output.

- [ ] **Step 2: Run to verify they fail.** Run: `uv run pytest tests/monitoring`. Expected: the new tests FAIL; the existing ones still PASS.

- [ ] **Step 3: Implement** the Task 4 interfaces. The existing heartbeat-test lambdas accept only
`url, *, failure, start, autoprovision`. So the regular ping call stays exactly as today; only the
new `/fail` calls pass `body=`.

- [ ] **Step 4: Run to verify they pass.** Run: `uv run pytest tests/monitoring`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/monitoring/health.py src/aeth_ext/monitoring/heartbeat.py tests/monitoring
git commit -m "feat(monitoring): apply the health-check rules when no supervisor owns the ping"
```

### Task 5: The built-in `heartbeat` check

**Files:**

- Modify: `src/aeth_ext/__init__.py:64-145` (both overloads, the implementation and the docstring),
  `src/aeth_ext/monitoring/heartbeat.py` (`_send_heartbeat`, `run_heartbeat_async`, `HeartbeatThread.__init__`)
- Test: `tests/test_init.py`, `tests/monitoring/test_heartbeat.py`

**Interfaces:**

- Consumes: Tasks 1, 2 and 4.
- Produces:
  - `initialize(..., heartbeat_check: bool = True, ...)`, placed after `install_crash_hooks` in
    all three signatures (L71, L84, L96). With it on, `wrapped_initialize` declares
    `declare_health_check("heartbeat", period=DEFAULT_HEARTBEAT_INTERVAL_SECS, grace=120, affects_ping=True, fail_ping=True)`.
  - `health.adopt_heartbeat_interval(interval: float) -> None` (public, not in `__all__`).
    It re-declares `heartbeat` with `period=max(1, ceil(interval))`, but only while `heartbeat`
    is declared and the registry is open; otherwise it does nothing.

- [ ] **Step 1: Write the failing tests**
  - `test_initialize_declares_the_builtin_heartbeat_check` and
    `test_heartbeat_check_false_opts_out`. Follow the monkeypatch pattern in
    `tests/test_init.py:69-104`, so no logging or signal handlers are really installed.
  - In `test_heartbeat.py`, `test_each_heartbeat_pings_the_builtin_check`: declare the built-in
    as `initialize` would, call `_send_heartbeat` once, and check that `state/heartbeat.json`
    exists. Without the declaration, no health file is written, so the existing tests are untouched.
  - `test_a_custom_interval_redeclares_the_builtin_period` (Review Focus 3):
    `run_heartbeat_async(interval=0.01)` and `HeartbeatThread(interval=90.5)` leave periods of 1
    and 91 (`max(1, ceil(interval))`). Once the registry has frozen, a scheduler doesn't
    re-declare.

- [ ] **Step 2: Run to verify they fail.** Run: `uv run pytest tests/test_init.py tests/monitoring`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement.** Make the declaration the first step of `wrapped_initialize`, before
logging, and add one line to the docstring's step list. `run_heartbeat_async` and
`HeartbeatThread.__init__` call `health.adopt_heartbeat_interval(interval)`. The built-in check
is pinged by `heartbeat_tick` (Task 4).

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/__init__.py src/aeth_ext/monitoring/heartbeat.py tests/test_init.py tests/monitoring/test_heartbeat.py
git commit -m "feat(monitoring): declare a built-in heartbeat health check at startup"
```

### Task 6: Full verification, PR, two Copilot review rounds

- [ ] **Step 1:** `uv run pytest`, `uv run ruff check .`, `uv run pyright`. Expected: all clean.
- [ ] **Step 2:** Push, then `gh pr create`. In the body, say that the `devkit-container>=2.4.0`
  floor follows once 2.4.0 is published, link the spec, and end with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] **Step 3:** Request a Copilot review (GitHub MCP `request_copilot_review`). Verify each
  comment against the code (superpowers:receiving-code-review), fix what's real, answer what
  isn't, and push.
- [ ] **Step 4:** Repeat Step 3 once more (round two). Leave the PR unmerged.

### Task 7: The devkit-container floor (after the user publishes 2.4.0)

- [ ] **Step 1:** Set `pyproject.toml:13` to `"devkit-container>=2.4.0"` and run `uv lock`. The
  Linux entry must resolve 2.4.0 from SFTPyPI.
- [ ] **Step 2:** `uv run pytest tests/monitoring`; commit
  `chore(deps): require devkit-container 2.4.0, which reads health checks`; push to the PR.
