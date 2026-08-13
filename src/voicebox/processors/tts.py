#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Helpers shared by text-to-speech services."""

from pipecat.services.tts_service import TTSService


async def warm_up_tts_service(tts: TTSService) -> None:
    """Consume and discard one synthesis to pay lazy model costs at startup."""
    async for _frame in tts.run_tts("Ready.", "voicebox-warm-up"):
        pass
