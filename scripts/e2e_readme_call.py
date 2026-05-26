"""End-to-end test of the browser-session flow against the readme app.

Sequence:
  1. start_browser_session(http://localhost:3000) — pipecat + Chromium + shim
  2. Connect via CDP and drive the UI:
       a. login with the test account
       b. land on /h/<uid>, pick the first reader, pick a book
       c. wait for the call page to reach `connected` / `ready`
  3. Once connected, exchange a few turns via speak / listen.
  4. End the call by clicking the red cross (aria-label="End session").
  5. Tear everything down.

Run: ``uv run python scripts/e2e_readme_call.py``
"""

import asyncio
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")


EMAIL = "isabelledebacker@live.com.au"
PASSWORD = "embertales456"


async def login(page, email: str, password: str):
    logger.info(f"page url: {page.url}")
    if "/auth/login" not in page.url:
        return
    # AuthPage uses <Input> components — labels by placeholder/type may vary.
    # Use semantic email/password fields.
    await page.fill('input[type="email"]', email)
    await page.fill('input[type="password"]', password)
    # Submit the form
    await page.get_by_role("button", name="Log in", exact=False).first.click()
    # Wait for redirect away from /auth/login
    await page.wait_for_url(lambda u: "/auth/login" not in u, timeout=15000)
    logger.info(f"logged in; now at {page.url}")


async def navigate_to_call(page):
    """From the household home, click into the first reader, then the first book.

    Returns once we're on a `/call` URL.
    """
    # We may already be on call (if a continue-reading link was clicked).
    if "/call" in page.url:
        return
    # Click the first ReaderActionCard's CTA — either "Continue reading" or "Pick a story".
    # Look for either; whichever exists first.
    logger.info(f"household page url: {page.url}")
    # Cards are <motion.div> elements with the kid's name. The CTA is a button or
    # element containing one of these labels.
    candidates = ["Continue reading", "Pick a story", "Pick another"]
    clicked = False
    for label in candidates:
        try:
            loc = page.get_by_text(label, exact=False).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                clicked = True
                logger.info(f"clicked CTA: {label}")
                break
        except Exception as e:
            logger.debug(f"locator {label!r}: {e}")
    if not clicked:
        raise RuntimeError("could not find any reader CTA on the household page")

    # If we land on the reader page (book picker), pick the first book.
    try:
        await page.wait_for_url(lambda u: "/r/" in u and "/call" not in u, timeout=5000)
        logger.info(f"reader page: {page.url} — need to pick a book")
        # Books are likely cards with titles. Click the first one with any link or
        # button child. Heuristic: anything that looks like a card.
        # Easiest: find any anchor whose href contains "/call?bookId=" and click it.
        book = page.locator('a[href*="/call?bookId="]').first
        await book.wait_for(state="visible", timeout=10000)
        await book.click()
    except Exception:
        pass

    await page.wait_for_url(lambda u: "/call" in u, timeout=10000)
    logger.info(f"on call page: {page.url}")


async def wait_for_connected(page, timeout: float = 60.0):
    """Wait until the status indicator text reads `connected` or `ready`."""
    logger.info("waiting for call to connect…")
    end = asyncio.get_event_loop().time() + timeout
    last_state = None
    while asyncio.get_event_loop().time() < end:
        try:
            state = await page.evaluate(
                "() => document.querySelector('div[style*=\"color: var(--muted-foreground)\"]')?.innerText"
            )
        except Exception:
            state = None
        if state != last_state:
            logger.info(f"connection state: {state!r}")
            last_state = state
        if state in ("connected", "ready"):
            return state
        await asyncio.sleep(0.5)
    raise TimeoutError(f"call did not reach connected within {timeout}s (last: {last_state!r})")


async def click_red_cross(page):
    """Click the X button with aria-label="End session"."""
    logger.info("clicking red cross to end call")
    await page.get_by_role("button", name="End session").click()


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

    logger.info("=== starting pipecat (browser-shim mode) ===")
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port, sample_rate=48000)
    )
    await asyncio.sleep(2)

    logger.info("=== launching browser ===")
    info = await asyncio.to_thread(
        start_browser,
        url="http://localhost:3000",
        audio_ws_url=audio_ws_url,
        cdp_port=cdp_port,
        headless=True,
    )
    logger.info(f"browser ready: {info}")

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(info["cdp_endpoint"])
        ctx = browser.contexts[0]
        page = ctx.pages[0]
        page.on("console", lambda m: logger.debug(f"console[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: logger.warning(f"pageerror {e}"))

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
            await login(page, EMAIL, PASSWORD)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await navigate_to_call(page)
            await wait_for_connected(page, timeout=90.0)

            # First turn: greet
            logger.info("=== speak: greet ===")
            await send_command("speak", text="Hi Ember! Can you tell me about this book in one short sentence?")
            await asyncio.sleep(1)

            logger.info("=== listen ===")
            r = await send_command("listen", timeout=45.0)
            transcript = r.get("text", "")
            logger.info(f"ember (turn 1): {transcript!r}")

            if not transcript:
                logger.warning("empty transcript — checking shim audio counters")
                state = await page.evaluate(
                    "() => ({inbound: window.__qzShim?.inboundChunks, outbound: window.__qzShim?.outboundChunks, pcCount: window.__qzShim?.pcCount, audioTrackCount: window.__qzShim?.audioTrackCount})"
                )
                logger.info(f"shim counters: {state}")

            # Second turn
            logger.info("=== speak: follow-up ===")
            await send_command("speak", text="Who is the main character?")
            await asyncio.sleep(1)
            r2 = await send_command("listen", timeout=30.0)
            logger.info(f"ember (turn 2): {r2.get('text','')!r}")

            # End call by clicking red cross
            await click_red_cross(page)
            await asyncio.sleep(2)
            logger.info(f"after end-call, page url: {page.url}")

        finally:
            try:
                await browser.close()
            except Exception:
                pass

    logger.info("=== teardown ===")
    await asyncio.to_thread(stop_browser)
    stop_pipecat_process()
    logger.info("done.")


if __name__ == "__main__":
    asyncio.run(main())
