"""Smoke test for Stage 1 full-duplex IPC (speak during a pending listen).

Spawns the pipecat child + a shim-injected Chromium (same plumbing as
``smoke_browser_shim.py``), then:

  1. Issues ``listen(timeout=15)`` as a background task.
  2. 2 s later — with the listen still pending — issues ``speak(...)``.
  3. Asserts the speak response arrives while the listen is STILL pending
     (serial pre-Stage-1 IPC would queue it behind the listen), and that
     Kokoro audio reaches the shim during that window.
  4. Asserts the listen later resolves on its own (out-of-order response
     routed to the right caller by correlation id).

Run: ``uv run python scripts/smoke_full_duplex.py``
"""

import asyncio
import sys
import time

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")


async def main():
    """Run the full-duplex smoke sequence; exit non-zero on any failure."""
    from voicebox.agent_ipc import (
        send_command,
        start_pipecat_process,
        stop_pipecat_process,
    )
    from voicebox.browser_session import start_browser, stop_browser
    from voicebox.runner_args import BrowserShimRunnerArguments
    from voicebox.server import _assert_port_free

    audio_port = 9091
    cdp_port = 9222

    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")

    logger.info("=== starting pipecat in browser-shim mode ===")
    start_pipecat_process(BrowserShimRunnerArguments(host="localhost", port=audio_port))
    await asyncio.sleep(2)

    logger.info("=== launching Chromium with shim ===")
    try:
        info = await asyncio.to_thread(
            start_browser,
            url="http://localhost:3000",  # secure context so the hooks install
            audio_ws_url=f"ws://localhost:{audio_port}",
            cdp_port=cdp_port,
            headless=True,
        )
    except Exception as e:
        logger.error(f"start_browser failed: {e}")
        stop_pipecat_process()
        sys.exit(1)

    # Give the shim a moment to connect its WebSocket.
    await asyncio.sleep(3)

    failures = []

    logger.info("=== issuing listen(timeout=15) in the background ===")
    listen_task = asyncio.create_task(send_command("listen", timeout=15.0, deadline=45.0))

    await asyncio.sleep(2)
    if listen_task.done():
        failures.append(f"listen resolved too early: {listen_task.result()!r}")
    else:
        logger.success("✓ listen still pending after 2s")

    logger.info("=== issuing speak() while listen is pending ===")
    t0 = time.monotonic()
    speak_response = await send_command(
        "speak", text="testing full duplex, speaking while listening", deadline=60.0
    )
    speak_rtt = time.monotonic() - t0
    logger.info(f"speak response: {speak_response} (rtt {speak_rtt:.2f}s)")

    if listen_task.done():
        failures.append("listen resolved before speak returned — responses not concurrent")
    elif speak_rtt > 10.0:
        failures.append(f"speak took {speak_rtt:.1f}s — likely queued behind the listen")
    else:
        logger.success(f"✓ speak completed in {speak_rtt:.2f}s while listen stayed pending")

    # Kokoro audio must reach the shim during the pending listen.
    await asyncio.sleep(4)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(info["cdp_endpoint"])
        page = browser.contexts[0].pages[0]
        inbound = await page.evaluate("() => window.__voiceShim?.inboundChunks")
        await browser.close()
    if inbound and inbound > 0:
        logger.success(f"✓ shim received {inbound} audio chunks during the pending listen")
    else:
        failures.append(f"no audio reached the shim during the listen (inbound={inbound})")

    logger.info("=== awaiting the listen result (should time out to '') ===")
    try:
        listen_response = await listen_task
        logger.success(f"✓ listen resolved independently: {listen_response!r}")
    except Exception as e:
        failures.append(f"listen failed: {e}")

    logger.info("=== teardown ===")
    try:
        await send_command("stop", deadline=30.0)
    except Exception as e:
        logger.warning(f"graceful stop failed: {e}")
    await asyncio.to_thread(stop_browser)
    stop_pipecat_process()

    if failures:
        for f in failures:
            logger.error(f"✗ {f}")
        sys.exit(1)
    logger.success("✓ full-duplex smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
