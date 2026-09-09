"""External single-use execution authorization for the frozen M20-v2 pilot.

The committed v2 manifest carries ``execution_authorized: false`` and every
validating command rejects a v2 manifest whose value is not exactly ``false``.
That field is a permanent statement that the study contract does not authorize
itself; it is not an operator switch, because changing it would change the
manifest bytes and therefore the committed digest the owner reviewed.

Provider execution is therefore authorized outside the study by a private,
single-use receipt. A receipt may permit one campaign against one manifest
digest, one study, one canonical artifact root, and one cap. It can never
select a different population, model, task, profile, limit, or price.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, cast

from tools.controlled_spend.manifest import (
    PACKAGE_ROOT,
    V2_SCHEMA_VERSION,
    PilotManifest,
    canonical_json,
)

AUTHORIZATION_SCHEMA_VERSION: Final = 1
V2_STUDY_ID: Final = "m20-variance-pilot-v2"
REPOSITORY_ROOT: Final = PACKAGE_ROOT.parent.parent
AUTHORIZATION_KEYS: Final = frozenset(
    {
        "artifact_root",
        "authorization_id",
        "authorized_at",
        "execution_authorized",
        "manifest_sha256",
        "schema_version",
        "single_use",
        "study_id",
        "total_spend_usd",
    }
)
_HEX_64: Final = frozenset("0123456789abcdef")


class PilotAuthorizationError(ValueError):
    """Raised when external pilot execution authorization is absent or unusable."""


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """One validated capability to execute one frozen pilot campaign once."""

    authorization_id: str
    study_id: str
    manifest_sha256: str
    artifact_root: Path
    total_spend_usd: Decimal
    authorized_at: str
    receipt_path: Path
    receipt_sha256: str


def _validate_private_directory(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise PilotAuthorizationError(
            f"cannot stat authorization receipt directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise PilotAuthorizationError(
            "authorization receipt directory must be a non-symlink directory"
        )
    if entry_stat.st_uid != os.getuid():
        raise PilotAuthorizationError(
            "authorization receipt directory must be owned by the current user"
        )
    if entry_stat.st_mode & 0o777 != 0o700:
        raise PilotAuthorizationError("authorization receipt directory must have mode 0700")


def _validate_private_file(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise PilotAuthorizationError(
            f"cannot stat authorization receipt {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise PilotAuthorizationError("authorization receipt must be a non-symlink regular file")
    if entry_stat.st_uid != os.getuid():
        raise PilotAuthorizationError("authorization receipt must be owned by the current user")
    if entry_stat.st_mode & 0o777 != 0o600:
        raise PilotAuthorizationError("authorization receipt must have mode 0600")


def _require_hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _HEX_64:
        raise PilotAuthorizationError(f"{field} must be a 64-character lowercase hexadecimal value")
    return value


def _require_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise PilotAuthorizationError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise PilotAuthorizationError(f"{field} must be a decimal string") from error
    if not result.is_finite() or result <= 0:
        raise PilotAuthorizationError(f"{field} must be finite and positive")
    return result


def _campaign_cap(manifest: PilotManifest) -> Decimal:
    limits = cast(dict[str, Any], manifest.payload["limits"])
    return Decimal(cast(str, limits["total_spend_usd"]))


def _containment_path(path: Path) -> Path:
    """Return a symlink- and case-normalized path for containment comparison only."""
    resolved = Path(os.path.realpath(path))
    return Path(os.path.normcase(resolved))


def load_execution_authorization(
    receipt_path: Path,
    *,
    manifest: PilotManifest,
    artifact_root: Path,
    excluded_roots: tuple[Path, ...] = (),
) -> ExecutionAuthorization:
    """Validate one external receipt before any credential, environment, or provider work."""
    if manifest.payload["schema_version"] != V2_SCHEMA_VERSION:
        raise PilotAuthorizationError("only the M20-v2 pilot may be executed")
    receipt = receipt_path.expanduser().absolute()
    root = artifact_root.expanduser().absolute()
    contained = _containment_path(receipt)
    for excluded in (REPOSITORY_ROOT, root, *excluded_roots):
        resolved = _containment_path(excluded.expanduser().absolute())
        if contained == resolved or contained.is_relative_to(resolved):
            raise PilotAuthorizationError(
                "authorization receipt must live outside the repository, the artifact root, "
                "and every live-state root"
            )
    _validate_private_file(receipt)
    _validate_private_directory(receipt.parent)

    raw = receipt.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PilotAuthorizationError(
            "authorization receipt is not readable canonical JSON"
        ) from error
    if not isinstance(payload, dict):
        raise PilotAuthorizationError("authorization receipt must be an object")
    document = cast(dict[str, Any], payload)
    actual = set(document)
    if actual != set(AUTHORIZATION_KEYS):
        raise PilotAuthorizationError(
            f"authorization receipt keys differ: missing={sorted(AUTHORIZATION_KEYS - actual)} "
            f"extra={sorted(actual - AUTHORIZATION_KEYS)}"
        )
    if raw != canonical_json(document):
        raise PilotAuthorizationError("authorization receipt must use canonical JSON serialization")

    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or schema_version != AUTHORIZATION_SCHEMA_VERSION:
        raise PilotAuthorizationError("authorization receipt schema_version is not supported")
    authorization_id = _require_hex64(document["authorization_id"], "authorization_id")
    study_id = document["study_id"]
    if study_id != V2_STUDY_ID or study_id != manifest.payload["study_id"]:
        raise PilotAuthorizationError("authorization receipt names a different study")
    manifest_sha256 = _require_hex64(document["manifest_sha256"], "manifest_sha256")
    if manifest_sha256 != manifest.digest:
        raise PilotAuthorizationError("authorization receipt is bound to a different manifest")
    if document["artifact_root"] != str(root):
        raise PilotAuthorizationError("authorization receipt is bound to a different artifact root")
    total_spend_usd = _require_decimal(document["total_spend_usd"], "total_spend_usd")
    if total_spend_usd != _campaign_cap(manifest):
        raise PilotAuthorizationError(
            "authorization receipt cap differs from the frozen campaign cap"
        )
    authorized_at = document["authorized_at"]
    if not isinstance(authorized_at, str):
        raise PilotAuthorizationError("authorized_at must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(authorized_at)
    except ValueError as error:
        raise PilotAuthorizationError("authorized_at must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise PilotAuthorizationError("authorized_at must carry an explicit UTC offset")
    if document["execution_authorized"] is not True:
        raise PilotAuthorizationError("authorization receipt does not authorize execution")
    if document["single_use"] is not True:
        raise PilotAuthorizationError("authorization receipt must be single use")
    if root.exists() or root.is_symlink():
        raise PilotAuthorizationError(
            "authorized artifact root already exists; a campaign is never resumed"
        )

    return ExecutionAuthorization(
        authorization_id=authorization_id,
        study_id=study_id,
        manifest_sha256=manifest_sha256,
        artifact_root=root,
        total_spend_usd=total_spend_usd,
        authorized_at=authorized_at,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(raw).hexdigest(),
    )


def verify_execution_authorization(
    authorization: ExecutionAuthorization,
    *,
    manifest: PilotManifest,
    artifact_root: Path,
) -> None:
    """Re-check that a validated capability still binds this manifest, study, root, and cap."""
    if manifest.payload["schema_version"] != V2_SCHEMA_VERSION:
        raise PilotAuthorizationError("only the M20-v2 pilot may be executed")
    if (
        authorization.study_id != V2_STUDY_ID
        or authorization.study_id != manifest.payload["study_id"]
    ):
        raise PilotAuthorizationError("authorization receipt names a different study")
    if authorization.manifest_sha256 != manifest.digest:
        raise PilotAuthorizationError("authorization receipt is bound to a different manifest")
    if authorization.artifact_root != artifact_root.expanduser().absolute():
        raise PilotAuthorizationError("authorization receipt is bound to a different artifact root")
    if authorization.total_spend_usd != _campaign_cap(manifest):
        raise PilotAuthorizationError(
            "authorization receipt cap differs from the frozen campaign cap"
        )


def consume_execution_authorization(authorization: ExecutionAuthorization) -> None:
    """Destroy a validated receipt so it can never authorize a second campaign."""
    _validate_private_file(authorization.receipt_path)
    digest = hashlib.sha256(authorization.receipt_path.read_bytes()).hexdigest()
    if digest != authorization.receipt_sha256:
        raise PilotAuthorizationError("authorization receipt changed after validation")
    authorization.receipt_path.unlink()
    if authorization.receipt_path.exists() or authorization.receipt_path.is_symlink():
        raise PilotAuthorizationError("authorization receipt could not be consumed")
