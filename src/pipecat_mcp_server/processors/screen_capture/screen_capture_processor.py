#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Screen capture processor for Pipecat MCP Server.

This module provides a FrameProcessor that captures screenshots of the screen
or a specific window and injects them into the pipeline as OutputImageRawFrames.

On macOS, uses ScreenCaptureKit for true window-level capture (content not
affected by overlapping windows).
"""

import asyncio
from typing import Optional

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    OutputImageRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .base_capture_backend import get_capture_backend


class ScreenCaptureProcessor(FrameProcessor):
    """FrameProcessor that captures screenshots and pushes them downstream.

    Screen capture is inactive by default. Call ``screen_capture()`` to start
    capturing a specific window or the full screen. Frames are pushed as
    ``OutputImageRawFrame`` at the configured interval.

    """

    def __init__(self, monitor: int = 0, capture_interval: float = 1.0):
        """Initialize the screen capture processor.

        Args:
            monitor: The monitor index to capture (default: 0 for primary monitor).
                    Only used when not capturing a specific window.
            capture_interval: Time in seconds between captures (default: 1.0).

        """
        super().__init__(name="screen-capture")
        self._monitor = monitor
        self._capture_interval = capture_interval
        self._capture_task: Optional[asyncio.Task] = None
        self._backend = get_capture_backend()

    async def cleanup(self) -> None:
        """Clean up resources when processor is shutting down."""
        await super().cleanup()
        await self._stop_capture()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames and manage capture task lifecycle.

        Args:
            frame: The frame to process.
            direction: The frame direction (DOWNSTREAM or UPSTREAM).

        """
        await super().process_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_capture()

        await self.push_frame(frame, direction)

    async def screen_capture(self, window_id: Optional[int] = None) -> Optional[int]:
        """Start or restart screen capture for a window or full screen.

        Stops any existing capture before starting the new one.

        Args:
            window_id: Window ID to capture (from list_windows()),
                or None for full screen.

        Returns:
            The window ID if found, or None if the window was not found
            or capturing full screen.

        """
        await self._stop_capture()
        return await self._start_capture(window_id)

    async def _start_capture(self, window_id: Optional[int] = None) -> Optional[int]:
        """Start capturing from a window or full screen.

        Returns:
            The window ID if found, or None if the window was not found
            or capturing full screen.

        """
        try:
            matched_id = await self._backend.start(window_id, self._monitor)
        except PermissionError as e:
            logger.error(str(e))
            return None

        if window_id is not None:
            logger.debug(f"Capturing window ID: {window_id}")
        else:
            logger.debug(f"Capturing monitor {self._monitor}")

        self._capture_task = self.create_task(self._capture_task_handler())

        # Schedule task if we don't await
        await asyncio.sleep(0)

        return matched_id

    async def _stop_capture(self) -> None:
        """Stop the periodic capture task."""
        if self._capture_task:
            await self.cancel_task(self._capture_task)
            self._capture_task = None
        if self._backend:
            await self._backend.stop()

    async def _capture_task_handler(self) -> None:
        """Periodically capture screenshots and push them downstream."""
        while True:
            try:
                result = await self._backend.capture()

                if result:
                    rgb_bytes, (width, height) = result
                    frame = OutputImageRawFrame(
                        image=rgb_bytes,
                        size=(width, height),
                        format="RGB",
                    )
                    await self.push_frame(frame)

                await asyncio.sleep(self._capture_interval)
            except PermissionError as e:
                logger.error(str(e))
                break
            except Exception as e:
                logger.error(f"Error in capture task: {e}")
                await asyncio.sleep(self._capture_interval)
