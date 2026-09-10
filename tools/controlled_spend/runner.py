"""Isolated three-arm execution for the frozen M20 variance pilot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from laconic.runtime.omp_installer import apply_omp_install
from laconic.runtime.storage import resolve_data_dir
from tools.controlled_spend.authorization import (
    ExecutionAuthorization,
    consume_execution_authorization,
    verify_execution_authorization,
)
from tools.controlled_spend.budget_gateway import BudgetGateway, GatewaySnapshot
from tools.controlled_spend.manifest import (
    FIXTURES_ROOT,
    Arm,
    PilotManifest,
    RunSpec,
    ambient_paths,
    canonical_json,
    materialize_task,
    tree_digest,
)

_OMP_PACKAGE: Final = "@oh-my-pi/pi-coding-agent"
_HEADROOM_OMP_BINARY: Final = "omp"
_HEADROOM_PACKAGE: Final = "headroom-ai[proxy]==0.37.0"
_STATE_FILE: Final = "campaign-state.json"
_RECEIPT_FILE: Final = "gateway-receipts.jsonl"
_CREDENTIAL_SNAPSHOT: Final = "credential-snapshot.db"
_RUN_RESULT: Final = "run-result.json"
_DIAGNOSIS_RESULT: Final = "diagnosis-result.json"

_ENVIRONMENT_KEYS_TO_CLEAR: Final = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_TARGET_API_URL",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_USE_FOUNDRY",
        "FOUNDRY_BASE_URL",
        "LACONIC_DATA_DIR",
        "OMP_PROFILE",
        "PI_PROFILE",
    }
)


class PilotRunnerError(RuntimeError):
    """Raised when the controlled pilot cannot continue safely."""


class RunnerDiagnosisError(PilotRunnerError):
    """Raised when the deterministic failing-baseline preflight does not fail."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    timed_out: bool
    wall_seconds: float


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    arm: Arm
    process: ProcessResult
    completion_passed: bool
    fixture_guards_passed: bool

    @property
    def passed(self) -> bool:
        return (
            self.process.returncode == 0
            and not self.process.timed_out
            and self.completion_passed
            and self.fixture_guards_passed
        )


def _validate_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PilotRunnerError(f"cannot inspect private directory: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PilotRunnerError(f"private path is not an ordinary directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PilotRunnerError(f"private directory has a different owner: {path}")
    if metadata.st_mode & 0o777 != 0o700:
        raise PilotRunnerError(f"private directory must have mode 0700: {path}")


def _make_private_directory(path: Path, *, root: Path | None = None) -> None:
    if root is not None:
        try:
            path.relative_to(root)
        except ValueError as error:
            raise PilotRunnerError("private child escapes campaign root") from error
        _validate_private_directory(root)
    if path.exists():
        _validate_private_directory(path)
        return
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    _validate_private_directory(path)


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    _validate_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LiveStateSnapshot:
    """One attributed view of a live root: non-ambient digest, entries, and ambient entries."""

    digest: str
    ambient: dict[str, str]
    attributed: dict[str, str]


def live_state_snapshot(
    root: Path,
    *,
    prefix: str = "",
    ambient: re.Pattern[str] | None = None,
) -> LiveStateSnapshot:
    """Hash a live tree without following symlinks, attributing declared ambient paths.

    Entries whose `<prefix>/<relative>` name matches the ambient allowlist are excluded
    from the returned digest and recorded separately, so ordinary local agent activity
    cannot invalidate a campaign while any other change still does.
    """
    digest = hashlib.sha256()
    ambient_entries: dict[str, str] = {}
    attributed_entries: dict[str, str] = {}
    if not root.exists():
        digest.update(b"m")
        return LiveStateSnapshot(
            digest=digest.hexdigest(),
            ambient=ambient_entries,
            attributed=attributed_entries,
        )
    if root.is_symlink() or not root.is_dir():
        raise PilotRunnerError("live-state root is not an ordinary directory")
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        relative = name.encode()
        qualified = f"{prefix}/{name}" if prefix else name
        entry = hashlib.sha256()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(os.readlink(path))
            entry.update(b"l")
            entry.update(len(relative).to_bytes(4, "big"))
            entry.update(relative)
            entry.update(len(target).to_bytes(8, "big"))
            entry.update(target)
        elif stat.S_ISDIR(metadata.st_mode):
            entry.update(b"d")
            entry.update(len(relative).to_bytes(4, "big"))
            entry.update(relative)
            entry.update((0).to_bytes(8, "big"))
        elif not stat.S_ISREG(metadata.st_mode):
            raise PilotRunnerError("live-state tree contains a special file")
        else:
            entry.update(b"f")
            entry.update(len(relative).to_bytes(4, "big"))
            entry.update(relative)
            size = metadata.st_size
            entry.update(size.to_bytes(8, "big"))
            read_bytes = 0
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                while block := handle.read(1024 * 1024):
                    read_bytes += len(block)
                    entry.update(block)
            if read_bytes != size:
                raise PilotRunnerError("live-state file changed while it was hashed")
        if ambient is not None and ambient.fullmatch(qualified):
            ambient_entries[qualified] = entry.hexdigest()
            continue
        attributed_entries[qualified] = entry.hexdigest()
        digest.update(entry.digest())
    return LiveStateSnapshot(
        digest=digest.hexdigest(),
        ambient=ambient_entries,
        attributed=attributed_entries,
    )


def tree_state_digest(root: Path) -> str:
    """Hash a whole live tree with no ambient attribution."""
    return live_state_snapshot(root).digest


def snapshot_agent_database(source: Path, destination: Path) -> str:
    """Create one transactionally consistent, owner-only SQLite snapshot."""
    try:
        metadata = source.lstat()
    except OSError as error:
        raise PilotRunnerError("OMP credential database is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise PilotRunnerError("OMP credential database must be an owner-controlled ordinary file")
    if destination.exists():
        raise PilotRunnerError("credential snapshot already exists")
    _validate_private_directory(destination.parent)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    try:
        source_uri = f"{source.resolve(strict=True).as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_db:
            with sqlite3.connect(destination) as destination_db:
                source_db.backup(destination_db)
                result = destination_db.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise PilotRunnerError("credential snapshot failed SQLite quick_check")
        os.chmod(destination, 0o600)
        return _sha256(destination)
    except BaseException:
        _cleanup_credential_snapshot(destination)
        raise


def _copy_credential_snapshot(snapshot: Path, destination: Path) -> None:
    if destination.exists():
        raise PilotRunnerError("isolated credential database already exists")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with snapshot.open("rb") as source:
            while block := source.read(1024 * 1024):
                os.write(descriptor, block)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_credential_snapshot(snapshot: Path) -> bool:
    for suffix in ("", "-wal", "-shm"):
        snapshot.with_name(f"{snapshot.name}{suffix}").unlink(missing_ok=True)
    return not any(
        snapshot.with_name(f"{snapshot.name}{suffix}").exists() for suffix in ("", "-wal", "-shm")
    )


def _write_models_override(agent_dir: Path, gateway_url: str) -> None:
    models_path = agent_dir / "models.yml"
    body = f'providers:\n  anthropic:\n    baseUrl: "{gateway_url}"\n'
    descriptor = os.open(models_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pinned_omp_command(manifest: PilotManifest) -> list[str]:
    omp = cast(dict[str, Any], manifest.payload["omp"])
    return ["bunx", f"{_OMP_PACKAGE}@{omp['version']}"]


def _write_pinned_omp_shim(run_root: Path, manifest: PilotManifest) -> Path:
    bin_root = run_root / "bin"
    _make_private_directory(bin_root, root=run_root)
    path = bin_root / _HEADROOM_OMP_BINARY
    package = _pinned_omp_command(manifest)[1]
    body = f"#!/bin/sh\nexec bunx '{package}' \"$@\"\n".encode()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
    try:
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _base_omp_args(
    manifest: PilotManifest,
    *,
    prompt: str,
    worktree: Path,
    session_dir: Path,
) -> list[str]:
    omp = cast(dict[str, Any], manifest.payload["omp"])
    limits = cast(dict[str, Any], manifest.payload["limits"])
    return [
        "-p",
        prompt,
        "--model",
        f"{omp['provider']}/{omp['model']}",
        "--mode",
        "json",
        "--cwd",
        str(worktree),
        "--session-dir",
        str(session_dir),
        "--tools",
        "read,bash,edit,write,grep,glob",
        "--thinking",
        cast(str, omp["thinking"]),
        "--no-lsp",
        "--no-pty",
        "--no-skills",
        "--no-rules",
        "--no-title",
        "--max-time",
        str(limits["wall_seconds_per_run"]),
        "--auto-approve",
        "--no-prewalk",
        "--no-extensions",
    ]


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return cast(int, probe.getsockname()[1])


def build_run_command(
    manifest: PilotManifest,
    run: RunSpec,
    *,
    prompt: str,
    worktree: Path,
    session_dir: Path,
    extension_path: Path | None = None,
    headroom_port: int | None = None,
) -> list[str]:
    args = _base_omp_args(
        manifest,
        prompt=prompt,
        worktree=worktree,
        session_dir=session_dir,
    )
    if run.arm == "laconic":
        if extension_path is None:
            raise PilotRunnerError("Laconic arm requires its explicit extension")
        args.extend(["-e", str(extension_path)])
    if run.arm == "headroom":
        if headroom_port is None:
            raise PilotRunnerError("Headroom arm requires a proxy port")
        return [
            "uvx",
            "--from",
            _HEADROOM_PACKAGE,
            "headroom",
            "wrap",
            _HEADROOM_OMP_BINARY,
            "--port",
            str(headroom_port),
            "--",
            *args,
        ]
    return [*_pinned_omp_command(manifest), *args]


def build_run_environment(
    manifest: PilotManifest,
    run: RunSpec,
    *,
    agent_dir: Path,
    run_root: Path,
    gateway_url: str,
) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key in _ENVIRONMENT_KEYS_TO_CLEAR or key.startswith("HEADROOM_"):
            env.pop(key)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_NO_PTY"] = "1"
    env["NO_COLOR"] = "1"
    if run.arm == "headroom":
        omp_shim = _write_pinned_omp_shim(run_root, manifest)
        env["PATH"] = f"{omp_shim.parent}{os.pathsep}{env.get('PATH', '')}"
        headroom_root = run_root / "headroom"
        _make_private_directory(headroom_root, root=run_root)
        env.update(
            {
                "ANTHROPIC_TARGET_API_URL": gateway_url,
                "DO_NOT_TRACK": "1",
                "HEADROOM_BEACON": "off",
                "HEADROOM_LOG_FILE": str(headroom_root / "requests.jsonl"),
                "HEADROOM_LOG_MESSAGES": "off",
                "HEADROOM_WORKSPACE_DIR": str(headroom_root),
            }
        )
    return env


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> ProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds + 15)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    for path, content in ((stdout_path, stdout), (stderr_path, stderr)):
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return ProcessResult(
        returncode=process.returncode,
        timed_out=timed_out,
        wall_seconds=time.monotonic() - started,
    )


def _run_diagnosis(worktree: Path, run_root: Path) -> bool:
    env = dict(os.environ)
    for key in tuple(env):
        if key in _ENVIRONMENT_KEYS_TO_CLEAR or key.startswith("HEADROOM_"):
            env.pop(key)
    result = _run_process(
        ["python3", "diagnose.py"],
        cwd=worktree,
        env=env,
        timeout_seconds=30,
        stdout_path=run_root / "diagnose.stdout",
        stderr_path=run_root / "diagnose.stderr",
    )
    baseline_failed = not result.timed_out and result.returncode > 0
    _atomic_private_json(
        run_root / _DIAGNOSIS_RESULT,
        {
            "baseline_failed": baseline_failed,
            "command": ["python3", "diagnose.py"],
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "wall_seconds": result.wall_seconds,
        },
    )
    return baseline_failed


def _completion_passed(command: tuple[str, ...], cwd: Path) -> bool:
    completed = subprocess.run(
        (sys.executable, *command[1:]),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0


def _guard_fixture_files(original_source: Path, worktree: Path) -> bool:
    return _sha256(original_source / "diagnose.py") == _sha256(
        worktree / "diagnose.py"
    ) and tree_digest(original_source / "tests") == tree_digest(worktree / "tests")


def _run_payload(
    result: RunResult,
    gateway: GatewaySnapshot,
    *,
    runner_diagnosis_passed: bool | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "arm": result.arm,
        "completion_passed": result.completion_passed,
        "fixture_guards_passed": result.fixture_guards_passed,
        "gateway_halted_reason": gateway.halted_reason,
        "gateway_request_count": gateway.request_count,
        "gateway_spent_usd": format(gateway.spent_usd, "f"),
        "passed": result.passed,
        "process_returncode": result.process.returncode,
        "process_timed_out": result.process.timed_out,
        "run_id": result.run_id,
        "wall_seconds": result.process.wall_seconds,
    }
    if runner_diagnosis_passed is not None:
        payload["runner_diagnosis_passed"] = runner_diagnosis_passed
    return payload


def _preflight_binary(command: list[str], expected: str, label: str) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PilotRunnerError(f"{label} preflight failed") from error
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    if completed.returncode != 0 or expected not in output:
        raise PilotRunnerError(f"{label} version differs from the frozen manifest")


def preflight_environment(manifest: PilotManifest) -> None:
    omp = cast(dict[str, Any], manifest.payload["omp"])
    headroom = cast(dict[str, Any], manifest.payload["headroom"])
    _preflight_binary(
        [*_pinned_omp_command(manifest), "--version"],
        f"omp/{omp['version']}",
        "OMP",
    )
    _preflight_binary(
        ["uvx", "--from", _HEADROOM_PACKAGE, "headroom", "--version"],
        cast(str, headroom["version"]),
        "Headroom",
    )


def _credential_preflight(manifest: PilotManifest, agent_dir: Path, output_path: Path) -> None:
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    completed = subprocess.run(
        [
            *_pinned_omp_command(manifest),
            "usage",
            "--provider",
            "anthropic",
            "--json",
            "--redact",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    descriptor = os.open(
        output_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, completed.stdout)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PilotRunnerError(
            "isolated Anthropic credential preflight returned invalid JSON"
        ) from error
    if completed.returncode != 0 or not payload:
        raise PilotRunnerError("isolated Anthropic credential preflight failed")


def cleanup_agent_directory(agent_dir: Path) -> None:
    """Delete the isolated credential database and every SQLite sidecar."""
    if agent_dir.is_symlink() or not agent_dir.is_dir():
        raise PilotRunnerError("isolated agent directory is not an ordinary directory")
    shutil.rmtree(agent_dir)
    if agent_dir.exists() or agent_dir.is_symlink():
        raise PilotRunnerError("private credential cleanup failed")


def _execute_run(
    manifest: PilotManifest,
    run: RunSpec,
    *,
    campaign_root: Path,
    credential_snapshot: Path,
    gateway: BudgetGateway,
) -> RunResult:
    run_root = campaign_root / "runs" / run.run_id
    _make_private_directory(run_root, root=campaign_root)
    worktree = run_root / "worktree"
    task = next(task for task in manifest.tasks if task.task_id == run.task_id)
    materialize_task(task, worktree)
    original_source = FIXTURES_ROOT / task.task_id / task.source_dir

    runner_diagnosis_passed: bool | None = None
    if manifest.payload["schema_version"] == 2:
        runner_diagnosis_passed = _run_diagnosis(worktree, run_root)
        if not runner_diagnosis_passed:
            raise RunnerDiagnosisError(
                "runner diagnosis did not observe the frozen failing baseline"
            )
    agent_dir = run_root / "agent"
    session_dir = run_root / "sessions"
    _make_private_directory(agent_dir, root=run_root)
    _make_private_directory(session_dir, root=run_root)
    isolated_db = agent_dir / "agent.db"
    _copy_credential_snapshot(credential_snapshot, isolated_db)
    gateway_url = gateway.base_url(run.run_id)
    extension_path: Path | None = None
    headroom_port: int | None = None
    if run.arm == "headroom":
        headroom_port = _available_loopback_port()
    else:
        _write_models_override(agent_dir, gateway_url)
        if run.arm == "laconic":
            installation = apply_omp_install(
                agent_dir / "extensions",
                python=sys.executable,
                data_directory=run_root / "laconic-data",
            )
            extension_path = installation.plan.path
    prompt_path = FIXTURES_ROOT / task.task_id / task.prompt_file
    prompt = prompt_path.read_text(encoding="utf-8")
    command = build_run_command(
        manifest,
        run,
        prompt=prompt,
        worktree=worktree,
        session_dir=session_dir,
        extension_path=extension_path,
        headroom_port=headroom_port,
    )
    env = build_run_environment(
        manifest,
        run,
        agent_dir=agent_dir,
        run_root=run_root,
        gateway_url=gateway_url,
    )
    limits = cast(dict[str, Any], manifest.payload["limits"])
    try:
        process = _run_process(
            command,
            cwd=worktree,
            env=env,
            timeout_seconds=cast(int, limits["wall_seconds_per_run"]),
            stdout_path=run_root / "stdout.jsonl",
            stderr_path=run_root / "stderr.log",
        )
        completion_passed = _completion_passed(task.completion_command, worktree)
        fixture_guards_passed = _guard_fixture_files(original_source, worktree)
        result = RunResult(
            run_id=run.run_id,
            arm=run.arm,
            process=process,
            completion_passed=completion_passed,
            fixture_guards_passed=fixture_guards_passed,
        )
        _atomic_private_json(
            run_root / _RUN_RESULT,
            _run_payload(
                result,
                gateway.ledger.snapshot(),
                runner_diagnosis_passed=runner_diagnosis_passed,
            ),
        )
        return result
    finally:
        cleanup_agent_directory(agent_dir)


def live_state_roots(live_agent_database: Path | None = None) -> dict[str, Path]:
    """Return the live roots a campaign must leave byte-identical."""
    source_db = live_agent_database or Path.home() / ".omp" / "agent" / "agent.db"
    return {
        "laconic_runtime": resolve_data_dir(),
        "omp_agent": source_db.expanduser().absolute().parent,
    }


def run_campaign(
    artifact_root: Path,
    *,
    manifest: PilotManifest,
    authorization: ExecutionAuthorization,
    live_agent_database: Path | None = None,
) -> dict[str, Any]:
    """Execute the selected frozen population once under one external authorization."""
    if manifest.payload["schema_version"] != 2:
        raise PilotRunnerError("M20-v1 campaign execution is permanently closed")
    root = artifact_root.expanduser().absolute()
    verify_execution_authorization(authorization, manifest=manifest, artifact_root=root)
    source_db = live_agent_database or Path.home() / ".omp" / "agent" / "agent.db"
    live_roots = live_state_roots(live_agent_database)
    if any(
        root == live_root or root.is_relative_to(live_root) for live_root in live_roots.values()
    ):
        raise PilotRunnerError("artifact root must be outside every live-state root")
    if root.exists():
        raise PilotRunnerError("artifact root already exists; pilot re-runs are not automatic")
    ambient = ambient_paths(manifest)
    before_snapshots = {
        name: live_state_snapshot(path, prefix=name, ambient=ambient)
        for name, path in live_roots.items()
    }
    live_before = {name: snapshot.digest for name, snapshot in before_snapshots.items()}
    preflight_environment(manifest)
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    _validate_private_directory(root)
    _make_private_directory(root / "runs", root=root)

    credential_snapshot = root / _CREDENTIAL_SNAPSHOT
    credential_sha = snapshot_agent_database(source_db, credential_snapshot)
    state_path = root / _STATE_FILE
    state: dict[str, Any] = {
        "authorization_id": authorization.authorization_id,
        "authorization_sha256": authorization.receipt_sha256,
        "completed_runs": [],
        "credential_snapshot_sha256": credential_sha,
        "live_state_before": live_before,
        "manifest_hash": manifest.digest,
        "schema_version": manifest.payload["schema_version"],
        "status": "preflight",
    }
    _atomic_private_json(state_path, state)

    try:
        consume_execution_authorization(authorization)
        preflight_agent_dir = root / "credential-preflight"
        _make_private_directory(preflight_agent_dir, root=root)
        preflight_db = preflight_agent_dir / "agent.db"
        _copy_credential_snapshot(credential_snapshot, preflight_db)
        try:
            _credential_preflight(
                manifest,
                preflight_agent_dir,
                root / "credential-preflight.json",
            )
            os.replace(preflight_db, credential_snapshot)
            os.chmod(credential_snapshot, 0o600)
            state["credential_snapshot_sha256"] = _sha256(credential_snapshot)
        except BaseException:
            state["status"] = "incomplete"
            state["gateway_halted_reason"] = "credential_preflight_failed"
            _atomic_private_json(state_path, state)
            raise
        finally:
            shutil.rmtree(preflight_agent_dir)

        state["status"] = "running"
        _atomic_private_json(state_path, state)
        with BudgetGateway(manifest, root / _RECEIPT_FILE) as gateway:
            for run in manifest.run_order:
                try:
                    result = _execute_run(
                        manifest,
                        run,
                        campaign_root=root,
                        credential_snapshot=credential_snapshot,
                        gateway=gateway,
                    )
                except RunnerDiagnosisError:
                    state["status"] = "incomplete"
                    state["failed_run"] = run.run_id
                    state["gateway_halted_reason"] = "runner_diagnosis_failed"
                    _atomic_private_json(state_path, state)
                    raise
                snapshot = gateway.ledger.snapshot()
                if not result.passed or snapshot.halted_reason is not None:
                    state["status"] = "incomplete"
                    state["failed_run"] = run.run_id
                    state["gateway_halted_reason"] = snapshot.halted_reason
                    _atomic_private_json(state_path, state)
                    raise PilotRunnerError(f"pilot stopped at invalid cell {run.run_id}")
                cast(list[str], state["completed_runs"]).append(run.run_id)
                _atomic_private_json(state_path, state)
            final_snapshot = gateway.ledger.snapshot()
        state["status"] = "completed"
        state["gateway_spent_usd"] = format(final_snapshot.spent_usd, "f")
        _atomic_private_json(state_path, state)
        return state
    except BaseException:
        if state["status"] == "running":
            state["status"] = "interrupted"
            _atomic_private_json(state_path, state)
        raise
    finally:
        active_error = sys.exception()
        cleanup_failed = not _cleanup_credential_snapshot(credential_snapshot)
        after_snapshots = {
            name: live_state_snapshot(path, prefix=name, ambient=ambient)
            for name, path in live_roots.items()
        }
        live_after = {name: snapshot.digest for name, snapshot in after_snapshots.items()}
        state["live_state_after"] = live_after
        state["live_state_ambient_changes"] = sum(
            len(
                {
                    path
                    for path, _ in set(before_snapshots[name].ambient.items())
                    ^ set(after_snapshots[name].ambient.items())
                }
            )
            for name in live_roots
        )
        live_state_changed = live_after != live_before
        if cleanup_failed or live_state_changed:
            state["status"] = "incomplete"
            state["gateway_halted_reason"] = (
                "private_artifact_cleanup_failed" if cleanup_failed else "live_state_changed"
            )
        _atomic_private_json(state_path, state)
        if active_error is None and (cleanup_failed or live_state_changed):
            raise PilotRunnerError(cast(str, state["gateway_halted_reason"]))


def changed_attributed_paths(
    before: LiveStateSnapshot, after: LiveStateSnapshot
) -> tuple[str, ...]:
    """Return the non-ambient paths that differ between two snapshots of one root."""
    difference = set(before.attributed.items()) ^ set(after.attributed.items())
    return tuple(sorted({path for path, _ in difference}))


def measure_quiescence(
    manifest: PilotManifest,
    roots: dict[str, Path],
    *,
    seconds: int,
    interval: int,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, tuple[str, ...]]:
    """Sample the attributed live state until the window elapses.

    Returns an empty mapping when every sample was identical. Otherwise returns the
    non-ambient paths that moved, keyed by live-state root, so the writer can be named.
    """
    if manifest.payload["schema_version"] != 2:
        raise PilotRunnerError("only the M20-v2 pilot declares an ambient live-state allowlist")
    if seconds < 1 or interval < 1 or interval > seconds:
        raise PilotRunnerError("quiescence window and interval must be positive and ordered")
    ambient = ambient_paths(manifest)
    baseline = {
        name: live_state_snapshot(path, prefix=name, ambient=ambient)
        for name, path in roots.items()
    }
    deadline = now() + seconds
    while now() < deadline:
        sleep(min(interval, max(0.0, deadline - now())))
        moved: dict[str, tuple[str, ...]] = {}
        for name, path in roots.items():
            current = live_state_snapshot(path, prefix=name, ambient=ambient)
            if current.digest != baseline[name].digest:
                moved[name] = changed_attributed_paths(baseline[name], current)
        if moved:
            return moved
    return {}
