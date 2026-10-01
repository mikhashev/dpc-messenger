"""A new path that hands an image to a provider must be put on the list.

A picture reaches a model through more than one function, and a size or
orientation fix hung on one of them leaves the others as they were
(AN-IMAGE-REACHES-THE-VISION-MODEL-THROUGH-FOUR-DOORS-..., step 1: seven doors
were found in review, not one). `llm_manager.IMAGE_ENTRY_POINTS` names them;
this test finds them again in the source, with `ast`, and fails on a
difference in either direction — an unlisted function, or a listed one that is
gone or no longer does what its kind says.

The rule, per outermost function (closures count toward the function that
holds them), in every module of the package except `providers/`, where the
images are serialised rather than handed over:

- `provider_call`: calls `.generate_with_vision(...)` or `.generate_with_tools(...)`.
- `chat_entry`: unpacks `entry_point_for(...)` and calls the second name it bound.
- `gate`: calls `entry_point_for(...)` and does not call what it returned.
- `wire`: calls `create_remote_inference_request(...)` with an `images` keyword.

`gate` stands on the list although it hands nothing over, so that a fifth
caller of the predicate is seen too. A function of two kinds is two rows.
"""
import ast
from pathlib import Path

import dpc_client_core
from dpc_client_core.llm_manager import IMAGE_ENTRY_POINTS

PACKAGE = Path(dpc_client_core.__file__).resolve().parent
PROVIDER_CALLS = {"generate_with_vision", "generate_with_tools"}


def _called_name(node: ast.Call):
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _kinds_of(function: ast.AST) -> set:
    """The kinds one function belongs to, by the rule in the module docstring."""
    kinds, chosen_names, asked = set(), set(), False
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and _called_name(node.value) == "entry_point_for":
            for target in node.targets:
                if isinstance(target, (ast.Tuple, ast.List)) and len(target.elts) == 2 \
                        and isinstance(target.elts[1], ast.Name):
                    chosen_names.add(target.elts[1].id)
    for call in calls:
        name = _called_name(call)
        if name in PROVIDER_CALLS and isinstance(call.func, ast.Attribute):
            kinds.add("provider_call")
        elif name == "entry_point_for":
            asked = True
        elif name == "create_remote_inference_request" and any(k.arg == "images" for k in call.keywords):
            kinds.add("wire")
        if isinstance(call.func, ast.Name) and call.func.id in chosen_names:
            kinds.add("chat_entry")
    if asked:
        kinds.add("chat_entry" if "chat_entry" in kinds else "gate")
    return kinds


def scan(sources: dict) -> set:
    """`{(kind, file, function)}` over `{relative path: source text}`."""
    found = set()
    for file, text in sources.items():
        tree = ast.parse(text)

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, child.name + ".")
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for kind in _kinds_of(child):
                        found.add((kind, file, owner + child.name))
                else:
                    visit(child, owner)

        visit(tree, "")
    return found


def package_sources() -> dict:
    sources = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if not relative.startswith("providers/"):
            sources[relative] = path.read_text(encoding="utf-8")
    return sources


def declared() -> set:
    return {(e.kind, e.file, e.function) for e in IMAGE_ENTRY_POINTS}


def test_the_ids_are_unique():
    ids = [e.id for e in IMAGE_ENTRY_POINTS]
    assert len(ids) == len(set(ids))


def test_the_source_and_the_list_name_the_same_paths():
    sources = package_sources()
    assert "llm_manager.py" in sources and "dpc_agent/llm_adapter.py" in sources, "the scan saw too little"
    found = scan(sources)
    assert found == declared(), (
        f"on the source but not on the list: {sorted(found - declared())}; "
        f"on the list but not in the source: {sorted(declared() - found)}"
    )


def test_a_new_call_to_a_provider_that_is_not_on_the_list_is_seen():
    sources = package_sources()
    sources["somewhere.py"] = (
        "class Thing:\n    async def look(self, provider, prompt, images):\n"
        "        return await provider.generate_with_vision(prompt, images)\n"
    )
    assert scan(sources) - declared() == {("provider_call", "somewhere.py", "Thing.look")}


def test_a_listed_function_that_stops_handing_the_image_over_is_seen():
    sources = package_sources()
    sources["dpc_agent/llm_adapter.py"] = sources["dpc_agent/llm_adapter.py"].replace(
        "provider.generate_with_vision(", "provider.generate_response(")
    assert declared() - scan(sources) == {
        ("provider_call", "dpc_agent/llm_adapter.py", "DpcLlmAdapter._chat_with_native_vision")}
