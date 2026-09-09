from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import tools.controlled_spend.manifest as manifest_module
from tools.controlled_spend.manifest import (
    ARMS,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_V2_MANIFEST_PATH,
    RUN_COUNT,
    ManifestError,
    canonical_json,
    materialize_task,
    validate_manifest_file,
    validate_manifest_json,
    verify_completion_oracles,
)


@pytest.mark.parametrize("manifest_path", [DEFAULT_MANIFEST_PATH, DEFAULT_V2_MANIFEST_PATH])
def test_frozen_manifest_and_fixture_oracles_are_complete(manifest_path: Path) -> None:
    manifest = validate_manifest_file(manifest_path)

    assert len(manifest.tasks) == 4
    assert len(manifest.run_order) == RUN_COUNT
    assert {run.arm for run in manifest.run_order} == set(ARMS)
    verify_completion_oracles(manifest)


def test_materialized_task_is_a_clean_git_repository(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    destination = tmp_path / "task"

    materialize_task(manifest.tasks[0], destination)

    assert (destination / ".git").is_dir()
    assert not (destination / "PROMPT.txt").exists()
    assert not (destination / "solution.patch").exists()


def test_runtime_bytecode_does_not_change_or_leak_into_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(
        manifest_module.FIXTURES_ROOT,
        fixtures,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    monkeypatch.setattr(manifest_module, "FIXTURES_ROOT", fixtures)
    cache = fixtures / "t01" / "seed" / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "runtime.pyc").write_bytes(b"runtime")

    manifest = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    destination = tmp_path / "task"
    materialize_task(manifest.tasks[0], destination)

    assert not list(destination.rglob("__pycache__"))
    assert not list(destination.rglob("*.pyc"))


def test_fixture_source_mutation_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "pilot-manifest.json"
    manifest_path.write_bytes(DEFAULT_MANIFEST_PATH.read_bytes())
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["tasks"][0]["source_sha256"] = "0" * 64
    manifest_path.write_bytes(canonical_json(payload))

    with pytest.raises(ManifestError, match="fixture source digest drifted"):
        validate_manifest_file(manifest_path)


def test_duplicate_run_cell_is_rejected() -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["run_order"][1] = dict(payload["run_order"][0], run_id="replacement")

    with pytest.raises(ManifestError, match="each task/repetition/arm cell exactly once"):
        validate_manifest_json(payload)


def test_unreviewed_manifest_key_is_rejected() -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["new_field"] = "not reviewed"

    with pytest.raises(ManifestError, match="manifest keys differ"):
        validate_manifest_json(payload)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("omp", "model", "different-model", "OMP/model settings differ"),
        ("limits", "total_spend_usd", "10.01", "total spend cap"),
        ("analysis", "action_threshold_fraction", "0.11", "analysis differs"),
    ],
)
def test_safety_pins_reject_drift(section: str, key: str, value: str, message: str) -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload[section][key] = value

    with pytest.raises(ManifestError, match=message):
        validate_manifest_json(payload)


def test_noncanonical_manifest_serialization_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_path = tmp_path / "pilot-manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ManifestError, match="canonical JSON serialization"):
        validate_manifest_file(manifest_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ("pilot", "preflight"),
        ("pilot", "run", "--artifact-root", "/tmp/unused"),
        (
            "pilot",
            "report",
            "--artifact-root",
            "/tmp/unused",
            "--output-json",
            "/tmp/unused.json",
            "--output-markdown",
            "/tmp/unused.md",
        ),
        (
            "pilot",
            "check",
            "--artifact-root",
            "/tmp/unused",
            "--report-json",
            "/tmp/unused.json",
            "--report-markdown",
            "/tmp/unused.md",
        ),
    ],
)
def test_pilot_cli_requires_explicit_manifest(arguments: tuple[str, ...]) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tools.controlled_spend", *arguments],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--manifest" in completed.stderr


def test_v2_manifest_is_distinct_and_fail_closed() -> None:
    v1 = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    v2 = validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)

    assert v1.digest == "a76c6cb0d2f34737ccd629398b0b2122a3c0a74c63a77055f8112cea602f7b44"
    assert v2.digest == "526c5de204d39c4c2bb8d9d96bb54163f5caff52e55940467fd036f4f4acf45f"
    assert set(v2.payload) - set(v1.payload) == {"execution_authorized"}
    assert v2.payload["execution_authorized"] is False
    assert v2.payload["limits"]["provider_requests_per_run"] == 16
    assert v2.payload["stopping_rules"][0] == "runner_diagnosis_failed"
    for v1_task, v2_task in zip(v1.tasks, v2.tasks, strict=True):
        assert v1_task.prompt_file == "PROMPT.txt"
        assert v2_task.prompt_file == "PROMPT-v2.txt"
        assert v1_task.prompt_sha256 != v2_task.prompt_sha256
        v2_prompt = (
            manifest_module.FIXTURES_ROOT / v2_task.task_id / v2_task.prompt_file
        ).read_text()
        assert "diagnose.py" not in v2_prompt


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.__setitem__("execution_authorized", True),
            "execution_authorized must remain false",
        ),
        (
            lambda payload: payload["limits"].__setitem__("provider_requests_per_run", 8),
            "request/time limits",
        ),
        (
            lambda payload: payload.__setitem__("random_seed", "0" * 64),
            "random_seed differs",
        ),
        (
            lambda payload: payload["tasks"][0].__setitem__("prompt_file", "PROMPT.txt"),
            "layout differs",
        ),
    ],
)
def test_v2_protocol_drift_is_rejected(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    payload = json.loads(DEFAULT_V2_MANIFEST_PATH.read_text(encoding="utf-8"))
    mutate(payload)

    with pytest.raises(ManifestError, match=message):
        validate_manifest_json(payload)
