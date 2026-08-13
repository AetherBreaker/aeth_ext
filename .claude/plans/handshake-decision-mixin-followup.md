# Follow-up: unify handshake-decision logic across the three drainers

**Status:** not started. Filed after ultrareview finding on PR #11
(https://github.com/AetherBreaker/aeth_ext/pull/11#discussion_r3762405866) surfaced that
`HandshakeSocketHandler._send_handshake` skipped starting the apply-result watcher on the
D-E7 ack-read-failure path, while `AsyncioQueueDrainer.run` and `ThreadedQueueDrainer._connect`
both got it right. Fixed directly for now (inline duplicate thread-start); this note is the
deferred structural fix so the same class of drift doesn't recur.

## What's actually duplicated vs. what's genuinely different

`HandshakeSocketHandler._send_handshake`, `AsyncioQueueDrainer.run`, and
`ThreadedQueueDrainer._connect` each reimplement the same **decision sequence** after sending
the handshake:

1. read ack (via the already-shared `read_server_message_sync`/`read_server_message_async`)
2. if rejected (`HandshakeAck(ok=False)`) → log critical, `trigger_shutdown`, close socket
3. if read failed (not a `HandshakeAck`) → log warning, `alert(...)` with the shared
   `_ACK_READ_FAILURE_REASON` message, treat ack as `None`
4. replay backlog (own `_replay_backlog` per class, same shape modulo sync/async)
5. **postcondition: if the socket is still live, an apply-result watcher must be watching it**

Steps 2, 3, and the postcondition in step 5 are duplicated three times with near-identical
code and no mechanical reason for the duplication — this is where drift happened and can
happen again.

What's *not* duplicated for the right reasons (a mixin must not try to unify these): the I/O
primitives per drainer are genuinely different execution models —
`socket.sendall`/`threading.Thread` (sync), `StreamWriter.drain`/`asyncio.ensure_future` (async
task), and `sock.sendall`/`select()`-based polling reusing the existing 0.5s queue-poll cadence
(threaded, deliberately avoids spawning a watcher thread). Forcing these into one shared
concrete implementation would either break the threaded drainer's poll-reuse optimization or
require `isinstance` branching inside the mixin — worse than the current duplication.

## Shape: Template Method, not a classic mixin

A plain mixin (shared concrete methods) doesn't fit because method signatures differ (`socket`
vs `StreamWriter`/`StreamReader`, sync vs coroutine). Instead:

- A mixin/base owns the **decision sequence** (steps 2–3 plus the "live socket ⇒ watched"
  postcondition) as a single method.
- Each drainer implements a small set of injected primitives the mixin calls into:
  `_do_read_ack(...)`, `_on_rejected(...)`, `_on_read_failed(...)`, `_start_watcher(...)`.
- Sync and async variants likely still need separate mixins (`_SyncHandshakeMixin`,
  `_AsyncHandshakeMixin`) since `await` can't be abstracted away generically — but each removes
  duplication *within* its own concurrency model, and the decision policy text (log messages,
  `_ACK_READ_FAILURE_REASON`, the postcondition itself) lives in exactly one place per model
  instead of two-or-three.

## Why deferred rather than folded into PR #11

- Higher blast radius: touches all three drainers' hot paths at once, on a branch already
  mid-refactor of this exact code (two-phase logging config).
- Concurrency models here are subtle (sync-blocking, asyncio, thread+select); a template-method
  extraction is easy to get subtly wrong in ways tests won't catch (timing/cancellation edges).
- The immediate bug has a low-risk, one-branch fix (done inline in `_send_handshake`) that
  unblocks review now; the structural fix deserves its own PR judged on its own risk/benefit.

## When to pick this up

Next time this file's drainers need a coordinated behavior change (e.g. the D-E6/D-E7 policy
itself changes, or a fourth drainer variant is added), do the extraction then — don't do it
speculatively ahead of a concrete second driver for the change.
