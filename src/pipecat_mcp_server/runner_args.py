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
    WebSocket to ``ws://{host}:{port}`` and streams raw 16-bit LE mono
    PCM in both directions at ``sample_rate``.

    If ``record_dir`` is set, an ``AudioBufferProcessor`` is added to the
    pipeline and ``stop_recording()`` writes WAVs (kokoro-out + bot-in +
    merged) into that directory on shutdown — useful for end-to-end test
    artifacts.
    """

    host: str = field(default="localhost", kw_only=True)
    port: int = field(default=9091, kw_only=True)
    sample_rate: int = field(default=48000, kw_only=True)
    record_dir: str | None = field(default=None, kw_only=True)
