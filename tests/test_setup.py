"""Host-capability honesty and runtime verification for `laconic setup`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.cli import main
from laconic.ledger import RuntimeDecisionOutcome
from laconic.runtime.storage import RuntimeStorage
from laconic.setup import (
    HOST_CLAUDE_CODE,
    HOST_CODEX,
    HOST_OMP,
    HostCapability,
    detect_hosts,
    verify_runtime,
)


def _hosts(cwd: Path, home: Path) -> dict[str, HostCapability]:
    return {host.host: host for host in detect_hosts(cwd=cwd, home=home, env={})}


def _record(root: Path, session: str, *, outcome: RuntimeDecisionOutcome, reason: str) -> None:
    storage = RuntimeStorage(root)
    with storage.open_ledger(session) as ledger:
        ledger.record_runtime_decision(
            sequence=0,
            request_id=f"encode-{session}",
            tool_name="Read",
            outcome=outcome,
            reason=reason,
            candidate_reference=f"{session}/F1",
            raw_chars=100,
            visible_chars=40 if outcome == "emitted" else 100,
            latency_ms=2,
        )


def test_claude_code_is_reported_as_observe_only_even_when_installed(tmp_path: Path) -> None:
    """The claim a user is most likely to get wrong.

    Claude Code's hooks fire after a tool has already returned its full
    result, so they cannot compress anything. A successful install there
    still buys zero token reduction, and reporting `codec: yes` would be a
    false savings claim dressed up as a capability.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".omp").mkdir()

    hosts = _hosts(tmp_path / "proj", home)

    claude_code = hosts[HOST_CLAUDE_CODE]
    assert claude_code.detected is True
    assert claude_code.codec is False
    assert claude_code.observe is True
    assert hosts[HOST_OMP].codec is True


def test_an_unsupported_host_is_reported_rather_than_omitted(tmp_path: Path) -> None:
    """A user who runs Codex must be told it is unsupported.

    Filtering the row out is indistinguishable from a detection bug, and
    leaves them waiting for savings that can never arrive.
    """
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)

    codex = _hosts(tmp_path / "proj", home)[HOST_CODEX]

    assert codex.detected is True
    assert codex.codec is False
    assert codex.observe is False
    assert codex.actionable is False


def test_verification_is_unconfirmed_when_the_codec_has_never_run(tmp_path: Path) -> None:
    verification = verify_runtime(tmp_path / "data")

    assert verification.confirmed is False
    assert verification.eligible_observations == 0


def test_verification_confirms_a_codec_that_only_ever_passed_through(tmp_path: Path) -> None:
    """A pass-through is a working codec, not a failed one.

    Most real tool results are too small to shrink, so declining them is the
    correct outcome. Keying confirmation on compressed observations would
    report failure on a healthy install and teach a user to distrust the
    honest answer.
    """
    root = tmp_path / "data"
    _record(root, "quiet", outcome="pass_through", reason="not_smaller")

    verification = verify_runtime(root)

    assert verification.confirmed is True
    assert verification.eligible_observations == 1
    assert verification.compressed_observations == 0


def test_verification_distinguishes_a_session_with_no_eligible_tool_call(
    tmp_path: Path,
) -> None:
    """Storage existing is not evidence the codec decided anything.

    A session that only ran ineligible tools leaves a ledger behind with no
    decisions in it. Treating the ledger's existence as confirmation would
    claim the codec works without a single observation to show for it.
    """
    root = tmp_path / "data"
    storage = RuntimeStorage(root)
    with storage.open_ledger("empty"):
        pass

    verification = verify_runtime(root)

    assert verification.sessions == 1
    assert verification.confirmed is False
    assert "no eligible tool result yet" in verification.detail


def test_setup_installs_the_codec_against_the_storage_root_it_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The install and the verification must agree on where decisions land.

    `--data-dir` selects the runtime storage root. If setup verifies that
    root but installs an adapter pointed at the default one, the codec's
    decisions never reach the directory being checked: `--verify-only`
    could never confirm, and the operator's chosen root is silently
    dropped. Nothing in the pure detection or verification helpers can
    catch that disagreement -- only the handler that wires both.
    """
    home = tmp_path / "home"
    (home / ".omp" / "agent" / "extensions").mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)

    exit_code = main(
        ["setup", "--data-dir", str(storage), "--format", "json"],
    )

    assert exit_code == 0
    document = json.loads(capsys.readouterr().out)
    installed = [action for action in document["actions"] if action["host"] == HOST_OMP]
    assert installed, "the detected OMP host should have been installed"
    extension = Path(str(installed[0]["path"])).read_text(encoding="utf-8")
    assert str(storage) in extension
