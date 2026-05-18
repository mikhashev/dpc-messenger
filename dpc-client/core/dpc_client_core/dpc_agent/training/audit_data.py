"""Task 000: Data audit — corpus inventory + existing extraction surface.

One-shot pre-flight script for Knowledge-as-Weights Phase 1. Answers two
questions before any extraction code is written (Task 001):

1. How much data do we actually have? File counts + sizes + token estimates
   for knowledge files, ADRs, session archives; KG node/edge counts +
   edge-type breakdown.
2. What extraction already exists? Static inspection of relevant modules
   (knowledge_graph, sleep_pipeline) to flag re-usable code surfaces.

Output: ~/.dpc/agents/<agent_id>/training_data/audit_report.json

KG query strategy: read from the agent's SQLite KG file directly via
stdlib `sqlite3` (read-only). Reason: Grafeo backend may be exclusively
locked by a running DPC service. SQLite gives a known snapshot (may be
slightly stale post-Grafeo-migration); staleness is recorded in the
report so callers can re-run after backend shutdown if exact live numbers
matter.

Usage:
    uv run python -m dpc_client_core.dpc_agent.training.audit_data \\
        --agent-id agent_001 \\
        [--repo-root c:/Users/mike/Documents/dpc-messenger]
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))


def _estimate_tokens(byte_count: int) -> int:
    return byte_count // 4


def _inventory(path: Path, pattern: str, recursive: bool = False) -> Dict[str, int]:
    if not path.exists():
        return {"count": 0, "total_bytes": 0, "estimated_tokens": 0, "path": str(path), "present": False}
    files = list(path.rglob(pattern) if recursive else path.glob(pattern))
    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    return {
        "count": sum(1 for f in files if f.is_file()),
        "total_bytes": total_bytes,
        "estimated_tokens": _estimate_tokens(total_bytes),
        "path": str(path),
        "present": True,
    }


def _kg_stats(agent_root: Path) -> Dict:
    db_path = agent_root / "knowledge_graph.db"
    grafeo_path = agent_root / "knowledge_graph.grafeo"
    grafeo_wal = agent_root / "knowledge_graph.grafeo.wal"

    out = {
        "sqlite_present": db_path.exists(),
        "grafeo_present": grafeo_path.exists(),
        "node_count": None,
        "edge_count": None,
        "edge_types_breakdown": {},
        "source_used": None,
        "as_of_mtime": None,
        "staleness_warning": None,
    }

    if not db_path.exists():
        out["staleness_warning"] = "No SQLite KG file available; can't audit KG via sqlite stdlib."
        return out

    out["as_of_mtime"] = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc).isoformat()
    out["source_used"] = "sqlite (stdlib, read-only)"

    if grafeo_wal.exists():
        wal_log = grafeo_wal / "wal_00000000.log"
        if wal_log.exists():
            sqlite_age = time.time() - db_path.stat().st_mtime
            grafeo_age = time.time() - wal_log.stat().st_mtime
            if grafeo_age < sqlite_age:
                hours_stale = (sqlite_age - grafeo_age) / 3600
                out["staleness_warning"] = (
                    f"SQLite KG is older than Grafeo WAL by ~{hours_stale:.1f}h. "
                    f"For exact live numbers, stop backend and re-query Grafeo directly."
                )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        out["node_count"] = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        out["edge_count"] = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        for row in cur.execute(
            "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type ORDER BY 2 DESC"
        ):
            out["edge_types_breakdown"][row[0]] = row[1]
    finally:
        conn.close()

    return out


def _extraction_surface(repo_root: Path) -> Dict:
    """Static inspection: report which extraction surfaces exist as code.

    We don't import the modules (avoids torch/grafeo load); we just check
    for symbol presence via grep-style text matching.
    """
    core_pkg = repo_root / "dpc-client" / "core" / "dpc_client_core"
    surfaces = {}

    kg_file = core_pkg / "dpc_agent" / "knowledge_graph.py"
    if kg_file.exists():
        text = kg_file.read_text(encoding="utf-8")
        surfaces["gliner_extract_entities"] = {
            "module": "dpc_agent/knowledge_graph.py",
            "function_present": "extract_entities_gliner" in text or "_extract_entities" in text,
            "_get_gliner_model_present": "_get_gliner_model" in text,
            "note": "GLiNER NER for entity extraction. If present, reusable for Task 001 entity step.",
        }
        surfaces["kg_structural_edges"] = {
            "module": "dpc_agent/knowledge_graph.py",
            "function_present": "clear_structural_edges" in text,
            "note": "Structural edges (DERIVED_FROM, DEPENDS_ON, etc.) populated by `populate_structural_edges`. Reusable as triples via KG export in Task 001.",
        }

    sleep_file = core_pkg / "dpc_agent" / "sleep_pipeline.py"
    if sleep_file.exists():
        text = sleep_file.read_text(encoding="utf-8")
        surfaces["sleep_llm_relations"] = {
            "module": "dpc_agent/sleep_pipeline.py",
            "function_present": (
                "extracted_relations" in text
                or "llm_relation" in text
                or "extract_relations" in text
                or "llm_relations" in text
                or "extract_edges" in text
            ),
            "note": "LLM-based relation extraction during sleep consolidation. Output already lands in KG as LLM-marked edges (props.source='llm_relation'). Reusable via KG export.",
        }

    return surfaces


def _delta(actual: int, expected: int) -> str:
    if expected == 0:
        return "n/a"
    pct = (actual - expected) / expected * 100
    return f"{actual} vs ~{expected} ({pct:+.0f}%)"


def _drift_check(report: Dict) -> Tuple[bool, list]:
    """Compare against 003 §3 estimates. Return (any_drift_over_20pct, notes)."""
    estimates = {
        "knowledge_files": 164,
        "adrs": 28,
        "session_archives": 133,
        "kg_nodes": 826,
        "kg_edges": 1201,
    }
    actuals = {
        "knowledge_files": report["corpus_inventory"]["knowledge_files"]["count"],
        "adrs": report["corpus_inventory"]["adrs"]["count"],
        "session_archives": report["corpus_inventory"]["session_archives"]["count"],
        "kg_nodes": report["corpus_inventory"]["kg_nodes"] or 0,
        "kg_edges": report["corpus_inventory"]["kg_edges"] or 0,
    }
    notes = []
    any_drift = False
    for key, expected in estimates.items():
        actual = actuals[key]
        if expected > 0:
            pct = abs(actual - expected) / expected
            tag = "DRIFT>20%" if pct > 0.20 else "ok"
            if pct > 0.20:
                any_drift = True
            notes.append(f"{key}: {_delta(actual, expected)} [{tag}]")
    return any_drift, notes


def run_audit(agent_id: str, repo_root: Path) -> Dict:
    agent_root = DPC_HOME / "agents" / agent_id
    if not agent_root.exists():
        raise FileNotFoundError(f"Agent root not found: {agent_root}")

    # Repo-relative paths (ADRs live in the codebase, not in ~/.dpc)
    repo_adr_dir = repo_root / "docs" / "decisions"

    # Per-agent + per-session inventory
    knowledge_inv = _inventory(DPC_HOME / "knowledge", "*.md")
    adr_inv = _inventory(repo_adr_dir, "*.md")
    sessions_root = DPC_HOME / "conversations" / agent_id / "archive"
    session_inv = _inventory(sessions_root, "*.json", recursive=True)

    kg = _kg_stats(agent_root)

    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "agent_root": str(agent_root),
        "repo_root": str(repo_root),
        "corpus_inventory": {
            "knowledge_files": knowledge_inv,
            "adrs": adr_inv,
            "session_archives": session_inv,
            "kg_nodes": kg["node_count"],
            "kg_edges": kg["edge_count"],
            "kg_edge_types_breakdown": kg["edge_types_breakdown"],
            "kg_source": kg["source_used"],
            "kg_as_of": kg["as_of_mtime"],
            "kg_staleness_warning": kg["staleness_warning"],
        },
        "existing_extraction_surface": _extraction_surface(repo_root),
    }

    triples_estimate = (
        (report["corpus_inventory"]["kg_edges"] or 0)
        + report["corpus_inventory"]["knowledge_files"]["count"] * 20
        + report["corpus_inventory"]["adrs"]["count"] * 30
    )
    report["estimated_triple_yield"] = {
        "lower_bound": (report["corpus_inventory"]["kg_edges"] or 0),
        "rough_total_estimate": triples_estimate,
        "note": "Lower bound = KG edges (already triples). Rough total adds ~20 triples per knowledge file and ~30 per ADR — to be replaced by Task 001 actual extraction.",
    }

    drift_detected, drift_notes = _drift_check(report)
    report["drift_check"] = {
        "expected_from_003_§3": {
            "knowledge_files": 164,
            "adrs": 28,
            "session_archives": 133,
            "kg_nodes": 826,
            "kg_edges": 1201,
        },
        "deltas": drift_notes,
        "any_over_20pct": drift_detected,
        "action": "ESCALATE to Mike before Task 001" if drift_detected else "OK to proceed",
    }

    recommendations = []
    if kg["edge_count"] and kg["edge_count"] >= 1000:
        recommendations.append(
            "Task 001 can export KG edges directly as triples (no GLiNER re-extraction needed for entity-mention edges)."
        )
    if report["existing_extraction_surface"].get("kg_structural_edges", {}).get("function_present"):
        recommendations.append(
            "Reuse `populate_structural_edges` output via KG-export; do not re-derive structural relations in Task 001."
        )
    if report["existing_extraction_surface"].get("sleep_llm_relations", {}).get("function_present"):
        recommendations.append(
            "Sleep pipeline already extracts LLM relations into KG. Task 001 markdown extractor can be limited to fresh files not yet processed by sleep."
        )
    if not recommendations:
        recommendations.append(
            "No clear reuse path detected — Task 001 will need full extraction pass."
        )
    report["recommendations"] = recommendations

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Knowledge-as-Weights Phase 1 — Task 000 Data Audit")
    parser.add_argument("--agent-id", default="agent_001", help="Agent folder id (default: agent_001)")
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[5]),
        help="Repository root (auto-detected by default)",
    )
    parser.add_argument("--print-only", action="store_true", help="Print to stdout, skip writing report")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    report = run_audit(args.agent_id, repo_root)

    if not args.print_only:
        out_dir = DPC_HOME / "agents" / args.agent_id / "training_data"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "audit_report.json"
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Task 000] Wrote {out_path}")

    print(json.dumps(report, indent=2, ensure_ascii=False))

    return 1 if report["drift_check"]["any_over_20pct"] else 0


if __name__ == "__main__":
    sys.exit(main())
