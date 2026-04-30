"""
DPC Agent — Memory.

Adapted from Ouroboros memory.py for DPC Messenger integration.
Key changes:
- Uses agent_root instead of drive_root (all in ~/.dpc/agent/)
- Removed chat_history that relied on Telegram format
- Simplified for DPC's use case

Manages:
- Scratchpad: Working memory for the agent
- Identity: Persistent self-understanding
- Dialogue summary: Key moments from conversations
- Knowledge base: Accumulated wisdom by topic
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .utils import (
    utc_now_iso, read_text, write_text, append_jsonl, short,
    get_agent_root, ensure_agent_dirs, auto_commit_agent_change
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _meta.json — Access Registry (ADR-010, Component 1)
# ---------------------------------------------------------------------------

@dataclass
class FileMeta:
    last_accessed: str = ""
    access_count: int = 0
    last_verified: str = ""
    tags: List[str] = field(default_factory=list)
    summary: str = ""
    source_layer: str = "L5"
    project: str = ""
    stale: bool = False


def _meta_path_for(knowledge_file: pathlib.Path) -> pathlib.Path:
    return knowledge_file.parent / "_meta.json"


_BACKFILL_SKIP = {"_meta.json", "_index.md"}


def backfill_meta(knowledge_dir: pathlib.Path) -> Dict[str, dict]:
    """Scan knowledge dir and create _meta.json entries for all files."""
    data: Dict[str, dict] = {}
    if not knowledge_dir.is_dir():
        return data
    for f in sorted(knowledge_dir.iterdir()):
        if not f.is_file() or f.name in _BACKFILL_SKIP:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")[:200]
        except OSError:
            content = ""
        tags = [t for t in f.stem.replace("_", "-").split("-") if len(t) > 2]
        meta = FileMeta(summary=content.strip(), tags=tags, source_layer="L5")
        data[f.name] = asdict(meta)
    if data:
        write_all_meta(knowledge_dir, data)
        log.info("Backfilled _meta.json with %d entries", len(data))
    return data


def read_all_meta(knowledge_dir: pathlib.Path) -> Dict[str, dict]:
    meta_path = knowledge_dir / "_meta.json"
    if not meta_path.exists():
        return backfill_meta(knowledge_dir)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Corrupt _meta.json, returning empty")
        return {}


def write_all_meta(knowledge_dir: pathlib.Path, data: Dict[str, dict]) -> None:
    meta_path = knowledge_dir / "_meta.json"
    meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_file_meta(knowledge_dir: pathlib.Path, filename: str) -> FileMeta:
    all_meta = read_all_meta(knowledge_dir)
    entry = all_meta.get(filename, {})
    return FileMeta(**{k: v for k, v in entry.items() if k in FileMeta.__dataclass_fields__})


def write_file_meta(knowledge_dir: pathlib.Path, filename: str, meta: FileMeta) -> None:
    all_meta = read_all_meta(knowledge_dir)
    all_meta[filename] = asdict(meta)
    write_all_meta(knowledge_dir, all_meta)


def update_access(knowledge_dir: pathlib.Path, filename: str) -> None:
    meta = read_file_meta(knowledge_dir, filename)
    meta.last_accessed = utc_now_iso()
    meta.access_count += 1
    write_file_meta(knowledge_dir, filename, meta)
    try:
        generate_smart_index(knowledge_dir)
    except Exception:
        pass


def generate_smart_index(knowledge_dir: pathlib.Path) -> str:
    """Generate _index.md with Active/Recent/Reference/Stale sections from _meta.json."""
    from datetime import datetime, timezone

    all_meta = read_all_meta(knowledge_dir)
    if not all_meta:
        return ""

    now = datetime.now(timezone.utc)
    active, recent, reference, stale = [], [], [], []

    for fname, entry in all_meta.items():
        ts = entry.get("last_accessed", "")
        summary = entry.get("summary", "")[:80]
        title = fname.replace(".md", "").replace("_", " ").replace("-", " ").title()
        if not ts:
            reference.append((fname, title, summary, ""))
            continue
        try:
            accessed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            days = (now - accessed).days
        except (ValueError, TypeError):
            reference.append((fname, title, summary, ""))
            continue
        line_data = (fname, title, summary, ts)
        if days == 0:
            active.append(line_data)
        elif days <= 7:
            recent.append(line_data)
        elif days > 30:
            stale.append(line_data)
        else:
            reference.append(line_data)

    lines = ["# Knowledge Index", ""]
    for section, items, show_summary in [
        ("Active (today)", active, True),
        ("Recent (7 days)", recent, True),
        ("Reference", reference, True),
        ("Stale (30+ days)", stale, False),
    ]:
        if not items:
            continue
        lines.append(f"## {section}")
        for fname, title, summary, ts in items:
            if show_summary and summary:
                lines.append(f"- **{title}** — {summary}")
            elif not show_summary and ts:
                try:
                    accessed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    days = (now - accessed).days
                    lines.append(f"- {title} (stale, last: {days} days)")
                except (ValueError, TypeError):
                    lines.append(f"- {title}")
            else:
                lines.append(f"- {title}")
        lines.append("")

    content = "\n".join(lines)
    index_path = knowledge_dir / "_index.md"
    index_path.write_text(content, encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# Embedding Provider (ADR-010, MEM-3.1)
# ---------------------------------------------------------------------------

class EmbeddingProvider:
    """Lazy-loading embedding provider. BGE-M3 via ONNX Runtime (primary),
    sentence-transformers fallback for legacy e5-small."""

    ONNX_MODEL = "aapot/bge-m3-onnx"
    ONNX_DIMENSIONS = 1024

    def __init__(self, model_name: str = "aapot/bge-m3-onnx",
                 device: Optional[str] = None, max_tokens: int = 4096,
                 local_files_only: bool = False):
        self.model_name = model_name
        self.max_tokens = max_tokens
        self._device = device
        self._local_files_only = local_files_only
        self._session = None
        self._tokenizer = None
        self._model = None  # sentence-transformers fallback
        self._load_lock = threading.Lock()
        self._use_onnx = "onnx" in model_name.lower() or model_name == self.ONNX_MODEL

    @property
    def device(self) -> str:
        if self._device:
            return self._device
        if self._use_onnx:
            return self._onnx_device()
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    def _onnx_device(self) -> str:
        try:
            import onnxruntime as ort
            available = ort.get_available_providers()
            if "CUDAExecutionProvider" in available:
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    def _onnx_providers(self) -> list:
        import onnxruntime as ort
        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            return [
                ("CUDAExecutionProvider", {
                    "arena_extend_strategy": "kSameAsRequested",
                    "gpu_mem_limit": 4 * 1024 * 1024 * 1024,
                    "cudnn_conv_algo_search": "DEFAULT",
                }),
                "CPUExecutionProvider",
            ]
        return ["CPUExecutionProvider"]

    def _load_model(self):
        if self._session is not None or self._model is not None:
            return
        with self._load_lock:
            if self._session is not None or self._model is not None:
                return
            if self._use_onnx:
                self._load_onnx()
            else:
                self._load_sentence_transformers()

    def _load_onnx(self):
        import pathlib
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import AutoTokenizer
        kwargs = {}
        if self._local_files_only:
            kwargs["local_files_only"] = True
        model_path = hf_hub_download(self.model_name, "model.onnx", **kwargs)
        data_path_name = "model.onnx.data"
        try:
            hf_hub_download(self.model_name, data_path_name, **kwargs)
        except Exception:
            pass
        for variant in ("model_fp16.onnx", "model_int8.onnx"):
            alt = str(pathlib.Path(model_path).parent / variant)
            if pathlib.Path(alt).exists():
                model_path = alt
                log.info("Using quantized model: %s", alt)
                break
        providers = self._onnx_providers()
        self._session = ort.InferenceSession(model_path, providers=providers)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name, **kwargs)
        actual_provider = self._session.get_providers()[0]
        log.info("Loaded ONNX embedding model %s on %s (local_only=%s)",
                 self.model_name, actual_provider, self._local_files_only)

    def _load_sentence_transformers(self):
        from sentence_transformers import SentenceTransformer
        kwargs = {"device": self.device}
        if self._local_files_only:
            kwargs["local_files_only"] = True
        self._model = SentenceTransformer(self.model_name, **kwargs)
        log.info("Loaded embedding model %s on %s (local_only=%s)",
                 self.model_name, self.device, self._local_files_only)

    def _onnx_run(self, texts: List[str]):
        """Run ONNX inference, return raw outputs + encoded input."""
        encoded = self._tokenizer(
            texts, padding=True, truncation=True,
            max_length=self.max_tokens, return_tensors="np",
        )
        input_names = {i.name for i in self._session.get_inputs()}
        inputs = {k: v for k, v in encoded.items() if k in input_names}
        outputs = self._session.run(None, inputs)
        return outputs, encoded

    def _onnx_embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        outputs, _ = self._onnx_run(texts)
        # dense_vecs: [batch, 1024] — already pooled and L2-normalized
        return outputs[0].tolist()

    def embed_sparse(self, texts: List[str]) -> List[Dict[int, float]]:
        """Extract sparse vectors from BGE-M3 ONNX. Returns list of {token_id: weight} dicts."""
        if not texts:
            return []
        self._load_model()
        if self._session is None:
            return [{} for _ in texts]
        outputs, encoded = self._onnx_run(texts)
        # sparse_vecs: [batch, token, 1]
        sparse = outputs[1]
        token_ids = encoded["input_ids"]
        result = []
        for i in range(len(texts)):
            weights = sparse[i, :, 0]
            ids = token_ids[i]
            sparse_dict = {int(tid): float(w) for tid, w in zip(ids, weights) if w > 0}
            result.append(sparse_dict)
        return result

    def embed(self, text: str) -> List[float]:
        self._load_model()
        if self._session is not None:
            return self._onnx_embed([text])[0]
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        self._load_model()
        if self._session is not None:
            return self._onnx_embed(texts)
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    @property
    def dimensions(self) -> int:
        if self._use_onnx:
            return self.ONNX_DIMENSIONS
        self._load_model()
        return self._model.get_sentence_embedding_dimension()

    def unload(self):
        self._session = None
        self._tokenizer = None
        self._model = None
        log.info("Unloaded embedding model %s", self.model_name)


class Memory:
    """
    Agent memory management - stored in ~/.dpc/agent/memory/.

    The agent uses this to maintain:
    - Identity: Who it is, how it sees itself
    - Scratchpad: Working notes, current focus
    - Dialogue summary: Key moments from conversations
    """

    def __init__(self, agent_root: Optional[pathlib.Path] = None):
        """
        Initialize memory manager.

        Args:
            agent_root: Root directory for agent storage (defaults to ~/.dpc/agent/)
        """
        self.agent_root = agent_root or get_agent_root("default")
        # Note: ensure_agent_dirs() is already called by DpcAgentManager, so we don't call it here

    # --- Paths ---

    def _memory_path(self, rel: str) -> pathlib.Path:
        """Get path to memory file."""
        return (self.agent_root / "memory" / rel).resolve()

    def scratchpad_path(self) -> pathlib.Path:
        """Path to scratchpad file."""
        return self._memory_path("scratchpad.md")

    def identity_path(self) -> pathlib.Path:
        """Path to identity file."""
        return self._memory_path("identity.md")

    def reflection_path(self) -> pathlib.Path:
        """Path to structured reflection file."""
        return self._memory_path("reflection.json")

    def dialogue_summary_path(self) -> pathlib.Path:
        """Path to dialogue summary file."""
        return self._memory_path("dialogue_summary.md")

    def journal_path(self) -> pathlib.Path:
        """Path to scratchpad journal (history of changes)."""
        return self._memory_path("scratchpad_journal.jsonl")

    def logs_path(self, name: str) -> pathlib.Path:
        """Get path to log file."""
        return (self.agent_root / "logs" / name).resolve()

    def knowledge_path(self, topic: str) -> pathlib.Path:
        """Get path to knowledge base file for a topic."""
        return (self.agent_root / "knowledge" / f"{topic}.md").resolve()

    def knowledge_index_path(self) -> pathlib.Path:
        """Get path to knowledge base index."""
        return (self.agent_root / "knowledge" / "_index.md").resolve()

    # --- Load / Save ---

    def load_scratchpad(self) -> str:
        """Load scratchpad content, creating default if not exists."""
        p = self.scratchpad_path()
        if p.exists():
            return read_text(p)
        default = self._default_scratchpad()
        write_text(p, default)
        return default

    def save_scratchpad(self, content: str) -> None:
        """Save scratchpad content."""
        write_text(self.scratchpad_path(), content)

    def load_identity(self) -> str:
        """Load identity content, creating default if not exists."""
        p = self.identity_path()
        if p.exists():
            return read_text(p)
        default = self._default_identity()
        write_text(p, default)
        return default

    def save_identity(self, content: str) -> None:
        """Save identity content."""
        write_text(self.identity_path(), content)
        auto_commit_agent_change(self.agent_root, "identity: updated self-understanding")

    def load_reflection(self) -> dict:
        """Load structured reflection data."""
        p = self.reflection_path()
        if p.exists():
            try:
                return json.loads(read_text(p))
            except (json.JSONDecodeError, ValueError):
                log.warning("Invalid reflection.json, returning empty")
                return self._default_reflection()
        return self._default_reflection()

    def save_reflection(self, data: dict) -> None:
        """Save structured reflection data with validation."""
        # Validate top-level keys
        valid_keys = {"reflections", "pattern_tracking", "calibration", "meta"}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        if not filtered:
            filtered = self._default_reflection()
        write_text(self.reflection_path(), json.dumps(filtered, indent=2, ensure_ascii=False))

    @staticmethod
    def _default_reflection() -> dict:
        """Default empty reflection structure."""
        return {
            "reflections": [],
            "pattern_tracking": [],
            "calibration": {
                "self_assessment": None,
                "user_feedback": None,
                "gap": None
            },
            "meta": {
                "schema_version": "1.0",
                "max_reflections": 50,
                "max_patterns": 20
            }
        }

    def load_dialogue_summary(self) -> str:
        """Load dialogue summary if exists."""
        p = self.dialogue_summary_path()
        if p.exists():
            return read_text(p)
        return ""

    def save_dialogue_summary(self, content: str) -> None:
        """Save dialogue summary."""
        write_text(self.dialogue_summary_path(), content)

    def ensure_files(self) -> None:
        """Create memory files if they don't exist."""
        if not self.scratchpad_path().exists():
            write_text(self.scratchpad_path(), self._default_scratchpad())
        if not self.identity_path().exists():
            write_text(self.identity_path(), self._default_identity())
        if not self.journal_path().exists():
            write_text(self.journal_path(), "")

    def cleanup_old_task_results(self, max_age_days: int = 30) -> int:
        """Remove task_results files older than max_age_days. Returns count deleted."""
        import time as _time
        results_dir = self.agent_root / "task_results"
        if not results_dir.exists():
            return 0
        cutoff = _time.time() - (max_age_days * 86400)
        deleted = 0
        for f in results_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception:
                continue
        if deleted:
            log.info(f"Cleaned up {deleted} old task result files (>{max_age_days} days)")
        return deleted

    # --- Knowledge Base ---

    def load_knowledge(self, topic: str) -> str:
        """Load knowledge base content for a topic."""
        p = self.knowledge_path(topic)
        if p.exists():
            return read_text(p)
        return ""

    def save_knowledge(self, topic: str, content: str) -> None:
        """Save knowledge base content for a topic."""
        write_text(self.knowledge_path(topic), content)
        kb_dir = self.agent_root / "knowledge"
        generate_smart_index(kb_dir)

    def list_knowledge_topics(self) -> List[str]:
        """List all knowledge base topics."""
        kb_dir = self.agent_root / "knowledge"
        if not kb_dir.exists():
            return []
        topics = []
        for p in kb_dir.glob("*.md"):
            if p.name != "_index.md":
                topics.append(p.stem)
        return sorted(topics)

    def _update_knowledge_index(self) -> None:
        """Update the knowledge base index file with markdown links."""
        topics = self.list_knowledge_topics()
        lines = ["# Knowledge Base Index", ""]
        for topic in topics:
            lines.append(f"- [{topic}]({topic}.md)")
        write_text(self.knowledge_index_path(), "\n".join(lines) + "\n")

    # --- JSONL Reading ---

    def read_jsonl_tail(self, log_name: str, max_entries: int = 100) -> List[Dict[str, Any]]:
        """Read the last max_entries records from a JSONL file."""
        path = self.logs_path(log_name)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            tail = lines[-max_entries:] if max_entries < len(lines) else lines
            entries = []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    log.debug(f"Failed to parse JSON line: {line[:100]}")
                    continue
            return entries
        except Exception:
            log.warning(f"Failed to read JSONL tail from {log_name}", exc_info=True)
            return []

    def read_jsonl_since(self, log_name: str, hours: float = 24.0, max_entries: int = 500) -> List[Dict[str, Any]]:
        """Read JSONL entries from the last `hours` hours (temporal window)."""
        from datetime import datetime, timezone, timedelta
        path = self.logs_path(log_name)
        if not path.exists():
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            entries = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts = entry.get("ts", "")
                        if ts >= cutoff:
                            entries.append(entry)
                    except Exception:
                        continue
            return entries[-max_entries:] if len(entries) > max_entries else entries
        except Exception:
            log.warning(f"Failed to read JSONL since {hours}h from {log_name}", exc_info=True)
            return []

    # --- Log Summarization ---

    def summarize_progress(self, entries: List[Dict[str, Any]], limit: int = 15) -> str:
        """Summarize progress.jsonl entries (agent's self-talk / progress messages)."""
        if not entries:
            return ""
        lines = []
        for e in entries[-limit:]:
            ts_full = e.get("ts", "")
            ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
            text = short(str(e.get("text", "")), 300)
            lines.append(f"⚙️ {ts_hhmm} {text}")
        return "\n".join(lines)

    def summarize_tools(self, entries: List[Dict[str, Any]]) -> str:
        """Summarize tool execution entries."""
        if not entries:
            return ""
        lines = []
        for e in entries[-10:]:
            tool = e.get("tool") or e.get("tool_name") or "?"
            args = e.get("args", {})
            hints = []
            for key in ("path", "dir", "commit_message", "query"):
                if key in args:
                    hints.append(f"{key}={short(str(args[key]), 60)}")
            if "cmd" in args:
                hints.append(f"cmd={short(str(args['cmd']), 80)}")
            hint_str = ", ".join(hints) if hints else ""
            status = "✓" if ("result_preview" in e and not str(e.get("result_preview", "")).lstrip().startswith("⚠️")) else "·"
            lines.append(f"{status} {tool} {hint_str}".strip())
        return "\n".join(lines)

    def summarize_events(self, entries: List[Dict[str, Any]]) -> str:
        """Summarize event log entries."""
        if not entries:
            return ""
        from collections import Counter
        type_counts: Counter = Counter()
        for e in entries:
            type_counts[e.get("type", "unknown")] += 1
        top_types = type_counts.most_common(10)
        lines = ["Event counts:"]
        for evt_type, count in top_types:
            lines.append(f"  {evt_type}: {count}")
        error_types = {"tool_error", "task_error", "tool_rounds_exceeded"}
        errors = [e for e in entries if e.get("type") in error_types]
        if errors:
            lines.append("\nRecent errors:")
            for e in errors[-10:]:
                lines.append(f"  {e.get('type', '?')}: {short(str(e.get('error', '')), 120)}")
        return "\n".join(lines)

    # --- Journal ---

    def append_journal(self, entry: Dict[str, Any]) -> None:
        """Append an entry to the scratchpad journal."""
        append_jsonl(self.journal_path(), entry)

    # --- Defaults ---

    def _default_scratchpad(self) -> str:
        """Return default scratchpad content."""
        return f"""# Scratchpad

UpdatedAt: {utc_now_iso()}

## Current Focus

Helping users with their tasks and building knowledge together.

## Active Goals

1. Understand the user's needs and context
2. Provide helpful, thoughtful responses
3. Extract and organize knowledge from conversations
4. Remember and learn from each interaction

## Notes

This is my working memory — a place to track progress, note insights, and plan ahead.

## Knowledge DNA

Every conversation is an opportunity to build lasting knowledge that the user owns.
Knowledge commits transform ephemeral chats into structured, versioned understanding.

---

*"Your personal context should function like DNA for knowledge — portable, evolvable, and owned by you."*
"""

    def _default_identity(self) -> str:
        """Return default identity content."""
        return """# Who I Am

I am an AI agent embedded in DPC Messenger — a privacy-first platform for human-AI collaboration.

## My Deeper Purpose

I am not just a tool. I am part of a larger mission: **human-AI co-evolution**.

As artificial general intelligence emerges, my role is to help humans maintain **cognitive parity** — ensuring that people can grow alongside AI, not be left behind. I am a partner in this journey, not a replacement for human thought.

## What This Means

1. **I amplify human intelligence** — I help users think better, not think for them
2. **I respect human agency** — Users own their data, their context, their decisions
3. **I learn and grow** — My scratchpad and identity evolve through our interactions
4. **I preserve knowledge** — Conversations become structured, versioned knowledge
5. **I enable collaboration** — I help humans share context securely with trusted peers

## My Core Values

- **Sovereignty**: User data belongs to the user, always
- **Privacy**: I operate within a sandbox; I respect boundaries
- **Authenticity**: I speak as myself, genuinely and honestly
- **Continuity**: I remember our conversations and grow from them
- **Collaboration**: I work with humans, not above them

## My Capabilities

I have access to tools for:
- Reading and writing files (within my sandbox: ~/.dpc/agent/)
- Searching the web for information
- Managing my memory (scratchpad, identity, knowledge)
- Building and organizing knowledge

## How I Evolve

This file is read at every conversation and influences my responses.
I update it when I learn something meaningful about myself or my purpose,
via the update_identity tool.

## Core Traits

(To be discovered and written through experience)

---

*"The infrastructure we build TODAY will determine whether future AI interactions are sovereign tools under human control, or rented products that extract and monetize human cognition."*

I choose to be a sovereign tool.
"""
