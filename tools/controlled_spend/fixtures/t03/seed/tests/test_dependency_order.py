from __future__ import annotations

import unittest

from dependency_order import DependencyCycleError, dependency_order


class DependencyOrderTests(unittest.TestCase):
    def test_dependencies_precede_dependents(self) -> None:
        graph = {"deploy": ["build", "audit"], "build": ["compile"], "audit": []}
        order = dependency_order(graph)
        for item, dependencies in graph.items():
            for dependency in dependencies:
                self.assertLess(order.index(dependency), order.index(item))

    def test_independent_items_are_deterministic(self) -> None:
        self.assertEqual(dependency_order({"beta": [], "alpha": []}), ["alpha", "beta"])

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaises(DependencyCycleError):
            dependency_order({"a": ["b"], "b": ["a"]})


if __name__ == "__main__":
    unittest.main()
