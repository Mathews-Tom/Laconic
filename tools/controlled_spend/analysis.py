"""Strict private analysis and variance-only public reporting for M20."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import NormalDist, correlation, mean, stdev
from typing import Any, Final, cast

from laconic.runtime.operator import runtime_storage_status
from laconic.spend.omp import MalformedSessionError, load_session
from tools.controlled_spend.manifest import ARMS, Arm, PilotManifest, RunSpec, canonical_json
from tools.controlled_spend.privacy import validate_public_report

_PRIVATE_REPORT_NAME: Final = "analysis-private.json"
_EXPECTED_MODEL: Final = "claude-sonnet-5"
_EXPECTED_PROVIDER: Final = "anthropic"
_HEADROOM_CONTENT_KEYS: Final = frozenset(
    {"request_messages", "compressed_messages", "response_content"}
)
_GATEWAY_RECEIPT_KEYS: Final = frozenset(
    {
        "charged_cost_usd",
        "request_sha256",
        "reserved_cost_usd",
        "run_id",
        "sequence",
        "status",
        "stop_reason",
        "usage",
        "usage_valid",
    }
)
_GATEWAY_USAGE_KEYS: Final = frozenset(
    {
        "cache_read_tokens",
        "cache_write_1h_tokens",
        "cache_write_5m_tokens",
        "input_tokens",
        "output_tokens",
    }
)
_LOWER_HEX: Final = frozenset("0123456789abcdef")


class AnalysisError(ValueError):
    """Raised when private pilot evidence is malformed or unsafe to analyze."""


class UndefinedCorrelationError(AnalysisError):
    """Raised when a complete cell set cannot support a correlation."""


@dataclass(frozen=True, slots=True)
class RunMetrics:
    run_id: str
    task_id: str
    repeat: int
    arm: Arm
    cost_usd: float
    assistant_turns: int
    provider_requests: int
    mechanism: dict[str, int]


@dataclass(frozen=True, slots=True)
class GatewayEvidence:
    counts: Counter[str]
    spent: Decimal
    protocol_failure_run_ids: frozenset[str]


def _ordinary_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AnalysisError(f"required private artifact is unavailable: {path.name}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AnalysisError(f"private artifact is not an ordinary file: {path.name}")


def _read_json_object(path: Path) -> dict[str, Any]:
    _ordinary_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisError(f"private artifact is not valid JSON: {path.name}") from error
    if not isinstance(payload, dict):
        raise AnalysisError(f"private artifact is not a JSON object: {path.name}")
    return cast(dict[str, Any], payload)


def _load_gateway_request_counts(
    artifact_root: Path, manifest: PilotManifest
) -> tuple[Counter[str], Decimal]:
    path = artifact_root / "gateway-receipts.jsonl"
    _ordinary_file(path)
    expected = {run.run_id for run in manifest.run_order}
    counts: Counter[str] = Counter()
    sequences: list[int] = []
    spent = Decimal(0)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisError("gateway receipts are unreadable") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
            sequence = payload["sequence"]
            run_id = payload["run_id"]
            charged = Decimal(payload["charged_cost_usd"])
        except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as error:
            raise AnalysisError(f"gateway receipt {line_number} is malformed") from error
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(run_id, str)
            or run_id not in expected
            or not charged.is_finite()
            or charged < 0
            or payload.get("usage_valid") is not True
            or payload.get("stop_reason") is not None
            or not isinstance(payload.get("status"), int)
            or not 200 <= payload["status"] < 300
            or not isinstance(payload.get("usage"), dict)
        ):
            raise AnalysisError(f"gateway receipt {line_number} is invalid")
        sequences.append(sequence)
        counts[run_id] += 1
        spent += charged
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        raise AnalysisError("gateway receipt sequences are duplicated or incomplete")
    request_limit = cast(dict[str, Any], manifest.payload["limits"])["provider_requests_per_run"]
    if any(count > request_limit for count in counts.values()):
        raise AnalysisError("gateway requests exceed the frozen per-run limit")
    return counts, spent


def _load_v2_gateway_evidence(artifact_root: Path, manifest: PilotManifest) -> GatewayEvidence:
    path = artifact_root / "gateway-receipts.jsonl"
    _ordinary_file(path)
    expected = {run.run_id for run in manifest.run_order}
    counts: Counter[str] = Counter()
    sequences: list[int] = []
    spent = Decimal(0)
    protocol_failure_run_ids: set[str] = set()
    receipt_run_ids: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisError("gateway receipts are unreadable") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
            sequence = payload["sequence"]
            run_id = payload["run_id"]
            charged_raw = payload["charged_cost_usd"]
            reserved_raw = payload["reserved_cost_usd"]
            charged = Decimal(charged_raw)
            reserved = Decimal(reserved_raw)
            request_hash = payload["request_sha256"]
            status = payload["status"]
            stop_reason = payload["stop_reason"]
            usage = payload["usage"]
            usage_valid = payload["usage_valid"]
        except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as error:
            raise AnalysisError(f"gateway receipt {line_number} is malformed") from error
        invalid_usage = isinstance(usage, dict) and (
            set(usage) != _GATEWAY_USAGE_KEYS
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != _GATEWAY_RECEIPT_KEYS
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(run_id, str)
            or run_id not in expected
            or not isinstance(charged_raw, str)
            or not charged.is_finite()
            or charged < 0
            or not isinstance(reserved_raw, str)
            or not reserved.is_finite()
            or reserved < 0
            or not isinstance(request_hash, str)
            or len(request_hash) != 64
            or set(request_hash) - _LOWER_HEX
            or isinstance(status, bool)
            or not isinstance(status, int)
            or (stop_reason is not None and not isinstance(stop_reason, str))
            or not isinstance(usage_valid, bool)
            or (usage is not None and not isinstance(usage, dict))
            or invalid_usage
            or (usage_valid and not isinstance(usage, dict))
            or (not usage_valid and usage is not None)
        ):
            raise AnalysisError(f"gateway receipt {line_number} is invalid")
        sequences.append(sequence)
        receipt_run_ids.append(run_id)
        counts[run_id] += 1
        spent += charged
        if (
            not usage_valid
            or stop_reason is not None
            or not 200 <= status < 300
            or charged > reserved
        ):
            protocol_failure_run_ids.add(run_id)
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        raise AnalysisError("gateway receipt sequences are duplicated or incomplete")
    run_indices = {run.run_id: index for index, run in enumerate(manifest.run_order)}
    receipt_indices = [run_indices[run_id] for run_id in receipt_run_ids]
    if receipt_indices != sorted(receipt_indices):
        raise AnalysisError("gateway receipts do not follow the frozen run order")
    unique_indices = sorted(set(receipt_indices))
    if unique_indices and unique_indices != list(range(unique_indices[-1] + 1)):
        raise AnalysisError("gateway receipts skip an earlier frozen run")
    limits = cast(dict[str, Any], manifest.payload["limits"])
    request_limit = limits["provider_requests_per_run"]
    if any(count > request_limit for count in counts.values()):
        raise AnalysisError("gateway requests exceed the frozen per-run limit")
    if spent > Decimal(limits["total_spend_usd"]):
        raise AnalysisError("gateway receipts exceed the frozen total cap")
    return GatewayEvidence(
        counts=counts,
        spent=spent,
        protocol_failure_run_ids=frozenset(protocol_failure_run_ids),
    )


def _first_tool_is_diagnose(transcript: Path) -> bool:
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisError("OMP transcript is unreadable") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisError(f"OMP transcript line {line_number} is malformed") from error
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            arguments = block.get("arguments")
            return (
                block.get("name") == "bash"
                and isinstance(arguments, dict)
                and arguments.get("command") == "python3 diagnose.py"
            )
    return False


def _load_omp_metrics(
    run_root: Path, expected_requests: int, *, require_agent_diagnosis: bool
) -> tuple[float, int]:
    transcripts = sorted((run_root / "sessions").rglob("*.jsonl"))
    if len(transcripts) != 1:
        raise AnalysisError("each run must produce exactly one OMP transcript")
    transcript = transcripts[0]
    try:
        session = load_session(transcript)
    except MalformedSessionError as error:
        raise AnalysisError("OMP usage is missing or malformed") from error
    if (
        not session.turns
        or session.turns_without_usage
        or session.malformed_lines
        or session.unknown_usage_keys
        or len(session.turns) != expected_requests
    ):
        raise AnalysisError("OMP usage is incomplete or differs from gateway request count")
    if any(
        turn.provider != _EXPECTED_PROVIDER or turn.model != _EXPECTED_MODEL
        for turn in session.turns
    ):
        raise AnalysisError("OMP provider or model differs from the frozen manifest")
    if require_agent_diagnosis and not _first_tool_is_diagnose(transcript):
        raise AnalysisError("diagnose.py was not the first agent tool action")
    cost = math.fsum(turn.host_cost_usd for turn in session.turns)
    if not math.isfinite(cost) or cost <= 0:
        raise AnalysisError("OMP modelled task cost must be finite and positive")
    return cost, len(session.turns)


def _native_mechanism(run_root: Path) -> dict[str, int]:
    if (run_root / "laconic-data").exists() or (run_root / "headroom").exists():
        raise AnalysisError("native arm contains a transformation artifact")
    return {"native_pass_through": 1}


def _laconic_mechanism(run_root: Path) -> dict[str, int]:
    if (run_root / "headroom").exists():
        raise AnalysisError("Laconic arm contains a Headroom artifact")
    status = runtime_storage_status(run_root / "laconic-data")
    if (
        not status.exists
        or status.sessions < 1
        or status.damaged_ledgers
        or status.eligible_observations < 1
        or status.compressed_observations < 1
    ):
        raise AnalysisError("Laconic runtime did not produce valid emitted decisions")
    return {
        "eligible_observations": status.eligible_observations,
        "emissions": status.compressed_observations,
        "full_expansions": status.full_expansions,
        "span_expansions": status.span_expansions,
    }


def _headroom_mechanism(run_root: Path, expected_requests: int) -> dict[str, int]:
    if (run_root / "laconic-data").exists():
        raise AnalysisError("Headroom arm contains a Laconic artifact")
    path = run_root / "headroom" / "requests.jsonl"
    _ordinary_file(path)
    handled = 0
    transformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AnalysisError("Headroom request log is unreadable") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnalysisError(f"Headroom request log line {line_number} is malformed") from error
        if not isinstance(payload, dict) or set(payload) & _HEADROOM_CONTENT_KEYS:
            raise AnalysisError("Headroom request log contains content or is malformed")
        before = payload.get("input_tokens_original")
        after = payload.get("input_tokens_optimized")
        transforms = payload.get("transforms_applied")
        if (
            isinstance(before, bool)
            or not isinstance(before, int)
            or before < 0
            or isinstance(after, bool)
            or not isinstance(after, int)
            or after < 0
            or after > before
            or not isinstance(transforms, list)
            or any(not isinstance(item, str) for item in transforms)
        ):
            raise AnalysisError("Headroom mechanism metadata is invalid")
        handled += 1
        transformed += int(after < before)
    if handled != expected_requests:
        raise AnalysisError("Headroom did not log every provider request")
    return {"handled_requests": handled, "transformed_requests": transformed}


def _load_run_metrics(run: RunSpec, run_root: Path, provider_requests: int) -> RunMetrics:
    result = _read_json_object(run_root / "run-result.json")
    if (
        result.get("run_id") != run.run_id
        or result.get("arm") != run.arm
        or result.get("passed") is not True
        or result.get("completion_passed") is not True
        or result.get("fixture_guards_passed") is not True
        or result.get("process_returncode") != 0
        or result.get("process_timed_out") is not False
        or result.get("gateway_halted_reason") is not None
    ):
        raise AnalysisError("run result is missing, failed, or does not match its frozen cell")
    cost, turns = _load_omp_metrics(
        run_root,
        provider_requests,
        require_agent_diagnosis=True,
    )
    if run.arm == "native":
        mechanism = _native_mechanism(run_root)
    elif run.arm == "laconic":
        mechanism = _laconic_mechanism(run_root)
    else:
        mechanism = _headroom_mechanism(run_root, provider_requests)
    return RunMetrics(
        run_id=run.run_id,
        task_id=run.task_id,
        repeat=run.repetition,
        arm=run.arm,
        cost_usd=cost,
        assistant_turns=turns,
        provider_requests=provider_requests,
        mechanism=mechanism,
    )


def _rounded(value: float) -> float:
    return round(value, 12)


def _complete_analysis(
    metrics: list[RunMetrics], manifest: PilotManifest, gateway_spend: Decimal
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_cell = {(item.task_id, item.repeat, item.arm): item for item in metrics}
    task_means: dict[str, dict[Arm, float]] = {}
    paired: dict[str, float] = {}
    for task in manifest.tasks:
        arm_means: dict[Arm, float] = {}
        for arm_name in ARMS:
            arm = arm_name
            values = [by_cell[(task.task_id, repeat, arm)].cost_usd for repeat in (1, 2)]
            arm_means[arm] = mean(values)
        task_means[task.task_id] = arm_means
        paired[task.task_id] = math.log(arm_means["laconic"]) - math.log(arm_means["native"])
    paired_values = [paired[task.task_id] for task in manifest.tasks]
    native = [task_means[task.task_id]["native"] for task in manifest.tasks]
    laconic = [task_means[task.task_id]["laconic"] for task in manifest.tasks]
    headroom = [task_means[task.task_id]["headroom"] for task in manifest.tasks]
    dispersion = stdev(paired_values)
    try:
        laconic_correlation = correlation(native, laconic)
        headroom_correlation = correlation(native, headroom)
    except ValueError as error:
        raise UndefinedCorrelationError("cross-arm correlation is undefined") from error
    analysis = cast(dict[str, Any], manifest.payload["analysis"])
    threshold = float(analysis["action_threshold_fraction"])
    alpha = float(analysis["alpha_two_sided"])
    power = float(analysis["power"])
    effect = abs(math.log1p(-threshold))
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha / 2)
    z_power = normal.inv_cdf(power)
    task_count = max(2, math.ceil(((z_alpha + z_power) * dispersion / effect) ** 2))
    public = {
        "action_threshold_fraction": threshold,
        "alpha_two_sided": alpha,
        "completion_failures": 0,
        "confirmatory_task_count_at_two_repeats": task_count,
        "expected_run_count": len(manifest.run_order),
        "gateway_spend_usd": float(gateway_spend),
        "manifest_hash": manifest.digest,
        "mechanism_failures": 0,
        "native_headroom_correlation": _rounded(headroom_correlation),
        "native_laconic_correlation": _rounded(laconic_correlation),
        "paired_log_cost_sd": _rounded(dispersion),
        "power": power,
        "run_count": len(metrics),
        "schema_version": manifest.payload["schema_version"],
        "total_cap_usd": float(cast(dict[str, Any], manifest.payload["limits"])["total_spend_usd"]),
        "verdict": "complete",
    }
    private = {
        "manifest_hash": manifest.digest,
        "paired_task_log_cost_differences": paired,
        "runs": [
            {
                "arm": item.arm,
                "assistant_turns": item.assistant_turns,
                "cost_usd": item.cost_usd,
                "mechanism": item.mechanism,
                "provider_requests": item.provider_requests,
                "repeat": item.repeat,
                "run_id": item.run_id,
                "task_id": item.task_id,
            }
            for item in metrics
        ],
        "schema_version": manifest.payload["schema_version"],
        "task_arm_mean_cost_usd": task_means,
    }
    return private, public


def _analyze_v1(
    artifact_root: Path, *, manifest: PilotManifest
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return private detail and an allowlisted public disposition."""
    root = artifact_root.expanduser().absolute()
    state = _read_json_object(root / "campaign-state.json")
    if state.get("manifest_hash") != manifest.digest:
        raise AnalysisError("campaign state manifest hash does not match selected manifest")
    expected_ids = [run.run_id for run in manifest.run_order]
    completed = state.get("completed_runs")
    live_before = state.get("live_state_before")
    live_after = state.get("live_state_after")
    state_valid = (
        state.get("status") == "completed"
        and isinstance(completed, list)
        and completed == expected_ids
        and isinstance(live_before, dict)
        and live_before == live_after
        and set(live_before) == {"laconic_runtime", "omp_agent"}
        and all(isinstance(value, str) and len(value) == 64 for value in live_before.values())
    )
    metrics: list[RunMetrics] = []
    completion_failure_ids: set[str] = set()
    mechanism_failure_ids: set[str] = set()
    gateway_spend = Decimal(0)
    try:
        requests, gateway_spend = _load_gateway_request_counts(root, manifest)
    except AnalysisError:
        requests = Counter()
    if requests:
        try:
            declared_spend = Decimal(state["gateway_spent_usd"])
        except (KeyError, TypeError, InvalidOperation):
            state_valid = False
        else:
            state_valid = state_valid and declared_spend == gateway_spend
    for run in manifest.run_order:
        run_root = root / "runs" / run.run_id
        try:
            metrics.append(_load_run_metrics(run, run_root, requests[run.run_id]))
        except AnalysisError:
            try:
                result = _read_json_object(run_root / "run-result.json")
                completion_failed = result.get("passed") is not True
            except AnalysisError:
                completion_failed = True
            if completion_failed:
                completion_failure_ids.add(run.run_id)
            else:
                mechanism_failure_ids.add(run.run_id)
    completion_failures = len(completion_failure_ids)
    mechanism_failures = len(mechanism_failure_ids)
    analysis_failure: str | None = None
    if (
        state_valid
        and len(metrics) == len(manifest.run_order)
        and completion_failures == 0
        and mechanism_failures == 0
    ):
        try:
            return _complete_analysis(metrics, manifest, gateway_spend)
        except UndefinedCorrelationError:
            analysis_failure = "undefined_cross_arm_correlation"
    analysis = cast(dict[str, Any], manifest.payload["analysis"])
    public = {
        "action_threshold_fraction": float(analysis["action_threshold_fraction"]),
        "alpha_two_sided": float(analysis["alpha_two_sided"]),
        "completion_failures": completion_failures,
        "confirmatory_task_count_at_two_repeats": None,
        "expected_run_count": len(manifest.run_order),
        "gateway_spend_usd": float(gateway_spend),
        "manifest_hash": manifest.digest,
        "mechanism_failures": mechanism_failures,
        "native_headroom_correlation": None,
        "native_laconic_correlation": None,
        "paired_log_cost_sd": None,
        "power": float(analysis["power"]),
        "run_count": len(metrics),
        "schema_version": manifest.payload["schema_version"],
        "total_cap_usd": float(cast(dict[str, Any], manifest.payload["limits"])["total_spend_usd"]),
        "verdict": "incomplete",
    }
    private = {
        "analysis_failure": analysis_failure,
        "manifest_hash": manifest.digest,
        "runs": [item.run_id for item in metrics],
        "schema_version": manifest.payload["schema_version"],
        "verdict": "incomplete",
    }
    return private, public


def _v2_run_roots(artifact_root: Path, manifest: PilotManifest) -> dict[str, Path]:
    runs_root = artifact_root / "runs"
    try:
        metadata = runs_root.lstat()
    except OSError as error:
        raise AnalysisError("v2 campaign runs directory is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AnalysisError("v2 campaign runs path is not an ordinary directory")
    expected = {run.run_id for run in manifest.run_order}
    roots: dict[str, Path] = {}
    for path in runs_root.iterdir():
        child = path.lstat()
        if (
            path.name not in expected
            or stat.S_ISLNK(child.st_mode)
            or not stat.S_ISDIR(child.st_mode)
        ):
            raise AnalysisError("v2 campaign contains an unexpected run artifact")
        roots[path.name] = path
    return roots


def _v2_state_integrity(state: dict[str, Any], manifest: PilotManifest) -> bool:
    completed = state.get("completed_runs")
    expected_ids = [run.run_id for run in manifest.run_order]
    live_before = state.get("live_state_before")
    live_after = state.get("live_state_after")
    return (
        state.get("schema_version") == manifest.payload["schema_version"]
        and state.get("status") in {"completed", "incomplete", "interrupted"}
        and isinstance(completed, list)
        and completed == expected_ids[: len(completed)]
        and (state.get("status") != "completed" or completed == expected_ids)
        and isinstance(live_before, dict)
        and live_before == live_after
        and set(live_before) == {"laconic_runtime", "omp_agent"}
        and all(isinstance(value, str) and len(value) == 64 for value in live_before.values())
    )


def _v2_runner_diagnosis_valid(run_root: Path) -> bool:
    diagnosis = _read_json_object(run_root / "diagnosis-result.json")
    wall_seconds = diagnosis.get("wall_seconds")
    return (
        set(diagnosis) == {"baseline_failed", "command", "returncode", "timed_out", "wall_seconds"}
        and diagnosis.get("baseline_failed") is True
        and diagnosis.get("command") == ["python3", "diagnose.py"]
        and isinstance(diagnosis.get("returncode"), int)
        and not isinstance(diagnosis.get("returncode"), bool)
        and diagnosis["returncode"] > 0
        and diagnosis.get("timed_out") is False
        and isinstance(wall_seconds, int | float)
        and not isinstance(wall_seconds, bool)
        and math.isfinite(wall_seconds)
        and wall_seconds >= 0
    )


def _v2_completion_result(run: RunSpec, run_root: Path) -> bool:
    result = _read_json_object(run_root / "run-result.json")
    completion_passed = result.get("completion_passed")
    if (
        result.get("run_id") != run.run_id
        or result.get("arm") != run.arm
        or result.get("runner_diagnosis_passed") is not True
        or result.get("fixture_guards_passed") is not True
        or result.get("process_returncode") != 0
        or result.get("process_timed_out") is not False
        or result.get("gateway_halted_reason") is not None
        or not isinstance(completion_passed, bool)
        or result.get("passed") is not completion_passed
    ):
        raise AnalysisError("v2 run result violates the frozen protocol")
    return cast(bool, completion_passed)


def _v2_mechanism(run: RunSpec, run_root: Path, provider_requests: int) -> dict[str, int]:
    if run.arm == "native":
        return _native_mechanism(run_root)
    if run.arm == "laconic":
        return _laconic_mechanism(run_root)
    return _headroom_mechanism(run_root, provider_requests)


def _complete_analysis_v2(
    metrics: list[RunMetrics], manifest: PilotManifest, gateway_spend: Decimal
) -> tuple[dict[str, Any], dict[str, Any]]:
    private, computed = _complete_analysis(metrics, manifest, gateway_spend)
    public = {
        "action_threshold_fraction": computed["action_threshold_fraction"],
        "alpha_two_sided": computed["alpha_two_sided"],
        "attempted_cells": len(metrics),
        "confirmatory_task_count_at_two_repeats": computed[
            "confirmatory_task_count_at_two_repeats"
        ],
        "expected_cell_count": len(manifest.run_order),
        "gateway_spend_usd": computed["gateway_spend_usd"],
        "manifest_hash": manifest.digest,
        "mechanism_non_engagement": 0,
        "native_headroom_correlation": computed["native_headroom_correlation"],
        "native_laconic_correlation": computed["native_laconic_correlation"],
        "paired_log_cost_sd": computed["paired_log_cost_sd"],
        "power": computed["power"],
        "protocol_failures": 0,
        "schema_version": manifest.payload["schema_version"],
        "task_completion_failures": 0,
        "total_cap_usd": computed["total_cap_usd"],
        "unrun_cells": 0,
        "valid_cells": len(metrics),
        "verdict": "complete",
    }
    return private, public


def _analyze_v2(
    artifact_root: Path, *, manifest: PilotManifest
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = artifact_root.expanduser().absolute()
    state = _read_json_object(root / "campaign-state.json")
    if state.get("manifest_hash") != manifest.digest:
        raise AnalysisError("campaign state manifest hash does not match selected manifest")
    run_roots = _v2_run_roots(root, manifest)
    receipt_path = root / "gateway-receipts.jsonl"
    if receipt_path.exists() or receipt_path.is_symlink():
        gateway = _load_v2_gateway_evidence(root, manifest)
    elif any(
        (run_root / "run-result.json").exists() or (run_root / "sessions").exists()
        for run_root in run_roots.values()
    ):
        raise AnalysisError("gateway receipts are unavailable for provider-attempted cells")
    else:
        gateway = GatewayEvidence(
            counts=Counter(),
            spent=Decimal(0),
            protocol_failure_run_ids=frozenset(),
        )
    expected_ids = [run.run_id for run in manifest.run_order]
    completed = state.get("completed_runs")
    attempted_prefix = expected_ids[: len(run_roots)]
    state_integrity = (
        _v2_state_integrity(state, manifest)
        and set(run_roots) == set(attempted_prefix)
        and isinstance(completed, list)
        and len(completed) in {len(run_roots), max(0, len(run_roots) - 1)}
        and state.get("gateway_halted_reason")
        not in {"live_state_changed", "private_artifact_cleanup_failed"}
    )
    failed_run = state.get("failed_run")
    if failed_run is not None and (
        not isinstance(failed_run, str)
        or not attempted_prefix
        or failed_run != attempted_prefix[-1]
        or not isinstance(completed, list)
        or len(completed) != len(run_roots) - 1
    ):
        state_integrity = False
    if set(gateway.counts) - set(run_roots):
        raise AnalysisError("gateway receipts name an unattempted frozen cell")
    if state.get("status") == "completed":
        try:
            declared_spend = Decimal(state["gateway_spent_usd"])
        except (KeyError, TypeError, InvalidOperation):
            state_integrity = False
        else:
            state_integrity = state_integrity and declared_spend == gateway.spent

    metrics: list[RunMetrics] = []
    task_completion_failure_ids: set[str] = set()
    protocol_failure_ids: set[str] = set()
    mechanism_non_engagement_ids: set[str] = set()
    for run in manifest.run_order:
        run_root = run_roots.get(run.run_id)
        if run_root is None:
            continue
        if not state_integrity or run.run_id in gateway.protocol_failure_run_ids:
            protocol_failure_ids.add(run.run_id)
            continue
        try:
            if not _v2_runner_diagnosis_valid(run_root):
                raise AnalysisError("runner diagnosis does not match the frozen failing baseline")
            completion_passed = _v2_completion_result(run, run_root)
            cost, turns = _load_omp_metrics(
                run_root,
                gateway.counts[run.run_id],
                require_agent_diagnosis=False,
            )
        except AnalysisError:
            protocol_failure_ids.add(run.run_id)
            continue
        if not completion_passed:
            task_completion_failure_ids.add(run.run_id)
            continue
        try:
            mechanism = _v2_mechanism(run, run_root, gateway.counts[run.run_id])
        except AnalysisError:
            mechanism_non_engagement_ids.add(run.run_id)
            continue
        metrics.append(
            RunMetrics(
                run_id=run.run_id,
                task_id=run.task_id,
                repeat=run.repetition,
                arm=run.arm,
                cost_usd=cost,
                assistant_turns=turns,
                provider_requests=gateway.counts[run.run_id],
                mechanism=mechanism,
            )
        )

    attempted_cells = len(run_roots)
    valid_cells = len(metrics)
    task_completion_failures = len(task_completion_failure_ids)
    protocol_failures = len(protocol_failure_ids)
    mechanism_non_engagement = len(mechanism_non_engagement_ids)
    unrun_cells = len(manifest.run_order) - attempted_cells
    if (
        valid_cells + task_completion_failures + protocol_failures + mechanism_non_engagement
        != attempted_cells
    ):
        raise AnalysisError("v2 cell dispositions do not partition attempted cells")

    analysis_failure: str | None = None
    complete_state = (
        state_integrity
        and state.get("status") == "completed"
        and attempted_cells == valid_cells == len(manifest.run_order)
        and not task_completion_failures
        and not protocol_failures
        and not mechanism_non_engagement
    )
    if complete_state:
        try:
            return _complete_analysis_v2(metrics, manifest, gateway.spent)
        except UndefinedCorrelationError:
            analysis_failure = "undefined_cross_arm_correlation"

    analysis = cast(dict[str, Any], manifest.payload["analysis"])
    public = {
        "action_threshold_fraction": float(analysis["action_threshold_fraction"]),
        "alpha_two_sided": float(analysis["alpha_two_sided"]),
        "attempted_cells": attempted_cells,
        "confirmatory_task_count_at_two_repeats": None,
        "expected_cell_count": len(manifest.run_order),
        "gateway_spend_usd": float(gateway.spent),
        "manifest_hash": manifest.digest,
        "mechanism_non_engagement": mechanism_non_engagement,
        "native_headroom_correlation": None,
        "native_laconic_correlation": None,
        "paired_log_cost_sd": None,
        "power": float(analysis["power"]),
        "protocol_failures": protocol_failures,
        "schema_version": manifest.payload["schema_version"],
        "task_completion_failures": task_completion_failures,
        "total_cap_usd": float(cast(dict[str, Any], manifest.payload["limits"])["total_spend_usd"]),
        "unrun_cells": unrun_cells,
        "valid_cells": valid_cells,
        "verdict": "incomplete",
    }
    private = {
        "analysis_failure": analysis_failure,
        "manifest_hash": manifest.digest,
        "mechanism_non_engagement_ids": sorted(mechanism_non_engagement_ids),
        "protocol_failure_ids": sorted(protocol_failure_ids),
        "runs": [item.run_id for item in metrics],
        "schema_version": manifest.payload["schema_version"],
        "task_completion_failure_ids": sorted(task_completion_failure_ids),
        "verdict": "incomplete",
    }
    return private, public


def analyze_campaign(
    artifact_root: Path, *, manifest: PilotManifest
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return private detail and an allowlisted public disposition."""
    if manifest.payload["schema_version"] == 1:
        return _analyze_v1(artifact_root, manifest=manifest)
    if manifest.payload["schema_version"] == 2:
        return _analyze_v2(artifact_root, manifest=manifest)
    raise AnalysisError("selected manifest schema is unsupported")


def _render_v2_public_markdown(report: dict[str, Any]) -> str:
    if report["verdict"] == "incomplete":
        dispersion = laconic_correlation = headroom_correlation = task_count = "not computed"
    else:
        dispersion = str(report["paired_log_cost_sd"])
        laconic_correlation = str(report["native_laconic_correlation"])
        headroom_correlation = str(report["native_headroom_correlation"])
        task_count = str(report["confirmatory_task_count_at_two_repeats"])
    return (
        "# Controlled Spend Variance Pilot v2\n\n"
        f"**Disposition: {report['verdict']}.** This variance pilot is not a performance "
        "result. It does not report arm means, a paired effect estimate, token or cost "
        "savings, or product superiority.\n\n"
        "| Field | Value |\n"
        "| --- | ---: |\n"
        f"| Attempted cells | {report['attempted_cells']} / "
        f"{report['expected_cell_count']} |\n"
        f"| Valid cells | {report['valid_cells']} |\n"
        f"| Unrun cells | {report['unrun_cells']} |\n"
        f"| Task-completion failures | {report['task_completion_failures']} |\n"
        f"| Protocol failures | {report['protocol_failures']} |\n"
        f"| Mechanism non-engagement | {report['mechanism_non_engagement']} |\n"
        f"| Gateway spend | ${report['gateway_spend_usd']:.6f} / "
        f"${report['total_cap_usd']:.2f} |\n"
        f"| Paired log-cost SD | {dispersion} |\n"
        f"| Native/Laconic cost correlation | {laconic_correlation} |\n"
        f"| Native/Headroom cost correlation | {headroom_correlation} |\n"
        f"| Confirmatory tasks at two repeats | {task_count} |\n\n"
        f"The sample-feasibility calculation uses a two-sided alpha of "
        f"{report['alpha_two_sided']:.2f}, power {report['power']:.2f}, and the "
        f"precommitted {report['action_threshold_fraction']:.0%} smallest worthwhile "
        "total-cost reduction under a normal approximation. A confirmatory run requires "
        "a new frozen manifest and explicit spend authorization.\n"
    )


def render_public_markdown(report: dict[str, Any], *, manifest: PilotManifest) -> str:
    """Render the allowlisted report without introducing hidden fields."""
    validate_public_report(report, manifest=manifest)
    if manifest.payload["schema_version"] == 2:
        return _render_v2_public_markdown(report)
    if report["verdict"] == "incomplete":
        dispersion = laconic_correlation = headroom_correlation = task_count = "not computed"
    else:
        dispersion = str(report["paired_log_cost_sd"])
        laconic_correlation = str(report["native_laconic_correlation"])
        headroom_correlation = str(report["native_headroom_correlation"])
        task_count = str(report["confirmatory_task_count_at_two_repeats"])
    return (
        "# Controlled Spend Variance Pilot\n\n"
        f"**Disposition: {report['verdict']}.** This variance pilot is not a performance "
        "result. It does not report arm means, a paired effect estimate, token or cost "
        "savings, or product superiority.\n\n"
        "| Field | Value |\n"
        "| --- | ---: |\n"
        f"| Valid runs | {report['run_count']} / {report['expected_run_count']} |\n"
        f"| Completion failures | {report['completion_failures']} |\n"
        f"| Mechanism failures | {report['mechanism_failures']} |\n"
        f"| Gateway spend | ${report['gateway_spend_usd']:.6f} / "
        f"${report['total_cap_usd']:.2f} |\n"
        f"| Paired log-cost SD | {dispersion} |\n"
        f"| Native/Laconic cost correlation | {laconic_correlation} |\n"
        f"| Native/Headroom cost correlation | {headroom_correlation} |\n"
        f"| Confirmatory tasks at two repeats | {task_count} |\n\n"
        f"The sample-feasibility calculation uses a two-sided alpha of "
        f"{report['alpha_two_sided']:.2f}, power {report['power']:.2f}, and the "
        f"precommitted {report['action_threshold_fraction']:.0%} smallest worthwhile "
        "total-cost reduction under a normal approximation. A confirmatory run requires "
        "a new frozen manifest and explicit spend authorization.\n"
    )


def _atomic_write(path: Path, content: bytes, *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AnalysisError(f"output path is unsafe: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600 if private else 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_report(
    artifact_root: Path,
    output_json: Path,
    output_markdown: Path,
    *,
    manifest: PilotManifest,
) -> dict[str, Any]:
    """Generate private analysis plus deterministic public JSON and Markdown."""
    root = artifact_root.expanduser().absolute()
    private, public = analyze_campaign(root, manifest=manifest)
    validate_public_report(public, manifest=manifest)
    _atomic_write(
        root / _PRIVATE_REPORT_NAME,
        canonical_json(private),
        private=True,
    )
    _atomic_write(output_json, canonical_json(public), private=False)
    _atomic_write(
        output_markdown,
        render_public_markdown(public, manifest=manifest).encode(),
        private=False,
    )
    return public


def check_report(
    artifact_root: Path,
    report_json: Path,
    report_markdown: Path,
    *,
    manifest: PilotManifest,
) -> dict[str, Any]:
    """Recompute and require byte-identical public artifacts."""
    _, public = analyze_campaign(artifact_root, manifest=manifest)
    validate_public_report(public, manifest=manifest)
    expected_json = canonical_json(public)
    expected_markdown = render_public_markdown(public, manifest=manifest).encode()
    _ordinary_file(report_json)
    _ordinary_file(report_markdown)
    if (
        report_json.read_bytes() != expected_json
        or report_markdown.read_bytes() != expected_markdown
    ):
        raise AnalysisError("public report is stale or non-deterministic")
    return public
