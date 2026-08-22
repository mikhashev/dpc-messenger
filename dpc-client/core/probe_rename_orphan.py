"""The live scenario for tier 1, run against the real classes and a real child.

Neither review executed this: both stubbed the spawn, so what they proved is that
the code path is reachable, not that a process survives it. This starts an actual
`llama-server`, renames its alias the way a providers save does, runs the shutdown
sweep, and then asks the operating system whether the process is still there.

Two safeguards, because Mike's fleet is on this box:
  * a 1.8 GiB Ollama blob, not the 14 GiB production model;
  * `--device none`, so the child never touches the card.
The orphan does not depend on either — `retire_absent` drops the handle whatever
the alias is serving — so the mechanism is the same and the fleet is untouched.

Run it once BEFORE the fix (the negative control: the orphan must be visible, or
the instrument is blind and a green run after the fix proves nothing) and once
after.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


MODEL = Path.home() / ".ollama" / "models" / "blobs" / (
    "sha256-b1f2d3258054084f6f446b0d3d8d4ece0cb8c2959c5cde987fc780fc6750c5fc"
)

ALIAS_BEFORE = "probe-rename-before"
ALIAS_AFTER = "probe-rename-after"


def alive(pid: int) -> bool:
    """Whether the OS still has that pid, asked of the OS and not of our records."""
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True,
    ).stdout
    return str(pid) in out


def llama_pids() -> set:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/NH"],
        capture_output=True, text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().startswith("llama-server"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


async def main() -> int:
    from dpc_client_core.providers import llamacpp_server_provider as prov

    if not MODEL.exists():
        print(f"FAIL  probe model missing: {MODEL}")
        return 2

    before = llama_pids()
    print(f"llama-server pids already running (left alone): {sorted(before) or 'none'}")

    config = {
        "gguf_path": str(MODEL),
        "n_ctx": 4096,
        "spec_type": "none",          # a plain model has no MTP head
        "n_gpu_layers": 0,
        "extra_args": ["--device", "none"],
        "start_timeout_s": 180.0,
    }

    print(f"\n1. starting a real child under '{ALIAS_BEFORE}' ...")
    provider = prov.LlamaServerProvider(ALIAS_BEFORE, dict(config))
    await provider.supervisor.ensure_running()
    pid = provider.supervisor._proc.pid
    print(f"   child pid {pid} on :{provider.supervisor.port}, alive={alive(pid)}")
    assert alive(pid), "the probe never started; nothing below is meaningful"

    # The window only exists while the child is busy. The first run of this probe
    # renamed an IDLE child and found no orphan: the drain had nothing to wait for,
    # finished at once and stopped the process properly. A green run in that shape
    # proves nothing, which is what the negative control is for.
    print(f"\n1b. putting the child to work THROUGH THE PROVIDER")
    # The second run of this probe hit the child's HTTP port directly with httpx.
    # The child was busy and there was still no orphan, because the supervisor was
    # not: `_in_flight` is incremented by `call_slot()`, which only the provider's
    # own methods take. A request that goes around the provider leaves the drain
    # with nothing to wait for. Busy child != busy supervisor, and it was the
    # negative control that said so — twice.
    async def keep_it_busy():
        try:
            # A coroutine returning the whole string, not an async generator —
            # `async for` over it raises TypeError before the slot is ever taken,
            # which the third run of this probe did while reporting "no orphan".
            async def sink(*_a, **_k):
                return None

            await provider.generate_response_stream(
                "Write a five hundred line poem, one line per number, counting up.",
                on_chunk=sink,
            )
        except Exception as exc:
            print(f"   (generation ended: {type(exc).__name__}: {exc})")

    work = asyncio.create_task(keep_it_busy())
    for _ in range(60):
        await asyncio.sleep(0.5)
        if provider.supervisor._in_flight > 0:
            break
    print(f"   supervisor._in_flight = {provider.supervisor._in_flight}, "
          f"generation task done = {work.done()}")
    if provider.supervisor._in_flight == 0:
        print("   WARNING the supervisor never counted the call — window missed, "
              "this run cannot see the defect")

    print(f"\n2. the rename: the alias leaves the configuration, as a providers save does")
    retired = prov.retire_absent({ALIAS_AFTER})
    print(f"   retire_absent returned {retired}")
    print(f"   registry now holds: {sorted(prov._ACTIVE_SUPERVISORS)}")

    print(f"\n3. the shutdown sweep, the half c9889eb0 added")
    stopped = await prov.stop_all_supervisors()
    print(f"   stop_all_supervisors returned {stopped}")

    await asyncio.sleep(3)  # a stopped child needs a moment to actually leave

    survived = alive(pid)
    print(f"\n4. VERDICT — is pid {pid} still running after the shutdown? {survived}")
    if survived:
        print("   ORPHAN VISIBLE — the instrument can see the defect.")
        print("   (killing the probe child so nothing is left behind)")
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        print("   no orphan on this run.")

    leaked = llama_pids() - before
    if leaked:
        print(f"   WARNING probe left these pids behind: {sorted(leaked)}")
        for p in leaked:
            subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True)
    return 0 if survived else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
