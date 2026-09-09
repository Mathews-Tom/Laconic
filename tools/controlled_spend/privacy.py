"""Independent exact-key privacy gate for the M20 public report."""

from __future__ import annotations

import math
from typing import Any, Final, cast

from tools.controlled_spend.manifest import PilotManifest

_HEX_64: Final = frozenset("0123456789abcdef")
_NULLABLE_FLOAT_KEYS: Final = frozenset(
    {
        "paired_log_cost_sd",
        "native_laconic_correlation",
        "native_headroom_correlation",
    }
)
_FLOAT_KEYS: Final = frozenset(
    {
        "action_threshold_fraction",
        "alpha_two_sided",
        "power",
        "total_cap_usd",
        "gateway_spend_usd",
    }
)
_V1_INT_KEYS: Final = frozenset(
    {
        "schema_version",
        "run_count",
        "expected_run_count",
        "completion_failures",
        "mechanism_failures",
    }
)
_V2_INT_KEYS: Final = frozenset(
    {
        "schema_version",
        "attempted_cells",
        "valid_cells",
        "expected_cell_count",
        "unrun_cells",
        "task_completion_failures",
        "protocol_failures",
        "mechanism_non_engagement",
    }
)


class PrivacyViolationError(ValueError):
    """Raised when a public pilot artifact is not content-free and claim-safe."""


def _number(field: str, value: Any, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PrivacyViolationError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise PrivacyViolationError(f"{field} must be finite")
    return result


def _non_negative_int(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PrivacyViolationError(f"{field} must be a non-negative integer")
    return value


def validate_public_report(payload: dict[str, Any], *, manifest: PilotManifest) -> None:
    """Validate the only public shape the selected controlled pilot may serialize."""
    allowed = frozenset(manifest.payload["public_report_keys"])
    if set(payload) != allowed:
        extra = sorted(set(payload) - allowed)
        missing = sorted(allowed - set(payload))
        raise PrivacyViolationError(f"public report keys differ: missing={missing} extra={extra}")
    schema_version = manifest.payload["schema_version"]
    if schema_version == 1:
        int_keys = _V1_INT_KEYS
        expected_count_key = "expected_run_count"
    elif schema_version == 2:
        int_keys = _V2_INT_KEYS
        expected_count_key = "expected_cell_count"
    else:
        raise PrivacyViolationError("selected manifest schema is unsupported")
    for key in int_keys:
        _non_negative_int(key, payload[key])
    for key in _FLOAT_KEYS:
        value = _number(key, payload[key])
        if cast(float, value) < 0:
            raise PrivacyViolationError(f"{key} must not be negative")
    for key in _NULLABLE_FLOAT_KEYS:
        _number(key, payload[key], nullable=True)
    if payload["schema_version"] != schema_version or payload[expected_count_key] != len(
        manifest.run_order
    ):
        raise PrivacyViolationError("public report differs from the selected schema or population")
    analysis = cast(dict[str, Any], manifest.payload["analysis"])
    limits = cast(dict[str, Any], manifest.payload["limits"])
    if (
        payload["action_threshold_fraction"] != float(analysis["action_threshold_fraction"])
        or payload["alpha_two_sided"] != float(analysis["alpha_two_sided"])
        or payload["power"] != float(analysis["power"])
        or payload["total_cap_usd"] != float(limits["total_spend_usd"])
    ):
        raise PrivacyViolationError("public report differs from the selected statistical contract")
    dispersion = payload["paired_log_cost_sd"]
    if dispersion is not None and dispersion < 0:
        raise PrivacyViolationError("paired_log_cost_sd must not be negative")
    for key in ("native_laconic_correlation", "native_headroom_correlation"):
        value = payload[key]
        if value is not None and not -1 <= value <= 1:
            raise PrivacyViolationError(f"{key} must be within [-1, 1]")
    manifest_hash = payload["manifest_hash"]
    if (
        not isinstance(manifest_hash, str)
        or len(manifest_hash) != 64
        or set(manifest_hash) - _HEX_64
    ):
        raise PrivacyViolationError("manifest_hash must be a lowercase SHA-256 digest")
    if manifest_hash != manifest.digest:
        raise PrivacyViolationError("manifest_hash does not match the selected manifest")
    verdict = payload["verdict"]
    if verdict not in {"complete", "incomplete"}:
        raise PrivacyViolationError("verdict is outside the closed vocabulary")
    task_count = payload["confirmatory_task_count_at_two_repeats"]
    if task_count is not None:
        if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 2:
            raise PrivacyViolationError(
                "confirmatory_task_count_at_two_repeats must be null or at least two"
            )
    statistical_keys = (*_NULLABLE_FLOAT_KEYS, "confirmatory_task_count_at_two_repeats")
    if schema_version == 1:
        if payload["run_count"] > payload["expected_run_count"]:
            raise PrivacyViolationError("run_count exceeds the frozen population")
        if (
            payload["completion_failures"] + payload["mechanism_failures"]
            > payload["expected_run_count"]
        ):
            raise PrivacyViolationError("failure counts exceed the frozen population")
        complete = (
            payload["run_count"] == payload["expected_run_count"]
            and payload["completion_failures"] == 0
            and payload["mechanism_failures"] == 0
        )
    else:
        if payload["attempted_cells"] + payload["unrun_cells"] != payload["expected_cell_count"]:
            raise PrivacyViolationError("attempted and unrun cells do not partition the population")
        if (
            payload["valid_cells"]
            + payload["task_completion_failures"]
            + payload["protocol_failures"]
            + payload["mechanism_non_engagement"]
            != payload["attempted_cells"]
        ):
            raise PrivacyViolationError("v2 dispositions do not partition attempted cells")
        complete = (
            payload["attempted_cells"] == payload["valid_cells"] == payload["expected_cell_count"]
            and payload["unrun_cells"] == 0
            and payload["task_completion_failures"] == 0
            and payload["protocol_failures"] == 0
            and payload["mechanism_non_engagement"] == 0
        )
    if verdict == "complete":
        if not complete or any(payload[key] is None for key in statistical_keys):
            raise PrivacyViolationError("complete report does not satisfy the frozen validity gate")
    elif any(payload[key] is not None for key in statistical_keys):
        raise PrivacyViolationError("incomplete report must suppress every statistical output")
    if payload["gateway_spend_usd"] > payload["total_cap_usd"]:
        raise PrivacyViolationError("gateway spend exceeds the frozen cap")
