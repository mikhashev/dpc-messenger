"""Does the index find a card, and does fusing the two channels help?

The first instrument in the set: no model to serve, no network, no money —
only the embedding encoder that production already loads. It answers three
questions the codebase has never asked:

1. Given a query that names a card, does the card come back at all?
2. Which channel finds it — BM25, vectors, or only the two fused?
3. Do the two retrieval backends behind `retrieval_vector` / `retrieval_text`
   differ? That flag has shipped since ADR-024 §1.6 and nothing has ever
   compared what it switches between.

Bootstrap query set, and its weakness is stated rather than hidden: a card's
own title is the query and the card is the gold answer. That measures «can the
index find a card by its own name» and nothing more — it is a floor, not a
grade. A query set drawn from real questions needs labels, and labels are the
cost this deliberately avoids for the first run.

Run from `dpc-client/core`:

    uv run python ../../eval/retrieval/run_retrieval_eval.py --agent agent_001

Add `--json out.json` to keep the run. `--limit N` for a smoke pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ranks beyond this are "not found"; recall@k is reported for these cutoffs.
TOP_K = 20
CUTOFFS = (1, 5, 10)

TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def card_title(path: Path) -> str:
    """The query for a card: its first heading, else its filename."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return path.stem.replace("-", " ").replace("_", " ")
    m = TITLE_RE.search(head)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ")


def build_query_set(agent_root: Path, limit: Optional[int] = None) -> List[Tuple[str, str]]:
    """(query, gold source_file) for every card in the agent's knowledge dir."""
    knowledge = agent_root / "knowledge"
    if not knowledge.exists():
        return []
    cards = sorted(
        f for f in knowledge.rglob("*.md")
        if not f.name.startswith("_")
    )
    if limit:
        cards = cards[:limit]
    return [(card_title(c), str(c)) for c in cards]


def _source_of(meta: dict) -> str:
    return str(meta.get("source_file") or meta.get("source") or "")


def _same_card(candidate: str, gold: str) -> bool:
    """Index rows carry a path; compare on the resolved path, then the name.

    A card is chunked, so several rows share one `source_file`; the first row
    that names the gold card is the hit.
    """
    if not candidate:
        return False
    if candidate == gold:
        return True
    try:
        if Path(candidate).resolve() == Path(gold).resolve():
            return True
    except OSError:
        pass
    return Path(candidate).name == Path(gold).name


def rank_of(results: List[Tuple[dict, float]], gold: str) -> Optional[int]:
    """1-based rank of the first row naming the gold card, deduplicated by card."""
    seen: List[str] = []
    for meta, _score in results:
        src = _source_of(meta)
        if not src or src in seen:
            continue
        seen.append(src)
        if _same_card(src, gold):
            return len(seen)
    return None


def score(ranks: List[Optional[int]]) -> Dict[str, float]:
    n = len(ranks) or 1
    out = {f"recall@{k}": sum(1 for r in ranks if r is not None and r <= k) / n for k in CUTOFFS}
    out["mrr"] = sum(1.0 / r for r in ranks if r is not None) / n
    out["found"] = sum(1 for r in ranks if r is not None)
    out["queries"] = len(ranks)
    return out


def run(agent_root: Path, limit: Optional[int]) -> dict:
    from dpc_client_core.dpc_agent.memory import get_embedding_provider
    from dpc_client_core.dpc_agent.retrieval.factory import make_backend_for_agent

    queries = build_query_set(agent_root, limit)
    if not queries:
        raise SystemExit(f"no cards under {agent_root / 'knowledge'}")

    provider = get_embedding_provider()
    backend = make_backend_for_agent(agent_root, dimensions=provider.dimensions)
    loaded = backend.load()

    per_channel: Dict[str, List[Optional[int]]] = {"text": [], "vector": [], "fused": []}
    started = time.time()

    for query, gold in queries:
        text_hits = backend.text.search(query, TOP_K)
        vector_hits = backend.vector.search(np.asarray(provider.embed(query), dtype="float32"), TOP_K)
        fused = backend.fuser.fuse(vector_results=vector_hits, text_results=text_hits)
        fused_as_pairs = [(f.chunk_meta, f.score) for f in fused]

        per_channel["text"].append(rank_of(text_hits, gold))
        per_channel["vector"].append(rank_of(vector_hits, gold))
        per_channel["fused"].append(rank_of(fused_as_pairs, gold))

    elapsed = time.time() - started
    return {
        "agent": agent_root.name,
        "backend_id": backend.backend_id,
        "both_indexes_loaded": loaded,
        "queries": len(queries),
        "seconds": round(elapsed, 1),
        "top_k": TOP_K,
        "channels": {name: score(ranks) for name, ranks in per_channel.items()},
        # Kept so a later run can say which cards the index cannot find at all,
        # rather than only how many.
        "misses_fused": [
            gold for (_, gold), r in zip(queries, per_channel["fused"]) if r is None
        ],
    }


def render(report: dict) -> str:
    lines = [
        f"agent {report['agent']} · backend {report['backend_id']!r} · "
        f"{report['queries']} queries · {report['seconds']}s · top_k={report['top_k']}",
        "",
        f"{'channel':8} {'recall@1':>9} {'recall@5':>9} {'recall@10':>10} {'MRR':>7} {'found':>7}",
    ]
    for name in ("text", "vector", "fused"):
        s = report["channels"][name]
        lines.append(
            f"{name:8} {s['recall@1']:9.3f} {s['recall@5']:9.3f} "
            f"{s['recall@10']:10.3f} {s['mrr']:7.3f} {s['found']:4}/{s['queries']}"
        )
    if not report["both_indexes_loaded"]:
        lines.append("")
        lines.append("NOTE: at least one index did not load — numbers below are of a partial index.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", default="agent_001", help="agent id under ~/.dpc/agents")
    ap.add_argument("--agents-root", default=str(Path.home() / ".dpc" / "agents"))
    ap.add_argument("--limit", type=int, default=None, help="use only the first N cards")
    ap.add_argument("--json", default=None, help="write the full report here")
    args = ap.parse_args()

    agent_root = Path(args.agents_root) / args.agent
    if not agent_root.exists():
        raise SystemExit(f"no such agent: {agent_root}")

    report = run(agent_root, args.limit)
    print(render(report))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nfull report → {args.json}")


if __name__ == "__main__":
    sys.exit(main())
