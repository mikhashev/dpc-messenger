"""Task 002: paraphrase corpus_input.jsonl triples → corpus.jsonl.

Each triple is rewritten into k=3 natural-language sentences via GLM-5.1
(Z.AI subscription, concurrency 8 + retry backoff on 429). Each paraphrase
preserves the factual content of (subject, predicate, object) while varying
surface form — DRAGON insight (002 §4): language models learn better from
prose than from raw `(subj, pred, obj)` tokens.

Provenance preserved per output entry:
  - source_triple_hash — links back to triple in corpus_input.jsonl
  - template_idx — 0..k-1 paraphrase index within the triple's k variants
  - source_category — knowledge / adr / ideas / kg

Output: ~/.dpc/agents/<agent_id>/training_data/corpus.jsonl
                                                corpus_stats.json

Usage:
    # smoke (10 triples, ~30 sec)
    uv run python -m dpc_client_core.dpc_agent.training.paraphrase_corpus \\
        --agent-id agent_001 --smoke

    # full (24,922 triples × 3 = ~75K LLM calls, ~30-45 min)
    uv run python -m dpc_client_core.dpc_agent.training.paraphrase_corpus \\
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))

ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_MODEL_DEFAULT = "glm-5.1"
CONCURRENCY = 8
RETRY_BACKOFF_SECONDS = (1, 2, 4)
K_PARAPHRASES = 3
SMOKE_N = 10

PARAPHRASE_PROMPT = """You rewrite factual triples into natural English sentences.

Input triple: ({subject}, {predicate}, {object})

Output exactly {k} different sentences expressing the same fact. Each must:
- Be a complete grammatical English sentence
- Preserve the factual content (subject, relation, object)
- Vary the wording — different word order, voice, or phrasing
- Be 5-30 words

Output STRICTLY a JSON array of {k} strings, nothing else. No prose, no markdown fences.

Example for ("nanoGPT", "is", "50M params"):
["nanoGPT is a model with 50M parameters.", "The 50M-parameter model nanoGPT.", "With 50M parameters, nanoGPT is a small model."]

Now rewrite ({subject}, {predicate}, {object}):"""


@dataclass
class Counters:
    triples_processed: int = 0
    paraphrases_emitted: int = 0
    json_parse_failures: int = 0
    api_failures: int = 0
    rate_limit_hits: int = 0
    by_category: dict = field(default_factory=dict)


async def _paraphrase_one(client, model: str, triple: dict, counters: Counters,
                          sem: asyncio.Semaphore, out_f) -> None:
    async with sem:
        prompt = PARAPHRASE_PROMPT.format(
            subject=triple["subject"],
            predicate=triple["predicate"],
            object=triple["object"],
            k=K_PARAPHRASES,
        )

        text_out = None
        last_err = None
        for attempt, delay in enumerate([0, *RETRY_BACKOFF_SECONDS]):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await client.messages.create(
                    model=model,
                    max_tokens=512,
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
            print(f"[ERR] paraphrase failed for hash={triple.get('triple_hash','?')[:10]}: {last_err}",
                  file=sys.stderr)
            return

        text_out = text_out.strip()
        if text_out.startswith("```"):
            text_out = re.sub(r"^```(?:json)?\s*", "", text_out)
            text_out = re.sub(r"\s*```$", "", text_out)

        try:
            paraphrases = json.loads(text_out)
        except json.JSONDecodeError:
            counters.json_parse_failures += 1
            return

        if not isinstance(paraphrases, list):
            counters.json_parse_failures += 1
            return

        for idx, text in enumerate(paraphrases[:K_PARAPHRASES]):
            if not isinstance(text, str) or not text.strip():
                continue
            record = {
                "text": text.strip(),
                "source_triple_hash": triple.get("triple_hash", ""),
                "template_idx": idx,
                "subject": triple["subject"],
                "predicate": triple["predicate"],
                "object": triple["object"],
                "source_category": triple.get("source_category") or triple.get("source", "unknown"),
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            counters.paraphrases_emitted += 1
            cat = record["source_category"]
            counters.by_category[cat] = counters.by_category.get(cat, 0) + 1

        counters.triples_processed += 1


async def run_paraphrase(agent_id: str, smoke: bool, model: str) -> Counters:
    api_key = os.environ.get("ZAI_API_KEY")
    if not api_key:
        raise EnvironmentError("ZAI_API_KEY is not set")

    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise ImportError("anthropic SDK required: pip install anthropic") from e

    client = AsyncAnthropic(api_key=api_key, base_url=ZAI_BASE_URL)

    data_dir = DPC_HOME / "agents" / agent_id / "training_data"
    input_path = data_dir / "corpus_input.jsonl"
    if not input_path.exists():
        raise FileNotFoundError(f"corpus_input.jsonl not found at {input_path}")

    triples = []
    with input_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    triples.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if smoke:
        triples = triples[:SMOKE_N]
        out_path = data_dir / "corpus_smoke.jsonl"
    else:
        out_path = data_dir / "corpus.jsonl"

    print(f"[paraphrase] Triples: {len(triples)} ({'SMOKE' if smoke else 'FULL'}); model={model}; k={K_PARAPHRASES}")

    counters = Counters()
    sem = asyncio.Semaphore(CONCURRENCY)
    start = time.time()

    with out_path.open("w", encoding="utf-8") as out_f:
        await asyncio.gather(
            *(_paraphrase_one(client, model, t, counters, sem, out_f) for t in triples)
        )

    elapsed = time.time() - start
    print(f"[paraphrase] Done in {elapsed:.1f}s. Output: {out_path}")
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Task 002 — paraphrase corpus_input.jsonl")
    parser.add_argument("--agent-id", default="agent_001")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="10-triple smoke calibration")
    mode.add_argument("--full", action="store_true", help="Full corpus pass")
    parser.add_argument("--model", default=ZAI_MODEL_DEFAULT)
    args = parser.parse_args()

    counters = asyncio.run(run_paraphrase(args.agent_id, args.smoke, args.model))
    print(json.dumps({
        "triples_processed": counters.triples_processed,
        "paraphrases_emitted": counters.paraphrases_emitted,
        "json_parse_failures": counters.json_parse_failures,
        "api_failures": counters.api_failures,
        "rate_limit_hits": counters.rate_limit_hits,
        "by_category": counters.by_category,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
