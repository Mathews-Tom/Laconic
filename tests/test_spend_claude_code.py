"""Claude Code transcripts load as joinable, non-duplicating priced turns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from laconic.spend.claude_code import load_session, load_sessions

SESSION = "9568e5dc-ccf9-4a6c-99ca-1ae8343660cd"


def _turn(session: str, *, cache_read: int = 1000, cache_write: int = 100) -> dict[str, Any]:
    return {
        "sessionId": session,
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": 50,
                "service_tier": "standard",
                "cache_creation": {"ephemeral_1h_input_tokens": cache_write},
            },
        },
    }


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_subagent_transcripts_merge_into_the_session_they_belong_to(tmp_path: Path) -> None:
    """Claude Code gives a subagent its *parent's* session id.

    One session routinely spans several files. Emitting one row per file
    would make several rows claim the same join key, and joining each
    against the same runtime ledger would multiply every codec counter
    derived from it -- the join refuses outright rather than double-count.
    The subagent's tokens are real spend for that session, so they are
    summed in rather than dropped.
    """
    project = tmp_path / "projects" / "-Users-x-proj"
    _write(project / f"{SESSION}.jsonl", [_turn(SESSION)])
    _write(project / "subagents" / "a.jsonl", [_turn(SESSION), _turn(SESSION)])
    _write(project / "subagents" / "b.jsonl", [_turn(SESSION)])

    sessions = load_sessions([tmp_path])

    assert len(sessions) == 1, "four transcripts, one session id, one joinable row"
    assert sessions[0].session_id == SESSION
    assert len(sessions[0].turns) == 4
    assert sum(turn.cache_read for turn in sessions[0].turns) == 4000


def test_distinct_sessions_stay_distinct(tmp_path: Path) -> None:
    other = "32f56d6d-5d56-4c02-b9dd-13a486e780e3"
    project = tmp_path / "projects" / "-Users-x-proj"
    _write(project / f"{SESSION}.jsonl", [_turn(SESSION)])
    _write(project / f"{other}.jsonl", [_turn(other)])

    assert {session.session_id for session in load_sessions([tmp_path])} == {SESSION, other}


def test_a_turn_carries_no_host_cost_because_claude_code_reports_none(tmp_path: Path) -> None:
    """The difference that would otherwise corrupt the host-reported total.

    OMP records a per-turn `costUSD`; Claude Code records token counters
    only. Reporting zero here is correct, and the report counts such
    sessions separately so a partial host total never reads as a whole one.
    """
    path = tmp_path / f"{SESSION}.jsonl"
    _write(path, [_turn(SESSION)])

    session = load_session(path)

    assert session is not None
    assert all(turn.host_cost_usd == 0.0 for turn in session.turns)
    assert sum(turn.cache_write for turn in session.turns) == 100


def test_an_unmodelled_usage_key_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    """Host schema drift must stay visible.

    A reader that silently skips what it does not understand reports a
    smaller number with the same confidence as a complete one.
    """
    record = _turn(SESSION)
    record["message"]["usage"]["speed"] = "fast"
    path = tmp_path / f"{SESSION}.jsonl"
    _write(path, [record])

    session = load_session(path)

    assert session is not None
    assert "speed" in session.unknown_usage_keys


def test_a_transcript_with_no_session_id_is_not_a_joinable_session(tmp_path: Path) -> None:
    path = tmp_path / "orphan.jsonl"
    _write(path, [{"type": "summary", "summary": "no session id here"}])

    assert load_session(path) is None
    assert load_sessions([tmp_path]) == []


def test_a_torn_final_line_is_tolerated(tmp_path: Path) -> None:
    """A live session's last line is routinely a partial write."""
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text(json.dumps(_turn(SESSION)) + "\n" + '{"sessionId": "trunc', encoding="utf-8")

    session = load_session(path)

    assert session is not None
    assert len(session.turns) == 1
