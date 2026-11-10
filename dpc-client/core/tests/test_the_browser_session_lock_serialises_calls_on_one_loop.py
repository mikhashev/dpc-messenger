"""The per-agent browser lock is keyed on the running loop it was made for."""
import asyncio

from dpc_client_core.dpc_agent.tools import browser


def _clear():
    browser._session_locks.clear()


def test_two_concurrent_calls_on_one_loop_never_overlap():
    _clear()
    state = {"now": 0, "peak": 0}

    async def call():
        async with browser._get_session_lock("agent-a"):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1

    async def main():
        await asyncio.gather(call(), call(), call())

    asyncio.run(main())
    assert state["peak"] == 1


def test_an_uncontended_lock_is_reused_not_replaced():
    _clear()

    async def main():
        first = browser._get_session_lock("agent-a")
        async with first:
            pass
        assert browser._get_session_lock("agent-a") is first

    asyncio.run(main())


def test_a_different_loop_gets_a_different_lock():
    _clear()
    seen = []

    async def grab():
        seen.append(browser._get_session_lock("agent-a"))

    asyncio.run(grab())
    asyncio.run(grab())
    assert seen[0] is not seen[1]


def test_locks_of_two_agents_are_separate():
    _clear()

    async def main():
        assert browser._get_session_lock("a") is not browser._get_session_lock("b")

    asyncio.run(main())
