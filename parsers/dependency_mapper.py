"""Build and reconcile the dependency DAG of the legacy estate.

Two graphs, deliberately kept apart:

  declared   what the manifest says the nightly scheduler enforces today
  inferred   what the scripts' own input/output datasets imply

Migration order has to follow the union. Where they disagree the difference is
reported rather than silently merged: a declared edge with no data reason is
usually a scheduling artefact that can be dropped, and an inferred edge with no
declared counterpart is a hidden coupling nobody documented - which is exactly
the thing that breaks a phased cutover.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from config import Unit, load_manifest

logger = logging.getLogger(__name__)


@dataclass
class DependencyGraph:
    nodes: List[str] = field(default_factory=list)
    declared_edges: Set[Tuple[str, str]] = field(default_factory=set)
    inferred_edges: Set[Tuple[str, str]] = field(default_factory=set)
    shared_inputs: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def edges(self) -> Set[Tuple[str, str]]:
        return self.declared_edges | self.inferred_edges

    @property
    def undocumented_edges(self) -> Set[Tuple[str, str]]:
        """Inferred couplings the manifest never mentions."""
        return self.inferred_edges - self.declared_edges

    @property
    def unjustified_edges(self) -> Set[Tuple[str, str]]:
        """Declared edges with no shared-data reason behind them."""
        return self.declared_edges - self.inferred_edges


def build(units: Optional[List[Unit]] = None) -> DependencyGraph:
    units = units if units is not None else load_manifest()
    graph = DependencyGraph(nodes=[u.name for u in units])

    for unit in units:
        for upstream in unit.depends_on:
            graph.declared_edges.add((upstream, unit.name))

    # Two units that read the same dataset are coupled: a change to that
    # dataset's contract lands on both, so they migrate together.
    readers = defaultdict(list)
    for unit in units:
        for dataset in unit.inputs.values():
            readers[dataset].append(unit.name)

    order = {u.name: i for i, u in enumerate(units)}
    for dataset, names in readers.items():
        if len(names) < 2:
            continue
        graph.shared_inputs[dataset] = sorted(names)
        ordered = sorted(names, key=lambda n: order[n])
        for downstream in ordered[1:]:
            graph.inferred_edges.add((ordered[0], downstream))

    return graph


def topological_order(graph: DependencyGraph) -> List[str]:
    """Kahn's algorithm over the union graph. Raises on a cycle."""
    incoming = {node: 0 for node in graph.nodes}
    outgoing = defaultdict(list)
    for source, target in graph.edges:
        outgoing[source].append(target)
        incoming[target] += 1

    # Manifest order breaks ties so the migration plan is reproducible.
    ready = [n for n in graph.nodes if incoming[n] == 0]
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in sorted(outgoing[node], key=graph.nodes.index):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
        ready.sort(key=graph.nodes.index)

    if len(order) != len(graph.nodes):
        stuck = sorted(set(graph.nodes) - set(order))
        raise ValueError("dependency cycle involving: %s" % ", ".join(stuck))
    return order


def migration_waves(graph: DependencyGraph) -> List[List[str]]:
    """Group units into waves that can be migrated in parallel.

    Wave N depends only on waves < N, so every unit in a wave can be
    translated, validated and promoted concurrently.
    """
    depth: Dict[str, int] = {}
    for node in topological_order(graph):
        upstreams = [s for s, t in graph.edges if t == node]
        depth[node] = 0 if not upstreams else max(depth[u] for u in upstreams) + 1

    waves: Dict[int, List[str]] = defaultdict(list)
    for node, level in depth.items():
        waves[level].append(node)
    return [sorted(waves[level], key=graph.nodes.index) for level in sorted(waves)]


def to_mermaid(graph: DependencyGraph) -> str:
    """Mermaid source for the migration-plan page of the Power BI report."""
    lines = ["graph LR"]
    for node in graph.nodes:
        lines.append('  %s["%s"]' % (node, node))
    for source, target in sorted(graph.declared_edges):
        style = "-->" if (source, target) in graph.inferred_edges else "-.->"
        lines.append("  %s %s %s" % (source, style, target))
    for source, target in sorted(graph.undocumented_edges):
        lines.append("  %s ==> %s" % (source, target))
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    graph = build()

    print("units            : %d" % len(graph.nodes))
    print("declared edges   : %d" % len(graph.declared_edges))
    print("inferred edges   : %d" % len(graph.inferred_edges))

    if graph.undocumented_edges:
        print("\nUNDOCUMENTED coupling (shared input, no manifest edge):")
        for source, target in sorted(graph.undocumented_edges):
            print("  %s -> %s" % (source, target))
    if graph.unjustified_edges:
        print("\nDeclared with no shared data (candidate for removal):")
        for source, target in sorted(graph.unjustified_edges):
            print("  %s -> %s" % (source, target))

    print("\nshared inputs:")
    for dataset, names in sorted(graph.shared_inputs.items()):
        print("  %-20s %s" % (dataset, ", ".join(names)))

    print("\nmigration waves:")
    for index, wave in enumerate(migration_waves(graph)):
        print("  wave %d: %s" % (index, ", ".join(wave)))

    print("\nmermaid:\n%s" % to_mermaid(graph))
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
