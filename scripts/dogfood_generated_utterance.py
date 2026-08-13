"""Live dogfood for the generated-utterance playout path (issue #19).

Runs a real session against the bundled fake app (Nova, ``tests/eval/fake_app``)
and checks the three contracts unit tests cannot see:

  1. ``speak(wait_for_playout=True)`` resolves at real audio end.
  2. Exactly one ``tester_speech_started``/``stopped`` pair per ``speak()`` —
     i.e. the utterance reached the app as ONE contiguous span, not several.
  3. The app bot does not take a turn mid-utterance.

Start Nova first::

    VOICEBOX_FAKE_APP_LLM_PROVIDER=scripted uv run python tests/eval/fake_app/bot.py

Run: ``uv run python scripts/dogfood_generated_utterance.py``
"""

import asyncio
import json
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

APP_URL = "http://localhost:7860"
RECORD_DIR = "temp/dogfood"

# Long enough that Kokoro yields multiple chunks: the six-sentence probe took
# 19 s to its first chunk and produced two. A single-chunk utterance would not
# exercise the buffer at all.
LONG_UTTERANCE = (
    "Hello Nova, I would like to introduce myself properly before we begin the quiz. "
    "I have been reading about the outer planets for several weeks now. "
    "Saturn's rings fascinate me the most, especially the gaps between them. "
    "I also find the moons of Jupiter remarkable, particularly Europa and its hidden ocean. "
    "Please ask me a difficult question about the solar system when I finish speaking. "
    "I am ready to be tested on anything you choose."
)


async def main():
    """Run one live session against Nova and report the playout contracts."""
    from voicebox.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process
    from voicebox.browser_session import start_browser, stop_browser
    from voicebox.runner_args import BrowserShimRunnerArguments
    from voicebox.server import _assert_port_free

    audio_port, cdp_port = 9091, 9222
    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")

    logger.info("=== starting pipecat child ===")
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port, record_dir=RECORD_DIR)
    )
    await asyncio.sleep(3)

    logger.info(f"=== launching Chromium at {APP_URL} ===")
    info = await asyncio.to_thread(
        start_browser,
        url=APP_URL,
        audio_ws_url=f"ws://localhost:{audio_port}",
        cdp_port=cdp_port,
        headless=True,
        record_dir=RECORD_DIR,
    )
    logger.info(f"browser ready: {info['cdp_endpoint']}")

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(info["cdp_endpoint"])
            page = browser.contexts[0].pages[0]

            logger.info("=== clicking Connect ===")
            await page.get_by_role("button", name="Connect").click(timeout=30000)
            await asyncio.sleep(12)  # let WebRTC negotiate and Nova greet

            diag = await page.evaluate("window.__voiceShim && {...window.__voiceShim}")
            logger.info(f"shim: installed={diag.get('installed')} wsReady={diag.get('wsReady')}")

            logger.info("=== speak(wait_for_playout=True) with a multi-chunk utterance ===")
            loop = asyncio.get_event_loop()
            queued_at = loop.time()
            result = await send_command(
                "speak", text=LONG_UTTERANCE, wait_for_playout=True, deadline=180.0
            )
            logger.info(f"speak returned after {loop.time() - queued_at:.1f}s: {result}")

            await asyncio.sleep(8)  # let Nova reply
            events = await send_command("listen", timeout=20, cursor=0, deadline=60.0)
    finally:
        logger.info("=== stopping ===")
        artifacts = await send_command("stop", deadline=210.0)
        logger.info(f"artifacts: {artifacts}")
        await asyncio.to_thread(stop_browser)
        stop_pipecat_process()

    log = events.get("events", [])
    with open(f"{RECORD_DIR}/dogfood_events.json", "w") as fh:
        json.dump(log, fh, indent=2)

    print("\n===== EVENT LOG =====")
    for e in log:
        print(f"  {e['t']:.2f}  {e['type']:32} {str(e.get('text', ''))[:70]}")

    starts = [e for e in log if e["type"] == "tester_speech_started"]
    stops = [e for e in log if e["type"] == "tester_speech_stopped"]
    print("\n===== CONTRACTS =====")
    print(f"  played                        : {result.get('played')}")
    print(f"  tester_speech_started count   : {len(starts)}  (expect 1)")
    print(f"  tester_speech_stopped count   : {len(stops)}  (expect 1)")
    if starts and stops:
        print(f"  contiguous span               : {stops[-1]['t'] - starts[0]['t']:.1f}s")

    bot_starts = [e for e in log if e["type"] == "app_bot_speech_started"]
    if starts and stops:
        during = [e for e in bot_starts if starts[0]["t"] < e["t"] < stops[-1]["t"]]
        print(f"  app bot turns mid-utterance   : {len(during)}  (expect 0)")


if __name__ == "__main__":
    asyncio.run(main())
