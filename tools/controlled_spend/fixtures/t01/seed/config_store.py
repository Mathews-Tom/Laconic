from __future__ import annotations

from collections.abc import Iterable, Mapping

Scalar = str | int | bool
Config = dict[str, Scalar]


def merge_layers(defaults: Mapping[str, Scalar], layers: Iterable[Mapping[str, Scalar]]) -> Config:
    """Return defaults overlaid by each layer from lowest to highest priority."""
    merged = dict(defaults)
    for layer in layers:
        for key, value in layer.items():
            merged.setdefault(key, value)
    return merged


def render_config(config: Mapping[str, Scalar]) -> str:
    return "\n".join(f"{key}={config[key]}" for key in sorted(config))
