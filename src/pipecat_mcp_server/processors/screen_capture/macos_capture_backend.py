#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""macOS screen capture backend using ScreenCaptureKit.

All pyobjc imports are deferred to first use to avoid triggering CoreGraphics
initialization at import time, which crashes in non-GUI processes.
"""

import asyncio
from typing import List, Optional, Tuple

from loguru import logger

from .base_capture_backend import BaseCaptureBackend, WindowInfo

# Lazy references populated by _ensure_frameworks()
_Quartz = None
_SCKit = None


def _ensure_frameworks():
    """Import pyobjc frameworks on first use.

    Importing Quartz/ScreenCaptureKit triggers CoreGraphics initialization,
    which must happen after the process has window-server access.
    """
    global _Quartz, _SCKit
    if _Quartz is not None:
        return

    import CoreMedia  # noqa: F401 — needed for CMSampleBuffer bridging
    import Quartz
    import ScreenCaptureKit

    # Force CG initialization so later calls don't hit the assertion
    Quartz.CGMainDisplayID()

    _Quartz = Quartz
    _SCKit = ScreenCaptureKit


def _cgimage_to_rgb(cg_image) -> Optional[Tuple[bytes, Tuple[int, int]]]:
    """Convert a CGImage to RGB bytes.

    Args:
        cg_image: A CGImage reference.

    Returns:
        Tuple of (rgb_bytes, (width, height)) or None on failure.

    """
    import numpy as np

    Q = _Quartz

    width = Q.CGImageGetWidth(cg_image)
    height = Q.CGImageGetHeight(cg_image)

    if width == 0 or height == 0:
        return None

    # Get raw pixel data via CGDataProvider (returns NSData → bytes)
    provider = Q.CGImageGetDataProvider(cg_image)
    ns_data = Q.CGDataProviderCopyData(provider)
    raw = bytes(ns_data)

    bpp = Q.CGImageGetBitsPerPixel(cg_image) // 8
    src_row = Q.CGImageGetBytesPerRow(cg_image)

    # Handle row padding: if bytes_per_row > width * bpp, strip padding per row
    if src_row != width * bpp:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, src_row)
        arr = arr[:, : width * bpp].reshape(-1, bpp)
    else:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, bpp)

    # ScreenCaptureKit returns BGRA — swap to RGB
    rgb = np.ascontiguousarray(arr[:, [2, 1, 0]]).tobytes()
    return (rgb, (width, height))


async def _get_shareable_content(exclude_desktop: bool = False, onscreen_only: bool = False):
    """Enumerate shareable content (windows and displays).

    Args:
        exclude_desktop: If True, exclude desktop wallpaper windows.
        onscreen_only: If True, only include windows currently on screen.

    Returns:
        An SCShareableContent instance.

    Raises:
        PermissionError: If screen recording permission is denied.

    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def handler(content, error):
        if future.done():
            return
        if error is not None:
            err_str = str(error)
            if "permission" in err_str.lower() or "denied" in err_str.lower():
                loop.call_soon_threadsafe(
                    future.set_exception,
                    PermissionError(
                        "Screen recording permission denied. "
                        "Grant access in System Settings > Privacy & Security > Screen Recording."
                    ),
                )
            else:
                loop.call_soon_threadsafe(
                    future.set_exception,
                    RuntimeError(f"Failed to get shareable content: {error}"),
                )
            return
        loop.call_soon_threadsafe(future.set_result, content)

    if exclude_desktop or onscreen_only:
        _SCKit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            exclude_desktop, onscreen_only, handler
        )
    else:
        _SCKit.SCShareableContent.getShareableContentWithCompletionHandler_(handler)

    return await future


class MacOSCaptureBackend(BaseCaptureBackend):
    """macOS capture backend using ScreenCaptureKit (macOS 14+).

    Uses SCScreenshotManager for single-frame capture with true window-level
    isolation (content not affected by overlapping windows).
    """

    def __init__(self):
        """Initialize the macOS capture backend."""
        self._window_id: Optional[int] = None
        self._monitor: int = 0

    async def list_windows(self) -> List[WindowInfo]:
        """List all open windows via ScreenCaptureKit."""
        _ensure_frameworks()
        from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication

        content = await _get_shareable_content(exclude_desktop=True)
        windows = []
        for window in content.windows():
            # Only include normal-layer windows (layer 0) that belong to an app
            if window.windowLayer() != 0:
                continue
            app = window.owningApplication()
            if not app:
                continue
            # Only include windows from regular (Dock-visible) apps
            bundle_id = app.bundleIdentifier()
            if bundle_id:
                ns_apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
                if (
                    ns_apps
                    and ns_apps[0].activationPolicy() != NSApplicationActivationPolicyRegular
                ):
                    continue

            title = window.title() or ""
            if not title:
                continue
            app_name = app.applicationName() if app else ""
            windows.append(
                WindowInfo(
                    title=title,
                    app_name=app_name,
                    window_id=window.windowID(),
                )
            )
        return windows

    async def start(self, window_id: Optional[int], monitor: int) -> Optional[int]:
        """Store the target window ID and monitor index.

        Args:
            window_id: Optional window ID to capture (from list_windows()).
            monitor: Monitor index when not capturing a specific window.

        Returns:
            The window ID if found, or None if not found or capturing full screen.

        """
        _ensure_frameworks()

        self._monitor = monitor
        matched_id = None

        if window_id is not None:
            # Verify the window exists
            content = await _get_shareable_content()
            for window in content.windows():
                if window.windowID() == window_id:
                    title = window.title() or ""
                    logger.debug(f"Found window: '{title}' (ID: {window_id})")
                    self._window_id = window_id
                    matched_id = window_id
                    break
            if matched_id is None:
                logger.warning(f"Window ID {window_id} not found, falling back to full screen")
                self._window_id = None
        else:
            self._window_id = None

        return matched_id

    async def capture(self) -> Optional[Tuple[bytes, Tuple[int, int]]]:
        """Capture a single screenshot via SCScreenshotManager.

        Each call freshly resolves the window/display filter to avoid stale state.

        Returns:
            Tuple of (rgb_bytes, (width, height)) or None on failure.

        """
        content = await _get_shareable_content()

        # Build filter
        sc_filter = None
        if self._window_id is not None:
            for window in content.windows():
                if window.windowID() == self._window_id:
                    sc_filter = _SCKit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(
                        window
                    )
                    break
            if sc_filter is None:
                logger.warning(f"Window ID {self._window_id} no longer found")
                return None

        if sc_filter is None:
            # Full screen capture
            displays = content.displays()
            monitor_index = self._monitor
            if monitor_index >= len(displays):
                logger.warning(f"Monitor index {monitor_index} out of range, using primary display")
                monitor_index = 0
            display = displays[monitor_index]
            sc_filter = _SCKit.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                display, []
            )

        # Build config
        config = _SCKit.SCStreamConfiguration.alloc().init()
        config.setScalesToFit_(True)

        # Capture
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def handler(image, error):
            if future.done():
                return
            if error is not None:
                err_str = str(error)
                if "permission" in err_str.lower() or "denied" in err_str.lower():
                    loop.call_soon_threadsafe(
                        future.set_exception,
                        PermissionError(
                            "Screen recording permission denied. "
                            "Grant access in System Settings > Privacy & Security > Screen Recording."
                        ),
                    )
                else:
                    loop.call_soon_threadsafe(
                        future.set_exception, RuntimeError(f"SCScreenshotManager error: {error}")
                    )
                return
            loop.call_soon_threadsafe(future.set_result, image)

        _SCKit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            sc_filter, config, handler
        )

        try:
            cg_image = await asyncio.wait_for(future, timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("Screenshot capture timed out (completion handler not called)")
            return None
        except PermissionError:
            raise
        except Exception as e:
            logger.error(f"Screenshot capture failed: {e}")
            return None

        if cg_image is None:
            return None

        return await loop.run_in_executor(None, _cgimage_to_rgb, cg_image)

    async def stop(self) -> None:
        """Release capture resources."""
        self._window_id = None
