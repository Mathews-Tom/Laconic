from __future__ import annotations

import shlex


class AssignmentError(ValueError):
    pass


def parse_assignment(line: str) -> tuple[str, str]:
    """Parse one shell-like KEY=VALUE record with optional trailing comment."""
    candidate = line.split("#", 1)[0].strip()
    if not candidate or "=" not in candidate:
        raise AssignmentError("expected KEY=VALUE")
    key, raw_value = candidate.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "a").isalnum():
        raise AssignmentError("invalid key")
    try:
        parts = shlex.split(raw_value, comments=False, posix=True)
    except ValueError as error:
        raise AssignmentError("invalid quoted value") from error
    if len(parts) != 1:
        raise AssignmentError("value must be one shell word")
    return key, parts[0]
