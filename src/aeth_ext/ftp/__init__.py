"""Public entry points for FTP/SFTP connection pooling and one-shot transfer sessions.

`create_ftp_adapter` is the primary entry point for most consumers (or `FTPAdapter`/`SFTPAdapter`
directly, if you want the concrete type without the `isinstance` dispatch). Everything else in
`aeth_ext.ftp`'s submodules is an internal implementation detail, not a supported import path.
"""

# Local folder imports
from .credentials import FTPCredentials, SFTPCredentials
from .factory import create_ftp_adapter

__all__ = [
  "FTPCredentials",
  "SFTPCredentials",
  "create_ftp_adapter",
]
