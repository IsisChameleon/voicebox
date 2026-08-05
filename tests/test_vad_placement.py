import asyncio
from collections.abc import AsyncGenerator

from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner

from voicebox.agent import VAD_STOP_SECS, PipecatMCPAgent

RATE = 16000
CHUNK_SECS = 0.1
CHUNK = int(RATE * CHUNK_SECS) * 2  # bytes, 16-bit mono
SPEECH = b"\x11\x22" * (CHUNK // 2)
SILENCE = b"\x00\x00" * (CHUNK // 2)

# How long the stub STT holds its frame task. Everything queued behind it waits.
STALL_SECS = 2.0
# The flood that arrives while the STT is stalled: this is the audio the bug ate.
FLOOD_SECS = 10.0


class _ByteVADAnalyzer(VADAnalyzer):
    """Deterministic stand-in for Silero: non-zero bytes are speech, zeros are not.

    The real analyzer would make these tests depend on ONNX inference over
    synthetic audio. What is under test here is where the VAD sits in the
    pipeline, not how well it detects speech.
    """

    def num_frames_required(self) -> int:
        """Analyse in 512-sample windows (32 ms at 16 kHz), as Silero does."""
        return 512

    def voice_confidence(self, buffer: bytes) -> float:
        """Report speech for any window that is not pure silence."""
        return 0.0 if not any(buffer) else 1.0


def _stub_vad() -> VADProcessor:
    # start/stop_secs are shortened so a test does not have to feed a real
    # second of trailing silence; placement is what is being measured.
    return VADProcessor(
        vad_analyzer=_ByteVADAnalyzer(
            params=VADParams(confidence=0.5, start_secs=0.1, stop_secs=0.2, min_volume=0.0)
        )
    )


class _StallingSTT(SegmentedSTTService):
    """Records how much audio each segment handed to ``run_stt``, then stalls."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.segment_secs: list[float] = []

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Measure the segment (a WAV container: 44-byte header + PCM) and stall."""
        self.segment_secs.append((len(audio) - 44) / (self.sample_rate * 2))
        await asyncio.sleep(STALL_SECS)
        yield TranscriptionFrame("x", "", time_now_iso8601())


class _Sink(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Forward every frame unchanged."""
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def _feed(worker: PipelineWorker, payload: bytes, secs: float):
    for _ in range(round(secs / CHUNK_SECS)):
        await worker.queue_frame(InputAudioRawFrame(payload, RATE, 1))


async def _run_flood_through(stt: _StallingSTT, stages: list[FrameProcessor]) -> float:
    """Stall the STT, flood it with speech, and return the seconds it then saw.

    Segment 1 (2 s of speech, ended by silence) puts ``run_stt`` into its stall.
    ``FLOOD_SECS`` of speech is queued while that stall is in flight; the
    trailing silence that closes segment 2 is only queued once the stall has
    cleared, because in a real session audio arrives at wall-clock speed rather
    than as fast as a test can enqueue it.
    """
    worker = PipelineWorker(Pipeline(stages), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.2)

    await _feed(worker, SPEECH, 2.0)
    await _feed(worker, SILENCE, 0.5)
    await asyncio.sleep(0.3)  # let the VAD stop land and run_stt take the frame task

    await _feed(worker, SPEECH, FLOOD_SECS)
    await asyncio.sleep(STALL_SECS + 0.5)  # stall clears, the flood drains

    await _feed(worker, SILENCE, 0.5)
    await asyncio.sleep(STALL_SECS + 0.8)

    # stop_when_done() queues the EndFrame that ends the run. stop() only
    # cancels the job groups, so WorkerRunner.run() would never return.
    await worker.stop_when_done()
    await run_task

    assert len(stt.segment_secs) == 2, f"expected two segments, got {stt.segment_secs}"
    return stt.segment_secs[1]


async def test_audio_survives_stalled_stt_when_vad_is_upstream():
    # D1: the STT is busy for STALL_SECS while FLOOD_SECS of speech arrives.
    # With the VAD upstream, VADUserStartedSpeakingFrame (a SystemFrame) reaches
    # the STT before the audio it describes, so _user_speaking is already True
    # and the buffer is allowed to grow instead of being trimmed to 1 s.
    stt = _StallingSTT()
    heard = await _run_flood_through(stt, [_stub_vad(), stt, _Sink()])
    assert heard >= FLOOD_SECS - 0.5, f"{heard:.2f}s of {FLOOD_SECS}s reached run_stt"


async def test_audio_is_lost_when_vad_is_downstream():
    # The negative control: the same flood through the topology voicebox shipped
    # before this fix. It exists so the test above proves PLACEMENT is what
    # matters, and so moving the VAD back downstream fails loudly.
    stt = _StallingSTT()
    heard = await _run_flood_through(stt, [stt, _stub_vad(), _Sink()])
    assert heard < 3.0, f"{heard:.2f}s survived — the downstream-VAD trim no longer bites"


class _FakeTransport:
    """Stands in for the WebSocket transport: _build_stages only needs its ends."""

    def __init__(self):
        self.input_stage = _Sink()
        self.output_stage = _Sink()

    def input(self) -> FrameProcessor:
        """Return the transport's input stage."""
        return self.input_stage

    def output(self) -> FrameProcessor:
        """Return the transport's output stage."""
        return self.output_stage


def test_vad_stage_precedes_the_stt():
    # D2: the shipped pipeline order, asserted on the real _build_stages.
    agent = PipecatMCPAgent(_FakeTransport())  # type: ignore[arg-type]
    vad, stt, user_aggregator, tts, assistant_aggregator = (_Sink() for _ in range(5))
    stages = agent._build_stages(vad, stt, user_aggregator, tts, assistant_aggregator)  # type: ignore[arg-type]

    assert stages.index(vad) < stages.index(stt) < stages.index(user_aggregator)


def test_vad_analyzer_keeps_the_one_second_stop():
    # D2: 1.0 s, not pipecat's 0.2 s default, and read from the single constant
    # that the session_started event header also reports.
    vad = PipecatMCPAgent(_FakeTransport())._create_vad_processor()  # type: ignore[arg-type]

    assert vad._vad_controller._vad_analyzer.params.stop_secs == VAD_STOP_SECS == 1.0


def test_aggregator_does_no_vad_of_its_own():
    # D2: the aggregator sits downstream of the STT, so a VAD there is the bug.
    # _vad_controller is the thing the aggregator builds when it is given an
    # analyzer; None means it emits no VAD frames at all.
    from pipecat.processors.aggregators.llm_context import LLMContext

    user_aggregator, _ = PipecatMCPAgent(_FakeTransport())._create_context_aggregators(  # type: ignore[arg-type]
        LLMContext()
    )

    assert user_aggregator._vad_controller is None
