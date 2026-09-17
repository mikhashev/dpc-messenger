"""The board's link graph is laid out by `tools/backlog/build.py`, not by the browser.

On 2026-09-04 the picture was a dot in the middle of an empty canvas with a dozen
spokes: the pair repulsion `2600 / d2` had no lower bound on the distance, so two
nodes seeded a fraction of a pixel apart got a kick of thousands of pixels and never
came back. `build.py` runs top-level on import, so the function under test is read
out of the source and executed on its own.
"""
import math
import random
import re
from pathlib import Path

BUILD = Path(__file__).resolve().parents[3] / "tools" / "backlog" / "build.py"


def _layout_from(source: str):
    match = re.search(r"^def layout\(.*?^    return steps\n", source, re.S | re.M)
    assert match, "layout() not found in build.py"
    namespace = {"math": math}
    exec(match.group(0), namespace)
    return namespace["layout"]


def _dense_graph(n=120, seed=7):
    """n nodes seeded inside a 3 px disc, chained in a ring: the worst case for repulsion."""
    rnd = random.Random(seed)
    nodes = [{"id": f"N{i}"} for i in range(n)]
    prev = {}
    for i in range(n):
        ang, rad = rnd.random() * 2 * math.pi, rnd.random() * 3
        prev[f"N{i}"] = (600 + rad * math.cos(ang), 410 + rad * math.sin(ang))
    links = [{"s": i, "t": (i + 1) % n} for i in range(n)]
    return nodes, links, prev


def _farthest(nodes):
    return max(math.hypot(d["x"] - 600, d["y"] - 410) for d in nodes)


def test_nodes_seeded_on_top_of_each_other_settle_within_the_canvas():
    layout = _layout_from(BUILD.read_text(encoding="utf-8"))
    nodes, links, prev = _dense_graph()
    layout(nodes, links, prev)
    far = _farthest(nodes)
    assert far < 1500, f"a node ended {far:.0f} px from the centre — the layout exploded"


def test_the_same_maths_without_the_floor_does_explode():
    """The bound above is not vacuous: remove the floor and the same input flies away."""
    source = BUILD.read_text(encoding="utf-8")
    floored = "d2 = max(dx * dx + dy * dy, 400.0)"
    assert floored in source, "the distance floor left build.py"
    layout = _layout_from(source.replace(floored, "d2 = dx * dx + dy * dy or 1.0"))
    nodes, links, prev = _dense_graph()
    layout(nodes, links, prev)
    assert _farthest(nodes) > 1500
