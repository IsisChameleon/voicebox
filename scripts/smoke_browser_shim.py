"""Smoke test for the browser-shim audio path.

Spawns:
  * the pipecat child in BrowserShimRunnerArguments mode (WebSocket server on :9091)
  * a Playwright Chromium with the shim injected, navigated to about:blank

Then it:
  1. Waits for the shim's WS to connect.
  2. Calls ``speak("hello")`` via the IPC queues — verifies Kokoro audio gets
     pushed over the WebSocket toward the shim.
  3. Polls the page for ``window.__voiceShim`` debug counters.
  4. Tears everything down.

This validates the audio plumbing without needing the readme app.

Run: ``uv run python scripts/smoke_browser_shim.py``
"""

import asyncio
import sys
import time

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")


async def main():
    from pipecat_mcp_server.agent_ipc import (
        send_command,
        start_pipecat_process,
        stop_pipecat_process,
    )
    from pipecat_mcp_server.browser_session import start_browser, stop_browser
    from pipecat_mcp_server.runner_args import BrowserShimRunnerArguments

    audio_port = 9091
    cdp_port = 9222
    audio_ws_url = f"ws://localhost:{audio_port}"

    logger.info("=== starting pipecat in browser-shim mode ===")
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port, sample_rate=48000)
    )

    # Give pipecat a moment to bind the WS port.
    await asyncio.sleep(2)

    logger.info("=== launching Chromium with shim ===")
    try:
        info = await asyncio.to_thread(
            start_browser,
            url="http://localhost:3000",  # secure context for hook to install
            audio_ws_url=audio_ws_url,
            cdp_port=cdp_port,
            headless=True,
        )
        logger.info(f"browser ready: {info}")
    except Exception as e:
        logger.error(f"start_browser failed: {e}")
        stop_pipecat_process()
        sys.exit(1)

    # Wait for the shim to connect to the WS.
    logger.info("=== waiting for shim to connect ===")
    await asyncio.sleep(3)

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(info["cdp_endpoint"])
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        logger.info(f"page url: {page.url}, title: {(await page.title())!r}")

        shim_state = await page.evaluate(
            "() => Object.assign({}, window.__voiceShim, {"
            " hasMediaDevices: window.__voiceShim?.hasMediaDevices,"
            " hasWebCodecs: window.__voiceShim?.hasWebCodecs"
            "})"
        )
        logger.info(f"shim state (pre-speak): {shim_state}")

        if not shim_state.get("installed"):
            logger.error("✗ shim never installed")
            await browser.close()
            await asyncio.to_thread(stop_browser)
            stop_pipecat_process()
            sys.exit(1)
        if not shim_state.get("wsReady"):
            logger.error("✗ shim WS not ready — pipecat not reachable from browser")
        else:
            logger.success("✓ shim WS connected to pipecat")
        if not shim_state.get("micHookInstalled"):
            logger.warning("⚠ mic hook not installed — secure-context issue?")

        logger.info("=== sending speak() ===")
        r = await send_command("speak", text="hello world this is a test")
        logger.info(f"speak response: {r}")

        await asyncio.sleep(5)
        shim_state2 = await page.evaluate(
            "() => ({"
            " wsReady: window.__voiceShim?.wsReady,"
            " inbound: window.__voiceShim?.inboundChunks,"
            " outbound: window.__voiceShim?.outboundChunks,"
            " errors: window.__voiceShim?.errors,"
            "})"
        )
        logger.info(f"shim state (post-speak): {shim_state2}")
        if shim_state2["inbound"] > 0:
            logger.success(f"✓ shim received {shim_state2['inbound']} audio chunks from pipecat")
        else:
            logger.error("✗ no inbound audio chunks received")

        await browser.close()

    logger.info("=== teardown ===")
    await asyncio.to_thread(stop_browser)
    stop_pipecat_process()
    logger.info("done.")


if __name__ == "__main__":
    asyncio.run(main())
