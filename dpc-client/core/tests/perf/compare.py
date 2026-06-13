"""Phase A benchmark — compare baseline FAISS+BM25+RRF vs Grafeo native.

Joins per-query results from run_baseline.py and run_grafeo_native.py.
Computes:
  - top-K Jaccard overlap on the fused result set
  - top-1 agreement (boolean: same best result?)
  - per-channel agreement (FAISS vs Grafeo vector; BM25 vs Grafeo text)
  - latency comparison (mean + p50 + p95)
  - divergence cases (queries with low overlap → flagged for human review)

Output:
  - report.json: machine-readable per-query + summary
  - report.md: human-readable summary with divergence call-outs

Run:
    python -m tests.perf.compare \\
        --baseline baseline_results.json \\
        --grafeo grafeo_results.json \\
        --out-json report.json --out-md report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


DIVERGENCE_OVERLAP_THRESHOLD = 0.6  # queries below this get a markdown call-out


def _ids(rows: list[dict]) -> list[str]:
    return [r.get("id") or r.get("source_file", "?") for r in rows if r is not None]


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def per_query_compare(baseline_q: dict, grafeo_q: dict) -> dict:
    base_rrf = _ids(baseline_q["rrf_top"])
    graf_rrf = _ids(grafeo_q["hybrid_top"])
    base_faiss = _ids(baseline_q["faiss_top"])
    graf_vec = _ids(grafeo_q["vector_top"])
    base_bm25 = _ids(baseline_q["bm25_top"])
    graf_text = _ids(grafeo_q["text_top"])

    top1_same = bool(base_rrf and graf_rrf and base_rrf[0] == graf_rrf[0])

    return {
        "query_text": baseline_q["query_text"],
        "fused_jaccard": round(jaccard(base_rrf, graf_rrf), 3),
        "top1_agreement": top1_same,
        "vector_channel_jaccard": round(jaccard(base_faiss, graf_vec), 3),
        "text_channel_jaccard": round(jaccard(base_bm25, graf_text), 3),
        "baseline_top1": base_rrf[0] if base_rrf else None,
        "grafeo_top1": graf_rrf[0] if graf_rrf else None,
        "latency_ms": {
            "baseline_total": baseline_q["latency_ms"]["total"],
            "grafeo_total": grafeo_q["latency_ms"]["total"],
        },
    }


def write_markdown(report: dict, out: Path) -> None:
    s = report["summary"]
    lines = [
        "# Phase A Benchmark Report — Grafeo Native vs FAISS+BM25+RRF",
        "",
        f"- Queries: **{s['queries_count']}**",
        f"- Fused Jaccard (mean): **{s['fused_jaccard_mean']}** (median {s['fused_jaccard_median']})",
        f"- Top-1 agreement rate: **{s['top1_agreement_rate']:.1%}**",
        f"- Vector channel Jaccard (mean): **{s['vector_channel_jaccard_mean']}**",
        f"- Text channel Jaccard (mean): **{s['text_channel_jaccard_mean']}**",
        "",
        "## Latency",
        "",
        "| Stack | mean | p50 | p95 | max |",
        "|---|---|---|---|---|",
        f"| Baseline (FAISS+BM25+custom RRF) | {s['latency']['baseline']['mean']} ms | {s['latency']['baseline']['p50']} ms | {s['latency']['baseline']['p95']} ms | {s['latency']['baseline']['max']} ms |",
        f"| Grafeo native (HNSW+BM25+hybrid_search) | {s['latency']['grafeo']['mean']} ms | {s['latency']['grafeo']['p50']} ms | {s['latency']['grafeo']['p95']} ms | {s['latency']['grafeo']['max']} ms |",
        "",
        f"## Divergence cases (fused Jaccard < {DIVERGENCE_OVERLAP_THRESHOLD})",
        "",
    ]
    divergent = [pq for pq in report["per_query"] if pq["fused_jaccard"] < DIVERGENCE_OVERLAP_THRESHOLD]
    if not divergent:
        lines.append("_None — all queries agree above threshold._")
    else:
        lines.append(f"_{len(divergent)} of {s['queries_count']} queries flagged._")
        lines.append("")
        for pq in divergent:
            preview = pq["query_text"][:120].replace("\n", " ")
            lines.append(f"### `{preview}{'...' if len(pq['query_text']) > 120 else ''}`")
            lines.append("")
            lines.append(f"- Fused Jaccard: **{pq['fused_jaccard']}**, Top-1 agreement: **{pq['top1_agreement']}**")
            lines.append(f"- Baseline top-1: `{pq['baseline_top1']}`")
            lines.append(f"- Grafeo top-1:   `{pq['grafeo_top1']}`")
            lines.append(f"- Vector ch. Jaccard: {pq['vector_channel_jaccard']}; Text ch. Jaccard: {pq['text_channel_jaccard']}")
            lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--grafeo", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()

    base = json.loads(args.baseline.read_text(encoding="utf-8"))
    graf = json.loads(args.grafeo.read_text(encoding="utf-8"))

    base_q = base["per_query"]
    graf_q = graf["per_query"]
    if len(base_q) != len(graf_q):
        print(f"WARN: query count mismatch: baseline={len(base_q)} grafeo={len(graf_q)}", file=sys.stderr)

    per_q = [per_query_compare(b, g) for b, g in zip(base_q, graf_q)]

    fused_j = [pq["fused_jaccard"] for pq in per_q]
    vec_j = [pq["vector_channel_jaccard"] for pq in per_q]
    text_j = [pq["text_channel_jaccard"] for pq in per_q]
    top1 = [pq["top1_agreement"] for pq in per_q]

    summary = {
        "queries_count": len(per_q),
        "fused_jaccard_mean": round(float(np.mean(fused_j)), 3),
        "fused_jaccard_median": round(float(np.median(fused_j)), 3),
        "fused_jaccard_min": round(float(np.min(fused_j)), 3),
        "vector_channel_jaccard_mean": round(float(np.mean(vec_j)), 3),
        "text_channel_jaccard_mean": round(float(np.mean(text_j)), 3),
        "top1_agreement_rate": round(sum(top1) / len(top1), 3),
        "latency": {
            "baseline": base["summary"]["latency_ms"],
            "grafeo": graf["summary"]["latency_ms"],
        },
    }
    report = {"summary": summary, "per_query": per_q}

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.out_md)
    print(f"Wrote {args.out_json}", file=sys.stderr)
    print(f"Wrote {args.out_md}", file=sys.stderr)
    print(f"Summary: {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
