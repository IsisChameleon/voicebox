import asyncio
import re
from collections.abc import AsyncGenerator, Iterator

import pytest
from loguru import logger
from pipecat.audio.turn.base_turn_analyzer import (
    BaseTurnAnalyzer,
    BaseTurnParams,
    EndOfTurnState,
)
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import MetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner

from voicebox.timing import TIMING_PREFIX, TimedSTTMixin, TimedTurnAnalyzerMixin

RATE = 16000
CHUNK = 3200  # 0.1 s of 16-bit mono @ 16 kHz
STT_WORK_SECS = 0.05
ANALYZER_WORK_SECS = 0.05

# One line per measured call: "voicebox.timing name=<call> secs=<float>".
TIMING_LINE = re.compile(rf"{re.escape(TIMING_PREFIX)} name=(\w+) secs=([0-9]+\.[0-9]+)")


@pytest.fixture
def timing_lines() -> Iterator[list[str]]:
    """Capture DEBUG-level loguru output in a list (caplog does not see loguru)."""
    lines: list[str] = []
    handler_id = logger.add(lines.append, level="DEBUG", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


@pytest.fixture
def default_level_lines() -> Iterator[list[str]]:
    """Capture output at the default (INFO) level, where DEBUG must not appear."""
    lines: list[str] = []
    handler_id = logger.add(lines.append, level="INFO", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


class _StubSTT(SegmentedSTTService):
    """A concrete STT that does measurable work without loading Whisper."""

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        await asyncio.sleep(STT_WORK_SECS)
        yield TranscriptionFrame("hello", "", time_now_iso8601())


class _TimedStubSTT(TimedSTTMixin, _StubSTT):
    """The mixin over a SUBCLASS of a concrete service — the Task E composition."""


class _StubTurnAnalyzer(BaseTurnAnalyzer):
    """A concrete turn analyzer that does measurable work without the ONNX model."""

    @property
    def speech_triggered(self) -> bool:
        return True

    @property
    def params(self) -> BaseTurnParams:
        return BaseTurnParams()

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self) -> tuple[EndOfTurnState, MetricsData | None]:
        await asyncio.sleep(ANALYZER_WORK_SECS)
        return EndOfTurnState.COMPLETE, None

    def clear(self):
        pass


class _TimedStubTurnAnalyzer(TimedTurnAnalyzerMixin, _StubTurnAnalyzer):
    """The mixin over a concrete analyzer, exactly as agent.py composes it."""


class _Sink(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def _run_one_stt_segment(stt: SegmentedSTTService):
    """Drive one VAD-delimited speech segment through a minimal pipeline."""
    worker = PipelineWorker(Pipeline([stt, _Sink()]), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigterm=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.2)

    await worker.queue_frame(VADUserStartedSpeakingFrame())
    for _ in range(5):
        await worker.queue_frame(InputAudioRawFrame(b"\x11" * CHUNK, RATE, 1))
    await worker.queue_frame(VADUserStoppedSpeakingFrame())
    await asyncio.sleep(STT_WORK_SECS + 0.5)

    # stop_when_done() queues an EndFrame, which is what actually ends the
    # pipeline and lets runner.run() return — the same teardown agent.py uses
    # (agent.py:449). BaseWorker.stop() only sets the finished event; the
    # runner task would run forever and the test would hang.
    await worker.stop_when_done()
    await run_task


def _parse(lines: list[str]) -> list[tuple[str, float]]:
    return [(m.group(1), float(m.group(2))) for line in lines if (m := TIMING_LINE.search(line))]


async def test_timing_lines_emitted(timing_lines: list[str]):
    # C1: at DEBUG, one greppable line per STT call and per turn-analyzer call,
    # each carrying a duration that can be summed.
    await _run_one_stt_segment(_TimedStubSTT())
    await _TimedStubTurnAnalyzer().analyze_end_of_turn()

    measured = _parse(timing_lines)
    by_name = {name: secs for name, secs in measured}

    assert [name for name, _ in measured].count("run_stt") == 1
    assert [name for name, _ in measured].count("analyze_end_of_turn") == 1
    # The float is real elapsed time, not a placeholder: each stub sleeps a
    # known minimum, so the logged value must be at least that.
    assert by_name["run_stt"] >= STT_WORK_SECS
    assert by_name["analyze_end_of_turn"] >= ANALYZER_WORK_SECS


async def test_timed_stt_passes_frames_through_unchanged():
    # Criterion 3: the wrapper must not add, drop or reorder frames. The stub
    # yields one TranscriptionFrame; the timed subclass must yield exactly that.
    frames = [f async for f in _TimedStubSTT().run_stt(b"")]
    assert len(frames) == 1
    assert isinstance(frames[0], TranscriptionFrame)
    assert frames[0].text == "hello"


async def test_timing_silent_at_default_level(default_level_lines: list[str]):
    # C2: instrumentation costs nothing when it is not asked for — a sink at the
    # default level sees no timing line at all.
    await _run_one_stt_segment(_TimedStubSTT())
    await _TimedStubTurnAnalyzer().analyze_end_of_turn()

    assert _parse(default_level_lines) == []
    assert not [line for line in default_level_lines if TIMING_PREFIX in line]
