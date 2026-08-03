# Redact secrets from debug/log/alert output

## Context

`aeth_ext` currently stores every credential (SMTP password, Pushover token/user
key, healthchecks.io ping key) as a plain `str` in `BaseSettings`
([settings.py](../../src/aeth_ext/settings.py)), with no redaction anywhere in the
logging or alerting pipeline. Research this session found two concrete leak
vectors:

1. **Plain-`str` credential fields** — `repr(SETTINGS)`/`str(SETTINGS)` (pydantic's
   default) prints every field, including secrets, in full. Any accidental
   `logger.debug(settings)` or crash dump exposes them. This also affects the
   central log server: `record_to_payload()`
   ([protocol.py:97](../../src/aeth_ext/central_log_server/protocol.py#L97)) copies
   the full `LogRecord.__dict__` (including any `extra=` fields a caller
   passes to a logger call), which the 5 call sites in
   `central_log_server/client/__init__.py`/`client/history.py` then serialize
   with `orjson.dumps(..., default=str)`.
2. **`show_locals=True` in exception rendering** —
   [err_handling.py:130](../../src/aeth_ext/errors/err_handling.py#L130) and
   [traceback_image.py:73](../../src/aeth_ext/errors/traceback_image.py#L73) dump every
   local variable's `repr()` (including config/settings objects) into the
   traceback text/image that gets emailed and pushed as an alert.

Goal: make secret exposure structurally hard by typing every credential as
`pydantic.SecretStr`, and give the alert path an opt-in (default-off) toggle
to show real values for rare manual debugging.

Confirmed by a quick test (`orjson.dumps({"password": SecretStr(...)}, default=str)`)
that `orjson`'s existing `default=str` fallback already calls `str()` on any
`SecretStr`/`SecretBytes` it hits — at any nesting depth, since `default` is
invoked per-value during serialization, not just at the top level — and
`SecretStr.__str__` returns `'**********'`, never the real value. So typing
the credential fields as `SecretStr` (step 1 below) is sufficient on its own
to mask them everywhere they flow through `record_to_payload`/`orjson.dumps`;
no separate scrubbing pass in `protocol.py` is needed.

## Approach

### 0. Branch
Create and switch to a new branch (`feat/secret-redaction`) before making any
changes, off current `main`.

### 1. `SecretStr` for every credential field
In [settings.py](../../src/aeth_ext/settings.py), change the type of:
- `alerts_email_pwd` (L50): `str` → `SecretStr`
- `alerts_pushover_token` (L59): `str | None` → `SecretStr | None`
- `alerts_pushover_user_key` (L60): `str | None` → `SecretStr | None`
- `alerts_healthcheck_pingkey` (L67): `str | None` → `SecretStr | None`

Pydantic coerces plain string env values into `SecretStr` automatically, and
`SecretStr`'s default `__repr__`/`__str__` already produce `'**********'`, so
this alone fixes any accidental `repr(SETTINGS)`/log-dump leak and gives
`orjson`'s existing `default=str` fallback a safe value to fall back to.

Update every call site that currently uses these fields as plain strings, via
`.get_secret_value()` at the point of actual use:
- [utils.py:215](../../src/aeth_ext/utils.py#L215) `batch_send_emails` — unwrap
  `SETTINGS.alerts_email_pwd` when defaulting `smtp_password` (keep the
  function's own `smtp_password: str | None` parameter type unchanged — it's a
  public API taking a caller-supplied override).
- [send_alert_push.py:49-50](../../src/aeth_ext/errors/send_alert_push.py#L49-L50) —
  unwrap `alerts_pushover_token`/`alerts_pushover_user_key` when building the
  Pushover `payload` dict. The existing `if not SETTINGS.alerts_pushover_token`
  guard (L44) keeps working unchanged (`SecretStr` implements `__len__`/falsy
  behavior against the wrapped value).
- [startup.py:67,115](../../src/aeth_ext/central_log_server/startup.py#L67) — unwrap
  `settings.alerts_healthcheck_pingkey` (both `send_heartbeat`/
  `run_heartbeat_async` call sites) before passing to `heartbeat.py`'s
  `pingkey: str | None` parameter, which stays plain `str` since it's a public,
  widely-threaded API (`monitoring/heartbeat.py`).

No FTP/SFTP password fields were found in the repo (`ftp/adapter.py` etc. take
pre-built connection objects) — nothing to change there.

### 2. Opt-in `show_locals` toggle on the alert path (default off)
In [err_handling.py](../../src/aeth_ext/errors/err_handling.py) and
[traceback_image.py](../../src/aeth_ext/errors/traceback_image.py):
- Add `show_locals: bool = False` to `_format_exception_traceback()` and
  `render_exception_image()`, passed straight through to their respective
  `console.print_exception(show_locals=...)` calls.
- Add `show_locals: bool = False` to `_send_alerts()`, threaded to both of the
  above.
- Add `show_locals: bool = False` to the public alert-triggering entry points —
  `alert_exception()`, `report_exc()`, `handle_fatal_exc_sync()`,
  `handle_fatal_exc_async()` — threaded down to their `_send_alerts(...)` calls,
  so a caller can opt in for a specific troublesome call site
  (`report_exc("worker", show_locals=True)`) without changing the global
  default.

This is scoped to the alert path only. The Rich `install(show_locals=True)`
hooks in [logging/setup.py](../../src/aeth_ext/logging/setup.py) (L167, 173, 347, 628)
and `console_rich.toml`'s `tracebacks_show_locals = true` govern the
*interactive terminal* traceback display only — never transmitted anywhere —
so they're left untouched.

### Verification
- `uv run pytest tests/test_settings.py tests/errors tests/central_log_server/test_protocol.py` —
  extend/add cases: a `SecretStr` settings field round-trips from env and
  `repr()`/`str()` never contains the raw value; `orjson.dumps(record_to_payload(record), default=str)`
  masks a `SecretStr` placed in `extra=` (including when nested in a dict/list);
  `show_locals=True` opt-in still renders real local values while the default
  omits them.
- `uv run pytest` (full suite), `uv run ruff check .`, `uv run pyright`.
