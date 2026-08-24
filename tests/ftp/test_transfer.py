"""Tests for `AdaptedFTP.transfer_file`/`AdaptedSFTP.transfer_file` (server-to-server transfers).

Covers all four protocol combinations (`_ftp_to_ftp`, `_ftp_to_sftp`,
`_sftp_to_ftp`, `_sftp_to_sftp`), the file-size-mismatch detection path, the
"size lookup fails mid-transfer" fallback, and progress-bar invocation.
"""

# Standard library imports
from ftplib import all_errors
from typing import TYPE_CHECKING, Never

# Third party imports
import pytest
from paramiko import SFTPError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp.session import AdaptedFTP, AdaptedSFTP
  from aeth_ext.types import SizedBuffer

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


class TestVoidrespRunsDespiteMidTransferException:
  """A chunk callback raising mid-transfer must not skip `voidresp()` on either FTP handler
  involved -- doing so would leave that handler's control connection desynced (an unread
  transfer-completion reply sitting in the pipe) even though the exception itself (e.g. a plain
  `RuntimeError`) isn't one `__exit__` classifies as connection-fatal, so the handler would
  otherwise be pooled back as if nothing were wrong.

  `voidcmd` (e.g. the "TYPE I" calls every transfer path issues) invokes `voidresp` internally, so
  a raw call count isn't a clean signal on its own -- these track only whether `voidresp` runs
  again *after* the callback has already raised, which isolates the specific finally-guarded call
  the fix added from that ordinary pre-transfer traffic.
  """

  def test_ftp_to_ftp_callback_exception_still_drains_both_replies(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"ftp to ftp payload"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      raised, src_ran_after, dst_ran_after = _track_voidresp_after_raise(src, dst, monkeypatch)

      def _raising_callback(_chunk: bytes) -> None:
        raised[0] = True
        raise RuntimeError("boom")

      with pytest.raises(RuntimeError, match="boom"):
        src.transfer_file("source.bin", "dest.bin", dst, callback=_raising_callback)

    assert src_ran_after()
    assert dst_ran_after()

  def test_ftp_to_sftp_callback_exception_still_drains_the_source_reply(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      raised, src_ran_after, _dst_ran_after = _track_voidresp_after_raise(src, None, monkeypatch)

      def _raising_callback(_chunk: bytes) -> None:
        raised[0] = True
        raise RuntimeError("boom")

      with pytest.raises(RuntimeError, match="boom"):
        src.transfer_file("source.bin", "dest.bin", dst, callback=_raising_callback)

    assert src_ran_after()

  def test_sftp_to_ftp_callback_exception_still_drains_the_destination_reply(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"sftp to ftp payload"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      raised, _src_ran_after, dst_ran_after = _track_voidresp_after_raise(None, dst, monkeypatch)

      def _raising_callback(_chunk: bytes) -> None:
        raised[0] = True
        raise RuntimeError("boom")

      with pytest.raises(RuntimeError, match="boom"):
        src.transfer_file("source.bin", "dest.bin", dst, callback=_raising_callback)

    assert dst_ran_after()


def _track_voidresp_after_raise(
  src: AdaptedFTP | None, dst: AdaptedFTP | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[bool], Callable[[], bool], Callable[[], bool]]:
  """Wraps `voidresp` on whichever of `src`/`dst`'s (FTP-only) handlers is given, returning a
  shared `raised` flag (set by the test's callback) plus a zero-arg getter per side reporting
  whether `voidresp` ran again after that flag was set.

  Args:
    src: The source `AdaptedFTP` to instrument, or `None` to skip it.
    dst: The destination `AdaptedFTP` to instrument, or `None` to skip it.
    monkeypatch: Standard pytest fixture, used to patch each handler's bound `voidresp` in place.

  Returns:
    `(raised, get_src_ran_after, get_dst_ran_after)` -- whichever side was `None` always reports `False`.
  """
  raised = [False]

  def _make_tracker(adapter: AdaptedFTP | None) -> Callable[[], bool]:
    if adapter is None:
      return lambda: False
    assert adapter.handler is not None
    original = adapter.handler.voidresp
    ran_after = False

    def _tracking_voidresp() -> object:
      nonlocal ran_after
      if raised[0]:
        ran_after = True
      return original()

    monkeypatch.setattr(adapter.handler, "voidresp", _tracking_voidresp)
    return lambda: ran_after

  return raised, _make_tracker(src), _make_tracker(dst)


class TestAbortedTransferMarksSessionFatal:
  """A completion-reply failure (e.g. `ftplib.error_temp` from a `426` on an aborted transfer)
  isn't one of `_CONNECTION_FATAL_TYPES`, so `__exit__` wouldn't otherwise mark the session fatal
  on its own -- `drain_completion_reply` must do so itself, or a desynchronized control connection
  gets released back into the pool as if nothing were wrong (D-copilot regression)."""

  def test_drain_completion_reply_failure_marks_the_session_fatal(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    adapter = make_ftp_adapter()
    with adapter as session:
      assert session.handler is not None

      def _raise_error_temp() -> object:
        raise all_errors[0]("426 Connection closed; transfer aborted")

      monkeypatch.setattr(session.handler, "voidresp", _raise_error_temp)

      with pytest.raises(all_errors):
        session.drain_completion_reply()

      assert session._fatal is True  # pyright: ignore[reportPrivateUsage]

      # Restore a working voidresp before the session's own cleanup QUIT runs at __exit__.
      monkeypatch.undo()


class TestFtpToSftpSetsBinaryMode:
  def test_type_i_is_sent_before_retr(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    # _ftp_to_sftp previously never set binary mode on the source FTP connection, unlike every
    # other transfer path in this file -- a first-use FTP-to-SFTP transfer could apply the
    # protocol's default ASCII newline conversion and corrupt binary payloads.
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      assert src.handler is not None
      type_i_calls: list[str] = []
      original_voidcmd = src.handler.voidcmd

      def _tracking_voidcmd(cmd: str) -> object:
        type_i_calls.append(cmd)
        return original_voidcmd(cmd)

      monkeypatch.setattr(src.handler, "voidcmd", _tracking_voidcmd)

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert "TYPE I" in type_i_calls


class TestTransferCommandRejectionDoesNotCallVoidresp:
  """If the transfer command itself (`transfercmd`/`ntransfercmd`) is rejected before any data
  connection opens, there is no transfer-completion reply pending on that side -- calling
  `voidresp()` in that case would block waiting for a reply that will never arrive. Each of these
  forces that rejection and asserts `voidresp()` is never called as a result.

  Legitimate `voidresp()` traffic happens on either side of the call under test -- `voidcmd("TYPE
  I")` before it, and the test fixtures' own `QUIT` once the enclosing `with` block releases the
  session -- so a raw call count can't distinguish those from an erroneous drain. `_rejected_at`
  marks the instant the rejection fires and checks (inside the `with` block, before that release
  ever runs) that `voidresp()` never ran again afterward.
  """

  def test_upload_file_rejection_does_not_call_voidresp(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    adapter = make_ftp_adapter()
    with adapter as session:
      assert session.handler is not None
      rejected_at, ran_after = _rejected_at(session.handler, "voidresp", monkeypatch)

      def _raise_transfercmd(_cmd: str, _rest: object = None) -> Never:
        rejected_at[0] = True
        raise all_errors[0]("550 rejected")

      monkeypatch.setattr(session.handler, "transfercmd", _raise_transfercmd)

      with pytest.raises(all_errors):
        session.upload_file("no/such/dir/file.bin", lambda _size: b"", file_size=0)

      assert not ran_after()

  def test_download_file_rejection_does_not_call_voidresp(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    adapter = make_ftp_adapter()
    with adapter as session:
      assert session.handler is not None
      rejected_at, ran_after = _rejected_at(session.handler, "voidresp", monkeypatch)

      def _raise_ntransfercmd(_cmd: str, _rest: object = None) -> Never:
        rejected_at[0] = True
        raise all_errors[0]("550 rejected")

      monkeypatch.setattr(session.handler, "ntransfercmd", _raise_ntransfercmd)

      with pytest.raises(all_errors):
        session.download_file("no/such/file.bin", lambda _chunk: None)

      assert not ran_after()

  def test_ftp_to_ftp_source_rejection_does_not_call_either_voidresp(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"twelve bytes"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      assert src.handler is not None
      assert dst.handler is not None
      rejected_at, src_ran_after = _rejected_at(src.handler, "voidresp", monkeypatch)
      _dst_rejected_at, dst_ran_after = _rejected_at(dst.handler, "voidresp", monkeypatch, shared_flag=rejected_at)

      def _raise_ntransfercmd(_cmd: str, _rest: object = None) -> Never:
        rejected_at[0] = True
        raise all_errors[0]("550 rejected")

      monkeypatch.setattr(src.handler, "ntransfercmd", _raise_ntransfercmd)

      with pytest.raises(all_errors):
        src.transfer_file("source.bin", "dest.bin", dst)

      # Source's ntransfercmd() never succeeded, so neither side's voidresp() should run at all --
      # destination setup is never even reached once the source dial itself has already failed.
      assert not src_ran_after()
      assert not dst_ran_after()

  def test_ftp_to_ftp_destination_rejection_does_not_call_destinations_voidresp(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"twelve bytes"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      assert dst.handler is not None
      rejected_at, dst_ran_after = _rejected_at(dst.handler, "voidresp", monkeypatch)

      def _raise_transfercmd(_cmd: str, _rest: object = None) -> Never:
        rejected_at[0] = True
        raise all_errors[0]("550 rejected")

      monkeypatch.setattr(dst.handler, "transfercmd", _raise_transfercmd)

      with pytest.raises(all_errors):
        src.transfer_file("source.bin", "dest.bin", dst)

      # The source's own data connection did open, so its voidresp() legitimately runs once the
      # exception unwinds past it -- only the destination's is asserted here, since its
      # transfercmd() never actually opened a data connection.
      assert not dst_ran_after()

  def test_sftp_to_ftp_rejection_does_not_call_voidresp(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP], monkeypatch: pytest.MonkeyPatch
  ) -> None:
    data = b"twelve bytes"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    with source as src, dest as dst:
      _upload(src, "source.bin", data)
      assert dst.handler is not None
      rejected_at, ran_after = _rejected_at(dst.handler, "voidresp", monkeypatch)

      def _raise_transfercmd(_cmd: str, _rest: object = None) -> Never:
        rejected_at[0] = True
        raise all_errors[0]("550 rejected")

      monkeypatch.setattr(dst.handler, "transfercmd", _raise_transfercmd)

      with pytest.raises(all_errors):
        src.transfer_file("source.bin", "dest.bin", dst)

      assert not ran_after()


def _rejected_at(
  obj: object, attr: str, monkeypatch: pytest.MonkeyPatch, *, shared_flag: list[bool] | None = None
) -> tuple[list[bool], Callable[[], bool]]:
  """Wraps `obj.attr` (a bound method) to report whether it's called again after a `rejected_at`
  flag is set, patching the instance in place. Isolates the one drain call under test from
  incidental `voidresp()` traffic (`voidcmd("TYPE I")` before it, `QUIT` during teardown after).

  Args:
    obj: The object whose bound method to wrap.
    attr: The attribute name to wrap.
    monkeypatch: Standard pytest fixture, used to patch `attr` on `obj` in place.
    shared_flag: Reuse another call's flag instead of creating a new one, so one rejection can be
      observed from two different objects' wrapped methods.

  Returns:
    `(rejected_at, get_ran_after)` -- set `rejected_at[0] = True` at the point of rejection; the
    getter reports whether `attr` ran again on `obj` afterward.
  """
  rejected_at = shared_flag if shared_flag is not None else [False]
  original = getattr(obj, attr)
  ran_after = False

  def _tracking(*args: object, **kwargs: object) -> object:
    nonlocal ran_after
    if rejected_at[0]:
      ran_after = True
    return original(*args, **kwargs)

  monkeypatch.setattr(obj, attr, _tracking)
  return rejected_at, lambda: ran_after


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
  """`transfer_file` must invoke the destination adapter's own constructor-injected `_callbacks` --
  not just the source's -- on all four protocol combinations. Every `transfer_file` path writes to
  `other`'s connection directly (never through `other.upload_file`/`download_file`), so `other`'s own
  observers only ever fire if `transfer_file` explicitly taps them; `AdaptedFTP` and `AdaptedSFTP` are
  otherwise symmetric instrumentation-wise (both support constructor-injected callbacks), and
  `transfer_file` must not silently drop the destination's observers just because that destination
  happens to be FTP."""

  def test_ftp_to_ftp_taps_the_destinations_callbacks(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    data = b"ftp to ftp payload"
    source, dest = make_ftp_adapter(), make_ftp_adapter()
    seen_by_source: list[SizedBuffer] = []
    seen_by_dest: list[SizedBuffer] = []
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before _callbacks is set: transfer_file's tap is what's under test
      src._callbacks = (seen_by_source.append,)  # pyright: ignore[reportPrivateUsage]
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert b"".join(seen_by_source) == data
    assert b"".join(seen_by_dest) == data

  def test_ftp_to_sftp_taps_the_destinations_callbacks(
    self, make_ftp_adapter: Callable[[], AdaptedFTP], make_sftp_adapter: Callable[[], AdaptedSFTP]
  ) -> None:
    data = b"ftp to sftp payload"
    source, dest = make_ftp_adapter(), make_sftp_adapter()
    seen_by_source: list[SizedBuffer] = []
    seen_by_dest: list[SizedBuffer] = []
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before _callbacks is set: transfer_file's tap is what's under test
      src._callbacks = (seen_by_source.append,)  # pyright: ignore[reportPrivateUsage]
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert b"".join(seen_by_source) == data
    assert b"".join(seen_by_dest) == data

  def test_sftp_to_ftp_taps_the_destinations_callbacks(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP], make_ftp_adapter: Callable[[], AdaptedFTP]
  ) -> None:
    data = b"sftp to ftp payload"
    source, dest = make_sftp_adapter(), make_ftp_adapter()
    seen_by_source: list[SizedBuffer] = []
    seen_by_dest: list[SizedBuffer] = []
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
    seen_by_source: list[SizedBuffer] = []
    seen_by_dest: list[SizedBuffer] = []
    with source as src, dest as dst:
      _upload(src, "source.bin", data)  # before _callbacks is set: transfer_file's tap is what's under test
      src._callbacks = (seen_by_source.append,)  # pyright: ignore[reportPrivateUsage]
      dst._callbacks = (seen_by_dest.append,)  # pyright: ignore[reportPrivateUsage]

      result = src.transfer_file("source.bin", "dest.bin", dst)

    assert result is True
    assert b"".join(seen_by_source) == data
    assert b"".join(seen_by_dest) == data


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
