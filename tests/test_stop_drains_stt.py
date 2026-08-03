"""Task F: stop() drains the STT before writing artifacts; empty results are visible.

The field report lost a 44.8 s turn to teardown ordering; verification round 3
lost four turns (~100 s) to a prompt stop(). These tests pin the drain-before-
dump ordering, the bounded-drain guarantee, and the empty-transcript flag.
"""

import asyncio
import json
import time

import pytest

import voicebox.processors.nonblocking_whisper_stt as nbstt_module
from voicebox.agent import PipecatMCPAgent
from voicebox.events import EventType
from voicebox.processors.nonblocking_whisper_stt import NonBlockingSegmentedSTT


class _PipelineStub:
    """Absorbs the frames stop() and speak() push; no pipeline behind it."""

    async def queue_frames(self, frames):
        """Swallow queued frames."""

    async def queue_frame(self, frame):
        """Swallow the EndFrame stop() sends."""


class _LateTranscriptSTT(NonBlockingSegmentedSTT):
    """A transcription 'finishes' only while the drain is waiting on it."""

    def __init__(self, agent: PipecatMCPAgent):
        super().__init__()
        self._agent = agent

    async def drain(self, timeout: float) -> bool:
        """Deliver the pending transcript mid-drain, like a real late Whisper."""
        await self._agent._emit_app_bot_transcript(
            "the last thing the bot said", "2026-08-03T10:00:00.000+00:00"
        )
        return True


def _agent_mid_session(tmp_path) -> PipecatMCPAgent:
    """Build an agent that looks live, with a record_dir and no real pipeline."""
    agent = PipecatMCPAgent(transport=None, record_dir=str(tmp_path))  # type: ignore[arg-type]
    agent._started = True
    agent._pipeline_task = _PipelineStub()  # type: ignore[assignment]
    return agent


async def test_pending_transcript_reaches_artifacts(tmp_path):
    # F1: the report must be complete, not silently 44 s short — stop() waits
    # for in-flight transcription BEFORE writing events.json.
    agent = _agent_mid_session(tmp_path)
    agent._stt = _LateTranscriptSTT(agent)

    artifacts = await agent.stop()

    assert artifacts is not None
    events = json.load(open(artifacts["events"]))
    texts = [e["text"] for e in events if e["type"] == "app_bot_transcript"]
    assert "the last thing the bot said" in texts
    # Criterion 4: session_stopped is still emitted FIRST (so a pending
    # listen() returns cleanly); the drained transcript lands after it in the
    # log but inside the artifacts.
    types = [e["type"] for e in events]
    assert types.index("session_stopped") < types.index("app_bot_transcript")


async def test_empty_transcription_still_emits_event():
    # F2: "we tried and got nothing" must be distinguishable from "the bot
    # never spoke" — the old `if message.content:` gate swallowed it.
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]

    await agent._emit_app_bot_transcript("", "2026-08-03T10:00:00.000+00:00")

    event = agent._events[-1]
    assert event.type == EventType.APP_BOT_TRANSCRIPT
    assert event.text == ""  # type: ignore[attr-defined]
    assert event.transcription_empty is True  # type: ignore[attr-defined]


async def test_nonempty_transcription_is_not_flagged():
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]

    await agent._emit_app_bot_transcript("words", "2026-08-03T10:00:00.000+00:00")

    assert agent._events[-1].transcription_empty is False  # type: ignore[attr-defined]


async def test_empty_transcript_still_claims_a_vad_start():
    # The empty event must consume its speech interval's VAD start, or every
    # later transcript claims a neighbour's start (the D10 deque drifts by one
    # for the rest of the session).
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]
    agent._unclaimed_bot_speech_starts.append(100.0)
    agent._unclaimed_bot_speech_starts.append(200.0)

    await agent._emit_app_bot_transcript("", "fallback")
    await agent._emit_app_bot_transcript("words", "fallback")

    empty, spoken = agent._events[-2], agent._events[-1]
    assert empty.turn_started_at == "1970-01-01T00:01:40.000+00:00"  # type: ignore[attr-defined]
    assert spoken.turn_started_at == "1970-01-01T00:03:20.000+00:00"  # type: ignore[attr-defined]


async def test_stop_bounded_when_drain_stalls(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # F3: a transcription that never completes must not hang teardown — stop()
    # returns within the bounded drain window and still writes artifacts.
    monkeypatch.setattr(nbstt_module, "DRAIN_BASE_SECS", 0.2)
    monkeypatch.setattr(nbstt_module, "DRAIN_CAP_SECS", 0.3)
    agent = _agent_mid_session(tmp_path)
    stalled = NonBlockingSegmentedSTT()
    stalled._segments.put_nowait(b"x" * 32000)  # queued forever: no worker running
    stalled._waiting_since.append(time.time())
    stalled._pending_audio_bytes = 32000
    agent._stt = stalled

    t0 = time.monotonic()
    artifacts = await agent.stop()
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0
    assert artifacts is not None
    assert json.load(open(artifacts["events"]))  # events.json written regardless


async def test_drain_completes_when_queue_empties():
    # The real drain: returns True as soon as every queued segment is done.
    stt = NonBlockingSegmentedSTT()
    stt._segments.put_nowait(b"x")

    async def worker_finishes():
        await asyncio.sleep(0.1)
        stt._segments.get_nowait()
        stt._segments.task_done()

    asyncio.get_event_loop().create_task(worker_finishes())

    assert await stt.drain(timeout=2.0) is True


def test_drain_budget_scales_with_backlog():
    # Round 3 measured a 140 s transcript backlog; a flat ~15 s bound loses
    # data. One second of budget per queued audio second (~2x the measured
    # 0.55x-realtime decode), capped.
    stt = NonBlockingSegmentedSTT()
    assert stt.drain_budget_secs() == nbstt_module.DRAIN_BASE_SECS

    stt._pending_audio_bytes = 140 * 16000 * 2  # 140 s of 16 kHz mono PCM
    assert stt.drain_budget_secs() == pytest.approx(155.0)

    stt._pending_audio_bytes = 1000 * 16000 * 2
    assert stt.drain_budget_secs() == nbstt_module.DRAIN_CAP_SECS
