# TODO — FTP/SFTP transfer optimization (branch `perf/ftp-optimization`)

Everything on this list was measured on 2026-08-26 against the real servers with
`ScheduledInvoiceProcessor/scripts/benchmarks/profile_single_transfer.py` and a set of one-off
probes. Item 13 in `TODO.md` (logging) came out of the same session but is unrelated to this branch.

## Measured facts

|                                 | RYO vendor SFTP (Bitvise 9.66)                                                                               | SFT holding FTP (Pure-FTPd)                                                                              |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| round trip                      | 50 ms                                                                                                        | 125 ms                                                                                                   |
| cold connect → usable           | 0.72 s (TCP 0.14, KEX 0.21, auth 0.21, channel 0.17)                                                         | 3.0 s (`PASS` alone 2.55 s); 7 concurrent logins 3.8 s wall                                              |
| parallel lanes per connection   | 10 channels (pool default 4); 10 concurrent `stat`s = 0.105 s                                                | 1                                                                                                        |
| warm cost of one ~1 KB transfer | ~0.49 s: `listdir` checkout probe 0.20, `stat` 0.05, `open` 0.05, `read` 0.07, EOF `read` 0.07, `close` 0.05 | ~0.76 s: checkout `NOOP` 0.13, `TYPE I` 0.13, PASV+connect+STOR 0.39, completion drain 0.13, `SIZE` 0.13 |
| invoice sizes                   | 100 B–3 KB (RYO), 1.3–9.3 KB (SAS); 1 of 120 sampled spans more than one 8 KB chunk                          |                                                                                                          |

Consequences that frame every item below:

- **SFTP is the fast side.** The ~5 s/file seen in `dragrace_ryo.py` was the FTP cold login (3.8 s)
  charged to the pickup stage because pickup runs first; pooling's 7x on `dropoff_files` was
  entirely not re-paying that login.
- **Warmth is all-or-nothing per wave**: one cold lane stalls the wave, so a small pool that never
  goes cold beats a large one that does.
- **The `PASS` stall is out of scope** for this branch (assume the server stays as it is).
- **Rust / a custom SSH implementation is off the table**: per-packet cost is microseconds against
  50 ms round trips, paramiko multiplexes channels in parallel, and its cold handshake is mostly
  unavoidable protocol round trips. What paramiko withholds is request-level pipelining (item 6).
- **PR #19 (prefetch/pipelining + server-side destination stat)** is correct and its verification is
  stronger, but yields no measurable speedup on one-chunk files. Keep it on correctness grounds;
  reword its body before merge.

## Ranked plan (7-file wave)

| #   | change                                                                                                               | saves                                                                                                          | item |
| --- | -------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ---- |
| 1   | Pre-warm windows                                                                                                     | ~3.8 s per cold wave (FTP) + 0.7–1.5 s (SFTP); both checkout probes skippable inside the window (−0.33 s/file) | 7    |
| 2   | Cheaper / skippable checkout probes                                                                                  | 0.20 s/file SFTP, 0.13 s/file FTP                                                                              | 1    |
| 3   | `TYPE I` once per connection                                                                                         | 0.13 s/file                                                                                                    | 2    |
| 4   | Stop paying `stat` + EOF read                                                                                        | 0.07 s/file                                                                                                    | 5    |
| 5   | `channels_per_transport` 4 → 8–10                                                                                    | 0.72 s on cold waves > 4 files (moot once pre-warmed)                                                          | 4    |
| 6   | SFTP request pipelining (thin layer)                                                                                 | SFTP side 0.29 → ~0.10 s/file; a wave in ~3 RTT                                                                | 6    |
| —   | Check `max_connections=16` against Pure-FTPd `MaxClientsPerIP` (default 8) and the sibling programs sharing the host | politeness, not speed                                                                                          | —    |

Item 3 (keep pools warm all the time) is superseded by item 7 for ScheduledInvoiceProcessor's
bursty schedule; it stays for consumers without a schedule and for the keepalive mechanics item 7
reuses.

---

## 1. `SFTPChannelPool` revalidates every idle channel on checkout with a root `listdir` (~200 ms/file)

**Severity:** medium — the single largest fixed cost per small-file transfer on the vendor SFTP path.

**Where:** `src/aeth_ext/ftp/pool/sftp_channel_pool.py`

- `_checkout_idle_or_grow()` line 471 calls `_validate()` on every idle channel it hands out
- `_validate()` line 875 probes with `handle.listdir(".")`

**What's wrong:**

Profiled against the RYO vendor (`ScheduledInvoiceProcessor/scripts/benchmarks/profile_single_transfer.py`):
a plain SFTP round trip (`stat`/`open`/`close`) is ~50 ms, but the checkout probe costs ~200 ms because
`listdir(".")` is a full directory listing of the login root — O(entries), several packets — not a
ping. On a ~1 KB invoice that probe alone is 14% of the 1.43 s per-file total.

The FTP pool does the same thing more cheaply: `ftp_adapter.py` `_checkout_idle_or_grow()` also
probes every idle handle on checkout, but with a `NOOP` (one round trip, ~130 ms against the SFT
server -- the whole of the profile's "acquire FTP session" phase). Both probes are removable during a
pre-warmed window, where keepalive already vouches for liveness (see item 3).

**Fix direction:**

- Probe with `stat(".")` (one packet, ~50 ms) instead of `listdir(".")` — 4x cheaper, same signal.
- Skip the checkout probe entirely for channels released within the last N seconds (the keepalive
  thread already covers long-idle ones); a stale channel then surfaces as a transfer error, which the
  consumer's retry loop (`_transfer_file_vend_to_main`) already handles.
- Skip both pools' checkout probe while a pre-warm hold is active (item 3): keepalive already vouches.
- Either way, bring the two pools' probes into agreement on cost (`NOOP` vs a root listing).

---

## 2. `AdaptedSFTP._sftp_to_ftp` / `AdaptedFTP` transfers re-send `TYPE I` on every pooled connection (~130 ms/file)

**Severity:** low-medium — one wasted FTP control round trip per transfer.

**Where:** `src/aeth_ext/ftp/session.py` — `voidcmd("TYPE I")` at the top of `_sftp_to_ftp` (line ~862),
`_ftp_to_sftp` (line ~463), `_ftp_to_ftp`, `upload_file`, `download_file`.

**What's wrong:**

`TYPE` is sticky for the life of an FTP control connection, and these connections are pooled and
reused. So every transfer after the first on a given handle pays a full control round trip (~130 ms
against the SFT holding server, 9% of the per-file total) to set a mode that is already set.

**Fix direction:**

Send `TYPE I` once when `FTPAdapter` dials a connection (in the connector, before the handle enters
the pool) and drop it from the per-transfer paths. Nothing in this codebase ever switches to ASCII
mode, so there is no state to restore.

---

## 3. Keep both pools warm across scheduled runs (cold wave costs ~3.8 s, every run)

**Severity:** high for wall time — the single largest term in a pickup wave, paid on every cold run.

**Where:**

- `src/aeth_ext/ftp/factory.py` — `keepalive_interval` defaults to `None` on both adapters, so no keepalive
  thread ever starts and idle connections live only until the server's idle timeout reaps them.
- `src/aeth_ext/ftp/pool/sftp_channel_pool.py` line 281 — `_EMPTY_TRANSPORT_TTL = 30.0` closes any
  Transport that has had no checked-out channel for 30 s, independent of keepalive.

**What's wrong:**

Measured against the real servers: a cold SFT FTP connection costs ~3.0 s (Pure-FTPd stalls ~2.5 s on
`PASS`; 7 concurrent logins = 3.8 s wall), a cold vendor SFTP Transport ~0.72 s. ScheduledInvoiceProcessor
runs 24/7 with bursty scheduled waves; between waves the SFTP pool tears itself down after 30 s and the
FTP pool's connections die at the server's idle timeout (Pure-FTPd default 15 min). Any wave further
apart than that is a fully cold wave, so all the pooling work only ever helps *within* one wave.

**Fix direction:**

- Enable keepalive on both pools with an interval safely below each server's idle timeout, and raise
  `_EMPTY_TRANSPORT_TTL` (or make it configurable) so SFTP Transports survive between waves.
- Keepalive traffic must be one cheap packet per connection: FTP `NOOP` already is; the SFTP probe is
  currently `listdir(".")` (item 1) and must become `stat(".")` or, better, an SSH-level
  `Transport.set_keepalive()` global request, which costs nothing at the SFTP layer.
- `_keepalive_check_one` probes **one** idle connection per tick, so with N idle connections the
  effective per-connection cadence is N × interval — size the interval against that, or probe all idle
  connections per tick.
- Confirm the server idle timeouts before choosing the interval (an idle `NOOP` at increasing gaps,
  read-only, finds Pure-FTPd's; Bitvise's is in its settings and typically generous).

---

## 4. `channels_per_transport` defaults to 4; the vendor allows 10

**Severity:** low-medium — one extra serial 0.72 s Transport dial on any cold wave larger than 4 files.

**Where:** `src/aeth_ext/ftp/factory.py` — `channels_per_transport: int = 4`; consumers don't override it.

**What's wrong:**

Measured: Bitvise (RYO) accepts 10 SFTP channels on one Transport and refuses the 11th; 10 concurrent
`stat`s across 10 channels complete in 0.105 s, so paramiko multiplexes them genuinely in parallel. At
4 per Transport a 7-file wave needs a second Transport, dialed serially under `_dial_lock`
(`sftp_channel_pool.py` line ~415). The pool already learns a lower cap from refusals
(`TransportState.channel_cap`), so a higher default is safe against stricter servers.

**Fix direction:** default to 8–10, or probe upward the way `channel_cap` already probes downward.

---

## 5. `_sftp_to_ftp` / `_sftp_to_sftp` pay for both a `stat` and an EOF `read` (~70 ms/file)

**Severity:** low — one wasted SFTP round trip per file.

**Where:** `src/aeth_ext/ftp/session.py` — the `while data := source_file.read(self.chunk_size)` loops.

**What's wrong:**

The loop needs an empty read to terminate. Once the prefetch buffer is drained, paramiko's
`SFTPFile._read` issues a real `SSH_FXP_READ` and waits for the server's EOF status — a full round trip
spent learning a length the `stat` just above already returned. Profiled: `SFTPFile.read` is called
twice per one-chunk file, ~70 ms each.

**Fix direction:** when `source_file_size` is known, stop after `source_file_size` bytes instead of
reading to EOF (or read `min(chunk, remaining)`); keep the EOF loop only for the unknown-size path.

---

## 6. SFTP request pipelining across operations and across files (the SFTP-side ceiling)

**Severity:** medium — the difference between ~290 ms/file after items 1/5 and ~100 ms/file; and
between "N files in parallel across channels" and "N files in ~3 round trips on one channel".

**Where:** `src/aeth_ext/ftp/session.py` (`AdaptedSFTP` transfer paths); new code beside `SFTPClient`.

**What's wrong:**

paramiko's `SFTPClient` is one-request-at-a-time per channel; `prefetch`/`set_pipelined` are the only
exceptions and only pipeline *within* one file's reads or writes. The SFTP protocol itself allows any
number of outstanding requests matched by id: `open` for N files can all be in flight, then N
`read`+`close` pairs back-to-back — a whole wave in ~2–3 RTT on a single channel. This is an API-shape
limit in paramiko, not a speed limit: per-packet Python cost is ~µs against a 50 ms RTT, and 10-channel
parallelism measured as genuinely parallel, so a faster *implementation* (Rust, etc.) would not help;
only a smarter request pattern would.

**Fix direction:**

- Cheapest: a thin request/response layer over paramiko's own `Channel` speaking SFTP v3 directly
  (~10 message types, 32-bit request ids) — keeps Transport, auth, host keys, and the channel pool
  untouched; used only by the bulk small-file transfer paths.
- Alternative: asyncssh, whose coroutine API pipelines naturally — but it replaces the Transport the
  pool is built around, so the migration cost lands in the pool.
- Not worth it until items 1, 3, 4, 5 have landed and the wave is re-profiled; those are cheaper
  and together remove more time.

---

## 7. Pre-warm windows: `prewarm(count=None)` + keepalive until an Event or a deadline, then cool down

**Severity:** the headline item — removes the cold-wave cost entirely while staying polite to servers
that are shared with sibling programs.

**Where:** new public API on `FTPAdapter` / `SFTPAdapter` (shared machinery in `pool/base.py`);
ScheduledInvoiceProcessor schedules it.

**Why this shape:** ScheduledInvoiceProcessor runs 24/7 but transfers in short scheduled bursts
(~1% of the time). It knows the schedule, so it can warm the pool ~5 min before a wave and release it
right after, instead of holding server slots around the clock. That resolves the connection-slot
politeness problem (Pure-FTPd `MaxClientsPerIP`, sibling programs on the same host) by construction.

**Decided:**

- `prewarm(count: int | None = None, ...)` — `count` defaults to `max_connections`, clamped to the
  pool's ceiling (including any discovered server-side cap).
- Cool-down **closes every idle connection/channel immediately** and stops keepalive. Handles still
  checked out return to idle afterwards and linger as today; in practice nothing is checked out when
  the consumer signals.

**Proposed (not yet decided):**

- The hold ends on **either** an Event **or** a deadline, and the deadline always exists as a safety
  ceiling (default on the order of 30 min) so a crashed job can never leave connections warm forever;
  the Event is the early release. Accept `threading.Event` or `aiologic.Event`.
- Keepalive during the hold: SFTP via paramiko's `Transport.set_keepalive()` (SSH-level; a dead
  Transport is detected passively through `is_active()`, no SFTP traffic, nothing for the server to
  log); FTP via a `NOOP` sweep of **all** idle handles per tick (the current loop probes one per tick).
  Interval default ~60 s — under any plausible NAT/firewall idle timeout; measurable if needed.
- Overlapping holds (`waiting_ftp` is shared by every supplier processor): the pool stays warm while
  **any** hold is active; the warm count is the max requested.
- `prewarm` blocks, dials in parallel (the SFTP pool's `_dial_lock` serializes cold-start dials on
  purpose; a pre-warm knows its target and can dial `ceil(count / channels_per_transport)` Transports
  at once), and returns the number actually warmed; raises only if it could open nothing.
- While a hold is active, **skip the checkout probe** in both pools (item 1): keepalive vouches.
- Shutdown teardown ends any hold; a hold never blocks shutdown.
- Consumer side (ScheduledInvoiceProcessor): one `CronTrigger` job per transfer job, offset −5 min,
  calling `prewarm` via `to_thread`; the transfer job sets the Event when its wave completes.

**Open:** whether the hold is returned as an object (`.cool()`, context-manager) or is purely
Event/deadline-driven; exact interval defaults.
