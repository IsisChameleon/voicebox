#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Task H: listen() batches are time-ordered and paging stays lossless.

``app_bot_speech_*`` events carry ``t`` from frame construction while their
log position is where the observer saw them, so a stall between the two
appends them out of ``t`` order. The batch sort fixes the read; the cursor
stays append-order arithmetic so the sort can never skip or repeat an event.
"""

from voicebox.agent import PipecatMCPAgent
from voicebox.events import EventType, TesterTranscriptEvent, VoiceboxEvent
from voicebox.processors.nonblocking_whisper_stt import NonBlockingSegmentedSTT


def _agent_with_events(*events: VoiceboxEvent) -> PipecatMCPAgent:
    """Build a started agent whose log already holds ``events``."""
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]
    agent._started = True
    agent._stt = NonBlockingSegmentedSTT()  # idle queue: lag reads 0.0
    agent._events.extend(events)
    return agent


async def test_batch_sorted_by_t():
    # H1: events appended out of t order (a VAD-stamped frame observed late)
    # must read back as a conversation — sorted by t.
    agent = _agent_with_events(
        VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STOPPED, t=30.0),
        VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED, t=10.0),
        VoiceboxEvent(type=EventType.CLIENT_CONNECTED, t=20.0),
    )

    envelope = await agent.listen_events(timeout=0.01, cursor=0)

    assert [e["t"] for e in envelope["events"]] == [10.0, 20.0, 30.0]
    assert envelope["cursor"] == 3


async def test_equal_t_events_keep_append_order():
    # Criterion 2: the sort is stable, so simultaneous events don't swap.
    agent = _agent_with_events(
        TesterTranscriptEvent(text="first", t=5.0),
        TesterTranscriptEvent(text="second", t=5.0),
    )

    envelope = await agent.listen_events(timeout=0.01, cursor=0)

    assert [e["text"] for e in envelope["events"]] == ["first", "second"]


async def test_cursor_paging_lossless_under_sort():
    # H2: paging with successive cursors over an out-of-order log yields every
    # event exactly once — append order stays authoritative for the cursor.
    agent = _agent_with_events(
        VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STARTED, t=40.0),
        VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STOPPED, t=10.0),
        VoiceboxEvent(type=EventType.CLIENT_CONNECTED, t=30.0),
    )

    first = await agent.listen_events(timeout=0.01, cursor=0)
    # More events land after the first page — one with a t EARLIER than
    # everything already read (the late-transcript shape).
    agent._events.extend(
        [
            VoiceboxEvent(type=EventType.CLIENT_DISCONNECTED, t=5.0),
            VoiceboxEvent(type=EventType.SESSION_STOPPED, t=50.0),
        ]
    )
    second = await agent.listen_events(timeout=0.01, cursor=first["cursor"])

    collected = first["events"] + second["events"]
    assert sorted(e["t"] for e in collected) == [5.0, 10.0, 30.0, 40.0, 50.0]
    assert len(collected) == len(agent._events)  # nothing skipped, nothing repeated
    assert second["cursor"] == len(agent._events)
    # And each batch is individually time-ordered.
    assert [e["t"] for e in first["events"]] == [10.0, 30.0, 40.0]
    assert [e["t"] for e in second["events"]] == [5.0, 50.0]
