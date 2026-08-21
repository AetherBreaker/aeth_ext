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
