"""Tests for `AdaptedFTP.transfer_file`/`AdaptedSFTP.transfer_file` (server-to-server transfers).

Covers all four protocol combinations (`_ftp_to_ftp`, `_ftp_to_sftp`,
`_sftp_to_ftp`, `_sftp_to_sftp`), the file-size-mismatch detection path, the
"size lookup fails mid-transfer" fallback, and progress-bar invocation.
"""

# Standard library imports
from ftplib import all_errors
from typing import TYPE_CHECKING

# Third party imports
from paramiko import SFTPError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Third party imports
  import pytest

  # First party imports
  from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP

  # Local folder imports
  from .conftest import FakeProgress

  type _AnyAdapter = AdaptedFTP | AdaptedSFTP


def _upload(adapter: _AnyAdapter, remote_path: str, data: bytes) -> None:
  chunks = iter([data, b""])
  adapter.upload_file(remote_path, lambda _size: next(chunks), file_size=len(data))


def _download(adapter: _AnyAdapter, remote_path: str) -> bytes:
  received = bytearray()
  adapter.download_file(remote_path, lambda chunk: received.extend(bytes(chunk)))
  return bytes(received)


class TestTransferAllCombinations:
  def test_ftp_to_ftp(self, make_ftp_adapter: Callable[[], AdaptedFTP]):
    data = b"ftp to ftp payload"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_ftp_to_sftp(self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP]):
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_sftp_to_ftp(self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP]):
    data = b"sftp to ftp payload"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_sftp_to_sftp(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    data = b"sftp to sftp payload"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data


class TestTransferReportsProgress:
  def test_progress_bar_receives_task_and_advances(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], fake_progress: FakeProgress
  ):
    data = b"twelve bytes"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before pbar is attached: only the transfer itself should report progress
      src.pbar = fake_progress  # pyright: ignore[reportAttributeAccessIssue]
      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert len(fake_progress.tasks) == 1
    _description, total = fake_progress.tasks[0]
    assert total == len(data)
    assert sum(advance for _task_id, advance in fake_progress.updates) == len(data)


class TestFileSizeMismatchDetection:
  def test_ftp_to_ftp_mismatch_is_detected_and_logged(
    self,
    make_ftp_adapter: Callable[[], AdaptedFTP],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
  ):
    data = b"twelve bytes"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      # Force the reported source size to disagree with what actually transfers.
      monkeypatch.setattr(src.handler, "size", lambda _path: len(data) + 1)

      with caplog.at_level("ERROR"):
        result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is False
    assert any("File size mismatch" in record.message for record in caplog.records)

  def test_sftp_to_sftp_mismatch_is_detected_and_logged(
    self,
    make_sftp_adapter: Callable[[], AdaptedSFTP],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
  ):
    data = b"twelve bytes"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)

      class _WrongSizeStat:
        st_size = len(data) + 1

      monkeypatch.setattr(src.handler, "stat", lambda _path: _WrongSizeStat())

      with caplog.at_level("ERROR"):
        result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is False
    assert any("File size mismatch" in record.message for record in caplog.records)


class TestSourceSizeLookupFailureMidTransfer:
  def test_ftp_to_ftp_continues_with_unknown_size_when_size_lookup_fails(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ):
    data = b"twelve bytes"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)

      def _raise_size(_path: str):
        raise all_errors[0]("size unavailable")

      monkeypatch.setattr(src.handler, "size", _raise_size)

      result = src.transfer_file("source.bin", "dest.bin", dst)

      assert result is True  # streamed/dest sizes still agree with each other
      assert _download(dst, "dest.bin") == data

  def test_sftp_to_sftp_continues_with_unknown_size_when_size_lookup_fails(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], monkeypatch: pytest.MonkeyPatch
  ):
    data = b"twelve bytes"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)

      def _raise_stat(_path: str):
        raise SFTPError("stat unavailable")

      monkeypatch.setattr(src.handler, "stat", _raise_stat)

      result = src.transfer_file("source.bin", "dest.bin", dst)

      assert result is True
      assert _download(dst, "dest.bin") == data
