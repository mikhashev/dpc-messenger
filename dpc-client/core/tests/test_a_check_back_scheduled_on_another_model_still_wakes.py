"""A task scheduled while a chat runs on another model still wakes, once, on that model.

Chat #114 (Zcode's PoC, Forge's task-c3ba5e13): a chat on a provider alias other than
the agent's own runs on a second DpcAgent the manager builds for that alias. That agent
built its own TaskQueue over the same task_queue.json and started no processor, so a
check_back, chat or reminder it scheduled answered «✓ Scheduled» and never ran; a save
by either agent wrote its own list over the other's (a task erased, a finished one
brought back); the one-check_back-per-chat rule and the Task Board's cancel each saw one
list. Now there is one queue object and one processor per agent root; a task records
the alias it was scheduled on and wakes on that alias's agent; a queue nothing runs is
refused; and a task that was already more than OVERDUE_SKIP_AFTER_SEC late when the queue
loaded it from disk (its time passed while the backend was down) is skipped with a line in
its chat (Mike, 2026-11-25). One that fell late in a running process still runs.

Real code under test: DpcAgentManager (start, stop, the per-alias agent, the task
routing, the notice), TaskQueue (file, processor, overdue skip), DpcAgent.schedule_task
/ start_task_processor / _execute_queued_task / _post_overdue_notice / _execute_task,
tools.core.schedule_task, AgentService.cancel_agent_task, the real ConversationMonitor.
Stood in for: the model, memory and tools of DpcAgent (the class below keeps the queue
lines of DpcAgent.__init__) and the approval card. Nothing reaches the real ~/.dpc:
`Path.home()` is a temporary directory.
"""

import asyncio
import json
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.agent_service import AgentService
from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
from dpc_client_core.dpc_agent.task_queue import OVERDUE_SKIP_AFTER_SEC, TaskQueue
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools.core import NO_TASK_PROCESSOR_REFUSAL, schedule_task
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_manager as am

AGENT = "agent_wake00001"
ALIAS = "strata_wake"
CONV = AGENT  # the 1:1 chat with an agent is named by the agent's id
GROUP = "group-5e1c0d7a9b22"
NODE = "dpc-node-0000000000000000000000000000cafe"
RAN: list = []
BOUND = 6.0  # the processor polls every second; no wait here comes near this


class _Events:
    async def emit(self, *args, **kwargs):
        return None


class _Agent:
    """DpcAgent without its model, memory and tools. The queue lines are the real ones."""

    schedule_task = DpcAgent.schedule_task
    start_task_processor = DpcAgent.start_task_processor
    stop_task_processor = DpcAgent.stop_task_processor
    archive_old_task_results = DpcAgent.archive_old_task_results

    def __init__(self, config=None, agent_root=None, provider_alias=None, service=None,
                 run_gate=None, task_queue=None, **_):
        self.config = config
        self.agent_root = pathlib.Path(agent_root)
        self._run_gate = run_gate
        self._service = service
        self._task_handlers = {}
        self._telegram_send_fn = None
        self._task_progress_scope = None
        self._event_loop = None
        self.display_name = "Forge"
        self.provider_alias = provider_alias
        # DpcAgent.__init__, the queue lines
        self.queue = task_queue if task_queue is not None else TaskQueue(self.agent_root)
        self._queue_enabled = self.config.enable_task_queue
        self._wake_alias = None
        self._task_agent_for = None
        self._task_notice_fn = None
        self.events = _Events()

    async def _execute_task(self, task):
        """What a run would do: record which agent object ran it instead of calling a model."""
        RAN.append((self.provider_alias or "default", task.id, task.task_type))
        return "done"


@pytest.fixture(autouse=True)
def _fresh():
    RAN.clear()


@pytest.fixture
def approval(monkeypatch):
    asked = []

    def _card(ctx, *, task_type, when, about, music=None, void_on_stop=False):
        asked.append((task_type, when, about))
        return True, "approved (a stand-in for the owner's card)"

    monkeypatch.setattr(tools_core, "_await_schedule_approval", _card)
    return asked


def _service():
    return SimpleNamespace(
        llm_manager=SimpleNamespace(providers={ALIAS: object()}),
        settings=None,
        telegram_manager=None,
        firewall=None,
        p2p_manager=SimpleNamespace(node_id=NODE),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        send_group_agent_message=AsyncMock(return_value="msg-1"),
    )


def _new_manager(monkeypatch, cls=_Agent):
    monkeypatch.setattr(am, "DpcAgent", cls)
    mgr = am.DpcAgentManager(_service(), {"context_window": 32768}, agent_id=AGENT)
    mgr._agent_display_name = "Forge"
    mgr._deliver_telegram_result = AsyncMock()  # wired into the agents from here

    async def _no_memory_indexes():
        pass

    monkeypatch.setattr(mgr, "_init_memory_indexes", _no_memory_indexes)
    return mgr


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    AgentRegistry().register_agent(AGENT, "Wake test agent")
    monkeypatch.setattr(run_gate, "_RUN_GATES", {})
    return tmp_path


@pytest.fixture
def managers(home, monkeypatch):
    made = []

    def _make(cls=_Agent):
        mgr = _new_manager(monkeypatch, cls)
        made.append(mgr)
        return mgr

    yield _make
    for mgr in made:
        for agent in [mgr._agent, *mgr._agents.values()]:
            try:
                if agent is not None:
                    agent.queue.stop_processor()
            except RuntimeError:
                pass


@pytest.fixture
def manager(managers):
    return managers()


def _ctx(agent, conv=CONV, telegram=None):
    ctx = ToolContext(agent_root=agent.agent_root, current_task_id=conv, message_source="ui")
    ctx._agent = agent
    if telegram:
        ctx.reply_telegram_chat_id = telegram
    return ctx


def _schedule(agent, conv=CONV, text="wake up", delay=0, task_type="check_back", telegram=None):
    data = {"text": text, "message": text}
    return schedule_task(_ctx(agent, conv, telegram), task_type, json.dumps(data), delay_seconds=delay)


def _last_id(queue):
    return queue._queue[-1].id


def _file(queue):
    data = json.loads(queue.queue_file.read_text(encoding="utf-8"))
    return {t["id"]: t["status"] for t in data["tasks"]}


async def _until(condition, what):
    deadline = asyncio.get_running_loop().time() + BOUND
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def _write_queue_file(home, *tasks):
    path = home / ".dpc" / "agents" / AGENT / "state" / "task_queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": list(tasks)}), encoding="utf-8")
    return path


def _due(minutes_ago, task_id, task_type="check_back", conv=CONV, **extra):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    data = {"text": "see if the render is done", "_reply_conversation_id": conv,
            "_message_source": "ui", **extra}
    return {"id": task_id, "task_type": task_type, "data": data, "priority": "normal",
            "status": "pending", "scheduled_at": at.isoformat()}


# -- one queue, one processor ---------------------------------------------------------


def test_the_real_agent_takes_the_queue_it_is_given(tmp_path):
    """The stand-in above keeps DpcAgent's queue lines; this holds the real class to them."""
    root = tmp_path / "agents" / AGENT
    (root / "state").mkdir(parents=True)
    queue = TaskQueue(root)
    agent = DpcAgent(llm_manager=SimpleNamespace(providers={}), config=AgentConfig(),
                     agent_root=root, task_queue=queue)
    assert agent.queue is queue
    assert agent._wake_alias is None and agent._task_agent_for is None


@pytest.mark.asyncio
async def test_the_alias_agent_shares_the_default_agents_queue_and_wiring(manager):
    await manager.start()
    default, alias = manager.agent, manager._get_or_create_agent_for_provider(ALIAS)
    assert alias.queue is default.queue
    assert default.queue.is_running()
    assert alias._wake_alias == ALIAS and default._wake_alias is None
    # What a task run needs: chat progress and Stop (the scope), Telegram, the notice.
    assert alias._task_progress_scope == manager._task_progress_scope
    assert alias._telegram_send_fn is manager._deliver_telegram_result
    assert alias._task_notice_fn == manager._post_task_notice


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["check_back", "chat", "reminder"])
async def test_a_task_scheduled_on_the_alias_runs_once_on_the_alias(manager, approval, task_type):
    await manager.start()
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    out = _schedule(alias, task_type=task_type)
    assert out.startswith("✓ Scheduled"), out
    task_id = _last_id(alias.queue)
    assert alias.queue.get_task(task_id).data["_provider_alias"] == ALIAS

    await _until(lambda: RAN, "the task to run")
    await asyncio.sleep(1.5)  # a second poll would run it again
    assert RAN == [(ALIAS, task_id, task_type)]


@pytest.mark.asyncio
async def test_a_task_scheduled_on_the_default_agent_runs_on_the_default_agent(manager, approval):
    await manager.start()
    manager._get_or_create_agent_for_provider(ALIAS)
    _schedule(manager.agent, text="control")
    task_id = _last_id(manager.agent.queue)
    assert "_provider_alias" not in manager.agent.queue.get_task(task_id).data
    await _until(lambda: RAN, "the task to run")
    assert RAN == [("default", task_id, "check_back")]


@pytest.mark.asyncio
async def test_the_model_cannot_name_the_alias_its_task_wakes_on(manager, approval):
    await manager.start()
    manager._get_or_create_agent_for_provider(ALIAS)
    data = json.dumps({"text": "x", "_provider_alias": ALIAS})
    schedule_task(_ctx(manager.agent), "check_back", data, delay_seconds=3600)
    assert "_provider_alias" not in manager.agent.queue._queue[-1].data


@pytest.mark.asyncio
async def test_a_task_pending_when_the_alias_agent_is_built_runs_once(manager, approval):
    """Zcode's X1: two processors over one file ran such a task twice."""
    await manager.start()
    default = manager.agent
    _schedule(default, text="X", delay=3600)
    x = _last_id(default.queue)
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    await alias.start_task_processor()  # the alias agent's queue is off: starts nothing
    default.queue.get_task(x).scheduled_at = None  # X's time comes
    await _until(lambda: RAN, "X to run")
    await asyncio.sleep(1.5)
    assert [r[1] for r in RAN] == [x]


@pytest.mark.asyncio
async def test_saves_by_either_agent_neither_erase_nor_bring_back_a_task(managers, approval, home):
    manager = managers()
    await manager.start()
    default = manager.agent
    _schedule(default, text="X")
    x = _last_id(default.queue)
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    await _until(lambda: [r[1] for r in RAN] == [x], "X to run")

    # A save by the alias agent does not bring X back.
    _schedule(alias, conv=f"{CONV}-2", text="Y", delay=3600)
    y = _last_id(alias.queue)
    assert x not in _file(alias.queue)
    # A save by the default agent does not erase Y.
    _schedule(default, conv=f"{CONV}-3", text="Z", delay=3600)
    assert y in _file(default.queue)

    # The next start loads Y and Z, not X.
    manager.agent.queue.stop_processor()
    assert sorted(t.id for t in TaskQueue(manager.agent_root)._queue) == sorted(
        [y, _last_id(default.queue)])


@pytest.mark.asyncio
async def test_one_check_back_per_chat_counts_tasks_from_either_agent(manager, approval):
    await manager.start()
    default = manager.agent
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    assert _schedule(default, text="A", delay=3600).startswith("✓")
    assert _schedule(alias, text="B", delay=3600).startswith("⚠️ A check_back is already queued")

    other = f"{CONV}-other"
    assert _schedule(alias, conv=other, text="C", delay=3600).startswith("✓")
    assert _schedule(default, conv=other, text="D", delay=3600).startswith("⚠️ A check_back is already queued")


@pytest.mark.asyncio
async def test_the_task_board_cancels_a_task_scheduled_on_the_alias(manager, approval):
    await manager.start()
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    _schedule(alias, text="A", delay=3600)
    task_id = _last_id(alias.queue)

    board = AgentService.__new__(AgentService)
    board.llm_manager = SimpleNamespace(
        providers={"dpc_agent": SimpleNamespace(_managers={AGENT: manager})})
    answer = await AgentService.cancel_agent_task(board, agent_id=AGENT, task_id=task_id)
    assert answer["status"] == "success", answer
    assert task_id not in _file(alias.queue)
    # The chat is free again.
    assert _schedule(alias, text="B", delay=3600).startswith("✓")


# -- a queue nothing runs is refused ------------------------------------------------


@pytest.mark.asyncio
async def test_a_queue_with_no_running_processor_is_refused_before_any_card(home, approval):
    root = home / ".dpc" / "agents" / AGENT
    agent = _Agent(config=AgentConfig(enable_task_queue=False), agent_root=root)
    for task_type in ("check_back", "chat", "reminder"):
        out = _schedule(agent, task_type=task_type)
        assert out == NO_TASK_PROCESSOR_REFUSAL, out
        assert "not scheduled: this agent has no running task processor" in out.lower()
    assert agent.queue._queue == []
    assert approval == [], "the person was asked to approve a task nothing would run"


@pytest.mark.asyncio
async def test_after_the_manager_stops_the_alias_agent_refuses(manager, approval):
    await manager.start()
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    await manager.stop()
    assert _schedule(alias) == NO_TASK_PROCESSOR_REFUSAL
    assert alias.queue._queue == []


@pytest.mark.asyncio
async def test_an_alias_agent_built_before_the_start_takes_the_default_queue(manager, approval):
    """With no default agent there is no queue to share; the start hands it over."""
    early = manager._get_or_create_agent_for_provider(ALIAS)
    assert _schedule(early) == NO_TASK_PROCESSOR_REFUSAL
    await manager.start()
    assert early.queue is manager.agent.queue
    assert _schedule(early).startswith("✓")
    await _until(lambda: RAN, "the task to run")
    assert RAN[0][0] == ALIAS


# -- the alias after a restart ----------------------------------------------------------


@pytest.mark.asyncio
async def test_after_a_restart_the_task_wakes_on_a_rebuilt_alias_agent(managers, home):
    _write_queue_file(home, _due(1, "task-alias0001", _provider_alias=ALIAS))
    manager = managers()
    await manager.start()
    await _until(lambda: RAN, "the task to run")
    assert RAN == [(ALIAS, "task-alias0001", "check_back")]
    assert ALIAS in manager._agents


@pytest.mark.asyncio
async def test_a_task_whose_alias_is_gone_runs_on_the_default_agent_and_says_so(managers, home, caplog):
    _write_queue_file(home, _due(1, "task-gone0001", _provider_alias="removed_alias"))
    manager = managers()
    with caplog.at_level("WARNING", logger=am.log.name):
        await manager.start()
        await _until(lambda: RAN, "the task to run")
    assert RAN == [("default", "task-gone0001", "check_back")]
    assert any("removed_alias" in r.getMessage() and "no longer exists" in r.getMessage()
               for r in caplog.records)


# -- overdue ------------------------------------------------------------------------


def _history(home, conv):
    path = home / ".dpc" / "conversations" / conv / "history.json"
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def _hhmm(minutes_ago):
    """The time as the notice writes it: the date only when it is not today's."""
    now = datetime.now(timezone.utc)
    at = now - timedelta(minutes=minutes_ago)
    return at.strftime("%H:%M" if at.date() == now.date() else "%Y-%m-%d %H:%M")


def test_ten_minutes_is_the_named_limit():
    assert OVERDUE_SKIP_AFTER_SEC == 600


@pytest.mark.asyncio
async def test_a_check_back_37_minutes_late_is_skipped_with_a_line_in_its_chat(managers, home):
    path = _write_queue_file(home, _due(37, "task-late0001", _reply_telegram_chat_id="4242"))
    manager = managers()
    await manager.start()
    await _until(lambda: (home / ".dpc" / "conversations" / CONV / "history.json").exists(),
                 "the notice in the chat")
    await asyncio.sleep(1.2)
    assert RAN == [], "an overdue task ran"

    line = f"A check_back scheduled for {_hhmm(37)} UTC was skipped: it was 37 min overdue."
    records = _history(home, CONV)
    assert [r["role"] for r in records] == ["user", "assistant"], records
    assert records[0]["content"] == "see if the render is done"
    assert records[0]["sender_name"] == "Scheduled check_back"
    assert records[1]["content"] == line
    assert records[1]["turn_end"] == "guard"
    pushed = [c.args[1] for c in manager.service.local_api.broadcast_event.call_args_list
              if c.args[0] == "agent_history_updated"]
    assert pushed and pushed[-1]["messages"][-1]["content"] == line
    manager._deliver_telegram_result.assert_awaited_once_with("4242", line)

    # Not run, not retried, not waiting: failed with the reason, on disk too.
    assert manager.agent.queue._queue == []
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"] == []


@pytest.mark.asyncio
async def test_an_overdue_group_task_posts_its_line_into_the_group(managers, home):
    _write_queue_file(home, _due(25, "task-late0002", task_type="reminder", conv=GROUP))
    manager = managers()
    await manager.start()
    send = manager.service.send_group_agent_message
    await _until(lambda: send.await_count, "the group post")
    assert RAN == []
    kwargs = send.await_args.kwargs
    assert kwargs["group_id"] == GROUP and kwargs["agent_name"] == "Forge"
    assert kwargs["text"] == f"A reminder scheduled for {_hhmm(25)} UTC was skipped: it was 25 min overdue."
    assert not (home / ".dpc" / "conversations" / GROUP).exists()


@pytest.mark.asyncio
async def test_a_task_nine_minutes_late_still_runs(managers, home):
    _write_queue_file(home, _due(9, "task-late0003"))
    manager = managers()
    await manager.start()
    await _until(lambda: RAN, "the task to run")
    assert RAN == [("default", "task-late0003", "check_back")]
    assert not (home / ".dpc" / "conversations" / CONV / "history.json").exists()


def _queue_loaded_with(root, *tasks):
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "task_queue.json").write_text(json.dumps({"tasks": list(tasks)}),
                                                    encoding="utf-8")
    return TaskQueue(root)


def test_the_skip_at_load_is_a_failure_with_its_reason_and_no_retry(tmp_path):
    queue = _queue_loaded_with(tmp_path, _due(11, "task-load0001"), _due(9, "task-load0002"))
    failed = []
    queue.set_callbacks(on_task_failed=failed.append)
    late, near = queue.get_task("task-load0001"), queue.get_task("task-load0002")
    assert queue._skip_if_overdue(late) is True
    assert late.status == "failed" and late.error.startswith("overdue: 11 min late")
    assert late.retry_count == 0 and failed == [late]
    assert [t.id for t in queue._queue] == ["task-load0002"]
    assert queue._skip_if_overdue(near) is False, "9 min late at load is within the limit"


def test_a_task_that_fell_late_in_a_running_process_is_not_skipped(tmp_path):
    """The processor was busy past its time: no restart came between, so it runs."""
    queue = TaskQueue(tmp_path)
    task = queue.schedule("check_back", {"text": "x"},
                          scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=40))
    assert queue._skip_if_overdue(task) is False


@pytest.mark.asyncio
async def test_a_task_late_because_the_processor_was_busy_still_runs(manager, approval):
    await manager.start()
    queue = manager.agent.queue
    queue.stop_processor()  # the processor is held elsewhere while the task's time passes
    task_id = queue.schedule("check_back", {"text": "late", "_reply_conversation_id": CONV},
                             scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=30)).id
    await manager.agent.start_task_processor()  # the same process: nothing was reloaded
    await _until(lambda: RAN, "the late task to run")
    assert RAN == [("default", task_id, "check_back")]


@pytest.mark.asyncio
async def test_a_retry_rescheduled_into_the_past_still_runs(managers, home):
    """A retry is the queue's own reschedule (mark_failed), not a time the process slept through."""
    attempts = []

    async def _fail_then_succeed(self, task):
        attempts.append(task.id)
        if len(attempts) == 1:
            raise RuntimeError("provider flapped")
        RAN.append(("default", task.id, task.task_type))
        return "done"

    manager = managers(type("_FlakyAgent", (_Agent,), {"_execute_task": _fail_then_succeed}))
    await manager.start()
    task = manager.agent.queue.schedule("check_back", {"text": "x", "_reply_conversation_id": CONV})
    await _until(lambda: attempts, "the first attempt")
    await _until(lambda: task.status == "pending" and task.retry_count == 1, "the retry")
    # The backoff put it 30 s ahead; push it far behind, as a long stall would.
    task.scheduled_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    await _until(lambda: RAN, "the retry to run")
    assert RAN == [("default", task.id, "check_back")]


# -- the run on the alias shows itself, stops and reaches Telegram ------------------


class _RunningAgent(_Agent):
    """The real task executor on the stand-in; `process` answers and keeps what it got."""

    _execute_task = DpcAgent._execute_task
    _execute_task_guarded = DpcAgent._execute_task_guarded
    _convert_task_data_to_prompt = DpcAgent._convert_task_data_to_prompt
    runs: list = []

    async def process(self, message, conversation_id, **kwargs):
        _RunningAgent.runs.append((self.provider_alias or "default", conversation_id, kwargs))
        self._last_usage = {"thinking": f"thought on {self.provider_alias or 'default'}"}
        self._last_trace = {}
        return f"answer from {self.provider_alias or 'default'}"


@pytest.mark.asyncio
async def test_a_check_back_on_the_alias_shows_progress_can_stop_and_reaches_telegram(managers, approval, home):
    _RunningAgent.runs = []
    manager = managers(_RunningAgent)
    await manager.start()
    alias = manager._get_or_create_agent_for_provider(ALIAS)
    alias._service = manager.service
    _schedule(alias, telegram="777")
    await _until(lambda: (home / ".dpc" / "conversations" / CONV / "history.json").exists()
                 and len(_history(home, CONV)) == 2, "the run's record")

    [(ran_on, conv, kwargs)] = _RunningAgent.runs
    assert ran_on == ALIAS and conv == CONV
    assert {"emit_progress", "on_stream_chunk", "stop_event", "kill_event"} <= set(kwargs)
    prompt, answer = _history(home, CONV)
    assert answer["content"] == f"answer from {ALIAS}"
    assert answer["thinking"] == f"thought on {ALIAS}", "the record read another agent's run"
    manager._deliver_telegram_result.assert_awaited_once_with("777", f"answer from {ALIAS}")
