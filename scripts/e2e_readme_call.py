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
import os
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")
# Also tee to an artifacts log so we have a clean transcript artifact.
_LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "artifacts", "e2e_readme_call", "run.log")
)
os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
logger.add(_LOG_PATH, level="INFO", mode="w")


EMAIL = "isabelledebacker@live.com.au"
PASSWORD = "embertales456"


async def login(page, email: str, password: str):
    """Navigate to login if needed and submit credentials."""
    logger.info(f"page url: {page.url}")
    if "/auth/login" not in page.url:
        return
    # AuthPage uses placeholder-only inputs (type=email/password). Submit
    # button text is "Sign in" while activeTab === 'login' (the default
    # for /auth/login per app/auth/login/page.tsx).
    await page.fill('input[type="email"]', email)
    await page.fill('input[type="password"]', password)
    # The page contains two "Sign in" buttons — the tab toggle and the form
    # submit. Scope to the form's submit button.
    await page.locator('form button[type="submit"]').click()
    # After login the root page server-redirects to /h/<uid> (or /onboarding
    # if incomplete). Wait for the final destination, not just any non-login
    # URL — otherwise we race the redirect and see "/".
    await page.wait_for_url(
        lambda u: "/h/" in u or "/onboarding" in u,
        timeout=20000,
    )
    logger.info(f"logged in; now at {page.url}")


async def navigate_to_call(page):
    """From the household home, click into the first reader, then the first book.

    Returns once we're on a `/call` URL.
    """
    if "/call" in page.url:
        return
    logger.info(f"household page url: {page.url}")
    # Wait for the household page to actually render — RSC streaming means
    # the page can be at the right URL while the body is still empty.
    import re

    cta_regex = re.compile(r"Continue reading|Pick a story|Pick another|No readers yet")
    cta_locator = page.get_by_text(cta_regex).first
    try:
        await cta_locator.wait_for(state="visible", timeout=15000)
    except Exception:
        body_text = await page.evaluate("() => document.body.innerText.slice(0, 500)")
        raise RuntimeError(f"household page did not render a CTA. body excerpt:\n{body_text!r}")
    text = (await cta_locator.inner_text()).strip()
    logger.info(f"household CTA found: {text!r}")
    if "No readers yet" in text:
        raise RuntimeError("test account has no readers — onboarding not done?")
    await cta_locator.click()

    # If we land on the reader page (book picker), pick the first book.
    try:
        await page.wait_for_url(lambda u: "/r/" in u and "/call" not in u, timeout=4000)
        logger.info(f"reader page: {page.url}")
        book = page.locator('a[href*="/call?bookId="]').first
        await book.wait_for(state="visible", timeout=10000)
        await book.click()
    except Exception:
        pass

    await page.wait_for_url(lambda u: "/call" in u, timeout=15000)
    logger.info(f"on call page: {page.url}")


async def wait_for_connected(page, timeout: float = 60.0):
    """Wait until ConnectButton reports "End reading" (= connectionState connected/ready).

    voice-ui-kit's ConnectButton renders its label as aria-label with no
    innerText, so we check both.
    """
    logger.info("waiting for call to connect…")
    end = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < end:
        try:
            label = await page.evaluate(
                """() => {
                  const btns = Array.from(document.querySelectorAll('button'));
                  for (const b of btns) {
                    const text = (b.innerText || '').trim();
                    const aria = b.getAttribute('aria-label') || '';
                    const s = text || aria;
                    if (/Start reading|End reading|Waking Ember|Ending/.test(s)) return s;
                  }
                  return null;
                }"""
            )
        except Exception:
            label = None
        if label != last:
            logger.info(f"connect button label: {label!r}")
            last = label
        if label and "End reading" in label:
            return label
        await asyncio.sleep(0.5)
    raise TimeoutError(f"call did not reach connected within {timeout}s (last: {last!r})")


async def click_red_cross(page):
    """End the call via the UI.

    Per call/page.tsx the X button has ``aria-label="End session"``. If that
    isn't visible (e.g. covered by the book-reader overlay), fall back to the
    ConnectButton ("End reading" aria-label) — both invoke handleDisconnect.
    """
    for name in ("End session", "End reading"):
        loc = page.get_by_role("button", name=name)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                logger.info(f"clicking '{name}' to end call")
                await loc.first.click()
                return
        except Exception:
            continue
    raise RuntimeError("no end-call button visible (End session / End reading)")


async def main():
    """Run the full e2e call against a localhost:3000 readme app."""
    from voicebox.agent_ipc import (
        send_command,
        start_pipecat_process,
        stop_pipecat_process,
    )
    from voicebox.browser_session import start_browser, stop_browser
    from voicebox.runner_args import BrowserShimRunnerArguments

    audio_port = 9091
    cdp_port = 9222
    audio_ws_url = f"ws://localhost:{audio_port}"

    artifacts_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "artifacts", "e2e_readme_call")
    )
    os.makedirs(artifacts_dir, exist_ok=True)
    logger.info(f"artifacts dir: {artifacts_dir}")

    logger.info("=== starting pipecat (browser-shim mode) ===")
    start_pipecat_process(
        BrowserShimRunnerArguments(
            host="localhost",
            port=audio_port,
            record_dir=artifacts_dir,
        )
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

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(info["cdp_endpoint"])
            ctx = browser.contexts[0]
            page = ctx.pages[0]
            page.on("console", lambda m: logger.debug(f"console[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: logger.warning(f"pageerror {e}"))

            async def shot(name: str):
                path = os.path.join(artifacts_dir, f"{name}.png")
                try:
                    await page.screenshot(path=path, full_page=False)
                    logger.info(f"📸 {path}")
                except Exception as e:
                    logger.warning(f"screenshot failed ({name}): {e}")

            try:
                # Avoid networkidle — Next.js dev keeps a HMR WebSocket open and
                # it never settles. domcontentloaded is enough here.
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await shot("01_login_page")
                await login(page, EMAIL, PASSWORD)
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await shot("02_household_page")
                await navigate_to_call(page)
                await shot("03_call_page_loaded")
                await wait_for_connected(page, timeout=90.0)
                await shot("04_call_connected")

                async def dump_shim():
                    state = await page.evaluate(
                        "() => ({"
                        "inbound: window.__voiceShim?.inboundChunks,"
                        "outbound: window.__voiceShim?.outboundChunks,"
                        "pcCount: window.__voiceShim?.pcCount,"
                        "audioTrackCount: window.__voiceShim?.audioTrackCount,"
                        "outboundSampleRate: window.__voiceShim?.outboundSampleRate,"
                        "outboundNumChannels: window.__voiceShim?.outboundNumChannels,"
                        "outboundFormat: window.__voiceShim?.outboundFormat,"
                        "perTrackBytes: window.__voiceShim?.perTrackBytes,"
                        "errors: (window.__voiceShim?.errors || []).slice(-5)"
                        "})"
                    )
                    logger.info(f"shim counters: {state}")

                await dump_shim()

                # Ember likely greets first — listen briefly to catch any opening line.
                logger.info("=== listen (initial greeting) ===")
                r0 = await send_command("listen", timeout=20.0)
                logger.info(f"ember (greeting): {r0.get('text', '')!r}")

                await dump_shim()

                # First turn: ask
                logger.info("=== speak: ask ===")
                await send_command(
                    "speak", text="Hi Ember! Can you tell me about this book in one short sentence?"
                )
                await asyncio.sleep(1)

                logger.info("=== listen (turn 1) ===")
                r1 = await send_command("listen", timeout=45.0)
                logger.info(f"ember (turn 1): {r1.get('text', '')!r}")
                await dump_shim()

                # Second turn
                logger.info("=== speak: follow-up ===")
                await send_command("speak", text="Who is the main character?")
                await asyncio.sleep(1)
                r2 = await send_command("listen", timeout=30.0)
                logger.info(f"ember (turn 2): {r2.get('text', '')!r}")
                await dump_shim()

                # End call by clicking red cross (or End reading button as fallback).
                await shot("05_before_end_call")
                await click_red_cross(page)
                await asyncio.sleep(3)
                await shot("06_after_end_call")
                logger.info(f"after end-call, page url: {page.url}")

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    finally:
        logger.info("=== teardown ===")
        # Send the "stop" command FIRST so agent.stop() runs and flushes the
        # audio recordings to disk. stop_pipecat_process() just kills the
        # child, which would lose the buffered WAVs.
        try:
            await asyncio.wait_for(send_command("stop"), timeout=20.0)
        except Exception as e:
            logger.warning(f"send stop command failed: {e}")
        try:
            await asyncio.to_thread(stop_browser)
        except Exception as e:
            logger.warning(f"stop_browser failed: {e}")
        try:
            stop_pipecat_process()
        except Exception as e:
            logger.warning(f"stop_pipecat_process failed: {e}")
        logger.info("done.")


if __name__ == "__main__":
    asyncio.run(main())
