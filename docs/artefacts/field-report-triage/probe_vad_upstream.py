"""Does audio arriving during a slow run_stt survive, or get trimmed to 1s?

voicebox's pipeline is [input] -> stt -> aggregator(VAD) -> ...  The VAD that
tells the STT "the user is speaking" lives DOWNSTREAM of the STT, and its
VADUserStartedSpeakingFrame comes back upstream. So while run_stt blocks the
STT's frame task, incoming audio queues at the STT; when it drains, the STT
appends it to _audio_buffer with _user_speaking still False -- and
SegmentedSTTService.process_audio_frame trims the buffer to 1s in that state
(pipecat services/stt_service.py:804-807).

This probe drains 10s of audio through a stalled STT and reports how much
of it is left in the buffer when the next segment is cut.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
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

SLOW_SECS = 4.0
RATE = 16000
CHUNK = 3200  # 0.1s of 16-bit mono @16k


class SlowSTT(SegmentedSTTService):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.segment_sizes = []

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        # audio is a WAV container; 44-byte header + PCM
        secs = (len(audio) - 44) / (RATE * 2)
        self.segment_sizes.append(secs)
        print(f"[{time.time() - T0:6.2f}] run_stt got {secs:.2f}s of audio")
        await asyncio.sleep(SLOW_SECS)
        yield TranscriptionFrame("x", "", time_now_iso8601())


class Sink(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


T0 = time.time()


async def main():
    global T0
    stt = SlowSTT()
    worker = PipelineWorker(Pipeline([stt, Sink()]), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(1.0)
    T0 = time.time()

    # --- Segment 1: 2s of speech, ended cleanly. Triggers the slow run_stt.
    await worker.queue_frame(VADUserStartedSpeakingFrame())
    for _ in range(20):
        await worker.queue_frame(InputAudioRawFrame(b"\x11" * CHUNK, RATE, 1))
    await worker.queue_frame(VADUserStoppedSpeakingFrame())

    # --- Segment 2 arrives WHILE run_stt is stalled: 10s of speech.
    # The VAD lives downstream, so its "started speaking" frame cannot reach
    # the STT until the audio itself has drained through the STT.
    await asyncio.sleep(0.2)
    await worker.queue_frame(VADUserStartedSpeakingFrame())  # UPSTREAM VAD: start arrives with the audio
    for _ in range(100):
        await worker.queue_frame(InputAudioRawFrame(b"\x22" * CHUNK, RATE, 1))

    # STT unstalls, drains the 10s flood, and only THEN does the downstream VAD
    # get to say "the user started speaking".
    await asyncio.sleep(SLOW_SECS + 1.5)
    await asyncio.sleep(0.2)
    await worker.queue_frame(VADUserStoppedSpeakingFrame())
    await asyncio.sleep(SLOW_SECS + 1.5)

    print()
    print(f"segment 1 fed 2.0s of speech  -> run_stt saw {stt.segment_sizes[0]:.2f}s")
    print(f"segment 2 fed 10.0s of speech -> run_stt saw {stt.segment_sizes[1]:.2f}s")
    lost = 10.0 - stt.segment_sizes[1]
    print(f"AUDIO LOST FROM SEGMENT 2 (VAD upstream of STT): {lost:.2f}s ({lost / 10.0:.0%})")

    await worker.stop()
    await run_task


asyncio.run(main())
