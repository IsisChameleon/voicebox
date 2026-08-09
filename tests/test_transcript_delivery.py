"""Transcript delivery through the simplified pipeline."""

import asyncio

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

import voicebox.agent as agent_module
from voicebox.agent import PipecatMCPAgent
from voicebox.events import EventType


class _Relay(FrameProcessor):
    """Forwards every frame and records what reached it."""

    def __init__(self):
        super().__init__()
        self.seen: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Record the frame, then forward it unchanged."""
        await super().process_frame(frame, direction)
        self.seen.append(frame)
        await self.push_frame(frame, direction)


class _FakeTransport:
    """Stands in for the WebSocket transport; only its ends are used."""

    def input(self) -> FrameProcessor:
        """Return the transport's input stage."""
        return _Relay()

    def output(self) -> FrameProcessor:
        """Return the transport's output stage."""
        return _Relay()


async def test_transcript_is_delivered_exactly_once_without_user_aggregator():
    agent = PipecatMCPAgent(_FakeTransport())  # type: ignore[arg-type]
    stt_stand_in, downstream = _Relay(), _Relay()

    worker = PipelineWorker(
        Pipeline([stt_stand_in, downstream]),
        cancel_on_idle_timeout=False,
        enable_rtvi=False,
        observers=[agent_module._PipelineEventObserver(agent)],
    )
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    await worker.queue_frame(TranscriptionFrame("the bot said this", "", "iso"))
    await worker.stop_when_done()
    await run_task

    transcripts = [e for e in agent._events if e.type == EventType.APP_BOT_TRANSCRIPT]
    assert len(transcripts) == 1
    assert transcripts[0].text == "the bot said this"  # type: ignore[attr-defined]

    # It continues downstream, while frame-id deduplication ensures its
    # multiple observed hops still produce only one event.
    assert [f for f in downstream.seen if isinstance(f, TranscriptionFrame)]
    assert [f for f in stt_stand_in.seen if isinstance(f, TranscriptionFrame)]


async def test_tester_response_triplet_reaches_tts_and_assistant_context():
    """The retained aggregator remains after TTS and records tester speech."""
    agent = PipecatMCPAgent(_FakeTransport())  # type: ignore[arg-type]
    tts = _Relay()
    assistant_aggregator = agent._create_assistant_aggregator()
    worker = PipelineWorker(
        Pipeline([tts, assistant_aggregator]),
        cancel_on_idle_timeout=False,
        enable_rtvi=False,
    )
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    await worker.queue_frames(
        [
            LLMFullResponseStartFrame(),
            LLMTextFrame("hello from the tester"),
            LLMFullResponseEndFrame(),
        ]
    )
    await worker.stop_when_done()
    await run_task

    assert [type(frame) for frame in tts.seen if isinstance(frame, LLMTextFrame)] == [LLMTextFrame]
    assert assistant_aggregator._context.messages[-1]["role"] == "assistant"
    assert assistant_aggregator._context.messages[-1]["content"] == "hello from the tester"
