# SFTP Transport Multiplexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `FTPAdapter`'s SFTP pooling default to multiplexed channels on shared `paramiko.Transport`s
instead of one TCP connection per pooled handle, growing additional `Transport`s only when a
cross-sectional throughput comparison shows the existing ones are saturated.

**Architecture:** `SFTPProtocol` (and, for call-site parity, `FTPProtocol`) split `get_conn_handler()`
into `get_transport()` + `open_channel(transport)`. A new `src/aeth_ext/ftp/sftp_pool.py` module owns
*all* SFTP-specific two-tier bookkeeping (per-`Transport` channel counts, EWMA throughput, saturation,
cross-wave memory, and the handler→`TransportState` lookup used on release) as a `SFTPPool` object
composed into `FTPAdapter`. **`AdaptedFTP`/`AdaptedSFTP` stay exactly what they are today: transfer logic
that acquires a handler and releases it via a callback — they gain no transport/pool-awareness of any
kind.** The one deliberately-approved exception is a generalization of the callback mechanism they
already have: both classes gain a constructor-time `callbacks` sequence that's merged into their existing
per-chunk callback invocation in `upload_file`/`download_file`/`transfer_file`. `FTPAdapter` uses that
existing, generic mechanism to inject its own per-channel throughput observer — the Adapted classes never
know it's "for instrumentation," they just call every callback they were given, same as always.
`FTPAdapter`'s FTP-path pooling (`_idle`/`_current_size`/`_effective_ceiling`/`_discovered_max`) is reused
unchanged for the Transport tier and untouched for plain FTP.

**Tech Stack:** Python 3.14, `paramiko` (`Transport`, `SFTPClient`), `ftplib.FTP`, `pytest` with real
loopback `pyftpdlib`/`paramiko` servers (no socket mocking, per existing `tests/ftp/conftest.py`
conventions).

**Spec:** `docs/superpowers/specs/2026-08-14-sftp-transport-multiplexing-design.md`

## Global Constraints

- **`AdaptedFTP`/`AdaptedSFTP` hold transfer logic only: acquire a handler, run the transfer, release the
  handler via `return_to_pool_callback`.** No pool/transport bookkeeping (a `.transport` attribute,
  pool-awareness, saturation state, anything named "instrument") may be added to either class. The one
  approved exception is the generic `callbacks` sequence in Task 3 — a widening of the callback mechanism
  they already have, not a new pool-specific concept. Any change to this contract beyond what Task 3
  specifies needs to be pitched for approval before writing it, not decided unilaterally mid-implementation.
- No `from __future__ import annotations` anywhere (project-wide rule). Any type referenced in a real
  dataclass field annotation must be a top-level runtime import, not `TYPE_CHECKING`-guarded — `dataclass`
  forces `__annotations__` evaluation when building fields.
- All pydantic dataclasses inherit `aeth_ext.types.IsPydantic`. The new bookkeeping structs in this plan
  are plain `dataclasses.dataclass`, not pydantic — no `IsPydantic` needed for them.
- `except A, B, C:` (no `as e`) is valid PEP 758 syntax here, not a Python 2 relic.
- Google-style docstrings only (`Args:`/`Returns:`/`Raises:`, double-backtick names, no RST roles).
- Conventional Commits for every commit: `<type>(ftp): <summary>`; `fix` commits document bug/cause/fix.
- **Do not commit anything while executing this plan.** Every task ends with the working tree left dirty
  for local review — stage and leave staged/unstaged as appropriate, but do not run `git commit`. The
  human reviews and commits locally.
- API breaks are acceptable on `FTPAdapter`, `SFTPProtocol`, and `FTPProtocol` (per spec Scope).
- Run only targeted tests while iterating; run the full `uv run pytest tests/ftp/` sweep once, in the
  final task, immediately before stopping.

---

## File Structure

- **Modify** `src/aeth_ext/ftp/types.py` — `FTPProtocolBase`/`FTPProtocol`/`SFTPProtocol` reshaped to
  `get_transport()`/`open_channel()`.
- **Modify** `src/aeth_ext/ftp/adapter.py` — `chunk_size` param; generic `callbacks` sequence merged into
  `upload_file`/`download_file`/`transfer_file`'s existing per-chunk callback invocation; `FTPAdapter`
  composes an `SFTPPool` and splits `_grow`/`_checkout_idle`/`_release`/`_discard`/`_pool_return` into
  FTP-specific (unchanged behavior) and SFTP-specific (routes through `SFTPPool`) paths. `AdaptedFTP`/
  `AdaptedSFTP` themselves gain nothing pool-related — only the generic `callbacks` param from Task 3.
- **Create** `src/aeth_ext/ftp/sftp_pool.py` — `TransportState`, `Channel`, `SFTPPool`: *all* SFTP two-tier
  pool bookkeeping (channel caps, saturation, cross-wave memory, and the handler→`TransportState` lookup
  used when a session releases its handle), used only by the SFTP side of `FTPAdapter`. Kept out of
  `adapter.py` so that file's existing FTP-path pooling logic isn't diluted by SFTP-only concepts, and
  kept entirely out of `AdaptedSFTP` per the Global Constraints.
- **Modify** `tests/ftp/conftest.py` — `_TestFTPProtocol`/`_TestSFTPProtocol` updated to the new protocol
  shape.
- **Modify** `tests/ftp/test_ftp_adapter_factory.py` — every `get_conn_handler` reference (test doubles,
  monkeypatches) updated to `get_transport`/`open_channel`.
- **Create** `tests/ftp/test_sftp_pool.py` — unit tests for `SFTPPool`/`TransportState` in isolation (no
  real network), covering cap enforcement, saturation routing, and wave-boundary memory.

---

### Task 1: Protocol interface reshape (`get_conn_handler` → `get_transport`/`open_channel`)

**Files:**
- Modify: `src/aeth_ext/ftp/types.py:37-64`
- Modify: `src/aeth_ext/ftp/adapter.py:67-70` (`AdaptedFTP.__enter__`), `:336-339` (`AdaptedSFTP.__enter__`),
  `:656-673` (`FTPAdapter._grow`), `:690-696` (`FTPAdapter.start_session`, also fixes the pre-existing
  `pool_return=` kwarg name bug — the constructor parameter is `return_to_pool_callback`)
- Modify: `tests/ftp/conftest.py:40-67` (`_TestFTPProtocol`), `:197-215` (`_TestSFTPProtocol`)
- Modify: `tests/ftp/test_ftp_adapter_factory.py` (all `get_conn_handler` references: lines ~21-55,
  127-163, 240-266, 313-434)
- Test: `tests/ftp/test_ftp_adapter_factory.py` (existing tests, updated in place — this task is a pure
  rename, so no new test file)

**Interfaces:**
- Produces: `FTPProtocolBase.get_transport(self) -> Any`, `FTPProtocolBase.open_channel(self, transport:
  Any) -> Any`, `FTPProtocolBase.close_conn_handler(self) -> None` (unchanged). `FTPProtocol.get_transport(self)
  -> FTP`, `FTPProtocol.open_channel(self, transport: FTP) -> FTP` (identity passthrough).
  `SFTPProtocol.get_transport(self) -> Transport`, `SFTPProtocol.open_channel(self, transport: Transport)
  -> SFTPClient`. `AdaptedFTP.__enter__`/`AdaptedSFTP.__enter__` call both in sequence — **neither class
  stores the `Transport` anywhere**; it's used once to obtain a handler and then discarded from the
  Adapted class's perspective (the pool, in Task 4+, is what remembers it).
- Consumes: nothing from earlier tasks (this is the first task).

- [ ] **Step 1: Update `_TestFTPProtocol` and `_TestSFTPProtocol` in `tests/ftp/conftest.py` to the new
  shape, then run the existing suite to confirm the rename alone (with `adapter.py`/`types.py` still on
  the old interface) fails loudly rather than silently**

  Replace `tests/ftp/conftest.py:51-59` (`_TestFTPProtocol.get_conn_handler`):

  ```python
  def get_transport(self) -> FTP:
    from ftplib import FTP

    conn = FTP()
    conn.connect("127.0.0.1", self._port)
    conn.login(self._username, self._password)
    self._conn = conn
    return conn

  def open_channel(self, transport: FTP) -> FTP:
    return transport
  ```

  Replace `tests/ftp/conftest.py:206-210` (`_TestSFTPProtocol.get_conn_handler`):

  ```python
  def get_transport(self) -> paramiko.Transport:
    transport = paramiko.Transport(("127.0.0.1", self._port))
    transport.connect(username="anyone", password="anything")
    self._transport = transport
    return transport

  def open_channel(self, transport: paramiko.Transport) -> paramiko.SFTPClient:
    return paramiko.SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]
  ```

- [ ] **Step 2: Run the targeted suite to confirm it fails against the still-old `types.py`/`adapter.py`**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py -v`
  Expected: FAIL (AttributeError on `get_conn_handler`, or the fixtures no longer matching `FTPProtocol`'s
  abstract methods) — confirms the fixtures actually drive real behavior, not a no-op rename.

- [ ] **Step 3: Reshape `FTPProtocolBase`/`FTPProtocol`/`SFTPProtocol` in `src/aeth_ext/ftp/types.py`**

  Replace lines 37-64:

  ```python
  class FTPProtocolBase(Protocol):
    KIND: ProtocolEnum

    @abstractmethod
    def get_transport(self) -> Any:
      raise NotImplementedError

    @abstractmethod
    def open_channel(self, transport: Any) -> Any:
      raise NotImplementedError

    @abstractmethod
    def close_conn_handler(self) -> None:
      raise NotImplementedError


  class FTPProtocol(FTPProtocolBase):
    KIND = ProtocolEnum.FTP

    @override
    @abstractmethod
    def get_transport(self) -> FTP:
      raise NotImplementedError

    @override
    @abstractmethod
    def open_channel(self, transport: FTP) -> FTP:
      raise NotImplementedError


  class SFTPProtocol(FTPProtocolBase):
    KIND = ProtocolEnum.SFTP

    @override
    @abstractmethod
    def get_transport(self) -> Transport:
      raise NotImplementedError

    @override
    @abstractmethod
    def open_channel(self, transport: Transport) -> SFTPClient:
      raise NotImplementedError
  ```

  `Transport` needs importing under `TYPE_CHECKING` alongside the existing `SFTPClient` import
  (`from paramiko import SFTPClient` at `types.py:15` becomes `from paramiko import SFTPClient,
  Transport`) — these are plain `Protocol` method annotations, not pydantic dataclass fields, so
  `TYPE_CHECKING`-only is correct here (per the Annotation Conventions global constraint's exception for
  Pyright-only annotations).

- [ ] **Step 4: Update `AdaptedFTP.__enter__`/`AdaptedSFTP.__enter__` in `src/aeth_ext/ftp/adapter.py`**

  `adapter.py:67-70`:

  ```python
  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler = self.proto_instance.open_channel(self.proto_instance.get_transport())
    return self
  ```

  `adapter.py:336-339` — identical pattern, no new slot, no stored `Transport`:

  ```python
  def __enter__(self) -> Self:
    if self.handler is None:
      self.handler = self.proto_instance.open_channel(self.proto_instance.get_transport())
    return self
  ```

- [ ] **Step 5: Update `FTPAdapter._grow`'s dial call and fix the `pool_return` kwarg bug in
  `start_session`**

  `adapter.py:664` (`_grow`, FTP path only for now — Task 4 splits SFTP out):

  ```python
  handler = self.ftp_protocol().open_channel(self.ftp_protocol().get_transport())
  ```

  (Calling `self.ftp_protocol()` twice constructs two protocol instances; this is fine — `FTPProtocol`
  implementations are stateless dial helpers keyed by connection args, not by identity — but note it
  explicitly here so Task 4 doesn't accidentally "fix" it into a shared-state bug.)

  `adapter.py:695`: change `pool_return=self._pool_return` to `return_to_pool_callback=self._pool_return`
  — this was already broken (constructor parameter is named `return_to_pool_callback`); fixing it here
  because this task already touches the surrounding lines and Task 4 depends on `_pool_return` actually
  being wired up to verify two-tier release behavior.

- [ ] **Step 6: Update every `get_conn_handler` reference in `tests/ftp/test_ftp_adapter_factory.py`**

  - `_NoOpFTPProtocol` (lines 21-34): rename `get_conn_handler` → `get_transport` (returns `FTP()`), add
    `open_channel(self, transport: FTP) -> FTP: return transport`.
  - `_NoOpSFTPProtocol` (lines 46-55): rename `get_conn_handler` → `get_transport` — but this test double's
    whole point is returning an inert `_InertSFTPClient` without ever dialing a `Transport`. Since
    `get_transport()` must return something `open_channel()` can accept, and `_InertSFTPClient` has no
    real `Transport`, use `object()` as the fake transport and ignore it in `open_channel`:

    ```python
    class _NoOpSFTPProtocol(SFTPProtocol):
      @override
      def get_transport(self) -> Transport:
        return object()  # pyright: ignore[reportReturnType] -- never dialed, just a stand-in identity

      @override
      def open_channel(self, transport: Transport) -> SFTPClient:
        return _InertSFTPClient()

      @override
      def close_conn_handler(self) -> None:
        pass
    ```

  - `_TestFTPProtocolFactory._Protocol` (lines 149-163): rename `get_conn_handler` → `get_transport`
    (unchanged body), add `open_channel(self, transport: FTP) -> FTP: return transport`.
  - `_FakeProtocol`/`_FakeHandler` (lines 247-266): rename `get_conn_handler` → `get_transport`, add
    `open_channel` passthrough.
  - `TestRampUpDiscoversRealCeiling` and `TestRecoveringADiscoveredCeiling` (lines 313-434): these
    monkeypatch `protocol_cls.get_conn_handler` directly to inject `ConnectionRefusedError`s. Since
    `_grow()` (per Step 5) now calls `get_transport()` then `open_channel()`, and the refusal needs to
    happen on the *transport* dial (matching the design's "Transport tier ... using the existing
    connection-refused probing"), retarget these monkeypatches to `protocol_cls.get_transport` instead of
    `get_conn_handler`, keeping the rest of each test (assertions on `_discovered_max`, `open_attempts`)
    unchanged.

- [ ] **Step 7: Run the full targeted suite and confirm it passes**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS — this task is a pure interface rename plus one bugfix; no pooling behavior changed yet.

- [ ] **Step 8: Leave changes staged/unstaged for review (do not commit)**

---

### Task 2: `chunk_size` constructor parameter

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` — `AdaptedFTP.__init__`/`upload_file`/`download_file`/
  `_ftp_to_sftp`/`_ftp_to_ftp`; `AdaptedSFTP.__init__`/`upload_file`/`download_file`/`_sftp_to_ftp`/
  `_sftp_to_sftp`; `FTPAdapter.__init__`/`start_session`
- Test: `tests/ftp/test_ftp_adapter_factory.py` (new test class)

**Interfaces:**
- Consumes: `AdaptedFTP.__init__`/`AdaptedSFTP.__init__` signatures from Task 1 (unchanged by Task 1).
- Produces: `FTPAdapter(..., chunk_size: int = 8192)`, `AdaptedFTP(..., chunk_size: int = 8192)`,
  `AdaptedSFTP(..., chunk_size: int = 8192)`; both classes expose `self.chunk_size: int` used everywhere
  the `8192` literal previously appeared.

- [ ] **Step 1: Write the failing test**

  Add to `tests/ftp/test_ftp_adapter_factory.py`:

  ```python
  class TestChunkSizeThreading:
    def test_custom_chunk_size_reaches_the_session(self, ftp_env: _FTPTestEnv) -> None:
      adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4, chunk_size=4096)

      with adapter.start_session() as session:
        assert session.chunk_size == 4096

    def test_default_chunk_size_is_8192(self, ftp_env: _FTPTestEnv) -> None:
      adapter = FTPAdapter(_TestFTPProtocolFactory(ftp_env), max_connections=4)

      with adapter.start_session() as session:
        assert session.chunk_size == 8192
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestChunkSizeThreading -v`
  Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'chunk_size'` (or
  `AttributeError: 'AdaptedFTP' object has no attribute 'chunk_size'`).

- [ ] **Step 3: Add `chunk_size` to `AdaptedFTP`/`AdaptedSFTP`/`FTPAdapter` and replace the literals**

  `AdaptedFTP.__slots__`: add `"chunk_size"`. `AdaptedFTP.__init__`: add `chunk_size: int = 8192` param,
  `self.chunk_size = chunk_size`. Replace every `8192` literal in `upload_file` (`callback(8192)` →
  `callback(self.chunk_size)`), `download_file` (`conn.recv(8192)` → `conn.recv(self.chunk_size)`),
  `_ftp_to_sftp` (`source_conn.recv(8192)`), `_ftp_to_ftp` (`source_conn.recv(8192)`) with
  `self.chunk_size`.

  Same pattern for `AdaptedSFTP`: `__slots__` add `"chunk_size"`; `__init__` add the param; replace the
  `8192` literals in `upload_file` (`callback(8192)`), `download_file` (`remote_file.read(8192)`),
  `_sftp_to_ftp` (`source_file.read(8192)`), `_sftp_to_sftp` (`source_file.read(8192)`).

  `FTPAdapter.__init__`: add `chunk_size: int = 8192` param, `self.chunk_size = chunk_size` (new
  `__slots__` entry `"chunk_size"`). `FTPAdapter.start_session`: pass `chunk_size=self.chunk_size` into
  the `self.protocol_handler(...)` construction call.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestChunkSizeThreading -v`
  Expected: PASS

- [ ] **Step 5: Run the full targeted suite to confirm no regression, then leave uncommitted**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 3: Generic multi-callback support on `AdaptedFTP`/`AdaptedSFTP`

This is the one approved widening of the Adapted classes' contract: they already accept a per-chunk
`callback` at call time; this task adds a constructor-time `callbacks` sequence that gets merged into the
*same* per-chunk invocation. The Adapted classes never learn what any given callback is "for" — they just
call everything they were handed, exactly as they already call the single `callback` today. This task has
no dependency on pooling and is fully testable without `SFTPPool` existing yet.

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` — `AdaptedFTP.__init__`/`upload_file`/`download_file`/
  `_ftp_to_sftp`/`_ftp_to_ftp`; `AdaptedSFTP.__init__`/`upload_file`/`download_file`/`_sftp_to_ftp`/
  `_sftp_to_sftp`
- Test: `tests/ftp/test_ftp_adapter_factory.py` (new `TestConstructorCallbacks` class)

**Interfaces:**
- Consumes: `chunk_size` from Task 2 (unrelated, just present in the same constructors).
- Produces: `AdaptedFTP(..., callbacks: Sequence[Callable[[bytes], Any]] = ())`, `AdaptedSFTP(...,
  callbacks: Sequence[Callable[[bytes], Any]] = ())`; both expose `self._callbacks: tuple[Callable[[bytes],
  Any], ...]`. Every per-chunk sink invocation (`download_file`'s `callback`, `transfer_file`'s `callback`,
  and — since it's the same underlying mechanism — `upload_file`'s post-pull tap) now calls each of
  `self._callbacks` in addition to whatever was passed at call time. Task 5 (instrumentation) is the only
  later task that constructs a non-empty `callbacks` sequence — it passes `[observer]` from `FTPAdapter`
  at session-construction time and touches nothing else about this mechanism.

- [ ] **Step 1: Write the failing tests**

  Add to `tests/ftp/test_ftp_adapter_factory.py`:

  ```python
  class TestConstructorCallbacks:
    def test_download_file_invokes_constructor_callbacks_alongside_the_call_time_one(
      self, ftp_env: _FTPTestEnv, tmp_path: Path
    ) -> None:
      adapter = ftp_env.make_adapter()
      seen_by_ctor_cb: list[bytes] = []
      seen_by_call_cb: list[bytes] = []
      adapter._callbacks = (seen_by_ctor_cb.append,)  # constructed via make_adapter(); patch directly for this test

      with adapter as ftp:
        (tmp_path / "src").write_bytes(b"hello world")
        with open(tmp_path / "src", "rb") as f:
          ftp.handler.storbinary("STOR probe", f)
        ftp.download_file("probe", seen_by_call_cb.append)

      assert b"".join(seen_by_ctor_cb) == b"hello world"
      assert b"".join(seen_by_call_cb) == b"hello world"

    def test_upload_file_taps_constructor_callbacks_with_the_pulled_bytes(self, ftp_env: _FTPTestEnv) -> None:
      adapter = ftp_env.make_adapter()
      seen: list[bytes] = []
      adapter._callbacks = (seen.append,)
      chunks = [b"abc", b"def", b""]

      def _source(_size: int) -> bytes:
        return chunks.pop(0)

      with adapter as ftp:
        ftp.upload_file("probe2", _source, file_size=6)

      assert b"".join(seen) == b"abcdef"
  ```

  `_FTPTestEnv.make_adapter` (`tests/ftp/conftest.py:76-84`) constructs `AdaptedFTP` directly and doesn't
  expose a `callbacks=` passthrough — patching `adapter._callbacks` after construction is deliberate here
  (simplest way to exercise this without changing the fixture's signature) rather than plumbing a new
  fixture parameter through for a two-test increment.

- [ ] **Step 2: Run tests to verify they fail**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestConstructorCallbacks -v`
  Expected: FAIL — either `AttributeError` on `adapter._callbacks` (slot doesn't exist) or, once the slot
  exists but merging isn't wired up, `seen_by_ctor_cb`/`seen` staying empty.

- [ ] **Step 3: Add `callbacks` and merge it into every per-chunk invocation**

  `AdaptedFTP.__slots__`: add `"_callbacks"`. `AdaptedFTP.__init__`: add `callbacks:
  Sequence[Callable[[bytes], Any]] = ()` param, `self._callbacks = tuple(callbacks)`.

  `upload_file` — after the existing pull, tap every constructor callback with the pulled bytes:

  ```python
      while buffer := callback(self.chunk_size):
        conn.sendall(buffer)
        for observer in self._callbacks:
          observer(buffer)
        if self.pbar is not None:
          ...
  ```

  `download_file` — call every constructor callback alongside the call-time one:

  ```python
      while data := conn.recv(self.chunk_size):
        callback(data)
        for observer in self._callbacks:
          observer(data)
        if self.pbar is not None:
          ...
  ```

  `_ftp_to_sftp`/`_ftp_to_ftp` — same pattern, alongside the existing `if callback is not None:
  callback(data)`:

  ```python
        while data := source_conn.recv(self.chunk_size):
          if callback is not None:
            callback(data)
          for observer in self._callbacks:
            observer(data)
          ...
  ```

  Identical changes to `AdaptedSFTP`: `__slots__` add `"_callbacks"`, `__init__` add the param, and the
  same merge pattern in `upload_file`, `download_file`, `_sftp_to_ftp`, `_sftp_to_sftp`.

- [ ] **Step 4: Run tests to verify they pass**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestConstructorCallbacks -v`
  Expected: PASS

- [ ] **Step 5: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 4: `SFTPPool`/`TransportState` module and fixed-cap channel-tier wiring

**Files:**
- Create: `src/aeth_ext/ftp/sftp_pool.py`
- Modify: `src/aeth_ext/ftp/adapter.py` — `FTPAdapter.__init__` (new `channels_per_transport` param and
  `_sftp_pool` slot), split `_grow`/`_checkout_idle`/`_release`/`_discard`/`_pool_return` into FTP/SFTP
  variants, `start_session` dispatches by `protocol_handler`. **No changes to `AdaptedSFTP` in this task**
  — the pool tracks handler→`TransportState` ownership entirely on its own side.
- Test: `tests/ftp/test_sftp_pool.py` (new, unit-level, no real network)
- Test: `tests/ftp/test_ftp_adapter_factory.py` (new `TestSFTPChannelMultiplexing` class, real loopback
  SFTP server via `sftp_env`)

**Interfaces:**
- Consumes: `SFTPProtocol.get_transport()`/`open_channel(transport)` from Task 1. Nothing from Task 3
  (that mechanism is independent; Task 5 is what connects them).
- Produces: `TransportState(transport: Transport)` with `.channel_count: int`, `.ewma_throughput: float |
  None`, `.sample_count: int`. `Channel(handle: SFTPClient, state: TransportState)`. `SFTPPool(
  channels_per_transport: int)` with `.register_transport(transport: Transport) -> TransportState`,
  `.checkout_channel() -> Channel | None`, `.checkout_channel_blocking() -> Channel`,
  `.pick_growth_target() -> TransportState | None`, `.release_channel(channel: Channel) -> None`,
  `.discard_channel(channel: Channel) -> None`, `.discard_transport(state: TransportState) -> list[Channel]`,
  **`.track_handle(handler: SFTPClient, state: TransportState) -> None`** and **`.state_for_handle(handler:
  SFTPClient) -> TransportState | None`** — the mechanism `FTPAdapter._pool_return` uses to recover which
  `TransportState` a bare returned `SFTPClient` belongs to, entirely inside the pool. `FTPAdapter`'s
  `return_to_pool_callback` therefore keeps the exact same shape `AdaptedFTP` already uses —
  `Callable[[SFTPClient, bool], None]` — no `Transport` parameter added to it, and nothing added to
  `AdaptedSFTP`. These names are used unchanged through Tasks 5-8 — do not rename them.

- [ ] **Step 1: Write the failing unit tests for `SFTPPool` in isolation**

  Create `tests/ftp/test_sftp_pool.py`:

  ```python
  """Unit tests for `aeth_ext.ftp.sftp_pool.SFTPPool` -- pure bookkeeping, no real network."""

  # First party imports
  from aeth_ext.ftp.sftp_pool import Channel, SFTPPool, TransportState


  class _FakeTransport:
    """Stands in for `paramiko.Transport` -- SFTPPool only ever holds it as a dict key/attribute,
    never calls its methods, so identity is all that matters here."""


  class _FakeChannel:
    """Stands in for `paramiko.SFTPClient` -- same reasoning as `_FakeTransport`."""


  class TestChannelReuseUnderCap:
    def test_new_pool_has_no_growth_target(self) -> None:
      pool = SFTPPool(channels_per_transport=4)

      assert pool.pick_growth_target() is None

    def test_registered_transport_becomes_the_growth_target(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      transport = _FakeTransport()

      state = pool.register_transport(transport)

      assert pool.pick_growth_target() is state

    def test_transport_at_channel_cap_is_not_a_growth_target(self) -> None:
      pool = SFTPPool(channels_per_transport=2)
      state = pool.register_transport(_FakeTransport())
      state.channel_count = 2

      assert pool.pick_growth_target() is None

    def test_idle_channel_is_returned_and_removed_from_the_pool(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      channel = Channel(handle=_FakeChannel(), state=state)
      pool.release_channel(channel)

      assert pool.checkout_channel() is channel
      assert pool.checkout_channel() is None

    def test_discarding_a_transport_returns_and_clears_its_idle_channels(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      idle_channel = Channel(handle=_FakeChannel(), state=state)
      pool.release_channel(idle_channel)

      orphaned = pool.discard_transport(state)

      assert orphaned == [idle_channel]
      assert pool.checkout_channel() is None
      assert pool.pick_growth_target() is None


  class TestHandleToStateLookup:
    def test_untracked_handle_resolves_to_none(self) -> None:
      pool = SFTPPool(channels_per_transport=4)

      assert pool.state_for_handle(_FakeChannel()) is None

    def test_tracked_handle_resolves_to_its_state(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      handle = _FakeChannel()

      pool.track_handle(handle, state)

      assert pool.state_for_handle(handle) is state
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'aeth_ext.ftp.sftp_pool'`.

- [ ] **Step 3: Write `src/aeth_ext/ftp/sftp_pool.py`**

  ```python
  """Two-tier (Transport, channel) bookkeeping for `FTPAdapter`'s SFTP pooling.

  Kept out of ``adapter.py`` so the plain-FTP pooling path (unchanged fixed one-connection-per-slot
  queue) isn't diluted by SFTP-only concepts, and kept entirely out of `AdaptedSFTP` -- that class only
  ever sees a bare `SFTPClient` handler and a `return_to_pool_callback`, identical in shape to
  `AdaptedFTP`'s. This module is the only place that knows a checked-out `SFTPClient` came from a
  specific `Transport`.
  """

  # Standard library imports
  from dataclasses import dataclass
  from queue import Queue
  from threading import Lock
  from typing import ClassVar

  # Third party imports
  from paramiko import SFTPClient, Transport

  __all__ = ["Channel", "SFTPPool", "TransportState"]


  @dataclass(slots=True)
  class TransportState:
    """Per-`Transport` bookkeeping: how many channels it currently holds and its measured throughput."""

    transport: Transport
    channel_count: int = 0
    ewma_throughput: float | None = None
    sample_count: int = 0


  @dataclass(slots=True)
  class Channel:
    """A checked-out or idle SFTP handle, tagged with the `TransportState` it was opened from."""

    handle: SFTPClient
    state: TransportState


  class SFTPPool:
    """Owns the channel tier on top of `FTPAdapter`'s existing Transport-tier growth/probing, plus the
    handler-identity lookup `FTPAdapter._pool_return` needs to route a released `SFTPClient` back to its
    `TransportState` without `AdaptedSFTP` ever holding a reference to that state itself."""

    def __init__(self, channels_per_transport: int) -> None:
      self.channels_per_transport = channels_per_transport
      self._states: dict[int, TransportState] = {}
      self._handle_states: dict[int, TransportState] = {}
      self._idle: list[Channel] = []
      self._lock = Lock()
      self._wakeup: Queue[None] = Queue()

    def register_transport(self, transport: Transport) -> TransportState:
      state = TransportState(transport=transport)
      with self._lock:
        self._states[id(transport)] = state
      return state

    def track_handle(self, handler: SFTPClient, state: TransportState) -> None:
      with self._lock:
        self._handle_states[id(handler)] = state

    def state_for_handle(self, handler: SFTPClient) -> TransportState | None:
      with self._lock:
        return self._handle_states.get(id(handler))

    def pick_growth_target(self) -> TransportState | None:
      """Returns a `TransportState` under its channel cap to open a new channel on, or `None` if
      every live `Transport` is at cap (the caller should dial a new `Transport` instead)."""
      with self._lock:
        candidates = [s for s in self._states.values() if s.channel_count < self.channels_per_transport]
        if not candidates:
          return None
        candidates.sort(key=lambda s: s.channel_count)
        return candidates[0]

    def checkout_channel(self) -> Channel | None:
      with self._lock:
        if not self._idle:
          return None
        return self._idle.pop()

    def checkout_channel_blocking(self) -> Channel:
      while True:
        channel = self.checkout_channel()
        if channel is not None:
          return channel
        self._wakeup.get()

    def release_channel(self, channel: Channel) -> None:
      with self._lock:
        self._idle.append(channel)
      self._wakeup.put_nowait(None)

    def discard_channel(self, channel: Channel) -> None:
      with self._lock:
        channel.state.channel_count -= 1
        self._handle_states.pop(id(channel.handle), None)
        if channel in self._idle:
          self._idle.remove(channel)

    def discard_transport(self, state: TransportState) -> list[Channel]:
      """Removes `state` from tracking and returns whichever of its channels were sitting idle, for
      the caller to close. Channels still checked out elsewhere are not returned here -- they aren't
      tracked in `_idle` and fail naturally on their next I/O (see design's Error handling section)."""
      with self._lock:
        self._states.pop(id(state.transport), None)
        orphaned = [c for c in self._idle if c.state is state]
        self._idle = [c for c in self._idle if c.state is not state]
        for c in orphaned:
          self._handle_states.pop(id(c.handle), None)
        return orphaned
  ```

  Note: `channel in self._idle` / `list.remove` and the `discard_transport` list comprehensions are O(n)
  over a list expected to stay small (`channels_per_transport` is a handful per `Transport`, and
  `Transport` count is bounded by `max_connections`) — acceptable; do not introduce an index structure for
  this.

- [ ] **Step 4: Run tests to verify they pass**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py -v`
  Expected: PASS

- [ ] **Step 5: Write the failing integration test for real channel reuse over one `Transport`**

  Add to `tests/ftp/test_ftp_adapter_factory.py`, plus the `_TestSFTPProtocolFactory` helper it needs
  (same role as `_TestFTPProtocolFactory`, adapting `_SFTPTestEnv` into a `type[SFTPProtocol]` callable
  `FTPAdapter.__init__` can call directly):

  ```python
  class _TestSFTPProtocolFactory:
    def __new__(cls, sftp_env: _SFTPTestEnv) -> type[SFTPProtocol]:
      adapter = sftp_env.make_adapter()
      port = adapter.proto_instance._port  # pyright: ignore[reportPrivateUsage]

      class _Protocol(SFTPProtocol):
        KIND = ProtocolEnum.SFTP

        @override
        def get_transport(self) -> Transport:
          transport = Transport(("127.0.0.1", port))
          transport.connect(username="anyone", password="anything")
          return transport

        @override
        def open_channel(self, transport: Transport) -> SFTPClient:
          return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

        @override
        def close_conn_handler(self) -> None:
          pass

      return _Protocol


  class TestSFTPChannelMultiplexing:
    def test_two_checkouts_share_one_transports_channel_cap(self, sftp_env: _SFTPTestEnv) -> None:
      """Two concurrently-checked-out SFTP sessions should multiplex over the same `Transport`
      instead of each dialing a fresh TCP connection, as long as both fit under
      `channels_per_transport`. Asserted via the pool's own bookkeeping (not a `.transport` attribute
      on the session -- the Adapted classes don't expose one) by checking both handlers resolve to the
      same `TransportState`."""
      protocol_cls = _TestSFTPProtocolFactory(sftp_env)
      adapter = FTPAdapter(protocol_cls, max_connections=4, channels_per_transport=4)

      first = adapter.start_session()
      second = adapter.start_session()

      state_first = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage]
      state_second = adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage]
      assert state_first is state_second
      assert first.handler is not second.handler
      first.__exit__(None, None, None)
      second.__exit__(None, None, None)

    def test_channel_cap_forces_a_second_transport(self, sftp_env: _SFTPTestEnv) -> None:
      protocol_cls = _TestSFTPProtocolFactory(sftp_env)
      adapter = FTPAdapter(protocol_cls, max_connections=4, channels_per_transport=1)

      first = adapter.start_session()
      second = adapter.start_session()

      state_first = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage]
      state_second = adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage]
      assert state_first is not state_second
      first.__exit__(None, None, None)
      second.__exit__(None, None, None)
  ```

  Add `from paramiko import Transport` to this test file's imports (it already imports `SFTPClient`).

- [ ] **Step 6: Run the new integration tests to verify they fail**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestSFTPChannelMultiplexing -v`
  Expected: FAIL — `FTPAdapter.__init__` doesn't accept `channels_per_transport` yet, `_grow` doesn't
  multiplex, `_sftp_pool` doesn't exist.

- [ ] **Step 7: Wire `SFTPPool` into `FTPAdapter`**

  `FTPAdapter.__slots__`: add `"channels_per_transport"`, `"_sftp_pool"`.
  `FTPAdapter.__init__`: add `channels_per_transport: int = 4` param. After the existing
  `if issubclass(ftp_protocol, FTPProtocol): ... elif issubclass(ftp_protocol, SFTPProtocol): ...` block,
  add:

  ```python
    self.channels_per_transport = channels_per_transport
    self._sftp_pool = SFTPPool(channels_per_transport) if self.protocol_handler is AdaptedSFTP else None
  ```

  (`from aeth_ext.ftp.sftp_pool import Channel, SFTPPool, TransportState` added to `adapter.py`'s top-level
  imports.)

  Rename the existing `_grow`/`_checkout_idle`/`_release`/`_discard` to `_grow_ftp`/`_checkout_idle_ftp`/
  `_release_ftp`/`_discard_ftp` (bodies unchanged except the Task 1 Step 5 rename already applied to the
  dial call), and add their SFTP counterparts:

  ```python
  def _grow_sftp(self) -> Channel | None:
    assert self._sftp_pool is not None
    target = self._sftp_pool.pick_growth_target()
    if target is not None:
      channel_handle = self.ftp_protocol().open_channel(target.transport)
      target.channel_count += 1
      self._sftp_pool.track_handle(channel_handle, target)
      return Channel(handle=channel_handle, state=target)

    with self._size_lock:
      if self._current_size >= self._effective_ceiling():
        return None
      self._current_size += 1
      self._ensure_registered_for_shutdown()
      try:
        transport = self.ftp_protocol().get_transport()
      except ConnectionRefusedError, TimeoutError, OSError:
        self._current_size -= 1
        self._discovered_max = self._current_size
        self._discovered_max_last_probe = monotonic()
        raise
      else:
        if self._discovered_max is not None and self._current_size > self._discovered_max:
          self._discovered_max = self._current_size

    state = self._sftp_pool.register_transport(transport)
    channel_handle = self.ftp_protocol().open_channel(transport)
    state.channel_count += 1
    self._sftp_pool.track_handle(channel_handle, state)
    return Channel(handle=channel_handle, state=state)

  def _checkout_idle_sftp(self) -> Channel | None:
    assert self._sftp_pool is not None
    candidate = self._sftp_pool.checkout_channel()
    if candidate is None:
      return None
    if self._validate(candidate.handle):
      return candidate
    self._discard_sftp(candidate)
    return None

  def _discard_sftp(self, channel: Channel) -> None:
    self._sftp_pool.discard_channel(channel)  # pyright: ignore[reportOptionalMemberAccess]
    try:
      channel.handle.close()
    except Exception:  # noqa: BLE001, S110 -- best-effort close of an already-broken connection
      pass
  ```

  The `with self._size_lock:` block in `_grow_sftp` deliberately mirrors the original `_grow`'s scope
  exactly (the dial itself stays inside the lock, matching the FTP path's existing behavior) — only
  `register_transport`/`open_channel`/`track_handle` (channel-tier, `SFTPPool`'s own lock) happen after
  releasing `_size_lock`, since they're independent of the Transport-tier size accounting.

  `_pool_return` (`adapter.py:708-713`) becomes the single dispatch point — `return_to_pool_callback`'s
  shape is unchanged (`Callable[[FTP | SFTPClient, bool], None]`, identical to before this task):

  ```python
  def _pool_return(self, handler: FTP | SFTPClient, is_fatal: bool) -> None:
    if self._sftp_pool is None:
      if is_fatal:
        self._discard_ftp(handler)
      else:
        self._release_ftp(handler)
    else:
      assert isinstance(handler, SFTPClient)
      state = self._sftp_pool.state_for_handle(handler)
      assert state is not None, "handler must have been tracked via SFTPPool.track_handle at checkout"
      channel = Channel(handle=handler, state=state)
      if is_fatal:
        self._discard_sftp(channel)
      else:
        self._sftp_pool.release_channel(channel)
  ```

  `start_session` (`adapter.py:675-706`) dispatches on `self._sftp_pool`:

  ```python
  def start_session(self) -> HandlerType_T:
    try:
      if self.container_cvar is not None:
        container_cls = self.container_cvar.get()
      else:
        container_cls = self.container_cls
    except LookupError:
      container_cls = self.container_cls

    if self._sftp_pool is None:
      handler = self._checkout_idle_ftp()
      if handler is None:
        handler = self._grow_ftp()
      if handler is None:
        handler = self._idle.get()
    else:
      channel = self._checkout_idle_sftp()
      if channel is None:
        channel = self._grow_sftp()
      if channel is None:
        channel = self._sftp_pool.checkout_channel_blocking()
      handler = channel.handle

    session = self.protocol_handler(
      self.ftp_protocol(),
      container_cls=container_cls,
      pbar=self.pbar,
      tzinfo=self.tzinfo,
      chunk_size=self.chunk_size,
      return_to_pool_callback=self._pool_return,
    )
    if isinstance(session, AdaptedFTP):
      assert isinstance(handler, FTP), "protocol_handler is AdaptedFTP, so growth must have returned an FTP handler"
      session.handler = handler
    else:
      assert isinstance(handler, SFTPClient), (
        "protocol_handler is AdaptedSFTP, so growth must have returned an SFTPClient handler"
      )
      session.handler = handler
    self._ensure_keepalive_started()
    return session  # pyright: ignore[reportReturnType]
  ```

  Note `session` never receives anything transport-related — `handler` (a bare `SFTPClient`) is the only
  thing assigned onto it, exactly as `AdaptedFTP` already works today.

- [ ] **Step 8: Run the integration tests to verify they pass**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestSFTPChannelMultiplexing -v`
  Expected: PASS

- [ ] **Step 9: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 5: Instrumentation observer wiring

Connects Task 3's generic `callbacks` mechanism to Task 4's `SFTPPool` — `FTPAdapter` builds one observer
closure per checked-out channel and passes it into the session's constructor `callbacks=[...]`. Nothing
new is added to `AdaptedSFTP` itself; it already merges `self._callbacks` into every per-chunk invocation
from Task 3.

**Files:**
- Modify: `src/aeth_ext/ftp/sftp_pool.py` — `TransportState.update_throughput`
- Modify: `src/aeth_ext/ftp/adapter.py` — `FTPAdapter.start_session` builds the observer closure and
  passes it as `callbacks=[observer]` when constructing an SFTP session; new `FTPAdapter._make_instrument`
  helper.
- Test: `tests/ftp/test_sftp_pool.py` (new `TestUpdateThroughput` class)
- Test: `tests/ftp/test_ftp_adapter_factory.py` (new `TestThroughputInstrumentation` class)

**Interfaces:**
- Consumes: `TransportState`/`Channel`/`SFTPPool` from Task 4; `callbacks` constructor param from Task 3.
- Produces: `TransportState.update_throughput(self, nbytes: int, elapsed: float) -> None` updating
  `ewma_throughput`/`sample_count`. Task 6 reads `TransportState.ewma_throughput`/`.sample_count` to judge
  saturation — do not rename these.

- [ ] **Step 1: Write the failing unit test for `TransportState.update_throughput`**

  Add to `tests/ftp/test_sftp_pool.py`:

  ```python
  class TestUpdateThroughput:
    def test_first_sample_sets_ewma_directly(self) -> None:
      state = TransportState(transport=_FakeTransport())

      state.update_throughput(nbytes=8192, elapsed=1.0)

      assert state.ewma_throughput == 8192.0
      assert state.sample_count == 1

    def test_second_sample_blends_toward_the_new_rate(self) -> None:
      state = TransportState(transport=_FakeTransport())
      state.update_throughput(nbytes=1000, elapsed=1.0)

      state.update_throughput(nbytes=3000, elapsed=1.0)

      assert 1000.0 < state.ewma_throughput < 3000.0
      assert state.sample_count == 2
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestUpdateThroughput -v`
  Expected: FAIL with `AttributeError: 'TransportState' object has no attribute 'update_throughput'`.

- [ ] **Step 3: Implement `TransportState.update_throughput`**

  Add to `TransportState` in `sftp_pool.py`:

  ```python
    _EWMA_ALPHA: ClassVar[float] = 0.3

    def update_throughput(self, nbytes: int, elapsed: float) -> None:
      rate = nbytes / max(elapsed, 1e-6)
      if self.ewma_throughput is None:
        self.ewma_throughput = rate
      else:
        self.ewma_throughput = self._EWMA_ALPHA * rate + (1 - self._EWMA_ALPHA) * self.ewma_throughput
      self.sample_count += 1
  ```

  (`ClassVar` on a `slots=True` dataclass is fine — it isn't a per-instance field, so it isn't added to
  `__slots__`.)

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestUpdateThroughput -v`
  Expected: PASS

- [ ] **Step 5: Write the failing integration test for observer wiring**

  Add to `tests/ftp/test_ftp_adapter_factory.py` (needs `from pathlib import Path` as a real top-level
  import if not already present — it's used at runtime here, not just in an annotation):

  ```python
  class TestThroughputInstrumentation:
    def test_download_updates_the_owning_transports_throughput(self, sftp_env: _SFTPTestEnv, tmp_path: Path) -> None:
      protocol_cls = _TestSFTPProtocolFactory(sftp_env)
      adapter = FTPAdapter(protocol_cls, max_connections=4, channels_per_transport=4)

      (tmp_path / "probe_source").write_bytes(b"x" * 4096)
      with adapter.start_session() as session:
        assert isinstance(session, AdaptedSFTP)
        session.handler.put(str(tmp_path / "probe_source"), "probe_remote")
        received = bytearray()
        session.download_file("probe_remote", received.extend)
        state = adapter._sftp_pool.state_for_handle(session.handler)  # pyright: ignore[reportPrivateUsage]

      assert state is not None
      assert state.sample_count >= 1
      assert state.ewma_throughput is not None and state.ewma_throughput > 0
  ```

- [ ] **Step 6: Run test to verify it fails**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestThroughputInstrumentation -v`
  Expected: FAIL — `state.sample_count == 0` (no observer wired yet).

- [ ] **Step 7: Wire the observer into `start_session`**

  Add to `FTPAdapter`:

  ```python
    def _make_instrument(self, state: TransportState) -> Callable[[bytes], None]:
      last = [monotonic()]

      def observer(data: bytes) -> None:
        now = monotonic()
        elapsed = now - last[0]
        last[0] = now
        state.update_throughput(len(data), elapsed)

      return observer
  ```

  (One `observer` closure per checked-out session, so `last[0]` measures inter-chunk gaps for that
  specific transfer rather than being shared/contended across concurrent sessions on the same
  `Transport`.)

  In `start_session` (Task 4 Step 7), change the SFTP branch's session construction to pass the observer
  as one of Task 3's constructor `callbacks`:

  ```python
    else:
      channel = self._checkout_idle_sftp()
      if channel is None:
        channel = self._grow_sftp()
      if channel is None:
        channel = self._sftp_pool.checkout_channel_blocking()
      handler = channel.handle

    session = self.protocol_handler(
      self.ftp_protocol(),
      container_cls=container_cls,
      pbar=self.pbar,
      tzinfo=self.tzinfo,
      chunk_size=self.chunk_size,
      callbacks=[self._make_instrument(channel.state)] if self._sftp_pool is not None else (),
      return_to_pool_callback=self._pool_return,
    )
  ```

  `AdaptedFTP` also accepts `callbacks` (from Task 3) and is passed `()` implicitly (its default) — the
  FTP branch of `start_session` doesn't need to pass `callbacks` explicitly since `()` is already the
  default.

- [ ] **Step 8: Run test to verify it passes**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestThroughputInstrumentation -v`
  Expected: PASS

- [ ] **Step 9: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 6: Saturation detection and rebalancing

**Files:**
- Modify: `src/aeth_ext/ftp/sftp_pool.py` — `TransportState.is_saturated`, `SFTPPool.best_live_throughput`,
  `SFTPPool.pick_growth_target` (excludes saturated), `SFTPPool.release_channel` (pop-on-release when
  saturated)
- Modify: `src/aeth_ext/ftp/adapter.py` — `_pool_return`'s non-fatal SFTP branch acts on
  `release_channel`'s new return value
- Test: `tests/ftp/test_sftp_pool.py` (new `TestSaturationRouting` class)

**Interfaces:**
- Consumes: `TransportState.ewma_throughput`/`.sample_count` from Task 5.
- Produces: `TransportState.is_saturated(self, best_throughput: float) -> bool`.
  `SFTPPool.best_live_throughput(self) -> float | None`. `SFTPPool.release_channel(self, channel: Channel)
  -> bool` (changed return type: `True` if popped/closed due to saturation, `False` if idled normally) —
  Task 4's `_pool_return` call site is updated in this task to use the return value.

- [ ] **Step 1: Write the failing tests**

  Add to `tests/ftp/test_sftp_pool.py`:

  ```python
  class TestSaturationRouting:
    def test_transport_with_no_samples_is_never_saturated(self) -> None:
      state = TransportState(transport=_FakeTransport())

      assert state.is_saturated(best_throughput=1_000_000.0) is False

    def test_transport_under_the_minimum_sample_count_is_not_yet_judged(self) -> None:
      state = TransportState(transport=_FakeTransport())
      state.update_throughput(nbytes=1, elapsed=1.0)  # far below best, but only 1 sample

      assert state.is_saturated(best_throughput=1_000_000.0) is False

    def test_transport_meaningfully_below_the_best_peer_is_saturated(self) -> None:
      state = TransportState(transport=_FakeTransport())
      for _ in range(TransportState._MIN_SAMPLES):
        state.update_throughput(nbytes=100, elapsed=1.0)

      assert state.is_saturated(best_throughput=1_000_000.0) is True

    def test_transport_close_to_the_best_peer_is_not_saturated(self) -> None:
      state = TransportState(transport=_FakeTransport())
      for _ in range(TransportState._MIN_SAMPLES):
        state.update_throughput(nbytes=950_000, elapsed=1.0)

      assert state.is_saturated(best_throughput=1_000_000.0) is False

    def test_growth_target_excludes_a_saturated_transport(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      fast = pool.register_transport(_FakeTransport())
      slow = pool.register_transport(_FakeTransport())
      for _ in range(TransportState._MIN_SAMPLES):
        fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
        slow.update_throughput(nbytes=100, elapsed=1.0)

      target = pool.pick_growth_target()

      assert target is fast

    def test_releasing_a_channel_on_a_saturated_transport_does_not_idle_it(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      fast = pool.register_transport(_FakeTransport())
      slow = pool.register_transport(_FakeTransport())
      for _ in range(TransportState._MIN_SAMPLES):
        fast.update_throughput(nbytes=1_000_000, elapsed=1.0)
        slow.update_throughput(nbytes=100, elapsed=1.0)
      slow.channel_count = 1
      channel = Channel(handle=_FakeChannel(), state=slow)

      popped = pool.release_channel(channel)

      assert popped is True
      assert pool.checkout_channel() is None
      assert slow.channel_count == 0
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestSaturationRouting -v`
  Expected: FAIL — `is_saturated` doesn't exist yet, `release_channel` doesn't return a bool yet.

- [ ] **Step 3: Implement saturation on `TransportState` and route/rebalance in `SFTPPool`**

  Add to `TransportState`:

  ```python
    _MIN_SAMPLES: ClassVar[int] = 3
    _SATURATION_RATIO: ClassVar[float] = 0.6

    def is_saturated(self, best_throughput: float) -> bool:
      if self.ewma_throughput is None or self.sample_count < self._MIN_SAMPLES:
        return False
      return self.ewma_throughput < best_throughput * self._SATURATION_RATIO
  ```

  Add to `SFTPPool`:

  ```python
    def best_live_throughput(self) -> float | None:
      with self._lock:
        samples = [s.ewma_throughput for s in self._states.values() if s.ewma_throughput is not None]
      return max(samples) if samples else None
  ```

  Update `pick_growth_target`:

  ```python
    def pick_growth_target(self) -> TransportState | None:
      best = self.best_live_throughput()
      with self._lock:
        candidates = [
          s
          for s in self._states.values()
          if s.channel_count < self.channels_per_transport and not (best is not None and s.is_saturated(best))
        ]
        if not candidates:
          return None
        candidates.sort(key=lambda s: s.channel_count)
        return candidates[0]
  ```

  Update `release_channel` to return whether the channel was popped (closed) instead of idled:

  ```python
    def release_channel(self, channel: Channel) -> bool:
      """Returns `True` if the channel was popped (its `Transport` is saturated -- caller must close
      the handle; `channel_count` has already been decremented here), `False` if it was returned to
      `_idle` for reuse."""
      best = self.best_live_throughput()
      with self._lock:
        if best is not None and channel.state.is_saturated(best):
          channel.state.channel_count -= 1
          self._handle_states.pop(id(channel.handle), None)
          self._wakeup.put_nowait(None)  # a slot freed up on the saturated Transport's cap
          return True
        self._idle.append(channel)
      self._wakeup.put_nowait(None)
      return False
  ```

  Update `FTPAdapter._pool_return` (Task 4) to act on the return value:

  ```python
    else:
      assert isinstance(handler, SFTPClient)
      state = self._sftp_pool.state_for_handle(handler)
      assert state is not None, "handler must have been tracked via SFTPPool.track_handle at checkout"
      channel = Channel(handle=handler, state=state)
      if is_fatal:
        self._discard_sftp(channel)
      elif self._sftp_pool.release_channel(channel):
        try:
          channel.handle.close()
        except Exception:  # noqa: BLE001, S110 -- best-effort close of a popped, still-healthy channel
          pass
  ```

  This must **not** also call `self._sftp_pool.discard_channel(channel)` on the popped branch — that would
  double-decrement `channel_count`, since `release_channel`'s saturated branch already decremented it.

- [ ] **Step 4: Run tests to verify they pass**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestSaturationRouting -v`
  Expected: PASS

- [ ] **Step 5: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 7: Cross-wave memory

**Files:**
- Modify: `src/aeth_ext/ftp/sftp_pool.py` — `SFTPPool` gains `_in_flight`, `_wave_running_max`,
  `last_wave_best_throughput`, wave-boundary detection in `mark_checked_out` (increment) and
  `release_channel`/`discard_channel` (decrement + zero-crossing check via a shared `_mark_returned`
  helper). `best_live_throughput` falls back to `last_wave_best_throughput`.
- Modify: `src/aeth_ext/ftp/adapter.py` — `_grow_sftp` calls `self._sftp_pool.mark_checked_out()` on both
  its "existing Transport" and "new Transport" branches (freshly grown channels never pass through
  `checkout_channel`, which already calls it internally).
- Test: `tests/ftp/test_sftp_pool.py` (new `TestCrossWaveMemory` class)

**Interfaces:**
- Consumes: `SFTPPool.best_live_throughput`, `release_channel`/`discard_channel` from Task 6.
- Produces: `SFTPPool.mark_checked_out(self) -> None`. `SFTPPool.last_wave_best_throughput: float | None`
  (public attribute, read by `best_live_throughput`'s fallback).

- [ ] **Step 1: Write the failing tests**

  Add to `tests/ftp/test_sftp_pool.py`:

  ```python
  class TestCrossWaveMemory:
    def test_no_memory_before_any_wave_completes(self) -> None:
      pool = SFTPPool(channels_per_transport=4)

      assert pool.last_wave_best_throughput is None

    def test_wave_boundary_persists_the_running_max_on_zero_crossing(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      c1 = Channel(handle=_FakeChannel(), state=state)
      c2 = Channel(handle=_FakeChannel(), state=state)
      pool.mark_checked_out()  # c1
      pool.mark_checked_out()  # c2
      state.update_throughput(nbytes=500_000, elapsed=1.0)
      pool.release_channel(c1)
      assert pool.last_wave_best_throughput is None  # still in-flight (c2)

      state.update_throughput(nbytes=1_000_000, elapsed=1.0)
      pool.release_channel(c2)  # in-flight count hits zero here

      assert pool.last_wave_best_throughput == 1_000_000.0

    def test_next_wave_starts_the_running_max_fresh(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      c1 = Channel(handle=_FakeChannel(), state=state)
      pool.mark_checked_out()
      state.update_throughput(nbytes=1_000_000, elapsed=1.0)
      pool.release_channel(c1)
      assert pool.last_wave_best_throughput == 1_000_000.0

      c2 = Channel(handle=_FakeChannel(), state=state)
      pool.mark_checked_out()
      state.update_throughput(nbytes=200_000, elapsed=1.0)
      pool.release_channel(c2)

      assert pool.last_wave_best_throughput == 200_000.0

    def test_best_live_throughput_falls_back_to_last_wave_memory_when_no_live_samples(self) -> None:
      pool = SFTPPool(channels_per_transport=4)
      state = pool.register_transport(_FakeTransport())
      c1 = Channel(handle=_FakeChannel(), state=state)
      pool.mark_checked_out()
      state.update_throughput(nbytes=1_000_000, elapsed=1.0)
      pool.release_channel(c1)

      pool.register_transport(_FakeTransport())  # a fresh Transport, no samples yet

      assert pool.best_live_throughput() == 1_000_000.0
  ```

- [ ] **Step 2: Run tests to verify they fail**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestCrossWaveMemory -v`
  Expected: FAIL with `AttributeError: 'SFTPPool' object has no attribute 'mark_checked_out'`.

- [ ] **Step 3: Implement the in-flight counter and wave-boundary detection**

  `SFTPPool.__init__` additions:

  ```python
      self._in_flight = 0
      self._wave_running_max = 0.0
      self.last_wave_best_throughput: float | None = None
  ```

  New methods:

  ```python
    def mark_checked_out(self) -> None:
      with self._lock:
        self._in_flight += 1

    def _mark_returned(self, state: TransportState) -> None:
      with self._lock:
        if state.ewma_throughput is not None:
          self._wave_running_max = max(self._wave_running_max, state.ewma_throughput)
        self._in_flight -= 1
        if self._in_flight == 0:
          self.last_wave_best_throughput = self._wave_running_max
          self._wave_running_max = 0.0
  ```

  Call `self._mark_returned(channel.state)` from `release_channel` (both branches — saturated-pop and
  normal-idle, right after each existing `channel_count -= 1`/`_idle.append`) and from `discard_channel`
  (right after `channel_count -= 1`).

  Update `best_live_throughput`'s fallback:

  ```python
    def best_live_throughput(self) -> float | None:
      with self._lock:
        samples = [s.ewma_throughput for s in self._states.values() if s.ewma_throughput is not None]
      if samples:
        return max(samples)
      return self.last_wave_best_throughput
  ```

  Wire `mark_checked_out` into `_grow_sftp` (`adapter.py`, Task 4): call `self._sftp_pool.mark_checked_out()`
  right before `return Channel(...)` in both the "existing Transport" branch and the "new Transport"
  branch. `checkout_channel`/`checkout_channel_blocking` already call it internally (add this call to
  `checkout_channel` right before its `return` of a non-`None` channel, and to
  `checkout_channel_blocking` right before its final `return channel`, as part of this step).

- [ ] **Step 4: Run tests to verify they pass**

  Run: `uv run pytest tests/ftp/test_sftp_pool.py::TestCrossWaveMemory -v`
  Expected: PASS

- [ ] **Step 5: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 8: Error handling — cascading discard on transport death

**Files:**
- Modify: `src/aeth_ext/ftp/adapter.py` — `_pool_return`'s SFTP fatal branch checks
  `state.transport.is_active()` and cascades to `SFTPPool.discard_transport`.
- Test: `tests/ftp/test_ftp_adapter_factory.py` (new `TestSFTPTransportDeathCascade` class)

**Interfaces:**
- Consumes: `SFTPPool.discard_transport`/`state_for_handle` from Task 4 (already implemented, `discard_transport`
  unused until now).

- [ ] **Step 1: Write the failing test**

  Add to `tests/ftp/test_ftp_adapter_factory.py`:

  ```python
  class TestSFTPTransportDeathCascade:
    def test_dead_transport_discards_its_idle_channels_too(self, sftp_env: _SFTPTestEnv) -> None:
      protocol_cls = _TestSFTPProtocolFactory(sftp_env)
      adapter = FTPAdapter(protocol_cls, max_connections=4, channels_per_transport=4)

      first = adapter.start_session()
      second = adapter.start_session()
      state = adapter._sftp_pool.state_for_handle(first.handler)  # pyright: ignore[reportPrivateUsage]
      assert state is adapter._sftp_pool.state_for_handle(second.handler)  # pyright: ignore[reportPrivateUsage]
      first.__exit__(None, None, None)  # released -- now idle on the shared Transport

      dead_transport = state.transport
      with pytest.raises(ConnectionError):
        with second:
          dead_transport.close()  # kill the Transport out from under the still-open session
          raise ConnectionError("simulated transport death")

      assert adapter._sftp_pool.state_for_handle(first.handler) is None  # pyright: ignore[reportPrivateUsage]

      # A fresh checkout must not reuse the now-dead idle channel from `first`.
      third = adapter.start_session()
      assert adapter._sftp_pool.state_for_handle(third.handler).transport is not dead_transport  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
      third.__exit__(None, None, None)
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestSFTPTransportDeathCascade -v`
  Expected: FAIL — the idle channel from `first` is still tracked and can still be handed out.

- [ ] **Step 3: Implement the cascade in `_pool_return`**

  ```python
  def _pool_return(self, handler: FTP | SFTPClient, is_fatal: bool) -> None:
    if self._sftp_pool is None:
      if is_fatal:
        self._discard_ftp(handler)
      else:
        self._release_ftp(handler)
      return

    assert isinstance(handler, SFTPClient)
    state = self._sftp_pool.state_for_handle(handler)
    assert state is not None, "handler must have been tracked via SFTPPool.track_handle at checkout"
    channel = Channel(handle=handler, state=state)
    if not is_fatal:
      if self._sftp_pool.release_channel(channel):
        try:
          channel.handle.close()
        except Exception:  # noqa: BLE001, S110 -- best-effort close of a popped, still-healthy channel
          pass
      return

    if state.transport.is_active():
      self._discard_sftp(channel)
      return

    orphaned = self._sftp_pool.discard_transport(state)
    for orphan in (channel, *orphaned):
      try:
        orphan.handle.close()
      except Exception:  # noqa: BLE001, S110 -- best-effort close during transport-death cleanup
        pass
    with self._size_lock:
      self._current_size -= 1
  ```

  The `self._current_size -= 1` on the dead-transport branch mirrors `_discard_ftp`'s existing accounting
  (a dead `Transport` is one fewer live Transport-tier slot, same as a dead FTP connection is one fewer
  FTP-tier slot) — without it, `_effective_ceiling()` would keep counting a closed `Transport` as occupying
  a slot forever.

  `Transport.is_active()` is a real `paramiko.Transport` method (returns `False` once the transport's
  connection has closed or errored) — no new import needed beyond the existing `Transport` type import
  from Task 1.

- [ ] **Step 4: Run test to verify it passes**

  Run: `uv run pytest tests/ftp/test_ftp_adapter_factory.py::TestSFTPTransportDeathCascade -v`
  Expected: PASS

- [ ] **Step 5: Run the full targeted suite to confirm no regression**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS. Do not commit.

---

### Task 9: Full verification sweep

**Files:** none modified — this task only runs checks.

- [ ] **Step 1: Run the full FTP/SFTP test suite**

  Run: `uv run pytest tests/ftp/ -v`
  Expected: PASS, all tests including every one added in Tasks 1-8.

- [ ] **Step 2: Run the full project test suite**

  Run: `uv run pytest`
  Expected: PASS — confirms nothing outside `tests/ftp/` depends on the old `get_conn_handler` shape or
  the old `AdaptedFTP`/`AdaptedSFTP` constructor signatures.

- [ ] **Step 3: Run lint and type checks**

  Run: `uv run ruff check .`
  Expected: no findings (or only pre-existing ones unrelated to this branch's files).

  Run: `uv run pyright`
  Expected: no new errors in `src/aeth_ext/ftp/` or `tests/ftp/`.

- [ ] **Step 4: Leave everything uncommitted for local review**

  Do not run `git commit`, `git add -A`, or any staging beyond what earlier tasks already did. Report the
  full diff (`git status`, `git diff --stat`) so the human can review before committing themselves.

---

## Self-Review Notes

- **Contract respected:** `AdaptedFTP`/`AdaptedSFTP` gain exactly one thing beyond `chunk_size` across this
  entire plan — the `callbacks` sequence from Task 3, which is a generalization of the per-chunk callback
  mechanism they already had, not a new pool-aware concept. No `.transport` attribute, no
  instrumentation-named method, no pool reference exists on either class at any point in this plan. Every
  place that needs to know "which `Transport` does this handler belong to" (Tasks 4, 5, 6, 8) resolves it
  through `SFTPPool.state_for_handle`/`track_handle`, entirely inside `sftp_pool.py`/`FTPAdapter`.
- **Spec coverage:** Motivation/Scope → Task 1 (interface parity) + Task 4 (Transport tier reuses existing
  probing, channel tier new). Chunk size → Task 2. Instrumentation via existing callbacks → Tasks 3 + 5
  (split specifically so the generic mechanism and its pool-specific use are separately testable and the
  generic one carries no pool awareness). Growth/shrink cross-sectional comparison → Task 6. Cross-wave
  memory → Task 7. Error handling → Task 8. Testing section's explicit list (channel reuse,
  cap-triggers-new-Transport, cascading discard, pop-on-release, wave-boundary persistence) → covered by
  Tasks 4, 6, 7, 8's integration/unit tests respectively. Out-of-scope items (no proactive Transport
  shrink-on-idle) are honored — no task adds one.
- **Type consistency:** `Channel`/`TransportState`/`SFTPPool` names introduced in Task 4 are used
  identically through Tasks 5-8 (`state.ewma_throughput`, `state.channel_count`, `state.sample_count`,
  `pool.checkout_channel`/`checkout_channel_blocking`/`release_channel`/`discard_channel`/
  `discard_transport`/`register_transport`/`track_handle`/`state_for_handle`/`best_live_throughput`/
  `pick_growth_target`/`mark_checked_out`/`last_wave_best_throughput`) — no renames introduced later.
  `callbacks`/`self._callbacks` from Task 3 is used unchanged by Task 5's `_make_instrument` wiring.
