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
from tools.controlled_spend.manifest import (
    ARMS,
    Arm,
    PilotManifest,
    RunSpec,
    canonical_json,
    manifest_digest,
    validate_manifest_file,
)
from tools.controlled_spend.privacy import validate_public_report

_PRIVATE_REPORT_NAME: Final = "analysis-private.json"
_EXPECTED_MODEL: Final = "claude-sonnet-5"
_EXPECTED_PROVIDER: Final = "anthropic"
_HEADROOM_CONTENT_KEYS: Final = frozenset(
    {"request_messages", "compressed_messages", "response_content"}
)


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


def _load_omp_metrics(run_root: Path, expected_requests: int) -> tuple[float, int]:
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
    if not _first_tool_is_diagnose(transcript):
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
    cost, turns = _load_omp_metrics(run_root, provider_requests)
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
        "manifest_hash": manifest_digest(),
        "mechanism_failures": 0,
        "native_headroom_correlation": _rounded(headroom_correlation),
        "native_laconic_correlation": _rounded(laconic_correlation),
        "paired_log_cost_sd": _rounded(dispersion),
        "power": power,
        "run_count": len(metrics),
        "schema_version": 1,
        "total_cap_usd": float(cast(dict[str, Any], manifest.payload["limits"])["total_spend_usd"]),
        "verdict": "complete",
    }
    private = {
        "manifest_hash": manifest_digest(),
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
        "schema_version": 1,
        "task_arm_mean_cost_usd": task_means,
    }
    return private, public


def analyze_campaign(artifact_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return private detail and an allowlisted public disposition."""
    manifest = validate_manifest_file()
    root = artifact_root.expanduser().absolute()
    state = _read_json_object(root / "campaign-state.json")
    expected_ids = [run.run_id for run in manifest.run_order]
    completed = state.get("completed_runs")
    live_before = state.get("live_state_before")
    live_after = state.get("live_state_after")
    state_valid = (
        state.get("manifest_hash") == manifest_digest()
        and state.get("status") == "completed"
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
        "manifest_hash": manifest_digest(),
        "mechanism_failures": mechanism_failures,
        "native_headroom_correlation": None,
        "native_laconic_correlation": None,
        "paired_log_cost_sd": None,
        "power": float(analysis["power"]),
        "run_count": len(metrics),
        "schema_version": 1,
        "total_cap_usd": float(cast(dict[str, Any], manifest.payload["limits"])["total_spend_usd"]),
        "verdict": "incomplete",
    }
    private = {
        "analysis_failure": analysis_failure,
        "manifest_hash": manifest_digest(),
        "runs": [item.run_id for item in metrics],
        "schema_version": 1,
        "verdict": "incomplete",
    }
    return private, public


def render_public_markdown(report: dict[str, Any]) -> str:
    """Render the allowlisted report without introducing hidden fields."""
    validate_public_report(report)
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
    artifact_root: Path, output_json: Path, output_markdown: Path
) -> dict[str, Any]:
    """Generate private analysis plus deterministic public JSON and Markdown."""
    root = artifact_root.expanduser().absolute()
    private, public = analyze_campaign(root)
    validate_public_report(public)
    _atomic_write(
        root / _PRIVATE_REPORT_NAME,
        canonical_json(private),
        private=True,
    )
    _atomic_write(output_json, canonical_json(public), private=False)
    _atomic_write(output_markdown, render_public_markdown(public).encode(), private=False)
    return public


def check_report(artifact_root: Path, report_json: Path, report_markdown: Path) -> dict[str, Any]:
    """Recompute and require byte-identical public artifacts."""
    _, public = analyze_campaign(artifact_root)
    validate_public_report(public)
    expected_json = canonical_json(public)
    expected_markdown = render_public_markdown(public).encode()
    _ordinary_file(report_json)
    _ordinary_file(report_markdown)
    if (
        report_json.read_bytes() != expected_json
        or report_markdown.read_bytes() != expected_markdown
    ):
        raise AnalysisError("public report is stale or non-deterministic")
    return public
