"""Everything an unattended GAIA night depends on, checked before it starts.

A night that dies in its first minute on a renamed alias, a stale token or a
missing binary is a night lost, and the campaign used to find each of those out
one run at a time. These checks download nothing and load no model: the token
is tested with an access check, the binary is asked for `--version`, and the
GGUF is read for its chat template and hashed (once; the digest is cached). A
`strata` alias is read through its base config instead (`engine_checks`).

Each check is (status, name, detail), status one of OK / WARN / FAIL / SKIP. Only
FAIL stops a campaign. The card being full is a WARN, because the campaign waits
for it, bounded, and says who holds it. SKIP marks a check the operator turned
off on purpose (`--no-memory`) — not a pass, not a refusal, a recorded choice.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from _harness import benchmark_tools, local_engine, provenance  # noqa: E402

Check = Tuple[str, str, str]


def _alias_entry(alias: str):
    src = Path.home() / ".dpc" / "providers.json"
    raw = json.loads(src.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("providers", [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    match = [r for r in rows if r.get("alias") == alias]
    return (dict(match[0]) if match else None), sorted(str(r.get("alias")) for r in rows), src


def _effort_words(entry) -> Tuple[tuple, str]:
    from dpc_client_core.managers.llama_server_supervisor import gguf_effort_dictionary
    from dpc_client_core.providers.llamacpp_server_provider import (
        FLEET_TO_TEMPLATE, TEMPLATE_EFFORTS)
    if entry.get("type") == "strata":
        found = local_engine.strata_effort_words(entry)
        where = "the tokenizer template the Strata base config names"
    else:
        found = gguf_effort_dictionary(entry.get("gguf_path") or "")
        where = "the GGUF template"
    words, default = (found if found else (TEMPLATE_EFFORTS, None))
    source = f"read from {where}" if found else "the provider's fallback table"
    return words, f"{source}; default {default or 'unset'}; folds {FLEET_TO_TEMPLATE}"


def memory_check(no_memory: bool) -> Check:
    """Whether the agent's embedding model is cached — its own check, callable
    on its own so a test can exercise it without the token, tools and GPU
    checks around it in `run_checks`.
    """
    from dpc_client_core.dpc_agent.memory import DEFAULT_EMBEDDING_MODEL
    from dpc_client_core.providers.model_sizes import is_model_cached, model_size_bytes
    cached = is_model_cached(DEFAULT_EMBEDDING_MODEL)
    if no_memory:
        return ("SKIP", "memory",
                f"--no-memory: agent memory search disabled for this run "
                f"(embedding model {DEFAULT_EMBEDDING_MODEL!r} "
                f"{'is' if cached else 'is not'} cached)")
    if cached:
        return ("OK", "memory",
                f"embedding model {DEFAULT_EMBEDDING_MODEL!r} is cached; "
                f"memory_search and Active Recall can load it")
    size, source = model_size_bytes(DEFAULT_EMBEDDING_MODEL)
    size_detail = f"{size / 1e9:.2f} GB, {source}" if size else "size unknown"
    return ("FAIL",
            "memory",
            f"embedding model {DEFAULT_EMBEDDING_MODEL!r} is not cached ({size_detail}); "
            f"memory_search would answer 'unavailable' and Active Recall would skip "
            f"every turn — a run like that is not comparable with one that had memory. "
            f"Fetch it once: start the DPC app and confirm the download when asked, or "
            f"run `DISABLE_SAFETENSORS_CONVERSION=true uv run python -c "
            f"\"from sentence_transformers import SentenceTransformer as S; "
            f"S('{DEFAULT_EMBEDDING_MODEL}')\"`. Or pass --no-memory to run without "
            f"it on purpose — the run then records that choice instead of hiding it")


def engine_checks(alias: str, entry, known: List[str], src: Path, efforts: List[str],
                  results_dir: Path) -> List[Check]:
    """The alias and the engine it would start: checks 1-4 of `run_checks`, on
    their own so a test can run them without the token and the card.

    The campaign runs on a supervised local engine — `llamacpp_server` or
    `strata` (`local_engine.supervised_types`, read from the client's own type
    sets). Each check reads the facts of the engine the alias names: the pinned
    binary and the GGUF for llama-server; for Strata, the install, the base
    config and the files of the start config DPC would write.
    """
    checks: List[Check] = []

    def add(status, name, detail):
        checks.append((status, name, detail))

    # 1. the alias, before anything else — every other model check reads it
    supervised = sorted(local_engine.supervised_types())
    ptype = entry.get("type") if entry is not None else None
    if entry is None:
        if known:
            add("FAIL", "alias", f"no provider aliased {alias!r} in {src}; "
                                 f"available: {', '.join(known)}")
        return checks
    if ptype not in supervised:
        add("FAIL", "alias", f"{alias!r} is type {ptype!r}; the campaign is built for a "
                             f"supervised local engine: {', '.join(supervised)}")
        return checks
    what = "the GGUF below" if ptype == "llamacpp_server" else "the start config's files below"
    add("OK", "alias", f"{alias!r} type={ptype} (engine {local_engine.engine_of(ptype)}); "
                       f"`model` label {entry.get('model')!r} is free text, the model is {what}")

    if ptype == "llamacpp_server":
        # 2. the binary the provider would start
        info = provenance.llama_server_binary(entry)
        if info.get("binary"):
            which = ("the pin" if info.get("binary_is_the_pin")
                     else "configured binary_path, not the pin")
            add("OK", "llama-server",
                f"{info['binary']} ({which} {info['pinned_tag']}); "
                f"version: {' | '.join(info.get('version') or ['?'])}")
        else:
            add("FAIL", "llama-server",
                f"{info.get('error')} (pin {info.get('pinned_tag')} at "
                f"{info.get('pinned_install_root')})")
        # 3. the model files, by digest
        for key in ("gguf_path", "mmproj"):
            if not entry.get(key):
                if key == "gguf_path":
                    add("FAIL", "gguf", "the alias names no gguf_path")
                continue
            rec = provenance.file_sha256(Path(entry[key]), results_dir)
            label = key.replace("_path", "")
            if rec.get("error"):
                add("FAIL", label, f"{rec['path']}: {rec['error']}")
            else:
                cached = "cached digest" if rec.get("sha256_from_cache") else "hashed now, cached"
                add("OK", label, f"{rec['path']} {rec['size'] / 1e9:.2f} GB "
                                 f"sha256 {rec['sha256'][:16]}… ({cached})")
    else:
        # 2. the launcher the supervisor would start; nothing is started
        facts = local_engine.strata_start_facts(entry)
        problems = list(facts["config_errors"])
        for key in ("python", "server_script"):
            if key in facts and not facts.get(f"{key}_exists"):
                problems.append(f"{facts[key]} does not exist")
        if facts.get("start_error"):
            problems.append(facts["start_error"])
        if problems:
            add("FAIL", "strata", "; ".join(problems))
        else:
            add("OK", "strata",
                f"launcher {facts['server_script']} under {facts['python']}; base config "
                f"{facts['base_config']}; tested version {facts.get('tested_version')!r} "
                f"({facts.get('tested_version_date')}) — the engine's own version is read "
                f"only at start, where a mismatch warns")
        # 3. the model files of the start config. Not hashed: a Strata model is
        # shards, packs and folders, and one file's digest would not name it.
        for key, value in facts.get("model_files", {}).items():
            rec = local_engine.path_record(value)
            if rec.get("error"):
                add("FAIL", key, f"{rec['path']}: {rec['error']}")
            else:
                size = f" {rec['size'] / 1e9:.2f} GB" if "size" in rec else ""
                add("OK", key, f"{rec['path']} ({rec['kind']}{size}, not hashed)")
    # 4. the queue's effort words against the template that will receive them
    try:
        words, how = _effort_words(entry)
        from dpc_client_core.providers.llamacpp_server_provider import FLEET_TO_TEMPLATE
        bad = [e for e in efforts
               if e not in words and FLEET_TO_TEMPLATE.get(e) not in words]
        add("FAIL" if bad else "OK", "effort",
            (f"refused by the template: {bad}; " if bad else "")
            + f"queue {sorted(set(efforts))} vs template {list(words)} ({how})")
    except Exception as exc:
        add("FAIL", "effort", f"cannot read the template: {type(exc).__name__}: {exc}")
    add("OK", "sampling pins",
        "each run copies the alias and overwrites `temperature` and "
        "`reasoning_effort`; the provider reads both from that entry and the agent "
        f"sends no per-call override. Alias as stored: temperature="
        f"{entry.get('temperature')} effort={entry.get('reasoning_effort')} "
        f"reasoning_budget_tokens={entry.get('reasoning_budget_tokens')}")
    return checks


def card_checks(entry, gpu_needed_mib: int, free, holder_candidates,
                engines_on_the_card) -> List[Check]:
    """The card: VRAM short is a wait (WARN), the other engine holding it is a FAIL.

    ADR-044 point 4: the two engines cannot hold the card together and DPC does
    not swap them, so a campaign started beside the other engine would wait out
    its budget for a person, or — with room to spare — start beside it and page
    through WDDM. The in-process guard (`CardHeldError`) cannot see the DPC
    service's child from here, so the card is asked by process name; a card
    that did not answer is a WARN, not a free card. A Strata alias has no VRAM
    floor (`local_engine.vram_floor_mib`): its reading is a WARN with context,
    and the campaign does not wait on it."""
    checks: List[Check] = []
    ptype = entry.get("type") if entry is not None else None
    engines = engines_on_the_card() if local_engine.engine_of(ptype) else None
    if isinstance(engines, local_engine.CardUnavailable):
        checks.append(("WARN", "card holder",
                       f"card holder check unavailable: {engines.error} — the card is not "
                       f"known to be free of the other engine"))
    conflicts = local_engine.card_conflicts(ptype, engines)
    if conflicts:
        checks.append(("FAIL", "card holder",
                       local_engine.card_held_message(ptype, entry.get("alias"), conflicts)))
    if free is None:
        checks.append(("OK", "vram", "no nvidia-smi: the VRAM gate does not apply"))
        return checks
    if local_engine.vram_floor_mib(ptype) is None:
        checks.append(("WARN", "vram", f"{free} MiB free; {local_engine.STRATA_VRAM_NOTE}"))
        return checks
    if free >= gpu_needed_mib:
        checks.append(("OK", "vram", f"{free} MiB free, {gpu_needed_mib} needed"))
    else:
        holders = ", ".join(holder_candidates()) or "no compute process named it"
        checks.append(("WARN", "vram",
                       f"{free} MiB free, {gpu_needed_mib} needed (held by: {holders}). "
                       f"Stop the DPC service before the campaign: it holds the model "
                       f"through its own local engine child (llama-server or Strata). The "
                       f"campaign waits, bounded by --wait-budget-minutes, then exits 4"))
    return checks


def run_checks(alias: str, efforts: List[str], results_dir: Path,
               gpu_needed_mib: int, no_memory: bool = False) -> List[Check]:
    import run_gaia_eval as gaia
    from campaign import gpu_free_mib, gpu_holder_candidates

    checks: List[Check] = []

    def add(status, name, detail):
        checks.append((status, name, detail))

    try:
        entry, known, src = _alias_entry(alias)
    except Exception as exc:
        entry, known, src = None, [], Path.home() / ".dpc" / "providers.json"
        add("FAIL", "alias", f"cannot read {src}: {type(exc).__name__}: {exc}")
    checks.extend(engine_checks(alias, entry, known, src, efforts, results_dir))

    # 5. the token, checked against the gated repo without downloading it
    token, source = gaia.resolve_hf_token()
    if not token:
        add("FAIL", "hf token", "none: export HF_TOKEN or run `hf auth login` once")
    else:
        try:
            from huggingface_hub import HfApi
            HfApi().auth_check(gaia.REPO, repo_type="dataset", token=token)
            add("OK", "hf token", f"from {source}; access to {gaia.REPO} confirmed")
        except Exception as exc:
            add("FAIL", "hf token", f"from {source}; access check on {gaia.REPO} failed: "
                                    f"{type(exc).__name__}: {str(exc)[:160]}")

    # 6. the runner's own imports
    try:
        import pyarrow  # noqa: F401
        import huggingface_hub  # noqa: F401
        add("OK", "imports", "pyarrow and huggingface_hub importable")
    except Exception as exc:
        add("FAIL", "imports", f"{type(exc).__name__}: {exc} — start under "
                               f"`uv run --with pyarrow`")
    if shutil.which("uv") is None:
        add("FAIL", "uv", "`uv` is not on PATH; every run is launched through it")

    # 7. no readable answers, or every run refuses in its first second. Decoys a
    # killed run left are removed first — only files carrying the harness note.
    try:
        stale = gaia.remove_stale_decoys(gaia.hub_caches_in_effect(), results_dir)
        add("OK", "stale decoys", f"removed {len(stale)}" + (
            ": " + "; ".join(str(p) for p in stale[:4]) + (" …" if len(stale) > 4 else "")
            if stale else ""))
    except Exception as exc:
        add("WARN", "stale decoys", f"{type(exc).__name__}: {exc}")
    try:
        found = gaia.reachable_gold(gaia.hub_caches_in_effect(), results_dir,
                                    archives=[gaia.GOLD_ARCHIVE])
        add("FAIL" if found else "OK", "reachable gold",
            ("readable copies: " + "; ".join(str(f) for f in found[:4])) if found
            else "no readable copy of the answers on this machine")
    except Exception as exc:
        add("FAIL", "reachable gold", f"{type(exc).__name__}: {exc}")

    # 8. the card — a wait, not a refusal, unless the other engine holds it
    checks.extend(card_checks(entry, gpu_needed_mib, gpu_free_mib(),
                              gpu_holder_candidates, local_engine.engines_on_the_card))

    # 9. somewhere to write
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        probe = results_dir / f".write-probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("OK", "results dir", f"{results_dir} writable")
    except Exception as exc:
        add("FAIL", "results dir", f"{results_dir}: {type(exc).__name__}: {exc}")

    # 10. the tool set the agent will hold
    try:
        tools = benchmark_tools.tool_map()
        on = sorted(n for n, v in tools.items() if v)
        off = sorted(n for n, v in tools.items() if not v)
        add("OK", "tools", f"{len(on)} on of {len(tools)} registered: {', '.join(on)}")
        add("OK", "tools off", ", ".join(off))
        missing = sorted(benchmark_tools.BENCHMARK_TOOLS - set(tools))
        if missing:
            add("WARN", "tools", f"allowed but not registered: {', '.join(missing)}")
    except Exception as exc:
        add("FAIL", "tools", f"{type(exc).__name__}: {exc}")

    # 11. the answer-key policy wraps every web tool the agent will hold
    try:
        from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
        from _harness import answer_key_policy
        with tempfile.TemporaryDirectory(prefix="dpc-policy-probe-") as probe_root:
            registry = ToolRegistry(agent_root=Path(probe_root))
            wrapped = set(answer_key_policy.AnswerKeyPolicy().install(registry))
        web = {n for n in benchmark_tools.BENCHMARK_TOOLS
               if n in answer_key_policy.WRAPPED_TOOLS and n in registry._entries}
        info = answer_key_policy.describe()
        add("FAIL" if web - wrapped else "OK", "answer keys",
            (f"not wrapped: {sorted(web - wrapped)}; " if web - wrapped else "")
            + f"policy {info['version']} sha256 {info['sha256'][:16]}… on {len(wrapped)} "
              f"tool(s); run_shell is refused on its command text only")
    except Exception as exc:
        add("FAIL", "answer keys", f"{type(exc).__name__}: {exc}")

    # 12. the agent's embedding model — memory_search and Active Recall both need
    # it, and it is fetched only on explicit consent (commit 7a19bad3): a GAIA
    # night that started without it would run agent memory silently absent, and
    # the run comparing it against earlier nights would not know why. Refused
    # here, not discovered three hours in.
    try:
        checks.append(memory_check(no_memory))
    except Exception as exc:
        add("FAIL", "memory", f"{type(exc).__name__}: {exc}")
    return checks


def all_ok(checks: List[Check]) -> bool:
    return not any(status == "FAIL" for status, _, _ in checks)


def print_checks(checks: List[Check]) -> None:
    print("=== preflight ===", flush=True)
    for status, name, detail in checks:
        print(f"  {status:4} {name:15} {detail}", flush=True)
