# ADR-033 compaction benches

Five progressive benchmarks that produced the ADR-033 design (see the *Validation*
section of `../033-agent-tool-loop-llm-compaction.md`). Each stage killed a wrong
hypothesis before any production code was written.

Run from `dpc-client/core` (needs the core deps + a configured `deepseek_flash`
provider; the local bench needs a local Ollama model):

```
uv run python docs/decisions/adr-033-benches/bench_compaction.py
```

| Script | Question | Finding |
|--------|----------|---------|
| `bench_compaction.py` | single-shot summary of old tool-history on flash | 33s median, ≫10s timeout |
| `bench_compaction_archive.py` | same on a real autoresearch session | old-history = 1.4M tokens, exceeds the model window → incremental required |
| `bench_compaction_incremental.py` | per-round summary | 53.8s median, uncorrelated with input size → batching is not the latency lever |
| `bench_compaction_local.py` | local model vs flash | local 2–7s vs flash 43–86s → the model is the lever |
| `bench_compaction_nothink.py` | flash thinking-off vs on vs local | flash thinking-off = 1.7–4.1s, the winner |

They read real tool-history from `~/.dpc/agents/*/logs/tools.jsonl` and an archived
group session; paths are hardcoded at the top of each script. They are diagnostic
scripts, not tests — kept for reproducibility so the next compaction/fidelity
investigation does not reinvent the method.
