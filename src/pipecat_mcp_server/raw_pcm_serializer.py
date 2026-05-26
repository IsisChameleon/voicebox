#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Raw PCM frame serializer for the browser-shim WebSocket transport.

Wire format: little-endian 16-bit signed PCM, mono. Pipecat's WebSocket
transport handles rate conversion between the wire (configured via
``WebsocketServerParams.audio_{in,out}_sample_rate``) and the pipeline's
internal rate, so the browser can stream native 48 kHz with no
resampling on its side.
"""

from typing import Optional

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.base_serializer import FrameSerializer


class RawPCMSerializer(FrameSerializer):
    def __init__(self, sample_rate: int = 48000, num_channels: int = 1):
        super().__init__()
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def serialize(self, frame: Frame) -> Optional[bytes]:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data) -> Optional[Frame]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            return None
        return InputAudioRawFrame(
            audio=bytes(data),
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
