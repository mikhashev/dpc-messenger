"""Task 001 extractor 2: markdown corpus → triples.jsonl.

Glob markdown across three source categories, chunk by heading, then call
GLM-5.1 (Z.AI subscription tier, Anthropic-compatible endpoint, concurrency
quota = 10) to extract structured (subject, predicate, object) triples per
chunk.

Sources (S130 finalized with Mike):
  - knowledge:  ~/.dpc/knowledge/*.md            (~165 files)
  - adr:        <repo>/docs/decisions/*.md       (~28 files)
  - ideas:      <repo>/ideas/**/*.md             (~80-90 files after exclusion)

Exclusion regex (ideas/ only): *-prompt.md, *_OLD.md, *_old.md.

Output: ~/.dpc/agents/<agent_id>/training_data/triples_md.jsonl
   Each line: {subject, predicate, object, source, source_category,
               source_file, chunk_heading, chunk_index, raw_chunk_excerpt}

Usage:
    # smoke calibration (5 mixed files, ~1 min)
    uv run python -m dpc_client_core.dpc_agent.training.extract_triples_from_markdown \\
        --agent-id agent_001 --smoke

    # full run (~15-25 min wall-clock at concurrency 10)
    uv run python -m dpc_client_core.dpc_agent.training.extract_triples_from_markdown \\
        --agent-id agent_001 --full
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
REPO_ROOT = Path(__file__).resolve().parents[5]

ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_MODEL = "glm-4.6"  # smoke fallback — overridden by --model
# Ark S130 [36]: 8 concurrent leaves 2 slots for the running DPC backend
# (which calls glm-5.1 for live Ark replies). Empirically 10 hit 429s.
CONCURRENCY = 8
RETRY_BACKOFF_SECONDS = (1, 2, 4)  # Ark S130 [36]: retry 429s before giving up

EXCLUSION_PATTERNS = [
    re.compile(r"-prompt\.md$"),
    re.compile(r"_OLD\.md$", re.IGNORECASE),
    re.compile(r"_old\.md$"),
]

EXTRACTION_PROMPT = """You extract factual relationships from technical markdown documents.

Output STRICTLY a JSON array of triple objects — nothing else, no prose, no markdown fences.

Each object has exactly three keys: subject, predicate, object.

Rules:
- predicate must be a short verb phrase ("uses", "implements", "depends on", "is part of", "replaces", "contradicts")
- subject and object are concrete named entities or concepts from the text (1-5 words each)
- Skip vague triples ("we think", "it may be"). Only extract claims the text asserts as fact.
- 3-10 triples per chunk. If chunk has no factual content, return [].

Document chunk:
\"\"\"
{chunk}
\"\"\"

Output JSON array:"""


@dataclass
class Chunk:
    source_file: str
    source_category: str
    chunk_heading: str
    chunk_index: int
    text: str


@dataclass
class Counters:
    files_processed: int = 0
    chunks_processed: int = 0
    triples_emitted: int = 0
    json_parse_failures: int = 0
    api_failures: int = 0
    rate_limit_hits: int = 0  # 429s, counted even when retry succeeds
    empty_chunks: int = 0
    by_category: dict = field(default_factory=dict)


def _excluded(p: Path) -> bool:
    name = p.name
    return any(rx.search(name) for rx in EXCLUSION_PATTERNS)


def _gather_files(smoke: bool) -> List[tuple[Path, str]]:
    knowledge_dir = DPC_HOME / "knowledge"
    adr_dir = REPO_ROOT / "docs" / "decisions"
    ideas_dir = REPO_ROOT / "ideas"

    knowledge = [(p, "knowledge") for p in knowledge_dir.glob("*.md") if p.is_file()]
    adrs = [(p, "adr") for p in adr_dir.glob("*.md") if p.is_file()]
    ideas = [(p, "ideas") for p in ideas_dir.rglob("*.md") if p.is_file() and not _excluded(p)]

    if smoke:
        return (knowledge[:1] + adrs[:1] + ideas[:3])
    return knowledge + adrs + ideas


def _chunk_markdown(text: str) -> List[tuple[str, str]]:
    """Split markdown by H1/H2/H3 headings. Returns [(heading, body), ...]."""
    lines = text.splitlines()
    chunks = []
    current_heading = "(preamble)"
    current_body: list = []

    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            if current_body:
                chunks.append((current_heading, "\n".join(current_body).strip()))
            current_heading = re.sub(r"^#{1,3}\s+", "", line).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_body:
        chunks.append((current_heading, "\n".join(current_body).strip()))

    return [(h, b) for (h, b) in chunks if b and len(b) > 80]


async def _extract_one(client, chunk: Chunk, counters: Counters, sem: asyncio.Semaphore, out_f) -> None:
    async with sem:
        prompt = EXTRACTION_PROMPT.format(chunk=chunk.text[:8000])

        text_out = None
        last_err = None
        for attempt, delay in enumerate([0, *RETRY_BACKOFF_SECONDS]):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.messages.create(
                    model=ZAI_MODEL,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                text_out = "".join(b.text for b in resp.content if hasattr(b, "text"))
                break
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "rate_limit" in msg or "1302" in msg:
                    counters.rate_limit_hits += 1
                    if attempt < len(RETRY_BACKOFF_SECONDS):
                        continue
                break

        if text_out is None:
            counters.api_failures += 1
            print(f"[ERR] LLM call failed for {chunk.source_file}#{chunk.chunk_heading}: {last_err}", file=sys.stderr)
            return

        text_out = text_out.strip()
        if text_out.startswith("```"):
            text_out = re.sub(r"^```(?:json)?\s*", "", text_out)
            text_out = re.sub(r"\s*```$", "", text_out)

        try:
            triples = json.loads(text_out)
        except json.JSONDecodeError:
            counters.json_parse_failures += 1
            return

        if not triples:
            counters.empty_chunks += 1
            return

        for t in triples:
            if not isinstance(t, dict):
                continue
            subj = (t.get("subject") or "").strip()
            pred = (t.get("predicate") or "").strip()
            obj = (t.get("object") or "").strip()
            if not (subj and pred and obj):
                continue
            record = {
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "source": "markdown",
                "source_category": chunk.source_category,
                "source_file": chunk.source_file,
                "chunk_heading": chunk.chunk_heading,
                "chunk_index": chunk.chunk_index,
                "raw_chunk_excerpt": chunk.text[:200],
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            counters.triples_emitted += 1
            counters.by_category[chunk.source_category] = (
                counters.by_category.get(chunk.source_category, 0) + 1
            )

        counters.chunks_processed += 1


async def run_extraction(agent_id: str, smoke: bool, model: str) -> Counters:
    global ZAI_MODEL
    ZAI_MODEL = model

    api_key = os.environ.get("ZAI_API_KEY")
    if not api_key:
        raise EnvironmentError("ZAI_API_KEY is not set in environment")

    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise ImportError("anthropic SDK required: pip install anthropic") from e

    client = AsyncAnthropic(api_key=api_key, base_url=ZAI_BASE_URL)

    out_dir = DPC_HOME / "agents" / agent_id / "training_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("triples_md_smoke.jsonl" if smoke else "triples_md.jsonl")

    files = _gather_files(smoke)
    print(f"[Task 001 extractor 2] Files: {len(files)} ({'SMOKE' if smoke else 'FULL'}); model={model}")

    counters = Counters()
    sem = asyncio.Semaphore(CONCURRENCY)
    start = time.time()

    chunks_to_process: List[Chunk] = []
    for fp, category in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[WARN] Cannot read {fp}: {e}", file=sys.stderr)
            continue
        chunks = _chunk_markdown(text)
        for idx, (heading, body) in enumerate(chunks):
            chunks_to_process.append(Chunk(
                source_file=str(fp.relative_to(REPO_ROOT.parent) if REPO_ROOT.parent in fp.parents else fp),
                source_category=category,
                chunk_heading=heading,
                chunk_index=idx,
                text=body,
            ))
        counters.files_processed += 1

    print(f"[Task 001 extractor 2] Chunks: {len(chunks_to_process)} from {counters.files_processed} files")

    with out_path.open("w", encoding="utf-8") as out_f:
        await asyncio.gather(
            *(_extract_one(client, ch, counters, sem, out_f) for ch in chunks_to_process)
        )

    elapsed = time.time() - start
    print(f"[Task 001 extractor 2] Done in {elapsed:.1f}s. Output: {out_path}")
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Task 001 — Markdown → triples extractor (LLM-bound)")
    parser.add_argument("--agent-id", default="agent_001")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="Smoke calibration on 5 mixed files")
    mode.add_argument("--full", action="store_true", help="Full corpus pass")
    parser.add_argument("--model", default="glm-4.6", help="ZAI model (default: glm-4.6 — change to glm-5.1 when alias resolves)")
    args = parser.parse_args()

    counters = asyncio.run(run_extraction(args.agent_id, args.smoke, args.model))
    print(json.dumps({
        "files_processed": counters.files_processed,
        "chunks_processed": counters.chunks_processed,
        "triples_emitted": counters.triples_emitted,
        "json_parse_failures": counters.json_parse_failures,
        "api_failures": counters.api_failures,
        "rate_limit_hits": counters.rate_limit_hits,
        "empty_chunks": counters.empty_chunks,
        "by_category": counters.by_category,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
