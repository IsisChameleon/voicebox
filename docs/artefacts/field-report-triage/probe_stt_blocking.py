"""Does a slow SegmentedSTTService block later frames from reaching downstream processors?

Mirrors voicebox's pipeline shape: [source] -> stt -> [probe].
We push a VADUserStoppedSpeakingFrame (which triggers run_stt) immediately
followed by the LLM frame triplet speak() queues, and time when the triplet
reaches the probe sitting where the aggregator/TTS sit in voicebox.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner

SLOW_SECS = 5.0


class SlowSTT(SegmentedSTTService):
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        print(f"[{time.time() - T0:6.2f}] STT: run_stt START ({len(audio)} bytes)")
        await asyncio.sleep(SLOW_SECS)  # stands in for whisper on CPU
        print(f"[{time.time() - T0:6.2f}] STT: run_stt DONE")
        yield TranscriptionFrame("hello", "", time_now_iso8601())


class Probe(FrameProcessor):
    """Sits where the user aggregator / TTS sit in voicebox's pipeline."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (LLMTextFrame, VADUserStartedSpeakingFrame, TranscriptionFrame)):
            print(f"[{time.time() - T0:6.2f}] PROBE saw {frame.name}")
        await self.push_frame(frame, direction)


T0 = time.time()


async def main():
    global T0
    stt = SlowSTT()
    worker = PipelineWorker(Pipeline([stt, Probe()]), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(1.0)

    T0 = time.time()
    # Give the segmented STT a buffer of "speech", then end the segment.
    await worker.queue_frame(VADUserStartedSpeakingFrame())
    for _ in range(10):
        await worker.queue_frame(InputAudioRawFrame(b"\x00" * 3200, 16000, 1))
    await worker.queue_frame(VADUserStoppedSpeakingFrame())
    await asyncio.sleep(0.3)

    print(f"[{time.time() - T0:6.2f}] speak(): queueing LLM triplet")
    await worker.queue_frames(
        [LLMFullResponseStartFrame(), LLMTextFrame(text="hi"), LLMFullResponseEndFrame()]
    )
    print(f"[{time.time() - T0:6.2f}] speak(): queue_frames returned")

    # A VAD system frame arriving during the STT stall: does it get through?
    await asyncio.sleep(0.2)
    late_vad = VADUserStartedSpeakingFrame()
    print(f"[{time.time() - T0:6.2f}] VAD frame constructed, timestamp={late_vad.timestamp - T0:.2f}")
    await worker.queue_frame(late_vad)

    await asyncio.sleep(SLOW_SECS + 3)
    await worker.stop()
    await run_task


asyncio.run(main())
