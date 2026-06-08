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
or via ``@playwright/mcp`` with ``PLAYWRIGHT_MCP_CDP_ENDPOINT=http://localhost:<cdp_port>``
set before opening a session (env var wins over ``--config`` because the daemon
must be started fresh — reusing an existing daemon ignores the config).
"""

import multiprocessing
from pathlib import Path
from typing import Optional

from loguru import logger

SHIM_PATH = Path(__file__).parent / "shim.js"

_browser_process: Optional[multiprocessing.Process] = None
_ready_event: Optional[multiprocessing.Event] = None
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
    """
    global _browser_process, _ready_event, _stop_event

    stop_browser()

    _ready_event = multiprocessing.Event()
    _stop_event = multiprocessing.Event()
    _browser_process = multiprocessing.Process(
        target=_run_browser,
        args=(
            url,
            audio_ws_url,
            cdp_port,
            headless,
            user_data_dir,
            _ready_event,
            _stop_event,
        ),
    )
    _browser_process.start()
    logger.debug(f"Browser child process PID {_browser_process.ident}")

    if not _ready_event.wait(timeout=startup_timeout):
        stop_browser()
        raise RuntimeError(f"Browser failed to become ready within {startup_timeout}s")

    cdp_endpoint = f"http://localhost:{cdp_port}"
    return {
        "cdp_endpoint": cdp_endpoint,
        "audio_ws_url": audio_ws_url,
        "playwright_mcp_env": f"PLAYWRIGHT_MCP_CDP_ENDPOINT={cdp_endpoint} PLAYWRIGHT_MCP_ISOLATED=false",
        "attach_hint": f"playwright-cli close-all && PLAYWRIGHT_MCP_CDP_ENDPOINT={cdp_endpoint} PLAYWRIGHT_MCP_ISOLATED=false playwright-cli",
    }


def stop_browser():
    """Tear down the Playwright-controlled Chromium, if running."""
    global _browser_process, _ready_event, _stop_event

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

    _ready_event = None
    _stop_event = None


def _run_browser(
    url: str,
    audio_ws_url: str,
    cdp_port: int,
    headless: bool,
    user_data_dir: Optional[str],
    ready_event,
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
            ready_event,
            stop_event,
        )
    )


async def _run_browser_async(
    url: str,
    audio_ws_url: str,
    cdp_port: int,
    headless: bool,
    user_data_dir: Optional[str],
    ready_event,
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
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.error(f"page.goto failed: {e}")

        logger.info(f"Browser ready. CDP: http://localhost:{cdp_port} | audio: {audio_ws_url}")
        ready_event.set()

        try:
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
