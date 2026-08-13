#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Keep generated utterances contiguous when synthesis produces delayed chunks."""

from collections import deque

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class GeneratedUtteranceAudioBuffer(FrameProcessor):
    """Release each generated utterance as one gap-free downstream audio span.

    TTS providers may stream chunks more slowly than the transport plays them.
    Forwarding those chunks immediately exposes synthesis delays as silence in
    the synthetic microphone. This processor forwards the start marker, holds
    only the generated audio frames, then releases them in order immediately
    before the matching stop marker.

    An interruption discards audio that has not reached the transport: playing
    it later would resurrect speech the caller already interrupted.
    """

    def __init__(self, **kwargs):
        """Initialize an empty generated-utterance buffer."""
        super().__init__(**kwargs)
        self._audio_frames: deque[TTSAudioRawFrame] = deque()
        self._buffering = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Buffer downstream generated audio until its utterance is complete."""
        await super().process_frame(frame, direction)

        if direction is FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TTSStartedFrame):
            self._audio_frames.clear()
            self._buffering = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, TTSAudioRawFrame) and self._buffering:
            self._audio_frames.append(frame)
        elif isinstance(frame, TTSStoppedFrame) and self._buffering:
            self._buffering = False
            while self._audio_frames:
                await self.push_frame(self._audio_frames.popleft(), direction)
            await self.push_frame(frame, direction)
        elif isinstance(frame, (ErrorFrame, InterruptionFrame, CancelFrame, EndFrame)):
            self._audio_frames.clear()
            self._buffering = False
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)
