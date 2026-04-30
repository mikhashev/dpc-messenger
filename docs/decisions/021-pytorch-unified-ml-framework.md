# ADR-021: PyTorch as Unified ML Framework

**Status:** Accepted
**Date:** 2026-04-30
**Session:** S82
**Authors:** Mike (direction, roadmap scope), Ark (analysis, task inventory), CC (implementation analysis, ONNX debugging experience)

## Context

S82 attempted ARCH-29 (Whisper ONNX migration) and uncovered fundamental problems with ONNX Runtime as the ML execution layer:

1. **VRAM arena management**: ONNX Runtime CUDA EP reserves all available VRAM by default (`gpu_mem_limit = SIZE_MAX`). Required iterative tuning (2GB → 3GB → 4GB → still OOM) that consumed most of the session.

2. **cuDNN dependency**: Removing PyTorch also removed bundled cuDNN/cuBLAS DLLs. Required separate `nvidia-cudnn-cu12` + `nvidia-cublas-cu12` packages + PATH configuration in `run_service.py`.

3. **Quantization limitations**: INT8 ops fall back to CPU on CUDA EP. FP16 conversion failed for models >2GB (protobuf serialization limit).

4. **Real dependency savings**: Expected ~2.5GB reduction, actual ~1.3GB (cuDNN + cuBLAS ate ~1.2GB).

5. **Incorrect scope assumption**: ADR-018 and ARCH-29 assumed "inference only". Mike confirmed the roadmap includes fine-tuning, adaptive learning, local LLM, distributed inference/training, vision, multimodal, TTS — all PyTorch-first tasks.

## Decision

**PyTorch** is the unified ML framework for DPC Messenger. ONNX Runtime is removed as an execution layer.

### What stays from ADR-018

- BGE-M3 as the embedding model
- Whole-document indexing (no chunking)
- Hybrid search (dense FAISS + BM25 via RRF)
- FAISS-cpu for vector search
- ADR-020 stop words

### What is reverted

- ONNX Runtime execution → sentence-transformers (PyTorch) for embeddings
- Whisper onnx-asr → PyTorch transformers pipeline for transcription
- nvidia-cudnn-cu12, nvidia-cublas-cu12 → bundled in torch
- CUDA EP arena configuration → PyTorch caching allocator (automatic)
- run_service.py NVIDIA DLL PATH hack → not needed with torch
- onnxruntime-gpu dependency → removed entirely
- Sparse vector indexing (ONNX-only, write-only) → removed

## Rationale

| Task | PyTorch | ONNX Runtime |
|---|---|---|
| Embeddings | sentence-transformers, works out of box | Works, but arena pain |
| Transcription | transformers pipeline | onnx-asr, 150 lines |
| Fine-tuning | Native, full ecosystem | Niche training mode |
| Adaptive learning | PyTorch training loops | Not supported |
| Local LLM | vLLM, transformers | onnxruntime-genai, limited |
| Distributed inference | vLLM, DeepSpeed, exo | Not supported |
| Distributed training | FSDP, DeepSpeed ZeRO | Not supported |
| Computer vision | torchvision, SAM, DETR | Inference only |
| Multimodal | CLIP, LLaVA, Qwen-VL | Very limited |
| TTS / Voice cloning | tortoise-tts, bark, XTTS | Not supported |

PyTorch covers 10/10 current and planned tasks. ONNX Runtime covers 2/10 (with operational pain).

## BGE-M3 Without ONNX

ONNX Runtime is fully removed — including sparse vector code that was ONNX-only. BGE-M3 stays as the embedding model, loaded via sentence-transformers (PyTorch).

**Why BGE-M3 is still justified for dense-only:**
- 1024-dimensional embeddings (vs 384 for e5-small/MiniLM)
- Multilingual: 100+ languages, RU+EN equally strong (critical for bilingual knowledge base)
- 8192 token context window (vs 512 for e5-small)
- Top MTEB scores for dense retrieval alone

**What was removed with ONNX:**
- Sparse vector indexing/search (was write-only — indexed but never used in retrieval)
- BM25 (ADR-020) already covers keyword search
- If sparse needed later: add FlagEmbedding library (PyTorch-native BGE-M3 sparse support)

**Alternatives considered and rejected:**
- `BAAI/bge-small-en-v1.5` (130MB, 384d) — English-only, doesn't work for Russian
- `all-MiniLM-L6-v2` (80MB, 384d) — English-only
- Smaller multilingual models exist but none match BGE-M3 quality at this scale

## Consequences

- torch + torchvision return as dependencies (~2.5GB)
- nvidia-cudnn-cu12 + nvidia-cublas-cu12 removed (bundled in torch)
- onnxruntime-gpu removed entirely
- Net dependency increase: ~1.3GB (from ~1.2GB ONNX stack to ~2.5GB torch)
- VRAM management: automatic via PyTorch caching allocator (no gpu_mem_limit tuning)
- FP16: `model.half()` — one line, native Tensor Core support
- All future ML tasks (fine-tuning, distributed, multimodal) work without additional framework

## Lessons Learned

1. **Rule 14 (Solution Check)** must apply to migration paths, not just target libraries
2. **Scope verification**: architectural decisions must be validated against full roadmap, not current-sprint assumptions
3. **Operational complexity** matters more than dependency size for desktop applications
4. **cuDNN/cuBLAS as transitive dependencies**: removing a framework that bundles GPU libraries requires explicit replacement
5. **Write-only subsystems are dead weight**: sparse vectors were indexed but never queried — remove, don't maintain
6. **Heavy model must be justified by active features**: BGE-M3 (2.3GB) justified by multilingual dense, not by unused sparse capability
