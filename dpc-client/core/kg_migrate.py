"""Move one agent's knowledge graph to the other backend, and prove it arrived.

The migration this exists for is grafeo → sqlite, one agent at a time. Three things
make it delicate, all of them measured rather than feared:

1. A third to a half of every agent's edges is `llm_relation` — 40.8% of the fleet,
   57.2% on warren — and that class has no source outside the store. Everything else
   an indexing pass or a sleep rebuilds for free.
2. The old store is not deleted by a config change, it is simply left behind. The
   May SQLite file of agent_001 still holds 1179 nodes of a graph two schema
   generations old; flip the config with that file in place and the agent opens it
   under plausible counts, addressing documents by keys nothing produces any more.
3. Counts oscillate by the whole structural class — agent_001 reads 3377 or 4754
   depending on whether a pass has finished — so any check on totals has to be taken
   at rest and had better not be a check on totals.

So the tool does not flip anything. It builds the target store from a dump, renames
the old one aside, and compares the two dumps class by class; the config change and
the restart stay with the operator, who is the only one who knows the fleet is quiet.

    kg_migrate.py prepare  <dump.jsonl> --agent <id> --to sqlite|grafeo
    kg_migrate.py verify   <before.jsonl> <after.jsonl> [--expect-dropped N]

Both dumps come from the running service — `export_knowledge_graph` — because a
second process cannot open a live `.grafeo` at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dpc_client_core.dpc_agent.knowledge_graph import KnowledgeGraph  # noqa: E402

DPC_HOME = Path.home() / ".dpc"
STORE_FILE = {"sqlite": "knowledge_graph.db", "grafeo": "knowledge_graph.grafeo"}


def _read_dump(path: Path) -> tuple[dict, set, set, Counter]:
    """Header, node set, edge set, and edges by source — everything a comparison needs."""
    header: dict = {}
    nodes: set = set()
    edges: set = set()
    by_source: Counter = Counter()
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                # A dump cut mid-record. The operator asked whether a migration was
                # sound and deserves an answer, not a traceback. (Ark.)
                raise SystemExit(
                    f"{path}:{lineno} is not a whole record — the dump is truncated "
                    f"or corrupt, and nothing after this line was read ({e})"
                ) from None
            kind = r.get("kind")
            if kind == "header":
                header = r
            elif kind == "node":
                nodes.add((r["node_id"], r["node_type"], r["label"],
                           r.get("source_layer"), bool(r.get("exempt"))))
            elif kind == "edge":
                edges.add((r["source_id"], r["target_id"], r["edge_type"],
                           r.get("justification", ""), r.get("confidence"),
                           json.dumps(r.get("properties") or {}, sort_keys=True)))
                by_source[(r.get("properties") or {}).get("source", "(unmarked)")] += 1
    return header, nodes, edges, by_source


def prepare(args: argparse.Namespace) -> int:
    agent_root = DPC_HOME / "agents" / args.agent
    if not agent_root.is_dir():
        print(f"no such agent: {agent_root}")
        return 2

    target_path = agent_root / STORE_FILE[args.to]
    if target_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = target_path.with_name(target_path.name + f".pre-migration-{stamp}")
        target_path.rename(aside)
        print(f"moved the existing {target_path.name} aside → {aside.name}")
        print("  (that is the file a config flip would otherwise have opened silently)")

    kg = KnowledgeGraph(agent_root, backend=args.to)
    result = kg.import_from(Path(args.dump))
    snap = kg.snapshot()
    kg.backend.close()

    print(f"built {target_path.name}: {result['nodes']} nodes, {result['edges']} edges, "
          f"{result['skipped']} skipped")
    print(f"  by source: {snap['edges_by_source']}")
    print()
    print("Not done by this tool, on purpose — do these when the fleet is quiet:")
    print(f'  1. add  "kg_backend": "{args.to}"  to {agent_root / "config.json"}')
    print("  2. restart the service")
    print("  3. export the agent again through the service, then:")
    print(f"     kg_migrate.py verify {args.dump} <the-new-dump.jsonl>")
    print("     — the import above reported skipped=0; if a later run does not, the")
    print("       dump held records this build does not understand and the gate's")
    print("       counts will not tell you which")
    print("  4. once verify is green, rename the store you migrated *from* — this tool")
    print("     only moved the target aside, and the source is still sitting there.")
    print("     A later flip back to the old backend would open it in silence, and by")
    print("     then it is a graph several sleeps out of date. (Ark's point.)")
    return 0


def verify(args: argparse.Namespace) -> int:
    """Compare two dumps class by class. What this proves, and what it does not.

    It compares a **projection**, not the whole record: a node by
    (id, type, label, layer, exempt) and an edge by (ends, type, justification,
    confidence, properties). Node `properties` are not compared at all, and neither
    are `t_created`, `t_invalidated` or `edge_weight` on edges. A transform that
    silently rewrote a `file_mtime`, resurrected an invalidated edge or dropped an
    edge weight would pass this gate green. That is deliberate for the migration this
    was built for — `import_from` carries every field verbatim and the round-trip test
    checks all of them, so the carrier is proven elsewhere and the gate is here to
    guard identity and the fate of the irreplaceable class. It stops being adequate
    the moment someone points it at a second transform, and the L5/L6 relabelling is
    the first candidate. (Boundary named by Ark, Fable 5 and GLM 5.2 independently.)
    """
    before_header, before_nodes, before_edges, before_src = _read_dump(Path(args.before))
    after_header, after_nodes, after_edges, after_src = _read_dump(Path(args.after))

    # The counts below are set sizes, and a graph may hold two edges that agree on
    # every field this comparison reads — warren carries seven such pairs. They
    # collapse here, which is why these numbers can sit just under the header's, and
    # why the gate cannot see a duplicate being dropped. That is a deliberate
    # boundary: two indistinguishable edges are the same fact twice.
    for label, header, nodes, edges in (
        ("before", before_header, before_nodes, before_edges),
        ("after ", after_header, after_nodes, after_edges),
    ):
        snap = header.get("snapshot") or {}
        note = ""
        if snap.get("edges_total") not in (None, len(edges)):
            note = f"  (dump says {snap['edges_total']}; {snap['edges_total'] - len(edges)} are duplicates)"
        print(f"{label}: {len(nodes)} nodes, {len(edges)} distinct edges  "
              f"({header.get('exported_at', '?')}){note}")
    print()

    failures = []

    # Nodes first, because what happens to them decides which edge losses are
    # explained. The transform this was written for drops knowledge-file nodes, and a
    # dropped node takes its edges with it — an edge whose endpoint was declared gone
    # is collateral, not loss.
    # Gone means the id is absent afterwards, not that some field of it changed.
    # Reading this off the tuple difference let a node that survived with a new label
    # count as dropped, and every genuinely lost edge touching it was then filed as
    # collateral — a lost llm_relation edge passed the gate green. Found by Fable 5 in
    # review, with a reproduction; the transform this gate exists for is a declared
    # node drop, which is exactly the moment node tuples change.
    before_ids = {n[0] for n in before_nodes}
    after_ids = {n[0] for n in after_nodes}
    dropped_ids = before_ids - after_ids
    changed = {n[0] for n in before_nodes - after_nodes} - dropped_ids

    declared = args.expect_dropped or 0
    print(f"nodes       : {len(dropped_ids)} gone, {declared} declared"
          + (f", {len(changed)} still present but altered" if changed else ""))
    if len(dropped_ids) != declared:
        failures.append(
            f"{len(dropped_ids)} nodes gone, {declared} declared — "
            f"a delta has to be written down before the run, not explained after")
    for node_id in sorted(dropped_ids)[:5]:
        print(f"    dropped: {node_id}")
    for node_id in sorted(changed)[:5]:
        print(f"    altered: {node_id}")
    lost_edges = before_edges - after_edges
    collateral = {e for e in lost_edges if e[0] in dropped_ids or e[1] in dropped_ids}
    unexplained = lost_edges - collateral

    def _source(edge) -> str:
        return json.loads(edge[5]).get("source", "(unmarked)")

    unexplained_by_source = Counter(_source(e) for e in unexplained)

    # The class that cannot be rebuilt has to arrive whole. Nothing else here matters
    # as much: a missing structural edge is back after one pass, a missing
    # llm_relation edge is gone for good.
    print(f"llm_relation: {before_src.get('llm_relation', 0)} before, "
          f"{after_src.get('llm_relation', 0)} after, "
          f"{unexplained_by_source.get('llm_relation', 0)} lost with no dropped endpoint")
    if unexplained_by_source.get("llm_relation"):
        failures.append(
            f"{unexplained_by_source['llm_relation']} llm_relation edges vanished and "
            f"nothing declared explains them")

    print(f"gliner_ner  : {before_src.get('gliner_ner', 0)} before, "
          f"{after_src.get('gliner_ner', 0)} after, "
          f"{unexplained_by_source.get('gliner_ner', 0)} lost with no dropped endpoint "
          f"(a sleep rebuilds these from cache, but not before someone asks why)")
    if unexplained_by_source.get("gliner_ner"):
        failures.append(
            f"{unexplained_by_source['gliner_ner']} gliner_ner edges vanished undeclared")

    print(f"structural  : {before_src.get('structural', 0)} before, "
          f"{after_src.get('structural', 0)} after "
          f"(cleared and rebuilt by every pass — differences here are the normal state)")

    if collateral:
        print(f"collateral  : {len(collateral)} edges went with the declared nodes")
    for edge in sorted(unexplained)[:5]:
        print(f"    unexplained: {edge[0]} -[{edge[2]}]-> {edge[1]} ({_source(edge)})")

    print()
    if failures:
        print("GATE FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE PASSED — every irreplaceable edge arrived, and nothing vanished undeclared")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="build the target store from a dump")
    p.add_argument("dump")
    p.add_argument("--agent", required=True)
    p.add_argument("--to", required=True, choices=sorted(STORE_FILE))
    p.set_defaults(func=prepare)

    v = sub.add_parser("verify", help="compare two dumps class by class")
    v.add_argument("before")
    v.add_argument("after")
    v.add_argument("--expect-dropped", type=int, default=0,
                   help="how many nodes the transform was declared to drop")
    v.set_defaults(func=verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
