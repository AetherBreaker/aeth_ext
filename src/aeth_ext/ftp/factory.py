"""`create_ftp_adapter`: the entry point most consumers should use -- dispatches to `FTPAdapter` or
`SFTPAdapter` based on which credentials type it's given, so callers don't need an `isinstance` check
of their own.

`FTPAdapter`/`SFTPAdapter` are imported lazily, inside the function body, one per branch -- not at
module level. `SFTPAdapter` pulls in `paramiko`, which is only guaranteed present when the optional
`sftp` extra is installed; importing this module (or calling it with `FTPCredentials`) must not
require it. The two are still imported under `TYPE_CHECKING` for the `@overload` signatures below --
safe, since nothing calls `get_type_hints`/`inspect.signature(eval_str=True)` on a plain function
like this one, so those annotations never force runtime resolution (see `.claude/CLAUDE.md`'s
annotation-conventions section).
"""

# Standard library imports
from typing import TYPE_CHECKING, overload

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from contextvars import ContextVar
  from typing import Any
  from zoneinfo import ZoneInfo

  # First party imports
  from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter
  from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter
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
  acquire_timeout: float | None = 300.0,
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
  acquire_timeout: float | None = 300.0,
  channels_per_transport: int = 4,
) -> SFTPAdapter: ...
def create_ftp_adapter(credentials: object, **kwargs: Any) -> FTPAdapter | SFTPAdapter:
  """Builds an `FTPAdapter` or `SFTPAdapter`, chosen by `credentials`'s type.

  Args:
    credentials: FTP or SFTP server credentials; determines which adapter type is built.
    **kwargs: Forwarded to `FTPAdapter`/`SFTPAdapter`'s constructor.

  Returns:
    An `FTPAdapter` for `FTPCredentials`, or an `SFTPAdapter` for `SFTPCredentials`.

  Raises:
    TypeError: `credentials` is neither `FTPCredentials` nor `SFTPCredentials`.
  """
  if isinstance(credentials, FTPCredentials):
    # First party imports
    from aeth_ext.ftp.pool.ftp_adapter import FTPAdapter

    return FTPAdapter(credentials, **kwargs)
  if isinstance(credentials, SFTPCredentials):
    # First party imports
    from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter

    return SFTPAdapter(credentials, **kwargs)
  # An unconditional else would hand back an SFTPAdapter for any other object, which then fails
  # far from here with an unrelated missing-attribute error the first time it tries to connect.
  raise TypeError(f"credentials must be FTPCredentials or SFTPCredentials, got {type(credentials).__name__}")
