from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Mapping, Sequence


class DependencyCycleError(ValueError):
    pass


def dependency_order(graph: Mapping[str, Sequence[str]]) -> list[str]:
    """Return a deterministic topological order for item -> dependencies."""
    nodes = set(graph)
    for dependencies in graph.values():
        nodes.update(dependencies)
    indegree = {node: 0 for node in nodes}
    dependents: dict[str, list[str]] = defaultdict(list)
    for item, dependencies in graph.items():
        for dependency in dependencies:
            indegree[dependency] += 1
            dependents[item].append(dependency)
    ready = [node for node, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        ordered.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(nodes):
        raise DependencyCycleError("dependency cycle")
    return ordered
