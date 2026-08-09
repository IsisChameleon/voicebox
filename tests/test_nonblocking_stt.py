import asyncio
import time
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    ErrorFrame,
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

from voicebox.agent import PipecatMCPAgent, _PipelineEventObserver
from voicebox.events import EventType
from voicebox.processors.nonblocking_whisper_stt import (
    EagerSegmentsWhisperModel,
    NonBlockingSegmentedSTT,
)

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

    def __init__(self, stt: SegmentedSTTService, observers: list | None = None):
        self.stt = stt
        self.downstream = _Downstream()
        self._worker = PipelineWorker(
            Pipeline([stt, self.downstream]),
            cancel_on_idle_timeout=False,
            observers=observers,
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

    async def speech_segment(self, secs: float = 1.0, started_at: float | None = None):
        """Feed one VAD-delimited utterance, closing it so the STT cuts a segment.

        ``started_at`` stamps the VAD start frame, which is what the agent's
        observer reads as the utterance's turn start.
        """
        start = (
            VADUserStartedSpeakingFrame()
            if started_at is None
            else VADUserStartedSpeakingFrame(timestamp=started_at)  # type: ignore[call-arg]
        )
        await self._worker.queue_frame(start)
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


def test_eager_model_decodes_inside_the_transcribe_call():
    # faster-whisper's transcribe() is lazy: the decode runs while the
    # segments are iterated. pipecat only puts the transcribe() CALL on a
    # thread, so a lazy result hands the decode back to the event loop.
    # The wrapper must exhaust the generator before returning.
    consumed: list[int] = []

    def lazy_segments():
        for i in range(3):
            consumed.append(i)
            yield f"seg-{i}"

    class _LazyModel:
        def transcribe(self, audio, **kwargs):
            return lazy_segments(), {"language": "en"}

    segments, info = EagerSegmentsWhisperModel(_LazyModel()).transcribe(b"pcm", language="en")

    assert consumed == [0, 1, 2]  # decoded during the call, nothing left lazy
    assert segments == ["seg-0", "seg-1", "seg-2"]
    assert info == {"language": "en"}


class _SometimesSilentSTT(SegmentedSTTService):
    """Stands in for Whisper recovering nothing from some segments.

    Real Whisper yields NO frame at all for a segment it finds no text in
    (``pipecat/services/whisper/stt.py:377``, ``if text:``), which is exactly
    what makes the empty case invisible downstream.
    """

    def __init__(self, silent_segments: set[int], **kwargs):
        super().__init__(**kwargs)
        self.silent_segments = silent_segments
        self.segments_seen = 0

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Yield one transcript, or nothing for a segment marked silent."""
        self.segments_seen += 1
        if self.segments_seen in self.silent_segments:
            return
        yield TranscriptionFrame(f"segment-{self.segments_seen}", "", time_now_iso8601())


class _NonBlockingSometimesSilentSTT(NonBlockingSegmentedSTT, _SometimesSilentSTT):
    """The production composition over a sometimes-silent Whisper."""


async def test_empty_segment_signals_once_and_only_when_silent():
    # D24 Phase 2: with delivery moved onto the TranscriptionFrame, a silent
    # segment has no frame to carry "we tried and got nothing" — the worker,
    # which sees a segment go in and no transcript come out, must say so. And
    # exactly once per silent segment, or the app-bot VAD-start deque that
    # every transcript claims from drifts.
    stt = _NonBlockingSometimesSilentSTT(silent_segments={2})
    empties: list[int] = []

    async def on_empty():
        empties.append(1)

    stt.on_empty_segment = on_empty

    async with _Harness(stt) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)
        await h.speech_segment()
        await asyncio.sleep(0.5)
        await h.speech_segment()
        await asyncio.sleep(0.5)

        assert h.downstream.texts() == ["segment-1", "segment-3"]
        assert len(empties) == 1, f"expected one empty signal, got {len(empties)}"


class _ErrorFrameSTT(SegmentedSTTService):
    """Stands in for Whisper failing a segment WITHOUT raising.

    pipecat's Whisper services report a failed transcription by yielding an
    ``ErrorFrame`` — ``whisper/stt.py:355`` when the model is missing, and the
    MLX service's catch-all ``except Exception`` at ``whisper/stt.py:547``,
    which swallows the exception and yields instead. So a failure never reaches
    the worker's ``except``.
    """

    def __init__(self, failing_segments: set[int], **kwargs):
        super().__init__(**kwargs)
        self.failing_segments = failing_segments
        self.segments_seen = 0

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Yield one transcript, or only an ErrorFrame for a failing segment."""
        self.segments_seen += 1
        if self.segments_seen in self.failing_segments:
            yield ErrorFrame(error="whisper failed")
            return
        yield TranscriptionFrame(f"segment-{self.segments_seen}", "", time_now_iso8601())


class _NonBlockingErrorFrameSTT(NonBlockingSegmentedSTT, _ErrorFrameSTT):
    """The production composition over a Whisper that fails by ErrorFrame."""


async def test_error_frame_is_not_an_empty_segment():
    # A failed transcription is not silence. If the worker counts only
    # transcripts, an ErrorFrame run looks identical to a silent one and gets
    # reported as `transcription_empty: true` — consuming the VAD-start
    # timestamp that the NEXT real transcript needs, so every later transcript
    # in the session is attributed to the wrong turn.
    stt = _NonBlockingErrorFrameSTT(failing_segments={1})
    empties: list[int] = []
    failures: list[int] = []
    errors: list[ErrorFrame] = []

    async def on_empty():
        empties.append(1)

    async def on_failed():
        failures.append(1)

    stt.on_empty_segment = on_empty
    stt.on_failed_segment = on_failed
    # push_error_frame fires "on_error" and pushes UPSTREAM (frame_processor.py:688,700),
    # so the ErrorFrame never reaches a downstream processor — this handler is where
    # pipecat's own error path is observable.
    stt.add_event_handler("on_error", lambda _proc, frame: errors.append(frame))

    async with _Harness(stt) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)
        await h.speech_segment()
        await asyncio.sleep(0.5)

        assert empties == [], "a failed transcription must not signal an empty segment"
        # But it must signal SOMETHING, exactly once: the failed segment still
        # consumed a VAD start that no transcript will ever arrive to claim.
        assert len(failures) == 1, f"expected one failure signal, got {len(failures)}"
        assert [e.error for e in errors] == ["whisper failed"], (
            "the ErrorFrame must still travel pipecat's error path"
        )
        # And the worker survives it: the next segment still transcribes.
        assert h.downstream.texts() == ["segment-2"]


async def test_raised_failure_signals_the_same_as_an_error_frame():
    # The two ways Whisper can fail differ only in how pipecat reports them;
    # to the segment they are one outcome. A raised failure that signalled
    # nothing would drift the VAD-start correlation exactly as an unsignalled
    # ErrorFrame does.
    stt = _NonBlockingExplodingSTT()
    empties: list[int] = []
    failures: list[int] = []

    async def on_empty():
        empties.append(1)

    async def on_failed():
        failures.append(1)

    stt.on_empty_segment = on_empty
    stt.on_failed_segment = on_failed

    async with _Harness(stt) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)

        assert failures == [1] and empties == []


async def test_failed_segment_leaves_the_next_transcripts_vad_start_alone():
    # The end-to-end story behind the two signals, through the real agent: the
    # observer logs one VAD start per utterance and each transcript claims the
    # earliest unclaimed one. A failed segment that left its start behind would
    # hand it to the NEXT transcript, and every transcript after that would
    # carry its predecessor's turn start for the rest of the session (D26).
    stt = _NonBlockingErrorFrameSTT(failing_segments={1})
    agent = PipecatMCPAgent(transport=None)  # type: ignore[arg-type]
    # Exactly the wiring agent.start() does.
    stt.on_empty_segment = agent._on_empty_segment
    stt.on_failed_segment = agent._on_failed_segment

    async with _Harness(stt, observers=[_PipelineEventObserver(agent)]) as h:
        await h.speech_segment(started_at=100.0)  # Whisper fails this one
        await asyncio.sleep(0.5)
        await h.speech_segment(started_at=200.0)  # ...and transcribes this one
        await asyncio.sleep(0.5)

    transcripts = [e for e in agent._events if e.type == EventType.APP_BOT_TRANSCRIPT]
    assert [t.text for t in transcripts] == ["segment-2"]  # type: ignore[attr-defined]
    # 200.0, its own start — not 100.0, the failed segment's.
    assert transcripts[0].turn_started_at == "1970-01-01T00:03:20.000+00:00"  # type: ignore[attr-defined]
    assert list(agent._unclaimed_bot_speech_starts) == []


async def test_empty_segment_signal_is_optional():
    # Nothing sets the callback in the unit harness or in a bare service; a
    # silent segment must not blow up the worker (which would lose every later
    # transcript in the session).
    stt = _NonBlockingSometimesSilentSTT(silent_segments={1})

    async with _Harness(stt) as h:
        await h.speech_segment()
        await asyncio.sleep(0.5)
        await h.speech_segment()
        await asyncio.sleep(0.5)

        assert h.downstream.texts() == ["segment-2"]
