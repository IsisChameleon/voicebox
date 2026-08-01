import asyncio
import time
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
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

from voicebox.processors.nonblocking_whisper_stt import NonBlockingSegmentedSTT

RATE = 16000
CHUNK = 3200  # 0.1 s of 16-bit mono @ 16 kHz

# Long enough that a blocking STT could not possibly be mistaken for a fast one.
TRANSCRIBE_SECS = 4.0


class _SlowSTT(SegmentedSTTService):
    """Stands in for Whisper: takes TRANSCRIBE_SECS, names its segment."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.segments_seen = 0

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Take a fixed, measurable amount of time, then emit one transcript."""
        self.segments_seen += 1
        label = f"segment-{self.segments_seen}"
        await asyncio.sleep(TRANSCRIBE_SECS)
        yield TranscriptionFrame(label, "", time_now_iso8601())


class _NonBlockingSlowSTT(NonBlockingSegmentedSTT, _SlowSTT):
    """The production composition, with Whisper replaced by a timed sleep."""


class _ExplodingSTT(_SlowSTT):
    """Fails the first segment, transcribes every later one."""

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Raise on the first call; delegate afterwards."""
        if self.segments_seen == 0:
            self.segments_seen += 1
            raise RuntimeError("whisper exploded")
        async for frame in super().run_stt(audio):
            yield frame


class _NonBlockingExplodingSTT(NonBlockingSegmentedSTT, _ExplodingSTT):
    """The failing service behind the non-blocking worker."""


class _Downstream(FrameProcessor):
    """Records what arrives downstream of the STT, and when."""

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[float, Frame]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Timestamp every frame, then forward it."""
        await super().process_frame(frame, direction)
        self.seen.append((time.monotonic(), frame))
        await self.push_frame(frame, direction)

    def texts(self) -> list[str]:
        """Transcript texts in the order they arrived."""
        return [f.text for _, f in self.seen if isinstance(f, TranscriptionFrame)]

    def first_delay(self, frame_type: type[Frame], since: float) -> float:
        """Seconds from ``since`` until the first frame of ``frame_type``."""
        for t, f in self.seen:
            if isinstance(f, frame_type):
                return t - since
        raise AssertionError(f"no {frame_type.__name__} ever arrived downstream")


class _Harness:
    """A running pipeline of [stt, downstream] that can be fed speech segments."""

    def __init__(self, stt: SegmentedSTTService):
        self.stt = stt
        self.downstream = _Downstream()
        self._worker = PipelineWorker(
            Pipeline([stt, self.downstream]), cancel_on_idle_timeout=False
        )
        self._runner = WorkerRunner(handle_sigterm=False)

    async def __aenter__(self) -> "_Harness":
        await self._runner.add_workers(self._worker)
        self._run_task = asyncio.create_task(self._runner.run())
        await asyncio.sleep(0.2)
        return self

    async def __aexit__(self, *exc):
        # stop_when_done() queues the EndFrame that ends the run; stop() alone
        # would leave WorkerRunner.run() awaiting forever.
        await self._worker.stop_when_done()
        await self._run_task

    async def speech_segment(self, secs: float = 1.0):
        """Feed one VAD-delimited utterance, closing it so the STT cuts a segment."""
        await self._worker.queue_frame(VADUserStartedSpeakingFrame())
        for _ in range(round(secs / 0.1)):
            await self._worker.queue_frame(InputAudioRawFrame(b"\x11" * CHUNK, RATE, 1))
        await self._worker.queue_frame(VADUserStoppedSpeakingFrame())

    async def queue(self, frame: Frame):
        """Queue any frame behind whatever the pipeline is already handling."""
        await self._worker.queue_frame(frame)


async def test_speak_not_blocked_by_transcription():
    # E1: the frame task must stay free while Whisper runs. A speak() reaches
    # the pipeline as an LLMTextFrame; queued behind an in-flight transcription
    # it used to wait for the whole thing (measured: 4.7 s).
    async with _Harness(_NonBlockingSlowSTT()) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)  # transcription is now in flight

        queued_at = time.monotonic()
        await h.queue(LLMTextFrame(text="hello"))
        await asyncio.sleep(0.5)

        delay = h.downstream.first_delay(LLMTextFrame, queued_at)
        assert delay < 0.5, f"speech frame waited {delay:.2f}s behind the transcription"

        await asyncio.sleep(TRANSCRIBE_SECS)  # let the worker finish before teardown


async def test_blocking_stt_is_what_this_fixes():
    # The negative control: the same measurement against the unmodified pipecat
    # behaviour. Without it, E1 above cannot show that the mixin is what freed
    # the frame task.
    async with _Harness(_SlowSTT()) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)

        queued_at = time.monotonic()
        await h.queue(LLMTextFrame(text="hello"))
        await asyncio.sleep(TRANSCRIBE_SECS + 0.5)

        delay = h.downstream.first_delay(LLMTextFrame, queued_at)
        assert delay > 2.0, f"inline transcription only held the frame task {delay:.2f}s"


async def test_transcripts_preserve_segment_order():
    # E4: two segments queued while the worker is busy must come back in the
    # order they were spoken — hence one worker, never a pool.
    async with _Harness(_NonBlockingSlowSTT()) as h:
        await h.speech_segment()
        await asyncio.sleep(0.2)
        await h.speech_segment()
        await asyncio.sleep(2 * TRANSCRIBE_SECS + 1.0)

        assert h.downstream.texts() == ["segment-1", "segment-2"]


async def test_listen_reports_transcription_backlog():
    # E3: a caller must be able to tell "still transcribing" from "nothing was
    # said". The lag is the age of the oldest segment not yet transcribed.
    stt = _NonBlockingSlowSTT()
    async with _Harness(stt) as h:
        assert stt.transcription_lag_secs == 0.0

        await h.speech_segment()
        await asyncio.sleep(1.0)
        lag = stt.transcription_lag_secs
        assert 0.5 < lag < TRANSCRIBE_SECS, f"lag {lag:.2f}s does not track the wait"

        await asyncio.sleep(TRANSCRIBE_SECS)
        assert stt.transcription_lag_secs == 0.0, "lag must fall back to zero when idle"


async def test_worker_survives_a_failing_segment():
    # One bad segment must not take the rest of the session's transcripts with
    # it: the worker is the only thing draining the queue.
    async with _Harness(_NonBlockingExplodingSTT()) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)
        await h.speech_segment()
        await asyncio.sleep(TRANSCRIBE_SECS + 1.0)

        assert h.downstream.texts() == ["segment-2"]


async def test_teardown_leaves_no_worker_running():
    # E criterion 4: the worker starts and stops with the processor's lifecycle.
    stt = _NonBlockingSlowSTT()
    async with _Harness(stt):
        await asyncio.sleep(0.1)
        assert stt._worker is not None and not stt._worker.done()

    assert stt._worker is None
