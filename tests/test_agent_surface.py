import asyncio

import pytest

import voicebox.agent as agent_module
from voicebox.agent import PipecatMCPAgent


class _NoopPipelineTask:
    """Swallows the frames speak() queues; nothing ever plays out."""

    def __init__(self):
        self.queued: list = []

    async def queue_frames(self, frames):
        """Record the frames instead of pushing them into a pipeline."""
        self.queued.extend(frames)


def _agent_ready_to_speak() -> PipecatMCPAgent:
    """Build an agent with a live-looking session but no pipeline behind it."""
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]
    agent._started = True
    agent._connected.set()
    agent._pipeline_task = _NoopPipelineTask()  # type: ignore[assignment]
    return agent


async def test_playout_timeout_returns_diagnostic(monkeypatch: pytest.MonkeyPatch):
    # E2: audio that never plays out must come back as a diagnosis the caller
    # can act on. It used to raise, which said nothing about which half failed.
    monkeypatch.setattr(agent_module, "PLAYOUT_TIMEOUT_SECS", 0.2)
    agent = _agent_ready_to_speak()

    result = await agent.speak("hello", wait_for_playout=True)

    assert result["queued"] is True
    assert result["played"] is False
    assert "0.2" in result["reason"] and "listen()" in result["reason"]
    # The speech really was queued — "played: false" is not "nothing happened".
    assert len(agent._pipeline_task.queued) == 3  # type: ignore[union-attr]


async def test_playout_timeout_does_not_raise(monkeypatch: pytest.MonkeyPatch):
    # The same story from the caller's side: no exception to catch, ever.
    monkeypatch.setattr(agent_module, "PLAYOUT_TIMEOUT_SECS", 0.2)
    agent = _agent_ready_to_speak()

    try:
        await agent.speak("hello", wait_for_playout=True)
    except Exception as e:  # pragma: no cover - the assertion is the point
        pytest.fail(f"speak(wait_for_playout=True) raised {e!r} instead of reporting")


async def test_successful_playout_reports_played(monkeypatch: pytest.MonkeyPatch):
    # played=True is what makes the flag readable: present either way, so a
    # caller branches on it rather than on the absence of a key.
    monkeypatch.setattr(agent_module, "PLAYOUT_TIMEOUT_SECS", 5.0)
    monkeypatch.setattr(agent_module, "PLAYOUT_SETTLE_SECS", 0.1)
    agent = _agent_ready_to_speak()

    async def play_out_shortly():
        await asyncio.sleep(0.1)
        agent._playout.on_started(1.0)  # type: ignore[union-attr]
        agent._playout.on_stopped(2.0)  # type: ignore[union-attr]

    task = asyncio.create_task(play_out_shortly())
    result = await agent.speak("hello", wait_for_playout=True)
    await task

    assert result == {
        "queued": True,
        "played": True,
        "started_at": 1.0,
        "finished_at": 2.0,
        "interrupted": False,
    }


async def test_listen_envelope_reports_transcription_lag():
    # E3: listen() must let a caller tell "still transcribing" from "nothing
    # was said" — an empty events list with a non-zero lag means wait, not stop.
    from voicebox.processors.nonblocking_whisper_stt import NonBlockingSegmentedSTT

    class _StubSTT(NonBlockingSegmentedSTT):
        @property
        def transcription_lag_secs(self) -> float:
            return 2.71828

    agent = _agent_ready_to_speak()
    agent._stt = _StubSTT()

    envelope = await agent.listen_events(timeout=0.01, cursor=0)

    assert envelope["transcription_lag_secs"] == 2.718
    assert envelope["events"] == [] and envelope["cursor"] == 0


async def test_transcript_turn_started_at_uses_observed_vad_start():
    # Round 2: the aggregator stamps *transcript arrival* as the turn start
    # whenever a monologue chunk VAD-starts while the previous chunk is still
    # in Whisper (off by up to 103 s live). voicebox's own VAD log is the
    # truth; each transcript claims the earliest unclaimed start.
    agent = _agent_ready_to_speak()
    agent._unclaimed_bot_speech_starts.append(100.0)
    agent._unclaimed_bot_speech_starts.append(200.0)

    await agent._emit_app_bot_transcript("first chunk", "1970-01-01T00:99:99")

    event = agent._events[-1]
    assert event.turn_started_at == "1970-01-01T00:01:40.000+00:00"  # epoch 100.0
    assert list(agent._unclaimed_bot_speech_starts) == [200.0]  # claimed exactly one


async def test_transcript_turn_started_at_falls_back_to_aggregator():
    # No observed VAD start (shouldn't happen live) — keep pipecat's stamp
    # rather than inventing one.
    agent = _agent_ready_to_speak()

    await agent._emit_app_bot_transcript("text", "2026-08-03T09:40:58.127+00:00")

    assert agent._events[-1].turn_started_at == "2026-08-03T09:40:58.127+00:00"


async def test_listen_lag_sampled_after_speech_stop_settles():
    # Round 2: listen() is woken BY the speech-stopped event, racing ahead of
    # the STT queuing that segment — it read 0.0 exactly when a transcript was
    # guaranteed pending. The envelope must sample the lag after a settle.
    import time as time_module

    from voicebox.events import EventType, VoiceboxEvent
    from voicebox.processors.nonblocking_whisper_stt import NonBlockingSegmentedSTT

    t0 = time_module.monotonic()

    class _RacySTT(NonBlockingSegmentedSTT):
        @property
        def transcription_lag_secs(self) -> float:
            # 0.0 in the race window right after the event lands, real after.
            return 0.0 if time_module.monotonic() - t0 < 0.02 else 3.3

    agent = _agent_ready_to_speak()
    agent._stt = _RacySTT()
    agent._events.append(VoiceboxEvent(type=EventType.APP_BOT_SPEECH_STOPPED))

    envelope = await agent.listen_events(timeout=0.01, cursor=0)

    assert envelope["transcription_lag_secs"] == 3.3


def test_whisper_model_is_wrapped_eager(monkeypatch: pytest.MonkeyPatch):
    # faster-whisper's lazy decode escapes pipecat's to_thread and freezes the
    # event loop for the whole decode (measured 21 s on a 40 s utterance).
    # _load must wrap the model so the decode happens inside the thread.
    from pipecat.services.whisper.stt import WhisperSTTService

    from voicebox.processors.nonblocking_whisper_stt import EagerSegmentsWhisperModel

    def load_stub_model(self):
        self._model = object()  # stands in for a loaded WhisperModel

    monkeypatch.setattr(WhisperSTTService, "_load", load_stub_model)
    service = agent_module._NonBlockingWhisperSTTService(
        settings=WhisperSTTService.Settings(model="stub"), device="cpu", compute_type="int8"
    )

    assert isinstance(service._model, EagerSegmentsWhisperModel)


def test_turn_stop_timeout_outlives_batch_stt():
    # pipecat's 5 s watchdog default assumes streaming STT. Batch Whisper
    # delivers text ~0.5x realtime AFTER the VAD stop, so at 5 s every long
    # turn was force-closed empty and the late transcript re-opened a turn
    # stamped at arrival time (turn_started_at lied by 25-34 s live).
    from pipecat.processors.aggregators.llm_context import LLMContext

    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]
    user_aggregator, _ = agent._create_context_aggregators(LLMContext())

    assert user_aggregator._params.user_turn_stop_timeout == agent_module.TURN_STOP_TIMEOUT_SECS
    assert agent_module.TURN_STOP_TIMEOUT_SECS >= 60.0
