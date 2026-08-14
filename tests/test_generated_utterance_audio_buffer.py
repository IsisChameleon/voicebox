"""The generated-utterance buffer exercised through real pipelines.

Ported from a hand-driven harness (issue #24). The old version subclassed the
buffer to capture ``push_frame`` and called ``process_frame`` by hand, which
cannot observe anything the framework owns — frame direction above all. Its
error test fed an ``ErrorFrame`` DOWNSTREAM, a direction production never
produces, and so passed against behaviour that could not execute (BUILDLOG D32).

Everything here runs inside a real ``Pipeline`` via ``pipecat.tests.utils``:
frames are queued through a real ``PipelineWorker`` and BOTH directions are
captured.
"""

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    TextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test

from voicebox.processors.generated_utterance_audio_buffer import (
    GeneratedUtteranceAudioBuffer,
)

FAIL_TRIGGER = "fail here"


class _FailsBelowTheBuffer(FrameProcessor):
    """Reports a failure the way production does: an ErrorFrame pushed UPSTREAM.

    Stands for the transport or the aggregator — anything below the buffer whose
    error passes back through it. ``push_error_frame`` is pipecat's own path
    (``frame_processor.py:680``); it ends in ``push_frame(error, UPSTREAM)``.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text == FAIL_TRIGGER:
            await self.push_error_frame(ErrorFrame("synthesis failed"))


def _audio(value: int, context_id: str = "utterance") -> TTSAudioRawFrame:
    return TTSAudioRawFrame(bytes([value]), 48000, 1, context_id=context_id)


async def test_generated_audio_is_withheld_until_its_utterance_closes():
    # The proof of withholding is the ORDER INVERSION, not the final sequence:
    # the marker is sent after the first audio frame and must arrive before it.
    # A buffer that forwarded audio immediately would emit them as sent.
    await run_test(
        GeneratedUtteranceAudioBuffer(),
        frames_to_send=[
            TTSStartedFrame(context_id="utterance"),
            _audio(1),
            TextFrame("marker"),
            _audio(2),
            TTSStoppedFrame(context_id="utterance"),
        ],
        expected_down_frames=[
            TTSStartedFrame,
            TextFrame,
            TTSAudioRawFrame,
            TTSAudioRawFrame,
            TTSStoppedFrame,
        ],
    )


async def test_upstream_frames_pass_through_unchanged():
    # Sent from the END of the pipeline, so they traverse the buffer upwards -
    # the direction the old hand-driven harness could not reach.
    await run_test(
        GeneratedUtteranceAudioBuffer(),
        frames_to_send=[TextFrame("travelling up"), _audio(3)],
        frames_to_send_direction=FrameDirection.UPSTREAM,
        expected_up_frames=[TextFrame, TTSAudioRawFrame],
    )


async def test_an_error_from_below_discards_the_partial_utterance():
    # Scope note: this is an error raised BELOW the buffer, whose ErrorFrame
    # passes back up through it. A TTS SYNTHESIS failure is pushed upstream from
    # ABOVE and never reaches the buffer at all - see the strict xfail in
    # tests/test_generated_utterance_in_pipeline.py and BUILDLOG D32.
    received_down, _ = await run_test(
        Pipeline([GeneratedUtteranceAudioBuffer(), _FailsBelowTheBuffer()]),
        frames_to_send=[
            TTSStartedFrame(context_id="utterance"),
            _audio(1),
            TextFrame(FAIL_TRIGGER),
            SleepFrame(0.2),  # let the upstream ErrorFrame reach the buffer
            TTSStoppedFrame(context_id="utterance"),
        ],
        expected_up_frames=[ErrorFrame],
    )

    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in received_down), (
        "a failed utterance's buffered audio must never reach the transport"
    )
    assert any(isinstance(frame, TTSStoppedFrame) for frame in received_down), (
        "the utterance must still be closed so the agent's bookkeeping stays balanced"
    )


async def test_interruption_discards_generated_audio_that_has_not_reached_playout():
    # InterruptionFrame is a SystemFrame: it is delivered out of band and can
    # overtake queued frames, so this asserts presence and absence rather than
    # an exact sequence.
    received_down, _ = await run_test(
        GeneratedUtteranceAudioBuffer(),
        frames_to_send=[
            TTSStartedFrame(context_id="utterance"),
            _audio(1),
            SleepFrame(0.1),
            InterruptionFrame(),
            SleepFrame(0.1),
            TTSStoppedFrame(context_id="utterance"),
        ],
    )

    assert not any(isinstance(frame, TTSAudioRawFrame) for frame in received_down), (
        "interrupted audio would resurrect speech the caller already cut off"
    )
    assert any(isinstance(frame, InterruptionFrame) for frame in received_down)
