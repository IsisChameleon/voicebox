"""Q4 of the D24 spec, against a real pipeline rather than by reading pipecat.

The app bot's transcript is now delivered by the pipeline observer when the
``TranscriptionFrame`` is pushed, not when the app bot's turn aggregator closes
a turn. That only works because the ``stt``→``user_aggregator`` hop is a
downstream push and therefore observable — the aggregator itself *consumes* the
frame and never pushes it on. Both halves of that claim are asserted here
through a real ``Pipeline`` containing the real aggregator.
"""

import asyncio

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
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


async def test_transcript_is_delivered_at_the_hop_into_the_aggregator():
    agent = PipecatMCPAgent(_FakeTransport())  # type: ignore[arg-type]
    user_aggregator, _ = agent._create_context_aggregators(LLMContext())
    stt_stand_in, downstream_of_aggregator = _Relay(), _Relay()

    worker = PipelineWorker(
        Pipeline([stt_stand_in, user_aggregator, downstream_of_aggregator]),
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

    # The other half of Q4: the aggregator consumes the frame, so watching any
    # hop AFTER it would have seen nothing. This is why _WATCHED is enough only
    # because observers see pushes rather than deliveries.
    assert not [f for f in downstream_of_aggregator.seen if isinstance(f, TranscriptionFrame)]
    assert [f for f in stt_stand_in.seen if isinstance(f, TranscriptionFrame)]
