#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Helpers shared by text-to-speech services."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import ErrorFrame
from pipecat.services.tts_service import TTSService

WARM_UP_SAMPLE_RATE_WAIT_SECS = 30.0


async def warm_up_tts_service(tts: TTSService) -> None:
    """Consume and discard one synthesis to pay lazy model costs at startup.

    Waits for the pipeline to hand the service its output sample rate first.
    ``TTSService`` keeps ``sample_rate`` at 0 until ``StartFrame`` reaches it,
    and the service resamples every chunk to that rate, so warming up before
    the pipeline runs fails with "Sample rate should be over 0" — silently,
    because the failure is *yielded* as an ``ErrorFrame`` rather than raised.

    Args:
        tts: The service to warm. Its ``run_tts`` stream is consumed and dropped.

    """
    deadline = time.monotonic() + WARM_UP_SAMPLE_RATE_WAIT_SECS
    while tts.sample_rate == 0:
        if time.monotonic() >= deadline:
            logger.warning("TTS warm-up skipped: pipeline never set a sample rate")
            return
        await asyncio.sleep(0.05)

    async for frame in tts.run_tts("Ready.", "voicebox-warm-up"):
        if isinstance(frame, ErrorFrame):
            logger.warning(f"TTS warm-up failed: {frame.error}")
