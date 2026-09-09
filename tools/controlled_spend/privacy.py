"""Independent exact-key privacy gate for the M20 public report."""

from __future__ import annotations

import math
from typing import Any, Final, cast

from tools.controlled_spend.manifest import validate_manifest_file

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
_INT_KEYS: Final = frozenset(
    {
        "schema_version",
        "run_count",
        "expected_run_count",
        "completion_failures",
        "mechanism_failures",
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


def validate_public_report(payload: dict[str, Any]) -> None:
    """Validate the only public shape the controlled pilot may serialize."""
    allowed = frozenset(validate_manifest_file().payload["public_report_keys"])
    if set(payload) != allowed:
        extra = sorted(set(payload) - allowed)
        missing = sorted(allowed - set(payload))
        raise PrivacyViolationError(f"public report keys differ: missing={missing} extra={extra}")
    for key in _INT_KEYS:
        _non_negative_int(key, payload[key])
    for key in _FLOAT_KEYS:
        value = _number(key, payload[key])
        if cast(float, value) < 0:
            raise PrivacyViolationError(f"{key} must not be negative")
    for key in _NULLABLE_FLOAT_KEYS:
        _number(key, payload[key], nullable=True)
    if payload["schema_version"] != 1 or payload["expected_run_count"] != 24:
        raise PrivacyViolationError("public report differs from the frozen schema or population")
    if (
        payload["action_threshold_fraction"] != 0.10
        or payload["alpha_two_sided"] != 0.05
        or payload["power"] != 0.80
        or payload["total_cap_usd"] != 10.0
    ):
        raise PrivacyViolationError("public report differs from the frozen statistical contract")
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
    verdict = payload["verdict"]
    if verdict not in {"complete", "incomplete"}:
        raise PrivacyViolationError("verdict is outside the closed vocabulary")
    task_count = payload["confirmatory_task_count_at_two_repeats"]
    if task_count is not None:
        if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 2:
            raise PrivacyViolationError(
                "confirmatory_task_count_at_two_repeats must be null or at least two"
            )
    if payload["run_count"] > payload["expected_run_count"]:
        raise PrivacyViolationError("run_count exceeds the frozen population")
    if (
        payload["completion_failures"] + payload["mechanism_failures"]
        > payload["expected_run_count"]
    ):
        raise PrivacyViolationError("failure counts exceed the frozen population")
    statistical_keys = (*_NULLABLE_FLOAT_KEYS, "confirmatory_task_count_at_two_repeats")
    if verdict == "complete":
        if (
            payload["run_count"] != payload["expected_run_count"]
            or payload["completion_failures"] != 0
            or payload["mechanism_failures"] != 0
            or any(payload[key] is None for key in statistical_keys)
        ):
            raise PrivacyViolationError("complete report does not satisfy the frozen validity gate")
    elif any(payload[key] is not None for key in statistical_keys):
        raise PrivacyViolationError("incomplete report must suppress every statistical output")
    if payload["gateway_spend_usd"] > payload["total_cap_usd"]:
        raise PrivacyViolationError("gateway spend exceeds the frozen cap")
