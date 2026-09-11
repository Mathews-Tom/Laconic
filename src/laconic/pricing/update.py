"""Fetch and store a refreshed price registry.

The only code in Laconic that reaches the network, and it runs only when
a user types ``laconic pricing update``. Nothing else fetches, nothing
fetches on a schedule, and a price lookup never reaches the internet on
its own -- a tool that sends no telemetry cannot start making silent
outbound requests to correct a rounding error.

The download is trimmed to the four fields the cost model uses before it
is written. The upstream file is 2.3 MB of context windows, modalities,
and provider metadata; storing all of it would mean carrying an
unreviewed blob in the user's data directory in order to read four
numbers per model.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from laconic.pricing.registry import DOWNLOAD_FILENAME, UPSTREAM_COMMIT, UPSTREAM_URL

#: Bounded so a hung mirror fails the command rather than the session.
FETCH_TIMEOUT_SECONDS: Final = 30.0

#: Refuse anything implausible for a price table, before parsing it.
MAX_DOWNLOAD_BYTES: Final = 32 * 1024 * 1024


class PriceUpdateError(RuntimeError):
    """Raised when a refresh cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What a completed refresh wrote."""

    path: Path
    models: int
    source_commit: str


def _trim(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        raise PriceUpdateError("upstream registry is not a JSON object")
    trimmed: dict[str, dict[str, float]] = {}
    for name, entry in payload.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        value = entry.get("input_cost_per_token")
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            continue
        output = entry.get("output_cost_per_token")
        row: dict[str, float] = {
            "input": float(value),
            "output": float(output) if isinstance(output, int | float) else 0.0,
        }
        for key, field in (
            ("cache_read", "cache_read_input_token_cost"),
            ("cache_write", "cache_creation_input_token_cost"),
        ):
            found = entry.get(field)
            if not isinstance(found, bool) and isinstance(found, int | float) and found > 0:
                row[key] = float(found)
        trimmed[name] = row
    if not trimmed:
        raise PriceUpdateError("upstream registry carried no priced model")
    return trimmed


def fetch_registry(url: str = UPSTREAM_URL) -> dict[str, dict[str, float]]:
    """Download and trim the upstream registry."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
    except OSError as error:
        raise PriceUpdateError(f"could not fetch {url}: {error}") from error
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise PriceUpdateError("upstream registry is implausibly large; refusing it")
    try:
        payload: Any = json.loads(raw)
    except ValueError as error:
        raise PriceUpdateError("upstream registry is not valid JSON") from error
    return _trim(payload)


def write_registry(
    data_dir: Path,
    models: dict[str, dict[str, float]],
    *,
    source_commit: str = UPSTREAM_COMMIT,
    url: str = UPSTREAM_URL,
) -> UpdateResult:
    """Atomically replace the downloaded registry under ``data_dir``."""
    if not models:
        # Writing an empty registry would replace a good cache with one
        # that prices nothing, silently sending every model back to the
        # fallback. Refuse rather than persist it.
        raise PriceUpdateError("refusing to write a registry with no priced model")
    document = {
        "schema_version": 1,
        "source": url,
        "source_commit": source_commit,
        "models": dict(sorted(models.items())),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / DOWNLOAD_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=data_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=1, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return UpdateResult(path=target, models=len(models), source_commit=source_commit)
