"""Task 002 prep: clean + normalize + dedup + holdout BEFORE paraphrase.

Pipeline (S130 Mike + Ark + CC consensus, msgs 45-49):

  Step 1. Load triples_kg.jsonl + triples_md.jsonl
  Step 2. Drop GLiNER MENTIONS  (filter on source_subtype == "gliner_ner")
  Step 3. Length filter         (drop subj/obj <2 or >100 chars)
  Step 4. Normalize KG predicates → verb phrases  (7 active rules)
  Step 5. Deduplicate by hash(lower(subj), lower(pred), lower(obj))
  Step 6. Reserve predicate-level holdout (max(2, ceil(0.10 * N_predicates)))

Outputs:
  ~/.dpc/agents/<agent_id>/training_data/corpus_input.jsonl   (post-holdout, paraphrase input)
  ~/.dpc/agents/<agent_id>/training_data/holdout_predicates.json
  ~/.dpc/agents/<agent_id>/training_data/prep_stats.json

Usage:
    uv run python -m dpc_client_core.dpc_agent.training.prepare_corpus \\
        --agent-id agent_001 --seed 42
"""

import argparse
import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))

# KG EdgeType enum → verb phrase (S130 consensus, Ark note: skip
# TEMPORAL_NEXT and SHARED_WITH because corpus has 0 edges of each).
KG_PREDICATE_NORMALIZE = {
    "MENTIONS": "mentions",
    "DERIVED_FROM": "is derived from",
    "DEPENDS_ON": "depends on",
    "RESPONDS_TO": "responds to",
    "CONTRADICTS": "contradicts",
    "SUPPORTS": "supports",
    "DECIDED_BY": "is decided by",
    # SHARED_WITH, TEMPORAL_NEXT — skipped, 0 edges in agent_001 corpus
}

MIN_LEN = 2
MAX_LEN = 100


def _load_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _triple_hash(subj: str, pred: str, obj: str) -> str:
    s = f"{subj.lower().strip()}{pred.lower().strip()}{obj.lower().strip()}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def prepare(agent_id: str, seed: int) -> dict:
    agent_root = DPC_HOME / "agents" / agent_id
    data_dir = agent_root / "training_data"
    kg_path = data_dir / "triples_kg.jsonl"
    md_path = data_dir / "triples_md.jsonl"

    stats = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "seed": seed,
        "stages": {},
    }

    # Step 1: Load
    raw = list(_load_jsonl(kg_path)) + list(_load_jsonl(md_path))
    stats["stages"]["1_loaded"] = {
        "total": len(raw),
        "from_kg": sum(1 for t in raw if t.get("source") == "kg"),
        "from_markdown": sum(1 for t in raw if t.get("source") == "markdown"),
    }

    # Step 2: Drop GLiNER MENTIONS (timestamp subjects)
    after_step2 = [t for t in raw if t.get("source_subtype") != "gliner_ner"]
    stats["stages"]["2_drop_gliner"] = {
        "kept": len(after_step2),
        "dropped": len(raw) - len(after_step2),
    }

    # Step 3: Length filter
    def _ok_len(t):
        for field in ("subject", "object"):
            v = (t.get(field) or "").strip()
            if len(v) < MIN_LEN or len(v) > MAX_LEN:
                return False
        return True

    after_step3 = [t for t in after_step2 if _ok_len(t)]
    stats["stages"]["3_length_filter"] = {
        "kept": len(after_step3),
        "dropped": len(after_step2) - len(after_step3),
        "min_len": MIN_LEN,
        "max_len": MAX_LEN,
    }

    # Step 4: Normalize KG predicates (verb phrases)
    normalized_count = 0
    after_step4 = []
    for t in after_step3:
        pred = t.get("predicate") or ""
        if pred in KG_PREDICATE_NORMALIZE:
            t = {**t, "predicate": KG_PREDICATE_NORMALIZE[pred], "predicate_original": pred}
            normalized_count += 1
        after_step4.append(t)
    stats["stages"]["4_normalize_kg"] = {
        "kept": len(after_step4),
        "normalized": normalized_count,
        "rules_applied": list(KG_PREDICATE_NORMALIZE.keys()),
    }

    # Step 5: Deduplicate
    seen = set()
    after_step5 = []
    for t in after_step4:
        h = _triple_hash(t["subject"], t["predicate"], t["object"])
        if h in seen:
            continue
        seen.add(h)
        t["triple_hash"] = h
        after_step5.append(t)
    stats["stages"]["5_deduplicate"] = {
        "kept": len(after_step5),
        "dropped": len(after_step4) - len(after_step5),
    }

    # Step 6: Predicate-level holdout
    # Formula: `min(30, max(2, ceil(N * 0.10)))` per Mike S130 [56].
    # The 10% rule was designed for small predicate spaces (~9 KG enum types);
    # LLM extraction inflated N to ~4.5K, making 10% (446) too stringent for a
    # viability test. Cap at 30 keeps the test informative without testing
    # language generalization on rare verbs.
    predicates = sorted({t["predicate"] for t in after_step5})
    holdout_n = min(30, max(2, math.ceil(len(predicates) * 0.10)))
    rng = random.Random(seed)
    holdout_predicates = set(rng.sample(predicates, k=min(holdout_n, len(predicates))))

    corpus_input = [t for t in after_step5 if t["predicate"] not in holdout_predicates]
    holdout_triples = [t for t in after_step5 if t["predicate"] in holdout_predicates]

    stats["stages"]["6_holdout"] = {
        "total_predicates": len(predicates),
        "holdout_n": holdout_n,
        "holdout_formula": "min(30, max(2, ceil(N * 0.10)))",
        "holdout_predicates": sorted(holdout_predicates),
        "corpus_input_size": len(corpus_input),
        "holdout_triples_size": len(holdout_triples),
        "seed": seed,
    }

    # Outputs
    corpus_path = data_dir / "corpus_input.jsonl"
    holdout_path = data_dir / "holdout_predicates.json"
    stats_path = data_dir / "prep_stats.json"

    with corpus_path.open("w", encoding="utf-8") as f:
        for t in corpus_input:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    holdout_doc = {
        "schema_version": "1.0",
        "generated_at": stats["generated_at"],
        "seed": seed,
        "total_predicates": len(predicates),
        "holdout_n": holdout_n,
        "holdout_formula": "min(30, max(2, ceil(N * 0.10)))",
        "holdout_predicates": sorted(holdout_predicates),
        "holdout_triple_count": len(holdout_triples),
    }
    holdout_path.write_text(
        json.dumps(holdout_doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    stats["outputs"] = {
        "corpus_input_jsonl": str(corpus_path),
        "holdout_predicates_json": str(holdout_path),
        "prep_stats_json": str(stats_path),
    }
    stats_path.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Task 002 prep: clean corpus before paraphrase")
    parser.add_argument("--agent-id", default="agent_001")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for predicate holdout selection")
    args = parser.parse_args()

    stats = prepare(args.agent_id, args.seed)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
