# aeth-ext

> Sweet Fire Tobacco shared library — a batteries-included foundation for building
> Python services, batch jobs, and CLI tools.

`aeth-ext` bundles the cross-cutting infrastructure that the Sweet Fire Tobacco
projects rely on: a one-call application bootstrap, an opinionated logging stack
built on [Rich](https://github.com/Textualize/rich), pydantic-based settings,
FTP/SFTP transfer adapters, a static (import-free) subclass discovery engine, a
monkey-patching framework, alert emails, and assorted utilities.

- **Python:** `>=3.14`
- **Package name (PyPI):** `aeth-ext`
- **Import name:** `aeth_ext`
- **Author:** Jacob Ogden

---

## Table of contents

- [aeth-ext](#aeth-ext)
  - [Table of contents](#table-of-contents)
  - [Installation](#installation)
  - [Quick start](#quick-start)
  - [Architecture overview](#architecture-overview)
  - [Modules](#modules)
    - [`aeth_ext` — application bootstrap](#aeth_ext--application-bootstrap)
    - [`settings` — configuration](#settings--configuration)
      - [Helpers](#helpers)
    - [`logging` — logging stack](#logging--logging-stack)
      - [Public API](#public-api)
    - [`errors` — fatal-exception handling \& alerts](#errors--fatal-exception-handling--alerts)
    - [`ftp` — FTP / SFTP adapters](#ftp--ftp--sftp-adapters)
      - [FTP API](#ftp-api)
    - [`monkey_patcher` — patch framework](#monkey_patcher--patch-framework)
    - [`_search_for_subclasses` — static class discovery](#_search_for_subclasses--static-class-discovery)
    - [`const_parsing` — constant extraction](#const_parsing--constant-extraction)
    - [`utils` — email \& datetime helpers](#utils--email--datetime-helpers)
    - [`types` — shared types \& mixins](#types--shared-types--mixins)
    - [`rich` — enhanced progress bars](#rich--enhanced-progress-bars)
  - [Configuration reference](#configuration-reference)
  - [Development](#development)
  - [Releasing](#releasing)

---

## Installation

The library is published to the internal SFTPyPI index.

```bash
# base install
uv add aeth-ext

# with the high-performance async event loop (uvloop on Linux, winloop on Windows)
uv add "aeth-ext[async]"

# with SFTP support (paramiko)
uv add "aeth-ext[sftp]"

# everything
uv add "aeth-ext[async,sftp]"
```

| Extra   | Pulls in                               | Use when                             |
| ------- | -------------------------------------- | ------------------------------------ |
| `async` | `uvloop` (Linux) / `winloop` (Windows) | You call `initialize(asyncio=True)`  |
| `sftp`  | `paramiko`                             | You use `AdaptedSFTP`                |

Core runtime dependencies: `aiologic`, `pydantic-settings`, `python-dateutil`,
`rich`, `tzdata`.

---

## Quick start

```python
from aeth_ext import initialize

# Bootstraps logging, applies registered monkey patches, and (optionally)
# installs a high-performance asyncio event loop.
initialize(asyncio=True)
```

A more complete service entry point:

```python
# main.py
from aeth_ext import initialize
from aeth_ext.settings import BaseSettings


# 1. Define your settings by subclassing BaseSettings.
class Settings(BaseSettings):
    api_token: str  # read from env var API_TOKEN (or .env in debug)


def main() -> None:
    initialize(asyncio=False)          # logging + monkey patches
    settings = Settings.get_settings() # resolved singleton
    ...


if __name__ == "__main__":
    main()
```

---

## Architecture overview

A central theme of the library is **static, import-free discovery**: rather than
forcing you to register components in a central place, `aeth_ext` scans your
source tree, finds the most-derived subclass of a given base, and wires it up
automatically. This powers settings (`BaseSettings`), logging
(`BaseLoggingConfig`), and patches (`MonkeyPatcher`).

```mermaid
flowchart TD
    A["initialize()"] --> B["init_logging() / init_logging_worker()"]
    A --> C["MonkeyPatcher.apply_monkey_patches()"]
    A --> D["install uvloop / winloop (asyncio=True)"]
    B --> E["discover deepest BaseLoggingConfig subclass"]
    C --> F["discover MonkeyPatcher subclasses"]
    E --> G["_search_for_subclasses"]
    F --> G
    H["BaseSettings.get_settings()"] --> I["CapturesSubclasses mixin"]
```

---

## Modules

### `aeth_ext` — application bootstrap

The package root exposes a single orchestration function.

```python
def initialize(
    *queues: QueueCatchall,
    asyncio: bool = False,
    worker: bool = False,
    run_monkey_patches: bool = True,
    return_wrapped: bool = False,
) -> None | Callable[[], None]: ...
```

| Parameter            | Default | Description                                                                                   |
| -------------------- | ------- | --------------------------------------------------------------------------------------------- |
| `*queues`            | none    | Logging queues (`QueueCatchall`) to attach for multi-process / multi-thread log fan-in.       |
| `asyncio`            | `False` | Install `uvloop` (POSIX) or `winloop` (Windows) as the active event loop. Requires `[async]`. |
| `worker`             | `False` | Use worker-process logging config (`init_logging_worker`) instead of main-process config.     |
| `run_monkey_patches` | `True`  | Discover and apply every `MonkeyPatcher` subclass before the app starts.                      |
| `return_wrapped`     | `False` | Return the initializer as a callable instead of running it immediately (useful for deferral). |

```python
# Run immediately
initialize()

# Defer execution (e.g. to pass into a process pool initializer)
init = initialize(asyncio=True, return_wrapped=True)
init()
```

---

### `settings` — configuration

`BaseSettings` extends `pydantic_settings.BaseSettings` and the
`CapturesSubclasses` mixin, so the *most-derived* subclass is resolved
automatically. In debug builds it reads a `.env` file; in release builds it
relies purely on environment variables.

```python
from aeth_ext.settings import BaseSettings


class Settings(BaseSettings):
    api_token: str  # required, from API_TOKEN


settings = Settings.get_settings()  # singleton; same instance every call
creds = settings.creds_file_reusable("Missing creds", "ftp", "creds.json")
```

Key built-in fields (all overridable via env vars / `.env`):

| Field                | Env var              | Default                                            |
| -------------------- | -------------------- | -------------------------------------------------- |
| `persisted_dir_loc`  | `PERSISTED_DIR_LOC`  | `./persisted_data` (debug) / `/app/persisted_data` |
| `alerts_smtp_server` | `ALERTS_SMTP_SERVER` | `smtppro.zoho.com`                                 |
| `alerts_smtp_port`   | `ALERTS_SMTP_PORT`   | `587`                                              |
| `alerts_email`       | `ALERTS_EMAIL`       | `info@sweetfiretobacco.com`                        |
| `alerts_email_pwd`   | `ALERTS_EMAIL_PWD`   | *(required)*                                       |
| `alerts_recipients`  | `ALERTS_RECIPIENTS`  | `{jacob.ogden@sweetfiretobacco.com}`               |
| `log_loc_folder`     | `LOG_LOC_FOLDER`     | `<persisted_dir_loc>/logs`                         |
| `tz`                 | `TZ`                 | `US/Eastern`                                       |

#### Helpers

- `get_settings()` / `get_final_model()` — resolve the singleton.
- `creds_file_reusable(err_msg, *path_parts)` — validate and return a file path
  under `persisted_dir_loc`, raising `FileNotFoundError` with `err_msg` if absent.

---

### `logging` — logging stack

A Rich-powered logging system with daily/per-run file rotation, abbreviated
library paths, and queue-based fan-in for multi-process apps. Usually you don't
call these directly — `initialize()` does — but you can customize behavior by
subclassing `BaseLoggingConfig`.

```python
from aeth_ext.logging.config import BaseLoggingConfig
from rich.console import Console


class LoggingConfig(BaseLoggingConfig):
    def configure_logging_main(rich_console: Console, project_name: str, **kw) -> None:
        # override to customize handlers, formats, rotation, etc.
        ...
```

#### Public API

- `init_logging(*queues)` — main-process setup; discovers the deepest
  `BaseLoggingConfig` subclass and the `configure_logging_main` constants from your
  `__main__`.
- `init_logging_worker(queue)` — worker-process setup; routes logs to the parent
  via a `QueueHandler`.
- `BaseLoggingConfig` — override point for `configure_logging_main`,
  `configure_logging_worker`, `configure_base_per_runner`, and `configure_base_once`.
- `QueueCatchall` — union type of the supported queue backends
  (`InterpreterQueue | ProcessQueue | ThreadQueue`).
- `FixedRichHandler`, `FixedLogRecord`, `CustomTimedRotatingFileHandler` —
  the handler/record building blocks that abbreviate `site-packages`/`src`/`Lib`
  paths in output.

---

### `errors` — fatal-exception handling & alerts

Decorators that wrap a callable, log + email on any unhandled exception, set a
shared `FATAL_EVENT`, and swallow the error (returning `None`).

```python
from aeth_ext.errors.err_handling import (
    handle_fatal_exc_sync,
    handle_fatal_exc_async,
    FATAL_EVENT,
)


@handle_fatal_exc_sync
def risky() -> int:
    return 1 / 0  # logs, emails an alert, sets FATAL_EVENT, returns None


@handle_fatal_exc_async
async def risky_async() -> None:
    ...
```

**`send_alert_email(subject, content)`** composes and batch-sends an alert email
to `settings.alerts_recipients`, attaching `content` as a UTF-8 file. It logs (and
no-ops) if no recipients are configured.

---

### `ftp` — FTP / SFTP adapters

A unified, context-managed interface over plain FTP and Paramiko SFTP, with
optional Rich progress bars and server-to-server transfers.

```python
from aeth_ext.ftp.adapter import AdaptedSFTP  # requires the [sftp] extra
from aeth_ext.rich.progress import Progress

with Progress() as pbar, AdaptedSFTP(sftp_protocol, "my-container", pbar=pbar) as ftp:
    ftp.download_file(remote_path, callback, task_msg="Downloading")
    ftp.upload_file(remote_path, callback, file_size, task_msg="Uploading")
    ok = ftp.transfer_file(source, dest, other, task_msg="Relaying", ...)
```

#### FTP API

- `AdaptedFTP` / `AdaptedSFTP` — context managers exposing `upload_file`,
  `download_file`, and `transfer_file` (SFTP additionally provides `rename`,
  `makedir`, `get_size`, `test_connection`).
- `AdapterProtocol`, `FTPProtocol`, `SFTPProtocol`, `ProtocolEnum`,
  `ListDirResult` — the protocol/types layer (`ftp/types.py`).
- `ServerNotAvailableError(ConnectionError)` — raised when a server is unreachable.

---

### `monkey_patcher` — patch framework

Organize monkey patches as subclasses. Each plain method you define is forced
into a `staticmethod` by the metaclass and is invoked once when patches are
applied. The class is **not instantiable** — call its classmethods directly.

```python
from aeth_ext.monkey_patcher import MonkeyPatcher


class MyPatches(MonkeyPatcher):
    def patch_some_library():
        import some_library
        some_library.thing = replacement


MonkeyPatcher.apply_monkey_patches()  # discovers + runs every subclass's patches
```

`initialize(run_monkey_patches=True)` calls `apply_monkey_patches()` for you.

---

### `_search_for_subclasses` — static class discovery

The engine behind the auto-wiring. It scans `.py` files with the `ast` module —
**without importing them** — to find subclasses, then loads only the ones you ask
for.

- `find_subclasses(base, roots, *, ignored_dirs=..., include_name_fallback=False, recursive=True)`
  → `tuple[SubclassInfo, ...]`
- `get_entrypoint_root()` → topmost package dir of the running entrypoint.
- `SubclassInfo` — `NamedTuple` with `qualname`, `name`, `module`, `file`,
  `lineno`, `depth`; call `.load()` to import the live class.
- `iter_python_files`, `build_subclass_index`, `load_subclasses`,
  `reset_subclass_caches` — supporting helpers.

---

### `const_parsing` — constant extraction

Read uppercase constant assignments out of a source file via AST and safely
evaluate them against a restricted namespace.

```python
from pathlib import Path
from aeth_ext.const_parsing import parse_and_grab_constants

values = parse_and_grab_constants(
    Path("config.py"),
    expected_constants={"PROJECT_NAME": "project_name"},
    eval_locals={},
)
# -> {"project_name": "<value of PROJECT_NAME>"}
```

It scans both module-level statements and the `if __name__ == "__main__":` block.

---

### `utils` — email & datetime helpers

Email composition / batch sending plus offset-aware datetime helpers.

```python
from aeth_ext.utils import prepare_email_message, batch_send_emails, get_now, today

msg = prepare_email_message({
    "subject": "Report",
    "body": "See attached.",
    "from_addr": "info@sweetfiretobacco.com",
    "to_addrs": ["jacob.ogden@sweetfiretobacco.com"],
    "attachments": Path("report.csv"),
})
batch_send_emails(msg)  # SMTP config defaults to the alerts.* settings
```

| Function                              | Purpose                                                       |
| ------------------------------------- | ------------------------------------------------------------- |
| `prepare_email_message(parts)`        | Build an `EmailMessage` from an `EmailMessageParts` dict.     |
| `batch_send_emails(msgs, ...)`        | Send one or many messages over SMTP (defaults to alerts cfg). |
| `handle_addrlike` / `..._sequence`    | Normalize flexible `AddressLike` values.                      |
| `handle_attachment(path)`             | Read a file and return `(bytes, mime-info)`.                  |
| `get_now(tz=None)` / `today(tz=None)` | Current datetime / midnight with configurable offset.         |
| `get_last_sat(...)` / `get_next_sat`  | Previous / next Saturday.                                     |

---

### `types` — shared types & mixins

- `AddressLike` — `str | Address | tuple[str, str | None, str | None, str | None]`.
- `EmailMessageParts` — `TypedDict` for `prepare_email_message` (`subject`,
  `body`, `from_addr`, `to_addrs` required; `cc_addrs`, `bcc_addrs`,
  `attachments` optional).
- `StrEnum` — string enum whose value mirrors the member name.
- `CapturesSubclasses` (`types/abc.py`) — mixin that registers instances and can
  resolve the deepest subclass / final model; the backbone of the auto-wiring used
  by `BaseSettings` and `BaseLoggingConfig`.
- `SingletonType`, `SingletonTypeABC`, `SingletonTypeBaseModel` — singleton
  metaclasses.

---

### `rich` — enhanced progress bars

`Progress` is a `rich.progress.Progress` subclass preconfigured with a sensible
column layout (bar, M-of-N, percentage, time remaining). Its `TaskID` supports
use as a context manager so a task is auto-removed on exit.

```python
from aeth_ext.rich.progress import Progress

with Progress() as progress:
    with progress.add_task("Working", total=100) as task_id:
        progress.update(task_id, advance=50)
    # task is removed automatically here
```

---

## Configuration reference

Settings are read (in priority order) from explicit constructor args →
environment variables → a `.env` file (debug builds only) → field defaults.
Empty env values are ignored, and unknown keys are dropped (`extra="ignore"`).

Example `.env`:

```dotenv
PERSISTED_DIR_LOC=./persisted_data
ALERTS_EMAIL_PWD=super-secret
ALERTS_RECIPIENTS=["ops@sweetfiretobacco.com","jacob.ogden@sweetfiretobacco.com"]
TZ=US/Eastern
```

> Never commit real secrets. Provide `ALERTS_EMAIL_PWD` and any credentials via
> the environment or your secret manager.

---

## Development

This project uses [uv](https://github.com/astral-sh/uv).

```bash
# install dependencies (including the dev group)
uv sync

# run the type checker
uv run pyright

# lint
uv run ruff check

# import smoke test
uv run python -c "import aeth_ext"
```

The dev group includes `paramiko`, `pyright`, `types-python-dateutil`, and the
async event loop backends.

---

## Releasing

A [Poe the Poet](https://poethepoet.natn.io/) task automates version bump, tag,
build, and publish to GitHub + SFTPyPI:

```bash
uv run poe release patch   # or: minor | major
```

It bumps the version in `pyproject.toml`, commits, tags `vX.Y.Z`, pushes with
tags, builds, publishes to the `SFTPyPI` index, and creates a GitHub release with
generated notes.
