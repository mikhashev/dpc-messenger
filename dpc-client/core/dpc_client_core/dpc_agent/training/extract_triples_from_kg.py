"""Task 001 extractor 1: KG edges → triples.jsonl.

Exports an agent's knowledge graph as a JSONL stream of (subject, predicate,
object) triples for Knowledge-as-Weights Phase 1 training corpus.

Strategy (Task 000 finding):
- Sleep pipeline already populates GLiNER MENTIONS edges + LLM-extracted
  typed edges into the KG. We don't need to re-run extraction — just export
  what's there.
- Read SQLite KG file directly (stdlib, read-only) to avoid lock conflicts
  with running DPC backend. The snapshot may be hours stale; Task 001
  acceptance criterion tolerates that — backend can be stopped + this
  re-run for the canonical export before training (Task 003).

Output schema (one JSON object per line):
    {
      "subject": "Mike",
      "predicate": "MENTIONS",
      "object": "p2p",
      "source": "kg",
      "source_subtype": "gliner_ner",
      "node_ids": ["sa:2026-05-01T23-08-44", "e:p2p"],
      "edge_type": "MENTIONS",
      "t_created": "2026-05-12T19:50:31.606881+00:00",
      "confidence": 1.0,
      "justification": "GLiNER extracted (technology, score=0.68)"
    }

Usage:
    uv run python -m dpc_client_core.dpc_agent.training.extract_triples_from_kg \\
        --agent-id agent_001
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))


def _parse_props(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def extract(agent_id: str, include_invalidated: bool = False) -> dict:
    """Export KG edges from agent's SQLite snapshot to triples.jsonl.

    Returns counters dict for caller reporting.
    """
    agent_root = DPC_HOME / "agents" / agent_id
    db_path = agent_root / "knowledge_graph.db"
    if not db_path.exists():
        raise FileNotFoundError(f"No SQLite KG at {db_path}")

    out_dir = agent_root / "training_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "triples_kg.jsonl"

    counters = {
        "total_edges": 0,
        "exported": 0,
        "skipped_invalidated": 0,
        "skipped_missing_node": 0,
        "by_edge_type": {},
        "by_source_subtype": {},
        "snapshot_mtime": datetime.fromtimestamp(
            db_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    }

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        nodes = {}
        for row in cur.execute("SELECT node_id, label, node_type FROM nodes"):
            nodes[row["node_id"]] = {"label": row["label"], "node_type": row["node_type"]}

        query = """
            SELECT source_id, target_id, edge_type, t_created, t_invalidated,
                   confidence, justification, properties
            FROM edges
        """
        if not include_invalidated:
            query += " WHERE t_invalidated IS NULL"

        with out_path.open("w", encoding="utf-8") as f:
            for row in cur.execute(query):
                counters["total_edges"] += 1

                if row["t_invalidated"] is not None and not include_invalidated:
                    counters["skipped_invalidated"] += 1
                    continue

                src = nodes.get(row["source_id"])
                tgt = nodes.get(row["target_id"])
                if not src or not tgt:
                    counters["skipped_missing_node"] += 1
                    continue

                props = _parse_props(row["properties"])
                source_subtype = props.get("source", "legacy")

                triple = {
                    "subject": src["label"],
                    "predicate": row["edge_type"],
                    "object": tgt["label"],
                    "source": "kg",
                    "source_subtype": source_subtype,
                    "node_ids": [row["source_id"], row["target_id"]],
                    "edge_type": row["edge_type"],
                    "t_created": row["t_created"],
                    "confidence": row["confidence"],
                    "justification": row["justification"] or "",
                }
                f.write(json.dumps(triple, ensure_ascii=False) + "\n")
                counters["exported"] += 1
                counters["by_edge_type"][row["edge_type"]] = (
                    counters["by_edge_type"].get(row["edge_type"], 0) + 1
                )
                counters["by_source_subtype"][source_subtype] = (
                    counters["by_source_subtype"].get(source_subtype, 0) + 1
                )
    finally:
        conn.close()

    counters["output_path"] = str(out_path)
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Task 001 — KG → triples extractor")
    parser.add_argument("--agent-id", default="agent_001", help="Agent folder id")
    parser.add_argument(
        "--include-invalidated",
        action="store_true",
        help="Include bi-temporally invalidated edges (default: skip)",
    )
    args = parser.parse_args()

    counters = extract(args.agent_id, include_invalidated=args.include_invalidated)
    print(json.dumps(counters, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
