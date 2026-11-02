"""A stand-in for Strata Control's tools/strata_mcp.py: a `Strata` class whose `model_table` is
the `table.json` beside the Control root (what `strata_models_probe.py` imports)."""
import json
from pathlib import Path


class Strata:
    def __init__(self, root, src=None):
        self.root = Path(root)
        self.src = Path(src)

    def hardware(self, fresh=False):
        return {"ram_gb": 64.0}

    def model_table(self, hw):
        return json.loads((self.src / "table.json").read_text(encoding="utf-8"))
