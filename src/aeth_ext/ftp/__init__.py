"""FTP/SFTP connection pooling and one-shot transfer sessions.

`create_ftp_adapter` is the one re-export here -- the single entry point most consumers need, and
safe to import eagerly since `aeth_ext.ftp.factory` only imports `FTPAdapter`/`SFTPAdapter` lazily,
inside the function body. Everything else must be imported directly from its owning submodule
(`aeth_ext.ftp.credentials` for `FTPCredentials`/`SFTPCredentials`, `aeth_ext.ftp.session` for
`AdaptedFTP`/`AdaptedSFTP`, etc.) -- a package `__init__.py` always runs before any of its
submodules, so re-exporting more here would force every one of those submodules to be importable
(and everything *they* import) just to reach any single one of them, including the SFTP stack's
`paramiko` dependency, which is only guaranteed present when the optional `sftp` extra is installed.
"""

# First party imports
from aeth_ext.ftp.factory import create_ftp_adapter

__all__ = ["create_ftp_adapter"]
