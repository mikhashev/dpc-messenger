"""A UI command that waits on something outside this process is not awaited on the read loop.

`LocalApiServer._handler` awaits every handler inline unless it carries
`@slow_command` or `@sends_own_response`, and while it awaits, nothing else the
interface sends on that socket is read - Stop included. On 2026-10-29 a pasted
image in an agent chat (`send_image`) ran the agent's whole vision turn inline:
the Stop sat unread for minutes until the socket died of a keepalive timeout.

This walks the source, not a running service. From each handler in
`ALLOWED_COMMANDS` it follows every call that is awaited (an `await`, an
`async for`, an `async with`), through CoreService's own methods and into the
classes its attributes hold (the coordinators, the domain services, the
managers), and stops at a sentinel: an entry point that waits on an agent turn,
a model, a peer's answer or a remote API. A handler that reaches one carries a
mark or a reason in `INLINE_ON_PURPOSE`.

Only awaited calls are followed. A coroutine handed to `create_task` or
`ensure_future` does not hold the loop, which is why a group message that
mentions an agent is not flagged: the agent's turn is a task. What the walk
cannot see: a call through a variable whose class it cannot name is matched by
method name only for the sentinels marked `any_receiver`, and a task awaited
later by name (`t = create_task(...); await t`) is not followed.

A wait on a thread is a wait on the loop too. A handler that hands work to a
thread and awaits it holds the socket for as long as the thread works, though
nothing outside this process is involved: the whole-file hash before a
FILE_OFFER is that (`run_in_executor`). Section (e) of SENTINELS lists the forms
of that hand-off the code uses. `test_no_other_form_of_thread_hand_off_is_in_use`
fails when a module uses a form the walk does not read.
"""

from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PKG = Path(__file__).resolve().parent.parent / "dpc_client_core"

# key -> (any_receiver, what it waits on). A key is "Class.method", a module-level
# function's name, or a dotted name in the asyncio module ("asyncio.wait_for",
# "asyncio.to_thread", "asyncio.AbstractEventLoop.run_in_executor"). `any_receiver`
# lets a call through a local variable (`agent_manager.process_message(...)`)
# match by method name; the loop is always reached that way (`loop.`, `self._loop.`,
# `asyncio.get_running_loop().`).
SENTINELS: Dict[str, Tuple[bool, str]] = {
    # (a) an agent turn, or starting the agent that runs one
    "DpcAgentManager.process_message": (True, "a whole agent turn"),
    "DpcAgentManager.ensure_started": (True, "starts the agent: memory indexes, its Telegram bot"),
    "DpcAgentProvider._ensure_manager": (True, "may create and start the agent's manager"),
    # (b) a model
    "LLMManager.query": (False, "a model call"),
    "LLMManager.query_messages": (False, "a model call"),
    "InferenceOrchestrator.execute_inference": (False, "a model call, local or on a peer"),
    "LocalWhisperProvider.transcribe": (True, "a Whisper transcription"),
    "LocalWhisperProvider.ensure_model_loaded": (True, "loads a Whisper model"),
    "ConversationMonitor.generate_commit_proposal": (True, "knowledge extraction by a model"),
    "strata_status": (False, "asks the Strata launcher for /metrics"),
    "stop_local_engine": (False, "drains a local engine's running generation"),
    # (c) a peer's answer, or a connect
    "P2PManager.connect_directly": (False, "a TLS connect and HELLO"),
    "P2PManager.connect_via_node_id": (False, "a connect found through the DHT"),
    "ConnectionOrchestrator.connect": (False, "the six-tier connect"),
    "ContextCoordinator.request_peer_context": (False, "a peer's context answer"),
    "ContextCoordinator.request_device_context": (False, "a peer's device-context answer"),
    "P2PCoordinator.request_transcription_from_peer": (False, "a peer's transcription"),
    "asyncio.wait_for": (False, "a wait with a deadline: a future, a peer's answer"),
    # (d) a remote API
    "HubClient.login": (False, "the Hub's OAuth flow, up to three minutes"),
    "HubClient.connect_signaling_socket": (False, "a websocket to the Hub"),
    "HubClient.close": (False, "the Hub websocket's closing handshake"),
    "AIProvider.get_balance": (True, "a provider's balance API"),
    "OllamaProvider.get_model_info": (True, "the Ollama daemon's HTTP API"),
    "fetch_model_ids": (False, "a provider's model-list API"),
    "LocalWhisperProvider.download_model_async": (True, "a model download"),
    "DpcAgentManager._start_telegram_bridge": (True, "logs a Telegram bot in and starts polling"),
    "TelegramService._stop_agent_telegram_bridge": (False, "stops a Telegram bot's polling"),
    "AgentTelegramBridge.send_markdown": (True, "a Telegram Bot API send"),
    # (e) local work handed to a thread and awaited: nothing outside the process, but
    # the socket is held for as long as the thread works
    "asyncio.AbstractEventLoop.run_in_executor": (True, "work in an executor thread"),
    "asyncio.to_thread": (False, "work in a worker thread"),
    "write_index_async": (False, "an agent's index writer thread, possibly draining a rebuild"),
}

# Handlers that reach a sentinel and stay on the read loop, each with why.
_WHOLE_FILE_HASH = (
    "Hashes the whole file in the executor before FILE_OFFER (`send_file` > "
    "`compute_file_fingerprint` > `_hash_in_executor`, a read of the file that takes as "
    "long as the file is big) and stays inline so that a text sent next cannot overtake "
    "the offer. Card A-SEND-KEEPS-ITS-PLACE-IN-A-CHAT-ONLY-BY-HOLDING-THE-UI-SOCKETS-READ-LOOP."
)
# The handlers that hash a whole file before an offer, by the walk's own finding.
HASHES_A_WHOLE_FILE = (
    "send_file", "send_p2p_image", "send_voice_message",
    "send_group_file", "send_group_voice_message",
)

_STATUS_PUSH_SCAN = (
    "Local work in a thread, not yet judged: the status push after the change "
    "(`get_status`) scans the host's addresses in an executor (service.py:2419, "
    "`_scan_local_ips`: UDP connects, a host-name lookup bounded at 3 s, `ip addr` with "
    "a 2 s timeout); a scan under 30 s old is reused."
)

INLINE_ON_PURPOSE: Dict[str, str] = {
    **{c: _WHOLE_FILE_HASH for c in HASHES_A_WHOLE_FILE},
    "get_providers_list": (
        "Reaches the Ollama daemon in a thread (`_provider_rows` > `supports_vision`, "
        "service.py:2774). A read that sets a store: answers arriving out of order could "
        "leave an older snapshot on screen."
    ),
    # --- local work in a thread, not yet judged: the walk found them, nobody has decided ---
    "clear_peer_cache": _STATUS_PUSH_SCAN,
    "remove_cached_peer": _STATUS_PUSH_SCAN,
    "list_processes": (
        "Local work in a thread, not yet judged: the psutil walk of the process registry "
        "and the nvidia-smi reads (service.py:10604, `read` > `read_vram_mib`, each probe "
        "capped at 10 s, a reading reused for 30 s)."
    ),
    "reset_model_download_decline": (
        "Local work in a thread, not yet judged: for the Camoufox download it asks "
        "`camoufox_binary.size_bytes` in a thread (model_download_service.py:178), which "
        "makes one GitHub API read of the pinned tag only when the size table has no row "
        "for this platform (camoufox_binary.py:212-229; no timeout set here)."
    ),
    "send_group_image": (
        "Keeps the group's order: with 'Describe for agents' on it waits for a vision "
        "model, and a text the user sends after the image must not land before it."
    ),
}

# Marked for work inside this process that holds the socket, not for a sentinel.
MARKED_FOR_LOCAL_WORK: Dict[str, str] = {
    "trigger_group_sleep": "sleeps 2 s per agent between starts; the UI does not await it",
    "purge_denied_shared_knowledge": "waits on each agent's index writer, which may be draining a rebuild",
}

SPAWNERS = {"create_task", "ensure_future", "run_coroutine_threadsafe",
            "call_soon", "call_soon_threadsafe", "add_done_callback"}
MARKS = {"slow_command", "sends_own_response"}

FuncDef = (ast.FunctionDef, ast.AsyncFunctionDef)


class _Index:
    def __init__(self, pkg: Path) -> None:
        self.classes: Dict[str, List[ast.ClassDef]] = defaultdict(list)
        self.funcs: Dict[str, List[ast.AST]] = defaultdict(list)
        self.modules: Dict[str, ast.Module] = {}
        for path in sorted(pkg.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self.modules[path.relative_to(pkg).as_posix()] = tree
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    self.classes[node.name].append(node)
                elif isinstance(node, FuncDef):
                    self.funcs[node.name].append(node)
        self._attr_cache: Dict[str, Dict[str, set]] = {}
        self._ctor_params = self._collect_ctor_params()

    # --- classes and their methods ---
    def bases(self, cname: str, seen=None) -> List[str]:
        seen = seen or set()
        out = [cname]
        for c in self.classes.get(cname, []):
            for b in c.bases:
                name = b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
                if name in self.classes and name not in seen and name != cname:
                    seen.add(name)
                    out += self.bases(name, seen)
        return out

    def method(self, cname: str, name: str) -> List[ast.AST]:
        for cls in self.bases(cname):
            found = [n for c in self.classes.get(cls, []) for n in c.body
                     if isinstance(n, FuncDef) and n.name == name]
            if found:
                return found
        return []

    def _collect_ctor_params(self) -> Dict[Tuple[str, str], str]:
        """`Cls(self)` inside class C types Cls.__init__'s parameter as C."""
        out: Dict[Tuple[str, str], str] = {}
        for cname, defs in self.classes.items():
            for c in defs:
                for call in ast.walk(c):
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id in self.classes):
                        continue
                    inits = self.method(call.func.id, "__init__")
                    if not inits:
                        continue
                    params = [a.arg for a in inits[0].args.args[1:]]
                    for i, arg in enumerate(call.args):
                        if isinstance(arg, ast.Name) and arg.id == "self" and i < len(params):
                            out[(call.func.id, params[i])] = cname
                    for kw in call.keywords:
                        if isinstance(kw.value, ast.Name) and kw.value.id == "self" and kw.arg:
                            out[(call.func.id, kw.arg)] = cname
        return out

    def attr_types(self, cname: str) -> Dict[str, set]:
        if cname in self._attr_cache:
            return self._attr_cache[cname]
        res: Dict[str, set] = defaultdict(set)
        self._attr_cache[cname] = res
        # Two passes: `self.a = self.b.c` may be read before `self.b = B()`.
        for _ in range(2):
            for c in self.classes.get(cname, []):
                for fn in ast.walk(c):
                    if isinstance(fn, FuncDef):
                        self._assignments(fn, cname, res)
        return res

    def _assignments(self, fn: ast.AST, cname: str, res: Dict[str, set]) -> None:
        params = {}
        for a in fn.args.args + fn.args.kwonlyargs:
            t = _ann_name(a.annotation) or self._ctor_params.get((cname, a.arg))
            if t in self.classes:
                params[a.arg] = t
        for n in ast.walk(fn):
            if isinstance(n, ast.AnnAssign) and _is_self_attr(n.target):
                t = _ann_name(n.annotation)
                if t in self.classes:
                    res[n.target.attr].add(t)
            if not isinstance(n, (ast.Assign, ast.AnnAssign)):
                continue
            target = n.targets[0] if isinstance(n, ast.Assign) else n.target
            if _is_self_attr(target) and n.value is not None:
                res[target.attr].update(self._types_of(n.value, cname, params))

    def _types_of(self, value: ast.AST, cname: str, params: Dict[str, str]) -> set:
        if isinstance(value, ast.IfExp):
            return self._types_of(value.body, cname, params) | self._types_of(value.orelse, cname, params)
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in self.classes:
            return {value.func.id}
        if isinstance(value, ast.Name):
            if value.id == "self":
                return {cname}
            return {params[value.id]} if value.id in params else set()
        if isinstance(value, ast.Attribute):
            out: set = set()
            for owner in self._types_of(value.value, cname, params):
                out |= set(self.attr_types(owner).get(value.attr, ()))
            return out
        return set()


def _ann_name(a: Optional[ast.AST]) -> Optional[str]:
    if a is None:
        return None
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value.strip("'\"").split("[")[-1].rstrip("]").split(".")[-1]
    if isinstance(a, ast.Name):
        return a.id
    if isinstance(a, ast.Attribute):
        return a.attr
    if isinstance(a, ast.Subscript):
        return _ann_name(a.slice)
    return None


def _is_self_attr(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"


def _awaited_calls(fn: ast.AST) -> Tuple[List[ast.Call], Dict[str, ast.AST]]:
    """Calls the body awaits, minus those handed to a spawner. Nested defs are
    collected, not walked: they run only when something awaits them by name."""
    nested: Dict[str, ast.AST] = {}
    out: List[ast.Call] = []

    def visit(node: ast.AST, in_await: bool) -> None:
        if isinstance(node, (*FuncDef, ast.Lambda)):
            if isinstance(node, FuncDef):
                nested[node.name] = node
            return
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in SPAWNERS:
                return
            if in_await:
                out.append(node)
        if isinstance(node, ast.AsyncFor):
            visit(node.iter, True)
            for s in node.body + node.orelse:
                visit(s, in_await)
            return
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                visit(item.context_expr, True)
            for s in node.body:
                visit(s, in_await)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_await or isinstance(node, ast.Await))

    for stmt in fn.body:
        visit(stmt, False)
    return out, nested


class _Walker:
    def __init__(self, index: _Index) -> None:
        self.ix = index
        by_method: Dict[str, List[str]] = defaultdict(list)
        for key, (any_receiver, _) in SENTINELS.items():
            if "." in key and any_receiver:
                by_method[key.rsplit(".", 1)[1]].append(key)
        self._by_method = by_method

    def _sentinel_for(self, call: ast.Call, owner: Optional[str]) -> Optional[str]:
        f = call.func
        text = ast.unparse(f)
        if text in SENTINELS and "." in text and text.split(".")[0] not in self.ix.classes:
            return text  # asyncio.wait_for
        if isinstance(f, ast.Name):
            return f.id if f.id in SENTINELS and f.id in self.ix.funcs else None
        if not isinstance(f, ast.Attribute):
            return None
        for cls in self._receiver_types(f.value, owner):
            for base in self.ix.bases(cls):
                if f"{base}.{f.attr}" in SENTINELS:
                    return f"{base}.{f.attr}"
        hits = self._by_method.get(f.attr)
        return hits[0] if hits else None

    def _receiver_types(self, recv: ast.AST, owner: Optional[str]) -> List[str]:
        if owner is None:
            return []
        if isinstance(recv, ast.Name) and recv.id == "self":
            return [owner]
        if isinstance(recv, ast.Attribute):
            # `self.a` and the longer `self.service.a`: the coordinators reach
            # CoreService's managers through the `service` they were built with.
            return sorted(self.ix._types_of(recv, owner, {}))
        return []

    def _targets(self, call: ast.Call, owner: Optional[str], nested: Dict[str, ast.AST]):
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in nested:
                return [(nested[f.id], owner)]
            return [(d, None) for d in self.ix.funcs.get(f.id, [])]
        if isinstance(f, ast.Attribute):
            return [(d, cls) for cls in self._receiver_types(f.value, owner)
                    for d in self.ix.method(cls, f.attr)]
        return []

    def reach(self, owner: str, fn: ast.AST) -> List[str]:
        """Every path from fn to a sentinel, as 'a > b > sentinel'."""
        hits: List[str] = []
        seen = {id(fn)}

        def walk(fn_node, cls, path):
            calls, nested = _awaited_calls(fn_node)
            for call in calls:
                hit = self._sentinel_for(call, cls)
                label = ast.unparse(call.func)
                if hit:
                    hits.append(" > ".join(path + [hit]))
                    continue
                for target, tcls in self._targets(call, cls, nested):
                    if id(target) in seen or len(path) > 15:
                        continue
                    seen.add(id(target))
                    walk(target, tcls, path + [label])

        walk(fn, owner, [])
        return hits


def _asyncio_attr(dotted: str):
    obj = asyncio
    for part in dotted.split(".")[1:]:
        obj = getattr(obj, part, None)
    return obj


def _allowed_commands(index: _Index) -> List[str]:
    for node in ast.walk(index.modules["local_api.py"]):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "ALLOWED_COMMANDS":
            return sorted(e.value for e in node.value.args[0].elts)
    raise AssertionError("ALLOWED_COMMANDS not found in local_api.py")


def _marks(fn: ast.AST) -> set:
    return {d.id for d in fn.decorator_list if isinstance(d, ast.Name)} & MARKS


_INDEX = _Index(PKG)
_WALKER = _Walker(_INDEX)
# ui_log is answered inside local_api and never reaches CoreService.
_HANDLERS = {c: _INDEX.method("CoreService", c)[0]
             for c in _allowed_commands(_INDEX) if c != "ui_log"}
_REACH = {c: _WALKER.reach("CoreService", fn) for c, fn in _HANDLERS.items()}


def test_every_allowed_command_is_a_handler_the_walk_reads():
    assert len(_HANDLERS) >= 139, "the allowlist lost its handlers, or the walk lost the allowlist"
    missing = [c for c in _allowed_commands(_INDEX) if c != "ui_log" and c not in _HANDLERS]
    assert not missing


def test_every_sentinel_still_names_a_definition():
    """A renamed entry point would leave its sentinel matching nothing, and every
    handler that waits on it would pass as if it did not."""
    stale = []
    for key in SENTINELS:
        if key.startswith("asyncio."):
            ok = callable(_asyncio_attr(key))
        elif "." in key:
            cls, name = key.split(".", 1)
            ok = cls in _INDEX.classes and bool(_INDEX.method(cls, name))
        else:
            ok = key in _INDEX.funcs
        if not ok:
            stale.append(key)
    assert not stale, f"sentinels that name no definition: {stale}"


def test_a_handler_that_waits_carries_a_mark_or_a_reason():
    offenders = {
        c: paths for c, paths in _REACH.items()
        if paths and not _marks(_HANDLERS[c]) and c not in INLINE_ON_PURPOSE
    }
    lines = [f"{c}: {'; '.join(paths[:3])}" for c, paths in sorted(offenders.items())]
    assert not offenders, (
        "these handlers wait inline on the UI socket's read loop - mark them "
        "@slow_command, or give the reason in INLINE_ON_PURPOSE:\n  " + "\n  ".join(lines)
    )


def test_the_marked_handlers_are_found_waiting():
    """Not vacuous: the walk must still see the waits these handlers were marked for."""
    expected = {
        "send_image", "transcribe_audio", "ai_assisted_instruction_creation",
        "ai_assisted_instruction_creation_remote", "query_ollama_model_info",
        "query_remote_providers", "connect_to_peer", "connect_via_dht", "login_to_hub",
        "disconnect_from_hub", "preload_whisper_model", "link_agent_telegram",
        "set_agent_telegram_enabled", "unlink_agent_telegram", "schedule_agent_task",
        "local_engine_status", "send_cc_agent_response",
        # marked before this test existed
        "get_provider_balance", "get_provider_balances", "query_provider_models",
        "download_whisper_model", "download_model", "end_conversation_session",
        "stop_local_engine", "execute_ai_query",
    }
    blind = sorted(c for c in expected if not _REACH.get(c))
    assert not blind, f"the walk no longer sees these handlers wait: {blind}"
    unmarked = sorted(c for c in expected if not _marks(_HANDLERS[c]))
    assert not unmarked, f"expected a mark on: {unmarked}"


def test_an_inline_reason_still_describes_a_handler_that_waits():
    for c in INLINE_ON_PURPOSE:
        assert c in _HANDLERS, f"{c} is no longer a command"
        assert _REACH[c], f"{c} no longer waits; drop it from INLINE_ON_PURPOSE"
        assert not _marks(_HANDLERS[c]), f"{c} is marked and listed as inline"


def test_the_handlers_marked_for_local_work_keep_their_mark():
    for c in MARKED_FOR_LOCAL_WORK:
        assert "slow_command" in _marks(_HANDLERS[c]), c


def test_the_marks_read_from_the_source_are_the_ones_the_dispatcher_reads():
    """The walk reads decorators by name; the dispatcher reads the attributes they set."""
    from dpc_client_core.service import CoreService

    for c, fn in _HANDLERS.items():
        live = getattr(CoreService, c)
        assert ("slow_command" in _marks(fn)) == getattr(live, "dpc_slow", False), c
        assert ("sends_own_response" in _marks(fn)) == getattr(live, "dpc_sends_own_response", False), c


RUN_IN_EXECUTOR = "asyncio.AbstractEventLoop.run_in_executor"


def test_the_handlers_that_hash_a_file_are_found_reaching_the_executor():
    """Not vacuous: the hand-off to a thread is what the five file-sending handlers
    are listed for, so the walk must still see it from each of them."""
    blind = [c for c in HASHES_A_WHOLE_FILE
             if not any(p.endswith(RUN_IN_EXECUTOR) and "_hash_in_executor" in p
                        for p in _REACH.get(c, []))]
    assert not blind, f"the walk no longer sees these hash a file in the executor: {blind}"
    for c in HASHES_A_WHOLE_FILE:
        assert c in INLINE_ON_PURPOSE and not _marks(_HANDLERS[c]), c


def test_each_thread_sentinel_is_reached_by_a_handler():
    """One handler per sentinel kind the walk reads for a thread: a form the walk
    stopped matching would leave its handlers passing as if they did not wait."""
    expected = {
        "get_providers_list": "asyncio.to_thread",
        "clear_peer_cache": RUN_IN_EXECUTOR,
        "purge_denied_shared_knowledge": "write_index_async",
    }
    for c, sentinel in expected.items():
        assert any(p.endswith(sentinel) for p in _REACH.get(c, [])), (c, sentinel, _REACH.get(c))


def test_the_forms_of_thread_hand_off_are_read_wherever_the_receiver_is_written():
    src = (
        "async def a(self):\n    await asyncio.get_running_loop().run_in_executor(None, f)\n"
        "async def b(self):\n    await self._loop.run_in_executor(None, f)\n"
        "async def c(self, loop):\n    await asyncio.wait_for(loop.run_in_executor(ex, f), timeout=3)\n"
        "async def d(self):\n    await asyncio.gather(asyncio.to_thread(f), asyncio.to_thread(g))\n"
        "async def e(self):\n    await write_index_async(p, f)\n"
        "async def g(self, loop):\n    asyncio.create_task(asyncio.to_thread(f))\n"
    )
    funcs = {fn.name: fn for fn in ast.parse(src).body}
    for name in "abc":
        assert any(p.endswith(RUN_IN_EXECUTOR) for p in _WALKER.reach("CoreService", funcs[name])), name
    assert sum(p.endswith("asyncio.to_thread") for p in _WALKER.reach("CoreService", funcs["d"])) == 2
    assert _WALKER.reach("CoreService", funcs["e"]) == ["write_index_async"]
    assert _WALKER.reach("CoreService", funcs["g"]) == []


# Forms of handing work to a thread or another loop that the walk does not read, and
# where the code uses them today. A new use somewhere else fails the next test.
UNREAD_FORMS_KNOWN: Dict[str, str] = {
    "asyncio.wrap_future@local_api.py": "hops a send to the owner loop; waits on no work of its own",
    "asyncio.wrap_future@managers/llama_server_supervisor.py": (
        "`on_the_owner_loop`: awaits a coroutine on the supervisor's loop; what it waits "
        "on is the engine call the walk meets elsewhere"
    ),
    "asyncio.wrap_future@dpc_agent/index_writer.py": "`write_index_async`, a sentinel above",
}


def test_no_other_form_of_thread_hand_off_is_in_use():
    """The walk reads `run_in_executor` (any receiver), `asyncio.to_thread` and
    `write_index_async`. A bare `to_thread`, `anyio`'s `run_sync` or a
    `wrap_future` somewhere new is a way to wait on a thread it would not see."""
    found = set()
    for rel, tree in _INDEX.modules.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("asyncio", "anyio.to_thread", "anyio"):
                if any(a.name in ("to_thread", "run_sync", "wrap_future", "run_in_executor")
                       for a in node.names):
                    found.add(f"from {node.module} import@{rel}")
            if not isinstance(node, ast.Call):
                continue
            text = ast.unparse(node.func)
            if text in ("to_thread", "run_sync", "wrap_future") or text.endswith(".run_sync"):
                found.add(f"{text}@{rel}")
            elif text == "asyncio.wrap_future":
                found.add(f"asyncio.wrap_future@{rel}")
    assert found == set(UNREAD_FORMS_KNOWN), (
        f"forms of thread hand-off the walk does not read: new {sorted(found - set(UNREAD_FORMS_KNOWN))}, "
        f"gone {sorted(set(UNREAD_FORMS_KNOWN) - found)}"
    )


def test_a_task_handed_to_create_task_is_not_followed():
    """The rule the whole walk rests on, on a function written here."""
    src = (
        "async def h(self):\n"
        "    asyncio.create_task(self.agent_manager.process_message('x'))\n"
        "    await self.local_api.broadcast_event('e', {})\n"
        "async def g(self, agent_manager):\n"
        "    await agent_manager.process_message('x')\n"
    )
    mod = ast.parse(src)
    h, g = mod.body
    assert _WALKER.reach("CoreService", h) == []
    assert _WALKER.reach("CoreService", g) == ["DpcAgentManager.process_message"]
