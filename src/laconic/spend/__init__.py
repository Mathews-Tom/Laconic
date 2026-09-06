"""Local, read-only measurement of where a coding agent's model spend goes.

This package answers one question and refuses a second one. It answers
*composition*: of the money a real session actually cost, how much went to
uncached input, cache reads, cache writes, and output, and what the runtime
codec did in that same session. It does not answer *savings*: the sessions
it reads were all recorded with the codec enabled, so there is no
counterfactual anywhere in the data and no analysis of it can produce one.

Nothing here contacts a provider, and nothing here reads prompt text, tool
arguments, tool results, file contents, or the working directory of a
session. The host's session transcripts are opened read-only and never
modified.

The runtime store is read through :func:`laconic.runtime.operator.open_query_only`,
which refuses every write statement. One qualification, stated rather than
glossed: if a ledger's writer was killed mid-transaction, SQLite must roll
that writer's own aborted transaction back before anything can be read, and
that rollback touches the file. It is the recovery of a crashed engine's
incomplete write, not a change this package makes, and it is the same
recovery `laconic status` performs. No row, table, or counter is ever added,
altered, or removed here, and no ledger is ever created.
"""
