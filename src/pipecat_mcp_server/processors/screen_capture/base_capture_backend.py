#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Abstract base class for screen capture backends and factory function."""

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class WindowInfo:
    """Information about an open window."""

    title: str
    app_name: str
    window_id: int


class BaseCaptureBackend(ABC):
    """Abstract base class for platform-specific screen capture backends."""

    @abstractmethod
    async def list_windows(self) -> List[WindowInfo]:
        """List all open windows.

        Returns:
            A list of WindowInfo for each visible window.

        """

    @abstractmethod
    async def start(self, window_id: Optional[int], monitor: int) -> Optional[int]:
        """Initialize capture for a window or monitor.

        Args:
            window_id: Optional window ID to capture (from list_windows()).
            monitor: Monitor index to capture when window_id is None.

        Returns:
            The window ID if found, or None if not found or capturing full screen.

        """

    @abstractmethod
    async def capture(self) -> Optional[Tuple[bytes, Tuple[int, int]]]:
        """Capture a single frame.

        Returns:
            Tuple of (rgb_bytes, (width, height)) or None if capture failed.

        """

    @abstractmethod
    async def stop(self) -> None:
        """Release capture resources."""


def get_capture_backend() -> BaseCaptureBackend:
    """Return the appropriate capture backend for the current platform.

    Returns:
        A platform-specific BaseCaptureBackend instance.

    Raises:
        RuntimeError: If the current platform is not supported.

    """
    if sys.platform == "darwin":
        from .macos_capture_backend import MacOSCaptureBackend

        return MacOSCaptureBackend()

    if sys.platform == "linux":
        from .linux_x11_capture_backend import LinuxX11CaptureBackend

        return LinuxX11CaptureBackend()

    raise RuntimeError(
        f"Screen capture is not supported on platform '{sys.platform}'. "
        "Currently only macOS and Linux (X11) are supported."
    )
