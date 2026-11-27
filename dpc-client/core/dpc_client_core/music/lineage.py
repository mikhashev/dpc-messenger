"""The song lineage graph, `mp.lineage/1`: how a song evolved from its takes, edits and rejections.

Mike decided on 2026-11-20 that song creation is tracked as evolution (SPEC §12 decision 36, ADR-051
D29): each version records what changed from the one before and why versions were rejected, and the
process is shown as a graph of branches. This module builds that graph from what the core already
stores. It is **read-only**: it changes no job, take or library entry, and `write_lineage_files` only
writes two files beside a job's generated folder.

**Inputs** (plain dicts, so a test needs no engine):

- the jobs (`mp.job/1`) and takes (`mp.take/1`) of `music/jobs.py`: a take carries its engine, seed,
  batch fields, stage-1 QA report, and the person's decision (`pending`, `accepted`, `rejected` with
  `reason_text` / `reason_class` and `decided_at`);
- an optional `entry_of(library_track_id)` that answers the library entry (title, the `provenance`
  summary with `genome_sha256` / `intent_sha256` / `render_sha256`), or None.

**Nodes.** `intent`: the request of a `generate_from_text` job (generation zero of that branch; style,
lyrics shape + full text for live view, resolved seed, adapters with strengths, requester, and
optional request block), or of a song job from a source track (`generate_similar`, `cover`,
`generate_from_lyrics`: its caption, relation and what it kept). `take`: one file a job made (a
new song variant, or an edit sibling). `source`: a library track an edit or a song job started from that
no stored take explains (an imported file). Note that `render_html` (`lineage.html` shareable export)
keeps shape-only lyrics and omits private chat ids / `conversation_id` / personal message text. **Edges** are derived-from: `generate` (intent to take),
`edit` (parent to take, `op` the edit action, `region` its span; the takes of one edit job are siblings
and share `group`) and `derive` (the source track, a take or a `source` node, to a song job's intent;
`op` the lineage relation, "cover of", "same style as" or "new song from the lyrics of"). An edge into a
discarded take carries `rejected` and the `reason`, so a dead branch says why. **Events** are the
person's selections read from the takes: `keep` (with `over_take_ids`, the siblings it was picked over,
which is the A/B pick) and `discard` (with its reason text and failure class).

**The parent of an edit or a song job** is the take whose library entry is the job's stored source track
(`params.source_track_id`, else the `source_track_id` its takes recorded when they were made, else the
job's `track_id`), else the take whose file hash is the job's `params.source_content_hash`, else a
`source` node. The hash fallback is accepted only when exactly one take of the store has that hash (a
hash two takes share guesses no parent, Zcode #191): a relinked, deleted or cleaned track otherwise stays a
`source` node with a note naming the takes it matched, never a take of another song. The link is
**two-way** (follow-up 19, Zcode #199 finding 4): it joins the two jobs into one graph like an edit parent
does, so the graph seeded by the descendant pulls in the parent's job (and what hangs off it), and the graph
seeded by the parent pulls in the descendant. Only a take whose job has no lineage (a note-only edit)
cannot be joined, and the note says it is outside the graph. Region-edit takes record that
parent the same way (`parents[0].take_id` in their `mp.provenance/1`, written from the same lookup).

**Not recorded yet** (EVOLUTION.md §13, §14): `prefer` events of a plain A/B without a Keep, the
`N_delta` / `delta` of an edge, a per-edge `edge_kind` of clone / mutate, and who decided (only the
person can, so `decided_by` is always `person`). Each is named in `notes` of the document, and each
waits for a ruling of Mike's before it gets a shape.

**The document** (`mp.lineage/1`, `validate_lineage`) is one JSON object:

```
schema        "mp.lineage/1"
generated_at  ISO time
root          {track_id, job_id, node_id}   what was asked for; node_id is the node to focus
nodes[]       {id, kind: intent|source|take, label, generation, created_at, ...}
                intent: job_id, engine, params, job_state, state_reason (a song job's also relation)
                source: library_track_id, title
                take:   job_id, variant, take_kind new_track|edit, title, engine, params, qa
                        {verdict, failed[], not_measured[], text}, decision pending|keep|discard,
                        reason {text, class}|null, decided_at, library_track_id, content_hash,
                        duration_s, lufs_i, simulated, on_kept_path, ids{}|null
edges[]       {id, from, to, kind: generate|edit|derive, op, job_id, group, region{t_start,t_end}|null,
               rejected, reason{text, class}|null}
events[]      {id, at, kind: keep|discard, take_id, job_id, over_take_ids[], ab, reason|null,
               decided_by: {kind: "person"}}
jobs[]        {id, type, action, state, created_at, requested_by, take_ids[]}
stats         {jobs, takes, kept, discarded, pending, edits, branches, max_generation}
notes[]       what is not recorded yet
```

`generation` is the longest path from a root node, so a layout can put it in a column. Ids are the
store's own (`tak_...`), `intent:<job id>` and `source:<library id>`; nothing is keyed by creation order.
"""

from __future__ import annotations

import copy
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import conversation_paths
from . import job_schemas as js
from .qa import qa_summary
from .schemas import SchemaError

logger = logging.getLogger(__name__)

SCHEMA = "mp.lineage/1"
LINEAGE_JSON = "lineage.json"
LINEAGE_HTML = "lineage.html"

NODE_KINDS = ("intent", "source", "take")
EDGE_KINDS = ("generate", "edit", "derive")
DECISIONS = ("pending", "keep", "discard")
EVENT_KINDS = ("keep", "discard")
TAKE_KINDS = ("new_track", "edit")

GENERATE_TYPE = "generate_from_text"
EDIT_TYPE = "edit"
SONG_TYPES = js.SONG_TYPES      # a new song from a source track (music/similar.py)
NOTE_ACTION = "note"            # a comment-only edit job: it makes no audio, so it has no place in the graph
STYLE_MAX = 200
NOTES = [
    "A plain A/B pick without a Keep (a `prefer` event) is not recorded yet.",
    "The delta and N_delta of an edge (what changed, how many fields) are not recorded yet.",
    "An edge has no clone / mutate kind yet; `kind` and `op` say what the job did.",
]

_ENGINE_IDS = {"yue2": "yue2.cpp", "acestep": "acestep.cpp", "ace-step-1.5:text2music": "acestep.cpp"}


class LineageError(Exception):
    """The graph cannot be built for what was asked (`code` is a short machine word)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- building ------------------------------------------------------------------------------------


def _is_graph_job(job: Mapping[str, Any]) -> bool:
    if job.get("type") == GENERATE_TYPE:
        return True
    if job.get("type") in SONG_TYPES:
        return bool(job.get("track_id"))
    return job.get("type") == EDIT_TYPE and bool(job.get("track_id")) and job.get("action") not in (None, NOTE_ACTION)


def _engine_of(take: Optional[Mapping[str, Any]], job: Mapping[str, Any]) -> Optional[str]:
    if take and isinstance(take.get("engine"), str) and take["engine"]:
        return take["engine"]
    params = job.get("params") or {}
    eng = params.get("engine")
    if isinstance(eng, str) and eng:
        return _ENGINE_IDS.get(eng, eng)
    return None


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _adapters(params: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for a in params.get("adapters") or []:
        if isinstance(a, Mapping) and isinstance(a.get("name"), str) and a["name"]:
            entry: Dict[str, Any] = {"name": a["name"]}
            for k in ("music", "sound", "strength", "scale"):
                v = a.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    entry[k] = v
            out.append(entry)
        elif isinstance(a, str) and a:
            out.append({"name": a})
    return out


def _adapter_names(params: Mapping[str, Any]) -> List[str]:
    return [a["name"] for a in _adapters(params)]


def _format_adapter(a: Any) -> str:
    if isinstance(a, str):
        return a
    if not isinstance(a, Mapping) or not isinstance(a.get("name"), str) or not a["name"]:
        return ""
    parts = [f"{k} {a[k]:g}" for k in ("music", "sound", "strength", "scale")
             if isinstance(a.get(k), (int, float)) and not isinstance(a[k], bool)]
    return f"{a['name']} ({', '.join(parts)})" if parts else a["name"]


def _lyrics_shape(params: Mapping[str, Any]) -> Dict[str, Any]:
    lyrics = params.get("lyrics")
    if isinstance(lyrics, list):
        lines = [str(ln) for ln in lyrics]
        if not any(ln.strip() for ln in lines):
            return {"instrumental": True, "lines": 0}
        return {"instrumental": False, "lines": sum(1 for ln in lines if ln.strip() and not ln.strip().startswith("["))}
    if not isinstance(lyrics, str) or not lyrics.strip():
        return {"instrumental": True, "lines": 0}
    return {"instrumental": False, "lines": sum(1 for ln in lyrics.splitlines() if ln.strip() and not ln.strip().startswith("["))}


def _lyrics_text(params: Mapping[str, Any]) -> Optional[str]:
    lyrics = params.get("lyrics")
    if isinstance(lyrics, list):
        lines = [str(ln) for ln in lyrics]
        return "\n".join(lines) if any(ln.strip() for ln in lines) else None
    if isinstance(lyrics, str) and lyrics.strip():
        return lyrics
    return None


def _format_lyrics_shape(shape: Any) -> str:
    if not isinstance(shape, Mapping):
        return ""
    if shape.get("instrumental"):
        return "instrumental"
    lines = shape.get("lines")
    if isinstance(lines, int) and not isinstance(lines, bool):
        return f"{lines} line" if lines == 1 else f"{lines} lines"
    return "lyrics"


def _resolved_seed(params: Mapping[str, Any], takes: Sequence[Mapping[str, Any]] = ()) -> Any:
    raw = params.get("seed")
    if raw is not None:
        return raw
    seeded = [
        (t.get("variant"), t.get("seed"))
        for t in takes
        if isinstance(t.get("seed"), int) and not isinstance(t.get("seed"), bool)
    ]
    if not seeded:
        return "random" if "seed" in params else None
    unique = {s for _, s in seeded}
    if len(seeded) == 1:
        v, s = seeded[0]
        return f"random ({s} on v{v})" if v is not None else f"random ({s})"
    if len(unique) == 1:
        s = seeded[0][1]
        labels = [f"v{v}" for v, _ in seeded if v is not None]
        return f"random ({s} on {', '.join(labels)})" if len(labels) == len(seeded) else f"random ({s})"
    parts = [f"v{v}: {s}" if v is not None else str(s) for v, s in seeded]
    return f"random ({', '.join(parts)})"


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _intent_params(job: Mapping[str, Any], takes: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    p = job.get("params") or {}
    return _drop_none({
        "style": _clip(p.get("style"), STYLE_MAX) or None,
        "lyrics": _lyrics_shape(p),
        "lyrics_text": _lyrics_text(p),
        "variants": p.get("variants"), "duration_s": p.get("duration_s"),
        "seed": _resolved_seed(p, takes),
        "backbone": p.get("backbone"), "cot": p.get("cot"), "adapters": _adapters(p),
    })


def _song_intent_params(job: Mapping[str, Any], takes: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    p = job.get("params") or {}
    return _drop_none({
        "caption": _clip(p.get("caption"), STYLE_MAX) or None, "relation": js.SONG_RELATIONS.get(job.get("type")),
        "keep": copy.deepcopy(p.get("keep")) or None, "lyrics_mode": p.get("lyrics_mode"),
        "lyrics": _lyrics_shape(p),
        "lyrics_text": _lyrics_text(p),
        "reference": p.get("reference"), "kept": p.get("kept"), "melody_source": p.get("melody_source"),
        "variants": p.get("variants"), "duration_s": p.get("duration_s"),
        "seed": _resolved_seed(p, takes),
        "backbone": p.get("backbone"), "cot": p.get("cot"), "cover_strength": p.get("cover_strength"),
        "adapters": _adapters(p),
    })


def _take_params(take: Mapping[str, Any], job: Mapping[str, Any]) -> Dict[str, Any]:
    p = job.get("params") or {}
    out: Dict[str, Any] = {
        "variant": take.get("variant"), "seed": take.get("seed"), "lm_seed": take.get("lm_seed"),
        "synth_batch_size": take.get("synth_batch_size"), "synth_batch_index": take.get("synth_batch_index"),
        "duration_requested_s": take.get("duration_requested_s"),
    }
    if take.get("kind") == "edit":
        out.update({"action": job.get("action"), "region": copy.deepcopy(take.get("region") or job.get("region")),
                    "crossfade_ms": p.get("crossfade_ms"), "cut_with_shift": p.get("cut_with_shift"),
                    "takes": p.get("takes")})
    else:
        out.update({"backbone": p.get("backbone"), "cot": p.get("cot"), "adapters": _adapters(p)})
    return _drop_none(out)


def _reason(take: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if take.get("decision") != "rejected":
        return None
    return {"text": take.get("reason_text"), "class": take.get("reason_class")}


def _decision(take: Mapping[str, Any]) -> str:
    return {"accepted": "keep", "rejected": "discard"}.get(take.get("decision"), "pending")


def _qa(take: Mapping[str, Any]) -> Dict[str, Any]:
    s = qa_summary(take)
    return {"verdict": s.get("verdict"), "failed": list(s.get("failed") or []),
            "not_measured": list(s.get("not_measured") or []), "text": s.get("text")}


def _ids_of(entry: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    prov = (entry or {}).get("provenance")
    if not isinstance(prov, Mapping):
        return None
    ids = {k: prov[k] for k in ("genome_basis", "genome_sha256", "intent_sha256", "render_sha256") if isinstance(prov.get(k), str)}
    return ids or None


class _Index:
    """Lookups over the jobs and takes passed in; nothing in them is changed."""

    def __init__(self, jobs: Iterable[Mapping[str, Any]], takes: Iterable[Mapping[str, Any]]):
        self.jobs = {j["id"]: j for j in jobs if isinstance(j, Mapping) and isinstance(j.get("id"), str)}
        self.job_order = {jid: i for i, jid in enumerate(self.jobs)}
        self.takes = {t["id"]: t for t in takes if isinstance(t, Mapping) and isinstance(t.get("id"), str)}
        self.takes_of: Dict[str, List[Mapping[str, Any]]] = {}
        self.by_library: Dict[str, Mapping[str, Any]] = {}
        self.by_hash: Dict[str, List[Mapping[str, Any]]] = {}
        for t in sorted(self.takes.values(), key=lambda x: (x.get("variant") or 0, x["id"])):
            self.takes_of.setdefault(t.get("job_id"), []).append(t)
            if t.get("library_track_id"):
                self.by_library[t["library_track_id"]] = t
            h = (t.get("file") or {}).get("content_hash")
            if h:
                self.by_hash.setdefault(h, []).append(t)

    def source_track_of(self, job: Mapping[str, Any]) -> Optional[str]:
        """The library track a job started from, as stored: `params.source_track_id`, else the
        `source_track_id` its takes recorded, else the job's `track_id`."""
        sid = (job.get("params") or {}).get("source_track_id")
        if isinstance(sid, str) and sid:
            return sid
        for t in self.takes_of.get(job.get("id"), []):
            if isinstance(t.get("source_track_id"), str) and t["source_track_id"]:
                return t["source_track_id"]
        return job.get("track_id")

    def parent_of(self, job: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
        """`("take", take_id)` or `("source", library_track_id)` for an edit or a song job, None for any other:
        by the stored source track only. The content-hash fallback is not here: it is store-global, so
        `build_lineage` accepts it only inside the graph it is building (`hash_candidates`)."""
        if job.get("type") != EDIT_TYPE and job.get("type") not in SONG_TYPES:
            return None
        lib = self.source_track_of(job)
        hit = self.by_library.get(lib) if lib else None
        if hit is None and job.get("track_id") and job.get("track_id") != lib:
            hit = self.by_library.get(job["track_id"])
        if hit is not None:
            return ("take", hit["id"])
        return ("source", str(lib))

    def hash_candidates(self, job: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        """The takes of other jobs whose file hash is the job's `params.source_content_hash`."""
        h = (job.get("params") or {}).get("source_content_hash")
        found = self.by_hash.get(h) if isinstance(h, str) and h else None
        return [t for t in (found or []) if t.get("job_id") != job.get("id")]

    def hash_parent_jobs(self, graph_jobs: Mapping[str, Mapping[str, Any]]) -> Dict[str, str]:
        """`{job id: parent job id}` for the jobs of `graph_jobs` whose source track no take holds and whose
        `params.source_content_hash` matches exactly one take of the store whose job is a graph job too:
        the same uniqueness rule `_hash_parents` applies, evaluated once for the whole store so a graph
        seeded from either end finds the other."""
        links: Dict[str, str] = {}
        for jid, job in graph_jobs.items():
            parent = self.parent_of(job)
            if parent is None or parent[0] != "source":
                continue
            found = self.hash_candidates(job)
            if len(found) == 1 and found[0].get("job_id") in graph_jobs:
                links[jid] = found[0]["job_id"]
        return links


HASH_NOTE = "A source found by content hash only was not joined:"


def _hash_parents(idx: _Index, members: Sequence[str]) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """The content-hash fallback for the jobs of one graph whose source track no take holds (Zcode
    #191 on 4.2): accepted only when exactly one take of the store has the hash and that take's job is
    in this graph. `_component` joins the two jobs through the same rule (Zcode #199 finding 4), so a
    unique match is in the graph whichever end it was seeded from; what stays outside is a take whose
    job has no lineage. Otherwise the branch stays rooted at its `source` node and a note says why, so
    a relinked, deleted or cleaned track never pulls in a take of another song."""
    inside = set(members)
    parents: Dict[str, Tuple[str, str]] = {}
    notes: List[str] = []
    for jid in members:
        job = idx.jobs[jid]
        parent = idx.parent_of(job)
        if parent is None or parent[0] != "source":
            continue
        found = idx.hash_candidates(job)
        if not found:
            continue
        if len(found) > 1:
            notes.append(f"{HASH_NOTE} the source of job {jid} matches {len(found)} takes by content hash "
                         f"({', '.join(sorted(t['id'] for t in found))}), so its branch starts at a source node.")
        elif found[0].get("job_id") not in inside:
            notes.append(f"{HASH_NOTE} the source of job {jid} matches take {found[0]['id']} by content hash, but "
                         f"that take's job {found[0].get('job_id')} is outside this graph, so the branch starts at "
                         "a source node.")
        else:
            parents[jid] = ("take", found[0]["id"])
    return parents, notes


def _component(idx: _Index, seeds: Sequence[str]) -> List[str]:
    """The job ids connected to `seeds` through edit parents (up, down and across siblings) and through a
    unique content-hash parent (`_Index.hash_parent_jobs`)."""
    graph_jobs = {jid: j for jid, j in idx.jobs.items() if _is_graph_job(j)}
    adj: Dict[str, set] = {jid: set() for jid in graph_jobs}
    for jid, pj in idx.hash_parent_jobs(graph_jobs).items():   # a unique content-hash parent joins both ways
        adj[jid].add(pj)
        adj[pj].add(jid)
    by_source: Dict[str, List[str]] = {}
    for jid, job in graph_jobs.items():
        parent = idx.parent_of(job)
        if parent is None:
            continue
        if parent[0] == "take":
            pj = idx.takes[parent[1]].get("job_id")
            if pj in adj:
                adj[jid].add(pj)
                adj[pj].add(jid)
        else:
            by_source.setdefault(parent[1], []).append(jid)
    for group in by_source.values():
        for a in group:
            adj[a].update(group)
    seen: List[str] = []
    todo = [s for s in seeds if s in adj]
    while todo:
        jid = todo.pop()
        if jid in seen:
            continue
        seen.append(jid)
        todo.extend(sorted(adj[jid] - set(seen)))
    return seen


def _default_dpc_home() -> Path:
    from dpc_protocol.paths import dpc_home
    return dpc_home()


def _read_json_mapping(path: Path) -> Optional[Mapping[str, Any]]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                return data
    except Exception:
        logger.debug("lineage: could not read %s", path, exc_info=True)
    return None


def _disk_agent_name(home: Path, agent_id: str) -> Optional[str]:
    if not conversation_paths.is_safe_conversation_id(agent_id):
        return None
    cfg = _read_json_mapping(home / "agents" / agent_id / "config.json")
    if cfg and isinstance(cfg.get("name"), str) and cfg["name"].strip():
        return cfg["name"].strip()
    reg = _read_json_mapping(home / "agents" / "_registry.json")
    agents = reg.get("agents") if isinstance(reg, Mapping) else None
    entry = agents.get(agent_id) if isinstance(agents, Mapping) else None
    if isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry["name"].strip():
        return entry["name"].strip()
    return None


def _disk_conversation_name(
    home: Path,
    conversation_id: str,
    resolve_agent: Callable[[str], Optional[str]],
) -> Optional[str]:
    if not conversation_paths.is_safe_conversation_id(conversation_id):
        return None
    if conversation_id.startswith("group-"):
        for d in conversation_paths.existing_store_dirs(home / "conversations", conversation_id):
            meta = _read_json_mapping(d / "metadata.json")
            if meta and isinstance(meta.get("name"), str) and meta["name"].strip():
                return meta["name"].strip()
        legacy = _read_json_mapping(home / "groups" / f"{conversation_id}.json")
        if legacy and isinstance(legacy.get("name"), str) and legacy["name"].strip():
            return legacy["name"].strip()
    if conversation_id.startswith("agent_"):
        aname = resolve_agent(conversation_id)
        if aname and aname != conversation_id:
            return f"{aname} (1:1)"
    return None


def _job_request(job: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    req = job.get("request")
    if not isinstance(req, Mapping):
        return None
    cleaned = _drop_none(copy.deepcopy(dict(req)))
    return cleaned or None


def build_lineage(jobs: Iterable[Mapping[str, Any]], takes: Iterable[Mapping[str, Any]], *,
                  track_id: Optional[str] = None, job_id: Optional[str] = None,
                  entry_of: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
                  agent_name_of: Optional[Callable[[str], Optional[str]]] = None,
                  conversation_name_of: Optional[Callable[[str], Optional[str]]] = None,
                  dpc_home_dir: Optional[Path] = None,
                  generated_at: Optional[str] = None) -> Dict[str, Any]:
    """The `mp.lineage/1` document of the job chain that holds `track_id` (a library track id) or
    `job_id`; `LineageError("invalid_params")` when neither is given, `("not_found")` for a `job_id`
    that is not a job the graph covers. A track no job explains answers a graph of one `source` node.
    Read-only: the arguments are not changed."""
    if not track_id and not job_id:
        raise LineageError("invalid_params", "a lineage needs a track_id or a job_id")
    idx = _Index(jobs, takes)
    lookup = entry_of or (lambda _id: None)
    home = Path(dpc_home_dir) if dpc_home_dir is not None else _default_dpc_home()

    def entry(lib_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        if not lib_id:
            return None
        try:
            return lookup(lib_id)
        except Exception:
            logger.debug("lineage: no library entry for %s", lib_id, exc_info=True)
            return None

    def resolve_agent(agent_id: str) -> Optional[str]:
        if agent_name_of is not None:
            try:
                hit = agent_name_of(agent_id)
                if isinstance(hit, str) and hit.strip():
                    return hit.strip()
            except Exception:
                logger.debug("lineage: agent_name_of failed for %s", agent_id, exc_info=True)
        return _disk_agent_name(home, agent_id)

    def resolve_conversation(conversation_id: str) -> Optional[str]:
        if conversation_name_of is not None:
            try:
                hit = conversation_name_of(conversation_id)
                if isinstance(hit, str) and hit.strip():
                    return hit.strip()
            except Exception:
                logger.debug("lineage: conversation_name_of failed for %s", conversation_id, exc_info=True)
        return _disk_conversation_name(home, conversation_id, resolve_agent)

    def resolve_requested_by(job: Mapping[str, Any]) -> Dict[str, Any]:
        raw = job.get("requested_by")
        rb = dict(raw) if isinstance(raw, Mapping) else {}
        kind = rb.get("kind") if rb.get("kind") in ("person", "agent") else "person"
        req = job.get("request") if isinstance(job.get("request"), Mapping) else None
        if kind == "agent":
            agent_id = rb.get("agent_id") if isinstance(rb.get("agent_id"), str) and rb.get("agent_id") else None
            conv_id = rb.get("conversation_id") if isinstance(rb.get("conversation_id"), str) and rb.get("conversation_id") else None
            turn_id = rb.get("turn_id") if isinstance(rb.get("turn_id"), str) and rb.get("turn_id") else None
            agent_name = resolve_agent(agent_id) if agent_id else None
            conv_name = resolve_conversation(conv_id) if conv_id else None
            who = agent_name or agent_id or "agent"
            where = conv_name or conv_id
            label = f"asked by {who} in {where}" if where else f"asked by {who}"
            return _drop_none({
                "kind": "agent",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "conversation_id": conv_id,
                "conversation_name": conv_name,
                "turn_id": turn_id,
                "label": label,
            })
        surface = req.get("surface") if req and isinstance(req.get("surface"), str) and req.get("surface").strip() else "player"
        surface_label = "Player" if surface.lower() == "player" else surface
        return {
            "kind": "person",
            "surface": surface,
            "label": f"asked by you in {surface_label}",
        }

    seeds: List[str] = []
    if job_id:
        job = idx.jobs.get(job_id)
        if job is None or not _is_graph_job(job):
            raise LineageError("not_found", f"no music job {job_id!r} with a lineage")
        seeds.append(job_id)
    if track_id:
        hit = idx.by_library.get(track_id)
        if hit is not None:
            seeds.append(hit["job_id"])
        for jid, job in idx.jobs.items():
            if _is_graph_job(job) and idx.parent_of(job) == ("source", track_id):
                seeds.append(jid)
    members = _component(idx, seeds)
    members.sort(key=lambda j: (idx.jobs[j].get("created_at") or "", j))
    by_hash, hash_notes = _hash_parents(idx, members)

    def parent_in_graph(job: Mapping[str, Any]) -> Tuple[str, str]:
        return by_hash.get(job["id"]) or idx.parent_of(job)

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def add_node(node: Dict[str, Any]) -> None:
        nodes.setdefault(node["id"], node)

    def source_node(lib_id: str) -> str:
        e = entry(lib_id) or {}
        add_node({"id": f"source:{lib_id}", "kind": "source", "label": str(e.get("title") or lib_id),
                  "generation": 0, "created_at": e.get("added_at"), "library_track_id": lib_id,
                  "title": str(e.get("title") or lib_id)})
        return f"source:{lib_id}"

    for jid in members:
        job = idx.jobs[jid]
        jtakes = idx.takes_of.get(jid, [])
        req_by = resolve_requested_by(job)
        req_block = _job_request(job)
        if job["type"] == GENERATE_TYPE:
            parent_id = f"intent:{jid}"
            style = _clip((job.get("params") or {}).get("style"), 60)
            intent_node: Dict[str, Any] = {
                "id": parent_id, "kind": "intent", "label": style or "generate from text",
                "generation": 0, "created_at": job.get("created_at"), "job_id": jid,
                "engine": _engine_of(None, job), "params": _intent_params(job, jtakes),
                "job_state": job.get("state"), "state_reason": job.get("state_reason"),
                "requested_by": copy.deepcopy(req_by),
            }
            if req_block is not None:
                intent_node["request"] = copy.deepcopy(req_block)
            add_node(intent_node)
            edge_kind, op = "generate", GENERATE_TYPE
        elif job["type"] in SONG_TYPES:
            parent = parent_in_graph(job)
            from_id = parent[1] if parent[0] == "take" else source_node(parent[1])
            parent_id = f"intent:{jid}"
            relation = js.SONG_RELATIONS[job["type"]]
            intent_node = {
                "id": parent_id, "kind": "intent",
                "label": _clip((job.get("params") or {}).get("caption"), 60) or relation,
                "generation": 0, "created_at": job.get("created_at"), "job_id": jid,
                "engine": _engine_of(None, job), "params": _song_intent_params(job, jtakes), "relation": relation,
                "job_state": job.get("state"), "state_reason": job.get("state_reason"),
                "requested_by": copy.deepcopy(req_by),
            }
            if req_block is not None:
                intent_node["request"] = copy.deepcopy(req_block)
            add_node(intent_node)
            edges.append({"id": f"{from_id}>{parent_id}", "from": from_id, "to": parent_id, "kind": "derive",
                          "op": relation, "job_id": jid, "group": jid, "region": None, "rejected": False,
                          "reason": None})
            edge_kind, op = "generate", job["type"]
        else:
            parent = parent_in_graph(job)
            parent_id = parent[1] if parent[0] == "take" else source_node(parent[1])
            edge_kind, op = "edit", str(job.get("action"))
        for t in jtakes:
            e = entry(t.get("library_track_id"))
            file = t.get("file") or {}
            loud = t.get("loudness") if isinstance(t.get("loudness"), Mapping) else None
            add_node({"id": t["id"], "kind": "take", "label": str(t.get("title") or t["id"]), "generation": 0,
                      "created_at": t.get("created_at"), "job_id": jid, "variant": t.get("variant"),
                      "take_kind": t.get("kind"), "title": str(t.get("title") or ""), "engine": _engine_of(t, job),
                      "params": _take_params(t, job), "qa": _qa(t), "decision": _decision(t), "reason": _reason(t),
                      "decided_at": t.get("decided_at"), "library_track_id": t.get("library_track_id"),
                      "content_hash": file.get("content_hash"), "duration_s": file.get("duration_s"),
                      "lufs_i": loud.get("lufs_i") if loud else None, "simulated": bool(t.get("simulated")),
                      "on_kept_path": False, "ids": _ids_of(e)})
            region = t.get("region") or (job.get("region") if edge_kind == "edit" else None)
            reason = _reason(t)
            edges.append({"id": f"{parent_id}>{t['id']}", "from": parent_id, "to": t["id"], "kind": edge_kind,
                          "op": op, "job_id": jid, "group": jid,
                          "region": copy.deepcopy(dict(region)) if isinstance(region, Mapping) else None,
                          "rejected": reason is not None, "reason": reason})
    # An edit whose parent take is outside the graph's jobs never happens (the component joins them), but
    # an edge must not point at a node that is not there.
    edges = [e for e in edges if e["from"] in nodes and e["to"] in nodes]

    edges, broken = _break_cycles(nodes, edges)
    _set_generations(nodes, edges)
    _mark_kept_path(nodes, edges)
    events = _events(idx, [nodes[n]["id"] for n in nodes if nodes[n]["kind"] == "take"])

    focus = _focus(idx, nodes, track_id, job_id, source_node_for=lambda lib: f"source:{lib}")
    if not nodes and track_id:
        focus = source_node(track_id)
        _set_generations(nodes, edges)
    take_nodes = [n for n in nodes.values() if n["kind"] == "take"]
    has_out = {e["from"] for e in edges}
    def job_summary(jid: str) -> Dict[str, Any]:
        j = idx.jobs[jid]
        out: Dict[str, Any] = {
            "id": jid, "type": j["type"], "action": j.get("action"),
            "state": j.get("state"), "created_at": j.get("created_at"),
            "requested_by": resolve_requested_by(j),
            "take_ids": list(j.get("take_ids") or []),
        }
        req_block = _job_request(j)
        if req_block is not None:
            out["request"] = req_block
        return out

    doc = {
        "schema": SCHEMA,
        "generated_at": generated_at or _now(),
        "root": {"track_id": track_id, "job_id": job_id, "node_id": focus},
        "nodes": sorted(nodes.values(), key=lambda n: (n["generation"], n.get("created_at") or "", n["id"])),
        "edges": edges,
        "events": events,
        "jobs": [job_summary(jid) for jid in members],
        "stats": {
            "jobs": len(members), "takes": len(take_nodes),
            "kept": sum(1 for n in take_nodes if n["decision"] == "keep"),
            "discarded": sum(1 for n in take_nodes if n["decision"] == "discard"),
            "pending": sum(1 for n in take_nodes if n["decision"] == "pending"),
            "edits": sum(1 for n in take_nodes if n["take_kind"] == "edit"),
            "branches": sum(1 for n in nodes.values() if n["id"] not in has_out and n["kind"] == "take"),
            "max_generation": max((n["generation"] for n in nodes.values()), default=0),
        },
        "notes": list(NOTES) + hash_notes + broken,
    }
    validate_lineage(doc)
    return doc


def _focus(idx: _Index, nodes: Mapping[str, Any], track_id: Optional[str], job_id: Optional[str],
           source_node_for: Callable[[str], str]) -> Optional[str]:
    if track_id:
        hit = idx.by_library.get(track_id)
        if hit is not None and hit["id"] in nodes:
            return hit["id"]
        if source_node_for(track_id) in nodes:
            return source_node_for(track_id)
    if job_id:
        if f"intent:{job_id}" in nodes:
            return f"intent:{job_id}"
        first = next((t["id"] for t in idx.takes_of.get(job_id, []) if t["id"] in nodes), None)
        if first:
            return first
    return None


CYCLE_NOTE = "Cycle broken:"


def _break_cycles(nodes: Mapping[str, Mapping[str, Any]],
                  edges: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The edges without those that close a loop (a store whose records point in a circle), and a note
    per edge left out, so the rest of the graph is still drawn. A depth-first walk from the nodes in a
    fixed order (generation-zero candidates first: no parents, then by time and id) drops each edge that
    reaches a node still on the walk's path."""
    children: Dict[str, List[Dict[str, Any]]] = {}
    has_parent = set()
    for e in edges:
        children.setdefault(e["from"], []).append(e)
        has_parent.add(e["to"])
    order = sorted(nodes, key=lambda n: (n in has_parent, nodes[n].get("created_at") or "", n))
    state: Dict[str, int] = {}          # 1 on the path, 2 done
    dropped: List[Dict[str, Any]] = []
    for start in order:
        if state.get(start):
            continue
        state[start] = 1
        stack = [(start, iter(children.get(start, [])))]
        while stack:
            node, it = stack[-1]
            e = next(it, None)
            if e is None:
                state[node] = 2
                stack.pop()
                continue
            to = e["to"]
            if state.get(to) == 1:
                dropped.append(e)
            elif not state.get(to):
                state[to] = 1
                stack.append((to, iter(children.get(to, []))))
    if not dropped:
        return edges, []
    gone = {id(e) for e in dropped}
    notes = [f"{CYCLE_NOTE} the {e['kind']} edge {e['from']} -> {e['to']} ({e.get('op') or e['kind']}) closes a "
             "loop in the stored records and is left out; the rest of the graph is drawn." for e in dropped]
    return [e for e in edges if id(e) not in gone], notes


def _set_generations(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Mapping[str, Any]]) -> None:
    parents: Dict[str, List[str]] = {}
    for e in edges:
        parents.setdefault(e["to"], []).append(e["from"])
    memo: Dict[str, int] = {}

    def depth(n: str, stack: Tuple[str, ...] = ()) -> int:
        if n in memo:
            return memo[n]
        if n in stack:
            raise LineageError("cycle", f"the lineage has a cycle at {n}")
        memo[n] = 0 if not parents.get(n) else 1 + max(depth(p, stack + (n,)) for p in parents[n])
        return memo[n]

    for nid, node in nodes.items():
        node["generation"] = depth(nid)


def _mark_kept_path(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Mapping[str, Any]]) -> None:
    parents: Dict[str, List[str]] = {}
    for e in edges:
        parents.setdefault(e["to"], []).append(e["from"])
    todo = [n["id"] for n in nodes.values() if n["kind"] == "take" and n["decision"] == "keep"]
    seen = set()
    while todo:
        nid = todo.pop()
        if nid in seen:
            continue
        seen.add(nid)
        todo.extend(parents.get(nid, []))
    for nid in seen:
        if nodes[nid]["kind"] == "take":
            nodes[nid]["on_kept_path"] = True


def _events(idx: _Index, take_ids: Sequence[str]) -> List[Dict[str, Any]]:
    out = []
    for tid in take_ids:
        t = idx.takes[tid]
        decision = _decision(t)
        if decision == "pending":
            continue
        siblings = [s["id"] for s in idx.takes_of.get(t.get("job_id"), []) if s["id"] != tid]
        out.append({"id": f"sel:{tid}", "at": t.get("decided_at"), "kind": decision, "take_id": tid,
                    "job_id": t.get("job_id"), "over_take_ids": siblings if decision == "keep" else [],
                    "ab": bool(siblings), "reason": _reason(t), "decided_by": {"kind": "person"}})
    out.sort(key=lambda e: (e["at"] or "", *_store_order(idx, e["take_id"])))
    return out


def _store_order(idx: _Index, take_id: str) -> Tuple[int, int, int]:
    """The tie-break of two decisions stamped in the same millisecond: the store keeps no sequence of
    decisions, so they follow the store's own order, the job's place among the jobs passed in, then the
    take's place in its job (`take_ids`, else its variant). Never the take id, which is random: two
    selections in one millisecond came out in either order from run to run (follow-up 17)."""
    take = idx.takes[take_id]
    jid = take.get("job_id")
    job_pos = idx.job_order.get(jid, len(idx.job_order))
    ids = list((idx.jobs.get(jid) or {}).get("take_ids") or [])
    variant = take.get("variant") if isinstance(take.get("variant"), int) else 1 << 30
    return job_pos, ids.index(take_id) if take_id in ids else len(ids) + variant, variant


# -- validating ------------------------------------------------------------------------------------


def _expect(ok: bool, message: str) -> None:
    if not ok:
        raise SchemaError(f"{SCHEMA}: {message}")


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_reason(where: str, reason: Any, needed: bool) -> None:
    if reason is None:
        _expect(not needed, f"{where} must carry a reason")
        return
    _expect(isinstance(reason, Mapping) and set(reason) == {"text", "class"}, f"{where}.reason needs text and class")
    _expect(reason["text"] is None or _is_str(reason["text"]), f"{where}.reason.text must be text or null")
    _expect(reason["class"] is None or _is_str(reason["class"]), f"{where}.reason.class must be text or null")
    _expect(reason["text"] is not None or reason["class"] is not None, f"{where}.reason needs a text or a class")


def validate_lineage(doc: Any) -> None:
    """Raise `SchemaError` unless `doc` is an `mp.lineage/1` as `build_lineage` writes it: the required
    keys, unique node ids, every edge and event pointing at nodes that exist, a discarded take carrying
    a reason and every other take none, and generations that grow along every edge."""
    _expect(isinstance(doc, Mapping), "the document must be an object")
    for k in ("schema", "generated_at", "root", "nodes", "edges", "events", "jobs", "stats", "notes"):
        _expect(k in doc, f"missing '{k}'")
    _expect(doc["schema"] == SCHEMA, f"'schema' must be {SCHEMA}")
    _expect(_is_str(doc["generated_at"]), "'generated_at' must be a string")
    root = doc["root"]
    _expect(isinstance(root, Mapping) and {"track_id", "job_id", "node_id"} <= set(root), "'root' needs track_id, job_id, node_id")
    for k in ("nodes", "edges", "events", "jobs", "notes"):
        _expect(isinstance(doc[k], list), f"'{k}' must be a list")
    ids = set()
    for n in doc["nodes"]:
        _expect(isinstance(n, Mapping) and _is_str(n.get("id")), "a node needs an id")
        _expect(n["id"] not in ids, f"duplicate node id {n['id']}")
        ids.add(n["id"])
        _expect(n.get("kind") in NODE_KINDS, f"node {n['id']}: 'kind' must be one of {NODE_KINDS}")
        _expect(isinstance(n.get("label"), str), f"node {n['id']}: 'label' must be a string")
        _expect(_is_int(n.get("generation")) and n["generation"] >= 0, f"node {n['id']}: 'generation' must be an int >= 0")
        if n["kind"] == "take":
            _expect(_is_str(n.get("job_id")), f"take {n['id']}: needs job_id")
            _expect(n.get("take_kind") in TAKE_KINDS, f"take {n['id']}: 'take_kind' must be one of {TAKE_KINDS}")
            _expect(n.get("decision") in DECISIONS, f"take {n['id']}: 'decision' must be one of {DECISIONS}")
            _check_reason(f"take {n['id']}", n.get("reason"), n["decision"] == "discard")
            _expect(n["decision"] == "discard" or n.get("reason") is None, f"take {n['id']}: only a discarded take has a reason")
            qa = n.get("qa")
            _expect(isinstance(qa, Mapping) and {"verdict", "failed", "not_measured", "text"} <= set(qa), f"take {n['id']}: 'qa' is malformed")
            _expect(isinstance(n.get("params"), Mapping), f"take {n['id']}: 'params' must be an object")
            _expect(isinstance(n.get("on_kept_path"), bool), f"take {n['id']}: 'on_kept_path' must be a boolean")
        elif n["kind"] == "intent":
            _expect(_is_str(n.get("job_id")) and isinstance(n.get("params"), Mapping), f"intent {n['id']}: needs job_id and params")
        else:
            _expect(_is_str(n.get("library_track_id")), f"source {n['id']}: needs library_track_id")
    gen = {n["id"]: n["generation"] for n in doc["nodes"]}
    edge_ids = set()
    for e in doc["edges"]:
        _expect(isinstance(e, Mapping) and _is_str(e.get("id")), "an edge needs an id")
        _expect(e["id"] not in edge_ids, f"duplicate edge id {e['id']}")
        edge_ids.add(e["id"])
        _expect(e.get("from") in ids and e.get("to") in ids, f"edge {e['id']}: from and to must be nodes")
        _expect(e.get("kind") in EDGE_KINDS, f"edge {e['id']}: 'kind' must be one of {EDGE_KINDS}")
        _expect(_is_str(e.get("op")) and _is_str(e.get("job_id")) and _is_str(e.get("group")), f"edge {e['id']}: needs op, job_id, group")
        _expect(gen[e["to"]] > gen[e["from"]], f"edge {e['id']}: generation must grow along an edge")
        region = e.get("region")
        _expect(region is None or (isinstance(region, Mapping) and {"t_start", "t_end"} <= set(region)), f"edge {e['id']}: 'region' is malformed")
        _expect(isinstance(e.get("rejected"), bool), f"edge {e['id']}: 'rejected' must be a boolean")
        _check_reason(f"edge {e['id']}", e.get("reason"), e["rejected"])
    takes = {n["id"] for n in doc["nodes"] if n["kind"] == "take"}
    for ev in doc["events"]:
        _expect(isinstance(ev, Mapping) and _is_str(ev.get("id")), "an event needs an id")
        _expect(ev.get("kind") in EVENT_KINDS, f"event {ev['id']}: 'kind' must be one of {EVENT_KINDS}")
        _expect(ev.get("take_id") in takes, f"event {ev['id']}: take_id must be a take of the graph")
        _expect(isinstance(ev.get("over_take_ids"), list), f"event {ev['id']}: 'over_take_ids' must be a list")
        _expect(ev.get("at") is None or isinstance(ev["at"], str), f"event {ev['id']}: 'at' must be a string or null")
        _check_reason(f"event {ev['id']}", ev.get("reason"), ev["kind"] == "discard")
        _expect(ev.get("decided_by") == {"kind": "person"}, f"event {ev['id']}: only a person decides")
    stats = doc["stats"]
    _expect(isinstance(stats, Mapping) and all(_is_int(stats.get(k)) for k in
            ("jobs", "takes", "kept", "discarded", "pending", "edits", "branches", "max_generation")), "'stats' is malformed")


# -- layout and the static page -----------------------------------------------------------------

COL_W, ROW_H, NODE_W, NODE_H, PAD = 230, 96, 190, 62, 24


def layout(doc: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    """Node id -> (x, y) of the node's top-left corner: a column per generation, leaves in depth-first
    order down the rows, a parent centred on its children. The UI helper
    (`utils/musicLineage.ts` `layoutLineage`) does the same, so both views agree."""
    children: Dict[str, List[str]] = {}
    has_parent = set()
    order = {n["id"]: i for i, n in enumerate(doc["nodes"])}
    for e in doc["edges"]:
        children.setdefault(e["from"], []).append(e["to"])
        has_parent.add(e["to"])
    for kids in children.values():
        kids.sort(key=lambda k: order[k])
    rows: Dict[str, float] = {}
    next_row = [0]

    def place(nid: str) -> float:
        if nid in rows:
            return rows[nid]
        kids = children.get(nid, [])
        if not kids:
            rows[nid] = float(next_row[0])
            next_row[0] += 1
        else:
            rows[nid] = -1.0
            ys = [place(k) for k in kids]
            rows[nid] = sum(ys) / len(ys)
        return rows[nid]

    for n in doc["nodes"]:
        if n["id"] not in has_parent:
            place(n["id"])
    for n in doc["nodes"]:
        place(n["id"])
    gen = {n["id"]: n["generation"] for n in doc["nodes"]}
    return {nid: (PAD + gen[nid] * COL_W, PAD + rows[nid] * ROW_H) for nid in rows}


def _region_text(region: Optional[Mapping[str, Any]]) -> str:
    if not region:
        return ""
    try:
        return f"{float(region['t_start']):.1f}-{float(region['t_end']):.1f} s"
    except (KeyError, TypeError, ValueError):
        return ""


def _edge_label(e: Mapping[str, Any]) -> str:
    parts = [str(e["op"]).replace("_", " ")]
    r = _region_text(e.get("region"))
    if r:
        parts.append(r)
    return " ".join(parts)


def _reason_text(reason: Optional[Mapping[str, Any]]) -> str:
    if not reason:
        return ""
    return " / ".join(str(x) for x in (reason.get("text"), reason.get("class")) if x)


def _safe_requester_label(rb: Any) -> str:
    """Shareable requester summary for `lineage.html`: never includes conversation_id,
    conversation_name, or turn_id."""
    if isinstance(rb, str):
        return "asked by you" if rb == "person" else f"asked by {rb}"
    if not isinstance(rb, Mapping):
        return ""
    if rb.get("kind") == "agent":
        who = rb.get("agent_name") or rb.get("agent_id") or "agent"
        return f"asked by {who}"
    return "asked by you"


def _format_safe_request_value(val: Any) -> str:
    if isinstance(val, Mapping):
        return ", ".join(f"{k}: {_format_safe_request_value(v)}" for k, v in val.items() if v is not None)
    if isinstance(val, list):
        return "; ".join(_format_safe_request_value(v) for v in val if v is not None)
    return str(val)


def _intent_html_items(doc: Mapping[str, Any], esc: Callable[[Any], str]) -> List[str]:
    """Shareable intent summary rows for `lineage.html`: shape-only lyrics, resolved seed,
    adapters with strengths, and redacted requester/request metadata (no lyrics_text,
    conversation_id, conversation_name, turn_id, message_id, or request.text)."""
    jobs_by_id = {j.get("id"): j for j in doc.get("jobs") or [] if isinstance(j, Mapping) and j.get("id")}
    items: List[str] = []
    for n in doc.get("nodes") or []:
        if not isinstance(n, Mapping) or n.get("kind") != "intent":
            continue
        params = n.get("params") if isinstance(n.get("params"), Mapping) else {}
        job = jobs_by_id.get(n.get("job_id")) or {}
        parts: List[str] = []
        style = params.get("style") or params.get("caption")
        if style:
            parts.append(str(style))
        shape = _format_lyrics_shape(params.get("lyrics"))
        if shape:
            parts.append(f"lyrics: {shape}")
        if params.get("seed") is not None:
            parts.append(f"seed: {params['seed']}")
        adapters = [_format_adapter(a) for a in params.get("adapters") or []]
        adapters = [a for a in adapters if a]
        if adapters:
            parts.append("adapters: " + ", ".join(adapters))
        who = _safe_requester_label(n.get("requested_by") or job.get("requested_by"))
        if who:
            parts.append(who)
        req = n.get("request") if isinstance(n.get("request"), Mapping) else job.get("request")
        if isinstance(req, Mapping):
            if req.get("title"):
                parts.append(f"title: {req['title']}")
            if req.get("asked") is not None:
                parts.append(f"asked: {_format_safe_request_value(req['asked'])}")
            if req.get("changes") is not None:
                parts.append(f"changes: {_format_safe_request_value(req['changes'])}")
        items.append(f"<li><b>{esc(n.get('label') or 'intent')}</b>"
                     + (f" — {esc(' · '.join(parts))}" if parts else "") + "</li>")
    return items


def render_html(doc: Mapping[str, Any]) -> str:
    """A self-contained static page for the graph: inline SVG, inline CSS, no script and no network.
    Everything from the document is escaped. Keeps shape-only lyrics and omits private chat ids and
    personal message text."""
    pos = layout(doc)
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    width = max((x for x, _ in pos.values()), default=0) + NODE_W + PAD
    height = max((y for _, y in pos.values()), default=0) + NODE_H + PAD + 20
    svg: List[str] = []
    for e in doc["edges"]:
        x1, y1 = pos[e["from"]][0] + NODE_W, pos[e["from"]][1] + NODE_H / 2
        x2, y2 = pos[e["to"]][0], pos[e["to"]][1] + NODE_H / 2
        mid = (x1 + x2) / 2
        cls = "edge rejected" if e["rejected"] else "edge"
        svg.append(f'<path class="{cls}" d="M{x1:.1f},{y1:.1f} C{mid:.1f},{y1:.1f} {mid:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"/>')
        label = _edge_label(e)
        if e["rejected"] and _reason_text(e["reason"]):
            label += " - rejected: " + _reason_text(e["reason"])
        svg.append(f'<text class="elabel{" rejected" if e["rejected"] else ""}" x="{mid:.1f}" y="{(y1 + y2) / 2 - 6:.1f}" '
                   f'text-anchor="middle">{esc(_clip(label, 60))}</text>')
    for n in doc["nodes"]:
        x, y = pos[n["id"]]
        decision = n.get("decision", "none")
        cls = f"node {n['kind']} {decision}" + (" kept-path" if n.get("on_kept_path") else "")
        lines = [_clip(n["label"], 26)]
        if n["kind"] == "take":
            lines.append(" · ".join(x for x in (n.get("engine"), f"variant {n['variant']}" if n.get("variant") else None) if x))
            lines.append(f"{n['qa']['text']} · {decision}")
        elif n["kind"] == "intent":
            lines.append(str(n.get("engine") or ""))
            lines.append("intent · " + str(n.get("job_state") or ""))
        else:
            lines.append("source file")
        tip = _reason_text(n.get("reason")) if n["kind"] == "take" else ""
        svg.append(f'<g class="{esc(cls)}"><title>{esc(n["id"] + (" - " + tip if tip else ""))}</title>'
                   f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_W}" height="{NODE_H}" rx="6"/>'
                   + "".join(f'<text x="{x + 8:.1f}" y="{y + 18 + i * 16:.1f}">{esc(_clip(t, 30))}</text>' for i, t in enumerate(lines))
                   + "</g>")
    intent_rows = _intent_html_items(doc, esc)
    rows = []
    for ev in doc["events"]:
        why = _reason_text(ev["reason"])
        over = f", over {len(ev['over_take_ids'])} other take(s)" if ev["kind"] == "keep" and ev["over_take_ids"] else ""
        rows.append(f"<li><b>{esc(ev['kind'])}</b> {esc(ev['take_id'])}{esc(over)}"
                    + (f" - {esc(why)}" if why else "") + f' <span class="muted">{esc(ev["at"] or "")}</span></li>')
    s = doc["stats"]
    intent_html = f"<h2>Intent</h2>\n<ul>{''.join(intent_rows)}</ul>\n" if intent_rows else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Song lineage</title>
<style>
:root{{--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#8a8a8a;--card:#f4f4f4;--keep:#d6f0d6;--drop:#e6e6e6;--bad:#b3261e;--edge:#9a9a9a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16171a;--fg:#e8e8e8;--muted:#9a9a9a;--line:#6a6a6a;--card:#23252a;--keep:#1f3d28;--drop:#2c2c2c;--bad:#ff8a80;--edge:#777}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}}
.muted{{color:var(--muted)}} svg{{max-width:100%;height:auto;overflow:visible}}
.edge{{fill:none;stroke:var(--edge);stroke-width:1.5}} .edge.rejected{{stroke:var(--bad);stroke-dasharray:5 4}}
.elabel{{font-size:11px;fill:var(--muted)}} .elabel.rejected{{fill:var(--bad)}}
.node rect{{fill:var(--card);stroke:var(--line)}} .node.keep rect{{fill:var(--keep)}} .node.discard rect{{fill:var(--drop);stroke-dasharray:4 3}}
.node.pending rect{{stroke-dasharray:2 3}} .node.kept-path rect{{stroke-width:2.5}} .node text{{font-size:12px;fill:var(--fg)}}
</style></head><body>
<h1>Song lineage</h1>
<p class="muted">{esc(s['jobs'])} job(s), {esc(s['takes'])} take(s): {esc(s['kept'])} kept, {esc(s['discarded'])} discarded, {esc(s['pending'])} pending. Generated {esc(doc['generated_at'])}. Dashed red edges lead to a discarded take and say why.</p>
<svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" role="img" aria-label="Lineage graph">
{chr(10).join(svg)}
</svg>
{intent_html}<h2>Selections</h2>
<ul>{''.join(rows) or '<li class="muted">No Keep or Discard yet.</li>'}</ul>
<p class="muted">{esc(' '.join(doc['notes']))}</p>
</body></html>
"""


def _redact_requested_by(rb: Any) -> Any:
    """Return a shareable copy of `requested_by`, stripping private chat ids, names, and turn ids."""
    if not isinstance(rb, Mapping):
        return rb
    out = copy.deepcopy(dict(rb))
    for key in ("conversation_id", "conversation_name", "turn_id"):
        out.pop(key, None)
    if out.get("kind") == "agent":
        out["label"] = _safe_requester_label(out)
    return out


def _redact_request(req: Any) -> Optional[Dict[str, Any]]:
    """Return a shareable copy of `request` without `message_id` or `text`, or None if empty."""
    if not isinstance(req, Mapping):
        return None
    out = copy.deepcopy(dict(req))
    for key in ("message_id", "text"):
        out.pop(key, None)
    return out or None


def redact_lineage_for_export(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of `doc` safe for on-disk `lineage.json` export alongside `lineage.html`:
    removes `params.lyrics_text`, `requested_by.conversation_id` / `conversation_name` / `turn_id`
    (and removes the conversation suffix from agent `requested_by.label`), and `request.message_id` /
    `request.text`, while keeping shape-only lyrics and non-sensitive provenance fields."""
    out = copy.deepcopy(dict(doc))
    for node in out.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        params = node.get("params")
        if isinstance(params, dict):
            params.pop("lyrics_text", None)
        if "requested_by" in node:
            node["requested_by"] = _redact_requested_by(node["requested_by"])
        if "request" in node:
            redacted_req = _redact_request(node["request"])
            if redacted_req is None:
                node.pop("request", None)
            else:
                node["request"] = redacted_req
    for job in out.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        if "requested_by" in job:
            job["requested_by"] = _redact_requested_by(job["requested_by"])
        if "request" in job:
            redacted_req = _redact_request(job["request"])
            if redacted_req is None:
                job.pop("request", None)
            else:
                job["request"] = redacted_req
    return out


def write_lineage_files(doc: Mapping[str, Any], folder: Path) -> Dict[str, str]:
    """Write shareable `lineage.json` and `lineage.html` for `doc` into `folder` (created if missing),
    each in one step (temp file, then replace). Both files redact private fields (`lyrics_text`,
    `conversation_id`, `conversation_name`, `turn_id`, `message_id`, `request.text`) without mutating
    `doc`. Answers `{json, html}` paths. `OSError` is the caller's."""
    validate_lineage(doc)
    exported = redact_lineage_for_export(doc)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, text in ((LINEAGE_JSON, json.dumps(exported, ensure_ascii=False, indent=2, sort_keys=False) + "\n"),
                       (LINEAGE_HTML, render_html(exported))):
        target = folder / name
        tmp = folder / f".{name}.tmp"
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, target)
        out[name.rsplit(".", 1)[1]] = str(target)
    return out
