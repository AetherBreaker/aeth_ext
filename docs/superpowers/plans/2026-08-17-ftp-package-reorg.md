# FTP Package Reorg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `src/aeth_ext/ftp/adapter.py` (1469 lines, mixing connection-opening, session, and pool
concerns) and `src/aeth_ext/ftp/sftp_pool.py` into single-responsibility modules with a strictly acyclic
import graph, and replace every internal-only Protocol duplicate (`_ChannelConnector`, `TransportProvider`,
`AdapterProtocol`) with the real concrete type it stood in for -- with zero `# pyright: ignore` suppressions
anywhere in the result.

**Architecture:** New leaf-to-root module chain: `errors.py`/`types.py`/`credentials.py` (leaves) ->
`connectors.py` -> `session.py` -> `pool/base.py` -> `pool/sftp_channel_pool.py` -> `pool/ftp_adapter.py` /
`pool/sftp_adapter.py` -> `factory.py` -> `ftp/__init__.py` (public re-export surface). The one relationship
that looked like it would need a Protocol or a suppressed cycle (`ChannelLedger.transports`) is resolved by
adding `_PooledAdapterBase._make_transport_dialer()`, a method that builds two closures over its own
(same-class, privacy-safe) internals and hands back a plain `_TransportDialer` holding just those two
callables -- so `pool/sftp_channel_pool.py` can depend on a real, concrete type without ever needing to
import `SFTPAdapter`.

**Tech Stack:** Python 3.14, `paramiko`, `ftplib` (stdlib), `pytest`, `pyright` (`reportImportCycles` and
`reportPrivateUsage` both `true` in this project's `pyproject.toml` -- both are load-bearing constraints
for this plan, not just style preferences).

**Spec:** `docs/superpowers/specs/2026-08-17-ftp-package-reorg-design.md` -- this is a temporary spec
(per the user, delete it once this plan lands and passes review). This plan implements it in full,
including the corrected (zero-suppression) `_TransportDialer` design in its "Breaking the
`ChannelLedger`/`SFTPAdapter` mutual reference" section.

## Global Constraints

- **Do not commit anything, at any point, for any task.** Leave the working tree as-is for the user to
  review and commit themselves. This is a hard requirement from the user, not a suggestion -- there is
  no "Step N: Commit" in any task below, and none should be added.
- **No behavior change anywhere.** This is a pure structural move plus the Protocol-to-concrete-type
  swaps described below. The multiplexing algorithm, pooling/ceiling logic, and every public method's
  observable behavior must be byte-for-byte identical in effect to today's `adapter.py`/`sftp_pool.py`.
  Every code block given in this plan is either copied verbatim from the current source or is a small,
  explicitly-called-out addition (`_TransportDialer`, `_make_transport_dialer`, the `max_connections`/
  `keepalive_interval` validation already present on `main` -- do not remove it).
- **`from __future__ import annotations` is banned project-wide.** Python 3.14 evaluates annotations
  lazily (PEP 649) regardless. A type used only in an annotation position (parameter/return/variable
  type, never constructed, never a base class, never `isinstance`-checked) can stay under
  `TYPE_CHECKING`. A type used as a base class in a `class X(Base[Concrete]):` statement, constructed
  directly, or `isinstance`-checked **must** be a real top-level import -- Python evaluates class base
  lists eagerly at class-definition time, which is not an annotation position. Every import block given
  below already reflects this split correctly; preserve it exactly when copying code.
- **PEP 758 bare multi-exception `except A, B, C:` (no parentheses, no `as e`) is valid Python 3.14
  syntax in this project.** `pool/sftp_channel_pool.py`'s `_validate` method contains
  `except SSHException, EOFError:` -- this is not a bug, do not "fix" it into `except (SSHException, EOFError):`.
- **Docstrings are Google style** (`Args:`/`Returns:`/`Raises:`, no Sphinx roles, double-backtick names).
  Comments carry *why*, stay dense, don't restate what's already said elsewhere in the file.
- **`__slots__` on every plain class**, matching the existing codebase convention -- every class moved or
  added in this plan already has (or is given) one.
- **After writing each file, run `uv run ruff check --fix <path>` then `uv run ruff check <path>`**
  (expect the second to report no remaining issues) **before** running `uv run pyright <path>`. Ruff's
  `--fix` handles import ordering and `__all__` sorting automatically -- do not hand-sort `__all__` or
  import blocks; let the tool do it, then re-read the file to confirm the result still matches this
  plan's intent (all the same names present, nothing dropped).
- **Verify per-module as you go, not just at the end** (`.claude/CLAUDE.md`'s own stated preference, and
  this reorg touches enough files that batching all verification to the end would make failures hard to
  localize). Each task below has its own verification steps -- run them before moving to the next task.
- **This branch does not need backward compatibility.** No consumers exist outside this repo yet. Do not
  add re-export shims for removed/moved names beyond the deliberate `ftp/__init__.py` public surface
  defined in Task 9 -- `.claude/CLAUDE.md` explicitly discourages compatibility re-export shims, and the
  user has separately confirmed breaking internal import paths is fine for this branch.
- **`PYTHONPYCACHEPREFIX`** is auto-loaded from `.env` under `uv run pytest` -- no action needed.

---

## Task 1: Trim `types.py` -- remove the now-unnecessary `TransportProvider` Protocol

**Files:**
- Modify: `src/aeth_ext/ftp/types.py` (currently 79 lines, full file below)

**Interfaces:**
- Produces: `HandleProvider[HandleT]` (unchanged), `ListDirResult` (unchanged), type aliases
  `BufferSize`/`TransferSuccess`/`IntrumentCallable`/`ReadCallback`/`WriteCallback` (unchanged).
  `TransportProvider` is deleted -- nothing later in this plan imports it.

- [ ] **Step 1: Replace the full contents of `src/aeth_ext/ftp/types.py`**

```python
# Standard library imports
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from datetime import datetime
  from typing import Any

  # First party imports
  from aeth_ext.types import SizedBuffer


__all__ = ["HandleProvider", "ListDirResult"]

type BufferSize = int
type TransferSuccess = bool
type IntrumentCallable = Callable[[SizedBuffer], Any]
type ReadCallback = Callable[[BufferSize], Any]
type WriteCallback = Callable[[SizedBuffer], Any]


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

  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]:
    """Acquires a connection handle.

    Returns:
      The handle, plus any handle-scoped observer callbacks to attach to it.
    """
    ...

  def release(self, handle: HandleT, is_fatal: bool) -> None:
    """Returns a handle previously acquired via `acquire`.

    Args:
      handle: The handle to return.
      is_fatal: Whether the connection is broken and should be discarded rather than reused.
    """
    ...
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/types.py && uv run ruff check src/aeth_ext/ftp/types.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/types.py`
Expected: `0 errors, 0 warnings, 0 informations`. (`adapter.py`/`sftp_pool.py` will show a new
`TransportProvider` import error at this point -- that's expected and fixed by later tasks; ignore it
for now. Only judge `types.py` itself.)

Run: `uv run python -c "import aeth_ext.ftp.types"`
Expected: no output, exit code 0.

---

## Task 2: Create `connectors.py`

**Files:**
- Create: `src/aeth_ext/ftp/connectors.py`

**Interfaces:**
- Consumes: `FTPCredentials`, `SFTPCredentials` from `aeth_ext.ftp.credentials` (unchanged, existing file).
- Produces: `_FTPConnector(credentials: FTPCredentials)` with `.get_transport() -> None`,
  `.request_handler(*_args, **_kwargs) -> FTP`, `.close_conn_handler(handle: FTP) -> None`.
  `_SFTPConnector(credentials: SFTPCredentials)` with `.get_transport() -> Transport`,
  `.request_handler(transport: Transport) -> SFTPClient`, `.close_conn_handler(handle: Transport) -> None`.
  Both classes are private (leading underscore) -- no `__all__` in this module.

- [ ] **Step 1: Create `src/aeth_ext/ftp/connectors.py`**

```python
"""Credentials-driven connection-opening logic for `FTPAdapter`/`SFTPAdapter`.

Not a public extension point -- `HandleProvider` (`aeth_ext.ftp.types`) is. Each `FTPAdapter`/
`SFTPAdapter` builds exactly one of `_FTPConnector`/`_SFTPConnector` from its credentials and holds it
for its whole lifetime.
"""

# Standard library imports
from ftplib import FTP, FTP_TLS
from typing import TYPE_CHECKING

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SSHClient

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials

if TYPE_CHECKING:
  # Third party imports
  from paramiko import Transport


class _FTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: FTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The FTP server credentials to connect with.
    """
    self._credentials = credentials

  def get_transport(self) -> None:
    """No-op: plain FTP has no separate transport tier to open."""
    return  # no-op: FTP has no separate transport/channel tiers

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    """Opens and authenticates a new `FTP`/`FTP_TLS` connection.

    Accepts and ignores positional/keyword arguments so its call signature matches
    `_SFTPConnector.request_handler`'s (which takes the transport to open a channel on) --
    FTP has no such transport argument to pass.

    Returns:
      The connected, authenticated handle.
    """
    conn = FTP_TLS() if self._credentials.use_tls else FTP()
    conn.connect(
      self._credentials.host,
      self._credentials.port,
      timeout=self._credentials.connect_timeout,  # pyright: ignore[reportArgumentType] -- ftplib's stub omits `| None` even though the real default is None
    )
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    conn.set_pasv(self._credentials.passive_mode)
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    """Closes a connection, falling back to a raw close if the graceful QUIT fails.

    Args:
      handle: The connection to close.
    """
    try:
      handle.quit()
    except OSError:
      handle.close()


class _SFTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: SFTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The SFTP server credentials to connect with.
    """
    self._credentials = credentials

  def get_transport(self) -> Transport:
    """Opens and authenticates a new SSH `Transport`, applying the configured host-key policy.

    Returns:
      The connected, authenticated `Transport`.
    """
    client = SSHClient()
    if self._credentials.known_hosts_path is not None:
      client.load_host_keys(str(self._credentials.known_hosts_path))
    else:
      client.load_system_host_keys()
    client.set_missing_host_key_policy(AutoAddPolicy() if self._credentials.host_key_policy == "auto_add" else RejectPolicy())
    client.connect(
      self._credentials.host,
      port=self._credentials.port,
      username=self._credentials.username,
      password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
      key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
      passphrase=(
        self._credentials.private_key_passphrase.get_secret_value() if self._credentials.private_key_passphrase is not None else None
      ),
      timeout=self._credentials.connect_timeout,
    )
    transport = client.get_transport()
    assert transport is not None
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    """Opens a new SFTP channel.

    Args:
      transport: The `Transport` to open a channel on.

    Returns:
      The new channel.
    """
    return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

  def close_conn_handler(self, handle: Transport) -> None:
    """Closes the whole `Transport`, and every channel opened on it.

    Args:
      handle: The `Transport` to close.
    """
    handle.close()
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/connectors.py && uv run ruff check src/aeth_ext/ftp/connectors.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/connectors.py`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run python -c "import aeth_ext.ftp.connectors"`
Expected: no output, exit code 0.

---

## Task 3: Create `session.py`

**Files:**
- Create: `src/aeth_ext/ftp/session.py`

**Interfaces:**
- Consumes: `HandleProvider`, `IntrumentCallable`, `ListDirResult`, `ReadCallback`, `TransferSuccess`,
  `WriteCallback` from `aeth_ext.ftp.types` (Task 1's trimmed file -- none of these were removed).
- Produces: `AdaptedFTP`, `AdaptedSFTP` (public, `__all__`). `_AdaptedSessionBase[HandleT]` and
  `_AdapterBase` are private -- later tasks (`pool/base.py`) import `_AdapterBase` under
  `TYPE_CHECKING` only (it's used purely as a `TypeVar` bound, never constructed or `isinstance`-checked
  outside this file).
- **Change from today's `adapter.py`:** `AdapterProtocol` (a `Protocol` subclass) becomes `_AdapterBase`
  (a plain `ABC` subclass, `@abstractmethod` added to each method, same `raise NotImplementedError`
  bodies, same `__slots__ = ()`). This is a pure rename + base-class swap -- every method's signature,
  docstring, and body is unchanged. `AdaptedFTP`/`AdaptedSFTP` already nominally inherit it (not
  structural duck-typing), so `AdapterProtocol` -> `_AdapterBase` is invisible to every caller; the only
  reason to do it is that `@override` needs a real base method to point at (a Protocol member still
  counts, an ABC member also counts -- both work; the point of the swap is that nothing in this codebase
  ever needs `AdaptedFTP`/`AdaptedSFTP` treated as duck-typed/structural against some *other*,
  unrelated class, so `Protocol`'s structural-typing behavior was never actually exercised here).

- [ ] **Step 1: Create `src/aeth_ext/ftp/session.py`**

```python
"""Per-session transfer logic shared by `AdaptedFTP`/`AdaptedSFTP`. Holds only the acquire/release/handler
plumbing -- every transfer-protocol method (upload_file, download_file, transfer_file, ...) is defined
directly on `AdaptedFTP`/`AdaptedSFTP` themselves, never on a shared mixin, so goto-definition on a
transfer call always lands on the real implementation.
"""

# Standard library imports
from abc import ABC, abstractmethod
from contextlib import nullcontext
from datetime import datetime
from ftplib import FTP, _SSLSocket, all_errors  # type: ignore
from io import BytesIO
from logging import getLogger
from typing import TYPE_CHECKING, override

# Third party imports
from paramiko import SFTPClient, SFTPError, SSHException

# First party imports
from aeth_ext.ftp.types import (
  HandleProvider,
  IntrumentCallable,
  ListDirResult,
  ReadCallback,
  TransferSuccess,
  WriteCallback,
)
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Sequence
  from types import TracebackType
  from typing import Any, Self
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.rich.progress import Progress


logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


__all__ = ["AdaptedFTP", "AdaptedSFTP"]


_CONNECTION_FATAL_TYPES = (TimeoutError, ConnectionError, BrokenPipeError, EOFError, SSHException)


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
    callbacks: Sequence[IntrumentCallable] = (),
  ) -> None:
    """Stores session configuration; does not acquire a handle until `__enter__`.

    Args:
      provider: Supplies (and reclaims) the connection handle this session uses.
      container_cls: Label attached to log messages this session emits.
      pbar: Progress reporter for transfer methods to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      chunk_size: Bytes read/written per I/O call by transfer methods.
      callbacks: Observers invoked with each chunk transferred, in addition to any the provider
        attaches at acquire time.
    """
    self.handler: HandleT | None = None
    self._provider = provider
    self._callbacks = tuple(callbacks)
    self.container_cls = container_cls
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.chunk_size = chunk_size
    super().__init__()

  def __enter__(self) -> Self:
    """Acquires a handle from `provider` on first entry; a no-op on nested/repeat entry.

    Returns:
      This session.
    """
    if self.handler is None:
      self.handler, callbacks = self._provider.acquire()
      self._callbacks = (*self._callbacks, *callbacks)
    return self

  def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
    """Releases the handle, marking it fatal if `exc_val` indicates a broken connection.

    Args:
      exc_type: Unused; part of the context-manager protocol.
      exc_val: The exception raised inside the `with` block, if any.
      exc_tb: Unused; part of the context-manager protocol.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self._provider.release(self.handler, isinstance(exc_val, _CONNECTION_FATAL_TYPES))

  def _notify(self, data: bytes) -> None:
    """Invokes every constructor/acquire-time observer with one transferred chunk."""
    for observer in self._callbacks:
      observer(data)


class _AdapterBase(ABC):
  __slots__ = ()

  @abstractmethod
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection to the FTP/SFTP server.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the connection succeeded, `False` otherwise.
    """
    raise NotImplementedError

  @abstractmethod
  def get_size(self, path: str) -> int | None:
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes, or `None` if it can't be determined.
    """
    raise NotImplementedError

  @abstractmethod
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Uploads a file by pulling its bytes from `callback` until it returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with the desired chunk size; returns that many bytes, or `b""` when done.
      file_size: Total size of the upload, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

  @abstractmethod
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Downloads a file, pushing each chunk read to `callback`.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    raise NotImplementedError

  @abstractmethod
  def transfer_file(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Transfers a file server-to-server, without saving it to local disk.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session (may be FTP or SFTP).
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    raise NotImplementedError

  @abstractmethod
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    raise NotImplementedError

  @abstractmethod
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    raise NotImplementedError

  @abstractmethod
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries (bare filenames, not full paths) in a directory.

    An entry whose modification time can't be determined is skipped, not yielded with a `None` time.

    Args:
      path: Absolute path to a directory on the server.
    """
    raise NotImplementedError

  @abstractmethod
  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    raise NotImplementedError


class AdaptedFTP(_AdaptedSessionBase[FTP], _AdapterBase):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Streams `callback`-supplied chunks to `remote_path` over the FTP data connection until it
    returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with `self.chunk_size`; returns that many bytes, or `b""` when done.
      file_size: Total upload size, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
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
            self._notify(buffer)
            if self.pbar is not None:
              assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
              self.pbar.update(transfer_task, advance=len(buffer))
        if _SSLSocket is not None and isinstance(conn, _SSLSocket):
          conn.unwrap()  # type: ignore
    finally:
      self.handler.voidresp()

  @override
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Streams `remote_path`'s contents from the FTP data connection to `callback`, chunk by chunk.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
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
            self._notify(data)
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
    """Dispatches to the FTP-to-FTP or FTP-to-SFTP transfer path based on `other`'s type.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
    if isinstance(other, AdaptedFTP):
      return self._ftp_to_ftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    elif isinstance(other, AdaptedSFTP):  # pyright: ignore[reportUnnecessaryIsInstance]
      return self._ftp_to_sftp(source_remote_path, dest_remote_path, other, task_msg, callback, mem_stream)
    else:
      raise TypeError(f"Unsupported other protocol: {other.__class__}")  # pyright: ignore[reportUnreachable]

  def _ftp_to_sftp(  # noqa: PLR0917
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Streams `source_remote_path` from this FTP connection directly into `other`'s SFTP connection.

    Args:
      source_remote_path: Absolute source path on this session's FTP server.
      dest_remote_path: Absolute destination path on `other`'s SFTP server.
      other: The destination SFTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to both sessions' own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
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
            self._notify(data)
            other._notify(data)  # destination-side SFTP instrumentation
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
    """Streams `source_remote_path` from this FTP connection directly into `other`'s FTP connection.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination FTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
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
          self._notify(data)
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
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.voidcmd("TYPE I")  # Set binary mode
    return self.handler.size(path)

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.delete(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries via `MLSD`, skipping any entry with no `modify` fact.

    Args:
      path: Absolute path to a directory on the server.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.mlsd(path):
      name, facts = entry
      if "modify" in facts:
        dt = datetime.strptime(facts["modify"], "%Y%m%d%H%M%S")  # noqa: DTZ007
        new_dt = dt.replace(tzinfo=self.tzinfo)
        yield ListDirResult(filename=name, modified_time=new_dt)

  @override
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection with a `NOOP` round trip.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the round trip succeeded, `False` otherwise.
    """
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
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkd(remote_path)


class AdaptedSFTP(_AdaptedSessionBase[SFTPClient], _AdapterBase):
  __slots__ = ()

  @override
  def upload_file(self, remote_path: str, callback: ReadCallback, file_size: int, task_msg: str = "") -> None:
    """Streams `callback`-supplied chunks to `remote_path` until it returns an empty chunk.

    Args:
      remote_path: Absolute destination path on the server.
      callback: Called with `self.chunk_size`; returns that many bytes, or `b""` when done.
      file_size: Total upload size, for progress reporting.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    with (
      self.handler.open(remote_path, mode="wb") as remote_file,
      (
        self.pbar.add_task(task_msg or f"Transferring {remote_path}", total=file_size) if self.pbar is not None else nullcontext()
      ) as transfer_task,
    ):
      while buffer := callback(self.chunk_size):
        remote_file.write(buffer)
        self._notify(buffer)
        if self.pbar is not None:
          assert transfer_task is not None, "transfer_task should not be None when self.pbar is not None"
          self.pbar.update(transfer_task, advance=len(buffer))

  @override
  def download_file(self, remote_path: str, callback: WriteCallback, task_msg: str = "") -> None:
    """Streams `remote_path`'s contents to `callback`, chunk by chunk.

    Prefetches the whole file up front so paramiko can pipeline reads instead of waiting on each
    request/response round trip.

    Args:
      remote_path: Absolute source path on the server.
      callback: Called with each chunk read.
      task_msg: Progress-bar label; defaults to a message naming `remote_path`.
    """
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
          self._notify(data)
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
    """Dispatches to the SFTP-to-FTP or SFTP-to-SFTP transfer path based on `other`'s type.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
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
    """Streams `source_remote_path` from this SFTP connection directly into `other`'s FTP connection.

    Args:
      source_remote_path: Absolute source path on this session's SFTP server.
      dest_remote_path: Absolute destination path on `other`'s FTP server.
      other: The destination FTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to this session's own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
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
          self._notify(data)
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
    """Streams `source_remote_path` from this SFTP connection directly into `other`'s SFTP connection.

    Args:
      source_remote_path: Absolute source path on this session's server.
      dest_remote_path: Absolute destination path on `other`'s server.
      other: The destination SFTP session.
      task_msg: Progress-bar label; defaults to a message naming `source_remote_path`.
      callback: Called with each chunk transferred, in addition to both sessions' own observers.
      mem_stream: Buffer to also write transferred bytes to; a fresh one is used if omitted.

    Returns:
      Whether the source, destination, and streamed byte counts all agree.
    """
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
          self._notify(data)
          other._notify(data)
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
    """Returns a file's size.

    Args:
      path: Absolute path to a file on the server.

    Returns:
      The file's size in bytes, or `None` on `SFTPError`.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    try:
      return self.handler.stat(path).st_size
    except SFTPError:
      logger.exception("%s: Failed to get file size for %s.", self.container_cls, path)
      return None

  @override
  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the server.

    Args:
      old_remote_path: Absolute current path.
      new_remote_path: Absolute path to rename it to.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.rename(old_remote_path, new_remote_path)

  @override
  def remove(self, remote_path: str) -> None:
    """Deletes a file on the server.

    Args:
      remote_path: Absolute path to the file to delete.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.remove(remote_path)

  @override
  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Lists entries in a directory.

    Args:
      path: Absolute path to a directory on the server.

    Raises:
      ValueError: An entry has no modification time -- unlike `AdaptedFTP.listdir`, this does not
        silently skip it.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    for entry in self.handler.listdir_iter(path):
      if entry.st_mtime is None:
        raise ValueError(f"Entry {entry.filename} does not have a modification time, cannot be used in _sftp_listdir")
      yield ListDirResult(filename=entry.filename, modified_time=datetime.fromtimestamp(entry.st_mtime, tz=self.tzinfo))

  @override
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection with a `listdir(".")` round trip.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the round trip succeeded, `False` otherwise.
    """
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
    """Creates a directory on the server.

    Args:
      remote_path: Absolute path of the directory to create.
    """
    assert self.handler is not None, "This can only be called while the adapter is opened as a context manager"
    self.handler.mkdir(remote_path)
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/session.py && uv run ruff check src/aeth_ext/ftp/session.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/session.py`
Expected: `0 errors, 0 warnings, 0 informations`. In particular, confirm there is **no**
`reportAbstractUsage` or similar complaint about `AdaptedFTP`/`AdaptedSFTP` -- both implement every
`_AdapterBase` abstract method, so both must be freely instantiable.

Run: `uv run python -c "from aeth_ext.ftp.session import AdaptedFTP, AdaptedSFTP; AdaptedFTP; AdaptedSFTP"`
Expected: no output, exit code 0 (confirms both classes are concrete/instantiable-shaped, not still
abstract due to a missed `@override`/method).

---

## Task 4: Create `pool/base.py` (and `pool/__init__.py`)

**Files:**
- Create: `src/aeth_ext/ftp/pool/__init__.py` (empty file, 0 bytes -- matches how `src/aeth_ext/ftp/__init__.py`
  is empty today; makes `pool` a valid package with no public surface of its own)
- Create: `src/aeth_ext/ftp/pool/base.py`

**Interfaces:**
- Consumes: `_AdapterBase` from `aeth_ext.ftp.session` (Task 3), under `TYPE_CHECKING` only (used solely
  as the `SessionT` `TypeVar` bound -- PEP 695 type-parameter bounds are evaluated lazily like
  annotations, so this never forces resolution and creates no runtime dependency on `session.py`).
  `register_for_shutdown`, `ShutdownPhase` from `aeth_ext.errors.shutdown` (existing, unchanged, real
  import).
- Produces: `_PooledAdapterBase[SessionT: _AdapterBase, HandleT](ABC)` (unchanged behavior from today's
  `adapter.py`, including the `max_connections`/`keepalive_interval` validation and the `_open_new_slot`
  fix that avoids pinning `_discovered_max` to 0 -- both already on `main`, preserve them exactly) plus a
  new method, `_make_transport_dialer`. `_TransportDialer` (new type -- see below).

**This is the task that resolves the `ChannelLedger`/`SFTPAdapter` mutual-reference problem from the
spec.** `_TransportDialer` is a plain holder of two callables (`open_transport`, `transport_dropped`) --
it never imports or references `_PooledAdapterBase`, `SFTPAdapter`, or anything in
`pool/sftp_channel_pool.py`. `_make_transport_dialer` is a concrete (non-abstract) method on
`_PooledAdapterBase` that builds the two closures *inside its own method body*, so the closures'
access to `self._open_new_slot`/`self._size_lock`/`self._current_size` is same-class access -- verified
empirically that pyright's `reportPrivateUsage` is scoped per-class (not per-module): a *different*
class in the same file reaching into these same names would still be flagged. Putting the closures
inside `_PooledAdapterBase`'s own method avoids that entirely; there is nothing to suppress.

- [ ] **Step 1: Create the empty `src/aeth_ext/ftp/pool/__init__.py`**

Create a completely empty file (0 bytes, no content at all -- not even a docstring).

- [ ] **Step 2: Create `src/aeth_ext/ftp/pool/base.py`**

```python
"""Protocol-agnostic pool bookkeeping (size/ceiling/keepalive/shutdown) shared by `FTPAdapter` and
`SFTPAdapter`. Everything that actually knows about FTP-vs-SFTP shapes (how to check out an idle handle,
how to open a brand new one, how to release/discard) is abstract here and owned entirely by the
concrete subclass -- no isinstance/issubclass branching anywhere in this file.

Also home to `_TransportDialer`: the concrete type `ChannelLedger` (`pool.sftp_channel_pool`) depends on
for its `transports` field, instead of a `TransportProvider` Protocol or `SFTPAdapter` itself. Typing
`ChannelLedger.transports` as concrete `SFTPAdapter` would create an import cycle -- `SFTPAdapter`
(`pool.sftp_adapter`) needs a real import of `pool.sftp_channel_pool` to construct `ChannelLedger`/
`SFTPChannelPool`, so `pool.sftp_channel_pool` importing `SFTPAdapter` back (even under
`TYPE_CHECKING`) trips `reportImportCycles`. `_TransportDialer` breaks that: it holds two plain
callables and has no dependency on `SFTPAdapter` or `pool.sftp_channel_pool` at all, so
`pool.sftp_channel_pool` can depend on it for real with zero cycle.
"""

# Standard library imports
from abc import ABC, abstractmethod
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# First party imports
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from contextvars import ContextVar
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.ftp.session import _AdapterBase
  from aeth_ext.rich.progress import Progress


class _TransportDialer:
  """Dials new `Transport`s within a shared ceiling and records when one dies. Built via
  `_PooledAdapterBase._make_transport_dialer` and handed to `ChannelLedger` so the ledger depends on a
  real, narrow, navigable type instead of `SFTPAdapter` itself. See this module's docstring for why a
  plain callable-holder (rather than a reference to the pool that built it) is what avoids the cycle.
  """

  __slots__ = ("open_transport", "transport_dropped")

  def __init__(self, *, open_transport: Callable[[], Transport | None], transport_dropped: Callable[[], None]) -> None:
    """Stores the two bound operations `ChannelLedger` needs.

    Args:
      open_transport: Dials a new `Transport` within the owning pool's ceiling.
      transport_dropped: Records that a previously-dialed `Transport` has died.
    """
    self.open_transport = open_transport
    self.transport_dropped = transport_dropped


class _PooledAdapterBase[SessionT: _AdapterBase, HandleT](ABC):
  __slots__ = (
    "__weakref__",  # CPython-managed, never assigned directly
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
    """Initializes protocol-agnostic pool bookkeeping shared by `FTPAdapter`/`SFTPAdapter`.

    Args:
      max_connections: Ceiling on concurrently open connections.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle connections; `None` disables it.

    Raises:
      ValueError: `max_connections` is less than 1, or `keepalive_interval` is not `None` and not
        positive.
    """
    if max_connections < 1:
      raise ValueError(f"max_connections must be >= 1, got {max_connections}")
    if keepalive_interval is not None and keepalive_interval <= 0:
      raise ValueError(f"keepalive_interval must be positive or None, got {keepalive_interval}")

    self.max_connections = max_connections
    self.chunk_size = chunk_size
    self.pbar = pbar
    self.tzinfo = tzinfo
    self.container_cls = container_cls
    self.container_cvar = container_cvar
    self._keepalive_interval = keepalive_interval

    self._current_size = 0
    self._size_lock = Lock()
    self._discovered_max: int | None = None
    self._discovered_max_last_probe: float = 0.0
    self._keepalive_thread = None
    self._keepalive_stop = None
    self._registered_for_shutdown = False

    super().__init__()

  def _effective_ceiling(self) -> int:
    """Returns the connection-count ceiling to grow against: `max_connections`, capped to a previously
    discovered server-side limit until the re-probe interval next allows testing past it."""
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

    Args:
      dial: Opens the new low-level connection.

    Returns:
      `dial`'s result, or `None` if the ceiling was already reached.
    """
    with self._size_lock:
      if self._current_size >= self._effective_ceiling():
        return None
      self._current_size += 1
      self._ensure_registered_for_shutdown()
      try:
        result = dial()
      except OSError:
        self._current_size -= 1
        # A failure that drops _current_size to 0 means no connection exists at all (e.g. the server
        # is down), not that the server has a real ceiling -- pinning _discovered_max to 0 would leave
        # _effective_ceiling() at 0 forever, and callers that fall back to blocking on an empty idle
        # queue (see FTPAdapter.acquire()) would then hang forever with nothing left to release into
        # it. Only record a ceiling when it reflects an actual limit above zero.
        if self._current_size > 0:
          self._discovered_max = self._current_size
          self._discovered_max_last_probe = monotonic()
        raise
      else:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size
        return result

  def _make_transport_dialer(self, dial: Callable[[], Transport]) -> _TransportDialer:
    """Builds a `_TransportDialer` bound to this pool's own ceiling/size bookkeeping.

    Args:
      dial: Opens one new `Transport` (e.g. `_SFTPConnector.get_transport`).

    Returns:
      The dialer, for `SFTPAdapter` to hand to `ChannelLedger`.
    """

    def _open() -> Transport | None:
      return self._open_new_slot(dial)

    def _drop() -> None:
      with self._size_lock:
        self._current_size -= 1

    return _TransportDialer(open_transport=_open, transport_dropped=_drop)

  def start_session(self) -> SessionT:
    """Builds a new session, resolving `container_cls` from `container_cvar` when set and bound.

    Returns:
      The new session.
    """
    try:
      if self.container_cvar is not None:
        container_cls = self.container_cvar.get()
      else:
        container_cls = self.container_cls
    except LookupError:
      container_cls = self.container_cls
    return self._build_session(container_cls)

  def test_connection(self, logit: bool = False) -> bool:
    """Delegates to a fresh session's `test_connection`.

    Args:
      logit: Whether to log the failure reason if the connection test fails.

    Returns:
      `True` if the connection test succeeded, `False` otherwise.
    """
    return self.start_session().test_connection(logit)

  def _keepalive_loop(self) -> None:
    """Runs `_keepalive_check_one` every `_keepalive_interval` seconds until `_keepalive_stop` is set."""
    while not self._keepalive_stop.wait(timeout=self._keepalive_interval):  # pyright: ignore[reportOptionalMemberAccess]
      self._keepalive_check_one()

  def _ensure_keepalive_started(self) -> None:
    """Starts the keepalive thread on first call if `_keepalive_interval` is configured; a no-op after."""
    if self._keepalive_interval is None or self._keepalive_thread is not None:
      return
    self._keepalive_stop = Event()
    self._keepalive_thread = Thread(target=self._keepalive_loop, name="aeth-ext-ftp-keepalive", daemon=True)
    self._keepalive_thread.start()

  def _shutdown_teardown(self) -> None:
    """Stops the keepalive thread (if running) and closes all idle connections.

    Registered as this adapter's process-shutdown callback; never called directly.
    """
    if self._keepalive_stop is not None:
      self._keepalive_stop.set()
      if self._keepalive_thread is not None:
        self._keepalive_thread.join(timeout=2.0)
    self._teardown_idle()

  def _ensure_registered_for_shutdown(self) -> None:
    """Registers `_shutdown_teardown` for process shutdown on first call; a no-op after."""
    if self._registered_for_shutdown:
      return
    self._registered_for_shutdown = True
    register_for_shutdown(self._shutdown_teardown, phase=ShutdownPhase.THREADED)

  # --- abstract, subclass-owned: no runtime type checks here or in any subclass implementation, since
  # each subclass statically knows its own HandleT/SessionT. FTPAdapter is its own HandleProvider and
  # keeps concrete acquire/release/_validate methods to satisfy that structurally; SFTPAdapter is not --
  # SFTPChannelPool is AdaptedSFTP's provider instead, so SFTPAdapter carries none of the three. ---

  @abstractmethod
  def _build_session(self, container_cls: str | None) -> SessionT:
    """Constructs a new session bound to this adapter as its handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    ...

  @abstractmethod
  def _keepalive_check_one(self) -> None:
    """Pops one idle handle and validates it, discarding it if the validation fails."""
    ...

  @abstractmethod
  def _teardown_idle(self) -> None:
    """Closes every idle connection, leaving checked-out ones untouched."""
    ...
```

- [ ] **Step 3: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/pool/base.py && uv run ruff check src/aeth_ext/ftp/pool/base.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/pool/base.py`
Expected: `0 errors, 0 warnings, 0 informations`. In particular, confirm there is **no**
`reportImportCycles` diagnostic (the `_AdapterBase` import is `TYPE_CHECKING`-only and `session.py` has
no dependency back on this module, so there should be nothing to flag).

Run: `uv run python -c "from aeth_ext.ftp.pool.base import _PooledAdapterBase, _TransportDialer; _PooledAdapterBase; _TransportDialer"`
Expected: no output, exit code 0.

---

## Task 5: Create `pool/sftp_channel_pool.py`

**Files:**
- Create: `src/aeth_ext/ftp/pool/sftp_channel_pool.py`
- (Old `src/aeth_ext/ftp/sftp_pool.py` is left untouched for now -- deleted in Task 9.)

**Interfaces:**
- Consumes: `_SFTPConnector` from `aeth_ext.ftp.connectors` (Task 2), under `TYPE_CHECKING`.
  `_TransportDialer` from `aeth_ext.ftp.pool.base` (Task 4), under `TYPE_CHECKING`. `IntrumentCallable`
  from `aeth_ext.ftp.types` (Task 1), under `TYPE_CHECKING`. `SizedBuffer` from `aeth_ext.types`
  (existing, unchanged), under `TYPE_CHECKING`.
- Produces: `Channel`, `ChannelLedger`, `SFTPChannelPool`, `TransportState` (public, `__all__`).
  `_LockedDict`, `_LockedList` (private -- renamed from today's `LockedDict`/`LockedList`, dropped from
  `__all__`; niche, single-consumer utility, not worth a public export per the user's explicit
  direction).
- **Changes from today's `sftp_pool.py`:** the local `_ChannelConnector` `Protocol` is deleted --
  `SFTPChannelPool.__init__`'s `connector` parameter is typed as concrete `_SFTPConnector`.
  `ChannelLedger.__init__`'s `transports` parameter is typed as concrete `_TransportDialer` (was
  `TransportProvider`). `LockedDict`/`LockedList` are renamed `_LockedDict`/`_LockedList` everywhere
  they're referenced (class definitions and `ChannelLedger.__init__`'s three instantiations). No other
  logic changes -- every method body, docstring, and the module's own `# ...` comments about *why* this
  module is separate from the pool adapters are otherwise unchanged (module docstring paths updated to
  match the new file locations).

- [ ] **Step 1: Create `src/aeth_ext/ftp/pool/sftp_channel_pool.py`**

```python
"""Two-tier (Transport, channel) bookkeeping for `SFTPAdapter`'s channel multiplexing.

`ChannelLedger` holds the shared, self-locking state; `SFTPChannelPool` makes every decision on top of
it and is `AdaptedSFTP`'s `HandleProvider`. Kept out of `pool/sftp_adapter.py` so the plain-FTP pooling
path (unchanged fixed one-connection-per-slot queue) isn't diluted by SFTP-only concepts, and kept
entirely out of `AdaptedSFTP` -- that class only ever sees a bare `SFTPClient` handler, identical in
shape to `AdaptedFTP`'s. This module is the only place that knows a checked-out `SFTPClient` came from a
specific `Transport`.
"""

# Standard library imports
import errno
from dataclasses import dataclass
from logging import getLogger
from queue import Queue
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar

# Third party imports
from paramiko import SSHException

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from typing import Any

  # Third party imports
  from paramiko import SFTPClient, Transport

  # First party imports
  from aeth_ext.ftp.connectors import _SFTPConnector
  from aeth_ext.ftp.pool.base import _TransportDialer
  from aeth_ext.ftp.types import IntrumentCallable
  from aeth_ext.types import SizedBuffer

__all__ = ["Channel", "ChannelLedger", "SFTPChannelPool", "TransportState"]

logger = getLogger(__name__)


def _mirror_builtin[F: Callable[..., Any]](source: Callable[..., Any]) -> Callable[[F], F]:
  """Best-effort: copies `source`'s docstring onto the decorated method, plus any real annotations
  `source` happens to carry. Builtins like `dict.pop`/`list.remove` never carry runtime annotations,
  so the latter is a no-op in practice -- `_LockedDict`/`_LockedList` methods keep their own hand-written
  generic annotations as the source of truth regardless. Deliberately skips `functools.wraps`, which
  would also set `__wrapped__`: `inspect.signature` follows that and tries to introspect the builtin's
  C signature, raising `ValueError` instead of reporting this method's own signature.
  """

  def decorator(func: F) -> F:
    func.__doc__ = source.__doc__
    if hasattr(source, "__annotations__"):
      func.__annotations__ = {**source.__annotations__, **func.__annotations__}
    return func

  return decorator


@dataclass(slots=True)
class TransportState:
  """Per-`Transport` bookkeeping: how many channels it currently holds and its measured throughput."""

  transport: Transport
  channel_count: int = 0
  ewma_throughput: float | None = None
  sample_count: int = 0

  _EWMA_ALPHA: ClassVar[float] = 0.3
  _MIN_SAMPLES: ClassVar[int] = 3
  _SATURATION_RATIO: ClassVar[float] = 0.6

  def update_throughput(self, nbytes: int, elapsed: float) -> None:
    """Blends a new sample into the running EWMA throughput estimate.

    Args:
      nbytes: Bytes transferred in this sample.
      elapsed: Seconds elapsed for this sample.
    """
    rate = nbytes / max(elapsed, 1e-6)
    if self.ewma_throughput is None:
      self.ewma_throughput = rate
    else:
      self.ewma_throughput = self._EWMA_ALPHA * rate + (1 - self._EWMA_ALPHA) * self.ewma_throughput
    self.sample_count += 1

  def is_saturated(self, best_throughput: float) -> bool:
    """Reports whether this transport's throughput is meaningfully below `best_throughput`.

    Args:
      best_throughput: The best throughput currently observed across all transports.

    Returns:
      `True` if saturated; always `False` until at least `_MIN_SAMPLES` samples have been recorded.
    """
    if self.ewma_throughput is None or self.sample_count < self._MIN_SAMPLES:
      return False
    return self.ewma_throughput < best_throughput * self._SATURATION_RATIO


@dataclass(slots=True)
class Channel:
  """A checked-out or idle SFTP handle, tagged with the `TransportState` it was opened from."""

  handle: SFTPClient
  state: TransportState


class _LockedDict[K, V]:
  """A dict whose mutating/reading operations are individually atomic under one shared lock.

  Mirrors `dict`'s own contract exactly -- no method here does anything a plain `dict` method
  wouldn't (same exceptions, same return values). `values()` copies under the lock and returns a
  plain list, so callers never iterate while holding the lock; callers who need what `clear()`
  removed should snapshot `values()` first.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: dict[K, V] = {}
    self._lock = lock

  @_mirror_builtin(dict.__setitem__)
  def __setitem__(self, key: K, value: V) -> None:
    with self._lock:
      self._data[key] = value

  @_mirror_builtin(dict.__getitem__)
  def __getitem__(self, key: K) -> V:
    with self._lock:
      return self._data[key]

  @_mirror_builtin(dict.__delitem__)
  def __delitem__(self, key: K) -> None:
    with self._lock:
      del self._data[key]

  @_mirror_builtin(dict.__contains__)
  def __contains__(self, key: K) -> bool:
    with self._lock:
      return key in self._data

  @_mirror_builtin(dict.__len__)
  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  @_mirror_builtin(dict.get)
  def get(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.get(key, default)

  @_mirror_builtin(dict.pop)
  def pop(self, key: K, default: V | None = None) -> V | None:
    with self._lock:
      return self._data.pop(key, default)

  @_mirror_builtin(dict.values)
  def values(self) -> list[V]:
    with self._lock:
      return list(self._data.values())

  @_mirror_builtin(dict.clear)
  def clear(self) -> None:
    with self._lock:
      self._data.clear()


class _LockedList[T]:
  """A list whose mutating/reading operations are individually atomic under one shared lock.

  Mirrors `list`'s own contract exactly -- same exceptions (`pop()` on empty raises `IndexError`,
  `remove()` on a missing item raises `ValueError`), same return values.
  """

  def __init__(self, lock: RLock) -> None:
    self._data: list[T] = []
    self._lock = lock

  @_mirror_builtin(list.append)
  def append(self, item: T) -> None:
    with self._lock:
      self._data.append(item)

  @_mirror_builtin(list.pop)
  def pop(self) -> T:
    with self._lock:
      return self._data.pop()

  @_mirror_builtin(list.remove)
  def remove(self, item: T) -> None:
    with self._lock:
      self._data.remove(item)

  @_mirror_builtin(list.__contains__)
  def __contains__(self, item: T) -> bool:
    with self._lock:
      return item in self._data

  @_mirror_builtin(list.__len__)
  def __len__(self) -> int:
    with self._lock:
      return len(self._data)

  @_mirror_builtin(list.copy)
  def copy(self) -> list[T]:
    with self._lock:
      return self._data.copy()

  @_mirror_builtin(list.clear)
  def clear(self) -> None:
    with self._lock:
      self._data.clear()


class ChannelLedger:
  """Shared, self-locking books for `SFTPChannelPool`. Holds data only -- every read/write here is a
  single, obvious operation; anything that has to touch more than one of these atomically (e.g. "is this
  transport saturated, and if not, put its channel back in `idle`") is `SFTPChannelPool`'s job, done with
  an explicit `with ledger.lock:` block, not a method on this class.
  """

  def __init__(self, transports: _TransportDialer) -> None:
    self.lock = RLock()
    self.transports = transports
    self.pool: SFTPChannelPool | None = None  # filled in by SFTPAdapter once the pool exists
    self.states: _LockedDict[int, TransportState] = _LockedDict(self.lock)
    self.handle_states: _LockedDict[int, TransportState] = _LockedDict(self.lock)
    self.idle: _LockedList[Channel] = _LockedList(self.lock)
    self.in_flight = 0
    self.wave_running_max = 0.0
    self.last_wave_best_throughput: float | None = None


class SFTPChannelPool:
  """Owns every acquire/release/growth/saturation decision on top of a `ChannelLedger`. `AdaptedSFTP`'s
  `HandleProvider`."""

  def __init__(self, ledger: ChannelLedger, connector: _SFTPConnector, channels_per_transport: int) -> None:
    """Initializes an empty pool bound to `ledger`'s state and `connector`'s connection-opening.

    Args:
      ledger: The shared bookkeeping this pool reads and writes.
      connector: Opens channels on an existing `Transport` and closes whole `Transport`s.
      channels_per_transport: Maximum channels to multiplex onto a single `Transport`.
    """
    self._ledger = ledger
    self._connector = connector
    self.channels_per_transport = channels_per_transport
    self._wakeup: Queue[None] = Queue()

  def acquire(self) -> tuple[SFTPClient, Sequence[IntrumentCallable]]:
    """Checks out an idle channel if one validates, else multiplexes a new channel onto an
    under-cap `Transport`, dials a brand new `Transport`, or (if the pool is fully saturated)
    blocks until a channel is released.

    Returns:
      The handle, plus a throughput-instrumentation observer callback for it.
    """
    channel = self._checkout_idle()
    if channel is not None and not self._validate(channel.handle):
      self._discard(channel)
      channel = None

    if channel is None:
      target = self._pick_growth_target()
      if target is None:
        transport = self._ledger.transports.open_transport()
        if transport is not None:
          target = TransportState(transport=transport)
          self._ledger.states[id(transport)] = target
      if target is not None:
        handle = self._connector.request_handler(target.transport)
        with self._ledger.lock:
          target.channel_count += 1
          self._ledger.handle_states[id(handle)] = target
          self._ledger.in_flight += 1
        channel = Channel(handle=handle, state=target)

    if channel is None:
      channel = self._checkout_blocking()

    return channel.handle, (self._make_instrument(channel.state),)

  def release(self, handle: SFTPClient, is_fatal: bool) -> None:
    """Returns a handle to its `Transport`'s pool, or discards it (and the whole `Transport` if it's
    no longer active) when `is_fatal` marks it broken.

    Args:
      handle: The handle to return or discard.
      is_fatal: Whether the connection is broken and should be discarded rather than pooled.
    """
    state = self._ledger.handle_states.get(id(handle))
    assert state is not None, "handle must have been tracked at checkout"
    channel = Channel(handle=handle, state=state)

    if not is_fatal:
      if self._release_or_pop_saturated(channel):
        self._close_quietly(channel.handle)
      return

    if state.transport.is_active():
      self._discard(channel)
      return

    orphaned = self._drop_transport(state)
    for orphan in (channel, *orphaned):
      self._close_quietly(orphan.handle)
    self._ledger.transports.transport_dropped()

  def teardown(self) -> None:
    """Closes every tracked `Transport` (and every channel opened on it).

    Reports each closed `Transport` to `ledger.transports.transport_dropped()` -- without this, a
    reused pool (e.g. tests calling `_shutdown_teardown()` directly) would see `SFTPAdapter._current_size`
    stuck at its pre-teardown value, blocking new growth unnecessarily.
    """
    with self._ledger.lock:
      states = self._ledger.states.values()
      self._ledger.states.clear()
      self._ledger.handle_states.clear()
      self._ledger.idle.clear()
    for state in states:
      self._close_quietly_transport(state.transport)
      self._ledger.transports.transport_dropped()

  def keepalive_check_one(self) -> None:
    """Pops and validates one idle channel, discarding it (and possibly its `Transport`) if the
    check fails."""
    channel = self._checkout_idle()
    if channel is None:
      return
    self.release(channel.handle, is_fatal=not self._validate(channel.handle))

  def _checkout_idle(self) -> Channel | None:
    """Pops an idle channel for reuse, or returns `None` if none is idle."""
    try:
      channel = self._ledger.idle.pop()
    except IndexError:
      return None
    with self._ledger.lock:
      self._ledger.in_flight += 1
    return channel

  def _checkout_blocking(self) -> Channel:
    """Blocks until an idle channel becomes available, then checks it out."""
    while True:
      channel = self._checkout_idle()
      if channel is not None:
        return channel
      self._wakeup.get()

  def _best_live_throughput(self) -> float | None:
    """Returns the best currently-tracked throughput, falling back to the last completed wave's best
    if no transport has a live sample yet."""
    samples = [s.ewma_throughput for s in self._ledger.states.values() if s.ewma_throughput is not None]
    if samples:
      return max(samples)
    return self._ledger.last_wave_best_throughput

  def _pick_growth_target(self) -> TransportState | None:
    """Returns a `TransportState` under its channel cap to open a new channel on, or `None` if
    every live `Transport` is at cap or saturated (the caller should dial a new `Transport` instead).
    """
    best = self._best_live_throughput()
    candidates = [
      s
      for s in self._ledger.states.values()
      if s.channel_count < self.channels_per_transport and not (best is not None and s.is_saturated(best))
    ]
    if not candidates:
      return None
    return min(candidates, key=lambda s: s.channel_count)

  def _release_or_pop_saturated(self, channel: Channel) -> bool:
    """Returns a checked-out channel to the pool, or pops it if its `Transport` is saturated.

    Args:
      channel: The channel being released.

    Returns:
      `True` if the channel was popped (its `Transport` is saturated -- caller must close the
      handle; `channel_count` has already been decremented here), `False` if it was returned to
      `ledger.idle` for reuse.
    """
    best = self._best_live_throughput()
    with self._ledger.lock:
      if best is not None and channel.state.is_saturated(best):
        channel.state.channel_count -= 1
        self._ledger.handle_states.pop(id(channel.handle), None)
        popped = True
      else:
        self._ledger.idle.append(channel)
        popped = False
    self._wakeup.put_nowait(None)  # a slot freed up either way -- saturated pop or a fresh idle channel
    self._mark_returned(channel.state)
    return popped

  def _mark_returned(self, state: TransportState) -> None:
    """Records a channel as no longer in-flight, folding its throughput into the current wave's max.

    Once every in-flight channel has returned, snapshots the wave's max into
    `ledger.last_wave_best_throughput` and resets for the next wave.

    Args:
      state: The `TransportState` of the channel that just returned.
    """
    with self._ledger.lock:
      if state.ewma_throughput is not None:
        self._ledger.wave_running_max = max(self._ledger.wave_running_max, state.ewma_throughput)
      self._ledger.in_flight -= 1
      if self._ledger.in_flight == 0:
        self._ledger.last_wave_best_throughput = self._ledger.wave_running_max
        self._ledger.wave_running_max = 0.0

  def _discard(self, channel: Channel) -> None:
    """Stops tracking a channel (removing it from `ledger.idle` if present) and closes its handle.

    Args:
      channel: The channel to discard.
    """
    with self._ledger.lock:
      channel.state.channel_count -= 1
      self._ledger.handle_states.pop(id(channel.handle), None)
      if channel in self._ledger.idle:
        self._ledger.idle.remove(channel)
    self._mark_returned(channel.state)
    self._close_quietly(channel.handle)

  def _drop_transport(self, state: TransportState) -> list[Channel]:
    """Stops tracking a `Transport`, returning whichever of its channels were sitting idle.

    Channels still checked out elsewhere are not returned here -- they aren't tracked in `ledger.idle`
    and fail naturally on their next I/O (see the multiplexing design's Error handling section).

    Args:
      state: The `TransportState` to stop tracking.

    Returns:
      Whichever of its channels were sitting idle.
    """
    with self._ledger.lock:
      self._ledger.states.pop(id(state.transport), None)
      all_idle = self._ledger.idle.copy()
      self._ledger.idle.clear()
      orphaned: list[Channel] = []
      for c in all_idle:
        if c.state is state:
          orphaned.append(c)
          self._ledger.handle_states.pop(id(c.handle), None)
        else:
          self._ledger.idle.append(c)
      return orphaned

  def _validate(self, handle: SFTPClient) -> bool:
    """Checks whether a handle responds to a `listdir(".")` round trip.

    Args:
      handle: The handle to check.

    Returns:
      `True` if `handle` is still usable.
    """
    try:
      handle.listdir(".")
      return True
    except OSError as exc:
      # paramiko maps SFTP_PERMISSION_DENIED/SFTP_NO_SUCH_FILE status replies to IOError with
      # these errnos -- a non-listable root still means the server answered, so the channel is
      # fine. Any other OSError (socket reset, broken pipe, ...) means it isn't.
      return exc.errno in (errno.EACCES, errno.ENOENT)
    except SSHException, EOFError:
      # SSHException: protocol/transport failure. EOFError: listdir_attr() already swallows the
      # normal end-of-listing EOF internally, so one reaching here means the channel closed
      # mid-request. Either way the connection is unusable.
      return False

  def _close_quietly(self, handle: SFTPClient) -> None:
    """Best-effort close of a channel handle, swallowing any error.

    Args:
      handle: The handle to close.
    """
    try:
      handle.close()
    except Exception as e:
      logger.warning("Error closing SFTP channel handle: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP channel handle close error", exc_info=e)

  def _close_quietly_transport(self, transport: Transport) -> None:
    """Best-effort close of a whole `Transport` via the connector, swallowing any error.

    Args:
      transport: The `Transport` to close.
    """
    try:
      self._connector.close_conn_handler(transport)
    except Exception as e:
      logger.warning("Error closing SFTP transport: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP transport close error", exc_info=e)

  def _make_instrument(self, state: TransportState) -> IntrumentCallable:
    """Builds a per-checkout observer callback that feeds elapsed-time-weighted throughput samples
    into a `TransportState`.

    Args:
      state: The `TransportState` to feed throughput samples into.

    Returns:
      The observer callback.
    """
    last_sample = monotonic()

    def observer(data: SizedBuffer) -> None:
      """Records the bytes transferred since the last call as a throughput sample.

      Args:
        data: The chunk just transferred.
      """
      nonlocal last_sample
      now = monotonic()
      state.update_throughput(len(data), now - last_sample)
      last_sample = now

    return observer
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/pool/sftp_channel_pool.py && uv run ruff check src/aeth_ext/ftp/pool/sftp_channel_pool.py`
Expected: no issues. In particular, ruff must not flag the bare `except SSHException, EOFError:` --
`.claude/CLAUDE.md` documents this as valid PEP 758 syntax for this project; if ruff *does* flag it,
stop and re-read `.claude/CLAUDE.md`'s "Exception Handling" section before changing anything (do not
"fix" it into parenthesized form).

Run: `uv run pyright src/aeth_ext/ftp/pool/sftp_channel_pool.py`
Expected: `0 errors, 0 warnings, 0 informations`. In particular, confirm there is **no**
`reportImportCycles` diagnostic for the `_TransportDialer` import from `pool.base`, and none for
`_SFTPConnector` from `connectors`.

Run: `uv run python -c "from aeth_ext.ftp.pool.sftp_channel_pool import Channel, ChannelLedger, SFTPChannelPool, TransportState; Channel; ChannelLedger; SFTPChannelPool; TransportState"`
Expected: no output, exit code 0.

---

## Task 6: Create `pool/ftp_adapter.py`

**Files:**
- Create: `src/aeth_ext/ftp/pool/ftp_adapter.py`

**Interfaces:**
- Consumes: `_FTPConnector` from `aeth_ext.ftp.connectors` (Task 2, real import -- constructed in
  `__init__`). `_PooledAdapterBase` from `aeth_ext.ftp.pool.base` (Task 4, real import -- base class).
  `AdaptedFTP` from `aeth_ext.ftp.session` (Task 3, real import -- base class type parameter and
  constructed in `_build_session`). `FTPCredentials` from `aeth_ext.ftp.credentials` (existing,
  `TYPE_CHECKING` -- only used as a parameter annotation, never constructed here).
- Produces: `FTPAdapter` (public, `__all__`). No logic changes from today's `adapter.py` -- copied
  verbatim.

- [ ] **Step 1: Create `src/aeth_ext/ftp/pool/ftp_adapter.py`**

```python
"""`FTPAdapter`: fixed one-connection-per-slot pooling for plain FTP. No transport/channel tiers --
each pooled connection is a single, self-contained `FTP`/`FTP_TLS` object, so pooling is just an idle
queue plus `_PooledAdapterBase`'s shared ceiling bookkeeping.
"""

# Standard library imports
from ftplib import FTP
from queue import Empty, Queue
from typing import TYPE_CHECKING, override

# First party imports
from aeth_ext.ftp.connectors import _FTPConnector
from aeth_ext.ftp.pool.base import _PooledAdapterBase
from aeth_ext.ftp.session import AdaptedFTP
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from contextvars import ContextVar
  from typing import Any
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.ftp.credentials import FTPCredentials
  from aeth_ext.rich.progress import Progress


SETTINGS = BaseSettings.get_settings()


__all__ = ["FTPAdapter"]


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
    """Builds an FTP connection pool with an initially-empty idle queue.

    Args:
      credentials: The FTP server credentials to connect with.
      max_connections: Ceiling on concurrently open connections.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle connections; `None` disables it.
    """
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
    self._idle: Queue[FTP] = Queue(maxsize=max_connections)

  def acquire(self) -> tuple[FTP, Sequence[Callable[[bytes], Any]]]:
    """Checks out an idle connection if one validates, else opens (or waits for) a new one.

    Returns:
      The handle, with no handle-scoped observer callbacks (FTP has none to attach).
    """
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

  def release(self, handle: FTP, is_fatal: bool) -> None:
    """Discards a handle if fatal, else returns it to the idle queue for reuse.

    Args:
      handle: The handle to return or discard.
      is_fatal: Whether the connection is broken and should be discarded rather than pooled.
    """
    if is_fatal:
      with self._size_lock:
        self._current_size -= 1
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
        pass
    else:
      self._idle.put(handle)

  def _validate(self, handle: FTP) -> bool:
    """Checks whether a handle responds to a `NOOP` round trip.

    Args:
      handle: The handle to check.

    Returns:
      `True` if `handle` is still usable.
    """
    try:
      handle.voidcmd("NOOP")
      return True
    except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
      return False

  @override
  def _keepalive_check_one(self) -> None:
    """Pops and validates one idle connection, discarding it if the check fails."""
    try:
      handle = self._idle.get_nowait()
    except Empty:
      return
    self.release(handle, is_fatal=not self._validate(handle))

  @override
  def _teardown_idle(self) -> None:
    """Closes every idle connection, leaving checked-out ones untouched.

    Decrements `_current_size` per drained handle -- without this, a reused adapter (e.g. tests
    calling `_shutdown_teardown()` directly) would see an inflated size that never comes back down,
    making `_effective_ceiling()` block new checkouts unnecessarily.
    """
    while True:
      try:
        handle = self._idle.get_nowait()
      except Empty:
        break
      with self._size_lock:
        self._current_size -= 1
      try:
        self._connector.close_conn_handler(handle)
      except Exception:  # noqa: BLE001, S110 -- best-effort close during teardown
        pass

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedFTP:
    """Builds a new `AdaptedFTP` session bound to this adapter as its handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    return AdaptedFTP(self, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/pool/ftp_adapter.py && uv run ruff check src/aeth_ext/ftp/pool/ftp_adapter.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/pool/ftp_adapter.py`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run python -c "from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter; FTPAdapter"`
Expected: no output, exit code 0.

---

## Task 7: Create `pool/sftp_adapter.py`

**Files:**
- Create: `src/aeth_ext/ftp/pool/sftp_adapter.py`

**Interfaces:**
- Consumes: `_SFTPConnector` from `aeth_ext.ftp.connectors` (Task 2, real). `_PooledAdapterBase` from
  `aeth_ext.ftp.pool.base` (Task 4, real -- base class, and `_make_transport_dialer` is inherited from
  it). `ChannelLedger`, `SFTPChannelPool` from `aeth_ext.ftp.pool.sftp_channel_pool` (Task 5, real --
  constructed in `__init__`). `AdaptedSFTP` from `aeth_ext.ftp.session` (Task 3, real). `SFTPCredentials`
  from `aeth_ext.ftp.credentials` (existing, `TYPE_CHECKING`).
- Produces: `SFTPAdapter` (public, `__all__`).
- **Change from today's `adapter.py`:** `SFTPAdapter.open_transport`/`SFTPAdapter.transport_dropped`
  (the two methods that made `SFTPAdapter` structurally satisfy the now-deleted `TransportProvider`) are
  **removed entirely** -- their logic now lives inside `_PooledAdapterBase._make_transport_dialer`'s two
  closures (Task 4), and `SFTPAdapter.__init__` calls `self._make_transport_dialer(...)` instead of
  passing `self` to `ChannelLedger`. Everything else (the `__init__` body's connector/ledger/pool wiring
  order, `_keepalive_check_one`, `_teardown_idle`, `_build_session`) is copied verbatim.

- [ ] **Step 1: Create `src/aeth_ext/ftp/pool/sftp_adapter.py`**

```python
"""`SFTPAdapter`: dials `Transport`s within a shared ceiling and hands every channel-multiplexing
decision to `SFTPChannelPool` (`pool.sftp_channel_pool`) via a `ChannelLedger`. `SFTPAdapter` itself
never sees a `SFTPClient` handle or makes an acquire/release decision -- `AdaptedSFTP`'s `HandleProvider`
is `SFTPChannelPool`, not this class.
"""

# Standard library imports
from typing import TYPE_CHECKING, override

# First party imports
from aeth_ext.ftp.connectors import _SFTPConnector
from aeth_ext.ftp.pool.base import _PooledAdapterBase
from aeth_ext.ftp.pool.sftp_channel_pool import ChannelLedger, SFTPChannelPool
from aeth_ext.ftp.session import AdaptedSFTP
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from contextvars import ContextVar
  from zoneinfo import ZoneInfo

  # Third party imports
  from paramiko import SFTPClient

  # First party imports
  from aeth_ext.ftp.credentials import SFTPCredentials
  from aeth_ext.rich.progress import Progress


SETTINGS = BaseSettings.get_settings()


__all__ = ["SFTPAdapter"]


class SFTPAdapter(_PooledAdapterBase[AdaptedSFTP, SFTPClient]):
  __slots__ = ("_connector", "_ledger")

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
    """Builds an SFTP `Transport`/channel pool wired to an initially-empty `ChannelLedger`.

    Args:
      credentials: The SFTP server credentials to connect with.
      max_connections: Ceiling on concurrently open `Transport`s.
      chunk_size: Bytes read/written per I/O call by sessions built from this pool.
      pbar: Progress reporter for sessions to report against, if any.
      tzinfo: Timezone used to localize server-reported modification times.
      container_cls: Fallback label attached to log messages when `container_cvar` is unset or unbound.
      container_cvar: Preferred source for the container-label, resolved fresh per session.
      keepalive_interval: Seconds between keepalive pings on idle channels; `None` disables it.
      channels_per_transport: Maximum channels to multiplex onto a single `Transport`.
    """
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
    self._ledger = ChannelLedger(transports=self._make_transport_dialer(self._connector.get_transport))
    pool = SFTPChannelPool(self._ledger, self._connector, channels_per_transport)
    self._ledger.pool = pool

  @override
  def _keepalive_check_one(self) -> None:
    """Pops one idle channel and validates it, discarding it (and possibly its `Transport`) if the
    validation fails."""
    assert self._ledger.pool is not None
    self._ledger.pool.keepalive_check_one()

  @override
  def _teardown_idle(self) -> None:
    """Closes every tracked `Transport` (and every channel opened on it)."""
    assert self._ledger.pool is not None
    self._ledger.pool.teardown()

  @override
  def _build_session(self, container_cls: str | None) -> AdaptedSFTP:
    """Builds a new `AdaptedSFTP` session bound to `SFTPChannelPool` (not this adapter) as its
    handle provider.

    Args:
      container_cls: Label to attach to log messages the session emits.

    Returns:
      The new session.
    """
    assert self._ledger.pool is not None
    return AdaptedSFTP(self._ledger.pool, container_cls=container_cls, pbar=self.pbar, tzinfo=self.tzinfo, chunk_size=self.chunk_size)
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/pool/sftp_adapter.py && uv run ruff check src/aeth_ext/ftp/pool/sftp_adapter.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/pool/sftp_adapter.py`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run python -c "from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter; SFTPAdapter"`
Expected: no output, exit code 0.

---

## Task 8: Create `factory.py`

**Files:**
- Create: `src/aeth_ext/ftp/factory.py`

**Interfaces:**
- Consumes: `FTPCredentials`, `SFTPCredentials` from `aeth_ext.ftp.credentials` (real -- `isinstance`
  check). `FTPAdapter` from `aeth_ext.ftp.pool.ftp_adapter` (Task 6, real). `SFTPAdapter` from
  `aeth_ext.ftp.pool.sftp_adapter` (Task 7, real).
- Produces: `create_ftp_adapter` (public, `__all__`). No logic changes from today's `adapter.py` --
  copied verbatim.

- [ ] **Step 1: Create `src/aeth_ext/ftp/factory.py`**

```python
"""`create_ftp_adapter`: the entry point most consumers should use -- dispatches to `FTPAdapter` or
`SFTPAdapter` based on which credentials type it's given, so callers don't need an `isinstance` check
of their own.
"""

# Standard library imports
from typing import TYPE_CHECKING, overload

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter
from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from contextvars import ContextVar
  from typing import Any
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.rich.progress import Progress


SETTINGS = BaseSettings.get_settings()


__all__ = ["create_ftp_adapter"]


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
  """Builds an `FTPAdapter` or `SFTPAdapter`, chosen by `credentials`'s type.

  Args:
    credentials: FTP or SFTP server credentials; determines which adapter type is built.
    **kwargs: Forwarded to `FTPAdapter`/`SFTPAdapter`'s constructor.

  Returns:
    An `FTPAdapter` for `FTPCredentials`, or an `SFTPAdapter` for `SFTPCredentials`.
  """
  if isinstance(credentials, FTPCredentials):
    return FTPAdapter(credentials, **kwargs)
  return SFTPAdapter(credentials, **kwargs)
```

- [ ] **Step 2: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/factory.py && uv run ruff check src/aeth_ext/ftp/factory.py`
Expected: no issues.

Run: `uv run pyright src/aeth_ext/ftp/factory.py`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run python -c "from aeth_ext.ftp.factory import create_ftp_adapter; create_ftp_adapter"`
Expected: no output, exit code 0.

---

## Task 9: Flip the package -- create `ftp/__init__.py`, delete `adapter.py` and old `sftp_pool.py`

**Files:**
- Create/overwrite: `src/aeth_ext/ftp/__init__.py` (currently 0 bytes)
- Delete: `src/aeth_ext/ftp/adapter.py`
- Delete: `src/aeth_ext/ftp/sftp_pool.py`

**Interfaces:**
- Consumes: everything built in Tasks 1-8.
- Produces: the package's public surface -- `from aeth_ext.ftp import <name>` now works for
  `AdaptedFTP`, `AdaptedSFTP`, `FTPAdapter`, `SFTPAdapter`, `create_ftp_adapter`, `FTPCredentials`,
  `SFTPCredentials`, `HandleProvider`, `ListDirResult`, `ServerNotAvailableError`.

This is the one task in this plan that intentionally breaks the existing test suite's imports (they
still say `from aeth_ext.ftp.adapter import ...`, and that module is about to stop existing) -- Task 10
fixes every test file. Do not be alarmed when `uv run pytest tests/ftp/` fails to collect after this
task; that is expected and temporary. This task's own verification (below) is a smoke-level check, not
the test suite.

- [ ] **Step 1: Write `src/aeth_ext/ftp/__init__.py`**

```python
"""Public entry points for FTP/SFTP connection pooling and one-shot transfer sessions.

`create_ftp_adapter` is the primary entry point for most consumers (or `FTPAdapter`/`SFTPAdapter`
directly, if you want the concrete type without the `isinstance` dispatch). Everything else in
`aeth_ext.ftp`'s submodules is an internal implementation detail, not a supported import path.
"""

# Local folder imports
from .credentials import FTPCredentials, SFTPCredentials
from .errors import ServerNotAvailableError
from .factory import create_ftp_adapter
from .pool.ftp_adapter import FTPAdapter
from .pool.sftp_adapter import SFTPAdapter
from .session import AdaptedFTP, AdaptedSFTP
from .types import HandleProvider, ListDirResult

__all__ = [
  "AdaptedFTP",
  "AdaptedSFTP",
  "FTPAdapter",
  "FTPCredentials",
  "HandleProvider",
  "ListDirResult",
  "SFTPAdapter",
  "SFTPCredentials",
  "ServerNotAvailableError",
  "create_ftp_adapter",
]
```

- [ ] **Step 2: Delete the two superseded files**

Delete `src/aeth_ext/ftp/adapter.py` and `src/aeth_ext/ftp/sftp_pool.py` entirely. Every class either
file used to define now lives in one of the Task 1-8 files; nothing is lost.

- [ ] **Step 3: Verify**

Run: `uv run ruff check --fix src/aeth_ext/ftp/__init__.py && uv run ruff check src/aeth_ext/ftp/`
Expected: no issues anywhere under `src/aeth_ext/ftp/` (this now covers every file from Tasks 1-9 at
once, since `adapter.py`/`sftp_pool.py` -- the only files that could have stale references -- are gone).

Run: `uv run pyright src/aeth_ext/ftp/`
Expected: `0 errors, 0 warnings, 0 informations`. This is the strongest check in this task: if any
import in the whole new module graph is wrong, missing, or forms a real or type-only cycle, it surfaces
here across the *whole* package at once, not just one file at a time.

Run: `uv run python -c "from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP, FTPAdapter, SFTPAdapter, FTPCredentials, SFTPCredentials, HandleProvider, ListDirResult, ServerNotAvailableError, create_ftp_adapter; print('ok')"`
Expected: prints `ok`, exit code 0. This constructs the entire import graph end to end (package init ->
factory -> both pool adapters -> connectors/session/pool.base/pool.sftp_channel_pool) in one process; a
real cycle would raise `ImportError: cannot import name X from partially initialized module` here.

Run: `uv run python -c "
from aeth_ext.ftp import FTPCredentials, create_ftp_adapter
adapter = create_ftp_adapter(FTPCredentials(host='127.0.0.1', username='u', password='p', port=1))
print(type(adapter).__name__)
"`
Expected: prints `FTPAdapter`, exit code 0 (confirms the whole factory-to-pool-to-connector wiring
actually constructs an object, not just that the imports resolve).

Confirm (do not just assume) the old files are gone:
Run: `ls src/aeth_ext/ftp/adapter.py src/aeth_ext/ftp/sftp_pool.py` (or `Test-Path` on Windows
PowerShell) -- expected: both report "No such file" / `False`.

At this point, `uv run pytest tests/ftp/` **will fail to collect** with `ModuleNotFoundError` /
`ImportError` for `aeth_ext.ftp.adapter` and `aeth_ext.ftp.sftp_pool` across most test files. This is
expected -- do not attempt to fix it in this task. Task 10 fixes every test file.

---

## Task 10: Update all `tests/ftp/*.py` files

**Files:**
- Modify: `tests/ftp/conftest.py`
- Modify: `tests/ftp/test_adapter_ftp.py`
- Modify: `tests/ftp/test_adapter_sftp.py`
- Modify: `tests/ftp/test_transfer.py`
- Modify: `tests/ftp/test_ftp_adapter_factory.py`
- Create: `tests/ftp/test_sftp_channel_pool.py`
- Delete: `tests/ftp/test_sftp_pool.py`
- No change needed: `tests/ftp/test_credentials.py`, `tests/ftp/test_types_and_errors.py` (neither
  imports anything from `adapter.py`/`sftp_pool.py`; grep confirmed this before writing this plan --
  re-confirm with the grep in Step 1 before skipping them).

- [ ] **Step 1: Confirm the full scope with a grep before editing anything**

Run: `grep -rn "aeth_ext\.ftp\.adapter\|aeth_ext\.ftp\.sftp_pool\|LockedDict\|LockedList\|TransportProvider" tests/ftp/`

Expected output should be limited to the files/lines this task's remaining steps cover. If it finds
anything in `test_credentials.py` or `test_types_and_errors.py`, stop and re-read those two files before
proceeding -- this plan's earlier research (done against `main` before this branch's Tasks 1-9 ran)
found neither file needs changes, but confirm against current reality rather than trusting that blindly.

- [ ] **Step 2: `tests/ftp/conftest.py` -- one import line, one docstring line**

Change (around line 5-6, in the module docstring):
```
exercise the actual `ftplib`/`paramiko` wire protocols `aeth_ext.ftp.adapter`
talks to.
```
to:
```
exercise the actual `ftplib`/`paramiko` wire protocols `aeth_ext.ftp`
talks to.
```

Change (line 25):
```python
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP
```
to:
```python
from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP
```

- [ ] **Step 3: `tests/ftp/test_adapter_ftp.py` -- one import line, one docstring line**

Change (line 1):
```python
"""Tests for `aeth_ext.ftp.adapter.AdaptedFTP` against a real local `pyftpdlib` server."""
```
to:
```python
"""Tests for `aeth_ext.ftp.AdaptedFTP` against a real local `pyftpdlib` server."""
```

Change (line 15, inside the `if TYPE_CHECKING:` block):
```python
  from aeth_ext.ftp.adapter import AdaptedFTP
```
to:
```python
  from aeth_ext.ftp import AdaptedFTP
```

- [ ] **Step 4: `tests/ftp/test_adapter_sftp.py` -- imports, docstring, and the `TestPoolWiring` rewrite**

Change (line 1):
```python
"""Tests for `aeth_ext.ftp.adapter.AdaptedSFTP` against a real loopback `paramiko` SFTP server."""
```
to:
```python
"""Tests for `aeth_ext.ftp.AdaptedSFTP` against a real loopback `paramiko` SFTP server."""
```

Change (line 18, inside the `if TYPE_CHECKING:` block):
```python
  from aeth_ext.ftp.adapter import AdaptedSFTP
```
to:
```python
  from aeth_ext.ftp import AdaptedSFTP
```

Replace the entire `TestPoolWiring` class (currently the last class in the file, both its methods
constructing `SFTPAdapter` directly and importing from `aeth_ext.ftp.adapter`/`aeth_ext.ftp.credentials`
separately) with:

```python
class TestPoolWiring:
  def test_build_session_hands_the_pool_not_the_adapter_as_provider(self) -> None:
    # First party imports
    from aeth_ext.ftp import SFTPAdapter, SFTPCredentials

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"))  # pyright: ignore[reportArgumentType]

    session = adapter.start_session()

    assert session._provider is adapter._ledger.pool  # pyright: ignore[reportPrivateUsage]
    assert session._provider is not adapter  # pyright: ignore[reportPrivateUsage]

  def test_transport_dialer_delegates_to_the_adapters_slot_bookkeeping(self) -> None:
    """`SFTPAdapter` no longer implements `open_transport`/`transport_dropped` itself (that
    responsibility moved to `_TransportDialer`, built once in `__init__` and stored on
    `adapter._ledger.transports`) -- exercise the same behavior through that new indirection."""
    # First party imports
    from aeth_ext.ftp import SFTPAdapter, SFTPCredentials

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"), max_connections=1)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 1  # pyright: ignore[reportPrivateUsage] -- simulate the ceiling already reached

    assert adapter._ledger.transports.open_transport() is None  # pyright: ignore[reportPrivateUsage] -- _open_new_slot refuses past max_connections

    adapter._ledger.transports.transport_dropped()  # pyright: ignore[reportPrivateUsage]

    assert adapter._current_size == 0  # pyright: ignore[reportPrivateUsage]
```

(This is a direct behavioral replacement, not a weakened test: it verifies the exact same ceiling-refusal
and drop-decrements-size behavior as before, just reached through `adapter._ledger.transports` instead
of `adapter` directly, matching where the logic actually lives now.)

- [ ] **Step 5: `tests/ftp/test_transfer.py` -- one import line**

Change (line 23, inside the `if TYPE_CHECKING:` block):
```python
  from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP
```
to:
```python
  from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP
```

- [ ] **Step 6: `tests/ftp/test_ftp_adapter_factory.py` -- docstring, import, and four monkeypatch dotted paths**

Change (line 1):
```python
"""Tests for `aeth_ext.ftp.adapter.create_ftp_adapter`/`FTPAdapter`/`SFTPAdapter`."""
```
to:
```python
"""Tests for `aeth_ext.ftp.create_ftp_adapter`/`FTPAdapter`/`SFTPAdapter`."""
```

Change (line 16):
```python
from aeth_ext.ftp.adapter import AdaptedSFTP, FTPAdapter, SFTPAdapter, create_ftp_adapter
```
to:
```python
from aeth_ext.ftp import AdaptedSFTP, FTPAdapter, SFTPAdapter, create_ftp_adapter
```

Change **all four** occurrences of (they appear in
`test_refused_growth_pins_discovered_max`,
`test_subsequent_checkouts_respect_discovered_max_without_reattempting`,
`test_reprobe_after_interval_raises_discovered_max_on_success`, and
`test_reprobe_within_interval_does_not_reattempt` -- search for the exact string, don't rely on line
numbers, since earlier edits in this file shift them):
```python
    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _limited_get_transport)
```
and the one occurrence with `_gated_get_transport` instead of `_limited_get_transport`:
```python
    monkeypatch.setattr("aeth_ext.ftp.adapter._FTPConnector.get_transport", _gated_get_transport)
```
to (respectively, same variable name each time, only the dotted path changes):
```python
    monkeypatch.setattr("aeth_ext.ftp.connectors._FTPConnector.get_transport", _limited_get_transport)
```
```python
    monkeypatch.setattr("aeth_ext.ftp.connectors._FTPConnector.get_transport", _gated_get_transport)
```

**Why this specific path and not `aeth_ext.ftp._FTPConnector...` or the old one:** `monkeypatch.setattr`
with a dotted string patches the attribute on the module that actually defines the name -- `_FTPConnector`
is defined in (and only in) `aeth_ext.ftp.connectors` after this reorg, regardless of where else it gets
imported. Patching any other path (e.g. via the `aeth_ext.ftp` package re-export) would patch a
different name binding and silently not affect the real class method `FTPAdapter.acquire()` actually calls.

Change (in `test_reprobe_after_interval_raises_discovered_max_on_success`):
```python
    monkeypatch.setattr("aeth_ext.ftp.adapter.monotonic", lambda: monotonic() + 10_000)
```
to:
```python
    monkeypatch.setattr("aeth_ext.ftp.pool.base.monotonic", lambda: monotonic() + 10_000)
```
(`monotonic` is called inside `_PooledAdapterBase._effective_ceiling`/`_open_new_slot`, both now in
`aeth_ext.ftp.pool.base` -- same reasoning as above: patch where the name is actually looked up from.)

Change (in `test_registers_for_shutdown_on_first_connection`):
```python
    monkeypatch.setattr(
      "aeth_ext.ftp.adapter.register_for_shutdown",
      lambda callback, *, phase, priority=0, required=False: registered.append((callback, phase.name)),
    )
```
to:
```python
    monkeypatch.setattr(
      "aeth_ext.ftp.pool.base.register_for_shutdown",
      lambda callback, *, phase, priority=0, required=False: registered.append((callback, phase.name)),
    )
```
(`register_for_shutdown` is called inside `_PooledAdapterBase._ensure_registered_for_shutdown`, now in
`aeth_ext.ftp.pool.base`.)

Everything else in this file (`adapter._current_size`, `adapter._discovered_max`, `adapter._idle`,
`adapter._ledger.handle_states`, `adapter._keepalive_thread`, `adapter._callbacks`,
`adapter._shutdown_teardown()`) is instance-attribute access through the `adapter`/`session` objects
themselves, not module-path-based -- none of it needs to change; those attributes live in the same
place (on the instance) regardless of which module defines the class.

- [ ] **Step 7: Create `tests/ftp/test_sftp_channel_pool.py`, delete `tests/ftp/test_sftp_pool.py`**

Create `tests/ftp/test_sftp_channel_pool.py` with this content (identical test coverage to today's
`test_sftp_pool.py`, updated for the renamed import path and the `LockedDict`/`LockedList` ->
`_LockedDict`/`_LockedList` rename -- note the two `# pyright: ignore[reportArgumentType]` additions on
`_make_pool`, needed because `_FakeConnector`/`_FakeTransportProvider` are duck-typed test doubles, not
real `_SFTPConnector`/`_TransportDialer` instances, and those parameters are now concretely typed
instead of Protocol-typed):

```python
"""Unit tests for `aeth_ext.ftp.pool.sftp_channel_pool` -- pure bookkeeping, no real network."""

# Standard library imports
import threading
from typing import override

# Third party imports
import pytest
from paramiko import SFTPClient, Transport

# First party imports
from aeth_ext.ftp.pool.sftp_channel_pool import (
  Channel,
  ChannelLedger,
  SFTPChannelPool,
  TransportState,
  _LockedDict,
  _LockedList,
)


class _FakeTransport(Transport):
  """Stands in for `paramiko.Transport` -- the pool only ever holds it as a dict key/attribute or
  calls `.is_active()`/`.close()` on it, so a real handshake is never needed."""

  def __init__(self, *, active: bool = True) -> None:
    self._active = active  # deliberately skip Transport.__init__

  @override
  def is_active(self) -> bool:
    return self._active

  @override
  def close(self) -> None:
    self._active = False


class _FakeChannel(SFTPClient):
  """Stands in for `paramiko.SFTPClient` -- same reasoning as `_FakeTransport`."""

  def __init__(self) -> None:
    self.closed = False  # deliberately skip SFTPClient.__init__

  @override
  def close(self) -> None:
    self.closed = True

  @override
  def listdir(self, path: str = ".") -> list[str]:
    return []


class _FakeConnector:
  """Stands in for `_SFTPConnector` -- hands out fresh `_FakeChannel`s and no-ops on transport close."""

  def request_handler(self, transport: Transport) -> SFTPClient:
    return _FakeChannel()

  def close_conn_handler(self, handle: Transport) -> None:
    handle.close()


class _FakeTransportProvider:
  """Stands in for `_TransportDialer` -- dials up to `ceiling` fake transports. Duck-typed rather than a
  real `_TransportDialer`, since `_TransportDialer` wraps a real `_PooledAdapterBase`'s bookkeeping that
  this pure-bookkeeping test suite has no need to construct."""

  def __init__(self, ceiling: int = 100) -> None:
    self.ceiling = ceiling
    self.opened: list[Transport] = []
    self.dropped_count = 0

  def open_transport(self) -> Transport | None:
    if len(self.opened) >= self.ceiling:
      return None
    transport = _FakeTransport()
    self.opened.append(transport)
    return transport

  def transport_dropped(self) -> None:
    self.dropped_count += 1


def _make_pool(channels_per_transport: int = 4, ceiling: int = 100) -> tuple[SFTPChannelPool, ChannelLedger, _FakeTransportProvider]:
  provider = _FakeTransportProvider(ceiling)
  ledger = ChannelLedger(transports=provider)  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeTransportProvider's docstring
  pool = SFTPChannelPool(ledger, _FakeConnector(), channels_per_transport)  # pyright: ignore[reportArgumentType] -- duck-typed fake, see _FakeConnector's docstring
  ledger.pool = pool
  return pool, ledger, provider


class TestLockedDict:
  def test_setitem_getitem_roundtrip(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    assert d[1] == "a"
    assert 1 in d

  def test_pop_and_delitem(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    assert d.pop(1) == "a"
    assert d.pop(1, "default") == "default"
    d[2] = "b"
    del d[2]
    assert 2 not in d  # noqa: PLR2004

  def test_values_snapshots_under_lock(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    assert sorted(d.values()) == ["a", "b"]

  def test_clear_empties(self) -> None:
    d: _LockedDict[int, str] = _LockedDict(threading.RLock())
    d[1] = "a"
    d[2] = "b"
    d.clear()
    assert len(d) == 0

  def test_concurrent_mutation_never_corrupts_state(self) -> None:
    d: _LockedDict[int, int] = _LockedDict(threading.RLock())

    def writer(start: int) -> None:
      for i in range(start, start + 200):
        d[i] = i

    threads = [threading.Thread(target=writer, args=(base,)) for base in (0, 1000, 2000)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert len(d) == 600  # noqa: PLR2004 -- 3 threads * 200 unique keys each


class TestLockedList:
  def test_append_pop_contains(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert 1 in lst
    assert lst.pop() == 2  # noqa: PLR2004
    assert len(lst) == 1

  def test_pop_empty_raises_index_error(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    with pytest.raises(IndexError):
      lst.pop()

  def test_remove_missing_item_raises_value_error(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    with pytest.raises(ValueError, match="not in list"):
      lst.remove(99)

  def test_copy_snapshots_without_mutating(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    assert lst.copy() == [1, 2]
    assert len(lst) == 2  # noqa: PLR2004

  def test_clear_empties(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())
    lst.append(1)
    lst.append(2)
    lst.clear()
    assert len(lst) == 0

  def test_concurrent_append_never_corrupts_state(self) -> None:
    lst: _LockedList[int] = _LockedList(threading.RLock())

    def writer() -> None:
      for i in range(200):
        lst.append(i)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert len(lst) == 600  # noqa: PLR2004 -- 3 threads * 200 appends each


class TestChannelReuseUnderCap:
  def test_new_pool_has_no_growth_target(self) -> None:
    pool, _ledger, _provider = _make_pool(channels_per_transport=4)

    assert pool._pick_growth_target() is None  # pyright: ignore[reportPrivateUsage]

  def test_acquire_with_no_idle_channel_dials_a_new_transport(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)

    handle, _callbacks = pool.acquire()

    assert len(provider.opened) == 1
    assert ledger.handle_states.get(id(handle)) is not None

  def test_transport_at_channel_cap_is_not_a_growth_target(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=2)
    state = TransportState(transport=_FakeTransport())
    ledger.states[id(state.transport)] = state
    state.channel_count = 2

    assert pool._pick_growth_target() is None  # pyright: ignore[reportPrivateUsage]

  def test_released_channel_is_reused_on_next_acquire(self) -> None:
    pool, _ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=False)
    reused, _ = pool.acquire()

    assert reused is handle
    assert len(provider.opened) == 1  # no second Transport dialed -- the idle channel was reused


class TestHandleToStateLookup:
  def test_untracked_handle_resolves_to_none(self) -> None:
    _pool, ledger, _provider = _make_pool()

    assert ledger.handle_states.get(id(_FakeChannel())) is None

  def test_acquired_handle_resolves_to_its_state(self) -> None:
    pool, ledger, _provider = _make_pool()

    handle, _ = pool.acquire()

    assert ledger.handle_states.get(id(handle)) is not None


class TestSaturationRouting:
  def test_growth_target_excludes_a_saturated_transport(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    fast = TransportState(transport=_FakeTransport())
    slow = TransportState(transport=_FakeTransport())
    ledger.states[id(fast.transport)] = fast
    ledger.states[id(slow.transport)] = slow
    for _ in range(TransportState._MIN_SAMPLES):  # pyright: ignore[reportPrivateUsage]
      fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
      slow.update_throughput(nbytes=100, elapsed=1.0)

    target = pool._pick_growth_target()  # pyright: ignore[reportPrivateUsage]

    assert target is fast

  def test_releasing_a_channel_on_a_saturated_transport_does_not_idle_it(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    fast = TransportState(transport=_FakeTransport())
    slow = TransportState(transport=_FakeTransport())
    ledger.states[id(fast.transport)] = fast
    ledger.states[id(slow.transport)] = slow
    for _ in range(TransportState._MIN_SAMPLES):  # pyright: ignore[reportPrivateUsage]
      fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
      slow.update_throughput(nbytes=100, elapsed=1.0)
    slow.channel_count = 1
    handle = _FakeChannel()
    ledger.handle_states[id(handle)] = slow
    ledger.in_flight = 1

    pool.release(handle, is_fatal=False)

    assert handle.closed is True
    assert slow.channel_count == 0
    assert ledger.handle_states.get(id(handle)) is None


class TestFatalRelease:
  def test_fatal_release_on_a_still_active_transport_discards_only_the_channel(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()

    pool.release(handle, is_fatal=True)

    assert handle.closed is True  # pyright: ignore[reportAttributeAccessIssue]
    assert provider.dropped_count == 0
    assert ledger.handle_states.get(id(handle)) is None

  def test_fatal_release_on_a_dead_transport_drops_it_and_orphans_idle_siblings(self) -> None:
    pool, ledger, provider = _make_pool(channels_per_transport=4)
    first, _ = pool.acquire()
    state = ledger.handle_states.get(id(first))
    assert state is not None
    second_handle = _FakeChannel()
    ledger.handle_states[id(second_handle)] = state
    ledger.idle.append(Channel(handle=second_handle, state=state))
    state.transport.close()  # kill it out from under `first`

    pool.release(first, is_fatal=True)

    assert first.closed is True  # pyright: ignore[reportAttributeAccessIssue]
    assert second_handle.closed is True  # orphaned idle sibling closed too
    assert provider.dropped_count == 1
    assert ledger.states.get(id(state.transport)) is None


class TestTeardownAndKeepalive:
  def test_teardown_closes_every_transport_and_clears_tracking(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    state = ledger.handle_states.get(id(handle))

    pool.teardown()

    assert len(ledger.states) == 0
    assert len(ledger.idle) == 0
    assert state is None or not state.transport.is_active()

  def test_keepalive_check_one_with_nothing_idle_is_a_no_op(self) -> None:
    pool, _ledger, _provider = _make_pool(channels_per_transport=4)

    pool.keepalive_check_one()  # must not raise

  def test_keepalive_check_one_revalidates_and_reidles_a_healthy_channel(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    handle, _ = pool.acquire()
    pool.release(handle, is_fatal=False)
    assert len(ledger.idle) == 1

    pool.keepalive_check_one()

    assert handle.closed is False  # pyright: ignore[reportAttributeAccessIssue]
    assert len(ledger.idle) == 1


class TestCrossWaveMemory:
  def test_no_memory_before_any_wave_completes(self) -> None:
    _pool, ledger, _provider = _make_pool(channels_per_transport=4)

    assert ledger.last_wave_best_throughput is None

  def test_wave_boundary_persists_the_running_max_on_zero_crossing(self) -> None:
    pool, ledger, _provider = _make_pool(channels_per_transport=4)
    h1, callbacks1 = pool.acquire()
    h2, callbacks2 = pool.acquire()
    slower_nbytes = 500_000
    faster_nbytes = 1_000_000
    callbacks1[0](b"x" * slower_nbytes)
    pool.release(h1, is_fatal=False)
    assert ledger.last_wave_best_throughput is None  # still in-flight (h2)

    callbacks2[0](b"x" * faster_nbytes)
    pool.release(h2, is_fatal=False)  # in-flight count hits zero here

    assert ledger.last_wave_best_throughput is not None
    assert ledger.last_wave_best_throughput > 0
```

Delete `tests/ftp/test_sftp_pool.py` (fully superseded by the file above).

- [ ] **Step 8: Verify**

Run: `uv run ruff check --fix tests/ftp/ && uv run ruff check tests/ftp/`
Expected: no issues.

Run: `uv run pyright tests/ftp/`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run pytest tests/ftp/ -v`
Expected: **every** test passes (this is the full pre-existing FTP/SFTP suite -- 100+ tests across all
the files touched in this task, plus `test_credentials.py`/`test_types_and_errors.py` which were
untouched but must still collect and pass cleanly now that the package around them has changed).

---

## Task 11: Update `README.md`, `tests/test_public_api_exports.py`, and `.claude/CLAUDE.md`

**Files:**
- Modify: `README.md`
- Modify: `tests/test_public_api_exports.py`
- Modify: `.claude/CLAUDE.md`

- [ ] **Step 1: `README.md` -- one import line**

Change (around line 315, in the `ftp` section's first code example):
```python
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP
```
to:
```python
from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP
```

Run: `grep -n "aeth_ext\.ftp\.adapter\|aeth_ext\.ftp\.sftp_pool" README.md`
Expected: no matches remain.

- [ ] **Step 2: `.claude/CLAUDE.md` -- one path reference in the annotation-conventions example**

Change (in the "Annotation Conventions" section's `TYPE_CHECKING` exception bullet):
```
fields that nothing ever introspects (e.g. `aeth_ext.ftp.sftp_pool.TransportState`/`Channel`,
`aeth_ext.types.EmailMessageParts`).
```
to:
```
fields that nothing ever introspects (e.g. `aeth_ext.ftp.pool.sftp_channel_pool.TransportState`/`Channel`,
`aeth_ext.types.EmailMessageParts`).
```

- [ ] **Step 3: `tests/test_public_api_exports.py` -- replace the one `ftp.adapter` import/test with six
      entries covering every new module that defines `__all__`**

The old `sftp_pool.py` was never covered by this file even though it defined `__all__` -- this step
picks that gap up too, for the renamed `pool/sftp_channel_pool.py`.

Change the import block (currently, among the alphabetically-sorted `aeth_ext.*` imports):
```python
import aeth_ext.ftp.adapter as ftp_adapter_module
import aeth_ext.ftp.errors as ftp_errors_module
import aeth_ext.ftp.types as ftp_types_module
```
to:
```python
import aeth_ext.ftp as ftp_init_module
import aeth_ext.ftp.errors as ftp_errors_module
import aeth_ext.ftp.factory as ftp_factory_module
import aeth_ext.ftp.pool.ftp_adapter as ftp_pool_ftp_adapter_module
import aeth_ext.ftp.pool.sftp_adapter as ftp_pool_sftp_adapter_module
import aeth_ext.ftp.pool.sftp_channel_pool as ftp_pool_sftp_channel_pool_module
import aeth_ext.ftp.session as ftp_session_module
import aeth_ext.ftp.types as ftp_types_module
```

Change the test method (currently):
```python
  def test_ftp_adapter(self) -> None:
    _assert_all_exports_exist(ftp_adapter_module)

  def test_ftp_errors(self) -> None:
    _assert_all_exports_exist(ftp_errors_module)
```
to:
```python
  def test_ftp_init(self) -> None:
    _assert_all_exports_exist(ftp_init_module)

  def test_ftp_errors(self) -> None:
    _assert_all_exports_exist(ftp_errors_module)

  def test_ftp_factory(self) -> None:
    _assert_all_exports_exist(ftp_factory_module)

  def test_ftp_pool_ftp_adapter(self) -> None:
    _assert_all_exports_exist(ftp_pool_ftp_adapter_module)

  def test_ftp_pool_sftp_adapter(self) -> None:
    _assert_all_exports_exist(ftp_pool_sftp_adapter_module)

  def test_ftp_pool_sftp_channel_pool(self) -> None:
    _assert_all_exports_exist(ftp_pool_sftp_channel_pool_module)

  def test_ftp_session(self) -> None:
    _assert_all_exports_exist(ftp_session_module)
```

(leave the existing `test_ftp_types` method exactly where it is, right after -- it's unchanged, just
now sits after the new methods instead of right after the old `test_ftp_adapter`.)

- [ ] **Step 4: Verify**

Run: `uv run ruff check --fix tests/test_public_api_exports.py && uv run ruff check tests/test_public_api_exports.py`
Expected: no issues (ruff will re-sort the import block if this plan's ordering doesn't exactly match
isort's rules -- that's fine, let it).

Run: `uv run pyright tests/test_public_api_exports.py`
Expected: `0 errors, 0 warnings, 0 informations`.

Run: `uv run pytest tests/test_public_api_exports.py -v`
Expected: every test passes, including the seven new/renamed `test_ftp_*` methods.

---

## Task 12: Full-project verification

**Files:** none (verification only).

- [ ] **Step 1: Confirm no stray references remain anywhere in the repo**

Run: `grep -rn "aeth_ext\.ftp\.adapter\|aeth_ext\.ftp\.sftp_pool\b" --include="*.py" --include="*.md" .`

Expected: matches only inside historical, deliberately-untouched docs -- `docs/superpowers/plans/2026-08-1{4,5}-*.md`,
`docs/superpowers/specs/2026-08-1{3,4}-*.md`, and `.claude/plans/2026-08-13-ftp-connection-pooling-plan.md`
(an older plan-doc location predating the `docs/superpowers/plans/` convention, same "leave historical
record alone" reasoning) -- plus this plan file and `docs/superpowers/specs/2026-08-17-ftp-package-reorg-design.md`
themselves (both necessarily quote the old paths to describe what changed). If anything else shows up,
fix it before continuing.

- [ ] **Step 2: Full lint**

Run: `uv run ruff check .`
Expected: no issues.

- [ ] **Step 3: Full type check**

Run: `uv run pyright`
Expected: `0 errors, 0 warnings, 0 informations` project-wide. Pay particular attention to the absence of
any `reportImportCycles`/`reportPrivateUsage` diagnostics anywhere under `src/aeth_ext/ftp/` -- those
two rules were the entire motivation for this reorg.

- [ ] **Step 4: Full test suite**

Run: `uv run pytest`
Expected: every test in the project passes (not just `tests/ftp/` -- confirm nothing outside the `ftp`
package broke, even though this plan's own research found no other consumer of
`aeth_ext.ftp.adapter`/`aeth_ext.ftp.sftp_pool` in `src/`).

- [ ] **Step 5: Final structural sanity check**

Run (PowerShell) or equivalent: list `src/aeth_ext/ftp/` recursively and confirm it matches exactly:
```
src/aeth_ext/ftp/
  __init__.py
  connectors.py
  credentials.py
  errors.py
  factory.py
  pool/
    __init__.py
    base.py
    ftp_adapter.py
    sftp_adapter.py
    sftp_channel_pool.py
  session.py
  types.py
```
No `adapter.py`, no top-level `sftp_pool.py`.

**Do not commit.** Leave the full working tree (all Task 1-11 changes) unstaged/uncommitted for the
user to review. Report completion back to the user with a summary of what changed, but take no git
action of any kind.

---

## Self-Review Notes

- Spec coverage: target layout (Tasks 1-9) -- every file in the spec's tree exists after Task 9;
  dependency graph (Tasks 2-9, each task's "Interfaces" section states its real vs. `TYPE_CHECKING`
  edges) -- matches the spec's graph exactly, including the corrected zero-suppression
  `_TransportDialer` design (Task 4); Protocol audit table (`_ChannelConnector` -> Task 5,
  `TransportProvider` -> Tasks 1+4+5, `AdapterProtocol` -> Task 3, `HandleProvider` kept -> untouched in
  Task 1) -- all four rows covered; `LockedDict`/`LockedList` privatization -> Task 5 (module) + Task 10
  Step 7 (tests); public API surface -> Task 9; migration notes (tests, `test_public_api_exports.py`,
  README, historical docs left alone) -> Tasks 10-12.
- Placeholder scan: no TBD/TODO, no "add appropriate handling," no "similar to Task N" -- every task's
  code is either a full verbatim file or an exact search-and-replace with both old and new strings shown.
- Type consistency: `_TransportDialer` (Task 4) is consumed identically in Task 5
  (`ChannelLedger.__init__`) and Task 7 (`SFTPAdapter.__init__` via `self._make_transport_dialer(...)`).
  `_SFTPConnector`/`_FTPConnector` (Task 2) are consumed identically in Tasks 5, 6, 7. `AdaptedFTP`/
  `AdaptedSFTP`/`_AdapterBase` (Task 3) are consumed identically in Tasks 4, 6, 7, 9. `_PooledAdapterBase`
  (Task 4) is subclassed identically in Tasks 6 and 7. Checked every cross-task name against its
  producing task's "Produces" line -- no drift found.
