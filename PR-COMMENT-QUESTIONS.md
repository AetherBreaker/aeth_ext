# PR #14 review comment — needs your input

## `sftp_pool.py:66` — `_ChannelConnector` Protocol vs. concrete `_SFTPConnector`

Your comment:

> By the point in execution where we're using an SFTPPool, we already know what connector we're
> using: an `_SFTPConnector`. We should not be using a protocol here, we should be using the actual
> connector for typing.

I tried switching `SFTPChannelPool.__init__`'s `connector` parameter from `_ChannelConnector`
(the local `Protocol`) to the real `_SFTPConnector` class (imported under `TYPE_CHECKING` to dodge
the runtime cycle: `adapter.py` imports `sftp_pool.py` at module load, so a real import the other
way would be circular). That change made `pyright` fail on two rules this project has deliberately
turned on in `pyproject.toml`:

- `reportImportCycles = true` — pyright still flags a `TYPE_CHECKING`-only import as a cycle, since
  it's a cycle in the type graph even though not in the runtime one.
- `reportPrivateUsage = true` — `_SFTPConnector` is underscore-prefixed (module-private to
  `adapter.py`), and `sftp_pool.py` reaching into it trips this rule.

This is exactly the tradeoff the module's own docstring already calls out (`sftp_pool.py:59-61`):
the `Protocol` exists specifically *because* importing the concrete class would cycle back through
`adapter.py`'s real import of `sftp_pool.py`. So I reverted the change rather than force it through
lint suppressions.

Options, if you still want this addressed:

1. **Leave the `Protocol` as-is.** It's already structurally identical to `_SFTPConnector`'s public
   shape (`request_handler`/`close_conn_handler`), so nothing is lost at the type level — a mismatch
   between the two would still be caught. This is what I left in place.
2. **Rename `_SFTPConnector` to drop the leading underscore** (e.g. `SFTPConnector`), making it
   importable under `TYPE_CHECKING` without a `reportPrivateUsage` complaint. Cycle would still need
   a `# noqa`/pyright suppression for that one import line, or a `pyproject.toml` per-file ignore.
3. **Move `_SFTPConnector` (or an extracted shape it satisfies) into `types.py`** alongside
   `HandleProvider`/`TransportProvider`, so both `adapter.py` and `sftp_pool.py` import it from a
   third module with no cycle at all. Bigger change, but the cleanest fix for the *shape* of the
   problem, not just this one edge.

I didn't want to guess which tradeoff you'd prefer, so this one comment is still open — the resolved GitHub thread status is otherwise up to date for all other comments.
