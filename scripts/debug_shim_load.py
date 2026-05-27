"""Minimal: launch Playwright Chromium with the shim, eval features, dump console.

Used to debug why ``window.__voiceShim`` may be undefined when the launcher
in ``browser_session.py`` is used end-to-end.
"""

import asyncio
import sys
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

logger.remove()
logger.add(sys.stderr, level="INFO")


SHIM_PATH = Path(__file__).resolve().parent.parent / "src" / "pipecat_mcp_server" / "shim.js"


async def main(headless: bool):
    shim_src = SHIM_PATH.read_text(encoding="utf-8")
    init_script = f"window.__VOICE_SHIM_WS_URL__ = 'ws://localhost:9091';\n{shim_src}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--use-fake-ui-for-media-stream",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(permissions=["microphone"])

        consoles: list[str] = []
        page_errors: list[str] = []

        async def setup_listeners(page):
            page.on("console", lambda m: consoles.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)))

        await context.add_init_script(init_script)
        page = await context.new_page()
        await setup_listeners(page)

        await page.goto("about:blank")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.0)

        diag = await page.evaluate(
            """
            () => ({
              shimExists: typeof window.__voiceShim !== 'undefined',
              shimKeys: window.__voiceShim ? Object.keys(window.__voiceShim) : null,
              wsReady: window.__voiceShim?.wsReady,
              audioWsUrl: window.__voiceShim?.audioWsUrl,
              hasMSTG: typeof MediaStreamTrackGenerator !== 'undefined',
              hasMSTP: typeof MediaStreamTrackProcessor !== 'undefined',
              hasAudioData: typeof AudioData !== 'undefined',
              hasWebSocket: typeof WebSocket !== 'undefined',
              hasRTCPC: typeof RTCPeerConnection !== 'undefined',
              userAgent: navigator.userAgent,
              location: location.href,
              isSecureContext: window.isSecureContext,
            })
            """
        )
        logger.info(f"about:blank diag: {diag}")
        for c in consoles:
            logger.info(f"console: {c}")
        for e in page_errors:
            logger.warning(f"pageerror: {e}")

        # Try data: URL with a real document.
        consoles.clear()
        page_errors.clear()
        await page.goto("data:text/html,<!doctype html><title>shim test</title><script>console.log('inline OK')</script>")
        await asyncio.sleep(1.0)
        diag2 = await page.evaluate(
            """
            () => ({
              shimExists: typeof window.__voiceShim !== 'undefined',
              wsReady: window.__voiceShim?.wsReady,
              hasMSTG: typeof MediaStreamTrackGenerator !== 'undefined',
              isSecureContext: window.isSecureContext,
              location: location.href,
            })
            """
        )
        logger.info(f"data: diag: {diag2}")
        for c in consoles:
            logger.info(f"console (data): {c}")
        for e in page_errors:
            logger.warning(f"pageerror (data): {e}")

        await browser.close()


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    logger.info(f"running with headless={headless}")
    asyncio.run(main(headless))
