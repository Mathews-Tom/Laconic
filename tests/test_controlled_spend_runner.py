from __future__ import annotations

import itertools
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import tools.controlled_spend.__main__ as cli_module
import tools.controlled_spend.runner as runner_module
from tools.controlled_spend.manifest import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_V2_MANIFEST_PATH,
    RunSpec,
    ambient_paths,
    materialize_task,
    validate_manifest_file,
)
from tools.controlled_spend.runner import (
    PilotRunnerError,
    _cleanup_credential_snapshot,
    _execute_run,
    _run_diagnosis,
    build_run_command,
    build_run_environment,
    cleanup_agent_directory,
    live_state_snapshot,
    measure_quiescence,
    snapshot_agent_database,
    tree_state_digest,
)


def test_agent_database_snapshot_is_consistent_and_owner_only(tmp_path: Path) -> None:
    source = tmp_path / "agent.db"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE credentials (provider TEXT PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO credentials VALUES ('anthropic', 'opaque')")
    destination_root = tmp_path / "private"
    destination_root.mkdir(mode=0o700)
    destination = destination_root / "snapshot.db"

    digest = snapshot_agent_database(source, destination)

    with sqlite3.connect(destination) as database:
        row = database.execute("SELECT provider, value FROM credentials").fetchone()
    assert row == ("anthropic", "opaque")
    assert len(digest) == 64
    assert destination.stat().st_mode & 0o777 == 0o600


def test_agent_cleanup_removes_database_and_wal_sidecars(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(mode=0o700)
    for name in ("agent.db", "agent.db-wal", "agent.db-shm", "models.yml"):
        (agent_dir / name).write_bytes(b"private")

    cleanup_agent_directory(agent_dir)

    assert not agent_dir.exists()


def test_credential_cleanup_removes_database_and_wal_sidecars(tmp_path: Path) -> None:
    snapshot = tmp_path / "credential-snapshot.db"
    for suffix in ("", "-wal", "-shm"):
        snapshot.with_name(f"{snapshot.name}{suffix}").write_bytes(b"private")

    cleaned = _cleanup_credential_snapshot(snapshot)

    assert cleaned is True
    assert not any(tmp_path.glob("credential-snapshot.db*"))


def test_live_state_digest_changes_with_any_tree_byte(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    target = live_root / "state.json"
    target.write_bytes(b"before")
    before = tree_state_digest(live_root)

    target.write_bytes(b"after")

    assert tree_state_digest(live_root) != before


def test_live_state_digest_length_prefixes_file_content(tmp_path: Path) -> None:
    two_files = tmp_path / "two-files"
    two_files.mkdir()
    (two_files / "a").write_bytes(b"")
    (two_files / "b").write_bytes(b"X")
    one_file = tmp_path / "one-file"
    one_file.mkdir()
    (one_file / "a").write_bytes(b"\0f\0b\0X")

    assert tree_state_digest(two_files) != tree_state_digest(one_file)


def test_live_state_digest_hashes_symlink_without_following_target(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "state").write_bytes(b"before")
    link = live_root / "skills"
    link.symlink_to(external, target_is_directory=True)
    before = tree_state_digest(live_root)

    (external / "state").write_bytes(b"after")

    assert tree_state_digest(live_root) == before
    link.unlink()
    link.symlink_to(tmp_path / "other", target_is_directory=True)
    assert tree_state_digest(live_root) != before


def test_native_and_laconic_commands_share_the_frozen_omp_surface(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    worktree = tmp_path / "worktree"
    session_dir = tmp_path / "sessions"
    native = RunSpec("r001", "t01", 1, "native")
    laconic = RunSpec("r003", "t01", 1, "laconic")
    extension = tmp_path / "laconic.ts"

    native_command = build_run_command(
        manifest,
        native,
        prompt="fix it",
        worktree=worktree,
        session_dir=session_dir,
    )
    laconic_command = build_run_command(
        manifest,
        laconic,
        prompt="fix it",
        worktree=worktree,
        session_dir=session_dir,
        extension_path=extension,
    )

    assert native_command[:2] == [
        "bunx",
        "@oh-my-pi/pi-coding-agent@18.1.14",
    ]
    assert native_command == laconic_command[:-2]
    assert laconic_command[-2:] == ["-e", str(extension)]
    assert "--no-extensions" in native_command
    assert "--no-prewalk" in native_command
    assert native_command[native_command.index("--tools") + 1] == "read,bash,edit,write,grep,glob"
    assert native_command[native_command.index("--model") + 1] == "anthropic/claude-sonnet-5"


def test_headroom_command_and_environment_are_isolated(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    run = RunSpec("r001", "t01", 1, "headroom")
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    agent_dir = run_root / "agent"
    agent_dir.mkdir(mode=0o700)

    command = build_run_command(
        manifest,
        run,
        prompt="fix it",
        worktree=run_root / "worktree",
        session_dir=run_root / "sessions",
        headroom_port=8787,
    )
    env = build_run_environment(
        manifest,
        run,
        agent_dir=agent_dir,
        run_root=run_root,
        gateway_url="http://127.0.0.1:9999/run/r001",
    )

    assert command[:8] == [
        "uvx",
        "--from",
        "headroom-ai[proxy]==0.37.0",
        "headroom",
        "wrap",
        "omp",
        "--port",
        "8787",
    ]
    assert command[8:10] == ["--", "-p"]
    assert env["PI_CODING_AGENT_DIR"] == str(agent_dir)
    assert env["ANTHROPIC_TARGET_API_URL"].endswith("/run/r001")
    assert env["HEADROOM_BEACON"] == "off"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HEADROOM_LOG_MESSAGES"] == "off"
    assert Path(env["HEADROOM_LOG_FILE"]).is_relative_to(run_root)
    assert Path(env["HEADROOM_WORKSPACE_DIR"]).stat().st_mode & 0o777 == 0o700
    omp_shim = Path(env["PATH"].split(os.pathsep)[0]) / "omp"
    assert omp_shim.stat().st_mode & 0o777 == 0o700
    assert "@oh-my-pi/pi-coding-agent@18.1.14" in omp_shim.read_text()


def test_native_environment_removes_ambient_experiment_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_TARGET_API_URL", "https://wrong.invalid")
    monkeypatch.setenv("HEADROOM_BEACON", "on")
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    agent_dir = run_root / "agent"
    agent_dir.mkdir(mode=0o700)
    run = RunSpec("r002", "t01", 1, "native")

    env = build_run_environment(
        validate_manifest_file(DEFAULT_MANIFEST_PATH),
        run,
        agent_dir=agent_dir,
        run_root=run_root,
        gateway_url="http://127.0.0.1:9999/run/r002",
    )

    assert env["PI_CODING_AGENT_DIR"] == str(agent_dir)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_TARGET_API_URL" not in env
    assert "HEADROOM_BEACON" not in env
    assert not (run_root / "headroom").exists()


def test_v2_runner_diagnosis_observes_frozen_failing_baseline(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    worktree = tmp_path / "worktree"
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    materialize_task(manifest.tasks[0], worktree)

    observed = _run_diagnosis(worktree, run_root)

    diagnosis = json.loads((run_root / "diagnosis-result.json").read_text())
    assert observed is True
    assert diagnosis["baseline_failed"] is True
    assert diagnosis["command"] == ["python3", "diagnose.py"]
    assert diagnosis["returncode"] > 0
    assert diagnosis["timed_out"] is False


def test_v2_failed_diagnosis_stops_before_credential_or_omp_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir(mode=0o700)
    run = manifest.run_order[0]
    monkeypatch.setattr(runner_module, "_run_diagnosis", lambda *_: False)

    with pytest.raises(PilotRunnerError, match="did not observe the frozen failing baseline"):
        _execute_run(
            manifest,
            run,
            campaign_root=campaign_root,
            credential_snapshot=tmp_path / "missing-credential-snapshot.db",
            gateway=cast(Any, object()),
        )

    assert not (campaign_root / "runs" / run.run_id / "agent").exists()


def test_attributed_digest_ignores_declared_ambient_paths(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    ambient = ambient_paths(manifest)
    root = tmp_path / "omp_agent"
    (root / "sessions" / "proj").mkdir(parents=True)
    (root / "cache" / "composer").mkdir(parents=True)
    (root / "agent.db").write_bytes(b"credentials")
    (root / "agent.db-wal").write_bytes(b"before")
    (root / "sessions" / "proj" / "a.jsonl").write_bytes(b"before")
    before = live_state_snapshot(root, prefix="omp_agent", ambient=ambient)

    (root / "agent.db-wal").write_bytes(b"after-and-longer")
    (root / "sessions" / "proj" / "a.jsonl").write_bytes(b"after")
    (root / "cache" / "composer" / "status.json").write_bytes(b"new")
    after = live_state_snapshot(root, prefix="omp_agent", ambient=ambient)

    changed = {path for path, _ in set(before.ambient.items()) ^ set(after.ambient.items())}
    assert after.digest == before.digest
    assert changed == {
        "omp_agent/agent.db-wal",
        "omp_agent/sessions/proj/a.jsonl",
        "omp_agent/cache/composer/status.json",
    }


def test_a_campaign_write_to_agent_configuration_is_detected(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    ambient = ambient_paths(manifest)
    root = tmp_path / "omp_agent"
    (root / "extensions").mkdir(parents=True)
    before = live_state_snapshot(root, prefix="omp_agent", ambient=ambient)

    (root / "extensions" / "laconic.ts").write_bytes(b"campaign output")

    assert live_state_snapshot(root, prefix="omp_agent", ambient=ambient).digest != before.digest


def test_attributed_digest_still_detects_a_credential_database_write(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    ambient = ambient_paths(manifest)
    root = tmp_path / "omp_agent"
    root.mkdir()
    (root / "agent.db").write_bytes(b"credentials")
    before = live_state_snapshot(root, prefix="omp_agent", ambient=ambient)

    (root / "agent.db").write_bytes(b"tampered")

    assert live_state_snapshot(root, prefix="omp_agent", ambient=ambient).digest != before.digest


def test_attributed_digest_detects_any_undeclared_path(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    ambient = ambient_paths(manifest)
    root = tmp_path / "laconic_runtime"
    (root / "observe").mkdir(parents=True)
    (root / "observe" / "audit.jsonl").write_bytes(b"receipt")
    before = live_state_snapshot(root, prefix="laconic_runtime", ambient=ambient)

    (root / "observe" / "audit.jsonl").write_bytes(b"receipt-two")
    assert (
        live_state_snapshot(root, prefix="laconic_runtime", ambient=ambient).digest == before.digest
    )

    (root / "ledger.sqlite3").write_bytes(b"runtime")
    assert (
        live_state_snapshot(root, prefix="laconic_runtime", ambient=ambient).digest != before.digest
    )


def test_ambient_attribution_requires_the_root_prefix(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    ambient = ambient_paths(manifest)
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent.db-wal").write_bytes(b"before")
    before = live_state_snapshot(root, prefix="laconic_runtime", ambient=ambient)

    (root / "agent.db-wal").write_bytes(b"after")

    assert (
        live_state_snapshot(root, prefix="laconic_runtime", ambient=ambient).digest != before.digest
    )


def _quiet_roots(tmp_path: Path) -> dict[str, Path]:
    runtime = tmp_path / "laconic_runtime"
    (runtime / "observe").mkdir(parents=True)
    (runtime / "observe" / "audit.jsonl").write_bytes(b"receipt")
    (runtime / "ledger.sqlite3").write_bytes(b"runtime")
    agent = tmp_path / "omp_agent"
    (agent / "sessions").mkdir(parents=True)
    (agent / "agent.db").write_bytes(b"credentials")
    return {"laconic_runtime": runtime, "omp_agent": agent}


def test_quiescence_passes_while_only_ambient_paths_move(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    roots = _quiet_roots(tmp_path)
    ticks = itertools.count()
    clock = itertools.count(step=1.0)

    def churn(_: float) -> None:
        index = next(ticks)
        (roots["laconic_runtime"] / "observe" / "audit.jsonl").write_bytes(b"receipt" * index)
        (roots["omp_agent"] / "sessions" / f"s{index}.jsonl").write_bytes(b"turn")

    moved = measure_quiescence(
        manifest,
        roots,
        seconds=3,
        interval=1,
        sleep=churn,
        now=lambda: float(next(clock)),
    )

    assert moved == {}


def test_quiescence_fails_and_names_a_credential_database_write(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    roots = _quiet_roots(tmp_path)

    def tamper(_: float) -> None:
        (roots["omp_agent"] / "agent.db").write_bytes(b"checkpointed")

    moved = measure_quiescence(manifest, roots, seconds=3, interval=1, sleep=tamper)

    assert moved == {"omp_agent": ("omp_agent/agent.db",)}


def test_quiescence_names_a_new_undeclared_path(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)
    roots = _quiet_roots(tmp_path)

    def add(_: float) -> None:
        (roots["laconic_runtime"] / "stray.json").write_bytes(b"new")

    moved = measure_quiescence(manifest, roots, seconds=3, interval=1, sleep=add)

    assert moved == {"laconic_runtime": ("laconic_runtime/stray.json",)}


def test_quiescence_rejects_an_interval_wider_than_the_window(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)

    with pytest.raises(PilotRunnerError, match="positive and ordered"):
        measure_quiescence(
            manifest,
            _quiet_roots(tmp_path),
            seconds=1,
            interval=5,
            sleep=lambda _: None,
        )


@pytest.mark.parametrize(
    ("moved", "expected_code", "expected_output"),
    [
        ({}, 0, "quiescent=true"),
        ({"omp_agent": ("omp_agent/agent.db",)}, 1, "omp_agent/agent.db"),
    ],
)
def test_quiesce_cli_exit_code_follows_the_measurement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    moved: dict[str, tuple[str, ...]],
    expected_code: int,
    expected_output: str,
) -> None:
    monkeypatch.setattr(cli_module, "measure_quiescence", lambda *_, **__: moved)
    monkeypatch.setattr(cli_module, "live_state_roots", lambda: {"omp_agent": Path("/nonexistent")})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controlled-spend",
            "pilot",
            "quiesce",
            "--manifest",
            str(DEFAULT_V2_MANIFEST_PATH),
            "--seconds",
            "2",
            "--interval",
            "1",
        ],
    )

    assert cli_module.main() == expected_code
    assert expected_output in capsys.readouterr().out


def test_quiescence_refuses_the_permanently_closed_v1_pilot(tmp_path: Path) -> None:
    with pytest.raises(PilotRunnerError, match="only the M20-v2 pilot"):
        measure_quiescence(
            validate_manifest_file(DEFAULT_MANIFEST_PATH),
            _quiet_roots(tmp_path),
            seconds=2,
            interval=1,
            sleep=lambda _: None,
        )
