#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Launches a Playwright-controlled Chromium with the browser audio shim.

The shim (``shim.js``) intercepts ``getUserMedia`` and wraps
``RTCPeerConnection`` so the page's mic comes from our MCP server and the
page's remote audio is teed back to it — over a raw-PCM WebSocket served
by ``BrowserShimRunnerArguments``.

Chromium is exposed on CDP port ``cdp_port`` so an external Playwright
client can attach via ``chromium.connect_over_cdp("http://localhost:<cdp_port>")``
or via ``playwright-cli attach --cdp http://localhost:<cdp_port>`` — ``attach``
takes a snapshot and performs no navigation, so it leaves the shim tab intact.
"""

import multiprocessing
from pathlib import Path
from queue import Empty
from typing import Optional

from loguru import logger

SHIM_PATH = Path(__file__).parent / "shim.js"

# How long the child waits for the page to navigate and the shim to install
# itself before it reports startup failure to the parent.
SHIM_READY_TIMEOUT_SECS = 10.0

_browser_process: Optional[multiprocessing.Process] = None
_startup_queue: Optional[multiprocessing.Queue] = None
_stop_event: Optional[multiprocessing.Event] = None


def start_browser(
    url: str,
    audio_ws_url: str,
    cdp_port: int = 9222,
    headless: bool = False,
    user_data_dir: Optional[str] = None,
    startup_timeout: float = 60.0,
) -> dict:
    """Launch Chromium with the shim pre-injected. Blocks until the page is loaded.

    ``user_data_dir`` reuses a full persistent Chrome profile so callers don't
    have to log in every run; the profile lives in the browser's default
    context, which is CDP-coherent (an attached client both drives and shares
    its cookies).

    Returns a dict with ``cdp_endpoint`` (HTTP URL for ``connect_over_cdp``)
    and ``audio_ws_url``.

    Raises:
        RuntimeError: if the page did not navigate or the shim did not install —
            the child's error text is included. A browser sitting on
            ``about:blank`` is never reported as a started session.

    """
    global _browser_process, _startup_queue, _stop_event

    stop_browser()

    _startup_queue = multiprocessing.Queue()
    _stop_event = multiprocessing.Event()
    _browser_process = multiprocessing.Process(
        target=_run_browser,
        args=(
            url,
            audio_ws_url,
            cdp_port,
            headless,
            user_data_dir,
            _startup_queue,
            _stop_event,
        ),
    )
    _browser_process.start()
    logger.debug(f"Browser child process PID {_browser_process.ident}")

    try:
        result = _startup_queue.get(timeout=startup_timeout)
    except Empty:
        stop_browser()
        raise RuntimeError(f"Browser failed to become ready within {startup_timeout}s") from None

    if not result.get("ok"):
        stop_browser()
        raise RuntimeError(f"Browser session startup failed: {result.get('error')}")

    cdp_endpoint = f"http://localhost:{cdp_port}"
    return {
        "cdp_endpoint": cdp_endpoint,
        "audio_ws_url": audio_ws_url,
        "attach_hint": f"playwright-cli attach --cdp {cdp_endpoint}",
    }


def stop_browser():
    """Tear down the Playwright-controlled Chromium, if running."""
    global _browser_process, _startup_queue, _stop_event

    if _stop_event is not None:
        _stop_event.set()

    if _browser_process is not None:
        if _browser_process.is_alive():
            _browser_process.join(timeout=5.0)
            if _browser_process.is_alive():
                logger.debug("Terminating browser process")
                _browser_process.terminate()
                _browser_process.join(timeout=5.0)
            if _browser_process.is_alive():
                logger.debug("Killing browser process")
                _browser_process.kill()
                _browser_process.join(timeout=5.0)
        _browser_process = None

    if _startup_queue is not None:
        _startup_queue.close()
        _startup_queue = None
    _stop_event = None


def _run_browser(
    url: str,
    audio_ws_url: str,
    cdp_port: int,
    headless: bool,
    user_data_dir: Optional[str],
    startup_queue,
    stop_event,
):
    """Child-process entry. Runs the asyncio loop with Playwright."""
    import asyncio

    asyncio.run(
        _run_browser_async(
            url,
            audio_ws_url,
            cdp_port,
            headless,
            user_data_dir,
            startup_queue,
            stop_event,
        )
    )


async def _wait_for_shim(page, timeout: float):
    """Poll until the page has navigated away from ``about:blank`` and the shim installed.

    Args:
        page: The Playwright page the shim was injected into.
        timeout: Seconds to poll before giving up.

    Raises:
        RuntimeError: if either condition is still unmet when the timeout expires.

    """
    import asyncio
    import time

    probe = "() => !!(window.__voiceShim && window.__voiceShim.installed)"
    deadline = time.monotonic() + timeout
    state = "not polled"
    while time.monotonic() < deadline:
        try:
            installed = await page.evaluate(probe)
        except Exception as e:
            # A navigation in flight destroys the execution context; keep polling.
            installed = False
            logger.debug(f"shim probe failed, retrying: {e}")
        if page.url != "about:blank" and installed:
            return
        state = f"url={page.url!r}, shim installed={installed}"
        await asyncio.sleep(0.2)
    raise RuntimeError(f"page did not become ready within {timeout}s ({state})")


async def _run_browser_async(
    url: str,
    audio_ws_url: str,
    cdp_port: int,
    headless: bool,
    user_data_dir: Optional[str],
    startup_queue,
    stop_event,
):
    import asyncio

    from playwright.async_api import async_playwright

    shim_src = SHIM_PATH.read_text(encoding="utf-8")
    init_script = f"window.__VOICE_SHIM_WS_URL__ = {audio_ws_url!r};\n{shim_src}"

    chromium_args = [
        f"--remote-debugging-port={cdp_port}",
        "--use-fake-ui-for-media-stream",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]

    async with async_playwright() as p:
        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                args=chromium_args,
                permissions=["microphone"],
            )
            browser = None
        else:
            browser = await p.chromium.launch(
                headless=headless,
                args=chromium_args,
            )
            context = await browser.new_context(permissions=["microphone"])

        page = await context.new_page()
        # Inject shim into this page only, not every future tab. New tabs opened
        # by an attached CDP client must not connect to the audio WS — if they
        # did, pipecat would kick the active connection and start a 1 Hz storm.
        await page.add_init_script(init_script)

        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _wait_for_shim(page, timeout=SHIM_READY_TIMEOUT_SECS)
            except Exception as e:
                # The parent must not be told a session started when the page
                # never navigated — a blank tab wastes the caller's whole run.
                logger.error(f"Browser startup failed for {url}: {e}")
                startup_queue.put({"ok": False, "error": str(e)})
                return

            logger.info(f"Browser ready. CDP: http://localhost:{cdp_port} | audio: {audio_ws_url}")
            startup_queue.put({"ok": True})

            # Park here until parent asks us to stop or the browser dies.
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
                if browser is not None and not browser.is_connected():
                    logger.warning("Browser disconnected")
                    break
        finally:
            try:
                await context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            logger.info("Browser child exiting")
