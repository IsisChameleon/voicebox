"""Smoke test for T5: connection-state and deadline coherence for ``speak``.

Exercises the LIVE audio path for the behaviors that unit tests can only prove
in isolation (``tests/test_speak_deadlines.py`` covers the pipeline-free parts):

  1. speak while connected succeeds and logs a ``tester_transcript``.
  2. Reload the page (drops then re-establishes the shim WebSocket), then speak
     again: the second speak either succeeds after the reconnect or fails with
     the named "no browser client connected" error — NEVER a silent success.
     Proven by pairing accepted speaks with ``tester_transcript`` events in the
     log.
  3. Navigate the page away (about:blank drops the shim and it does NOT
     reconnect), then issue a ``wait_for_turn`` speak with the real derived
     deadline. It must fail with the named connection error, and NO
     ``tester_speech_started`` (nor ``tester_transcript``) event may appear in
     the log afterwards — the no-ghost-speech invariant (finding F4), polled from
     the event log to prove it.

ENVIRONMENT NOTE: this needs the real Kokoro/Whisper models, which cannot be
downloaded in the CI/agent sandbox (the Kokoro model host returns 403 there).
Run it on localhost with a warm model cache:

    uv run python scripts/smoke_reconnect.py
"""

import asyncio
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")


async def _event_types_since(send_command, cursor):
    """Drain the event log from ``cursor`` and return (types, next_cursor)."""
    resp = await send_command("listen", timeout=1.0, cursor=cursor, deadline=30.0)
    return [e["type"] for e in resp["events"]], resp["cursor"]


async def _shim_ws_ready(cdp_endpoint) -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_endpoint)
        page = browser.contexts[0].pages[0]
        ready = await page.evaluate("() => !!window.__voiceShim?.wsReady")
        await browser.close()
    return bool(ready)


async def _navigate(cdp_endpoint, url):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_endpoint)
        page = browser.contexts[0].pages[0]
        await page.goto(url)
        await browser.close()


async def main():
    """Run the reconnect + deadline-coherence smoke; exit non-zero on failure."""
    from voicebox.agent_ipc import (
        send_command,
        start_pipecat_process,
        stop_pipecat_process,
        wait_for_pipecat_ready,
    )
    from voicebox.browser_session import start_browser, stop_browser
    from voicebox.runner_args import BrowserShimRunnerArguments
    from voicebox.server import _assert_port_free
    from voicebox.timeouts import speak_parent_deadline

    audio_port = 9091
    cdp_port = 9222

    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")

    logger.info("=== starting pipecat in browser-shim mode ===")
    start_pipecat_process(BrowserShimRunnerArguments(host="localhost", port=audio_port))
    await wait_for_pipecat_ready(timeout=300.0)

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

    failures: list[str] = []
    cdp = info["cdp_endpoint"]

    # Let the shim connect its WS.
    await asyncio.sleep(3)
    cursor = 0

    plain_deadline = speak_parent_deadline(wait_for_turn=False, wait_for_playout=False)
    turn_deadline = speak_parent_deadline(wait_for_turn=True, wait_for_playout=False)

    # ----- (1) speak while connected succeeds -----
    logger.info("=== (1) speak while connected ===")
    try:
        r = await send_command("speak", text="first hello", deadline=plain_deadline)
        logger.info(f"speak #1: {r}")
        if not r.get("queued"):
            failures.append(f"speak #1 did not report queued: {r}")
    except Exception as e:
        failures.append(f"speak #1 unexpectedly failed while connected: {e}")

    types, cursor = await _event_types_since(send_command, cursor)
    logger.info(f"events after speak #1: {types}")
    if "tester_transcript" not in types:
        failures.append("speak #1 accepted but no tester_transcript logged")

    # ----- (2) reload the page, then speak again -----
    logger.info("=== (2) reload page (drops + re-establishes the shim WS) ===")
    await _navigate(cdp, "http://localhost:3000")
    # Give the shim up to the grace window to reconnect.
    reconnected = False
    for _ in range(15):
        await asyncio.sleep(1)
        if await _shim_ws_ready(cdp):
            reconnected = True
            break
    logger.info(f"shim reconnected after reload: {reconnected}")

    speak2_accepted = False
    try:
        r = await send_command("speak", text="hello again after reload", deadline=plain_deadline)
        logger.info(f"speak #2: {r}")
        speak2_accepted = bool(r.get("queued"))
    except Exception as e:
        # A named connection error is an acceptable outcome — NOT a silent success.
        if "no browser client connected" in str(e):
            logger.info(f"speak #2 correctly refused (no reconnect): {e}")
        else:
            failures.append(f"speak #2 failed with an unexpected error: {e}")

    types, cursor = await _event_types_since(send_command, cursor)
    logger.info(f"events after speak #2: {types}")
    transcript_after = types.count("tester_transcript")
    # The invariant: a tester_transcript appears IFF the speak was accepted.
    if speak2_accepted and transcript_after == 0:
        failures.append("speak #2 reported queued but logged no tester_transcript")
    if not speak2_accepted and transcript_after > 0:
        failures.append("speak #2 was refused yet a tester_transcript was logged (silent success!)")

    # ----- (3) drop the client, then wait_for_turn must expire without a ghost -----
    logger.info("=== (3) navigate away; wait_for_turn speak must expire, no ghost ===")
    await _navigate(cdp, "about:blank")  # insecure context: shim WS drops and stays down
    await asyncio.sleep(2)

    # Snapshot the cursor: nothing tester_speech_* may appear at or after here.
    _, cursor = await _event_types_since(send_command, cursor)

    ghost_refused = False
    try:
        r = await send_command(
            "speak", text="ghost interjection", wait_for_turn=True, deadline=turn_deadline
        )
        failures.append(f"wait_for_turn speak unexpectedly succeeded while disconnected: {r}")
    except Exception as e:
        if "no browser client connected" in str(e) or "did not fall silent" in str(e):
            ghost_refused = True
            logger.info(f"wait_for_turn speak correctly failed: {e}")
        else:
            failures.append(f"wait_for_turn speak failed with an unexpected error: {e}")

    # Poll a little AFTER the error to catch any ghost audio the child might still
    # emit — the whole point is that none appears.
    await asyncio.sleep(3)
    types, cursor = await _event_types_since(send_command, cursor)
    logger.info(f"events after the refused wait_for_turn speak: {types}")
    if ghost_refused and ("tester_speech_started" in types or "tester_transcript" in types):
        failures.append(f"GHOST SPEECH: tester events appeared after a failed speak: {types}")

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
    logger.success("✓ reconnect + deadline-coherence smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
