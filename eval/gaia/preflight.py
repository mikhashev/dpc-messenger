"""Everything an unattended GAIA night depends on, checked before it starts.

A night that dies in its first minute on a renamed alias, a stale token or a
missing binary is a night lost, and the campaign used to find each of those out
one run at a time. These checks download nothing and load no model: the token
is tested with an access check, the binary is asked for `--version`, and the
GGUF is read for its chat template and hashed (once; the digest is cached).

Each check is (status, name, detail), status one of OK / WARN / FAIL. Only FAIL
stops a campaign. The card being full is a WARN, because the campaign waits for
it, bounded, and says who holds it.
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from _harness import benchmark_tools, provenance  # noqa: E402

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
    found = gguf_effort_dictionary(entry.get("gguf_path") or "")
    words, default = (found if found else (TEMPLATE_EFFORTS, None))
    source = "read from the GGUF template" if found else "the provider's fallback table"
    return words, f"{source}; default {default or 'unset'}; folds {FLEET_TO_TEMPLATE}"


def run_checks(alias: str, efforts: List[str], results_dir: Path,
               gpu_needed_mib: int) -> List[Check]:
    import run_gaia_eval as gaia
    from campaign import gpu_free_mib, gpu_holder_candidates

    checks: List[Check] = []

    def add(status, name, detail):
        checks.append((status, name, detail))

    # 1. the alias, before anything else — every other model check reads it
    try:
        entry, known, src = _alias_entry(alias)
    except Exception as exc:
        entry, known, src = None, [], Path.home() / ".dpc" / "providers.json"
        add("FAIL", "alias", f"cannot read {src}: {type(exc).__name__}: {exc}")
    if entry is None:
        if known:
            add("FAIL", "alias", f"no provider aliased {alias!r} in {src}; "
                                 f"available: {', '.join(known)}")
    elif entry.get("type") != "llamacpp_server":
        add("FAIL", "alias", f"{alias!r} is type {entry.get('type')!r}; the campaign is "
                             f"built for a local llamacpp_server alias")
    else:
        add("OK", "alias", f"{alias!r} type=llamacpp_server; `model` label "
                           f"{entry.get('model')!r} is free text, the model is the GGUF below")

    if entry is not None and entry.get("type") == "llamacpp_server":
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

    # 7. no readable answers, or every run refuses in its first second
    try:
        found = gaia.reachable_gold(gaia.hub_caches_in_effect(), results_dir,
                                    archives=[gaia.GOLD_ARCHIVE])
        add("FAIL" if found else "OK", "reachable gold",
            ("readable copies: " + "; ".join(str(f) for f in found[:4])) if found
            else "no readable copy of the answers on this machine")
    except Exception as exc:
        add("FAIL", "reachable gold", f"{type(exc).__name__}: {exc}")

    # 8. the card — a wait, not a refusal
    free = gpu_free_mib()
    if free is None:
        add("OK", "vram", "no nvidia-smi: the VRAM gate does not apply")
    elif free >= gpu_needed_mib:
        add("OK", "vram", f"{free} MiB free, {gpu_needed_mib} needed")
    else:
        holders = ", ".join(gpu_holder_candidates()) or "no compute process named it"
        add("WARN", "vram", f"{free} MiB free, {gpu_needed_mib} needed (held by: {holders}). "
                            f"Stop the DPC service before the campaign: it holds the model "
                            f"through its own llama-server child. The campaign waits, "
                            f"bounded by --wait-budget-minutes, then exits 4")

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
    return checks


def all_ok(checks: List[Check]) -> bool:
    return not any(status == "FAIL" for status, _, _ in checks)


def print_checks(checks: List[Check]) -> None:
    print("=== preflight ===", flush=True)
    for status, name, detail in checks:
        print(f"  {status:4} {name:15} {detail}", flush=True)
