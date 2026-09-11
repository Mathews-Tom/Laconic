"""Claude Code transforming-hook contract: shape fidelity and fail-open."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from laconic.runtime.claude_code import main, transform

_LONG_OUTPUT = "\n".join(f"line {index}: {'payload ' * 12}" for index in range(400))


def _bash_payload(tmp_path: Path, *, session: str, call: str, **extra: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "stdout": _LONG_OUTPUT,
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
    }
    response.update(extra)
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": session,
        "tool_use_id": call,
        "cwd": str(tmp_path),
        "tool_input": {"command": "ls -R"},
        "tool_response": response,
    }


def _updated(result: dict[str, Any]) -> dict[str, Any]:
    output = result["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(output, dict)
    return output


def test_an_unrecognized_key_survives_the_replacement(tmp_path: Path) -> None:
    """Claude Code silently ignores a replacement that misses the tool's shape.

    There is no error and no signal back to the hook, so a replacement
    built from an assumed schema degrades into a silent no-op the moment
    Claude Code adds a field. Real transcripts already carry
    ``noOutputExpected`` and ``gitOperation``, neither of which appears in
    the published example. The adapter must copy the observed response and
    overwrite one field rather than construct a new object.
    """
    payload = _bash_payload(
        tmp_path,
        session="shapes",
        call="toolu_1",
        gitOperation={"pr": {"number": 311}},
        someFutureField=["unknown"],
    )
    original = payload["tool_response"]

    result = transform(payload, data_dir=tmp_path / "data")

    assert result is not None
    updated = _updated(result)
    assert sorted(updated) == sorted(original)
    assert all(updated[key] == original[key] for key in original if key != "stdout")
    assert len(updated["stdout"]) < len(original["stdout"])


def test_a_second_tool_call_in_the_same_session_still_transforms(tmp_path: Path) -> None:
    """Every tool call in a Claude Code session shares one ``session_id``.

    The ledger keys a decision on ``(session_id, request_id)``, so a
    constant request id transforms the first observation of a session and
    then fails open on every one after it -- the failure mode a
    single-payload test cannot see, because it only ever makes one call.
    """
    data_dir = tmp_path / "data"
    first = transform(_bash_payload(tmp_path, session="reuse", call="toolu_1"), data_dir=data_dir)
    second = transform(_bash_payload(tmp_path, session="reuse", call="toolu_2"), data_dir=data_dir)
    third = transform(_bash_payload(tmp_path, session="reuse", call="toolu_3"), data_dir=data_dir)

    assert first is not None
    assert second is not None
    assert third is not None


def test_the_transformed_output_is_exactly_recoverable(tmp_path: Path) -> None:
    """Replacement is only safe because the original is still retrievable."""
    from laconic.runtime.storage import RuntimeStorage

    payload = _bash_payload(tmp_path, session="recover", call="toolu_1")
    data_dir = tmp_path / "data"

    result = transform(payload, data_dir=data_dir)

    assert result is not None
    emitted = _updated(result)["stdout"]
    reference = emitted.split("\n", 1)[0]
    handle = reference.split("/", 1)[1].split(" ", 1)[0]
    with RuntimeStorage(data_dir).open_ledger("recover") as ledger:
        assert ledger.expand(handle) == _LONG_OUTPUT


def test_a_file_read_carries_its_tool_input_to_the_encoder(tmp_path: Path) -> None:
    """The file encoder reads its subject and span hints from ``tool_input``.

    Dropping it leaves the outliner unable to identify the language, which
    does not raise -- it silently produces a weaker encoding that loses the
    strictly-smaller comparison, so the tool result is never transformed.
    """
    source = "\n".join(f"def function_{index}():\n    return {index}" for index in range(300))
    file_fields: dict[str, Any] = {
        "content": source,
        "filePath": str(tmp_path / "module.py"),
        "numLines": source.count("\n") + 1,
        "startLine": 1,
        "totalLines": source.count("\n") + 1,
    }
    response: dict[str, Any] = {"type": "text", "file": file_fields}
    base = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "session_id": "reads",
        "cwd": str(tmp_path),
        "tool_response": response,
    }

    with_input = transform(
        {**base, "tool_use_id": "a", "tool_input": {"file_path": str(tmp_path / "module.py")}},
        data_dir=tmp_path / "with",
    )
    without_input = transform(
        {**base, "tool_use_id": "b", "tool_input": {}},
        data_dir=tmp_path / "without",
    )

    assert with_input is not None, "a real file read must transform"
    file_output = _updated(with_input)["file"]
    assert sorted(file_output) == sorted(file_fields)
    assert file_output["filePath"] == file_fields["filePath"]
    assert len(file_output["content"]) < len(source)
    # Strictly better, not merely no worse: a disjunction would still hold if
    # forwarding regressed and both paths produced the same encoding, which
    # is exactly the regression this test exists to catch.
    assert without_input is None, "without its subject the encoding must lose the size test"
    # `numLines` follows the replacement so the model is never told it
    # received more lines than it can see.
    assert file_output["numLines"] == file_output["content"].count("\n") + 1
    assert file_output["totalLines"] == file_fields["totalLines"]


def test_a_non_textual_read_is_left_alone(tmp_path: Path) -> None:
    """An image read carries no text to shrink and no string to round-trip."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "session_id": "image",
        "tool_use_id": "toolu_1",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": str(tmp_path / "shot.png")},
        "tool_response": {"type": "image", "file": {"base64": "iVBORw0KGgo=", "type": "image/png"}},
    }

    assert transform(payload, data_dir=tmp_path / "data") is None


def test_a_malformed_payload_writes_nothing_and_exits_zero(tmp_path: Path) -> None:
    """Fail-open is the safety story: a crash costs compression, not correctness.

    Empty stdout leaves Claude Code's original tool result in place, and a
    zero exit keeps the hook's stderr out of the model's context.
    """
    stdout = io.StringIO()

    code = main(
        ["--data-dir", str(tmp_path / "data")],
        stdin=io.StringIO("not json at all"),
        stdout=stdout,
    )

    assert code == 0
    assert stdout.getvalue() == ""


def test_the_entrypoint_emits_one_hook_response_on_stdout(tmp_path: Path) -> None:
    stdout = io.StringIO()

    code = main(
        ["--data-dir", str(tmp_path / "data")],
        stdin=io.StringIO(json.dumps(_bash_payload(tmp_path, session="cli", call="toolu_1"))),
        stdout=stdout,
    )

    assert code == 0
    document = json.loads(stdout.getvalue())
    assert document["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "updatedToolOutput" in document["hookSpecificOutput"]


def test_an_unsupported_event_is_never_transformed(tmp_path: Path) -> None:
    payload = _bash_payload(tmp_path, session="pre", call="toolu_1")
    payload["hook_event_name"] = "PreToolUse"

    assert transform(payload, data_dir=tmp_path / "data") is None
