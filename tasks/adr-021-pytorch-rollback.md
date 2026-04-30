# ADR-021: PyTorch Rollback — Task Decomposition

**ADR:** [021-pytorch-unified-ml-framework.md](../docs/decisions/021-pytorch-unified-ml-framework.md)
**Status:** READY FOR IMPLEMENTATION
**Session:** S82 (decision), S83 (implementation)

---

## Task 1: pyproject.toml — restore PyTorch deps
- Restore: `torch`, `torchvision`, `accelerate`, `sentence-transformers`
- Restore: `[tool.uv.sources]` torch CUDA index + `[[tool.uv.index]]` pytorch-cu124
- **Files:** `pyproject.toml`
- **Scope:** ~15 lines

## Task 2: pyproject.toml — remove ONNX deps
- Remove: `onnx-asr`, `nvidia-cudnn-cu12`
- Keep: `onnxruntime-gpu` (still used by BGE-M3 ONNX path in memory.py as fallback)
- Remove: `onnx`, `onnxconverter-common` (dev-only, not in pyproject)
- **Files:** `pyproject.toml`
- **Scope:** ~3 lines

## Task 3: whisper_provider.py — restore PyTorch pipeline
- Revert to pre-ARCH-29 version from git history (`git show HEAD~N:path`)
- Keep: `_get_audio_duration()` from review fix (soundfile.info)
- Keep: `asyncio.Lock` in `__init__` (review fix)
- **Files:** `whisper_provider.py`
- **Scope:** ~635 lines (restore from git)

## Task 4: memory.py — restore sentence-transformers primary path
- Remove ONNX-specific: `_onnx_providers()` tuple format, `gpu_mem_limit`, `cudnn_conv_use_max_workspace`
- Remove: quantized model lookup (`model_fp16.onnx`, `model_int8.onnx`)
- Restore: `_load_sentence_transformers()` as primary path when model contains "bge-m3"
- Keep: ONNX fallback path for backwards compat
- **Files:** `memory.py`
- **Scope:** ~30 lines changed

## Task 5: llm_manager.py — restore Whisper defaults
- Revert default whisper config to PyTorch format (device, compile_model, etc.)
- **Files:** `llm_manager.py`
- **Scope:** ~10 lines

## Task 6: settings.py — restore defaults
- Revert `provider_priority` and `model` defaults
- **Files:** `settings.py`
- **Scope:** ~5 lines

## Task 7: run_service.py — remove NVIDIA PATH hack
- Remove NVIDIA DLL directory PATH setup (torch bundles cuDNN)
- **Files:** `run_service.py`
- **Scope:** ~14 lines removed

## Task 8: providers.json — restore whisper entry
- Restore `whisper-large-v3-turbo` with PyTorch config fields
- **Files:** `~/.dpc/providers.json` (user config)
- **Scope:** ~10 lines

## Task 9: uv sync + runtime verification
- `uv sync` to install torch, remove ONNX packages
- Test: embeddings indexing on GPU
- Test: Whisper transcription (voice message from Telegram)
- Verify: VRAM usage (should be automatic, no arena config needed)
- **Scope:** manual testing

## Task 10: backlog.md + docs update
- Update backlog: ARCH-29 → REVERTED, add ADR-021 rollback entry
- Clean up any ONNX references in CLAUDE.md if needed
- **Files:** `backlog.md`
- **Scope:** ~15 lines

---

**Total estimate:** ~10 files, ~700 lines changed (mostly whisper_provider.py restore)
**Risk:** LOW — reverting to proven code that worked before S82
**Dependencies:** None — pure rollback
