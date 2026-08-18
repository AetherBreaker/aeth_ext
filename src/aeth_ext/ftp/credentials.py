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
  protect_data_channel: bool = True
  """Whether to call `prot_p()` after login when `use_tls` is set, encrypting the data channel
  (file contents) as well as the control channel. `FTP_TLS.login()` only secures the control
  channel on its own; set this to `False` only if the server can't negotiate `PROT P` or a
  control-only handshake is genuinely intended -- otherwise file contents are sent in the clear
  despite `use_tls=True`. Ignored when `use_tls` is `False`."""
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
    """Validates that at least one authentication method is configured.

    Raises:
      ValueError: Both `password` and `private_key_path` are unset.
    """
    if self.password is None and self.private_key_path is None:
      raise ValueError("SFTPCredentials requires either password or private_key_path")
    return self
