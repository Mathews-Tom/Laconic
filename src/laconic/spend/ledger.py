"""Read the runtime store's per-session decision counters, read-only.

`laconic.runtime.operator` already aggregates the whole store into one
:class:`~laconic.runtime.operator.RuntimeStorageStatus`. That is the right
shape for `laconic status` and the wrong shape here: joining spend to codec
activity needs the counters *per session id*, because the session id is the
only key the host transcript and the ledger share.

This module therefore issues its own grouped query, but opens and enumerates
through :func:`laconic.runtime.operator.open_query_only` and
:func:`laconic.runtime.operator.owned_ledger_files` rather than reimplementing
either. Those two carry the hot-journal recovery and the symlink/escape
checks that the operator surface depends on; a private second copy here would
be free to regress on its own.

Nothing in this module writes. The owner's store is live while it runs.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from laconic.runtime.operator import open_query_only, owned_ledger_files
from laconic.runtime.storage import resolve_data_dir


@dataclass(frozen=True, slots=True)
class SessionDecisions:
    """What the codec did in one session, as its ledger recorded it.

    Attributes:
        session_id: The host session this ledger belongs to. The join key.
        eligible: Decisions recorded, one per observation the adapter
            offered the engine.
        emitted: Decisions where a smaller envelope replaced the raw result.
        raw_chars: Characters the tool actually produced, across every
            decision.
        visible_chars: Characters the model actually saw instead. Equal to
            ``raw_chars`` for a pass-through, smaller for an emission.
        full_expansions: Times a reference was expanded in full afterwards.
        span_expansions: Times a line span was expanded afterwards.
    """

    session_id: str
    eligible: int
    emitted: int
    raw_chars: int
    visible_chars: int
    full_expansions: int
    span_expansions: int

    @property
    def pass_through(self) -> int:
        """Decisions that left the raw result in place."""
        return self.eligible - self.emitted

    @property
    def chars_avoided(self) -> int:
        """Characters the model did not see because an envelope replaced them.

        A character count, and only that. It is not a token count, and the
        sessions this is measured over have no codec-off counterpart, so it
        is not a saving either.
        """
        return self.raw_chars - self.visible_chars


@dataclass(frozen=True, slots=True)
class StoreScan:
    """Every readable session in the store, plus what was not readable."""

    sessions: tuple[SessionDecisions, ...]
    damaged_ledgers: int
    """Ledgers with no schema, which a killed initialize leaves behind."""


def _is_missing_schema(error: sqlite3.Error) -> bool:
    # Mirrors laconic.runtime.operator: only an absent table is damage. A
    # locked database, an I/O error, or a malformed image may still hold real
    # evidence and must reach the caller instead of being counted as damage.
    return isinstance(error, sqlite3.OperationalError) and "no such table" in str(error)


def _read_one(path: Path) -> tuple[SessionDecisions, ...] | None:
    """Return one ledger's per-session rows, or ``None`` if its schema is gone."""
    try:
        with closing(open_query_only(path)) as database:
            decisions = database.execute(
                "SELECT session_id, count(*), "
                "coalesce(sum(CASE WHEN outcome = 'emitted' THEN 1 ELSE 0 END), 0), "
                "coalesce(sum(raw_chars), 0), coalesce(sum(visible_chars), 0) "
                "FROM runtime_decisions GROUP BY session_id"
            ).fetchall()
            expansions = database.execute(
                "SELECT session_id, "
                "coalesce(sum(CASE WHEN span = 0 THEN 1 ELSE 0 END), 0), "
                "coalesce(sum(CASE WHEN span = 1 THEN 1 ELSE 0 END), 0) "
                "FROM runtime_expansions GROUP BY session_id"
            ).fetchall()
    except sqlite3.Error as error:
        if _is_missing_schema(error):
            return None
        raise

    expanded = {row[0]: (int(row[1]), int(row[2])) for row in expansions}
    rows = {
        str(row[0]): SessionDecisions(
            session_id=str(row[0]),
            eligible=int(row[1]),
            emitted=int(row[2]),
            raw_chars=int(row[3]),
            visible_chars=int(row[4]),
            full_expansions=expanded.get(row[0], (0, 0))[0],
            span_expansions=expanded.get(row[0], (0, 0))[1],
        )
        for row in decisions
    }
    # A session that recorded an expansion but no decision cannot happen
    # through the engine, but the ledger schema permits it; representing it
    # as a zero-decision session is honest, dropping it is not.
    for session_id, (full, span) in expanded.items():
        if str(session_id) not in rows:
            rows[str(session_id)] = SessionDecisions(
                session_id=str(session_id),
                eligible=0,
                emitted=0,
                raw_chars=0,
                visible_chars=0,
                full_expansions=full,
                span_expansions=span,
            )
    return tuple(rows[key] for key in sorted(rows))


def scan_store(data_dir: Path | None = None) -> StoreScan:
    """Read every owned ledger in the runtime store without writing to it."""
    root = resolve_data_dir(data_dir)
    sessions: list[SessionDecisions] = []
    damaged = 0
    for ledger in owned_ledger_files(root):
        rows = _read_one(ledger)
        if rows is None:
            damaged += 1
            continue
        sessions.extend(rows)
    return StoreScan(
        sessions=tuple(sorted(sessions, key=lambda row: row.session_id)),
        damaged_ledgers=damaged,
    )
