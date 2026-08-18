"""Public entry points for FTP/SFTP connection pooling and one-shot transfer sessions.

`create_ftp_adapter` is the primary entry point for most consumers. If you want the concrete type
without the `isinstance` dispatch, import `FTPAdapter`/`SFTPAdapter` explicitly from
`aeth_ext.ftp.pool.ftp_adapter`/`aeth_ext.ftp.pool.sftp_adapter` -- everything in `aeth_ext.ftp`'s
submodules is an internal implementation detail, not re-exported here.
"""

# Local folder imports
from .credentials import FTPCredentials, SFTPCredentials
from .factory import create_ftp_adapter

__all__ = [
  "FTPCredentials",
  "SFTPCredentials",
  "create_ftp_adapter",
]
