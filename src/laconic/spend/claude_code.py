"""Read Claude Code session transcripts as priced turns.

The codec runs on Claude Code as well as OMP, so a spend report that scans
only OMP's sessions undercounts its own coverage and silently omits half
the hosts it supports. This module supplies the same
:class:`~laconic.spend.omp.SessionUsage` shape from Claude Code's own
transcript format, so the join, the report, and the privacy gate stay
single-sourced.

Two differences from OMP's transcripts are load-bearing.

**Claude Code reports no cost.** OMP records a ``costUSD`` per turn, which
is what the report calls the "host-reported" figure and uses as an
independent check on Laconic's own pricing model. Claude Code records token
counters only. Turns loaded here therefore carry ``host_cost_usd = 0.0``
and set :attr:`SessionUsage.reports_host_cost` to ``False``, so the report
can say that its host-reported total covers only part of the corpus rather
than quietly presenting a partial sum as a whole one. The flag is a
capability of the host, not a property of the number: an OMP session whose
turns genuinely price to zero still reports cost.

**Cache writes are split by lifetime.** Claude Code bills a 5-minute and a
1-hour ephemeral cache at different multipliers. ``laconic.costs`` models a
single cache-write rate, so a 1-hour write is priced low here. That is a
known understatement of modelled cost, recorded rather than silently
corrected, because correcting it would mean this reader and the OMP reader
no longer agree on what a cache-write token costs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

from laconic.spend.omp import MalformedSessionError, SessionUsage, TurnUsage

#: Claude Code files one directory per project and one transcript per
#: session inside it.
TRANSCRIPT_GLOB: Final = "*.jsonl"

#: Where Claude Code keeps its session transcripts.
DEFAULT_SESSION_DIR: Final = Path.home() / ".claude" / "projects"

#: The usage keys this reader models. Anything else is reported by name
#: rather than ignored, so host schema drift stays visible.
_MODELLED_USAGE_KEYS: Final = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)

#: Usage keys that are structure or metadata rather than billable counters.
#: Enumerated so they do not show up as unmodelled drift on every session.
_IGNORED_USAGE_KEYS: Final = frozenset(
    {
        "cache_creation",
        "inference_geo",
        "iterations",
        "output_tokens_details",
        "server_tool_use",
        "service_tier",
    }
)


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MalformedSessionError(f"usage counter must be a non-negative integer: {value!r}")
    return value


def _iter_records(path: Path) -> Iterator[Any]:
    """Yield each parseable record, tolerating a torn final line.

    A live session's last line is routinely a partial write. Mid-file
    garbage is a real defect and is not tolerated.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                return
            raise MalformedSessionError(f"unparseable record in {path.name}") from error


def load_session(path: Path) -> SessionUsage | None:
    """Read one transcript, or ``None`` if it carries no session id.

    A transcript with no ``sessionId`` cannot be joined to a runtime ledger
    and is not a session this report can say anything about.
    """
    session_id = ""
    turns: list[TurnUsage] = []
    turns_without_usage = 0
    unknown: set[str] = set()
    for record in _iter_records(path):
        if not isinstance(record, dict):
            continue
        if not session_id and isinstance(record.get("sessionId"), str):
            session_id = record["sessionId"]
        message = record.get("message")
        if not isinstance(message, dict) or record.get("type") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            turns_without_usage += 1
            continue
        unknown |= set(usage) - _MODELLED_USAGE_KEYS - _IGNORED_USAGE_KEYS
        model = message.get("model")
        turns.append(
            TurnUsage(
                model=model if isinstance(model, str) and model else "unknown",
                provider="anthropic",
                input_tokens=_token_count(usage.get("input_tokens", 0)),
                cache_read=_token_count(usage.get("cache_read_input_tokens", 0)),
                cache_write=_token_count(usage.get("cache_creation_input_tokens", 0)),
                output_tokens=_token_count(usage.get("output_tokens", 0)),
                # Claude Code reports no per-turn cost. See the module
                # docstring: this is counted, not silently treated as free.
                host_cost_usd=0.0,
            )
        )
    if not session_id:
        return None
    return SessionUsage(
        session_id=session_id,
        turns=tuple(turns),
        turns_without_usage=turns_without_usage,
        malformed_lines=0,
        unknown_usage_keys=frozenset(unknown),
        nested=False,
        reports_host_cost=False,
    )


def find_transcripts(roots: Sequence[Path]) -> list[Path]:
    """Return every Claude Code transcript under ``roots``."""
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            found.add(root)
        elif root.is_dir():
            found.update(path for path in root.rglob(TRANSCRIPT_GLOB) if path.is_file())
    return sorted(found)


def load_sessions(roots: Sequence[Path]) -> list[SessionUsage]:
    """Read every Claude Code transcript under ``roots``, one row per session.

    Claude Code files a subagent's transcript under a ``subagents``
    directory but gives it **the parent's** ``sessionId``, so one session
    routinely spans several files -- one observed session here spans
    nineteen. This differs from OMP, where a subagent receives a session id
    of its own and is reported as its own nested row.

    Those files are therefore merged into one row rather than emitted
    separately. Emitting them separately would make several rows claim the
    same join key, and joining each against the same runtime ledger would
    multiply every codec counter derived from it. The subagent's tokens are
    still real spend for that session, so they are summed in rather than
    dropped -- and the codec's hook reports a subagent's tool calls under
    the same session id, so both sides of the join agree on the boundary.
    """
    merged: dict[str, SessionUsage] = {}
    for path in find_transcripts(roots):
        session = load_session(path)
        if session is None:
            continue
        existing = merged.get(session.session_id)
        if existing is None:
            merged[session.session_id] = session
            continue
        merged[session.session_id] = SessionUsage(
            session_id=existing.session_id,
            turns=existing.turns + session.turns,
            turns_without_usage=existing.turns_without_usage + session.turns_without_usage,
            malformed_lines=existing.malformed_lines + session.malformed_lines,
            unknown_usage_keys=existing.unknown_usage_keys | session.unknown_usage_keys,
            nested=False,
            reports_host_cost=False,
        )
    return [merged[key] for key in sorted(merged)]
