"""
Strata Fetcher — the Strata release DPC is tested against (ADR-044 point 5, B2).

The pin is two numbers that belong together, on two version lines. `STRATA_RELEASE_TAG`
is the tag of one Strata release, on Strata's app line (0.2.x), published with its
`strata-feed.json`. The assets of that release carry the other line: the **Control**
package (`strata-control-<ver>-<platform>.zip`) and the engine packages are versioned
0.1.x inside it. `STRATA_ENGINE_VERSION` is that engine version (`strata.exe` and
`strata-vision.exe`, launched by the **launcher** `serve/server.py`), the one it reports
at `/metrics` `engine.version`. A person sees both as "Strata v0.2.9 (engine 0.1.55)".

Until now the second number lived on each alias as a hand-raised
`tested_version`. It is in code instead, as `LLAMA_CPP_TAG` is for the llama.cpp
child (`llama_server_fetcher.py`): one place, moved by one reviewed commit, the
same for every alias. The supervisor compares what the engine reports with it
(`StrataSupervisor._check_version`); a mismatch only warns, because an alias's
install is the owner's own checkout and ADR-044 point 5 says warn, not refuse.

To move the pin: update Strata on a node, run DPC's start smoke there live
(version check, `/health`, `/props`, the two forced tool calls), record the
result on the ADR-044 point-5 card, then change both constants in one commit.
Neither is ever read from the alias or from upstream's «latest».

This module holds the pin only. The fetch itself, a DPC-owned install under
`~/.dpc/bin/` verified against per-asset sha256 rows as `llama_server_fetcher.py`
does for llama.cpp, arrives in B2 step 2.
"""

STRATA_RELEASE_TAG = "v0.2.9"
STRATA_ENGINE_VERSION = "0.1.55"


def strata_pin() -> dict:
    """The pin as the status payload carries it: `{"release_tag", "engine_version"}`."""
    return {"release_tag": STRATA_RELEASE_TAG, "engine_version": STRATA_ENGINE_VERSION}

