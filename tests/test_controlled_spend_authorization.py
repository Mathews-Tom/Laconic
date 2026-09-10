from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import tools.controlled_spend.__main__ as cli_module
import tools.controlled_spend.runner as runner_module
from tools.controlled_spend.authorization import (
    ExecutionAuthorization,
    PilotAuthorizationError,
    consume_execution_authorization,
    load_execution_authorization,
    verify_execution_authorization,
)
from tools.controlled_spend.manifest import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_V2_MANIFEST_PATH,
    PilotManifest,
    canonical_json,
    validate_manifest_file,
)
from tools.controlled_spend.runner import PilotRunnerError, run_campaign


def _v2() -> PilotManifest:
    return validate_manifest_file(DEFAULT_V2_MANIFEST_PATH)


def _document(manifest: PilotManifest, artifact_root: Path, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "artifact_root": str(artifact_root),
        "authorization_id": "a1" * 32,
        "authorized_at": "2026-09-09T15:00:00Z",
        "execution_authorized": True,
        "manifest_sha256": manifest.digest,
        "schema_version": 1,
        "single_use": True,
        "study_id": "m20-variance-pilot-v2",
        "total_spend_usd": "10.00",
    }
    document.update(overrides)
    return document


def _write_receipt(
    directory: Path,
    document: dict[str, Any],
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    raw: bytes | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "authorization.json"
    path.write_bytes(canonical_json(document) if raw is None else raw)
    os.chmod(path, mode)
    os.chmod(directory, directory_mode)
    return path


def _accepted(tmp_path: Path) -> tuple[ExecutionAuthorization, PilotManifest, Path, Path]:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    receipt = _write_receipt(tmp_path / "auth", _document(manifest, artifact_root))
    authorization = load_execution_authorization(
        receipt,
        manifest=manifest,
        artifact_root=artifact_root,
    )
    return authorization, manifest, artifact_root, receipt


def test_valid_receipt_binds_the_frozen_manifest_root_and_cap(tmp_path: Path) -> None:
    authorization, manifest, artifact_root, receipt = _accepted(tmp_path)

    assert authorization.manifest_sha256 == manifest.digest
    assert authorization.study_id == "m20-variance-pilot-v2"
    assert authorization.artifact_root == artifact_root.absolute()
    assert str(authorization.total_spend_usd) == "10.00"
    assert authorization.receipt_path == receipt.absolute()
    assert authorization.authorization_id == "a1" * 32


def test_missing_receipt_is_refused(tmp_path: Path) -> None:
    manifest = _v2()

    with pytest.raises(PilotAuthorizationError, match="cannot stat authorization receipt"):
        load_execution_authorization(
            tmp_path / "absent.json",
            manifest=manifest,
            artifact_root=tmp_path / "campaign",
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document.__setitem__("manifest_sha256", "b" * 64),
            "bound to a different manifest",
        ),
        (
            lambda document: document.__setitem__("study_id", "m20-variance-pilot-v1"),
            "names a different study",
        ),
        (
            lambda document: document.__setitem__("artifact_root", "/tmp/somewhere-else"),
            "bound to a different artifact root",
        ),
        (
            lambda document: document.__setitem__("total_spend_usd", "25.00"),
            "cap differs from the frozen campaign cap",
        ),
        (
            lambda document: document.__setitem__("total_spend_usd", 10),
            "total_spend_usd must be a decimal string",
        ),
        (
            lambda document: document.__setitem__("authorized_at", "yesterday"),
            "must be an RFC 3339 timestamp",
        ),
        (
            lambda document: document.__setitem__("authorized_at", "2026-09-09T15:00:00"),
            "must carry an explicit UTC offset",
        ),
        (
            lambda document: document.__setitem__("execution_authorized", False),
            "does not authorize execution",
        ),
        (
            lambda document: document.__setitem__("single_use", False),
            "must be single use",
        ),
        (
            lambda document: document.__setitem__("schema_version", 2),
            "schema_version is not supported",
        ),
        (
            lambda document: document.__setitem__("authorization_id", "short"),
            "authorization_id must be a 64-character lowercase hexadecimal value",
        ),
        (
            lambda document: document.__setitem__("unexpected", 1),
            "keys differ",
        ),
        (
            lambda document: document.pop("single_use"),
            "keys differ",
        ),
    ],
)
def test_receipt_field_violations_are_refused(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object], message: str
) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    document = _document(manifest, artifact_root)
    mutate(document)
    receipt = _write_receipt(tmp_path / "auth", document)

    with pytest.raises(PilotAuthorizationError, match=message):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_non_canonical_receipt_serialization_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    document = _document(manifest, artifact_root)
    receipt = _write_receipt(
        tmp_path / "auth",
        document,
        raw=json.dumps(document, indent=2).encode(),
    )

    with pytest.raises(PilotAuthorizationError, match="canonical JSON serialization"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_receipt_with_wrong_mode_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    receipt = _write_receipt(tmp_path / "auth", _document(manifest, artifact_root), mode=0o644)

    with pytest.raises(PilotAuthorizationError, match="receipt must have mode 0600"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_receipt_in_a_group_readable_directory_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    receipt = _write_receipt(
        tmp_path / "auth",
        _document(manifest, artifact_root),
        directory_mode=0o755,
    )

    with pytest.raises(PilotAuthorizationError, match="directory must have mode 0700"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_symlinked_receipt_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    real = _write_receipt(tmp_path / "auth", _document(manifest, artifact_root))
    link_directory = tmp_path / "link"
    link_directory.mkdir(mode=0o700)
    link = link_directory / "authorization.json"
    link.symlink_to(real)

    with pytest.raises(PilotAuthorizationError, match="non-symlink regular file"):
        load_execution_authorization(link, manifest=manifest, artifact_root=artifact_root)


def test_receipt_inside_the_artifact_root_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    receipt = _write_receipt(artifact_root / "auth", _document(manifest, artifact_root))

    with pytest.raises(PilotAuthorizationError, match="must live outside"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_receipt_inside_a_live_state_root_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    live_root = tmp_path / "live"
    receipt = _write_receipt(live_root / "auth", _document(manifest, artifact_root))

    with pytest.raises(PilotAuthorizationError, match="must live outside"):
        load_execution_authorization(
            receipt,
            manifest=manifest,
            artifact_root=artifact_root,
            excluded_roots=(live_root,),
        )


def test_v1_manifest_can_never_be_authorized(tmp_path: Path) -> None:
    v1 = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    artifact_root = tmp_path / "campaign"
    receipt = _write_receipt(tmp_path / "auth", _document(_v2(), artifact_root))

    with pytest.raises(PilotAuthorizationError, match="only the M20-v2 pilot may be executed"):
        load_execution_authorization(receipt, manifest=v1, artifact_root=artifact_root)


def test_existing_artifact_root_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    artifact_root.mkdir(mode=0o700)
    receipt = _write_receipt(tmp_path / "auth", _document(manifest, artifact_root))

    with pytest.raises(PilotAuthorizationError, match="artifact root already exists"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_consumption_removes_the_receipt_and_blocks_reuse(tmp_path: Path) -> None:
    authorization, manifest, artifact_root, receipt = _accepted(tmp_path)

    consume_execution_authorization(authorization)

    assert not receipt.exists()
    with pytest.raises(PilotAuthorizationError, match="cannot stat authorization receipt"):
        load_execution_authorization(receipt, manifest=manifest, artifact_root=artifact_root)


def test_consumption_refuses_a_receipt_edited_after_validation(tmp_path: Path) -> None:
    authorization, manifest, artifact_root, receipt = _accepted(tmp_path)
    receipt.write_bytes(canonical_json(_document(manifest, artifact_root, single_use=False)))
    os.chmod(receipt, 0o600)

    with pytest.raises(PilotAuthorizationError, match="changed after validation"):
        consume_execution_authorization(authorization)


def test_verification_rebinds_manifest_root_and_cap(tmp_path: Path) -> None:
    authorization, manifest, artifact_root, _ = _accepted(tmp_path)

    verify_execution_authorization(authorization, manifest=manifest, artifact_root=artifact_root)

    with pytest.raises(PilotAuthorizationError, match="different artifact root"):
        verify_execution_authorization(
            authorization,
            manifest=manifest,
            artifact_root=tmp_path / "other",
        )
    with pytest.raises(PilotAuthorizationError, match="only the M20-v2 pilot may be executed"):
        verify_execution_authorization(
            authorization,
            manifest=validate_manifest_file(DEFAULT_MANIFEST_PATH),
            artifact_root=artifact_root,
        )


def test_v1_campaign_execution_is_permanently_closed(tmp_path: Path) -> None:
    authorization, _, artifact_root, _ = _accepted(tmp_path)

    with pytest.raises(PilotRunnerError, match="permanently closed"):
        run_campaign(
            artifact_root,
            manifest=validate_manifest_file(DEFAULT_MANIFEST_PATH),
            authorization=authorization,
            live_agent_database=tmp_path / "agent" / "agent.db",
        )

    assert not artifact_root.exists()


def test_mismatched_authorization_stops_before_preflight_and_root_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, manifest, artifact_root, receipt = _accepted(tmp_path)

    def forbidden_preflight(_: object) -> None:
        raise AssertionError("environment preflight must not run")

    monkeypatch.setattr(runner_module, "preflight_environment", forbidden_preflight)

    with pytest.raises(PilotAuthorizationError, match="different artifact root"):
        run_campaign(
            tmp_path / "elsewhere",
            manifest=manifest,
            authorization=authorization,
            live_agent_database=tmp_path / "agent" / "agent.db",
        )

    assert not artifact_root.exists()
    assert not (tmp_path / "elsewhere").exists()
    assert receipt.exists()


def test_authorized_campaign_records_the_receipt_then_consumes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization, manifest, artifact_root, receipt = _accepted(tmp_path)
    live_root = tmp_path / "live"
    live_root.mkdir()
    agent_directory = tmp_path / "agent"
    agent_directory.mkdir()
    monkeypatch.setattr(runner_module, "resolve_data_dir", lambda: live_root)
    monkeypatch.setattr(runner_module, "preflight_environment", lambda _: None)

    def fake_snapshot(source: Path, destination: Path) -> str:
        destination.write_bytes(b"snapshot")
        return "c" * 64

    def fake_copy(snapshot: Path, destination: Path) -> None:
        destination.write_bytes(snapshot.read_bytes())

    monkeypatch.setattr(runner_module, "snapshot_agent_database", fake_snapshot)
    monkeypatch.setattr(runner_module, "_copy_credential_snapshot", fake_copy)
    monkeypatch.setattr(runner_module, "_credential_preflight", lambda *_: None)

    def stop_before_provider_work(*_: object, **__: object) -> None:
        raise PilotRunnerError("halted for the test after authorization was consumed")

    monkeypatch.setattr(runner_module, "_execute_run", stop_before_provider_work)

    with pytest.raises(PilotRunnerError, match="halted for the test"):
        run_campaign(
            artifact_root,
            manifest=manifest,
            authorization=authorization,
            live_agent_database=agent_directory / "agent.db",
        )

    state = json.loads((artifact_root / "campaign-state.json").read_text())
    assert state["authorization_id"] == authorization.authorization_id
    assert state["authorization_sha256"] == authorization.receipt_sha256
    assert state["manifest_hash"] == manifest.digest
    assert not receipt.exists()


def test_cli_run_requires_an_explicit_authorization_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controlled-spend",
            "pilot",
            "run",
            "--manifest",
            str(DEFAULT_V2_MANIFEST_PATH),
            "--artifact-root",
            str(tmp_path / "campaign"),
        ],
    )

    with pytest.raises(SystemExit) as failure:
        cli_module.main()

    assert failure.value.code == 2
    assert not (tmp_path / "campaign").exists()


def test_receipt_reached_through_a_symlinked_prefix_is_refused(tmp_path: Path) -> None:
    manifest = _v2()
    artifact_root = tmp_path / "campaign"
    real_directory = tmp_path / "inside" / "auth"
    receipt = _write_receipt(real_directory, _document(manifest, artifact_root))
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "inside", target_is_directory=True)

    with pytest.raises(PilotAuthorizationError, match="must live outside"):
        load_execution_authorization(
            alias / "auth" / receipt.name,
            manifest=manifest,
            artifact_root=artifact_root,
            excluded_roots=(tmp_path / "inside",),
        )
