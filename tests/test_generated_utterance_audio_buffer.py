import asyncio

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from voicebox.processors.generated_utterance_audio_buffer import (
    GeneratedUtteranceAudioBuffer,
)
from voicebox.processors.tts import warm_up_tts_service


class _CaptureBuffer(GeneratedUtteranceAudioBuffer):
    """Capture pushes so ordinary frame ordering can be tested without a worker."""

    def __init__(self):
        super().__init__()
        self.pushed: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        self.pushed.append((frame, direction))


class _Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)


def _audio(value: int, context_id: str = "utterance") -> TTSAudioRawFrame:
    return TTSAudioRawFrame(bytes([value]), 48000, 1, context_id=context_id)


async def test_generated_audio_is_withheld_then_flushed_before_its_stop():
    processor = _CaptureBuffer()
    started = TTSStartedFrame(context_id="utterance")
    first = _audio(1)
    second = _audio(2)
    stopped = TTSStoppedFrame(context_id="utterance")

    await processor.process_frame(started, FrameDirection.DOWNSTREAM)
    await processor.process_frame(first, FrameDirection.DOWNSTREAM)
    await processor.process_frame(second, FrameDirection.DOWNSTREAM)

    assert [frame for frame, _ in processor.pushed] == [started]

    await processor.process_frame(stopped, FrameDirection.DOWNSTREAM)

    assert [frame for frame, _ in processor.pushed] == [started, first, second, stopped]


async def test_non_generated_and_upstream_frames_pass_through_unchanged():
    processor = _CaptureBuffer()
    downstream = LLMTextFrame("still flowing")
    upstream = _audio(3)

    await processor.process_frame(downstream, FrameDirection.DOWNSTREAM)
    await processor.process_frame(upstream, FrameDirection.UPSTREAM)

    assert processor.pushed == [
        (downstream, FrameDirection.DOWNSTREAM),
        (upstream, FrameDirection.UPSTREAM),
    ]


async def test_synthesis_error_discards_a_partial_generated_utterance():
    # The ErrorFrame must travel UPSTREAM, the direction production actually
    # produces: TTSService reports failure via push_error_frame, which pushes
    # upstream. An earlier version of this test pushed it downstream, so it
    # passed against a discard branch that could never run in production.
    processor = _CaptureBuffer()
    started = TTSStartedFrame(context_id="utterance")
    partial_audio = _audio(1)
    error = ErrorFrame("synthesis failed")
    stopped = TTSStoppedFrame(context_id="utterance")

    await processor.process_frame(started, FrameDirection.DOWNSTREAM)
    await processor.process_frame(partial_audio, FrameDirection.DOWNSTREAM)
    await processor.process_frame(error, FrameDirection.UPSTREAM)
    await processor.process_frame(stopped, FrameDirection.DOWNSTREAM)

    assert [frame for frame, _ in processor.pushed] == [started, error, stopped]
    assert not any(isinstance(frame, TTSAudioRawFrame) for frame, _ in processor.pushed)


async def test_interruption_discards_generated_audio_that_has_not_reached_playout():
    buffer = GeneratedUtteranceAudioBuffer()
    sink = _Sink()
    worker = PipelineWorker(Pipeline([buffer, sink]), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.1)

    await worker.queue_frame(TTSStartedFrame(context_id="utterance"))
    await worker.queue_frame(_audio(1))
    await asyncio.sleep(0.1)
    await worker.queue_frame(InterruptionFrame())
    await asyncio.sleep(0.1)
    await worker.queue_frame(TTSStoppedFrame(context_id="utterance"))
    await asyncio.sleep(0.1)

    await worker.stop_when_done()
    await run_task

    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in sink.frames)
    assert any(isinstance(frame, InterruptionFrame) for frame in sink.frames)


async def test_warm_up_consumes_the_service_stream_without_forwarding_audio():
    consumed: list[str] = []

    class _StreamingTTS:
        sample_rate = 48000

        async def run_tts(self, text: str, context_id: str):
            consumed.append(f"started:{text}:{context_id}")
            yield _audio(1, context_id)
            consumed.append("finished")

    await warm_up_tts_service(_StreamingTTS())  # type: ignore[arg-type]

    assert consumed == ["started:Ready.:voicebox-warm-up", "finished"]


async def test_warm_up_waits_for_the_pipeline_to_set_a_sample_rate():
    # TTSService.sample_rate stays 0 until StartFrame reaches it, and the
    # service resamples every chunk to that rate. Warming up before then failed
    # every session with "Sample rate should be over 0" — yielded as an
    # ErrorFrame, never raised, so the warm-up looked like it had worked while
    # the ~5 s first-inference cost stayed in the conversation.
    synthesized_at_rate: list[int] = []

    class _LateSampleRateTTS:
        def __init__(self):
            self.sample_rate = 0

        async def run_tts(self, text: str, context_id: str):
            synthesized_at_rate.append(self.sample_rate)
            yield _audio(1, context_id)

    tts = _LateSampleRateTTS()
    warm_up = asyncio.create_task(warm_up_tts_service(tts))  # type: ignore[arg-type]
    await asyncio.sleep(0.2)

    assert not warm_up.done()
    assert synthesized_at_rate == []  # parked, nothing synthesized at rate 0

    tts.sample_rate = 48000  # what the pipeline's StartFrame does
    await warm_up

    assert synthesized_at_rate == [48000]
