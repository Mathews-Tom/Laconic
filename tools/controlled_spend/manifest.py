"""Frozen population and materialization contract for the M20 variance pilot."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal, cast

Arm = Literal["native", "laconic", "headroom"]
ARMS: Final[tuple[Arm, ...]] = ("native", "laconic", "headroom")
SCHEMA_VERSION: Final = 1
V2_SCHEMA_VERSION: Final = 2
TASK_COUNT: Final = 4
REPEATS: Final = 2
TASK_IDS: Final = ("t01", "t02", "t03", "t04")
COMPLETION_COMMAND: Final = (
    "python3",
    "-m",
    "unittest",
    "discover",
    "-s",
    "tests",
    "-q",
)
RANDOM_SEED: Final = "3afb17d44bef47718e656f95831877644a1f92e2b154eab2c7ef753a46972fd0"
V2_RANDOM_SEED: Final = "59fb6151309c3d6e0dd5ae4031444d2b8ba1d5a6fa03ecd1e07e213b574126a3"
STOPPING_RULES: Final = (
    "request_reservation_exceeds_total_cap",
    "per_run_request_limit_reached",
    "provider_usage_missing_or_malformed",
    "task_completion_failed",
    "arm_mechanism_unverified",
    "task_or_configuration_drift",
    "live_state_changed",
    "private_artifact_cleanup_failed",
)
V2_STOPPING_RULES: Final = ("runner_diagnosis_failed", *STOPPING_RULES)
V2_LIVE_STATE_ROOTS: Final = ("laconic_runtime", "omp_agent")
V2_AMBIENT_PATHS: Final = (
    "laconic_runtime/observe/audit.jsonl",
    "laconic_runtime/sessions/*.sqlite3",
    "laconic_runtime/sessions/*.sqlite3-shm",
    "laconic_runtime/sessions/*.sqlite3-wal",
    "omp_agent/agent.db-shm",
    "omp_agent/agent.db-wal",
    "omp_agent/cache/**",
    "omp_agent/managed-skills/**",
    "omp_agent/memories/**",
    "omp_agent/sessions/**",
)
CREDENTIAL_STATE_PATH: Final = "omp_agent/agent.db"
PUBLIC_REPORT_KEYS: Final = (
    "schema_version",
    "manifest_hash",
    "verdict",
    "run_count",
    "expected_run_count",
    "completion_failures",
    "mechanism_failures",
    "paired_log_cost_sd",
    "native_laconic_correlation",
    "native_headroom_correlation",
    "confirmatory_task_count_at_two_repeats",
    "action_threshold_fraction",
    "alpha_two_sided",
    "power",
    "total_cap_usd",
    "gateway_spend_usd",
)
V2_PUBLIC_REPORT_KEYS: Final = (
    "schema_version",
    "manifest_hash",
    "verdict",
    "attempted_cells",
    "valid_cells",
    "expected_cell_count",
    "unrun_cells",
    "task_completion_failures",
    "protocol_failures",
    "mechanism_non_engagement",
    "paired_log_cost_sd",
    "native_laconic_correlation",
    "native_headroom_correlation",
    "confirmatory_task_count_at_two_repeats",
    "action_threshold_fraction",
    "alpha_two_sided",
    "power",
    "total_cap_usd",
    "gateway_spend_usd",
)
RUN_COUNT: Final = TASK_COUNT * REPEATS * len(ARMS)
PACKAGE_ROOT: Final = Path(__file__).resolve().parent
DEFAULT_MANIFEST_PATH: Final = PACKAGE_ROOT / "pilot-manifest.json"
DEFAULT_V2_MANIFEST_PATH: Final = PACKAGE_ROOT / "pilot-manifest-v2.json"
FIXTURES_ROOT: Final = PACKAGE_ROOT / "fixtures"
_HEX_64: Final = frozenset("0123456789abcdef")


class ManifestError(ValueError):
    """Raised when the frozen pilot contract or task sources drift."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    source_dir: str
    source_sha256: str
    prompt_file: str
    prompt_sha256: str
    solution_patch: str
    completion_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    task_id: str
    repetition: int
    arm: Arm


@dataclass(frozen=True, slots=True)
class PilotManifest:
    payload: dict[str, Any]
    tasks: tuple[TaskSpec, ...]
    run_order: tuple[RunSpec, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.payload)).hexdigest()


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _require_exact_keys(payload: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise ManifestError(
            f"{field} keys differ: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _require_hex64(value: Any, field: str) -> str:
    text = _require_string(value, field)
    if len(text) != 64 or set(text) - _HEX_64:
        raise ManifestError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{field} must be a positive integer")
    return value


def _require_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ManifestError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ManifestError(f"{field} must be a decimal string") from error
    if not result.is_finite() or result <= 0:
        raise ManifestError(f"{field} must be finite and positive")
    return result


def canonical_json(payload: Any) -> bytes:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{serialized}\n".encode()


def _glob_to_regex(pattern: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if pattern[index + 1 : index + 2] == "*":
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
        index += 1
    return "".join(parts)


def compile_ambient_paths(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the frozen ambient-writer allowlist into one anchored matcher."""
    if not patterns:
        raise ManifestError("ambient path allowlist must not be empty")
    compiled = re.compile("|".join(f"(?:{_glob_to_regex(item)})" for item in patterns))
    if compiled.fullmatch(CREDENTIAL_STATE_PATH):
        raise ManifestError("ambient path allowlist must never match the credential database")
    return compiled


def ambient_paths(manifest: PilotManifest) -> re.Pattern[str]:
    """Return the compiled allowlist for the selected manifest."""
    live_state = cast(dict[str, Any], manifest.payload["live_state"])
    return compile_ambient_paths(tuple(cast(list[str], live_state["ambient_paths"])))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ManifestError("fixture source must be a real directory")
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix != ".pyc"
    )
    if not files:
        raise ManifestError("fixture source must contain files")
    for path in files:
        if path.is_symlink():
            raise ManifestError("fixture source must not contain symlinks")
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def manifest_digest(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _parse_tasks(value: Any, *, prompt_file: str) -> tuple[TaskSpec, ...]:
    if not isinstance(value, list) or len(value) != TASK_COUNT:
        raise ManifestError(f"tasks must contain exactly {TASK_COUNT} entries")
    tasks: list[TaskSpec] = []
    for index, raw in enumerate(value):
        item = _require_object(raw, f"tasks[{index}]")
        _require_exact_keys(
            item,
            {
                "task_id",
                "source_dir",
                "source_sha256",
                "prompt_file",
                "prompt_sha256",
                "solution_patch",
                "completion_command",
            },
            f"tasks[{index}]",
        )
        command = item["completion_command"]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ManifestError(f"tasks[{index}].completion_command must be non-empty strings")
        if tuple(command) != COMPLETION_COMMAND:
            raise ManifestError(f"tasks[{index}].completion_command differs from the frozen pilot")
        task = TaskSpec(
            task_id=_require_string(item["task_id"], f"tasks[{index}].task_id"),
            source_dir=_require_string(item["source_dir"], f"tasks[{index}].source_dir"),
            source_sha256=_require_hex64(item["source_sha256"], f"tasks[{index}].source_sha256"),
            prompt_file=_require_string(item["prompt_file"], f"tasks[{index}].prompt_file"),
            prompt_sha256=_require_hex64(item["prompt_sha256"], f"tasks[{index}].prompt_sha256"),
            solution_patch=_require_string(
                item["solution_patch"], f"tasks[{index}].solution_patch"
            ),
            completion_command=tuple(command),
        )
        if (task.source_dir, task.prompt_file, task.solution_patch) != (
            "seed",
            prompt_file,
            "solution.patch",
        ):
            raise ManifestError(f"tasks[{index}] layout differs from the frozen pilot")
        fixture_root = FIXTURES_ROOT / task.task_id
        source = fixture_root / task.source_dir
        prompt = fixture_root / task.prompt_file
        patch = fixture_root / task.solution_patch
        if tree_digest(source) != task.source_sha256:
            raise ManifestError(f"fixture source digest drifted for {task.task_id}")
        if not prompt.is_file() or prompt.is_symlink() or sha256_file(prompt) != task.prompt_sha256:
            raise ManifestError(f"prompt digest drifted for {task.task_id}")
        if not patch.is_file() or patch.is_symlink():
            raise ManifestError(f"solution patch missing for {task.task_id}")
        tasks.append(task)
    ids = [task.task_id for task in tasks]
    if tuple(ids) != TASK_IDS:
        raise ManifestError("task_id values/order differ from the frozen pilot")
    return tuple(tasks)


def _parse_runs(value: Any, task_ids: set[str], *, random_seed: str) -> tuple[RunSpec, ...]:
    if not isinstance(value, list) or len(value) != RUN_COUNT:
        raise ManifestError(f"run_order must contain exactly {RUN_COUNT} entries")
    runs: list[RunSpec] = []
    for index, raw in enumerate(value):
        item = _require_object(raw, f"run_order[{index}]")
        _require_exact_keys(item, {"run_id", "task_id", "repetition", "arm"}, f"run_order[{index}]")
        arm = item["arm"]
        if arm not in ARMS:
            raise ManifestError(f"run_order[{index}].arm is unknown")
        run = RunSpec(
            run_id=_require_string(item["run_id"], f"run_order[{index}].run_id"),
            task_id=_require_string(item["task_id"], f"run_order[{index}].task_id"),
            repetition=_require_positive_int(item["repetition"], f"run_order[{index}].repetition"),
            arm=cast(Arm, arm),
        )
        if run.task_id not in task_ids or run.repetition > REPEATS:
            raise ManifestError(f"run_order[{index}] names an invalid task/repetition")
        runs.append(run)
    if len({run.run_id for run in runs}) != RUN_COUNT:
        raise ManifestError("run_id values must be unique")
    expected = {
        (task_id, repetition, arm)
        for task_id in task_ids
        for repetition in range(1, REPEATS + 1)
        for arm in ARMS
    }
    actual = {(run.task_id, run.repetition, run.arm) for run in runs}
    if actual != expected:
        raise ManifestError("run_order must contain each task/repetition/arm cell exactly once")
    rng = random.Random(int(random_seed, 16))
    expected_order: list[tuple[str, str, int, str]] = []
    for task_id in TASK_IDS:
        for repetition in range(1, REPEATS + 1):
            shuffled_arms = list(ARMS)
            rng.shuffle(shuffled_arms)
            for arm in shuffled_arms:
                expected_order.append((f"r{len(expected_order) + 1:03d}", task_id, repetition, arm))
    actual_order = [(run.run_id, run.task_id, run.repetition, run.arm) for run in runs]
    if actual_order != expected_order:
        raise ManifestError("run_order differs from the frozen seeded order")
    return tuple(runs)


def validate_manifest_json(payload: dict[str, Any]) -> PilotManifest:
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {
        SCHEMA_VERSION,
        V2_SCHEMA_VERSION,
    }:
        raise ManifestError("manifest schema_version is not supported")
    common_keys = {
        "schema_version",
        "study_id",
        "phase",
        "omp",
        "headroom",
        "tasks",
        "arms",
        "repeats",
        "random_seed",
        "run_order",
        "limits",
        "analysis",
        "stopping_rules",
        "public_report_keys",
    }
    is_v2 = schema_version == V2_SCHEMA_VERSION
    _require_exact_keys(
        payload,
        common_keys | ({"execution_authorized", "live_state"} if is_v2 else set()),
        "manifest",
    )
    if payload["phase"] != "variance_pilot":
        raise ManifestError("manifest phase is not the frozen pilot contract")
    study_id = "m20-variance-pilot-v2" if is_v2 else "m20-variance-pilot-v1"
    random_seed = V2_RANDOM_SEED if is_v2 else RANDOM_SEED
    prompt_file = "PROMPT-v2.txt" if is_v2 else "PROMPT.txt"
    request_limit = 16 if is_v2 else 8
    stopping_rules = V2_STOPPING_RULES if is_v2 else STOPPING_RULES
    public_report_keys = V2_PUBLIC_REPORT_KEYS if is_v2 else PUBLIC_REPORT_KEYS
    if payload["study_id"] != study_id:
        raise ManifestError("study_id differs from the selected frozen pilot")
    if is_v2 and payload["execution_authorized"] is not False:
        raise ManifestError("M20-v2 execution_authorized must remain false")
    if is_v2:
        live_state = _require_object(payload["live_state"], "live_state")
        _require_exact_keys(live_state, {"roots", "ambient_paths"}, "live_state")
        if live_state["roots"] != list(V2_LIVE_STATE_ROOTS):
            raise ManifestError("live_state.roots differ from the frozen pilot")
        if live_state["ambient_paths"] != list(V2_AMBIENT_PATHS):
            raise ManifestError("live_state.ambient_paths differ from the frozen pilot")
        compile_ambient_paths(tuple(V2_AMBIENT_PATHS))
    if payload["arms"] != list(ARMS) or payload["repeats"] != REPEATS:
        raise ManifestError("arms/repeats differ from the frozen pilot")
    if _require_hex64(payload["random_seed"], "random_seed") != random_seed:
        raise ManifestError("random_seed differs from the selected frozen pilot")

    omp = _require_object(payload["omp"], "omp")
    _require_exact_keys(
        omp,
        {
            "version",
            "provider",
            "model",
            "thinking",
            "max_output_tokens",
            "catalog",
            "catalog_sha256",
        },
        "omp",
    )
    if (omp["version"], omp["provider"], omp["model"], omp["thinking"]) != (
        "18.1.14",
        "anthropic",
        "claude-sonnet-5",
        "low",
    ):
        raise ManifestError("OMP/model settings differ from the frozen pilot")
    if omp["max_output_tokens"] != 4096:
        raise ManifestError("OMP max_output_tokens differs from the frozen pilot")
    catalog = _require_object(omp["catalog"], "omp.catalog")
    _require_exact_keys(
        catalog,
        {
            "input_per_mtok",
            "cache_write_5m_per_mtok",
            "cache_write_1h_per_mtok",
            "cache_read_per_mtok",
            "output_per_mtok",
            "effective_date",
            "source",
        },
        "omp.catalog",
    )
    if catalog != {
        "input_per_mtok": "2.00",
        "cache_write_5m_per_mtok": "2.50",
        "cache_write_1h_per_mtok": "4.00",
        "cache_read_per_mtok": "0.20",
        "output_per_mtok": "10.00",
        "effective_date": "2026-09-01",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    }:
        raise ManifestError("OMP catalog differs from the frozen price snapshot")
    if hashlib.sha256(canonical_json(catalog)).hexdigest() != _require_hex64(
        omp["catalog_sha256"], "omp.catalog_sha256"
    ):
        raise ManifestError("OMP catalog digest does not match its frozen payload")

    headroom = _require_object(payload["headroom"], "headroom")
    _require_exact_keys(headroom, {"package", "version", "profile", "beacon"}, "headroom")
    if headroom != {
        "package": "headroom-ai[proxy]",
        "version": "0.37.0",
        "profile": "coding",
        "beacon": "off",
    }:
        raise ManifestError("Headroom settings differ from the frozen pilot")

    limits = _require_object(payload["limits"], "limits")
    _require_exact_keys(
        limits,
        {"total_spend_usd", "provider_requests_per_run", "wall_seconds_per_run"},
        "limits",
    )
    if _require_decimal(limits["total_spend_usd"], "limits.total_spend_usd") != Decimal("10.00"):
        raise ManifestError("total spend cap must be exactly 10.00")
    if (
        limits["provider_requests_per_run"] != request_limit
        or limits["wall_seconds_per_run"] != 180
    ):
        raise ManifestError("request/time limits differ from the selected frozen pilot")

    analysis = _require_object(payload["analysis"], "analysis")
    _require_exact_keys(
        analysis,
        {
            "primary_contrast",
            "estimand",
            "action_threshold_fraction",
            "alpha_two_sided",
            "power",
            "confirmatory_repeats",
            "sample_approximation",
        },
        "analysis",
    )
    if analysis != {
        "primary_contrast": ["laconic", "native"],
        "estimand": "paired_task_log_cost_difference_of_cell_means",
        "action_threshold_fraction": "0.10",
        "alpha_two_sided": "0.05",
        "power": "0.80",
        "confirmatory_repeats": 2,
        "sample_approximation": "normal",
    }:
        raise ManifestError("analysis differs from the frozen pilot")

    if payload["stopping_rules"] != list(stopping_rules):
        raise ManifestError("stopping_rules differ from the selected frozen pilot")
    if payload["public_report_keys"] != list(public_report_keys):
        raise ManifestError("public_report_keys differ from the selected frozen pilot")

    tasks = _parse_tasks(payload["tasks"], prompt_file=prompt_file)
    runs = _parse_runs(
        payload["run_order"],
        {task.task_id for task in tasks},
        random_seed=random_seed,
    )
    return PilotManifest(payload=payload, tasks=tasks, run_order=runs)


def validate_manifest_file(path: Path) -> PilotManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError("manifest is not readable canonical JSON") from error
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be an object")
    manifest = validate_manifest_json(cast(dict[str, Any], payload))
    if path.read_bytes() != canonical_json(payload):
        raise ManifestError("manifest must use canonical JSON serialization")
    return manifest


def materialize_task(task: TaskSpec, destination: Path) -> None:
    if destination.exists():
        raise ManifestError("task destination must not already exist")
    source = FIXTURES_ROOT / task.task_id / task.source_dir
    if tree_digest(source) != task.source_sha256:
        raise ManifestError(f"fixture source digest drifted for {task.task_id}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Laconic Pilot",
        "GIT_AUTHOR_EMAIL": "pilot@invalid",
        "GIT_COMMITTER_NAME": "Laconic Pilot",
        "GIT_COMMITTER_EMAIL": "pilot@invalid",
    }
    subprocess.run(["git", "init", "-q"], cwd=destination, env=env, check=True)
    subprocess.run(["git", "add", "--all"], cwd=destination, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=destination, env=env, check=True)


def _run_completion(task: TaskSpec, cwd: Path) -> subprocess.CompletedProcess[str]:
    command = (sys.executable, *task.completion_command[1:])
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def verify_completion_oracles(manifest: PilotManifest) -> None:
    with tempfile.TemporaryDirectory(prefix="laconic-controlled-spend-oracles-") as raw:
        root = Path(raw)
        for task in manifest.tasks:
            worktree = root / task.task_id
            materialize_task(task, worktree)
            before = _run_completion(task, worktree)
            if before.returncode == 0:
                raise ManifestError(
                    f"completion oracle unexpectedly passes before fix for {task.task_id}"
                )
            patch = FIXTURES_ROOT / task.task_id / task.solution_patch
            applied = subprocess.run(
                ["git", "apply", "--unidiff-zero", str(patch)],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )
            if applied.returncode != 0:
                raise ManifestError(f"solution patch cannot be applied for {task.task_id}")
            after = _run_completion(task, worktree)
            if after.returncode != 0:
                raise ManifestError(
                    f"completion oracle still fails after reference fix for {task.task_id}"
                )
