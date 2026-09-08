from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.controlled_spend.manifest import RunSpec, validate_manifest_file
from tools.controlled_spend.runner import (
    build_run_command,
    build_run_environment,
    cleanup_agent_directory,
    snapshot_agent_database,
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


def test_native_and_laconic_commands_share_the_frozen_omp_surface(tmp_path: Path) -> None:
    manifest = validate_manifest_file()
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

    assert native_command[0] == "omp"
    assert native_command[1:] == laconic_command[1:-2]
    assert laconic_command[-2:] == ["-e", str(extension)]
    assert "--no-extensions" in native_command
    assert "--no-prewalk" in native_command
    assert native_command[native_command.index("--tools") + 1] == "read,bash,edit,write,grep,glob"
    assert native_command[native_command.index("--model") + 1] == "anthropic/claude-sonnet-5"


def test_headroom_command_and_environment_are_isolated(tmp_path: Path) -> None:
    manifest = validate_manifest_file()
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
