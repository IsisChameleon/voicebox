"""Task G: one utterance is one gap-free audio span, and the playout knows it.

Kokoro used to yield each chunk as synthesis produced it; the CPU gap between
chunks became real silence in the synthetic mic (1.6-4.2 s observed live) and
the app under test heard one utterance as several user turns. run_tts now
buffers the whole utterance; _Playout resolves on the first
BotStoppedSpeakingFrame after the utterance's TTSStoppedFrame instead of on a
silence timer.
"""

import asyncio
import time

from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame

from voicebox.agent import _Playout
from voicebox.processors.kokoro_tts import KokoroTTSService


class _SlowChunkStream:
    """Stands in for kokoro's create_stream: chunks arrive with synthesis gaps."""

    def __init__(self, chunks: int, gap_secs: float, record: list):
        self._chunks = chunks
        self._gap_secs = gap_secs
        self._record = record  # timestamps of chunk production

    def __aiter__(self):
        return self._generate()

    async def _generate(self):
        import numpy as np

        for _ in range(self._chunks):
            await asyncio.sleep(self._gap_secs)  # the synthesis gap
            self._record.append(time.monotonic())
            yield np.zeros(480, dtype=np.float32), 48000


def _kokoro_with_stubbed_synthesis(chunks: int, gap_secs: float, record: list) -> KokoroTTSService:
    service = KokoroTTSService(voice_id="af_heart")
    service._sample_rate = 48000  # normally set by StartFrame
    service._kokoro.create_stream = lambda *a, **k: _SlowChunkStream(chunks, gap_secs, record)
    return service


async def test_utterance_yields_single_audio_span():
    # G1: every audio frame is produced only after synthesis fully finished,
    # so no synthesis gap can become silence in the synthetic microphone.
    produced: list[float] = []
    service = _kokoro_with_stubbed_synthesis(chunks=3, gap_secs=0.1, record=produced)

    audio_yielded_at: list[float] = []
    frames = []
    async for frame in service.run_tts("three chunks of story", "ctx"):
        frames.append(frame)
        if isinstance(frame, TTSAudioRawFrame):
            audio_yielded_at.append(time.monotonic())

    assert len(audio_yielded_at) == 3
    last_synthesis = max(produced)
    assert all(t >= last_synthesis for t in audio_yielded_at)
    # And the gap between consecutive audio yields is negligible — the frames
    # go out back-to-back, not paced by synthesis.
    spans = [b - a for a, b in zip(audio_yielded_at, audio_yielded_at[1:])]
    assert all(span < 0.05 for span in spans)


async def test_tts_frames_still_bracket_and_error_path_closes():
    # G2 (bracket half): TTSStarted first, TTSStopped last — including when
    # synthesis blows up mid-stream (the finally guarantee).
    produced: list[float] = []
    service = _kokoro_with_stubbed_synthesis(chunks=2, gap_secs=0.01, record=produced)

    frames = [f async for f in service.run_tts("ok", "ctx")]
    assert isinstance(frames[0], TTSStartedFrame)
    assert isinstance(frames[-1], TTSStoppedFrame)

    class _ExplodingStream:
        def __aiter__(self):
            return self._generate()

        async def _generate(self):
            raise RuntimeError("synthesis died")
            yield  # pragma: no cover

    service._kokoro.create_stream = lambda *a, **k: _ExplodingStream()
    frames = [f async for f in service.run_tts("boom", "ctx")]
    assert isinstance(frames[-1], TTSStoppedFrame)  # never left unsent
    assert any(type(f).__name__ == "ErrorFrame" for f in frames)


async def test_multi_sentence_speak_is_one_synthesis_call():
    # Round 6: SENTENCE aggregation split a 3-sentence speak() into three
    # run_tts calls with real silent gaps between them (up to 11.5 s under
    # CPU contention) — the app answered the first fragment. voicebox sends
    # one LLMTextFrame per utterance, so TOKEN mode must hand the whole text
    # to a single synthesis call.
    service = KokoroTTSService(voice_id="af_heart")

    texts = [
        aggregation.text
        async for aggregation in service._text_aggregator.aggregate(
            "Hello Ember, lovely to meet you. Please read me the story. Give me choices."
        )
    ]

    assert texts == ["Hello Ember, lovely to meet you. Please read me the story. Give me choices."]


async def test_warm_up_consumes_one_synthesis():
    # Round 5: the session's FIRST synthesis carried ~5 s of one-time ONNX
    # cost, landing mid-utterance and splitting the first speak() into two
    # turns. warm_up() must actually pull the stream (the inference runs
    # during iteration), not just create it.
    produced: list[float] = []
    service = _kokoro_with_stubbed_synthesis(chunks=2, gap_secs=0.01, record=produced)

    await service.warm_up()

    assert len(produced) == 2  # the stream was consumed, so the model ran


async def test_playout_resolves_on_bot_stopped_after_tts_stopped():
    # G2: the call returns when the audio actually finished — not at a segment
    # boundary, not on a silence timer.
    playout = _Playout()
    playout.on_started(1.0)
    playout.on_stopped(2.0)  # bot pause BEFORE synthesis finished: not the end
    assert not playout.future.done()

    playout.on_tts_stopped()
    assert not playout.future.done()  # utterance complete, audio still playing

    playout.on_stopped(5.0)  # first stop AFTER tts finished: the real end
    assert playout.future.done()
    assert playout.future.result() == {
        "started_at": 1.0,
        "finished_at": 5.0,
        "interrupted": False,
    }


async def test_interruption_resolves_immediately():
    # G3: barge-in still cuts the wait short, whatever state synthesis is in.
    playout = _Playout()
    playout.on_started(1.0)
    playout.on_interrupted(3.0)

    assert playout.future.done()
    result = playout.future.result()
    assert result["interrupted"] is True and result["finished_at"] == 3.0
