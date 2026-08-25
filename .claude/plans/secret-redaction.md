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
`pydantic.SecretStr`, and audit for places where `.get_secret_value()`'s
unwrapped result gets bound to a variable/attribute that outlives the
expression that needed it — since that's what would still leak through Rich's
`show_locals=True` traceback rendering, which stays on (see "Revision" below).

Confirmed by a quick test (`orjson.dumps({"password": SecretStr(...)}, default=str)`)
that `orjson`'s existing `default=str` fallback already calls `str()` on any
`SecretStr`/`SecretBytes` it hits — at any nesting depth, since `default` is
invoked per-value during serialization, not just at the top level — and
`SecretStr.__str__` returns `'**********'`, never the real value. So typing
the credential fields as `SecretStr` (step 1 below) is sufficient on its own
to mask them everywhere they flow through `record_to_payload`/`orjson.dumps`;
no separate scrubbing pass in `protocol.py` is needed.

### Revision (2026-08-22)

Re-reviewed this plan after it went stale. Two changes:

1. **Dropped the `show_locals` opt-in toggle (old step 2).** Decided to keep
   `show_locals=True` as the permanent, unconditional default on the alert
   path (`err_handling.py`'s `_extract_rich_traceback()`,
   `traceback_image.py`'s `render_exception_image()`) for debugging value,
   rather than defaulting it off. It's also moot to redesign right now: the
   function names that section targeted (`_format_exception_traceback`,
   `_send_alerts`) no longer exist — `err_handling.py` was refactored since
   this plan was written into `_extract_rich_traceback()` (hardcoded
   `show_locals=True`) and a public `alert()` entry point, with
   `_handle_fatal()` now sitting in the call chain between the decorators and
   `alert()`.
2. **Added a lingering-secret-variable audit (new step 2)** in place of the
   toggle: since `show_locals` stays on, the only thing standing between a
   raw secret and an alert email/push is not typing it `SecretStr` — it's
   also making sure nothing keeps the unwrapped `.get_secret_value()` result
   bound to a name any longer than the single expression that needs it. A
   local variable/dict entry holding a raw secret can sit in a frame for many
   lines before `show_locals` might dump it; an attribute stored on a
   long-lived object is worse, since it persists for that object's entire
   lifetime, not just one function call.

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

Update every call site that currently uses these fields as plain strings.
Where the unwrapped value would otherwise sit in a local/attribute for more
than the one expression that needs it, restructure so the `SecretStr` stays
wrapped until the last possible moment instead of just relocating
`.get_secret_value()` to where the field is first read (see step 2 — several
of these call sites are also lingering-variable fixes, not just type changes):

- [utils.py:200-231](../../src/aeth_ext/utils.py#L200) `batch_send_emails` —
  currently `smtp_password = smtp_password or SETTINGS.alerts_email_pwd` (L215)
  unwraps immediately, then the raw string sits in that local for 16 lines
  (through a `match` block and into the `with SMTP(...)` block) before
  `server.login(smtp_user, smtp_password)` (L231) finally uses it. Keep the
  function's own `smtp_password: str | None` parameter type unchanged (public
  API, caller-supplied override) but internally re-type the *local* as
  `SecretStr | None` and unwrap only inline at
  `server.login(smtp_user, smtp_password.get_secret_value())`.
- [send_alert_push.py:43-69](../../src/aeth_ext/errors/send_alert_push.py#L43) —
  currently `payload["token"]`/`payload["user"]` (L49-50) get the raw values
  at dict-construction time, then `payload` lives ~20 lines (through the
  priority-2 branch and the image-attach branch) before `urlencode(payload)`
  (L69) consumes it. The existing `if not SETTINGS.alerts_pushover_token` guard
  (L44) keeps working unchanged (`SecretStr` implements truthiness against the
  wrapped value). Store the `SecretStr` objects in `payload` as built, and
  unwrap only at the `urlencode()` call site via a shallow copy:
  `urlencode({**payload, "token": payload["token"].get_secret_value(), "user": payload["user"].get_secret_value()})`
  — the raw-value dict then never gets bound to a name and is collectible the
  instant `urlencode` returns.
- [startup.py:73,121](../../src/aeth_ext/central_log_server/startup.py#L73) — unwrap
  `settings.alerts_healthcheck_pingkey` at both `send_heartbeat_async`/
  `run_heartbeat_async` call sites. Unlike the plain-`str` `pingkey` parameter
  this was originally scoped to keep unchanged, widen scope into
  `monitoring/heartbeat.py` per step 2 below — `pingkey` ends up stored as a
  long-lived instance attribute there, which is worse than a local.

`ftp/credentials.py` already types `password`/`private_key_passphrase` as
`SecretStr` (added after this plan was first written) — use it as the
reference pattern. Its three `.get_secret_value()` call sites
([connectors.py:96,164,167](../../src/aeth_ext/ftp/connectors.py#L96)) are all
inline call arguments, never bound to a name — already the target shape for
every unwrap site above.

### 2. Audit for lingering unwrapped-secret variables/attributes

Beyond the three call sites folded into step 1 above, one more instance found
by grepping every `.get_secret_value()`-worthy field and tracing where the
raw value ends up living:

- [heartbeat.py:269-307](../../src/aeth_ext/monitoring/heartbeat.py#L269)
  `HeartbeatThread.__init__` does `self._pingkey = pingkey` (L286), storing the
  raw key as an attribute on a `Thread` subclass that lives for the process's
  lifetime — not a transient local. If `run()` (or anything it calls) raises,
  `show_locals` renders `self` in that frame and the key comes along for free.
  Convert `pingkey: str | None` to `SecretStr | None` everywhere it's
  threaded in this module — `_resolve_ping_url`, `_send_heartbeat`,
  `send_heartbeat`/`send_heartbeat_async`, `run_heartbeat_async`,
  `HeartbeatThread.__init__`/`self._pingkey` — unwrapping only inline at the
  single f-string in `_resolve_ping_url` that builds
  `f"https://hc-ping.com/{pingkey}/{slug}"`. This reverses the original plan's
  call to leave this parameter as plain `str` (made when it was scoped as a
  "public, widely-threaded API" concern rather than a lingering-variable one).

The `show_locals=True` calls themselves
([err_handling.py:147](../../src/aeth_ext/errors/err_handling.py#L147),
[traceback_image.py:97](../../src/aeth_ext/errors/traceback_image.py#L97)) are
left as-is — see "Revision" above. The Rich `install(show_locals=True)` hooks
in [logging/setup.py](../../src/aeth_ext/logging/setup.py) (L161, 167, 334, 624)
and `console_rich.toml`'s `tracebacks_show_locals = true` govern the
*interactive terminal* traceback display only — never transmitted anywhere —
so they're untouched and out of scope for this audit.

### Verification

- `uv run pytest tests/test_settings.py tests/errors tests/central_log_server/test_protocol.py tests/monitoring` —
  extend/add cases: a `SecretStr` settings field round-trips from env and
  `repr()`/`str()` never contains the raw value; `orjson.dumps(record_to_payload(record), default=str)`
  masks a `SecretStr` placed in `extra=` (including when nested in a dict/list);
  `batch_send_emails`/`send_alert_push`/heartbeat pingkey-URL construction still
  work end-to-end with real values once unwrapped at the call site.
- `uv run pytest` (full suite), `uv run ruff check .`, `uv run pyright`.
