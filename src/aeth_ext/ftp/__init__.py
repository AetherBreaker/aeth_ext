"""Public entry points for FTP/SFTP connection pooling and one-shot transfer sessions.

`create_ftp_adapter` is the primary entry point for most consumers; `FTPAdapter`/`SFTPAdapter` are
re-exported for anyone who wants the concrete type without the `isinstance` dispatch. `AdaptedFTP`/
`AdaptedSFTP` are the session types those adapters hand out, and `HandleProvider` is the extension
point for driving a session from a hand-written provider instead of a pool. Everything else under
`aeth_ext.ftp` is an implementation detail and may move without notice.
"""

# Local folder imports
from .credentials import FTPCredentials, SFTPCredentials
from .errors import PoolClosedError, ServerNotAvailableError
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
  "PoolClosedError",
  "SFTPAdapter",
  "SFTPCredentials",
  "ServerNotAvailableError",
  "create_ftp_adapter",
]
