#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Custom RunnerArguments for the browser-shim transport.

Pipecat ships RunnerArguments subclasses for Daily, SmallWebRTC, LiveKit,
and telephony providers, but not for a plain WebSocket-server transport
used by an in-page shim. We define one here so ``create_agent`` can
dispatch on type.
"""

from dataclasses import dataclass, field

from pipecat.runner.types import RunnerArguments


@dataclass
class BrowserShimRunnerArguments(RunnerArguments):
    """Configuration for the WebSocket-server transport used by the browser shim.

    The shim (loaded into a Playwright-controlled Chromium) opens a
    WebSocket to ``ws://{host}:{port}`` and streams raw 16-bit LE mono PCM
    in both directions, but at different rates per direction:

      * ``mic_rate`` (default 48 kHz) — Kokoro audio sent FROM pipecat TO
        the browser, played back through the synthetic mic into the page.
        Kept high to match the page's native AudioContext.

      * ``tap_rate`` (default 16 kHz) — remote-track audio captured in the
        browser via Web Audio and sent TO pipecat. Fixed at 16 kHz because
        mlx_whisper.transcribe assumes 16 kHz input and has no sample_rate
        parameter — the shim's AudioContext does the 48→16 conversion.

    If ``record_dir`` is set, an ``AudioBufferProcessor`` is added to the
    pipeline and ``stop_recording()`` writes WAVs (kokoro-out + bot-in +
    merged) into that directory on shutdown — useful for end-to-end test
    artifacts.
    """

    host: str = field(default="localhost", kw_only=True)
    port: int = field(default=9091, kw_only=True)
    mic_rate: int = field(default=48000, kw_only=True)
    tap_rate: int = field(default=16000, kw_only=True)
    record_dir: str | None = field(default=None, kw_only=True)
