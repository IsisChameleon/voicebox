"""Smoke test for Stage 1 full-duplex IPC (speak during a pending listen).

Spawns the pipecat child + a shim-injected Chromium (same plumbing as
``smoke_browser_shim.py``), then:

  1. Drains startup events, then issues ``listen(timeout=15)`` as a
     background task at the live cursor.
  2. 2 s later — with the listen still pending — issues ``speak(...)``.
  3. Asserts the speak response arrives fast (serial pre-Stage-1 IPC would
     queue it ~15 s behind the listen), that Kokoro audio reaches the shim
     during that window, and that the pending listen resolves with the
     ``tester_speech_started`` event our own speak produced (out-of-order
     responses routed by correlation id).
  4. Issues ``speak(wait=True)`` and asserts a usable playout span
     (``started_at`` < ``finished_at``) is reported.

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

    # Drain the startup events (session_started, client_connected) so the
    # background listen below genuinely blocks on FUTURE events.
    drained = await send_command("listen", timeout=2.0, cursor=0, deadline=30.0)
    cursor = drained["cursor"]
    logger.info(f"startup events: {[e['type'] for e in drained['events']]} (cursor={cursor})")

    logger.info("=== issuing listen(timeout=15) in the background ===")
    listen_task = asyncio.create_task(
        send_command("listen", timeout=15.0, cursor=cursor, deadline=45.0)
    )

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

    if speak_rtt > 10.0:
        failures.append(f"speak took {speak_rtt:.1f}s — likely queued behind the listen")
    else:
        logger.success(f"✓ speak completed in {speak_rtt:.2f}s with the listen in flight")

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

    logger.info("=== awaiting the listen result (should carry our tts events) ===")
    try:
        listen_response = await listen_task
        cursor = listen_response["cursor"]
        types = [e["type"] for e in listen_response["events"]]
        logger.info(f"listen resolved with events: {types}")
        if "tester_speech_started" in types:
            logger.success("✓ the pending listen captured our own speak as tester_speech events")
        else:
            failures.append(f"expected tester_speech_started in the listen events, got {types}")
    except Exception as e:
        failures.append(f"listen failed: {e}")

    logger.info("=== speak(wait=True) — playout timing ===")
    timed = await send_command(
        "speak", text="and this one waits for playout to finish", wait=True, deadline=150.0
    )
    logger.info(f"speak(wait=True) response: {timed}")
    started, finished = timed.get("started_at"), timed.get("finished_at")
    if started and finished and finished > started:
        logger.success(
            f"✓ playout span reported: {finished - started:.2f}s, "
            f"interrupted={timed.get('interrupted')}"
        )
    else:
        failures.append(f"speak(wait=True) returned no usable playout span: {timed}")

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
