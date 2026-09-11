"""The price registry resolves by layer and never fetches on its own."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from laconic.costs import (
    ModelUsage,
    configure_pricing,
    price_for,
    reset_registry_cache,
    session_cost,
    unpriced_models,
)
from laconic.pricing.registry import (
    DOWNLOAD_FILENAME,
    OVERRIDE_FILENAME,
    UPSTREAM_COMMIT,
    load_registry,
)
from laconic.pricing.update import PriceUpdateError, write_registry


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Reset before *and* after: the registry is process-wide state, and a
    test that leaves an override configured would price every later test."""
    configure_pricing(None)
    yield
    configure_pricing(None)
    reset_registry_cache()


def _write(path: Path, models: dict[str, dict[str, float]], commit: str = "abc123") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "source": "t", "source_commit": commit, "models": models}),
        encoding="utf-8",
    )


def test_the_bundled_snapshot_prices_without_a_network_call(tmp_path: Path) -> None:
    """The default path must work offline.

    A price lookup that reached the internet would break the no-telemetry
    contract far more quietly than it would fix a price, so the bundled
    layer has to be sufficient on its own.
    """
    registry = load_registry(tmp_path)

    assert registry.source_commit == UPSTREAM_COMMIT
    assert len(registry.rates) > 1000
    assert registry.get("claude-opus-5") is not None


def test_a_local_override_beats_every_downloaded_price(tmp_path: Path) -> None:
    """The layer that exists for models no public registry knows."""
    _write(tmp_path / DOWNLOAD_FILENAME, {"m": {"input": 1e-6, "output": 2e-6}})
    _write(tmp_path / OVERRIDE_FILENAME, {"m": {"input": 9e-6, "output": 9e-6}})

    registry = load_registry(tmp_path)
    rate = registry.get("m")

    assert rate is not None
    assert rate.input == 9e-6
    assert registry.overrides == 1


def test_a_damaged_registry_falls_through_instead_of_failing(tmp_path: Path) -> None:
    """A corrupt cache costs a layer, never the command."""
    (tmp_path / DOWNLOAD_FILENAME).write_bytes(b"\xff\xfe not json")

    registry = load_registry(tmp_path)

    assert registry.get("claude-opus-5") is not None


def test_published_cache_rates_replace_the_fixed_multipliers(tmp_path: Path) -> None:
    """The multipliers were an approximation the registry can now correct.

    Exercised through `session_cost`, not through the registry object:
    the layers were once resolved correctly for display while every
    reported figure silently ignored them, and only a test that prices
    something can tell the difference.
    """
    _write(
        tmp_path / OVERRIDE_FILENAME,
        {"m": {"input": 1e-6, "output": 1e-6, "cache_read": 5e-9, "cache_write": 7e-6}},
    )
    configure_pricing(tmp_path)

    cost = session_cost(
        {
            "m": ModelUsage(
                input_tokens=0, cache_read=1_000_000, cache_write=1_000_000, output_tokens=0
            )
        }
    )

    # Published rates, not 0.10x/1.25x of the $1/Mtok input price.
    assert cost.cache_read == pytest.approx(0.005)
    assert cost.cache_write == pytest.approx(7.0)


def test_an_override_reaches_the_reported_figures_not_only_pricing_show(
    tmp_path: Path,
) -> None:
    """The layers must price, not merely resolve.

    `price_for` and `unpriced_models` once called the registry with no
    data directory while only `pricing show` passed one, so a downloaded
    registry and an override file had zero effect on any published
    number. Two commands disagreeing by construction is worse than no
    override mechanism at all.
    """
    _write(tmp_path / OVERRIDE_FILENAME, {"only-here": {"input": 4e-6, "output": 8e-6}})
    configure_pricing(tmp_path)

    assert price_for("only-here").input_per_mtok == pytest.approx(4.0)
    assert unpriced_models({"only-here": ModelUsage(1, 0, 0, 0)}) == []


def test_an_unknown_model_still_falls_back_rather_than_vanishing() -> None:
    price = price_for("a-model-no-registry-carries")

    assert price.input_per_mtok == 3.0
    assert price.cache_write_per_mtok is None
    assert price.cache_write_rate == pytest.approx(3.75)


def test_the_updater_refuses_a_registry_with_no_priced_model(tmp_path: Path) -> None:
    """An empty or malformed upstream must not silently replace a good cache."""
    with pytest.raises(PriceUpdateError, match="no priced model"):
        write_registry(tmp_path, {})


def test_a_written_registry_round_trips(tmp_path: Path) -> None:
    result = write_registry(tmp_path, {"m": {"input": 1e-6, "output": 2e-6}}, source_commit="zz")

    assert result.models == 1
    reloaded = load_registry(tmp_path)
    assert reloaded.source_commit == "zz"
    rate = reloaded.get("m")
    assert rate is not None and rate.input == 1e-6


def test_cost_uses_the_registry_for_a_model_outside_the_hand_written_table() -> None:
    """The whole point: 75% of one real corpus priced at a guess before this."""
    usage = {
        "claude-opus-5": ModelUsage(
            input_tokens=1_000_000, cache_read=0, cache_write=0, output_tokens=0
        )
    }

    cost = session_cost(usage)

    # Opus input is $5/Mtok, not the $3 Sonnet fallback it used to get.
    assert cost.uncached_input == pytest.approx(5.0)
