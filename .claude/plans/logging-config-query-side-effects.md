# Stop `query_logging_configs` from importing target packages unnecessarily

## Context

Investigating a production crash in a consumer app (ScheduledReportAggregator's
`TimeclockJob`) traced back to
[`config_provider.py`](../../src/aeth_ext/central_log_server/client/config_provider.py)'s
`query_logging_configs()`. That function spawns an isolated subprocess to fetch a
target package's logging config, and `_resolve_target()`
(`config_provider.py:194-262`) **unconditionally imports the target package**
before ever checking whether that import was even necessary. In production this
imported `timeclock_entry_processor` purely to check for a `get_logging_configs()`
function it doesn't define, and that import chain had an unrelated stray
`mkdir()` at module scope which crashed under the subprocess's inherited (wrong)
CWD and Docker's permission model. That specific instance was already patched at
the call-site package, but the underlying pattern in `aeth_ext` — "importing a
package to query its logging config can be broken by that package's own
unrelated import-time side effects" — can bite any future consumer the same way.
This plan reworks `aeth_ext` itself so the import only ever happens when a
target package has deliberately opted into customization.

**Design direction (agreed on with the user):** Replace the free-function
`get_logging_configs()` / `target="module:func_name"` contract (used today by
zero real production callers — confirmed via repo-wide grep across `aeth_ext`,
`ScheduledReportAggregator`, and `timeclock_entry_processor`; only test fixtures
define it) with **static subclass discovery**, reusing infrastructure that's
already proven in production:
[`CapturesSubclasses`](../../src/aeth_ext/types/subclass_capture.py) (the same
mixin `BaseSettings` uses for its own deepest-subclass resolution) and
[`find_subclasses_local`](../../src/aeth_ext/static_eval.py) (pure AST, no
import). `BaseLoggingConfig`
([logging/setup.py:185](../../src/aeth_ext/logging/setup.py#L185)) already
inherits `CapturesSubclasses` and already calls `cls.get_deepest_subclass()`
internally in `configure_logging_main()`
([logging/setup.py:378](../../src/aeth_ext/logging/setup.py#L378)) — this plan
just extends that same, already-idiomatic mechanism to the cross-package
subprocess query path, instead of inventing a new contract.

The multiprocessing subprocess itself is **kept, unconditionally, for every
call** — per the user's correction, even the no-subclass-found path calls
`BaseLoggingConfig` classmethods that mutate `aeth_ext`'s own shared runtime
logging registry (`_registry.register(...)` in `logging/setup.py`), so
isolation protects the *calling* process's own registry state regardless of
whether the target package itself gets imported. Only the *import* is made
conditional, not the subprocess.

## Design details

### `config_provider.py` — `_resolve_target` rewrite

Current flow: parse `target` into `module_path`/`func_name`, unconditionally
`importlib.import_module(module_path)`, look for `func_name` on the result,
call it if found, else fall back to
`BaseLoggingConfig.get_default_*_logging_configs(caller_file=...)` using an
already-imported (or, on failure, `_find_package_file`-resolved) `caller_file`.

New flow:

1. `target` is now always a plain dotted module path — **no more `:func_name`
   suffix support**. Drop the `rpartition(":")` parsing entirely.
2. `caller_file = _find_package_file(target)` — unchanged helper
   (`config_provider.py:166-191`), still fully static, still handles both
   regular and namespace packages, still works for both the Windows editable-path
   dependency and the Linux SFTPyPI-wheel-installed dependency (verified:
   `_find_package_file` → `importlib.util.find_spec`, and downstream
   `get_package_root` in `static_eval.py:378-415` explicitly special-cases
   `site-packages` installs to scope to just the top-level package, and
   otherwise climbs `__init__.py` parents — both paths confirmed safe for this
   workspace's dual-source `[tool.uv.sources]` setup).
3. Resolve the class to use:
   ```python
   deepest_cls = BaseLoggingConfig.get_deepest_subclass(caller_file=caller_file) if caller_file is not None else BaseLoggingConfig
   ```
   `get_deepest_subclass` (`types/subclass_capture.py:111-136`) internally calls
   `find_subclasses_local(cls, caller_file, get_package_root(caller_file))` —
   **pure AST, no import** — and only calls `.load()` (a real `import_module`)
   if a subclass was actually found. When nothing is found it returns `cls`
   (`BaseLoggingConfig`) unchanged, per its existing documented behavior. This is
   the crux of the fix: for `timeclock_entry_processor` (no subclass anywhere),
   this line performs **zero imports**, exactly matching today's fallback
   behavior but without the wasted up-front import.
   Guard against `caller_file is None` (target genuinely unresolvable) exactly
   as today's code already does — degrade to `BaseLoggingConfig` directly,
   don't call `get_deepest_subclass` with `caller_file=None` (that would search
   `config_provider.py`'s own ancestry via `get_caller_file(1)`, which is wrong
   here — it must never fall back to scanning the caller's own code).
4. `constants = parse_and_grab_constants({"PROJECT_NAME": "project_name",
   "TESTING": "testing"}, caller_file=caller_file) if caller_file is not None
   else {}` — unchanged from today.
5. `program_name`/`testing` computed exactly as today.
6. `local_config, _ = deepest_cls.get_default_main_logging_configs(caller_file=caller_file)`;
   `remote_config` via `deepest_cls.get_default_socket_logging_configs(caller_file=caller_file)`
   when `mode != "main"` else `{}` — same calls as today, just against
   `deepest_cls` instead of the hardcoded base class. Both methods are already
   `@classmethod`s designed to be called on subclasses (they're literally how
   `BaseLoggingConfig` subclassing has always worked for in-process callers).
7. Return the same 4-key `LoggingConfigResult` dict shape — no change to the
   public return contract.
8. Keep the deferred `importlib.import_module("aeth_ext.logging.setup")` trick
   to dodge the existing cycle comment (`client/__init__ → config_provider →
   setup → client/__init__`); just pull `BaseLoggingConfig` from it instead of
   aliasing it as `_base_cls`.

`_query_worker` and `query_logging_configs` themselves need no structural
changes — same spawn/pipe/timeout/exception-piping shape as today, since the
subprocess stays unconditional.

### Docstrings to update

- Module docstring (`config_provider.py:1-39`, the "Defining a config provider"
  section) — replace the `get_logging_configs()` free-function example with a
  `BaseLoggingConfig` subclass example, mirroring `BaseLoggingConfig`'s own
  docstring ("subclass this class and either adjust the class variables,
  override `modify_config`, or ship a `logging_config.toml` override file").
- `query_logging_configs`'s docstring (`config_provider.py:88-120`) — drop all
  mention of `:function_name` / "tried first" / `AttributeError`; describe the
  plain-module-path contract and point at subclassing `BaseLoggingConfig` for
  customization.

### Call-site impact

- `ScheduledReportAggregator/src/scheduled_report_aggregator/jobs/timeclock_job/__init__.py:308`
  — `AsyncioQueueDrainer(get_shared_queue(), target="timeclock_entry_processor")`
  already uses a bare module path with no `:func_name`. **No change required** —
  verify after this change lands (e.g. a quick local run against the fixed
  `aeth_ext`) that this still resolves correctly and, per the fix, no longer
  imports `timeclock_entry_processor` at all during the query.
- Re-grep both `ScheduledReportAggregator` and `timeclock_entry_processor` for
  any other `query_logging_configs`/`AsyncioQueueDrainer`/`ThreadedQueueDrainer`
  usage before merging, to confirm nothing relies on `:func_name` syntax (none
  found in the current research pass, but re-check since this is a breaking
  change to that syntax).

### Tests (`tests/central_log_server/`)

- `_config_provider_fixtures/with_default_func.py`, `with_named_func.py` — these
  test the retired free-function contract. Replace with a fixture package that
  defines a `BaseLoggingConfig` subclass (e.g. overriding
  `logging_file_name`/`logging_type` or `modify_config`) to cover "subclass
  found → its customization is applied."
- `_config_provider_fixtures/hangs_forever.py` — currently hangs inside the free
  function to test the timeout path. Replace with a fixture whose subclass
  hangs somewhere still reachable in the new flow (e.g. `modify_config`
  sleeping) so the timeout path stays covered.
- **New regression test** (the one that actually proves this bug class is
  fixed): a fixture package with a side-effectful `__init__.py`/transitively-
  imported submodule (e.g. writes a marker file, or raises if ever imported)
  and **no** `BaseLoggingConfig` subclass. Assert `query_logging_configs`
  succeeds and the marker/side-effect never fires — i.e. the target package's
  own code never actually gets imported by the "no customization" path.
- `test_client_config_provider.py`'s direct `_resolve_target` unit tests —
  update for the new signature/logic (no more explicit-function/`AttributeError`
  branch).
- `test_client_queue_drainers.py` — already monkeypatches `query_logging_configs`
  entirely; spot-check it doesn't assert anything about the old
  dict-construction path, but shouldn't need changes.

## Verification

- Run `aeth_ext`'s test suite (check `pyproject.toml` for the actual
  `poe`/`pytest` task name used in this repo) and confirm all
  `central_log_server` tests plus the new regression test pass.
- Directly re-run the same import-check used to verify the earlier bandaid fix
  in `timeclock_entry_processor` (fresh CWD, `ALERTS_EMAIL_PWD` dummy env var,
  `import timeclock_entry_processor` / simulate
  `query_logging_configs("timeclock_entry_processor")`) to confirm the target
  package is never imported by the new code path.
- No production Docker/Linux environment needed to validate this — it's a pure
  static-analysis + subprocess-isolation change, fully testable locally on
  Windows.

## Out of scope / follow-ups to flag

- Bumping `aeth_ext`'s version and updating `ScheduledReportAggregator`'s pin
  to pick up the fix is a separate step after this change is implemented and
  reviewed — not part of this plan.
