"""Tests for `AdaptedFTP.transfer_file`/`AdaptedSFTP.transfer_file` (server-to-server transfers).

Covers all four protocol combinations (`_ftp_to_ftp`, `_ftp_to_sftp`,
`_sftp_to_ftp`, `_sftp_to_sftp`), the file-size-mismatch detection path, the
"size lookup fails mid-transfer" fallback, and progress-bar invocation.
"""

# Standard library imports
from ftplib import all_errors
from typing import TYPE_CHECKING, Never

# Third party imports
from paramiko import SFTPError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Third party imports
  import pytest

  # First party imports
  from aeth_ext.ftp import AdaptedFTP, AdaptedSFTP

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
  def test_ftp_to_ftp(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    data = b"ftp to ftp payload"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_ftp_to_sftp(self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_sftp_to_ftp(self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    data = b"sftp to ftp payload"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    with source as src:
      _upload(src, "source.bin", data)
      with dest as dst:
        result = src.transfer_file("source.bin", "dest.bin", dst)
        assert result is True
        assert _download(dst, "dest.bin") == data

  def test_sftp_to_sftp(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
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
  ) -> None:
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
  ) -> None:
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
  ) -> None:
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


class TestDestinationCallbacksAreTapped:
  """`transfer_file`'s SFTP-destination paths (`_ftp_to_sftp`, `_sftp_to_sftp`) must invoke the
  destination adapter's own constructor-injected `_callbacks` -- not just the source's -- since that's
  how `FTPAdapter` instruments a pooled SFTP channel's throughput regardless of which side of the
  transfer it's on."""

  def test_ftp_to_sftp_taps_the_destinations_callbacks(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP]
  ) -> None:
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    seen_by_source: list[bytes] = []
    seen_by_dest: list[bytes] = []
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before _callbacks is set: transfer_file's tap is what's under test
      src._callbacks = (seen_by_source.append,)  # pyright: ignore[reportPrivateUsage]
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert b"".join(seen_by_source) == data
    assert b"".join(seen_by_dest) == data

  def test_sftp_to_sftp_taps_the_destinations_callbacks(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    data = b"sftp to sftp payload"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    seen_by_source: list[bytes] = []
    seen_by_dest: list[bytes] = []
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before _callbacks is set: transfer_file's tap is what's under test
      src._callbacks = (seen_by_source.append,)  # pyright: ignore[reportPrivateUsage]
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert b"".join(seen_by_source) == data
    assert b"".join(seen_by_dest) == data

  def test_sftp_to_ftp_does_not_tap_the_ftp_destination_as_an_sftp_callback(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP]
  ) -> None:
    """`AdaptedFTP` has its own `_callbacks`, but `_sftp_to_ftp` streams over a raw FTP data socket,
    not through `AdaptedFTP.upload_file`/`download_file` -- so the destination's callbacks are never
    invoked here. Documents the boundary rather than asserting a design goal."""
    data = b"sftp to ftp payload"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    seen_by_dest: list[bytes] = []
    with source as src, dest as dst:
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]
      _upload(src, "source.bin", data)

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert seen_by_dest == []


class TestSourceSizeLookupFailureMidTransfer:
  def test_ftp_to_ftp_continues_with_unknown_size_when_size_lookup_fails(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"twelve bytes"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)

      def _raise_size(_path: str) -> Never:
        raise all_errors[0]("size unavailable")

      monkeypatch.setattr(src.handler, "size", _raise_size)

      result = src.transfer_file("source.bin", "dest.bin", dst)

      assert result is True  # streamed/dest sizes still agree with each other
      assert _download(dst, "dest.bin") == data

  def test_sftp_to_sftp_continues_with_unknown_size_when_size_lookup_fails(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"twelve bytes"
    source, dest = make_sftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)

      def _raise_stat(_path: str) -> Never:
        raise SFTPError("stat unavailable")

      monkeypatch.setattr(src.handler, "stat", _raise_stat)

      result = src.transfer_file("source.bin", "dest.bin", dst)

      assert result is True
      assert _download(dst, "dest.bin") == data
