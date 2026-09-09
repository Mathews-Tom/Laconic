from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import tools.controlled_spend.__main__ as cli_module
from laconic.runtime.storage import RuntimeStorage
from tools.controlled_spend.analysis import AnalysisError, check_report, generate_report
from tools.controlled_spend.manifest import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_V2_MANIFEST_PATH,
    PilotManifest,
    RunSpec,
    canonical_json,
    validate_manifest_file,
)
from tools.controlled_spend.privacy import PrivacyViolationError, validate_public_report


def _cost_for(run: RunSpec) -> float:
    task = int(run.task_id[-1])
    repetition = run.repetition * 0.01
    if run.arm == "native":
        return task + repetition
    if run.arm == "laconic":
        return task * (0.80 + task * 0.04) + repetition
    return task * (1.05 - task * 0.03) + repetition


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(payload))


def _write_transcript(
    run_root: Path, run: RunSpec, *, first_command: str = "python3 diagnose.py"
) -> None:
    cost = _cost_for(run)
    usage = {
        "cacheRead": 0,
        "cacheWrite": 0,
        "cost": {
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "input": cost,
            "output": 0.0,
            "total": cost,
        },
        "cttl": 0,
        "input": 10,
        "output": 1,
        "reasoningTokens": 0,
        "totalTokens": 11,
    }
    records = [
        {"type": "session", "id": f"session-{run.run_id}"},
        {
            "type": "message",
            "message": {
                "content": [
                    {
                        "arguments": {"command": first_command},
                        "id": f"call-{run.run_id}",
                        "name": "bash",
                        "type": "toolCall",
                    }
                ],
                "model": "claude-sonnet-5",
                "provider": "anthropic",
                "role": "assistant",
                "usage": usage,
            },
        },
    ]
    session = run_root / "sessions" / f"{run.run_id}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("".join(json.dumps(row) + "\n" for row in records))


def _write_laconic_mechanism(run_root: Path, run: RunSpec) -> None:
    storage = RuntimeStorage(run_root / "laconic-data")
    with storage.open_ledger(f"session-{run.run_id}") as ledger:
        ledger.record_runtime_decision(
            sequence=1,
            request_id=f"request-{run.run_id}",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=f"session-{run.run_id}/F1",
            raw_chars=1000,
            visible_chars=100,
            latency_ms=1.0,
        )


def _write_headroom_mechanism(run_root: Path) -> None:
    _write_json(
        run_root / "headroom" / "requests.jsonl",
        {
            "input_tokens_optimized": 90,
            "input_tokens_original": 100,
            "transforms_applied": ["coding"],
        },
    )


def _seed_campaign(root: Path, *, manifest_path: Path = DEFAULT_MANIFEST_PATH) -> PilotManifest:
    manifest = validate_manifest_file(manifest_path)
    schema_version = manifest.payload["schema_version"]
    root.mkdir(mode=0o700)
    receipts: list[dict[str, Any]] = []
    for sequence, run in enumerate(manifest.run_order, start=1):
        run_root = root / "runs" / run.run_id
        run_root.mkdir(parents=True, mode=0o700)
        result = {
            "arm": run.arm,
            "completion_passed": True,
            "fixture_guards_passed": True,
            "gateway_halted_reason": None,
            "gateway_request_count": sequence,
            "gateway_spent_usd": f"{sequence / 100:.2f}",
            "passed": True,
            "process_returncode": 0,
            "process_timed_out": False,
            "run_id": run.run_id,
            "wall_seconds": 1.0,
        }
        if schema_version == 2:
            result["runner_diagnosis_passed"] = True
            _write_json(
                run_root / "diagnosis-result.json",
                {
                    "baseline_failed": True,
                    "command": ["python3", "diagnose.py"],
                    "returncode": 1,
                    "timed_out": False,
                    "wall_seconds": 0.1,
                },
            )
        _write_json(run_root / "run-result.json", result)
        _write_transcript(
            run_root,
            run,
            first_command=("cat pyproject.toml" if schema_version == 2 else "python3 diagnose.py"),
        )
        if run.arm == "laconic":
            _write_laconic_mechanism(run_root, run)
        elif run.arm == "headroom":
            _write_headroom_mechanism(run_root)
        receipts.append(
            {
                "charged_cost_usd": "0.01",
                "request_sha256": "0" * 64,
                "reserved_cost_usd": "0.05",
                "run_id": run.run_id,
                "sequence": sequence,
                "status": 200,
                "stop_reason": None,
                "usage": {
                    "cache_read_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "cache_write_5m_tokens": 0,
                    "input_tokens": 10,
                    "output_tokens": 1,
                },
                "usage_valid": True,
            }
        )
    (root / "gateway-receipts.jsonl").write_bytes(b"".join(canonical_json(row) for row in receipts))
    live_state = {"laconic_runtime": "1" * 64, "omp_agent": "2" * 64}
    _write_json(
        root / "campaign-state.json",
        {
            "completed_runs": [run.run_id for run in manifest.run_order],
            "credential_snapshot_sha256": "3" * 64,
            "gateway_spent_usd": "0.24",
            "live_state_after": live_state,
            "live_state_before": live_state,
            "manifest_hash": manifest.digest,
            "schema_version": schema_version,
            "status": "completed",
        },
    )
    return manifest


def test_complete_report_is_variance_only_and_reproducible(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    report_json = tmp_path / "report.json"
    report_markdown = tmp_path / "report.md"

    report = generate_report(artifacts, report_json, report_markdown, manifest=manifest)

    assert report["verdict"] == "complete"
    assert report["run_count"] == report["expected_run_count"] == len(manifest.run_order)
    assert report["completion_failures"] == report["mechanism_failures"] == 0
    assert report["paired_log_cost_sd"] > 0
    assert report["confirmatory_task_count_at_two_repeats"] >= 2
    assert set(report) == set(manifest.payload["public_report_keys"])
    assert not ({"arm_means", "effect", "paired_difference", "cost_by_arm"} & set(report))
    assert check_report(artifacts, report_json, report_markdown, manifest=manifest) == report
    assert (artifacts / "analysis-private.json").stat().st_mode & 0o777 == 0o600
    assert "not a performance result" in report_markdown.read_text()


def test_incomplete_campaign_preserves_gateway_spend(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    completed = manifest.run_order[:3]
    receipt_path = artifacts / "gateway-receipts.jsonl"
    receipt_path.write_bytes(b"".join(receipt_path.read_bytes().splitlines(keepends=True)[:3]))
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["completed_runs"] = [run.run_id for run in completed]
    state["status"] = "incomplete"
    state.pop("gateway_spent_usd")
    _write_json(state_path, state)

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["run_count"] == 3
    assert report["gateway_spend_usd"] == pytest.approx(0.03)
    assert report["completion_failures"] + report["mechanism_failures"] == 21


@pytest.mark.parametrize("mutation", ["missing_receipt", "duplicate_receipt"])
def test_receipt_population_mutations_suppress_statistics(tmp_path: Path, mutation: str) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    receipt_path = artifacts / "gateway-receipts.jsonl"
    lines = receipt_path.read_bytes().splitlines(keepends=True)
    if mutation == "missing_receipt":
        receipt_path.write_bytes(b"".join(lines[:-1]))
    else:
        receipt_path.write_bytes(b"".join((*lines, lines[0])))

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    if mutation == "missing_receipt":
        assert report["run_count"] == 23
        assert report["mechanism_failures"] == 1
        assert report["gateway_spend_usd"] == pytest.approx(0.23)
    else:
        assert report["run_count"] == 0
        assert report["mechanism_failures"] == len(
            validate_manifest_file(DEFAULT_MANIFEST_PATH).run_order
        )
        assert report["gateway_spend_usd"] == 0.0
    assert report["paired_log_cost_sd"] is None
    assert report["confirmatory_task_count_at_two_repeats"] is None


def test_differential_completion_is_an_incomplete_disposition(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    failed = manifest.run_order[0]
    result_path = artifacts / "runs" / failed.run_id / "run-result.json"
    result = json.loads(result_path.read_text())
    result["passed"] = False
    result["completion_passed"] = False
    _write_json(result_path, result)

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["completion_failures"] == 1
    assert report["paired_log_cost_sd"] is None


def test_missing_mechanism_is_an_incomplete_disposition(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    failed = next(run for run in manifest.run_order if run.arm == "laconic")
    ledger_root = artifacts / "runs" / failed.run_id / "laconic-data"
    for path in sorted(ledger_root.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    ledger_root.rmdir()

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["mechanism_failures"] == 1
    assert report["paired_log_cost_sd"] is None


def test_malformed_omp_usage_suppresses_statistics(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    failed = manifest.run_order[0]
    transcript = artifacts / "runs" / failed.run_id / "sessions" / f"{failed.run_id}.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    del records[1]["message"]["usage"]["cost"]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["mechanism_failures"] == 1
    assert report["paired_log_cost_sd"] is None


def test_non_diagnostic_first_tool_action_suppresses_statistics(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    failed = manifest.run_order[0]
    transcript = artifacts / "runs" / failed.run_id / "sessions" / f"{failed.run_id}.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    records[1]["message"]["content"][0]["arguments"]["command"] = "cat diagnose.py"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["mechanism_failures"] == 1
    assert report["paired_log_cost_sd"] is None


def test_headroom_numeric_pass_through_is_valid_mechanism_evidence(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    headroom = next(run for run in manifest.run_order if run.arm == "headroom")
    log_path = artifacts / "runs" / headroom.run_id / "headroom" / "requests.jsonl"
    _write_json(
        log_path,
        {
            "input_tokens_optimized": 100,
            "input_tokens_original": 100,
            "transforms_applied": [],
        },
    )

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "complete"


def test_gateway_failure_and_completion_failure_partition_cells(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    receipt_path = artifacts / "gateway-receipts.jsonl"
    receipt_path.write_bytes(b"not-json\n")
    failed = manifest.run_order[0]
    result_path = artifacts / "runs" / failed.run_id / "run-result.json"
    result = json.loads(result_path.read_text())
    result["passed"] = False
    _write_json(result_path, result)

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["completion_failures"] == 1
    assert report["mechanism_failures"] == 23
    assert report["completion_failures"] + report["mechanism_failures"] == 24


def test_undefined_correlation_yields_incomplete_disposition(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    for run in manifest.run_order:
        if run.arm != "native":
            continue
        transcript = artifacts / "runs" / run.run_id / "sessions" / f"{run.run_id}.jsonl"
        records = [json.loads(line) for line in transcript.read_text().splitlines()]
        cost = records[1]["message"]["usage"]["cost"]
        cost.update({"cacheRead": 0.0, "cacheWrite": 0.0, "input": 1.0, "output": 0.0})
        cost["total"] = 1.0
        transcript.write_text("".join(json.dumps(row) + "\n" for row in records))

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["completion_failures"] == report["mechanism_failures"] == 0
    assert report["paired_log_cost_sd"] is None
    assert report["confirmatory_task_count_at_two_repeats"] is None


def test_public_privacy_gate_rejects_arm_cost_field(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )
    report["native_mean_cost_usd"] = 1.0

    with pytest.raises(PrivacyViolationError, match="extra=.*native_mean_cost_usd"):
        validate_public_report(report, manifest=manifest)


def test_live_state_drift_forces_incomplete_disposition(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["live_state_after"] = {"laconic_runtime": "4" * 64, "omp_agent": "2" * 64}
    state["status"] = "incomplete"
    _write_json(state_path, state)

    report = generate_report(
        artifacts, tmp_path / "report.json", tmp_path / "report.md", manifest=manifest
    )

    assert report["verdict"] == "incomplete"
    assert report["paired_log_cost_sd"] is None


def test_campaign_state_manifest_mismatch_is_refused(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["manifest_hash"] = "0" * 64
    _write_json(state_path, state)

    with pytest.raises(AnalysisError, match="does not match selected manifest"):
        generate_report(
            artifacts,
            tmp_path / "report.json",
            tmp_path / "report.md",
            manifest=manifest,
        )


def test_public_report_manifest_mismatch_is_refused(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts)
    report = generate_report(
        artifacts,
        tmp_path / "report.json",
        tmp_path / "report.md",
        manifest=manifest,
    )
    different = PilotManifest(
        payload={**manifest.payload, "study_id": "different-campaign"},
        tasks=manifest.tasks,
        run_order=manifest.run_order,
    )

    with pytest.raises(PrivacyViolationError, match="does not match the selected manifest"):
        validate_public_report(report, manifest=different)


def test_v2_complete_report_accepts_runner_side_diagnosis(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    report_json = tmp_path / "report-v2.json"
    report_markdown = tmp_path / "report-v2.md"

    report = generate_report(
        artifacts,
        report_json,
        report_markdown,
        manifest=manifest,
    )

    assert report["verdict"] == "complete"
    assert report["attempted_cells"] == report["valid_cells"] == len(manifest.run_order)
    assert report["unrun_cells"] == 0
    assert (
        report["task_completion_failures"]
        == report["protocol_failures"]
        == report["mechanism_non_engagement"]
        == 0
    )
    assert report["paired_log_cost_sd"] > 0
    assert set(report) == set(manifest.payload["public_report_keys"])
    assert check_report(artifacts, report_json, report_markdown, manifest=manifest) == report
    assert "Controlled Spend Variance Pilot v2" in report_markdown.read_text()


def test_v2_partial_campaign_partitions_unrun_and_task_failures(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    attempted = manifest.run_order[:3]
    for run in manifest.run_order[3:]:
        shutil.rmtree(artifacts / "runs" / run.run_id)
    receipt_path = artifacts / "gateway-receipts.jsonl"
    receipt_path.write_bytes(b"".join(receipt_path.read_bytes().splitlines(keepends=True)[:3]))
    failed = attempted[-1]
    result_path = artifacts / "runs" / failed.run_id / "run-result.json"
    result = json.loads(result_path.read_text())
    result["completion_passed"] = False
    result["passed"] = False
    _write_json(result_path, result)
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["completed_runs"] = [run.run_id for run in attempted[:-1]]
    state["failed_run"] = failed.run_id
    state["gateway_halted_reason"] = None
    state["status"] = "incomplete"
    state.pop("gateway_spent_usd")
    _write_json(state_path, state)

    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )

    assert report["verdict"] == "incomplete"
    assert report["attempted_cells"] == 3
    assert report["valid_cells"] == 2
    assert report["unrun_cells"] == 21
    assert report["task_completion_failures"] == 1
    assert report["protocol_failures"] == report["mechanism_non_engagement"] == 0
    assert report["gateway_spend_usd"] == pytest.approx(0.03)
    assert report["paired_log_cost_sd"] is None


def test_v2_runner_diagnosis_failure_is_protocol_failure(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    failed = manifest.run_order[0]
    diagnosis_path = artifacts / "runs" / failed.run_id / "diagnosis-result.json"
    diagnosis = json.loads(diagnosis_path.read_text())
    diagnosis["baseline_failed"] = False
    diagnosis["returncode"] = 0
    _write_json(diagnosis_path, diagnosis)

    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )

    assert report["verdict"] == "incomplete"
    assert report["protocol_failures"] == 1
    assert report["valid_cells"] == 23
    assert report["task_completion_failures"] == report["mechanism_non_engagement"] == 0


def test_v2_missing_mechanism_is_distinct_from_protocol_failure(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    failed = next(run for run in manifest.run_order if run.arm == "laconic")
    shutil.rmtree(artifacts / "runs" / failed.run_id / "laconic-data")

    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )

    assert report["verdict"] == "incomplete"
    assert report["mechanism_non_engagement"] == 1
    assert report["valid_cells"] == 23
    assert report["task_completion_failures"] == report["protocol_failures"] == 0


def test_v2_failed_receipt_preserves_charged_spend(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    receipt_path = artifacts / "gateway-receipts.jsonl"
    receipts = [json.loads(line) for line in receipt_path.read_text().splitlines()]
    receipts[-1].update(
        {
            "charged_cost_usd": "0.02",
            "status": 500,
            "stop_reason": "upstream_non_success",
            "usage": None,
            "usage_valid": False,
        }
    )
    receipt_path.write_bytes(b"".join(canonical_json(receipt) for receipt in receipts))
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["completed_runs"] = [run.run_id for run in manifest.run_order[:-1]]
    state["failed_run"] = manifest.run_order[-1].run_id
    state["gateway_halted_reason"] = "upstream_non_success"
    state["status"] = "incomplete"
    state.pop("gateway_spent_usd")
    _write_json(state_path, state)

    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )

    assert report["verdict"] == "incomplete"
    assert report["protocol_failures"] == 1
    assert report["valid_cells"] == 23
    assert report["gateway_spend_usd"] == pytest.approx(0.25)


def test_v2_diagnosis_failure_without_receipt_reports_zero_spend(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    failed = manifest.run_order[0]
    for run in manifest.run_order[1:]:
        shutil.rmtree(artifacts / "runs" / run.run_id)
    run_root = artifacts / "runs" / failed.run_id
    (run_root / "run-result.json").unlink()
    shutil.rmtree(run_root / "sessions")
    shutil.rmtree(run_root / "headroom")
    diagnosis_path = run_root / "diagnosis-result.json"
    diagnosis = json.loads(diagnosis_path.read_text())
    diagnosis["baseline_failed"] = False
    diagnosis["returncode"] = 0
    _write_json(diagnosis_path, diagnosis)
    (artifacts / "gateway-receipts.jsonl").unlink()
    state_path = artifacts / "campaign-state.json"
    state = json.loads(state_path.read_text())
    state["completed_runs"] = []
    state["failed_run"] = failed.run_id
    state["gateway_halted_reason"] = "runner_diagnosis_failed"
    state["status"] = "incomplete"
    state.pop("gateway_spent_usd")
    _write_json(state_path, state)

    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )

    assert report["attempted_cells"] == 1
    assert report["protocol_failures"] == 1
    assert report["unrun_cells"] == 23
    assert report["gateway_spend_usd"] == 0.0


def test_v2_malformed_receipt_cannot_fabricate_zero_spend(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    (artifacts / "gateway-receipts.jsonl").write_bytes(b"not-json\n")

    with pytest.raises(AnalysisError, match="receipt 1 is malformed"):
        generate_report(
            artifacts,
            tmp_path / "report-v2.json",
            tmp_path / "report-v2.md",
            manifest=manifest,
        )


def test_v2_privacy_gate_rejects_invalid_cell_partition(tmp_path: Path) -> None:
    artifacts = tmp_path / "private"
    manifest = _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    report = generate_report(
        artifacts,
        tmp_path / "report-v2.json",
        tmp_path / "report-v2.md",
        manifest=manifest,
    )
    report["unrun_cells"] = 1

    with pytest.raises(PrivacyViolationError, match="do not partition"):
        validate_public_report(report, manifest=manifest)


def test_v2_cli_report_and_check_render_cell_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = tmp_path / "private"
    _seed_campaign(artifacts, manifest_path=DEFAULT_V2_MANIFEST_PATH)
    report_json = tmp_path / "report-v2.json"
    report_markdown = tmp_path / "report-v2.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controlled-spend",
            "pilot",
            "report",
            "--manifest",
            str(DEFAULT_V2_MANIFEST_PATH),
            "--artifact-root",
            str(artifacts),
            "--output-json",
            str(report_json),
            "--output-markdown",
            str(report_markdown),
        ],
    )

    assert cli_module.main() == 0
    assert "cells=24/24 attempted=24" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controlled-spend",
            "pilot",
            "check",
            "--manifest",
            str(DEFAULT_V2_MANIFEST_PATH),
            "--artifact-root",
            str(artifacts),
            "--report-json",
            str(report_json),
            "--report-markdown",
            str(report_markdown),
        ],
    )
    assert cli_module.main() == 0
    assert "cells=24/24 attempted=24 report=verified" in capsys.readouterr().out
