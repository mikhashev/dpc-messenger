"""A call the output limit ended inside is told to the client at the door, not lost.

ADR-041 amendment 2026-11-05. Live that day: on `llamacpp_server` the client got `write` with
`{}` under `finish_reason: length`, on `strata` no call at all, and every token the model had
written was gone. A person's "continue" regenerated the same oversized call, again and again.

The door now takes a cut call out of `tool_calls` (an unreadable one, and a last one with no
arguments), keeps what it had written in `<DPC home>/gateway/unexecuted/<request id>.txt`, and
appends one note to the reply text, in the words `loop._cut_call_request` gives an agent.
`finish_reason` stays `length` (`max_tokens` in the Messages form). The stack under the door is
real, as in the file `_Launcher` comes from: a fake OpenAI stream, the real provider classes, the
real `LLMManager`, the real gateway. The mark can die between any two of them.
"""

import json
import logging
import os
from types import SimpleNamespace

import pytest

from dpc_client_core import gateway as gateway_module
from dpc_client_core.gateway import UNEXECUTED_KEPT, _keep_unexecuted, _without_the_cut_call
from dpc_client_core.llm_manager import _tool_use_block
from tests.test_a_client_that_leaves_the_door_ends_the_call_and_nothing_is_generated_again import (
    CHAT_PATH,
    MESSAGES_PATH,
    TOOLS_ANTHROPIC,
    TOOLS_OPENAI,
    _chat,
    _forget,
    _local_provider,
    _messages,
    _service,
)
from tests.test_the_door_hands_over_the_reasoning_the_provider_already_keeps import (
    CALL_IN_PIECES,
    CONTENT,
    _door_call,
    _Launcher,
    _post,
    _strata_kwargs,
)
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _running

WRITE_CUT = {"id": "call_cut", "name": "write", "pieces": ('{"path": "a.txt", "content": "', 'half a file')}
WRITTEN = '{"path": "a.txt", "content": "half a file'
NOTE_HEAD = "Your call of `write` was cut at the output limit of 64 tokens, inside its arguments, so it was not executed."
NOTE_TAIL = "Make the call again with less in it, in smaller parts if it is long."
LIMIT = {"max_tokens": 64}

OPENAI_WHOLE = ("openai-whole", CHAT_PATH, _chat(tools=TOOLS_OPENAI))
OPENAI_STREAM = ("openai-stream", CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI))
MESSAGES_WHOLE = ("messages-whole", MESSAGES_PATH, _messages(tools=TOOLS_ANTHROPIC))
MESSAGES_STREAM = ("messages-stream", MESSAGES_PATH, _messages(tools=TOOLS_ANTHROPIC, stream=True))
FORMS = [OPENAI_WHOLE, OPENAI_STREAM, MESSAGES_WHOLE, MESSAGES_STREAM]
STREAMS = [OPENAI_STREAM, MESSAGES_STREAM]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """The DPC home of the door under test: nothing here may write into the real one."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


def _kept(tmp_path):
    folder = tmp_path / "gateway" / "unexecuted"
    return sorted(folder.glob("*")) if folder.exists() else []


def _events(text):
    return [json.loads(line[len("data: "):]) for line in text.split("\n") if line.startswith("data: {")]


def _read(form, text):
    """What a client of that form sees: (reply text, calls, finish word, the note arrived before the finish)."""
    if form == "openai-whole":
        body = json.loads(text)
        message = body["choices"][0]["message"]
        return (message.get("content") or ""), message.get("tool_calls") or [], body["choices"][0]["finish_reason"], True
    if form == "messages-whole":
        body = json.loads(text)
        blocks = body["content"]
        return ("".join(b["text"] for b in blocks if b["type"] == "text"),
                [b for b in blocks if b["type"] == "tool_use"], body["stop_reason"], True)
    events = _events(text)
    if "Your call of" not in text:
        noted = True
    else:
        noted = None
    if form == "openai-stream":
        choices = [e["choices"][0] for e in events if e.get("choices")]
        shown = "".join(c["delta"].get("content") or "" for c in choices)
        calls = [call for c in choices for call in c["delta"].get("tool_calls") or []]
        finish = next(c["finish_reason"] for c in reversed(choices) if c["finish_reason"])
        if noted is None:
            noted = next(i for i, c in enumerate(choices) if "Your call of" in (c["delta"].get("content") or "")) <= next(
                i for i, c in enumerate(choices) if c["finish_reason"])
        return shown, calls, finish, noted
    shown = "".join(e["delta"]["text"] for e in events
                    if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
    calls = [e["content_block"] for e in events
             if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
    finish = next(e["delta"]["stop_reason"] for e in events if e["type"] == "message_delta")
    if noted is None:
        noted = next(i for i, e in enumerate(events) if "Your call of" in json.dumps(e) and e["type"] != "message_start") < next(
            i for i, e in enumerate(events) if e["type"] == "message_delta")
    return shown, calls, finish, noted


def _the_limit_word(form):
    return "max_tokens" if form.startswith("messages") else "length"


def _engine(kind, **kwargs):
    """(double's kwargs, provider's kwargs) for the two engines' shapes: llama-server keeps a cut call
    in its whole reply and sends it as the stream's pieces; the launcher's whole reply leaves it out."""
    if kind == "strata":
        return kwargs, {**LIMIT}
    return {"usage_apart": True, **kwargs}, {**LIMIT}


_STRATA_INSTALLS = {}


async def _through_the_door(tmp_path, kind, path, body, **kwargs):
    engine_kwargs, provider_kwargs = _engine(kind, **kwargs)
    if kind == "strata":
        # The install is laid down once per directory: a test that asks four times reuses it.
        if str(tmp_path) not in _STRATA_INSTALLS:
            _STRATA_INSTALLS[str(tmp_path)] = _strata_kwargs(tmp_path)
        provider_kwargs = {**provider_kwargs, **_STRATA_INSTALLS[str(tmp_path)]}
    status, text, _, rows = await _door_call(
        tmp_path, engine_kwargs, body, path=path, engine_class=_Launcher, provider_kwargs=provider_kwargs,
    )
    assert status == 200, text
    return text, rows


async def _quiet(*args, **kwargs):
    return None


def _the_note(path):
    return f"{NOTE_HEAD} What it had written is kept at `{path}`. {NOTE_TAIL}"


# --- (1) the four forms, through the real provider and manager --------------------------------


@pytest.mark.parametrize("kind", ["llamacpp_server", "strata"])
@pytest.mark.parametrize("form, path, body", FORMS, ids=[f[0] for f in FORMS])
@pytest.mark.asyncio
async def test_a_cut_call_does_not_reach_the_client_in_any_form_and_the_note_says_so_once(
    tmp_path, kind, form, path, body,
):
    if kind == "strata" and not body.get("stream"):
        pytest.skip("the launcher's whole reply leaves the call out itself; there is nothing for the door to read")
    text, _ = await _through_the_door(
        tmp_path, kind, path, body, calls=(WRITE_CUT,), finish="length", content=CONTENT,
    )
    shown, calls, finish, noted_in_time = _read(form, text)
    (file,) = _kept(tmp_path)
    assert calls == [], "a call with `{}` or half its arguments would run on nothing"
    assert finish == _the_limit_word(form), "the turn did not end on a call"
    assert shown == "the answer\n\n" + _the_note(file.resolve())
    assert shown.count("Your call of") == 1
    assert noted_in_time, "the note came after the finish word"
    assert file.read_text(encoding="utf-8") == f"--- tool call `write` ---\n{WRITTEN}"
    assert file.name.endswith(".txt")


@pytest.mark.parametrize("kind", ["llamacpp_server", "strata"])
@pytest.mark.parametrize("form, path, body", STREAMS, ids=[f[0] for f in STREAMS])
@pytest.mark.asyncio
async def test_with_no_text_before_it_the_note_is_alone_and_once(tmp_path, kind, form, path, body):
    text, _ = await _through_the_door(tmp_path, kind, path, body, calls=(WRITE_CUT,), finish="length", content=())
    shown, calls, _, noted_in_time = _read(form, text)
    (file,) = _kept(tmp_path)
    assert shown == _the_note(file.resolve()), "no blank line in front of nothing"
    assert calls == [] and noted_in_time


@pytest.mark.parametrize("form, path, body", STREAMS, ids=[f[0] for f in STREAMS])
@pytest.mark.asyncio
async def test_a_stream_that_wrote_text_first_writes_the_note_as_its_own_piece_after_a_blank_line(
    tmp_path, form, path, body,
):
    text, _ = await _through_the_door(
        tmp_path, "llamacpp_server", path, body, calls=(WRITE_CUT,), finish="length", content=CONTENT,
    )
    if form == "openai-stream":
        pieces = [c["delta"]["content"] for c in (e["choices"][0] for e in _events(text) if e.get("choices"))
                  if c["delta"].get("content")]
    else:
        pieces = [e["delta"]["text"] for e in _events(text)
                  if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"]
    assert pieces[:2] == list(CONTENT), "the text the client already had is not written again"
    assert len(pieces) == 3 and pieces[2].startswith("\n\nYour call of `write`")


@pytest.mark.parametrize("kind", ["llamacpp_server", "strata"])
@pytest.mark.asyncio
async def test_an_earlier_whole_call_stays_and_the_cut_one_after_it_goes(tmp_path, kind):
    text, _ = await _through_the_door(
        tmp_path, kind, CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI),
        calls=(CALL_IN_PIECES, WRITE_CUT), finish="length", content=CONTENT,
    )
    shown, calls, finish, _ = _read("openai-stream", text)
    assert [c["function"]["name"] for c in calls] == ["read_file"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert finish == "length", "`tool_calls` here would say the turn ended on the call that went"
    assert shown.count("Your call of `write`") == 1 and "`read_file`" not in shown
    assert len(_kept(tmp_path)) == 1


@pytest.mark.asyncio
async def test_a_last_call_with_no_arguments_under_length_is_cut_with_nothing_to_keep(tmp_path):
    """`{}` is what a closed object and a cut one both say; an earlier call was finished before the next began."""
    empty = {"id": "call_empty", "name": "write", "pieces": ("{}",)}
    text, _ = await _through_the_door(
        tmp_path, "llamacpp_server", CHAT_PATH, _chat(tools=TOOLS_OPENAI),
        calls=(CALL_IN_PIECES, empty), finish="length", content=CONTENT,
    )
    shown, calls, finish, _ = _read("openai-whole", text)
    assert [c["function"]["name"] for c in calls] == ["read_file"] and finish == "length"
    assert shown == f"the answer\n\n{NOTE_HEAD} {NOTE_TAIL}", "no file, so no sentence about one"
    assert _kept(tmp_path) == []


# --- (2) the peer route: the wire carries no mark, a cut call arrives as `{}` -------------------


def _tooled_peer(tmp_path, **kwargs):
    service = _peer_service(tmp_path, **kwargs)
    for row in service.peer_metadata[PEER]["providers"]:
        row["supports_tools"] = True
    return service


def _peer_result(*calls, text="I will write it.", finish="length", **extra):
    return _priced_result(
        response=text, finish_reason=finish, response_tokens=64,
        tool_calls=[{"type": "tool_use", "id": f"c{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)],
        **extra,
    )


PEER_BODIES = {
    "openai-whole": (CHAT_PATH, _chat(REMOTE_MODEL, tools=TOOLS_OPENAI)),
    "openai-stream": (CHAT_PATH, _chat(REMOTE_MODEL, tools=TOOLS_OPENAI, stream=True)),
    "messages-whole": (MESSAGES_PATH, _messages(REMOTE_MODEL, tools=TOOLS_ANTHROPIC)),
    "messages-stream": (MESSAGES_PATH, _messages(REMOTE_MODEL, tools=TOOLS_ANTHROPIC, stream=True)),
}


@pytest.mark.parametrize("chunked", [False, True], ids=["no-chunks", "chunks-first"])
@pytest.mark.parametrize("form", PEER_BODIES)
@pytest.mark.asyncio
async def test_a_peer_reply_that_ends_on_an_empty_last_call_under_length_is_cut_in_every_form(tmp_path, form, chunked):
    if chunked and not form.endswith("stream"):
        pytest.skip("chunks are for a stream")
    service = _tooled_peer(
        tmp_path, result=_peer_result(("read_file", {"path": "a.txt"}), ("write", {})),
        chunks=["I will ", "write it."] if chunked else (),
    )
    path, body = PEER_BODIES[form]
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post(server, tmp_path, path, body)
    assert status == 200, text
    shown, calls, finish, noted_in_time = _read(form, text)
    names = [(c.get("function") or c)["name"] for c in calls]
    assert names == ["read_file"] and finish == _the_limit_word(form)
    assert shown == f"I will write it.\n\n{NOTE_HEAD.replace(' of 64 tokens', '')} {NOTE_TAIL}", (
        "the host's wire carries no ceiling and no written arguments: no number, no file")
    assert shown.count("Your call of") == 1 and noted_in_time
    assert _kept(tmp_path) == []


@pytest.mark.asyncio
async def test_a_peer_whole_call_under_length_and_a_peer_text_under_length_are_left_as_they_are(tmp_path):
    for result in (_peer_result(("write", {"path": "a.txt", "content": "whole"})), _peer_result()):
        service = _tooled_peer(tmp_path, result=result)
        async with _running(tmp_path, service) as (server, _):
            status, text = await _post(server, tmp_path, CHAT_PATH, _chat(REMOTE_MODEL, tools=TOOLS_OPENAI))
        shown, calls, finish, _ = _read("openai-whole", text)
        assert status == 200 and finish == "length" and shown == "I will write it."
        assert len(calls) == len(result["tool_calls"])


# --- (3) what does not change --------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["llamacpp_server", "strata"])
@pytest.mark.parametrize("form, path, body", STREAMS, ids=[f[0] for f in STREAMS])
@pytest.mark.asyncio
async def test_an_uncut_call_a_clean_finish_and_text_cut_in_prose_pass_as_they_did(tmp_path, kind, form, path, body):
    whole_call = {"calls": (CALL_IN_PIECES,), "finish": "length", "content": CONTENT}
    for scenario, finish in ((whole_call, "length"),
                             ({"calls": (CALL_IN_PIECES,), "finish": "tool_calls", "content": CONTENT}, "tool_calls"),
                             ({"finish": "length", "content": CONTENT}, "length"),
                             ({"finish": "stop", "content": CONTENT}, "stop")):
        text, rows = await _through_the_door(tmp_path, kind, path, body, **scenario)
        shown, calls, word, _ = _read(form, text)
        assert shown == "the answer" and "Your call of" not in text
        assert len(calls) == len(scenario.get("calls", ()))
        assert word == (_the_limit_word(form) if finish == "length" else
                        ("tool_use" if form.startswith("messages") else finish) if finish == "tool_calls" else
                        ("end_turn" if form.startswith("messages") else "stop"))
    assert _kept(tmp_path) == [], "nothing was cut, nothing is kept"


@pytest.mark.asyncio
async def test_the_ledger_row_is_what_was_measured_and_the_note_is_not_in_it(tmp_path):
    _, rows = await _through_the_door(
        tmp_path, "llamacpp_server", CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI),
        calls=(WRITE_CUT,), finish="length", content=CONTENT,
    )
    (row,) = rows
    assert row["finish_reason"] == "length" and row["completion_tokens"] == 52
    assert "Your call of" not in json.dumps(row)


@pytest.mark.asyncio
async def test_the_launchers_whole_reply_still_has_nothing_to_tell(tmp_path):
    """Not the door's to repair: the launcher left the call out before the reply was written."""
    text, _ = await _through_the_door(
        tmp_path, "strata", CHAT_PATH, _chat(tools=TOOLS_OPENAI), calls=(WRITE_CUT,), finish="length", content=CONTENT,
    )
    shown, calls, finish, _ = _read("openai-whole", text)
    assert (shown, calls, finish) == ("the answer", [], "length") and _kept(tmp_path) == []


# --- (4) the layers: the mark survives each hop and stays off the wire -----------------------------


@pytest.mark.asyncio
async def test_query_messages_carries_the_mark_beside_the_blocks_and_the_blocks_stay_what_a_peer_was_sent(tmp_path):
    async with _Launcher(usage_apart=True, calls=(CALL_IN_PIECES, WRITE_CUT), finish="length", content=CONTENT) as engine:
        provider = _local_provider(engine, **LIMIT)
        try:
            manager = _service(tmp_path, provider).llm_manager
            result = await manager.query_messages(
                [{"role": "user", "content": "hi"}], tools=TOOLS_ANTHROPIC, provider_alias=provider.alias,
                return_metadata=True, on_chunk=_quiet, on_reasoning=_quiet,
            )
        finally:
            _forget(provider)
    assert result["unreadable_arguments"] == {1: WRITTEN}, "by position, from the provider's own mark"
    assert [set(block) for block in result["tool_calls"]] == [{"type", "id", "name", "input"}] * 2, (
        "a new key in a block would travel on the DPTP wire to a guest")
    assert result["tool_calls"][1]["input"] == {} and result["max_tokens"] == 64


@pytest.mark.asyncio
async def test_a_result_with_no_cut_call_has_no_such_key(tmp_path):
    async with _Launcher(usage_apart=True, calls=(CALL_IN_PIECES,), finish="tool_calls", content=CONTENT) as engine:
        provider = _local_provider(engine, **LIMIT)
        try:
            manager = _service(tmp_path, provider).llm_manager
            result = await manager.query_messages(
                [{"role": "user", "content": "hi"}], tools=TOOLS_ANTHROPIC, provider_alias=provider.alias,
                return_metadata=True,
            )
        finally:
            _forget(provider)
    assert "unreadable_arguments" not in result


def test_a_block_made_of_a_marked_call_has_four_keys_whatever_the_mark():
    marked = SimpleNamespace(id="c", name="write", input={}, unreadable_arguments="{\"a\"")
    assert _tool_use_block(marked) == {"type": "tool_use", "id": "c", "name": "write", "input": {}}


# --- (5) the file -------------------------------------------------------------------------------------


def test_the_folder_keeps_the_newest_twenty_and_the_one_just_written(tmp_path):
    folder = tmp_path / "gateway" / "unexecuted"
    for i in range(UNEXECUTED_KEPT + 5):
        path = _keep_unexecuted(f"req-{i:02d}", [("write", f"arguments {i}")])
        assert path == str((folder / f"req-{i:02d}.txt").resolve())
        # Distinct ages whatever the clock's grain: the order the files were written in.
        os.utime(path, ns=(10**9 * (i + 1), 10**9 * (i + 1)))
    names = sorted(p.name for p in folder.iterdir())
    assert names == [f"req-{i:02d}.txt" for i in range(5, UNEXECUTED_KEPT + 5)]
    assert (folder / "req-24.txt").read_text(encoding="utf-8") == "--- tool call `write` ---\narguments 24"


def test_the_file_is_utf8_and_holds_every_cut_call(tmp_path):
    path = _keep_unexecuted("r1", [("write", "{\"t\": \"кусок"), ("edit", "{\"a")])
    assert open(path, "rb").read().decode("utf-8") == (
        "--- tool call `write` ---\n{\"t\": \"кусок\n\n--- tool call `edit` ---\n{\"a")


def test_a_request_id_that_is_not_a_file_name_is_made_one(tmp_path):
    path = _keep_unexecuted("a/b\\..:c", [("write", "x")])
    assert os.path.dirname(path) == str((tmp_path / "gateway" / "unexecuted").resolve())


def test_a_failed_replace_is_logged_leaves_no_temp_file_and_gives_no_address(tmp_path, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gateway_module.os, "replace", refuse)
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.gateway"):
        assert _keep_unexecuted("r1", [("write", "x")]) is None
    assert "was not kept: disk full" in caplog.text
    assert list((tmp_path / "gateway" / "unexecuted").iterdir()) == []


@pytest.mark.asyncio
async def test_when_the_file_cannot_be_kept_the_call_still_goes_and_the_note_has_no_address(tmp_path):
    (tmp_path / "gateway").write_text("a file where the folder should be", encoding="utf-8")
    text, _ = await _through_the_door(
        tmp_path, "llamacpp_server", CHAT_PATH, _chat(tools=TOOLS_OPENAI),
        calls=(WRITE_CUT,), finish="length", content=CONTENT,
    )
    shown, calls, finish, _ = _read("openai-whole", text)
    assert calls == [] and finish == "length"
    assert shown == f"the answer\n\n{NOTE_HEAD} {NOTE_TAIL}"


def test_the_words_come_from_what_the_result_says():
    call = {"type": "tool_use", "id": "c", "name": "write", "input": {}}
    out = lambda finish, ceiling: _without_the_cut_call("r", finish, "", [call], {0: "{\"a"}, ceiling)[2]  # noqa: E731
    assert "the output limit of 4096 tokens, inside" in out("length", 4096)
    assert "the output limit, inside" in out("max_tokens", None)
    assert "the output limit, inside" in out("LENGTH", True), "a bool is no count"
    assert out("stop", 4096) == "" and out(None, 4096) == "" and out("tool_calls", 4096) == ""
