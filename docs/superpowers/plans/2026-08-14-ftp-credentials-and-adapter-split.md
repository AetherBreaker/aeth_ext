# FTP/SFTP Credentials & Adapter Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the consumer-authored `FTPProtocol`/`SFTPProtocol` connection-opening classes with
`FTPCredentials`/`SFTPCredentials` dataclasses, and split the single generic `FTPAdapter[HandlerType_T]`
into two independent concrete classes (`FTPAdapter`, `SFTPAdapter`) behind an overloaded
`create_ftp_adapter` factory, so the FTP-vs-SFTP identity of an adapter and its sessions is statically
resolvable (goto-definition lands on the real method, not a shared protocol stub) and determined once,
at construction, with no runtime `isinstance`/`issubclass` branching in the per-session hot path.

**Architecture:** Consumers build a `FTPCredentials`/`SFTPCredentials` instance and pass it to
`create_ftp_adapter(...)`, which returns a concrete `FTPAdapter` or `SFTPAdapter` (both subclass a
shared `_PooledAdapterBase` holding only protocol-agnostic pool bookkeeping). Each adapter builds a
private `_FTPConnector`/`_SFTPConnector` from the credentials, which owns all raw connection-opening
logic (`ftplib`/`paramiko` calls) that used to live in consumer code. `AdaptedFTP`/`AdaptedSFTP` (the
per-session transfer objects) no longer hold a connector or protocol instance at all -- they take a
single `HandleProvider` object (constructor-injected) with `acquire()`/`release()` methods, and
`FTPAdapter`/`SFTPAdapter` structurally satisfy that protocol themselves. This also makes standalone,
non-pooled usage possible for the first time: any object with `acquire()`/`release()` methods can be
handed directly to `AdaptedFTP`/`AdaptedSFTP`, bypassing `FTPAdapter`/`create_ftp_adapter` entirely.

**Tech Stack:** Python 3.14, `pydantic` (dataclasses, `SecretStr`, validators), `ftplib` (`FTP`,
`FTP_TLS`), `paramiko` (`SSHClient`, `Transport`, `SFTPClient`, `AutoAddPolicy`, `RejectPolicy`),
`pytest` with real loopback FTP/SFTP servers (no socket mocking).

**Spec:** `docs/superpowers/specs/2026-08-14-ftp-credentials-and-adapter-split-design.md` -- this plan
implements that spec section-by-section (Section numbers referenced below correspond to that document).
The prior, already-implemented SFTP transport-multiplexing work
(`docs/superpowers/specs/2026-08-14-sftp-transport-multiplexing-design.md`) is the reason
`sftp_pool.py`/`SFTPPool`/`TransportState`/`Channel` already exist -- this plan builds on top of them,
not from scratch.

## Global Constraints

- **Do not commit anything while executing this plan.** No task ends with a commit step. Leave the
  working tree as-is at the end for the user to review locally.
- **This is a breaking API change with no backward-compatibility shim.** `FTPProtocol`, `SFTPProtocol`,
  `FTPProtocolBase`, `ProtocolEnum` are deleted outright from `aeth_ext.ftp.types`. There is no deprecated
  alias, no transitional dual-support period. This package has no in-tree consumers of these types
  outside its own tests, so nothing else in this repo needs migrating besides `tests/ftp/`.
- **Avoid overeager abstraction.** Do not extract a function/method whose body is 4 lines of code or
  fewer unless (a) a ruff complexity lint (too-many-statements, too-many-branches, too-long, C901,
  PLR0917, etc.) forces it after an initial write pass, or (b) the code has a genuine, significant
  responsibility reused across multiple call sites -- the larger the body, the fewer reuse sites are
  needed to justify extraction; the smaller the body, the more sites are needed. Before writing a short
  helper, stop and ask whether inlining its body at the call site would read more clearly. This plan's
  code samples already apply this judgment (e.g. `FTPAdapter.acquire()` inlines what would otherwise be
  tiny single-call-site `_checkout_idle`/`_dial`/`_instrumentation_callbacks` helpers) -- follow that
  precedent rather than re-splitting things back out. See `.claude/CLAUDE.md`'s "Abstraction
  Conventions" section for the full statement of this rule.
- **No `from __future__ import annotations` anywhere** (project-wide rule). Annotations are evaluated
  eagerly. A type used only as a plain method/function parameter or return annotation (never actually
  constructed, `isinstance`-checked, or read as a dataclass/pydantic field at runtime) may stay under
  `TYPE_CHECKING`. A type that pydantic needs to resolve for validator-building (any real field on an
  `IsPydantic`-inheriting dataclass), or that stdlib `@dataclass` needs for its own field introspection,
  or that is actually constructed/`isinstance`-checked at runtime, must be a real top-level import.
- **All pydantic dataclasses in this project inherit `aeth_ext.types.IsPydantic`.**
- **PEP 758 bare multi-exception `except A, B, C:` (no parentheses) is valid** when not binding with
  `as e` -- this project targets Python 3.14. Do not "fix" this syntax if you see it; it already appears
  in code this plan touches (`except ConnectionRefusedError, TimeoutError, OSError:`).
- **Docstrings are Google style** (`Args:`/`Returns:`/`Raises:`, no Sphinx roles). Comments/docstrings
  carry the *why*, stay dense, don't restate what the code already says.
- **Testing workflow:** this plan is executed on a feature branch, not `main`. Only run the specific
  tests relevant to the task you just finished as you go; run the full suite (`uv run pytest`) exactly
  once, in the final task, immediately before stopping.
- **Tests are not a statement of intent.** You have full authority to rewrite, restructure, or delete
  any test in `tests/ftp/` as needed -- an existing assertion is evidence an earlier session wrote it,
  never evidence of what behavior is actually wanted. This plan and the spec it implements are the
  source of truth for intended behavior.
- **Ground-truth verification:** the live IDE diagnostics feed has been observed to be stale/unreliable
  in this codebase (showing phantom errors already fixed, or missing real ones). Always re-verify with
  `uv run pyright <path>` and `uv run ruff check <path>` directly rather than trusting a live diagnostics
  push notification.
- **`PYTHONPYCACHEPREFIX`** is auto-loaded from `.env` under `uv run pytest` -- no action needed unless
  running scripts directly outside pytest.

---

## Task 1: `FTPCredentials`/`SFTPCredentials` dataclasses

**Files:**
- Create: `src/aeth_ext/ftp/credentials.py`
- Create: `tests/ftp/test_credentials.py`

**Interfaces:**
- Produces: `FTPCredentials(host, username, password, port=21, use_tls=False, passive_mode=True, connect_timeout=None)`
  and `SFTPCredentials(host, username, port=22, password=None, private_key_path=None,
  private_key_passphrase=None, host_key_policy="reject", known_hosts_path=None, connect_timeout=None)`,
  both frozen pydantic dataclasses. `password`/`private_key_passphrase` are `pydantic.SecretStr`, not
  `str` -- unwrap with `.get_secret_value()` wherever the raw string is actually needed (Task 4).
  `SFTPCredentials` raises `pydantic.ValidationError` at construction if both `password` and
  `private_key_path` are unset. Every other task in this plan consumes these two classes; nothing else
  in this task depends on anything else in the plan.

- [ ] **Step 1: Write the failing validation tests**

Create `tests/ftp/test_credentials.py`:

```python
"""Validation tests for `FTPCredentials`/`SFTPCredentials`."""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest
from pydantic import ValidationError

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


class TestFTPCredentials:
  def test_builds_with_required_fields_only(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")

    assert creds.host == "ftp.example.com"
    assert creds.port == 21
    assert creds.use_tls is False
    assert creds.passive_mode is True
    assert creds.connect_timeout is None

  def test_password_is_not_exposed_in_repr(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")

    assert "hunter2" not in repr(creds)

  def test_is_frozen(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")

    with pytest.raises(Exception):  # pydantic's own frozen-instance error type, not asserted precisely
      creds.host = "other.example.com"  # pyright: ignore[reportAttributeAccessIssue]

  def test_port_out_of_range_is_rejected(self) -> None:
    with pytest.raises(ValidationError):
      FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", port=0)


class TestSFTPCredentials:
  def test_password_only_is_valid(self) -> None:
    creds = SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2")

    assert creds.password is not None
    assert creds.password.get_secret_value() == "hunter2"

  def test_private_key_only_is_valid(self, tmp_path: Path) -> None:
    key_path = tmp_path / "id_rsa"
    key_path.write_text("not a real key, just needs to exist as a path")

    creds = SFTPCredentials(host="sftp.example.com", username="svc", private_key_path=key_path)

    assert creds.private_key_path == key_path

  def test_neither_password_nor_key_is_rejected(self) -> None:
    with pytest.raises(ValidationError):
      SFTPCredentials(host="sftp.example.com", username="svc")

  def test_default_host_key_policy_is_reject(self) -> None:
    creds = SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2")

    assert creds.host_key_policy == "reject"

  def test_passphrase_is_not_exposed_in_repr(self) -> None:
    creds = SFTPCredentials(
      host="sftp.example.com", username="svc", private_key_path=None, password="hunter2", private_key_passphrase="s3cr3t"
    )

    assert "s3cr3t" not in repr(creds)
```

- [ ] **Step 2: Run tests to verify they fail with an import error**

Run: `uv run pytest tests/ftp/test_credentials.py -v --no-cov`
Expected: `ModuleNotFoundError: No module named 'aeth_ext.ftp.credentials'` (or similar collection error)
for every test.

- [ ] **Step 3: Write `src/aeth_ext/ftp/credentials.py`**

```python
"""Connection credentials for `create_ftp_adapter` -- consumers build one of these (typically as a
module-level constant) instead of writing a `FTPProtocol`/`SFTPProtocol`-conforming class.
"""

# Standard library imports
from pathlib import Path
from typing import TYPE_CHECKING, Literal

# Third party imports
from pydantic import Field, SecretStr, model_validator
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.types import IsPydantic

if TYPE_CHECKING:
  # Standard library imports
  from typing import Self


__all__ = ["FTPCredentials", "SFTPCredentials"]


@dataclass(frozen=True, slots=True)
class FTPCredentials(IsPydantic):
  """Credentials for a plain (optionally TLS-wrapped) FTP server."""

  host: str
  username: str
  password: SecretStr
  port: int = Field(default=21, gt=0, le=65535)
  use_tls: bool = False
  passive_mode: bool = True
  connect_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class SFTPCredentials(IsPydantic):
  """Credentials for an SFTP server. Requires either `password` or `private_key_path` (or both).

  `known_hosts_path` is `None` by default, falling back to the OS's `~/.ssh/known_hosts`; set it
  explicitly for a deterministic trust source that doesn't depend on whichever account the process
  happens to run as.
  """

  host: str
  username: str
  port: int = Field(default=22, gt=0, le=65535)
  password: SecretStr | None = None
  private_key_path: Path | None = None
  private_key_passphrase: SecretStr | None = None
  host_key_policy: Literal["auto_add", "reject"] = "reject"
  known_hosts_path: Path | None = None
  connect_timeout: float | None = None

  @model_validator(mode="after")
  def _require_an_auth_method(self) -> Self:
    if self.password is None and self.private_key_path is None:
      raise ValueError("SFTPCredentials requires either password or private_key_path")
    return self
```

`frozen=True` because these are meant to be built once (typically as a module-level constant) and
handed around to `create_ftp_adapter`/one-off `HandleProvider`s -- immutability keeps a shared constant
from being mutated out from under every adapter built from it. `SecretStr` (not plain `str`) for
`password`/`private_key_passphrase` keeps them out of `repr()`/logs/exception messages, per this
project's existing secret-handling discipline around `.env`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ftp/test_credentials.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Verify types/lint**

Run: `uv run pyright src/aeth_ext/ftp/credentials.py tests/ftp/test_credentials.py`
Expected: 0 errors, 0 warnings.
Run: `uv run ruff check src/aeth_ext/ftp/credentials.py tests/ftp/test_credentials.py`
Expected: "All checks passed!" (fix any import-sort/heading findings ruff reports -- this project uses
`isort` import-heading comments like `# Standard library imports`/`# Third party imports`/`# First
party imports`, already used above).

---

## Task 2: `SFTPPool.drain_transports()`

**Files:**
- Modify: `src/aeth_ext/ftp/sftp_pool.py`
- Modify: `tests/ftp/test_sftp_pool.py`

**Interfaces:**
- Consumes: `SFTPPool`'s existing `_states: dict[int, TransportState]`, `_handle_states: dict[int,
  TransportState]`, `_idle: list[Channel]`, `_lock: Lock` (all already exist -- see current
  `src/aeth_ext/ftp/sftp_pool.py`).
- Produces: `SFTPPool.drain_transports(self) -> list[Transport]` -- used by Task 4's
  `SFTPAdapter._teardown_idle`.

- [ ] **Step 1: Write the failing test**

Add to `tests/ftp/test_sftp_pool.py` (the file already has `_FakeTransport`/`_FakeChannel` test doubles
and a `Channel` import -- reuse them):

```python
class TestDrainTransports:
  def test_drain_returns_all_registered_transports(self) -> None:
    pool = SFTPPool(channels_per_transport=4)
    t1, t2 = _FakeTransport(), _FakeTransport()
    pool.register_transport(t1)
    pool.register_transport(t2)

    drained = pool.drain_transports()

    assert set(drained) == {t1, t2}

  def test_drain_clears_all_tracking(self) -> None:
    pool = SFTPPool(channels_per_transport=4)
    state = pool.register_transport(_FakeTransport())
    handle = _FakeChannel()
    pool.track_handle(handle, state)
    idle_channel = Channel(handle=_FakeChannel(), state=state)
    pool.release_channel(idle_channel)

    pool.drain_transports()

    assert pool.pick_growth_target() is None
    assert pool.state_for_handle(handle) is None
    assert pool.checkout_channel() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestDrainTransports -v --no-cov`
Expected: FAIL with `AttributeError: 'SFTPPool' object has no attribute 'drain_transports'`.

- [ ] **Step 3: Implement `drain_transports`**

Add to the `SFTPPool` class in `src/aeth_ext/ftp/sftp_pool.py` (place it after `discard_transport`,
which it's conceptually adjacent to -- both clear tracking for one or more transports):

```python
  def drain_transports(self) -> list[Transport]:
    """Clears all tracked `Transport`s and returns them, for the caller to close (closing a `Transport`
    transitively closes every channel opened on it). Used by `SFTPAdapter._teardown_idle` -- shutdown
    closes whole `Transport`s directly rather than closing channels one at a time.
    """
    with self._lock:
      transports = [s.transport for s in self._states.values()]
      self._states.clear()
      self._handle_states.clear()
      self._idle.clear()
      return transports
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/ftp/test_sftp_pool.py -v --no-cov`
Expected: all PASS (including the pre-existing tests in this file -- confirm nothing regressed).

- [ ] **Step 5: Verify types/lint**

Run: `uv run pyright src/aeth_ext/ftp/sftp_pool.py tests/ftp/test_sftp_pool.py`
Run: `uv run ruff check src/aeth_ext/ftp/sftp_pool.py tests/ftp/test_sftp_pool.py`
Expected: both clean.

---

## Task 3: `types.py` -- remove old protocol classes, add `HandleProvider`

**Files:**
- Modify: `src/aeth_ext/ftp/types.py` (full replacement below -- the whole file is small)

**Interfaces:**
- Produces: `HandleProvider[HandleT](Protocol)` with `acquire(self) -> tuple[HandleT,
  Sequence[Callable[[bytes], Any]]]` and `release(self, handle: HandleT, is_fatal: bool) -> None`.
  `AdapterProtocol`, `ListDirResult`, `BufferSize`, `TransferSuccess` are unchanged and still exported.
  `FTPProtocolBase`, `FTPProtocol`, `SFTPProtocol`, `ProtocolEnum` no longer exist anywhere in this
  module or its `__all__`.
- Consumed by: Task 4 (`adapter.py` builds `_PooledAdapterBase`/`FTPAdapter`/`SFTPAdapter` to
  structurally satisfy `HandleProvider`, and `_AdaptedSessionBase` takes one as a constructor param).

This task will leave `adapter.py`'s imports temporarily broken (it still imports `FTPProtocol`/
`SFTPProtocol` from this module) -- that's expected and fixed by Task 4 immediately after. Don't run the
full test suite between Task 3 and Task 4; there's no useful intermediate state to verify here beyond
this file's own shape.

- [ ] **Step 1: Replace `src/aeth_ext/ftp/types.py` in full**

```python
# Standard library imports
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Buffer, Callable, Iterator, Sequence
  from datetime import datetime
  from io import BytesIO
  from typing import Any

  # First party imports
  from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP


__all__ = ["AdapterProtocol", "HandleProvider", "ListDirResult"]

BufferSize = int
TransferSuccess = bool


class ListDirResult(NamedTuple):
  filename: str
  modified_time: datetime


class HandleProvider[HandleT](Protocol):
  """Narrow extension point: something that can hand out a connection handle and take it back.

  `FTPAdapter`/`SFTPAdapter` structurally satisfy this via their own public `acquire`/`release` methods
  -- says nothing about *how* a handle is obtained, only that something can provide and reclaim one.
  Lets a consumer construct `AdaptedFTP`/`AdaptedSFTP` directly with a hand-written provider for
  one-shot, non-pooled usage, entirely bypassing `FTPAdapter`/`create_ftp_adapter`.
  """

  __slots__ = ()

  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]: ...
  def release(self, handle: HandleT, is_fatal: bool) -> None: ...


class AdapterProtocol(Protocol):
  __slots__ = ()

  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection to the FTP/SFTP server. Returns True if successful, False otherwise."""
    raise NotImplementedError

  def get_size(self, path: str) -> int | None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns its size in bytes."""
    raise NotImplementedError

  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns a writable file-like object (e.g. socket or SFTPFile) that can be used to send the file's contents."""
    raise NotImplementedError

  def download_file(self, remote_path: str, callback: Callable[[Buffer], Any], task_msg: str = "") -> None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns a readable file-like object (e.g. socket or SFTPFile) that can be used to read the file's contents."""
    raise NotImplementedError

  def transfer_file(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Transfers a file from source_remote_path to dest_remote_path on the FTP/SFTP server.
    This is intended to be used for server to server transfers that don't save the file locally."""
    raise NotImplementedError

  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the FTP/SFTP server from old_remote_path to new_remote_path."""
    raise NotImplementedError

  def remove(self, remote_path: str) -> None:
    """Removes a file on the FTP/SFTP server at the given absolute path."""
    raise NotImplementedError

  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Expects an absolute path to a directory on the FTP/SFTP server and returns an iterator of ListDirResult containing the filename and modification time of each file in the directory.
    The filename is not a full path, just the name of the file. The modification time is a datetime object representing the last modification time of the file on the server.
    Note that the modification time may be None if it cannot be determined, and in that case the tuple will not be yielded.
    """
    raise NotImplementedError

  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the FTP/SFTP server at the given absolute path."""
    raise NotImplementedError
```

Note what was removed relative to the current file: the `abstractmethod`, `Enum`/`auto`, and `override`
imports (all were only used by the now-deleted `FTPProtocolBase`/`FTPProtocol`/`SFTPProtocol`/
`ProtocolEnum`), and the `FTP`/`SFTPClient`/`Transport` `TYPE_CHECKING` imports (same reason). `Sequence`
is newly added to the `TYPE_CHECKING` block for `HandleProvider`.

- [ ] **Step 2: Confirm nothing else in `src/` still imports the removed names**

Run: `uv run python -c "import ast, pathlib; [print(p) for p in pathlib.Path('src').rglob('*.py') if any(n in p.read_text() for n in ('FTPProtocolBase', 'ProtocolEnum')) and 'ftp/types.py' not in str(p)]"`

Expected: no output (the only other place these names appeared was `adapter.py`, which Task 4 rewrites
next). This is a sanity check, not a real test -- don't treat empty output as a substitute for Task 4's
own verification.

---

## Task 4: `adapter.py` -- connectors, session base, pool base, concrete adapters, factory

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` (full replacement -- see below)

**Interfaces:**
- Consumes: `FTPCredentials`/`SFTPCredentials` (Task 1), `HandleProvider` (Task 3),
  `SFTPPool`/`Channel`/`TransportState`/`SFTPPool.drain_transports` (Task 2, already existed before this
  plan except `drain_transports`), `AdapterProtocol`/`ListDirResult`/`BufferSize`/`TransferSuccess`
  (Task 3, unchanged).
- Produces: `AdaptedFTP`, `AdaptedSFTP` (per-session transfer objects, now taking a `HandleProvider`
  instead of a protocol instance), `FTPAdapter`, `SFTPAdapter` (concrete pool classes, each with public
  `acquire()`/`release()` satisfying `HandleProvider` on themselves), `create_ftp_adapter` (overloaded
  factory function -- the main public entry point). Consumed by Task 5 (test fixtures) and Task 6/7
  (test rewrites).

This is the largest task in the plan and the point where nearly everything from the design spec comes
together. **There is no meaningful intermediate test-passing state within this task** -- `tests/ftp/`
still references the old protocol-based API until Task 5/6 land, and this file doesn't compile cleanly
until every piece below is in place (Task 3 already broke the old imports). Verify each step with
`uv run pyright src/aeth_ext/ftp/adapter.py` as you go rather than running tests, and don't be alarmed
that pytest is broken until Task 6 finishes.

### Step 1: Full replacement of `src/aeth_ext/ftp/adapter.py`

Write the entire file as follows. Read through the "Design notes" subsections after the code block --
they explain several deliberate choices (locking behavior, what got inlined vs. kept as a method, which
callback each keepalive/teardown method reuses) that matter for getting this right, not just for
understanding it after the fact.

```python
# pyright: reportImportCycles=false
# Standard library imports
from abc import ABC, abstractmethod
from contextlib import nullcontext
from datetime import datetime
from ftplib import FTP, FTP_TLS, _SSLSocket, all_errors  # type: ignore
from io import BytesIO
from logging import getLogger
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, overload, override

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SFTPError, SSHClient, SSHException

# First party imports
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.sftp_pool import Channel, SFTPPool, TransportState
from aeth_ext.ftp.types import AdapterProtocol, BufferSize, HandleProvider, ListDirResult, TransferSuccess
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Buffer, Callable, Iterator, Sequence
  from contextvars import ContextVar
  from types import TracebackType
  from typing import Any, Self
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.rich.progress import Progress


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP", "FTPAdapter", "SFTPAdapter", "create_ftp_adapter"]


_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError, SSHException)


def _is_connection_fatal(exc: BaseException | None) -> bool:
  if exc is None:
    return False
  return isinstance(exc, _CONNECTION_FATAL_TYPES)


# ---------------------------------------------------------------------------
# Connectors: private, credentials-driven connection-opening logic. Not a public extension point --
# HandleProvider (aeth_ext.ftp.types) is. Each FTPAdapter/SFTPAdapter builds exactly one of these from
# its credentials and holds it for its whole lifetime.
# ---------------------------------------------------------------------------


class _FTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: FTPCredentials) -> None:
    self._credentials = credentials

  def get_transport(self) -> None:
    return None  # no-op: FTP has no separate transport/channel tiers

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    conn = FTP_TLS() if self._credentials.use_tls else FTP()
    conn.connect(self._credentials.host, self._credentials.port, timeout=self._credentials.connect_timeout)
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    conn.set_pasv(self._credentials.passive_mode)
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    try:
      handle.quit()
    except OSError:
      handle.close()


class _SFTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: SFTPCredentials) -> None:
    self._credentials = credentials

  def get_transport(self) -> Transport:
    client = SSHClient()
    if self._credentials.known_hosts_path is not None:
      client.load_host_keys(str(self._credentials.known_hosts_path))
    else:
      client.load_system_host_keys()
    client.set_missing_host_key_policy(
      AutoAddPolicy() if self._credentials.host_key_policy == "auto_add" else RejectPolicy()
    )
    client.connect(
      self._credentials.host,
      port=self._credentials.port,
      username=self._credentials.username,
      password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
      key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
      passphrase=(
        self._credentials.private_key_passphrase.get_secret_value()
        if self._credentials.private_key_passphrase is not None
        else None
      ),
      timeout=self._credentials.connect_timeout,
    )
    transport = client.get_transport()
    assert transport is not None
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

  def close_conn_handler(self, handle: Transport) -> None:
    handle.close()


# ---------------------------------------------------------------------------
# Session lifecycle: shared between AdaptedFTP/AdaptedSFTP. Holds only the acquire/release/handler
# plumbing -- every transfer-protocol method (upload_file, download_file, transfer_file, ...) is defined
# directly on AdaptedFTP/AdaptedSFTP themselves, never here, so goto-definition on a transfer call always
# lands on the real implementation.
# ---------------------------------------------------------------------------


class _AdaptedSessionBase[HandleT]:
  __slots__ = ("_callbacks", "_provider", "chunk_size", "container_cls", "handler", "pbar", "tzinfo")

  def __init__(
    self,
    provider: HandleProvider[HandleT],
    *,
    container_cls: str | None,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    chunk_size: int = 8192,
    callbacks: Sequence[Callable[[bytes], Any]] = (),
  ) -> None:
    self.handler: HandleT | None = None
    self._provider = provider
    self._callbacks = tuple(callbacks)
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.chunk_size = chunk_size
    super().__init__()

  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler, callbacks = self._provider.acquire()
      self._callbacks = (*self._callbacks, *callbacks)
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self._provider.release(self.handler, _is_connection_fatal(exc_val))


class AdaptedFTP(_AdaptedSessionBase[FTP], AdapterProtocol):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      self.handler.voidcmd("TYPE I")  # Set binary mode
      with self.handler.transfercmd(f"STOR {remote_path}") as conn:
        with (
          self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          while buffer := callback(self.chunk_size):
            conn.sendall(buffer)
            for observer in self._callbacks:
              observer(buffer)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(buffer))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def download_file(self, remote_path: str, callback: Callable[[Buffer], Any], task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      self.handler.voidcmd("TYPE I")  # Set binary mode
      socket, size = self.handler.ntransfercmd(f"RETR {remote_path}")
      if size is None:
        size = self.handler.size(remote_path)
      with socket as conn:
        with (
          self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=size)
          if self.pbar is not None
          else nullcontext() as transfer_task
        ):
          while data := conn.recv(self.chunk_size):
            callback(data)
            for observer in self._callbacks:
              observer(data)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def transfer_file(
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    if isinstance(other, AdaptedFTP):
      return self._ftp_to_ftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    elif isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._ftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported other protocol: {other.__class__}")  # pyright: ignore[reportUnreachable]

  def _ftp_to_sftp(  # noqa: C901, PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    conn, source_file_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if source_file_size is None:
      try:
        source_file_size = self.handler.size(source_remote_path)
      except all_errors as e:
        logger.exception("%s: Failed to get source file size for %s", self.container_cls, source_remote_path, exc_info=e)
        source_file_size = None
    mem_stream = mem_stream or BytesIO()
    with (
      other.handler.open(dest_remote_path, mode="wb") as dest_file,
    ):
      with (
        self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        with conn as source_conn:
          while data := source_conn.recv(self.chunk_size):
            if callback is not None:
              callback(data)
            for observer in self._callbacks:
              observer(data)
            for observer in other._callbacks:  # pyright: ignore[reportPrivateUsage] -- destination-side SFTP instrumentation; other is the pool-injected callback owner, not a real API boundary
              observer(data)
            dest_file.write(data)
            mem_stream.write(data)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(data))
          if _SSLSocket is not None and isinstance(source_conn, _SSLSocket):
            source_conn.unwrap()  # type: ignore
        self.handler.voidresp()

      streamed_file_size = mem_stream.tell()
      try:
        dest_file_size = dest_file.tell()
      except Exception as e:
        dest_file_size = None
        logger.exception("%s: Failed to get destination file size after transfer", self.container_cls, exc_info=e)
        return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  def _ftp_to_ftp(  # noqa: C901, PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    socket, source_file_size = self.handler.ntransfercmd(f"RETR {source_remote_path}")
    if source_file_size is None:
      try:
        source_file_size = self.handler.size(source_remote_path)
      except all_errors:
        source_file_size = None
        logger.exception("%s: Failed to get source file size.", self.container_cls)
    mem_stream = mem_stream or BytesIO()
    with (
      self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
      if self.pbar is not None
      else nullcontext() as transfer_task
    ):
      self.handler.voidcmd("TYPE I")  # Set binary mode
      other.handler.voidcmd("TYPE I")  # Set binary mode
      with (
        socket as source_conn,
        other.handler.transfercmd(f"STOR {dest_remote_path}") as dest_conn,
      ):
        while data := source_conn.recv(self.chunk_size):
          if callback is not None:
            callback(data)
          for observer in self._callbacks:
            observer(data)
          dest_conn.sendall(data)
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None:
          if isinstance(source_conn, _SSLSocket):
            source_conn.unwrap()  # type: ignore
          if isinstance(dest_conn, _SSLSocket):
            dest_conn.unwrap()  # type: ignore
      self.handler.voidresp()
      other.handler.voidresp()
    streamed_file_size = mem_stream.tell()
    try:
      dest_file_size = other.handler.size(dest_remote_path)
    except all_errors:
      dest_file_size = None
      logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
      return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  @override
  def get_size(self, path: str) -> int | None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    return self.handler.size(path)

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.delete(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.mlsd(path):
      name, facts = entry
      if "modify" in facts:
        dt = datetime.strptime(facts["modify"], "%Y%m%d%H%M%S")  # noqa: DTZ007
        new_dt = dt.replace(tzinfo=self.tzinfo)
        yield ListDirResult(filename=name, modified_time=new_dt)

  @override
  def test_connection(self, logit: bool = False) -> bool:
    try:
      with self as ftp:
        assert isinstance(ftp.handler, FTP)
        ftp.handler.voidcmd("NOOP")
      return True
    except Exception:
      if logit:
        logger.exception("%s: Waiting FTP server is offline", self.container_cls)
      return False

  @override
  def makedir(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkd(remote_path)


class AdaptedSFTP(_AdaptedSessionBase[SFTPClient], AdapterProtocol):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with (
      self.handler.open(remote_path, mode="wb") as remote_file,
      (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size) if self.pbar is not None else nullcontext()
      ) as transfer_task,
    ):
      while buffer := callback(self.chunk_size):
        remote_file.write(buffer)
        for observer in self._callbacks:
          observer(buffer)
        if self.pbar is not None:
          assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
          self.pbar.update(transfer_task, advance=len(buffer))

  @override
  def download_file(self, remote_path: str, callback: Callable[[bytes], Any], task_msg: str = "") -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with self.handler.open(remote_path, mode="rb") as remote_file:
      size = remote_file.stat().st_size
      remote_file.prefetch(size)
      with (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=size)
        if self.pbar is not None
        else nullcontext() as transfer_task
      ):
        while data := remote_file.read(self.chunk_size):
          callback(data)
          for observer in self._callbacks:
            observer(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))

  @override
  def transfer_file(
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP | AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    if isinstance(other, AdaptedFTP):
      return self._sftp_to_ftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    elif isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._sftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported protocol kind: {other.__class__}")  # pyright: ignore[reportUnreachable]

  def _sftp_to_ftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    try:
      source_file_size = self.handler.stat(source_remote_path).st_size
    except SFTPError:
      source_file_size = None
      logger.exception("%s: Failed to get source file size for %s.", self.container_cls, source_remote_path)
    mem_stream = mem_stream or BytesIO()
    with (
      self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
      if self.pbar is not None
      else nullcontext() as transfer_task
    ):
      other.handler.voidcmd("TYPE I")  # Set binary mode
      with (
        other.handler.transfercmd(f"STOR {dest_remote_path}") as dest_conn,
        self.handler.open(source_remote_path, mode="rb") as source_file,
      ):
        while data := source_file.read(self.chunk_size):
          if callback is not None:
            callback(data)
          for observer in self._callbacks:
            observer(data)
          dest_conn.sendall(data)
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
        if _SSLSocket is not None and isinstance(dest_conn, _SSLSocket):
          dest_conn.unwrap()  # type: ignore
      other.handler.voidresp()
    streamed_file_size = mem_stream.tell()
    try:
      dest_file_size = other.handler.size(dest_remote_path)
    except all_errors:
      dest_file_size = None
      logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
      return False
    # all three file sizes should be equal
    result = (
      source_file_size == streamed_file_size == dest_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, streamed_file_size=%s, dest_file_size=%s",
        self.container_cls,
        source_file_size,
        streamed_file_size,
        dest_file_size,
      )
    return result

  def _sftp_to_sftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    assert other.handler is not None, "Other adapter must also be opened as a context manager"
    try:
      source_file_size = self.handler.stat(source_remote_path).st_size
    except SFTPError:
      source_file_size = None
      logger.exception("%s: Failed to get source file size for %s.", self.container_cls, source_remote_path)
    mem_stream = mem_stream or BytesIO()
    with other.handler.open(dest_remote_path, mode="wb") as dest_file:
      with (
        (
          self.pbar.add_task(task_msg or f"Transferring {source_remote_path}", total=source_file_size)
          if self.pbar is not None
          else nullcontext()
        ) as transfer_task,
        self.handler.open(source_remote_path, mode="rb") as source_file,
      ):
        while data := source_file.read(self.chunk_size):
          if callback is not None:
            callback(data)
          for observer in self._callbacks:
            observer(data)
          for observer in other._callbacks:
            observer(data)
          dest_file.write(data)
          mem_stream.write(data)
          if self.pbar is not None:
            assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
            self.pbar.update(transfer_task, advance=len(data))
      streamed_file_size = mem_stream.tell()
      try:
        dest_file_size = dest_file.tell()
      except Exception:
        dest_file_size = None
        logger.exception("%s: Failed to get destination file size after transfer.", self.container_cls)
        return False
    # all three file sizes should be equal
    result = (
      source_file_size == dest_file_size == streamed_file_size
      if source_file_size is not None
      else streamed_file_size == dest_file_size
    )
    if not result:
      logger.error(
        "%s: File size mismatch after transfer: source_file_size=%s, dest_file_size=%s, streamed_file_size=%s",
        self.container_cls,
        source_file_size,
        dest_file_size,
        streamed_file_size,
      )
    return result

  @override
  def get_size(self, path: str) -> int | None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      return self.handler.stat(path).st_size
    except SFTPError:
      logger.exception("%s: Failed to get file size for %s.", self.container_cls, path)
      return None

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.remove(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.listdir_iter(path):
      if entry.st_mtime is None:
        raise ValueError(f"Entry {entry.filename} does not have a modification time, cannot be used in _sftp_listdir")
      yield ListDirResult(filename=entry.filename, modified_time=datetime.fromtimestamp(entry.st_mtime, tz=self.tzinfo))

  @override
  def test_connection(self, logit: bool = False) -> bool:
    try:
      with self as sftp:
        assert isinstance(sftp.handler, SFTPClient)
        sftp.handler.listdir(".")
      return True
    except Exception:
      if logit:
        logger.exception("%s: Waiting SFTP server is offline.", self.container_cls)
      return False

  @override
  def makedir(self, remote_path: str) -> None:
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkdir(remote_path)


# ---------------------------------------------------------------------------
# Pool base: protocol-agnostic bookkeeping (size/ceiling/keepalive/shutdown) shared by FTPAdapter and
# SFTPAdapter. Everything that actually knows about FTP-vs-SFTP shapes (how to check out an idle handle,
# how to open a brand new one, how to release/discard) is abstract here and owned entirely by the
# concrete subclass -- no isinstance/issubclass branching anywhere in this file.
# ---------------------------------------------------------------------------


class _PooledAdapterBase[SessionT: AdapterProtocol, HandleT](ABC):
  __slots__ = (
    "__weakref__",  # pyright: ignore[reportUninitializedInstanceVariable] -- CPython-managed, never assigned directly
    "_current_size",
    "_discovered_max",
    "_discovered_max_last_probe",
    "_keepalive_interval",
    "_keepalive_stop",
    "_keepalive_thread",
    "_registered_for_shutdown",
    "_size_lock",
    "chunk_size",
    "container_cls",
    "container_cvar",
    "max_connections",
    "pbar",
    "tzinfo",
  )

  _REPROBE_INTERVAL: ClassVar[float] = 300.0

  def __init__(
    self,
    *,
    max_connections: int,
    chunk_size: int,
    pbar: Progress | None,
    tzinfo: ZoneInfo | None,
    container_cls: str | None,
    container_cvar: ContextVar[str] | None,
    keepalive_interval: float | None,
  ) -> None:
    self.max_connections = max_connections
    self.chunk_size = chunk_size
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.container_cls = container_cls
    self.container_cvar = container_cvar
    self._keepalive_interval = keepalive_interval

    # Standard library imports
    from threading import Lock

    self._current_size = 0
    self._size_lock = Lock()
    self._discovered_max: int | None = None
    self._discovered_max_last_probe: float = 0.0
    self._keepalive_thread = None
    self._keepalive_stop = None
    self._registered_for_shutdown = False

    super().__init__()

  def _effective_ceiling(self) -> int:
    if self._discovered_max is None:
      return self.max_connections
    if monotonic() - self._discovered_max_last_probe >= self._REPROBE_INTERVAL:
      return self.max_connections  # allow one probe past the discovered ceiling
    return min(self.max_connections, self._discovered_max)

  def _open_new_slot[T](self, dial: Callable[[], T]) -> T | None:
    """Ceiling-checked size-lock bookkeeping around opening a brand new low-level connection (a new
    `FTP` object, or a new `Transport`) -- the one piece of connection-establishment that's genuinely
    identical between FTP and SFTP. Does NOT decide *whether* a new connection needs to be opened at
    all (SFTPAdapter has a branch this can't represent: opening a new channel on an existing under-cap
    Transport needs no new slot and no ceiling check) -- that decision belongs entirely to each
    subclass's own `acquire()`.

    `dial` is called while `_size_lock` is held, matching this code's pre-existing locking behavior
    (this is not a new constraint introduced by this refactor) -- growth is serialized pool-wide, not
    just the counter increment.
    """
    with self._size_lock:
      if self._current_size >= self._effective_ceiling():
        return None
      self._current_size += 1
      self._ensure_registered_for_shutdown()
      try:
        result = dial()
      except ConnectionRefusedError, TimeoutError, OSError:
        self._current_size -= 1
        self._discovered_max = self._current_size
        self._discovered_max_last_probe = monotonic()
        raise
      else:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size
        return result

  def start_session(self) -> SessionT:
    try:
      if self.container_cvar is not None:
        container_cls = self.container_cvar.get()
      else:
        container_cls = self.container_cls
    except LookupError:
      container_cls = self.container_cls
    return self._build_session(container_cls)

  def test_connection(self, logit: bool = False) -> bool:
    return self.start_session().test_connection(logit)

  def _keepalive_loop(self) -> None:
    while not self._keepalive_stop.wait(timeout=self._keepalive_interval):  # pyright: ignore[reportOptionalMemberAccess]
      self._keepalive_check_one()

  def _ensure_keepalive_started(self) -> None:
    if self._keepalive_interval is None or self._keepalive_thread is not None:
      return
    # Standard library imports
    from threading import Event, Thread

    self._keepalive_stop = Event()
    self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
    self._keepalive_thread.start()

  def _shutdown_teardown(self) -> None:
    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      if self._keepalive_thread is not None:
        self._keepalive_thread.join(timeout=2.0)
    self._teardown_idle()

  def _ensure_registered_for_shutdown(self) -> None:
    if self._registered_for_shutdown:
      return
    self._registered_for_shutdown = True
    register_for_shutdown(self._shutdown_teardown, phase=ShutdownPhase.THREADED)

  # --- abstract, subclass-owned: no runtime type checks here or in any subclass implementation, since
  # each subclass statically knows its own HandleT/SessionT. acquire/release are public because
  # FTPAdapter/SFTPAdapter structurally satisfy HandleProvider[HandleT] through them, so `self` can be
  # passed directly as a session's provider (see _build_session in each subclass). ---

  @abstractmethod
  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]: ...

  @abstractmethod
  def release(self, handle: HandleT, is_fatal: bool) -> None: ...

  @abstractmethod
  def _validate(self, handle: HandleT) -> bool: ...

  @abstractmethod
  def _build_session(self, container_cls: str | None) -> SessionT: ...

  @abstractmethod
  def _keepalive_check_one(self) -> None: ...

  @abstractmethod
  def _teardown_idle(self) -> None: ...


class FTPAdapter(_PooledAdapterBase[AdaptedFTP, FTP]):
  __slots__ = ("_connector", "_idle")

  def __init__(
    self,
    credentials: FTPCredentials,
    *,
    max_connections: int = 16,
    chunk_size: int = 8192,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cls: str | None = None,
    container_cvar: ContextVar[str] | None = None,
    keepalive_interval: float | None = None,
  ) -> None:
    super().__init__(
      max_connections=max_connections,
      chunk_size=chunk_size,
      pbar=pbar,
      tzinfo=tzinfo,
      container_cls=container_cls,
      container_cvar=container_cvar,
      keepalive_interval=keepalive_interval,
    )
    self._connector = _FTPConnector(credentials)

    # Standard library imports
    from queue import Queue

    self._idle: Queue[FTP] = Queue(maxsize=max_connections)

  @override
  def acquire(self) -> tuple[FTP, Sequence[Callable[[bytes], Any]]]:
    # Standard library imports
    from queue import Empty

    try:
      candidate = self._idle.get_nowait()
    except Empty:
      candidate = None
    if candidate is not None and not self._validate(candidate):
      self.release(candidate, is_fatal=True)
      candidate = None

    handle = candidate
    if handle is None:
      handle = self._open_new_slot(lambda: self._connector.request_handler(self._connector.get_transport()))
    if handle is None:
      handle = self._idle.get()

    self._ensure_keepalive_started()
    return handle, ()

  @override
  def release(self, handle: FTP, is_fatal: bool) -> None:
    if is_fatal:
      with self._size_lock:
        self._current_size -= 1
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
        pass
    else:
      self._idle.put(handle)

  @override
  def _validate(self, handle: FTP) -> bool:
    try:
      handle.voidcmd("NOOP")
      return True
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False

  @override
  def _keepalive_check_one(self) -> None:
    # Standard library imports
    from queue import Empty

    try:
      handle = self._idle.get_nowait()
    except Empty:
      return
    self.release(handle, is_fatal=not self._validate(handle))

  @override
  def _teardown_idle(self) -> None:
    # Standard library imports
    from queue import Empty

    while True:
      try:
        handle = self._idle.get_nowait()
      except Empty:
        break
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close during teardown
        pass

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedFTP:
    return AdaptedFTP(self, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)


class SFTPAdapter(_PooledAdapterBase[AdaptedSFTP, SFTPClient]):
  __slots__ = ("_connector", "_sftp_pool", "channels_per_transport")

  def __init__(
    self,
    credentials: SFTPCredentials,
    *,
    max_connections: int = 16,
    chunk_size: int = 8192,
    pbar: Progress | None = None,
    tzinfo: ZoneInfo | None = SETTINGS.tz,
    container_cls: str | None = None,
    container_cvar: ContextVar[str] | None = None,
    keepalive_interval: float | None = None,
    channels_per_transport: int = 4,
  ) -> None:
    super().__init__(
      max_connections=max_connections,
      chunk_size=chunk_size,
      pbar=pbar,
      tzinfo=tzinfo,
      container_cls=container_cls,
      container_cvar=container_cvar,
      keepalive_interval=keepalive_interval,
    )
    self._connector = _SFTPConnector(credentials)
    self.channels_per_transport = channels_per_transport
    self._sftp_pool = SFTPPool(channels_per_transport)

  @override
  def acquire(self) -> tuple[SFTPClient, Sequence[Callable[[bytes], Any]]]:
    channel = self._sftp_pool.checkout_channel()
    if channel is not None and not self._validate(channel.handle):
      self._discard(channel)
      channel = None

    if channel is None:
      target = self._sftp_pool.pick_growth_target()
      if target is not None:
        channel_handle = self._connector.request_handler(target.transport)
        target.channel_count += 1
        self._sftp_pool.track_handle(channel_handle, target)
        self._sftp_pool.mark_checked_out()
        channel = Channel(handle=channel_handle, state=target)
      else:
        transport = self._open_new_slot(self._connector.get_transport)
        if transport is not None:
          state = self._sftp_pool.register_transport(transport)
          channel_handle = self._connector.request_handler(transport)
          state.channel_count += 1
          self._sftp_pool.track_handle(channel_handle, state)
          self._sftp_pool.mark_checked_out()
          channel = Channel(handle=channel_handle, state=state)

    if channel is None:
      channel = self._sftp_pool.checkout_channel_blocking()

    self._ensure_keepalive_started()
    return channel.handle, (self._make_instrument(channel.state),)

  @override
  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    state = self._sftp_pool.state_for_handle(handle)
    assert state is not None, "handle must have been tracked via SFTPPool.track_handle at checkout"
    channel = Channel(handle=handle, state=state)
    if not is_fatal:
      if self._sftp_pool.release_channel(channel):
        try:
          channel.handle.close()
        except Exception:  # noqa: BLE001, S110 -- best-effort close of a popped, still-healthy channel
          pass
      return

    if state.transport.is_active():
      self._discard(channel)
      return

    orphaned = self._sftp_pool.discard_transport(state)
    for orphan in (channel, *orphaned):
      try:
        orphan.handle.close()
      except Exception:  # noqa: BLE001, S110 -- best-effort close during transport-death cleanup
        pass
    with self._size_lock:
      self._current_size -= 1

  def _discard(self, channel: Channel) -> None:
    self._sftp_pool.discard_channel(channel)
    try:
      channel.handle.close()
    except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
      pass

  @override
  def _validate(self, handle: SFTPClient) -> bool:
    try:
      handle.listdir(".")
      return True
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False

  @override
  def _keepalive_check_one(self) -> None:
    channel = self._sftp_pool.checkout_channel()
    if channel is None:
      return
    self.release(channel.handle, is_fatal=not self._validate(channel.handle))

  @override
  def _teardown_idle(self) -> None:
    for transport in self._sftp_pool.drain_transports():
      try:
        self._connector.close_conn_handler(transport)
      except Exception:  # noqa: BLE001, S110 -- best-effort close during teardown
        pass

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedSFTP:
    return AdaptedSFTP(self, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)

  def _make_instrument(self, state: TransportState) -> Callable[[bytes], None]:
    last = [monotonic()]

    def observer(data: bytes) -> None:
      now = monotonic()
      elapsed = now - last[0]
      last[0] = now
      state.update_throughput(len(data), elapsed)

    return observer


@overload
def create_ftp_adapter(
  credentials: FTPCredentials,
  *,
  max_connections: int = 16,
  chunk_size: int = 8192,
  pbar: Progress | None = None,
  tzinfo: ZoneInfo | None = SETTINGS.tz,
  container_cls: str | None = None,
  container_cvar: ContextVar[str] | None = None,
  keepalive_interval: float | None = None,
) -> FTPAdapter: ...
@overload
def create_ftp_adapter(
  credentials: SFTPCredentials,
  *,
  max_connections: int = 16,
  chunk_size: int = 8192,
  pbar: Progress | None = None,
  tzinfo: ZoneInfo | None = SETTINGS.tz,
  container_cls: str | None = None,
  container_cvar: ContextVar[str] | None = None,
  keepalive_interval: float | None = None,
  channels_per_transport: int = 4,
) -> SFTPAdapter: ...
def create_ftp_adapter(credentials: FTPCredentials | SFTPCredentials, **kwargs: Any) -> FTPAdapter | SFTPAdapter:
  if isinstance(credentials, FTPCredentials):
    return FTPAdapter(credentials, **kwargs)
  return SFTPAdapter(credentials, **kwargs)
```

### Design notes for this task

- **`_open_new_slot` holds `_size_lock` across the entire dial**, including the actual network I/O
  (`dial()` is called while the lock is held). This exactly preserves the current code's behavior
  (`_grow_ftp`/`_grow_sftp` both dial while holding `_size_lock` today) -- it is **not** a new
  concurrency change introduced by this refactor. Don't "improve" this by releasing the lock before
  dialing; that would change observable pool behavior (connection growth would no longer be serialized
  pool-wide) beyond what this plan's spec covers.
- **`FTPAdapter.acquire()` inlines** what the design spec originally sketched as separate
  `_checkout_idle`/`_dial`/`_instrumentation_callbacks` helpers, per the Global Constraints' abstraction
  rule -- each would have been a single-call-site, 1-4 line body. `SFTPAdapter.acquire()` inlines the
  same way for its own checkout/growth logic. `_discard` on `SFTPAdapter` and `_validate`/
  `_make_instrument` on both classes stay as their own methods because they're either reused across 2+
  call sites (`_discard`, `_validate`) or build a genuinely standalone stateful object
  (`_make_instrument` returns a closure, not just a computed value).
- **`_keepalive_check_one` reuses `release()`** on both subclasses rather than reimplementing its own
  discard-or-requeue logic -- pops one idle handle (bypassing the blocking/waiting semantics `acquire()`
  has to respect, since a keepalive tick should never contend with a real caller for a handle), then
  calls `self.release(handle, is_fatal=not self._validate(handle))`. This is the fix for a latent gap in
  the pre-refactor code: `_keepalive_loop`/`_shutdown_teardown` used to touch only the flat `_idle`
  queue (FTP-only), so SFTP pooled channels never got periodic keepalive pings or shutdown cleanup at
  all. `SFTPAdapter._keepalive_check_one`/`_teardown_idle` now do real work.
- **`close_conn_handler` is what `release()`'s discard branch and `_teardown_idle` call**, not a bare
  `.close()` -- for FTP this is what gives discard a graceful `.quit()` attempt. For SFTP,
  `_SFTPConnector.close_conn_handler` is only ever invoked on whole `Transport`s (from `_teardown_idle`,
  via `drain_transports()`), never on a per-channel discard -- closing one `SFTPClient` channel is just
  `channel.handle.close()` in `release()`/`_discard`, no connector/credentials involvement needed.
- **`_SFTPConnector.get_transport()` builds the connection through `SSHClient`**, not a bare
  `Transport()`, purely as an internal implementation detail -- `AutoAddPolicy`/`RejectPolicy` are
  `SSHClient`-only concepts; a raw `Transport.connect()` has no host-key-policy mechanism at all. Only
  `client.get_transport()`'s result is returned; the `SSHClient` wrapper itself is never stored, since
  closing the extracted `Transport` later closes the same underlying connection `SSHClient.close()`
  would. `key_filename=` lets paramiko auto-detect the private key's type (RSA/Ed25519/ECDSA) instead of
  the connector guessing which `paramiko.PKey` subclass to instantiate.
- **The `container_cls` parameter on `_AdaptedSessionBase.__init__` is `str | None`**, not `str` like
  the old `AdaptedFTP.__init__`/`AdaptedSFTP.__init__` had it. The old, narrower `str` annotation was
  already wrong (the old `FTPAdapter.start_session()` always passed a `str | None` value there, papered
  over with a `# pyright: ignore[reportArgumentType]`). Since `_build_session` on each concrete subclass
  now has a fully concrete (non-union) signature, pyright can verify this call site correctly without
  any ignore comment -- if you find yourself wanting to add one here, something upstream is wrong.
- **`AdaptedFTP`/`AdaptedSFTP` still reach into `other._callbacks`** in `_ftp_to_sftp`/`_sftp_to_sftp`
  (destination-side SFTP instrumentation, added in a prior session). This is unchanged by this refactor
  -- `_callbacks` still exists as an attribute (now defined on `_AdaptedSessionBase`), so this keeps
  working exactly as before.
- **`overload`/`ABC`/`abstractmethod` must be real (non-`TYPE_CHECKING`) imports** -- they're used as
  decorators/base classes at class/function definition time, not just in annotations.
  **`Transport` stays `TYPE_CHECKING`-only** -- it's never constructed or `isinstance`-checked in this
  file (only used as a type annotation on `_SFTPConnector`'s methods), matching the same treatment
  `Buffer`/`Iterator`/`ZoneInfo`/`Progress` etc. already get in this file. `SFTPClient` stays a *real*
  import (unchanged from before) since it's both `isinstance`-checked (`test_connection`) and
  constructed via `SFTPClient.from_transport(...)`.

### Step 2: Verify types

Run: `uv run pyright src/aeth_ext/ftp/adapter.py`
Expected: 0 errors, 0 warnings, 0 informations. Fix anything that comes up before moving on -- don't
carry pyright errors into Task 5.

### Step 3: Verify lint

Run: `uv run ruff check src/aeth_ext/ftp/adapter.py`
Expected: "All checks passed!" (aside from possibly needing `# noqa` comments matching the ones already
present in the code above -- they're deliberate, carried over from the pre-refactor file, or newly
justified per the Design notes).

---

## Task 5: `tests/ftp/conftest.py` -- credential-driven fixtures

**Files:**
- Modify: `tests/ftp/conftest.py` (full replacement below)

**Interfaces:**
- Consumes: `create_ftp_adapter`, `FTPAdapter`, `SFTPAdapter`, `AdaptedFTP`, `AdaptedSFTP` (Task 4),
  `FTPCredentials`, `SFTPCredentials` (Task 1).
- Produces: `make_ftp_adapter: Callable[[], AdaptedFTP]`, `make_sftp_adapter: Callable[[], AdaptedSFTP]`
  fixtures (same names/shapes as before -- Tasks 6/7 and any future test file can keep using them
  unchanged), plus `ftp_env: _FTPTestEnv`, `sftp_env: _SFTPTestEnv` fixtures, `fake_progress:
  FakeProgress` (unchanged), and a small standalone `HandleProvider` test double
  (`_OneShotFTPProvider`/`_OneShotSFTPProvider`) for Task 6's standalone-usage test.

The current `_TestFTPProtocol`/`_TestSFTPProtocol` classes implement the *old* `FTPProtocol`/
`SFTPProtocol` shape (`get_transport`/`request_channel`/`close_conn_handler`, no-arg constructor style)
and construct `AdaptedFTP`/`AdaptedSFTP` by passing a protocol *instance* as the first positional arg.
The new shape needs a `HandleProvider` instead: an object with `acquire() -> tuple[Handle,
Sequence[Callable]]` / `release(handle, is_fatal) -> None`, passed to `AdaptedFTP`/`AdaptedSFTP` as
`provider`. Since these fixtures build genuinely standalone (non-pooled) sessions today (`make_adapter`
constructs `AdaptedFTP(protocol, container_cls=...)` directly, no `FTPAdapter` involved), they're the
natural first real usage of the new standalone-provider story from spec Section 4 -- not routed through
`create_ftp_adapter`/`FTPAdapter` at all.

- [ ] **Step 1: Replace `tests/ftp/conftest.py` in full**

```python
"""Shared pytest fixtures for the FTP/SFTP adapter test suite.

Both fixtures run a real local server (a real `pyftpdlib` FTP server, a real
loopback `paramiko` SSH/SFTP server) rather than faking sockets, so the tests
exercise the actual `ftplib`/`paramiko` wire protocols `aeth_ext.ftp.adapter`
talks to.
"""

# Standard library imports
import contextlib
import os
import socket
import threading
import uuid
from typing import TYPE_CHECKING, override

# Third party imports
import paramiko
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

# First party imports
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Sequence
  from ftplib import FTP
  from pathlib import Path
  from typing import Any


# ---------------------------------------------------------------------------
# FTP: one shared pyftpdlib server per test, isolated via a unique user+homedir.
# ---------------------------------------------------------------------------


class _OneShotFTPProvider:
  """Minimal `HandleProvider[FTP]`-shaped test double: connects/disconnects a real `ftplib.FTP`
  directly against a known host/port/user, without going through `FTPAdapter`/`create_ftp_adapter` --
  exercises the standalone (non-pooled) usage path `AdaptedFTP` supports."""

  def __init__(self, port: int, username: str, password: str) -> None:
    self._port = port
    self._username = username
    self._password = password
    self._conn: FTP | None = None

  def acquire(self) -> tuple[FTP, Sequence[Callable[[bytes], Any]]]:
    # Standard library imports
    from ftplib import FTP

    conn = FTP()
    conn.connect("127.0.0.1", self._port)
    conn.login(self._username, self._password)
    self._conn = conn
    return conn, ()

  def release(self, handle: FTP, is_fatal: bool) -> None:  # noqa: FBT001, ARG002 -- matches HandleProvider's shape
    try:
      handle.quit()
    except OSError:
      handle.close()
    self._conn = None


class _FTPTestEnv:
  def __init__(self, port: int, authorizer: DummyAuthorizer, root: Path) -> None:
    self._port = port
    self._authorizer = authorizer
    self._root = root

  def make_adapter(self, container_cls: str = "test") -> AdaptedFTP:
    name = uuid.uuid4().hex
    homedir = self._root / name
    homedir.mkdir()
    username, password = f"user_{name}", "password"
    self._authorizer.add_user(username, password, str(homedir), perm="elradfmwMT")
    provider = _OneShotFTPProvider(self._port, username, password)
    return AdaptedFTP(provider, container_cls=container_cls)


@pytest.fixture
def ftp_env(tmp_path: Path) -> Iterator[_FTPTestEnv]:
  root = tmp_path / "ftp_root"
  root.mkdir()

  authorizer = DummyAuthorizer()

  class _Handler(FTPHandler):
    pass

  _Handler.authorizer = authorizer

  server = FTPServer(("127.0.0.1", 0), _Handler)
  port = server.address[1]
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()

  try:
    yield _FTPTestEnv(port, authorizer, root)
  finally:
    server.close_all()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# SFTP: a fresh loopback paramiko server per adapter, rooted at a real directory.
# ---------------------------------------------------------------------------

_SFTP_OK: int = paramiko.SFTP_OK  # pyright: ignore[reportAttributeAccessIssue]


class _StubServerInterface(paramiko.ServerInterface):
  @override
  def check_auth_password(self, username: str, password: str) -> int:
    return paramiko.AUTH_SUCCESSFUL  # pyright: ignore[reportAttributeAccessIssue]

  @override
  def check_channel_request(self, kind: str, chanid: int) -> int:
    return paramiko.OPEN_SUCCEEDED  # pyright: ignore[reportAttributeAccessIssue]

  @override
  def get_allowed_auths(self, username: str) -> str:
    return "password"


class _StubSFTPHandle(paramiko.SFTPHandle):
  """An `SFTPHandle` that answers FSTAT requests via the real open file descriptor."""

  @override
  def stat(self) -> paramiko.SFTPAttributes:
    return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))  # pyright: ignore[reportAttributeAccessIssue]


def _make_stub_sftp_server(root: str) -> type[paramiko.SFTPServerInterface]:  # noqa: C901
  """Build an `SFTPServerInterface` rooted at `root` on the real filesystem."""

  class _StubSFTPServer(paramiko.SFTPServerInterface):
    def _realpath(self, path: str) -> str:
      return root + self.canonicalize(path)

    @override
    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes]:
      p = self._realpath(path)
      out: list[paramiko.SFTPAttributes] = []
      for fname in os.listdir(p):
        attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(p, fname)))
        attr.filename = fname
        out.append(attr)
      return out

    @override
    def stat(self, path: str) -> paramiko.SFTPAttributes:
      try:
        return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))
      except OSError as e:
        return paramiko.SFTPServer.convert_errno(e.errno or 0)  # pyright: ignore[reportReturnType]

    @override
    def lstat(self, path: str) -> paramiko.SFTPAttributes:
      return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))

    @override
    def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes) -> paramiko.SFTPHandle:
      p = self._realpath(path)
      flags |= getattr(os, "O_BINARY", 0)
      mode = getattr(attr, "st_mode", None) or 0o666
      fd = os.open(p, flags, mode)
      f = os.fdopen(fd, "rb" if (flags & os.O_WRONLY) == 0 and (flags & os.O_RDWR) == 0 else "wb")
      handle = _StubSFTPHandle(flags)
      handle.readfile = f  # pyright: ignore[reportAttributeAccessIssue]
      handle.writefile = f  # pyright: ignore[reportAttributeAccessIssue]
      return handle

    @override
    def remove(self, path: str) -> int:
      os.remove(self._realpath(path))
      return _SFTP_OK

    @override
    def rename(self, oldpath: str, newpath: str) -> int:
      os.rename(self._realpath(oldpath), self._realpath(newpath))
      return _SFTP_OK

    @override
    def mkdir(self, path: str, attr: paramiko.SFTPAttributes) -> int:
      os.mkdir(self._realpath(path))
      return _SFTP_OK

  return _StubSFTPServer


class _OneShotSFTPProvider:
  """Minimal `HandleProvider[SFTPClient]`-shaped test double: connects/disconnects a real
  `paramiko.SFTPClient` directly against a known port, without going through
  `SFTPAdapter`/`create_ftp_adapter`."""

  def __init__(self, port: int) -> None:
    self._port = port
    self._transport: paramiko.Transport | None = None

  def acquire(self) -> tuple[paramiko.SFTPClient, Sequence[Callable[[bytes], Any]]]:
    transport = paramiko.Transport(("127.0.0.1", self._port))
    transport.connect(username="anyone", password="anything")
    self._transport = transport
    return paramiko.SFTPClient.from_transport(transport), ()  # pyright: ignore[reportReturnType]

  def release(self, handle: paramiko.SFTPClient, is_fatal: bool) -> None:  # noqa: FBT001, ARG002 -- matches HandleProvider's shape
    handle.close()
    if self._transport is not None:
      self._transport.close()
      self._transport = None


class _SFTPTestEnv:
  def __init__(self, root: Path) -> None:
    self._root = root
    self._servers: list[paramiko.Transport] = []
    self._listeners: list[socket.socket] = []

  def make_adapter(self, container_cls: str = "test") -> AdaptedSFTP:
    name = uuid.uuid4().hex
    homedir = self._root / name
    homedir.mkdir()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    self._listeners.append(listener)

    host_key = paramiko.RSAKey.generate(2048)
    sftp_si = _make_stub_sftp_server(str(homedir))
    servers = self._servers

    def _serve() -> None:
      try:
        conn, _addr = listener.accept()
      except OSError:
        # The listener was closed during test teardown before a client ever
        # connected (e.g. a test that intentionally never opens this adapter).
        return
      transport = paramiko.Transport(conn)
      servers.append(transport)
      transport.add_server_key(host_key)
      transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_si=sftp_si)
      transport.start_server(server=_StubServerInterface())

    threading.Thread(target=_serve, daemon=True).start()

    provider = _OneShotSFTPProvider(port)
    return AdaptedSFTP(provider, container_cls=container_cls)

  def close(self) -> None:
    for server in self._servers:
      server.close()
    for listener in self._listeners:
      listener.close()


@pytest.fixture
def sftp_env(tmp_path: Path) -> Iterator[_SFTPTestEnv]:
  root = tmp_path / "sftp_root"
  root.mkdir()

  env = _SFTPTestEnv(root)
  try:
    yield env
  finally:
    env.close()


@pytest.fixture
def make_ftp_adapter(ftp_env: _FTPTestEnv) -> Callable[[], AdaptedFTP]:
  return ftp_env.make_adapter


@pytest.fixture
def make_sftp_adapter(sftp_env: _SFTPTestEnv) -> Callable[[], AdaptedSFTP]:
  return sftp_env.make_adapter


# ---------------------------------------------------------------------------
# Progress-bar spy, matching the `pbar.add_task(...)` / `pbar.update(...)` shape
# `AdaptedFTP`/`AdaptedSFTP` call against `aeth_ext.rich.progress.Progress`.
# ---------------------------------------------------------------------------


class FakeProgress:
  def __init__(self) -> None:
    self.tasks: list[tuple[str, int | None]] = []
    self.updates: list[tuple[int, int]] = []

  def add_task(self, description: str, total: int | None = None) -> contextlib.AbstractContextManager[int]:
    task_id = len(self.tasks)
    self.tasks.append((description, total))
    return contextlib.nullcontext(task_id)

  def update(self, task_id: int, advance: int) -> None:
    self.updates.append((task_id, advance))


@pytest.fixture
def fake_progress() -> FakeProgress:
  return FakeProgress()
```

Note what changed relative to the current file: `_TestFTPProtocol`/`_TestSFTPProtocol` (old
`FTPProtocol`/`SFTPProtocol`-shaped, `get_transport`/`request_channel`/`close_conn_handler`, no-arg
construction) are replaced by `_OneShotFTPProvider`/`_OneShotSFTPProvider` (`HandleProvider`-shaped,
`acquire`/`release`, still real per-test host/port/credentials passed at construction -- the actual
connection logic is nearly identical, just reshaped to the new two-method contract). `make_adapter()`
now passes the provider as `AdaptedFTP(provider, container_cls=...)`/`AdaptedSFTP(provider,
container_cls=...)` -- same call shape as before, just a provider object instead of a protocol instance.
The `ProtocolEnum` import is gone (no longer exists). `AdaptedFTP`/`AdaptedSFTP` import stays the same.

- [ ] **Step 2: Verify types**

Run: `uv run pyright tests/ftp/conftest.py`
Expected: 0 errors, 0 warnings.

- [ ] **Step 3: Verify lint**

Run: `uv run ruff check tests/ftp/conftest.py`
Expected: "All checks passed!"

- [ ] **Step 4: Confirm the fixtures work end-to-end**

Run: `uv run pytest tests/ftp/test_transfer.py -v --no-cov`
Expected: this file doesn't reference the old protocol API at all (it only uses `make_ftp_adapter`/
`make_sftp_adapter`/`fake_progress`), so it should now pass unmodified against the new fixtures. If
anything fails, the failure is real -- investigate rather than assuming the test needs updating (Task 7
covers `test_transfer.py` explicitly, but this step is where you'll actually discover whether it needs
any changes at all).

---

## Task 6: `tests/ftp/test_ftp_adapter_factory.py` -- rewrite for the new API

**Files:**
- Modify: `tests/ftp/test_ftp_adapter_factory.py` (full replacement below)

**Interfaces:**
- Consumes: `create_ftp_adapter`, `FTPAdapter`, `SFTPAdapter`, `AdaptedFTP`, `AdaptedSFTP` (Task 4),
  `FTPCredentials`, `SFTPCredentials` (Task 1), `_make_stub_sftp_server`, `_StubServerInterface` (Task 5,
  already private/test-only, imported the same way the current file does).

This file currently builds `type[FTPProtocol]`/`type[SFTPProtocol]` test doubles and passes them to
`FTPAdapter(protocol_cls, ...)`. The new equivalent is a `FTPCredentials`/`SFTPCredentials` instance
passed to `create_ftp_adapter(credentials, ...)`. Every test's *intent* stays the same (pool reuse,
growth/ceiling discovery, keepalive, shutdown, chunk size, constructor callbacks, SFTP multiplexing,
throughput instrumentation, transport-death cascade) -- only the construction mechanics change. Several
tests monkeypatch a protocol class's `get_transport` method to simulate connection failures; the
equivalent now is monkeypatching the relevant connector's `get_transport`/`request_handler` (accessible
via the adapter's `_connector` attribute, or by patching the class method directly the same way the
current tests already do via `protocol_cls.get_transport = ...`).

- [ ] **Step 1: Replace `tests/ftp/test_ftp_adapter_factory.py` in full**

```python
"""Tests for `aeth_ext.ftp.adapter.create_ftp_adapter`/`FTPAdapter`/`SFTPAdapter`."""

# Standard library imports
import socket
import threading
from contextvars import ContextVar
from ftplib import FTP
from typing import TYPE_CHECKING

# Third party imports
import paramiko
import pytest
from paramiko import Transport

# First party imports
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP, FTPAdapter, SFTPAdapter, create_ftp_adapter
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from tests.ftp.conftest import _make_stub_sftp_server, _StubServerInterface  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # First party imports
  from tests.ftp.conftest import _FTPTestEnv  # pyright: ignore[reportPrivateUsage]


class TestFactoryDispatch:
  def test_ftp_credentials_produce_an_ftp_adapter(self) -> None:
    creds = FTPCredentials(host="127.0.0.1", username="anyone", password="anything", port=1)
    adapter = create_ftp_adapter(creds)

    assert isinstance(adapter, FTPAdapter)

  def test_sftp_credentials_produce_an_sftp_adapter(self) -> None:
    creds = SFTPCredentials(host="127.0.0.1", username="anyone", password="anything", port=1)
    adapter = create_ftp_adapter(creds)

    assert isinstance(adapter, SFTPAdapter)


class TestContainerClsResolution:
  def test_plain_string_is_used_directly(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="explicit-name")

    session = adapter.start_session()

    assert session.container_cls == "explicit-name"

  def test_contextvar_is_preferred_when_set(self, ftp_env: _FTPTestEnv) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar")
    cvar.set("from-contextvar")
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "from-contextvar"

  def test_falls_back_to_plain_string_when_contextvar_is_unset(self, ftp_env: _FTPTestEnv) -> None:
    cvar: ContextVar[str] = ContextVar("test_container_cvar_unset")
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "fallback-name"


class TestTestConnectionDelegation:
  def test_delegates_to_a_fresh_sessions_test_connection(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """`FTPAdapter.test_connection` should be a thin delegate to
    `start_session().test_connection(logit)` -- exercised here via a spy
    rather than a real connection, since the real-connection path is already
    covered end-to-end by `AdaptedFTP`/`AdaptedSFTP`'s own test suites."""
    calls: list[bool] = []

    class _FakeSession:
      def test_connection(self, logit: bool = False) -> bool:
        calls.append(logit)
        return True

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))
    # FTPAdapter is __slots__-based, so an instance can't shadow a class method --
    # patch the class itself instead.
    monkeypatch.setattr(FTPAdapter, "start_session", lambda self: _FakeSession())

    result = adapter.test_connection(logit=True)

    assert result is True
    assert calls == [True]


def _ftp_credentials(ftp_env: _FTPTestEnv) -> FTPCredentials:
  """Adapts `_FTPTestEnv` (which builds ready-made `AdaptedFTP`s) into `FTPCredentials` for a fresh
  user+homedir on the same running pyftpdlib server, so these tests can exercise
  `create_ftp_adapter`/`FTPAdapter` itself rather than a pre-built adapter."""
  # Standard library imports
  import uuid

  port = ftp_env._port  # pyright: ignore[reportPrivateUsage]
  authorizer = ftp_env._authorizer  # pyright: ignore[reportPrivateUsage]
  root = ftp_env._root  # pyright: ignore[reportPrivateUsage]

  name = uuid.uuid4().hex
  homedir = root / name
  homedir.mkdir()
  username, password = f"user_{name}", "password"
  authorizer.add_user(username, password, str(homedir), perm="elradfmwMT")

  return FTPCredentials(host="127.0.0.1", username=username, password=password, port=port)


class TestConnectionPooling:
  def test_release_returns_connection_for_reuse(self, ftp_env: _FTPTestEnv) -> None:
    """Releasing a session should make the underlying connection available to
    the next start_session() call, not close it."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      first_handler = first.handler

    with adapter.start_session() as second:
      second_handler = second.handler

    assert second_handler is first_handler

  def test_concurrent_checkouts_up_to_max_connections_do_not_block(self, ftp_env: _FTPTestEnv) -> None:
    max_connections = 3
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=max_connections)

    sessions = [adapter.start_session() for _ in range(max_connections)]
    for s in sessions:
      s.__enter__()

    assert len({s.handler for s in sessions}) == max_connections
    for s in sessions:
      s.__exit__(None, None, None)

  def test_checkout_past_max_connections_blocks_until_release(self, ftp_env: _FTPTestEnv) -> None:
    # Standard library imports
    from threading import Event, Thread

    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=1)
    held = adapter.start_session()
    held.__enter__()
    got_second: list[object] = []
    unblocked = Event()

    def _checkout_second() -> None:
      session = adapter.start_session()
      session.__enter__()
      got_second.append(session)
      unblocked.set()

    t = Thread(target=_checkout_second, daemon=True)
    t.start()
    assert not unblocked.wait(timeout=0.3), "second checkout should still be blocked"

    held.__exit__(None, None, None)
    assert unblocked.wait(timeout=2), "second checkout should unblock after release"
    t.join(timeout=2)
    assert len(got_second) == 1


class TestConnectionFatalReleaseIsDiscarded:
  def test_connection_error_during_session_discards_the_handler(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    first_handler = session.handler
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    # A fatal exception must not return the handler to the pool -- the next
    # checkout should get a freshly opened one, not the poisoned one.
    with adapter.start_session() as second:
      assert second.handler is not first_handler
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]

  def test_non_fatal_exception_still_returns_handler_to_pool(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    first_handler = session.handler
    with pytest.raises(FileNotFoundError), session:
      raise FileNotFoundError("no such remote file")

    with adapter.start_session() as second:
      assert second.handler is first_handler

  def test_discard_closes_the_handler_directly(self, ftp_env: _FTPTestEnv) -> None:
    """Regression test for a bug where discard tried to invoke a protocol's
    `close_conn_handler` as an unbound method on the handler
    (`close_conn_handler.__func__(handler)`), which silently no-ops against
    any real implementation instead of actually closing the connection.
    Discard must call the handler's own `.close()`/`.quit()` instead."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    session = adapter.start_session()
    session.__enter__()
    handler = session.handler
    assert handler is not None
    with pytest.raises(ConnectionError), session:
      raise ConnectionError("simulated dead socket")

    with pytest.raises(OSError):
      handler.voidcmd("NOOP")  # a closed connection can no longer respond


class TestLazyValidationOnCheckout:
  def test_stale_pooled_connection_is_discarded_and_replaced(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as first:
      stale_handler = first.handler

    # Simulate the server having dropped the connection while it sat idle.
    stale_handler.close()

    with adapter.start_session() as second:
      assert second.handler is not stale_handler
      # The replacement must be a live, working connection.
      second.handler.voidcmd("NOOP")

  def test_freshly_opened_connection_skips_validation(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that was just opened (not popped from the idle queue)
    must not pay the extra validation round trip -- it was already proven
    live by successfully completing its handshake. Asserted behaviorally
    (no NOOP round trip sent) rather than by spying on a private validation
    method directly, so this doesn't couple to an implementation detail."""
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)
    calls: list[str] = []
    original_voidcmd = FTP.voidcmd
    monkeypatch.setattr(FTP, "voidcmd", lambda self, cmd: (calls.append(cmd), original_voidcmd(self, cmd))[1])

    with adapter.start_session():
      pass

    assert calls == []


class TestRampUpDiscoversRealCeiling:
  def test_refused_growth_pins_discovered_max(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed_connections = 2
    open_count = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_count
      if open_count >= allowed_connections:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1
      return None

    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    sessions = [adapter.start_session() for _ in range(allowed_connections)]
    for s in sessions:
      s.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()

    assert adapter._discovered_max == allowed_connections  # pyright: ignore[reportPrivateUsage]
    for s in sessions:
      s.__exit__(None, None, None)

  def test_subsequent_checkouts_respect_discovered_max_without_reattempting(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")
      return None

    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()  # succeeds, open_attempts == 1

    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()  # open_attempts == 2, fails, discovers max=1

    held.__exit__(None, None, None)  # returns the one connection to _idle

    # This checkout must come from _idle (no new open attempt), not retry growth.
    with adapter.start_session():
      pass

    assert open_attempts == expected_open_attempts


class TestRecoveringADiscoveredCeiling:
  def test_reprobe_after_interval_raises_discovered_max_on_success(
    self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    allow_growth = False
    open_count = 0

    def _gated_get_transport(self: object) -> None:
      nonlocal open_count
      if open_count >= 1 and not allow_growth:
        raise ConnectionRefusedError("server connection limit reached")
      open_count += 1
      return None

    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _gated_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()
    assert adapter._discovered_max == 1  # pyright: ignore[reportPrivateUsage]

    # Simulate the server now allowing more connections, and the reprobe window elapsing.
    allow_growth = True
    recovered_ceiling = 2
    # Standard library imports
    from time import monotonic

    # Comfortably past the adapter's re-probe interval (300s per the design doc) --
    # a fixed large offset avoids depending on the private _REPROBE_INTERVAL value.
    monkeypatch.setattr("aeth_ext.ftp.adapter.monotonic", lambda: monotonic() + 10_000)

    with adapter.start_session() as second:
      assert second.handler is not None

    assert adapter._discovered_max is None or adapter._discovered_max >= recovered_ceiling  # pyright: ignore[reportPrivateUsage]
    held.__exit__(None, None, None)

  def test_reprobe_within_interval_does_not_reattempt(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_open_attempts = 2
    open_attempts = 0

    def _limited_get_transport(self: object) -> None:
      nonlocal open_attempts
      open_attempts += 1
      if open_attempts > 1:
        raise ConnectionRefusedError("server connection limit reached")
      return None

    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _limited_get_transport)
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=16)

    held = adapter.start_session()
    held.__enter__()
    with pytest.raises(ConnectionRefusedError):
      adapter.start_session().__enter__()
    assert open_attempts == expected_open_attempts

    held.__exit__(None, None, None)
    with adapter.start_session():
      pass

    # Still within _REPROBE_INTERVAL -- must come from _idle, no new open attempt.
    assert open_attempts == expected_open_attempts


class TestOptInKeepAlive:
  def test_disabled_by_default_spawns_no_thread(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env))

    with adapter.start_session():
      pass

    assert adapter._keepalive_thread is None  # pyright: ignore[reportPrivateUsage]

  def test_keepalive_pings_idle_connection_without_touching_checked_out_one(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4, keepalive_interval=0.05)

    with adapter.start_session():
      pass  # released back to _idle

    checked_out = adapter.start_session()
    checked_out.__enter__()  # not released -- must stay untouched
    checked_out_handler = checked_out.handler

    # Standard library imports
    from time import sleep

    sleep(0.2)  # let the keepalive loop tick a few times

    assert checked_out.handler is checked_out_handler
    checked_out.handler.voidcmd("NOOP")  # still alive, unpinged connection wasn't broken by concurrent use
    checked_out.__exit__(None, None, None)


class TestConnectionPrewarmsPool:
  def test_test_connection_leaves_a_reusable_connection_pooled(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    assert adapter.test_connection() is True

    with adapter.start_session():
      pass

    # If test_connection()'s session had been closed instead of pooled, this
    # would open a second connection instead of reusing the first.
    assert adapter._current_size == 1  # pyright: ignore[reportPrivateUsage]


class TestShutdownIntegration:
  def test_registers_for_shutdown_on_first_connection(self, ftp_env: _FTPTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[tuple[object, str]] = []
    monkeypatch.setattr(
      "aeth_ext.ftp.adapter.register_for_shutdown",
      lambda callback, *, phase, priority=0, required=False: registered.append((callback, phase.name)),
    )
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    assert registered == []
    with adapter.start_session():
      pass
    assert len(registered) == 1
    assert registered[0][1] == "THREADED"

    with adapter.start_session():
      pass
    assert len(registered) == 1  # still only once

  def test_shutdown_teardown_closes_idle_connections_only(self, ftp_env: _FTPTestEnv) -> None:
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session():
      pass  # released -- now idle
    checked_out = adapter.start_session()
    checked_out.__enter__()  # stays checked out

    adapter._shutdown_teardown()  # pyright: ignore[reportPrivateUsage]

    assert adapter._idle.empty()  # pyright: ignore[reportPrivateUsage]
    # The checked-out connection must be untouched by teardown.
    checked_out.handler.voidcmd("NOOP")
    checked_out.__exit__(None, None, None)


class TestChunkSizeThreading:
  def test_custom_chunk_size_reaches_the_session(self, ftp_env: _FTPTestEnv) -> None:
    custom_chunk_size = 4096
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4, chunk_size=custom_chunk_size)

    with adapter.start_session() as session:
      assert session.chunk_size == custom_chunk_size

  def test_default_chunk_size_is_8192(self, ftp_env: _FTPTestEnv) -> None:
    default_chunk_size = 8192
    adapter = create_ftp_adapter(_ftp_credentials(ftp_env), max_connections=4)

    with adapter.start_session() as session:
      assert session.chunk_size == default_chunk_size


class TestConstructorCallbacks:
  def test_download_file_invokes_constructor_callbacks_alongside_the_call_time_one(self, ftp_env: _FTPTestEnv, tmp_path: Path) -> None:
    adapter = ftp_env.make_adapter()
    seen_by_ctor_cb: list[bytes] = []
    seen_by_call_cb: list[bytes] = []
    adapter._callbacks = (seen_by_ctor_cb.append,)  # pyright: ignore[reportPrivateUsage]

    with adapter as ftp:
      assert ftp.handler is not None
      (tmp_path / "src").write_bytes(b"hello world")
      with open(tmp_path / "src", "rb") as f:
        ftp.handler.storbinary("STOR probe", f)
      ftp.download_file("probe", lambda chunk: seen_by_call_cb.append(bytes(chunk)))

    assert b"".join(seen_by_ctor_cb) == b"hello world"
    assert b"".join(seen_by_call_cb) == b"hello world"

  def test_upload_file_taps_constructor_callbacks_with_the_pulled_bytes(self, ftp_env: _FTPTestEnv) -> None:
    adapter = ftp_env.make_adapter()
    seen: list[bytes] = []
    adapter._callbacks = (seen.append,)  # pyright: ignore[reportPrivateUsage]
    chunks = [b"abc", b"def", b""]

    def _source(_size: int) -> bytes:
      return chunks.pop(0)

    with adapter as ftp:
      ftp.upload_file("probe2", _source, file_size=6)

    assert b"".join(seen) == b"abcdef"


class TestStandaloneProviderUsage:
  """Confirms the one-shot, non-pooled usage path (spec Section 4) actually works end-to-end: a
  hand-written `HandleProvider` used to construct `AdaptedFTP` directly, without `FTPAdapter`/
  `create_ftp_adapter` in the picture at all. `ftp_env.make_adapter()` already exercises this same path
  implicitly (see `tests/ftp/conftest.py`'s `_OneShotFTPProvider`) -- this test asserts it explicitly as
  a first-class scenario rather than only incidentally through every other test in this file."""

  def test_upload_then_download_round_trips(self, ftp_env: _FTPTestEnv) -> None:
    session = ftp_env.make_adapter()
    data = b"standalone provider payload"
    chunks = iter([data, b""])

    with session as ftp:
      ftp.upload_file("probe.bin", lambda _size: next(chunks), file_size=len(data))
      received = bytearray()
      ftp.download_file("probe.bin", lambda chunk: received.extend(bytes(chunk)))

    assert bytes(received) == data


class _TestSFTPServer:
  """Runs a persistent, multi-accept loopback paramiko SFTP server for the lifetime of a test, so
  multiplexing tests can dial the same port repeatedly to open more than one `Transport`. Unlike
  `_SFTPTestEnv.make_adapter` (which spins up a single-accept listener per adapter, sufficient for the
  non-pooling adapter tests), the multiplexing tests below need a server that keeps accepting new
  connections for the lifetime of the test."""

  def __init__(self, tmp_path: Path) -> None:
    homedir = tmp_path / "sftp_pool_root"
    homedir.mkdir()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    self.port = listener.getsockname()[1]

    host_key = paramiko.RSAKey.generate(2048)
    sftp_si = _make_stub_sftp_server(str(homedir))

    def _serve_forever() -> None:
      while True:
        try:
          conn, _addr = listener.accept()
        except OSError:
          return
        transport = Transport(conn)
        transport.add_server_key(host_key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, sftp_si=sftp_si)
        transport.start_server(server=_StubServerInterface())

    threading.Thread(target=_serve_forever, daemon=True).start()

  def credentials(self) -> SFTPCredentials:
    return SFTPCredentials(host="127.0.0.1", username="anyone", password="anything", port=self.port)


class TestSFTPChannelMultiplexing:
  def test_two_checkouts_share_one_transports_channel_cap(self, tmp_path: Path) -> None:
    """Two concurrently-checked-out SFTP sessions should multiplex over the same `Transport`
    instead of each dialing a fresh TCP connection, as long as both fit under
    `channels_per_transport`. Asserted via the pool's own bookkeeping (not a `.transport` attribute
    on the session -- the Adapted classes don't expose one) by checking both handlers resolve to the
    same `TransportState`."""
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()

    state_first = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    state_second = adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    assert state_first is state_second
    assert first.handler is not second.handler
    first.__exit__(None, None, None)
    second.__exit__(None, None, None)

  def test_channel_cap_forces_a_second_transport(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=1)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()

    state_first = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    state_second = adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    assert state_first is not state_second
    first.__exit__(None, None, None)
    second.__exit__(None, None, None)


class TestThroughputInstrumentation:
  def test_download_updates_the_owning_transports_throughput(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    (tmp_path / "probe_source").write_bytes(b"x" * 4096)
    with adapter.start_session() as session:
      assert isinstance(session, AdaptedSFTP)
      assert session.handler is not None
      session.handler.put(str(tmp_path / "probe_source"), "probe_remote")
      received = bytearray()
      session.download_file("probe_remote", received.extend)
      state = adapter._sftp_pool.state_for_handle(session.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]

    assert state is not None
    assert state.sample_count >= 1
    assert state.ewma_throughput is not None and state.ewma_throughput > 0


class TestSFTPTransportDeathCascade:
  def test_dead_transport_discards_its_idle_channels_too(self, tmp_path: Path) -> None:
    server = _TestSFTPServer(tmp_path)
    adapter = create_ftp_adapter(server.credentials(), max_connections=4, channels_per_transport=4)

    first = adapter.start_session()
    first.__enter__()
    second = adapter.start_session()
    second.__enter__()
    state = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    assert state is not None
    assert state is adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    first.__exit__(None, None, None)  # released -- now idle on the shared Transport

    dead_transport = state.transport
    with pytest.raises(ConnectionError), second:
      dead_transport.close()  # kill the Transport out from under the still-open session
      raise ConnectionError("simulated transport death")

    assert adapter._sftp_pool.state_for_handle(first.handler) is None  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]

    # A fresh checkout must not reuse the now-dead idle channel from `first`.
    third = adapter.start_session()
    third.__enter__()
    third_state = adapter._sftp_pool.state_for_handle(third.handler)  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
    assert third_state is not None
    assert third_state.transport is not dead_transport
    third.__exit__(None, None, None)
```

Note several tests above changed from `adapter.start_session()` (used directly, relying on the old
eager-acquire `start_session()`) to `adapter.start_session()` followed by an explicit `.__enter__()`
call, or kept as `with adapter.start_session() as x:`. This reflects Task 4's design: `start_session()`
no longer acquires a handle -- that now happens lazily in `__enter__()`. Any test that inspects
`session.handler` or otherwise needs a live connection **must** enter the context (either via `with` or
an explicit `.__enter__()`) before that will be non-`None`. Read through each test above and confirm
this is handled correctly for what it's asserting -- a few have been adjusted from the original file
already; double-check the rest as you port them, since a missed `.__enter__()` will surface as
`self.handler is not None` assertion failures deep inside `AdaptedFTP`/`AdaptedSFTP` methods rather than
as an obvious test failure at the call site.

- [ ] **Step 2: Run and fix**

Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py -v --no-cov`
Work through failures one at a time. Common causes to check first: a test that needs `.__enter__()` and
doesn't have it; a monkeypatch target that doesn't match `_FTPConnector.get_transport`'s actual bound-vs-unbound
signature (test doubles above define `_limited_get_transport(self: object) -> None` matching an unbound
method being assigned onto the class); `container_cls` assertions relying on `create_ftp_adapter`'s
`FTPAdapter`-vs-`SFTPAdapter` return type.

- [ ] **Step 3: Verify types/lint**

Run: `uv run pyright tests/ftp/test_ftp_adapter_factory.py`
Run: `uv run ruff check tests/ftp/test_ftp_adapter_factory.py`
Expected: both clean.

---

## Task 7: `tests/ftp/test_transfer.py` -- confirm/adjust

**Files:**
- Modify: `tests/ftp/test_transfer.py` (only if Task 5's Step 4 surfaced real failures -- otherwise this
  file needs no changes at all, since it only depends on `make_ftp_adapter`/`make_sftp_adapter`/
  `fake_progress`, none of which changed shape)

**Interfaces:**
- Consumes: `make_ftp_adapter`, `make_sftp_adapter`, `fake_progress` fixtures (Task 5).

- [ ] **Step 1: Run the suite**

Run: `uv run pytest tests/ftp/test_transfer.py -v --no-cov`

- [ ] **Step 2: Fix anything that's actually broken**

If everything passes (expected, per Task 5's note), this task is done -- move on. If something fails,
it's a real behavior change to investigate (not a test-infrastructure mismatch, since this file never
touched the protocol classes directly), so read the failure carefully before changing anything. Do not
assume the fix is "update the test" -- confirm against the spec/this plan's Task 4 first.

---

## Task 8: Full verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Full FTP/SFTP suite**

Run: `uv run pytest tests/ftp/ -v --no-cov`
Expected: all PASS.

- [ ] **Step 2: Ruff, whole project**

Run: `uv run ruff check .`
Expected: "All checks passed!" Fix anything that comes up. If you're unsure whether a finding predates
this plan's changes, compare against a clean baseline first: `git stash -u`, re-run `uv run ruff check
.`, note the count, then `git stash pop` and diff -- don't assume every finding is yours to fix, but do
fix everything that's new.

- [ ] **Step 3: Pyright, whole project**

Run: `uv run pyright`
Expected: 0 errors, 0 warnings, 0 informations.

- [ ] **Step 4: Full project test suite**

Run: `uv run pytest --no-cov -q`
Expected: same pass count as the pre-existing baseline (778 passed, 1 skipped, as of the start of this
plan) plus this plan's new tests (Task 1's `test_credentials.py`, Task 2's `TestDrainTransports`, Task
6's rewritten factory tests). No regressions elsewhere in the project -- this plan only touches
`src/aeth_ext/ftp/` and `tests/ftp/`, so a failure anywhere else in the suite is a real regression to
investigate, not something to wave off as unrelated.

- [ ] **Step 5: Report final state -- do not commit**

Run `git status` and `git diff --stat` and report the result. Per this plan's Global Constraints, leave
everything uncommitted for the user's own review. Do not run `git add`, `git commit`, or any destructive
git command.
