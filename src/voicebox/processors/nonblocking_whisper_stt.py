#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Move Whisper transcription off pipecat's frame task.

``SegmentedSTTService`` awaits transcription inline: its
``_handle_user_stopped_speaking`` calls ``process_generator(self.run_stt(...))``
(``pipecat/services/stt_service.py:780``), and that handler runs on
``FrameProcessor.__input_frame_task_handler`` — the one task that also carries
system frames. **Nothing gets past the STT while Whisper runs.** Measured: a 5 s
stall delayed a queued ``speak()`` frame triplet by 4.7 s; live, it made a
``speak()`` play 51 s late and the caller believe it had deadlocked.

Raw throughput is not the problem — Whisper CPU int8 runs at 0.40x realtime
warm. The problem is *where* it runs. This mixin keeps the transcription exactly
where it was and moves only its execution onto a background worker.

How: ``run_stt`` is intercepted. On the frame task it hands the segment to a
queue and yields nothing, so ``process_generator`` finishes immediately and the
frame task is free. One worker task then pulls segments in order and calls the
wrapped service's real ``run_stt`` (``super().run_stt``), pushing whatever it
yields from there. Pushing frames off the frame task is how pipecat's own
websocket STT services deliver transcripts, so this is the supported shape.

Intercepting ``run_stt`` rather than re-implementing
``_handle_user_stopped_speaking`` means the segment framing — the WAV container
the base class builds — is never copied into this repo, so it cannot drift from
the pipecat we run against. See ``BUILDLOG.md`` D5.

The worker alone is not enough for faster-whisper: its decode is lazy, so the
CPU work escapes pipecat's ``asyncio.to_thread`` and lands back on the event
loop. ``EagerSegmentsWhisperModel`` closes that second hole — see its
docstring. ``BUILDLOG.md`` D8.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator

from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, Frame, StartFrame
from pipecat.services.stt_service import SegmentedSTTService


class EagerSegmentsWhisperModel:
    """Materializes faster-whisper's lazy segments inside the calling thread.

    ``WhisperModel.transcribe`` returns a LAZY generator: the call itself does
    almost no work, and the decode runs while the segments are iterated.
    pipecat's ``WhisperSTTService.run_stt`` wraps only the ``transcribe`` call
    in ``asyncio.to_thread`` and then iterates on the event loop — measured on
    a real 40 s utterance: the call returned in 35 ms and iteration froze the
    loop for 21.2 s. With the segments materialized inside ``to_thread`` the
    same decode left the loop free (max stall 55 ms).

    Wrapping the model rather than overriding ``run_stt`` keeps pipecat's own
    post-processing (no-speech filtering, frame construction) untouched.
    """

    def __init__(self, model):
        """Wrap a ``faster_whisper.WhisperModel``.

        Args:
            model: The real model whose ``transcribe`` output to materialize.

        """
        self._model = model

    def transcribe(self, *args, **kwargs):
        """Transcribe and exhaust the segments before returning.

        pipecat calls this via ``asyncio.to_thread``, so the ``list()`` — the
        actual decode — runs on that worker thread, not on the event loop.

        Returns:
            ``(segments, info)`` with ``segments`` a fully-decoded list.

        """
        segments, info = self._model.transcribe(*args, **kwargs)
        return list(segments), info


class NonBlockingSegmentedSTT(SegmentedSTTService):
    """Transcribes on a background worker instead of on the frame task.

    List it FIRST in the bases, ahead of the concrete service
    (``class X(NonBlockingSegmentedSTT, TimedSTTMixin, WhisperSTTService)``):
    ``super().run_stt`` must resolve to the real transcription further along
    the MRO.

    One worker, never a pool. Segments have to stay in the order they were
    spoken, and two concurrent Whisper runs would only contend for the same CPU.
    """

    def __init__(self, **kwargs):
        """Initialize the queue; the worker starts with the pipeline."""
        super().__init__(**kwargs)
        self._segments: asyncio.Queue[bytes] = asyncio.Queue()
        # Enqueue times, popped in lockstep with the queue by the single
        # worker. They are what makes the backlog reportable as an age rather
        # than as an opaque count.
        self._waiting_since: deque[float] = deque()
        self._worker: asyncio.Task | None = None
        self._in_flight_since: float | None = None

    @property
    def transcription_lag_secs(self) -> float:
        """Age of the oldest segment not yet transcribed; ``0.0`` when idle.

        Lets a caller tell "still transcribing" from "nothing was said". It is
        an age, not an estimate of remaining work: the queue knows when a
        segment arrived, not how long Whisper will take on it.
        """
        oldest = self._in_flight_since
        if oldest is None and self._waiting_since:
            oldest = self._waiting_since[0]
        return 0.0 if oldest is None else time.time() - oldest

    async def start(self, frame: StartFrame):
        """Start the background transcription worker."""
        await super().start(frame)
        if self._worker is None:
            self._worker = self.create_task(self._transcribe_worker())

    async def stop(self, frame: EndFrame):
        """Stop the worker at end of session."""
        await super().stop(frame)
        await self._stop_worker()

    async def cancel(self, frame: CancelFrame):
        """Stop the worker on an abrupt teardown."""
        await super().cancel(frame)
        await self._stop_worker()

    async def _stop_worker(self):
        if self._worker is None:
            return
        worker, self._worker = self._worker, None
        await self.cancel_task(worker)

    # pipecat annotates the abstract STTService.run_stt as `async def -> AsyncGenerator`
    # with no yield in its body, so a type checker reads the base as a coroutine that
    # returns a generator rather than as an async generator itself. The ignores here
    # and in _transcribe_worker cover that annotation mismatch and nothing else.
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:  # type: ignore
        """Hand the segment to the worker and return; do NOT transcribe here.

        This runs on the frame task. Yielding nothing is what keeps that task
        free — the real transcription is ``super().run_stt``, called from
        ``_transcribe_worker``.

        Args:
            audio: The segment as the base class framed it.

        Yields:
            Nothing, ever.

        """
        self._waiting_since.append(time.time())
        await self._segments.put(audio)
        return
        yield  # unreachable; makes this an async generator

    async def _transcribe_worker(self):
        """Transcribe queued segments one at a time, in the order spoken."""
        while True:
            audio = await self._segments.get()
            self._in_flight_since = self._waiting_since.popleft()
            try:
                await self.process_generator(super().run_stt(audio))  # type: ignore
            except Exception as e:
                # A failed segment must not kill the worker: every later
                # transcript in the session would be lost with it.
                logger.error(f"{self}: transcription failed, segment dropped: {e}")
            finally:
                self._in_flight_since = None
                self._segments.task_done()
