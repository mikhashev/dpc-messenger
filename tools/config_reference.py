"""Generate the complete config reference in docs/CONFIGURATION.md from settings.py.

    uv run python tools/config_reference.py           # rewrite the section
    uv run python tools/config_reference.py --check    # fail if it is out of date

The hand-written parts of CONFIGURATION.md (scenarios, troubleshooting, security, the
prose for the sections people actually tune) are untouched: this only owns the block
between the two markers. Written because the doc described 9 of the 24 sections the code
writes, and every number in it had to be trusted rather than checked.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dpc-client" / "core" / "dpc_client_core" / "settings.py"
DOC = ROOT / "docs" / "CONFIGURATION.md"

BEGIN = "<!-- BEGIN GENERATED CONFIG REFERENCE -->"
END = "<!-- END GENERATED CONFIG REFERENCE -->"


def defaults():
    """Every [section] key the code writes into a fresh config.ini, with its comment."""
    src = SRC.read_text(encoding="utf-8")
    lines = src.split("\n")

    # Comments live on the source line, which the AST drops, so keep both.
    comment_at = {}
    for i, line in enumerate(lines, 1):
        m = re.search(r"#\s*(.+?)\s*$", line)
        if m and "'" in line.split("#")[0]:
            comment_at[i] = m.group(1)

    out = {}
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript)):
            continue
        target = node.targets[0]
        if not (isinstance(target.value, ast.Attribute) and target.value.attr == "_config"):
            continue
        try:
            section = target.slice.value
        except AttributeError:
            continue
        if not (isinstance(section, str) and isinstance(node.value, ast.Dict)):
            continue
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                out.setdefault(section, []).append(
                    (k.value, str(v.value), comment_at.get(k.lineno, ""))
                )
    return out


def render(data):
    total = sum(len(v) for v in data.values())
    rows = [
        BEGIN,
        "",
        f"Every section and key `_create_default_config` writes into a fresh "
        f"`~/.dpc/config.ini`: **{len(data)} sections, {total} keys**. Generated from "
        f"`settings.py` by `tools/config_reference.py` — edit the code, then re-run it; "
        f"do not hand-edit between the markers.",
        "",
        "An empty default means the key is written blank and the feature stays off until "
        "you fill it in. Every key also accepts an environment variable named "
        "`DPC_<SECTION>_<KEY>` in upper case.",
        "",
    ]
    for section in sorted(data):
        rows.append(f"#### `[{section}]`")
        rows.append("")
        rows.append("| Key | Default | Notes |")
        rows.append("|---|---|---|")
        for key, value, comment in data[section]:
            shown = f"`{value}`" if value != "" else "*(empty)*"
            note = comment.replace("|", "\\|") if comment else ""
            rows.append(f"| `{key}` | {shown} | {note} |")
        rows.append("")
    rows.append(END)
    return "\n".join(rows)


def main():
    doc = DOC.read_text(encoding="utf-8")
    block = render(defaults())

    if BEGIN in doc and END in doc:
        start, stop = doc.index(BEGIN), doc.index(END) + len(END)
        current = doc[start:stop]
        if "--check" in sys.argv:
            if current.strip() == block.strip():
                print("config reference is up to date")
                return 0
            print("config reference is STALE — run tools/config_reference.py")
            return 1
        doc = doc[:start] + block + doc[stop:]
    else:
        if "--check" in sys.argv:
            print("markers not found in docs/CONFIGURATION.md")
            return 1
        anchor = "\n## Using Environment Variables"
        assert anchor in doc, "expected anchor not found"
        doc = doc.replace(anchor, f"\n## Complete Reference: every key the code writes\n\n{block}\n{anchor}", 1)

    DOC.write_text(doc, encoding="utf-8")
    data = defaults()
    print(f"wrote {len(data)} sections / {sum(len(v) for v in data.values())} keys into {DOC.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
