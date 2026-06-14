#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""No-browser logic test for the Stage 3 barge-in scheduling.

Exercises ``PipecatMCPAgent.speak``'s new modes (armed ``when`` trigger,
``wait_for_turn``, plain speak, validation) against the REAL event log and the
REAL agent methods. The only boundary stubbed out is ``_queue_speak_frames``
(the pipeline push) — everything else runs as in production.

Run with ``uv run python scripts/smoke_barge_in.py``; it exits 0 and prints
``ALL BARGE-IN LOGIC TESTS PASSED`` on success.
"""

import asyncio
from unittest.mock import MagicMock

from pipecat.frames.frames import VADUserStoppedSpeakingFrame

from voicebox.agent import PipecatMCPAgent
from voicebox.events import EventType, VoiceboxEvent


def _make_agent() -> tuple[PipecatMCPAgent, list[str]]:
    """Build an agent past start() with the pipeline boundary stubbed.

    Returns:
        The agent and a list that records every text passed to
        ``_queue_speak_frames``.

    """
    agent = PipecatMCPAgent(MagicMock())
    agent._started = True
    agent._pipeline_task = MagicMock()  # truthy so the guard passes
    agent._connected.set()

    calls: list[str] = []

    async def fake_queue(text: str):
        calls.append(text)

    agent._queue_speak_frames = fake_queue  # type: ignore[method-assign]
    return agent, calls


async def _test_armed_barge_in():
    """Armed trigger returns immediately and fires on the next event."""
    agent, calls = _make_agent()

    result = await agent.speak("hi", when="app_bot_speech_started", timer_secs=0.05)
    assert result == {"armed": True}, result
    assert "hi" not in calls, calls

    # The trigger arrives IMMEDIATELY — before the background task has run.
    # speak() snapshots the log position synchronously (not inside the task), so
    # this event is still caught. Pre-fix this ordering raced and dropped it.
    await agent._emit(VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED))
    await asyncio.sleep(0.2)
    assert "hi" in calls, calls

    types = [e.type for e in agent._events]
    order = [
        EventType.TESTER_BARGE_IN_ARMED.value,
        EventType.APP_BOT_SPEECH_STARTED.value,
        EventType.TESTER_BARGE_IN_FIRED.value,
        EventType.TESTER_TRANSCRIPT.value,
    ]
    positions = [types.index(t) for t in order]
    assert positions == sorted(positions), types
    print("  test 1 (armed barge-in) OK")


async def _test_wait_for_turn():
    """wait_for_turn blocks while the bot speaks and resumes when it stops."""
    agent, calls = _make_agent()
    agent._app_bot_speaking = True

    task = asyncio.create_task(agent.speak("later", wait_for_turn=True))
    await asyncio.sleep(0.1)
    assert not task.done(), "speak should still be blocked while bot speaks"
    assert "later" not in calls, calls

    # Flip the flag through the REAL pipeline path (exercises the toggle added in
    # _on_pipeline_frame), which also _emit()s, waking the condition waiter.
    await agent._on_pipeline_frame(VADUserStoppedSpeakingFrame())

    await task
    assert "later" in calls, calls
    print("  test 2 (wait_for_turn) OK")


async def _test_plain_speak():
    """Plain speak queues immediately and returns {'queued': True}."""
    agent, calls = _make_agent()
    result = await agent.speak("now")
    assert result == {"queued": True}, result
    assert "now" in calls, calls
    print("  test 3 (plain speak) OK")


async def _test_validation():
    """Bad 'when' and the when/wait_for_turn clash both raise ValueError."""
    agent, _ = _make_agent()

    try:
        await agent.speak("x", when="bogus")
        raise AssertionError("expected ValueError for unknown 'when'")
    except ValueError:
        pass

    try:
        await agent.speak("x", when="app_bot_speech_started", wait_for_turn=True)
        raise AssertionError("expected ValueError for when + wait_for_turn")
    except ValueError:
        pass
    print("  test 4 (validation) OK")


async def main():
    """Run every barge-in logic test in sequence."""
    await _test_armed_barge_in()
    await _test_wait_for_turn()
    await _test_plain_speak()
    await _test_validation()
    print("ALL BARGE-IN LOGIC TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
