# Standard library imports
from dataclasses import FrozenInstanceError
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
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")  # pyright: ignore[reportArgumentType] -- pydantic coerces str -> SecretStr at runtime

    assert creds.host == "ftp.example.com"
    assert creds.port == 21  # noqa: PLR2004
    assert creds.use_tls is False
    assert creds.protect_data_channel is None
    assert creds.passive_mode is True
    assert creds.connect_timeout is None

  def test_password_is_not_exposed_in_repr(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")  # pyright: ignore[reportArgumentType]

    assert "hunter2" not in repr(creds)

  def test_is_frozen(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2")  # pyright: ignore[reportArgumentType]

    with pytest.raises(FrozenInstanceError):
      creds.host = "other.example.com"  # pyright: ignore[reportAttributeAccessIssue]

  def test_port_out_of_range_is_rejected(self) -> None:
    with pytest.raises(ValidationError):
      FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", port=0)  # pyright: ignore[reportArgumentType]

  def test_protect_data_channel_true_without_tls_is_rejected(self) -> None:
    with pytest.raises(ValidationError, match="protect_data_channel=True requires use_tls=True"):
      FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", protect_data_channel=True)  # pyright: ignore[reportArgumentType]

  def test_protect_data_channel_false_without_tls_is_accepted(self) -> None:
    creds = FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", protect_data_channel=False)  # pyright: ignore[reportArgumentType]

    assert creds.protect_data_channel is False


class TestSFTPCredentials:
  def test_password_only_is_valid(self) -> None:
    creds = SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2")  # pyright: ignore[reportArgumentType]

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
    creds = SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2")  # pyright: ignore[reportArgumentType]

    assert creds.host_key_policy == "reject"

  def test_passphrase_is_not_exposed_in_repr(self) -> None:
    creds = SFTPCredentials(
      host="sftp.example.com",
      username="svc",
      private_key_path=None,
      password="hunter2",  # pyright: ignore[reportArgumentType]
      private_key_passphrase="s3cr3t",  # pyright: ignore[reportArgumentType]
    )

    assert "s3cr3t" not in repr(creds)
