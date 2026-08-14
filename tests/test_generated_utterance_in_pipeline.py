"""The generated-utterance path exercised through a REAL TTS service.

Written after three defects shipped behind 111 green tests (BUILDLOG D31). Every
one of them lived in base-class behaviour the old tests stubbed away: the
start/stop bracket, the audio-context idle timer, and error routing (which goes
UPSTREAM). Tests that drive the processor's ``process_frame`` directly cannot
see any of it.

The pattern here is pipecat's own: ``pipecat.tests.utils.run_test`` puts the
thing under test inside a real ``Pipeline`` between two capture processors and
asserts BOTH directions. The service is the real production one from
``_create_tts_service()``; only kokoro-onnx's ``create_stream`` — the actual
external dependency — is stubbed, so the pipecat machinery all still runs.
"""

import asyncio

import numpy as np
import pytest
from pipecat.frames.frames import (
    AggregatedTextFrame,
    ErrorFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.tests.utils import SleepFrame, run_test

from voicebox.agent import PipecatMCPAgent
from voicebox.processors.generated_utterance_audio_buffer import (
    GeneratedUtteranceAudioBuffer,
)
from voicebox.processors.tts import warm_up_tts_service


class _StubbedKokoroStream:
    """Stands in for kokoro-onnx's create_stream, the one real external call."""

    def __init__(self, chunks: int, raise_after: int | None = None):
        self._chunks = chunks
        self._raise_after = raise_after

    def __aiter__(self):
        return self._generate()

    async def _generate(self):
        for i in range(self._chunks):
            if self._raise_after is not None and i == self._raise_after:
                raise RuntimeError("synthesis died")
            yield np.zeros(480, dtype=np.float32), 24000


def _production_tts(chunks: int, raise_after: int | None = None, syntheses: list | None = None):
    """Build the real service with the production config, minus the ONNX model."""
    service = PipecatMCPAgent(None)._create_tts_service()  # type: ignore[arg-type]
    service._kokoro = type("_K", (), {})()

    def _create_stream(*args, **kwargs):
        if syntheses is not None:
            syntheses.append(kwargs.get("text", args[0] if args else None))
        return _StubbedKokoroStream(chunks, raise_after)

    service._kokoro.create_stream = _create_stream
    return service


def _tester_tts_stages(chunks: int, raise_after: int | None = None) -> Pipeline:
    """Compose the production TTS→buffer pair as ``agent._build_stages()`` does."""
    return Pipeline([_production_tts(chunks, raise_after), GeneratedUtteranceAudioBuffer()])


def _speak(text: str):
    """Build the frame triplet speak() queues for one utterance."""
    return [LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame()]


async def test_one_speak_produces_exactly_one_tts_bracket():
    # The defect this pins: stock TTSService pushed a SECOND TTSStoppedFrame
    # when stop_frame_timeout_s (3.0 s default) expired before synthesis
    # produced audio. Asserting the exact downstream frame sequence catches the
    # duplicate; the old capture-based tests never saw a bracket at all.
    await run_test(
        _tester_tts_stages(chunks=2),
        frames_to_send=_speak("two chunks of story"),
        expected_down_frames=[
            LLMFullResponseStartFrame,
            AggregatedTextFrame,
            TTSStartedFrame,
            TTSTextFrame,
            TTSAudioRawFrame,
            TTSAudioRawFrame,
            TTSStoppedFrame,
            LLMFullResponseEndFrame,
        ],
    )


async def test_warm_up_synthesizes_once_the_pipeline_hands_over_a_sample_rate():
    # The defect this pins (D31): TTSService.sample_rate is 0 until StartFrame
    # reaches it, and the service resamples every chunk to that rate, so the
    # warm-up failed EVERY session with "Sample rate should be over 0" - yielded
    # as an ErrorFrame, never raised, so it looked like it had worked. Only a
    # real pipeline delivers the StartFrame that sets the rate.
    syntheses: list[str] = []
    tts = _production_tts(chunks=2, syntheses=syntheses)
    warm_up = asyncio.create_task(warm_up_tts_service(tts))
    await asyncio.sleep(0.1)

    assert not warm_up.done(), "warm-up must park until the pipeline sets a sample rate"
    assert syntheses == [], "nothing may be synthesized at sample rate 0"

    received_down, _ = await run_test(tts, frames_to_send=[SleepFrame(0.5)])
    await asyncio.wait_for(warm_up, timeout=2)

    assert syntheses == ["Ready."], "the warm-up utterance must reach the model exactly once"
    assert not any(isinstance(f, TTSAudioRawFrame) for f in received_down), (
        "warm-up audio must be consumed by the helper, never played into the pipeline"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D30's failure contract is NOT implemented. The service reports synthesis failure by "
        "pushing an ErrorFrame UPSTREAM, so a processor placed DOWNSTREAM of it never sees the "
        "error in either direction. Wiring the service's on_error event does not fix it either: "
        "the event fires on the service's own task while the audio frames are still in flight, "
        "so the discard lands BEFORE the audio arrives and the following TTSStoppedFrame flushes "
        "it anyway. Needs a design pass - BUILDLOG D32, issue #25."
    ),
)
async def test_synthesis_failure_routes_upstream_and_drops_partial_audio():
    # This test is the specification, and it is currently red on purpose: it
    # states the contract BUILDLOG D30 claimed and D32 records as unmet. The
    # old unit test passed only because it fed the ErrorFrame downstream, a
    # direction production never produces.
    received_down, received_up = await run_test(
        _tester_tts_stages(chunks=3, raise_after=1),
        frames_to_send=_speak("this utterance fails midway"),
    )

    assert any(isinstance(f, ErrorFrame) for f in received_up), (
        "synthesis failure must reach the buffer upstream, where the discard lives"
    )
    assert not any(isinstance(f, TTSAudioRawFrame) for f in received_down), (
        "a failed utterance's partial audio must never reach the transport"
    )
    assert any(isinstance(f, TTSStoppedFrame) for f in received_down), (
        "the utterance must still be closed so _tts_pending stays balanced"
    )
