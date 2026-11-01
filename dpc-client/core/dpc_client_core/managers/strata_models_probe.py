"""The Strata models that can be installed, read from Strata's own table. Read-only.

This file is a script, not a module of the backend: `list_strata_models` runs it under
the pinned Strata Control's own Python, in a process of its own,

    python -I strata_models_probe.py <control_root> <run_configs_dir>

because the table is Strata's (`tools/strata_mcp.py`, class `Strata`) and imports
things only Control's interpreter has. It builds `Strata(root=<run_configs_dir>,
src=<control_root>)`, calls `model_table(hardware())` and prints one JSON line,
`{"families": [...]}`, to stdout; the rows are Strata's, field for field.

Read-only, on purpose:
- `Strata(...)` only resolves its paths; it creates nothing. `hardware()` and
  `model_table()` call no `save_state` and make no folder (checked in v0.2.9). `installed`
  is decided from `strata-*.json` under `<run_configs_dir>`; a folder that does not exist
  has none, so every size is not installed, and it is not created.
- Bytecode is not written (`-I` ignores PYTHONDONTWRITEBYTECODE, so it is set here): the
  imports leave no `__pycache__` in Control's folder.
- Whatever Strata prints while it imports or computes goes to stderr, the way Strata's own
  launcher imports it (strata_cli/launch/catalog.py): stdout is fd 1 pointed at stderr
  until the one JSON line is written, so a child process cannot pollute it either.

Stdlib only; nothing here imports the backend.
"""

import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True


def main(argv) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: strata_models_probe.py <control_root> <run_configs_dir>\n")
        return 2
    control = Path(argv[1])
    run_configs = Path(argv[2])
    mcp_file = control / "tools" / "strata_mcp.py"
    if not mcp_file.is_file():
        sys.stderr.write(f"{mcp_file} is not there: this is not a Strata Control folder\n")
        return 3

    out_fd = os.dup(1)
    real_stdout = sys.stdout
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        if str(control) not in sys.path:
            sys.path.insert(0, str(control))  # setup.py imports strata_control
        spec = importlib.util.spec_from_file_location("strata_mcp_for_dpc", mcp_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules["strata_mcp_for_dpc"] = module
        spec.loader.exec_module(module)
        strata = module.Strata(run_configs, control)
        families = strata.model_table(strata.hardware())
        line = json.dumps({"families": families}, ensure_ascii=True)
    except BaseException:  # noqa: BLE001 - the caller reads the reason from stderr
        traceback.print_exc()
        return 1
    finally:
        sys.stdout = real_stdout
        os.dup2(out_fd, 1)
        os.close(out_fd)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
