#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Unit tests for T5: connection-state and deadline coherence for ``speak``.

These cover the parts that need no pipeline, no browser and no models:

  * the shared timeout arithmetic (child budget strictly below parent deadline);
  * the stateful connection machine (``_wait_for_connection`` set/clear);
  * ``speak`` refusing to talk into a dead transport without emitting a
    ``tester_transcript`` or queueing frames;
  * an expired ``wait_for_turn`` resolving with an error and never queueing
    frames (the no-ghost-speech invariant);
  * an armed trigger firing while disconnected logging ``tester_barge_in_dropped``
    instead of speaking, and firing normally when connected.

The pipeline is never started: ``PipecatMCPAgent.__init__`` wires up only asyncio
primitives, so we construct the agent, mark it started, and stub
``_queue_speak_frames`` (the single seam where audio would be produced). Style
follows ``tests/test_metrics.py`` / ``tests/test_readiness.py`` — plain pytest,
synthetic state, documented scenarios.
"""

import asyncio

import pytest

import voicebox.agent as agent_module
from voicebox.agent import PipecatMCPAgent
from voicebox.events import EventType, VoiceboxEvent
from voicebox.timeouts import (
    APP_BOT_SILENCE_TIMEOUT_SECS,
    CONNECT_GRACE_SECS,
    IPC_MARGIN_SECS,
    PLAYOUT_SETTLE_SECS,
    PLAYOUT_TIMEOUT_SECS,
    speak_child_budget,
    speak_parent_deadline,
)


# --------------------------------------------------------------------------- #
# Timeout arithmetic: the child must always give up before the parent.
# --------------------------------------------------------------------------- #


def test_parent_deadline_exceeds_child_budget_by_margin():
    # For every speak shape, the parent deadline is exactly the child's total
    # budget plus the IPC margin — so the child resolves the command first.
    for wait_for_turn in (False, True):
        for wait_for_playout in (False, True):
            child = speak_child_budget(wait_for_turn, wait_for_playout)
            parent = speak_parent_deadline(wait_for_turn, wait_for_playout)
            assert parent == pytest.approx(child + IPC_MARGIN_SECS)
            assert child < parent


def test_child_budget_accumulates_per_wait():
    # Plain speak only budgets the connection grace; each opt-in wait adds its
    # own ceiling on top.
    assert speak_child_budget(False, False) == pytest.approx(CONNECT_GRACE_SECS)
    assert speak_child_budget(True, False) == pytest.approx(
        CONNECT_GRACE_SECS + APP_BOT_SILENCE_TIMEOUT_SECS
    )
    assert speak_child_budget(False, True) == pytest.approx(
        CONNECT_GRACE_SECS + PLAYOUT_TIMEOUT_SECS + PLAYOUT_SETTLE_SECS
    )
    assert speak_child_budget(True, True) == pytest.approx(
        CONNECT_GRACE_SECS
        + APP_BOT_SILENCE_TIMEOUT_SECS
        + PLAYOUT_TIMEOUT_SECS
        + PLAYOUT_SETTLE_SECS
    )


# --------------------------------------------------------------------------- #
# Test agent construction (no pipeline).
# --------------------------------------------------------------------------- #


def _make_agent():
    """Build an agent without starting its pipeline; stub the audio seam.

    Returns the agent and the list that ``_queue_speak_frames`` appends to, so a
    test can assert whether audio would have been produced.
    """
    agent = PipecatMCPAgent(transport=object(), record_dir=None)  # type: ignore[arg-type]
    agent._started = True
    agent._pipeline_task = object()  # type: ignore[assignment]  # truthy; never used (stubbed)

    queued: list[str] = []

    async def _fake_queue(text: str):
        queued.append(text)

    agent._queue_speak_frames = _fake_queue  # type: ignore[method-assign]
    return agent, queued


def _has_event(agent: PipecatMCPAgent, type_value: str) -> bool:
    return any(e.type == type_value for e in agent._events)


# --------------------------------------------------------------------------- #
# Stateful connection machine.
# --------------------------------------------------------------------------- #


def test_wait_for_connection_returns_when_connected():
    async def run():
        agent, _ = _make_agent()
        agent._connected.set()
        # Returns promptly, no raise.
        await asyncio.wait_for(agent._wait_for_connection(1.0), timeout=1.0)

    asyncio.run(run())


def test_wait_for_connection_times_out_when_cleared():
    # set() then clear() models a disconnect: the connection is no longer live,
    # so the wait fails with the named error.
    async def run():
        agent, _ = _make_agent()
        agent._connected.set()
        agent._connected.clear()
        with pytest.raises(RuntimeError, match="no browser client connected"):
            await agent._wait_for_connection(0.05)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# speak: refuse into a dead transport; succeed on a live one.
# --------------------------------------------------------------------------- #


def test_speak_refused_when_disconnected_emits_no_transcript(monkeypatch):
    # No client connected: speak waits the grace period, then fails with the
    # named error — and crucially logs NO tester_transcript and queues NO audio.
    monkeypatch.setattr(agent_module, "CONNECT_GRACE_SECS", 0.05)

    async def run():
        agent, queued = _make_agent()
        with pytest.raises(RuntimeError, match="has the page called getUserMedia"):
            await agent.speak("into the void")
        assert not _has_event(agent, EventType.TESTER_TRANSCRIPT.value)
        assert queued == []

    asyncio.run(run())


def test_speak_succeeds_when_connected():
    # A connected client: speak logs the ground-truth transcript and queues the
    # frames exactly once.
    async def run():
        agent, queued = _make_agent()
        agent._connected.set()
        result = await agent.speak("hello there")
        assert result == {"queued": True}
        assert _has_event(agent, EventType.TESTER_TRANSCRIPT.value)
        assert queued == ["hello there"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# wait_for_turn: expiry is an error, never a ghost utterance.
# --------------------------------------------------------------------------- #


def test_wait_for_turn_expiry_errors_and_never_queues(monkeypatch):
    # Connected, but the app bot never falls silent: the child's silence wait
    # expires and speak errors WITHOUT emitting tester_transcript or queueing
    # audio — the no-ghost-speech invariant (finding F4).
    monkeypatch.setattr(agent_module, "APP_BOT_SILENCE_TIMEOUT_SECS", 0.1)

    async def run():
        agent, queued = _make_agent()
        agent._connected.set()
        agent._app_bot_speaking = True  # never goes silent
        with pytest.raises(RuntimeError, match="did not fall silent"):
            await agent.speak("polite interjection", wait_for_turn=True)
        assert not _has_event(agent, EventType.TESTER_TRANSCRIPT.value)
        assert not _has_event(agent, EventType.TESTER_SPEECH_STARTED.value)
        assert queued == []

    asyncio.run(run())


def test_wait_for_turn_speaks_once_bot_silent():
    # Connected and the bot is already silent: wait_for_turn speaks immediately.
    async def run():
        agent, queued = _make_agent()
        agent._connected.set()
        agent._app_bot_speaking = False
        result = await agent.speak("go ahead", wait_for_turn=True)
        assert result == {"queued": True}
        assert queued == ["go ahead"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Armed triggers: respect connection rules when they fire.
# --------------------------------------------------------------------------- #


def test_armed_trigger_dropped_when_disconnected(monkeypatch):
    # The when-event is already present so the trigger fires at once; with no
    # client connected it logs tester_barge_in_dropped and speaks nothing.
    monkeypatch.setattr(agent_module, "CONNECT_GRACE_SECS", 0.05)

    async def run():
        agent, queued = _make_agent()
        await agent._emit(VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED))
        # start=0 so the pre-existing event satisfies the trigger's wait.
        await agent._armed_speak(
            text="barge in", when=EventType.APP_BOT_SPEECH_STARTED.value, timer_secs=0.0, start=0
        )
        assert _has_event(agent, EventType.TESTER_BARGE_IN_DROPPED.value)
        assert not _has_event(agent, EventType.TESTER_BARGE_IN_FIRED.value)
        assert not _has_event(agent, EventType.TESTER_TRANSCRIPT.value)
        assert queued == []

    asyncio.run(run())


def test_armed_trigger_fires_when_connected():
    # Same trigger, but connected: it fires, logs the transcript and speaks.
    async def run():
        agent, queued = _make_agent()
        agent._connected.set()
        await agent._emit(VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED))
        await agent._armed_speak(
            text="barge in", when=EventType.APP_BOT_SPEECH_STARTED.value, timer_secs=0.0, start=0
        )
        assert _has_event(agent, EventType.TESTER_BARGE_IN_FIRED.value)
        assert _has_event(agent, EventType.TESTER_TRANSCRIPT.value)
        assert queued == ["barge in"]

    asyncio.run(run())
