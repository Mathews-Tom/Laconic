"""The OMP session-usage reader refuses to guess."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from laconic.spend.omp import (
    MalformedSessionError,
    find_transcripts,
    load_session,
    load_sessions,
)

SESSION_ID = "01a078c0-15c2-7000-9389-19db9037f833"


def _usage(
    *,
    input_tokens: int = 4,
    output: int = 218,
    cache_read: int = 0,
    cache_write: int = 87173,
    total: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    components = {
        "input": input_tokens * 5.0 / 1e6,
        "output": output * 25.0 / 1e6,
        "cacheRead": cache_read * 0.5 / 1e6,
        "cacheWrite": cache_write * 6.25 / 1e6,
    }
    usage: dict[str, Any] = {
        "input": input_tokens,
        "output": output,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": (
            total if total is not None else input_tokens + output + cache_read + cache_write
        ),
        "cost": {**components, "total": sum(components.values())},
        "cttl": {"ephemeral5m": cache_write},
    }
    if extra:
        usage.update(extra)
    return usage


def _assistant(usage: dict[str, Any] | None, *, model: str = "claude-opus-5") -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "model": model,
        "provider": "anthropic",
        "content": [{"type": "text", "text": "SECRET ASSISTANT PROSE"}],
    }
    if usage is not None:
        message["usage"] = usage
    return {"type": "message", "id": "m1", "message": message}


def _transcript(records: list[dict[str, Any]], path: Path, *, truncate: bool = False) -> Path:
    body = "\n".join(json.dumps(record) for record in records)
    if truncate:
        body += '\n{"type": "message", "message": {"role": "assi'
    path.write_text(body + ("" if truncate else "\n"), encoding="utf-8")
    return path


def _session_record() -> dict[str, Any]:
    return {"type": "session", "version": 3, "id": SESSION_ID, "cwd": "/Users/owner/secret/repo"}


def test_reads_per_turn_counters_and_the_hosts_own_cost(tmp_path: Path) -> None:
    path = _transcript(
        [
            {"type": "title", "title": "SECRET TITLE"},
            _session_record(),
            {"type": "message", "message": {"role": "user", "content": "SECRET PROMPT"}},
            _assistant(_usage()),
            _assistant(_usage(input_tokens=2, output=320, cache_read=87173, cache_write=967)),
        ],
        tmp_path / "s.jsonl",
    )

    session = load_session(path)

    assert session.session_id == SESSION_ID
    assert len(session.turns) == 2
    first, second = session.turns
    assert (first.input_tokens, first.output_tokens) == (4, 218)
    assert (first.cache_read, first.cache_write) == (0, 87173)
    assert first.total_tokens == 87395
    # Per-turn, not cumulative: turn two re-reads what turn one wrote.
    assert second.cache_read == 87173
    assert second.total_tokens == 88462
    assert first.host_cost_usd == pytest.approx(0.55030125)
    assert first.model == "claude-opus-5"
    assert first.provider == "anthropic"


def test_a_live_sessions_partial_last_line_is_counted_not_fatal(tmp_path: Path) -> None:
    path = _transcript(
        [_session_record(), _assistant(_usage())], tmp_path / "s.jsonl", truncate=True
    )

    session = load_session(path)

    assert session.malformed_lines == 1
    assert len(session.turns) == 1


def test_a_partial_last_line_followed_by_a_blank_line_is_still_the_last_line(
    tmp_path: Path,
) -> None:
    # A live session's final write can land a partial record and a newline.
    # Counting the blank line as a record would turn the tolerated case into
    # a whole-corpus failure.
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps(_session_record())
        + "\n"
        + json.dumps(_assistant(_usage()))
        + '\n{"type": "message", "message": {"role": "assi\n\n',
        encoding="utf-8",
    )

    session = load_session(path)

    assert session.malformed_lines == 1
    assert len(session.turns) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cost_is_rejected_rather_than_silently_totalled(
    tmp_path: Path, value: float
) -> None:
    # json.loads accepts bare NaN/Infinity, and NaN satisfies the cost
    # identity for free because every comparison with it is False.
    usage = _usage()
    usage["cost"]["cacheWrite"] = value
    usage["cost"]["total"] = value
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps(_session_record()) + "\n" + json.dumps(_assistant(usage), allow_nan=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MalformedSessionError, match="not a finite number"):
        load_session(path)


def test_an_unparseable_line_before_the_end_is_a_damaged_file(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps(_session_record()) + "\n{ broken\n" + json.dumps(_assistant(_usage())) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MalformedSessionError, match="before the end of the file"):
        load_session(path)


def test_a_session_with_no_priced_turns_reads_as_empty_not_as_a_failure(tmp_path: Path) -> None:
    path = _transcript(
        [
            _session_record(),
            {"type": "message", "message": {"role": "user", "content": "SECRET"}},
            {"type": "message", "message": {"role": "toolResult", "content": "SECRET"}},
        ],
        tmp_path / "s.jsonl",
    )

    session = load_session(path)

    assert session.turns == ()
    assert session.turns_without_usage == 0


def test_an_assistant_turn_carrying_no_usage_is_counted_separately(tmp_path: Path) -> None:
    path = _transcript([_session_record(), _assistant(None)], tmp_path / "s.jsonl")

    session = load_session(path)

    assert session.turns == ()
    assert session.turns_without_usage == 1


def test_a_file_with_no_session_record_is_not_an_omp_transcript(tmp_path: Path) -> None:
    path = _transcript([_assistant(_usage())], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="no session record"):
        load_session(path)


@pytest.mark.parametrize("missing", ["input", "output", "cacheRead", "cacheWrite", "totalTokens"])
def test_a_missing_token_counter_raises_instead_of_counting_zero(
    tmp_path: Path, missing: str
) -> None:
    usage = _usage()
    del usage[missing]
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match=f"usage.{missing} is not an integer"):
        load_session(path)


def test_a_boolean_counter_is_not_silently_coerced_to_one(tmp_path: Path) -> None:
    usage = _usage()
    usage["cacheWrite"] = True
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="usage.cacheWrite is not an integer"):
        load_session(path)


def test_an_unexpected_billable_component_breaks_the_token_identity(tmp_path: Path) -> None:
    # A host that starts billing a fifth token class reports it inside
    # totalTokens. Summing the four this reader knows would understate it.
    usage = _usage(total=87395 + 500, extra={"audioTokens": 500})
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="billing a token class this reader does not"):
        load_session(path)


def test_an_unexpected_non_billable_key_is_reported_but_does_not_fail(tmp_path: Path) -> None:
    # reasoningTokens is real and already inside output; a comparable new
    # breakdown key must stay visible without stopping the read.
    usage = _usage(extra={"speculativeTokens": 12})
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    session = load_session(path)

    assert session.unknown_usage_keys == frozenset({"speculativeTokens"})
    assert len(session.turns) == 1


def test_reasoning_tokens_are_not_reported_as_drift(tmp_path: Path) -> None:
    path = _transcript(
        [_session_record(), _assistant(_usage(extra={"reasoningTokens": 96}))],
        tmp_path / "s.jsonl",
    )

    assert load_session(path).unknown_usage_keys == frozenset()


def test_a_cost_object_that_does_not_add_up_is_rejected(tmp_path: Path) -> None:
    usage = _usage()
    usage["cost"]["total"] = 99.0
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="usage.cost.total is 99.0"):
        load_session(path)


def test_a_missing_cost_object_is_rejected(tmp_path: Path) -> None:
    usage = _usage()
    del usage["cost"]
    path = _transcript([_session_record(), _assistant(usage)], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="usage.cost is missing"):
        load_session(path)


def test_an_unidentified_model_or_provider_is_rejected(tmp_path: Path) -> None:
    record = _assistant(_usage())
    del record["message"]["model"]
    path = _transcript([_session_record(), record], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="no model identifier"):
        load_session(path)


def test_two_conflicting_session_ids_in_one_file_are_rejected(tmp_path: Path) -> None:
    other = _session_record() | {"id": "01a06d0f-6060-7000-94fc-3301f336bf8b"}
    path = _transcript([_session_record(), other], tmp_path / "s.jsonl")

    with pytest.raises(MalformedSessionError, match="two different session ids"):
        load_session(path)


def test_a_diagnostic_names_the_file_but_never_its_directory(tmp_path: Path) -> None:
    secret = tmp_path / "WorkSpace-private-client"
    secret.mkdir()
    usage = _usage()
    del usage["input"]
    filename = f"2026-09-06T22-04-09-538Z_{SESSION_ID}.jsonl"
    path = _transcript([_session_record(), _assistant(usage)], secret / filename)

    with pytest.raises(MalformedSessionError) as caught:
        load_session(path)

    assert "WorkSpace-private-client" not in str(caught.value)
    assert filename in str(caught.value)
    # OMP puts the session id in the filename, so this message carries one.
    # That is why MalformedSessionError documents itself as terminal-only and
    # why nothing serializes it.
    assert SESSION_ID in str(caught.value)


def test_nothing_the_reader_keeps_carries_session_content(tmp_path: Path) -> None:
    path = _transcript(
        [
            {"type": "title", "title": "SECRET TITLE"},
            _session_record(),
            {"type": "message", "message": {"role": "user", "content": "SECRET PROMPT"}},
            _assistant(_usage()),
        ],
        tmp_path / "s.jsonl",
    )

    rendered = repr(load_session(path))

    for leaked in ("SECRET", "/Users/owner", "secret/repo"):
        assert leaked not in rendered


def test_a_subagents_transcript_is_marked_nested_at_any_depth(tmp_path: Path) -> None:
    project = tmp_path / "project"
    child = project / "2026-09-04T15-10-38-880Z_root-session"
    grandchild = child / "LitFrontierIdeas"
    grandchild.mkdir(parents=True)
    _transcript([_session_record()], project / "root.jsonl")
    _transcript([_session_record() | {"id": "child"}], child / "c.jsonl")
    _transcript([_session_record() | {"id": "grandchild"}], grandchild / "g.jsonl")

    by_id = {session.session_id: session for session in load_sessions([tmp_path])}

    assert by_id[SESSION_ID].nested is False
    assert by_id["child"].nested is True
    assert by_id["grandchild"].nested is True


def test_transcripts_are_found_recursively_and_deterministically(tmp_path: Path) -> None:
    for project in ("b-project", "a-project"):
        (tmp_path / project).mkdir()
        _transcript([_session_record()], tmp_path / project / "s.jsonl")
    (tmp_path / "a-project" / "notes.txt").write_text("SECRET", encoding="utf-8")

    found = find_transcripts([tmp_path])

    assert [path.parent.name for path, _ in found] == ["a-project", "b-project"]
    assert [nested for _, nested in found] == [False, False]


def test_a_corpus_scan_does_not_swallow_one_damaged_transcript(tmp_path: Path) -> None:
    _transcript([_session_record(), _assistant(_usage())], tmp_path / "good.jsonl")
    (tmp_path / "bad.jsonl").write_text("{ broken\n{}\n", encoding="utf-8")

    with pytest.raises(MalformedSessionError):
        load_sessions([tmp_path])
