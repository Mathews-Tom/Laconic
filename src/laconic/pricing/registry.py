"""Resolve per-model list prices, with provenance.

`laconic.costs` originally carried seven hand-written models and charged
Sonnet rates for everything else. On a real corpus that fallback covered
**75% of the cost the avoided-cost estimate is built from**, so the
estimate's per-token rates — which are divided out of that same cost —
rested on prices nobody published.

Hand-maintaining a larger table relocates the problem rather than solving
it: it goes stale exactly when nobody has time to update it. This module
instead resolves prices from a registry with three layers, most specific
first:

1. a **local override** file, for models no public registry knows — this
   corpus has four, including a synthetic fixture model and a floating
   `-latest` alias;
2. a **downloaded** registry, refreshed only by an explicit
   ``laconic pricing update``;
3. a **bundled snapshot**, pinned to one upstream commit and shipped in
   the wheel.

The bundled layer is what keeps the default path offline. Nothing here
ever fetches on its own: this tool sends no telemetry and makes no network
call a user did not ask for, and a price lookup silently reaching the
internet would break that contract far more quietly than it would fix a
price.

Every resolution carries its source, so a report can state which registry
produced a figure rather than leaving the reader to assume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final

#: The upstream file the bundled snapshot was trimmed from, pinned to a
#: commit rather than a branch. A branch would make the bundled prices
#: change under a rebuild, which is the one thing a *snapshot* must not do.
UPSTREAM_COMMIT: Final = "71f45683d73d741db4e8c0801045b75296ee7b54"
UPSTREAM_URL: Final = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    f"{UPSTREAM_COMMIT}/model_prices_and_context_window.json"
)

#: Filenames under the runtime data directory.
DOWNLOAD_FILENAME: Final = "model-prices.json"
OVERRIDE_FILENAME: Final = "model-prices-override.json"

_SNAPSHOT_PACKAGE: Final = "laconic.pricing"
_SNAPSHOT_RESOURCE: Final = "snapshot.json"


@dataclass(frozen=True, slots=True)
class ModelRate:
    """Per-token prices for one model, in USD.

    ``cache_read`` and ``cache_write`` are optional because not every
    provider publishes them. When absent the caller applies its own
    multipliers, which is what the whole table did before this module
    existed.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


@dataclass(frozen=True, slots=True)
class PriceRegistry:
    """Resolved prices plus where they came from."""

    rates: dict[str, ModelRate]
    source: str
    source_commit: str | None
    overrides: int

    def get(self, model: str) -> ModelRate | None:
        return self.rates.get(model)


def _parse(payload: Any) -> dict[str, ModelRate]:
    if not isinstance(payload, dict):
        return {}
    models = payload.get("models")
    if not isinstance(models, dict):
        return {}
    rates: dict[str, ModelRate] = {}
    for name, row in models.items():
        if not isinstance(name, str) or not isinstance(row, dict):
            continue
        value = row.get("input")
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            continue

        def _rate(key: str, source: dict[str, Any] = row) -> float | None:
            found = source.get(key)
            if isinstance(found, bool) or not isinstance(found, int | float):
                return None
            return float(found) if found > 0 else None

        output = _rate("output") or 0.0
        rates[name] = ModelRate(
            input=float(value),
            output=output,
            cache_read=_rate("cache_read"),
            cache_write=_rate("cache_write"),
        )
    return rates


def _read(path: Path) -> dict[str, Any] | None:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A damaged registry must never take the whole command down: the
        # next layer still prices, and the layer after that always does.
        return None
    return loaded if isinstance(loaded, dict) else None


def load_registry(data_dir: Path | None = None) -> PriceRegistry:
    """Resolve the registry from the bundled, downloaded, and override layers."""
    try:
        bundled: Any = json.loads(
            resources.files(_SNAPSHOT_PACKAGE).joinpath(_SNAPSHOT_RESOURCE).read_text("utf-8")
        )
    except (OSError, ValueError):
        # The bundled snapshot is packaging data, and packaging data is
        # exactly what goes missing silently. Degrade the same way the
        # other two layers do rather than taking down every command that
        # prices anything, which is most of them.
        bundled = None
    rates = _parse(bundled) if bundled is not None else {}
    source = f"bundled snapshot ({UPSTREAM_COMMIT[:12]})"
    commit: str | None = UPSTREAM_COMMIT

    if data_dir is not None:
        downloaded = _read(data_dir / DOWNLOAD_FILENAME)
        if downloaded is not None:
            parsed = _parse(downloaded)
            if parsed:
                rates = {**rates, **parsed}
                found = downloaded.get("source_commit")
                commit = found if isinstance(found, str) else None
                source = f"downloaded registry ({commit[:12] if commit else 'unpinned'})"

        override = _read(data_dir / OVERRIDE_FILENAME)
        overridden = _parse(override) if override is not None else {}
        if overridden:
            return PriceRegistry(
                rates={**rates, **overridden},
                source=f"{source} + {len(overridden)} local override(s)",
                source_commit=commit,
                overrides=len(overridden),
            )

    return PriceRegistry(rates=rates, source=source, source_commit=commit, overrides=0)
