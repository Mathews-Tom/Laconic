from __future__ import annotations

import unittest

from config_store import merge_layers, render_config


class MergeLayersTests(unittest.TestCase):
    def test_later_layers_override_earlier_values(self) -> None:
        result = merge_layers(
            {"host": "localhost", "port": 8080, "debug": False},
            ({"port": 9000, "region": "us"}, {"port": 9443, "debug": True}),
        )
        self.assertEqual(
            result,
            {"host": "localhost", "port": 9443, "debug": True, "region": "us"},
        )

    def test_render_is_stable(self) -> None:
        self.assertEqual(render_config({"z": 2, "a": 1}), "a=1\nz=2")


if __name__ == "__main__":
    unittest.main()
