#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""MCP server that drives a synthetic voice user against any browser voice app.

Exposes voice tools via MCP so an LLM client can:
  * launch a Playwright-controlled Chromium with the audio shim injected
    (``start_browser_session``),
  * speak Kokoro TTS into the page's microphone (``speak``),
  * read Whisper transcripts of the bot's WebRTC audio (``listen``),
  * tear it all down (``stop``).
"""

import asyncio
import sys

from loguru import logger
from mcp.server.fastmcp import FastMCP

from pipecat_mcp_server.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process
from pipecat_mcp_server.browser_session import start_browser, stop_browser
from pipecat_mcp_server.runner_args import BrowserShimRunnerArguments

logger.remove()
logger.add(sys.stderr, level="DEBUG")

# Create MCP server. Stateless + json_response per the MCP 2025-11-25 recommended
# config for streamable-http servers — no session bookkeeping, no SSE.
mcp = FastMCP(
    name="qz-mcp-server",
    host="localhost",
    port=9090,
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def start_browser_session(
    url: str = "http://localhost:3000",
    headless: bool = False,
    cdp_port: int = 9222,
    audio_port: int = 9091,
) -> dict:
    """Launch a Playwright-controlled Chromium with the browser audio shim injected.

    The shim hijacks the browser's microphone (fed by Kokoro TTS from the MCP
    server) and tees the bot's remote WebRTC audio back to Whisper, so an
    MCP-driven Claude can play the role of the user in any browser-based
    voice app — without the app being aware of the indirection.

    The returned ``cdp_endpoint`` is the URL an external Playwright client
    (e.g. ``@playwright/mcp``, playwright-cli) should ``connect_over_cdp``
    against to drive the UI (login, navigate, click "Start reading", etc).

    Args:
        url: Initial URL to open (e.g. the app's home page).
        headless: Run Chromium headless. Default false so you can watch.
        cdp_port: Chromium remote-debugging port.
        audio_port: Local port the WebSocket audio transport listens on.

    Returns:
        ``{cdp_endpoint, audio_ws_url}``.

    """
    audio_ws_url = f"ws://localhost:{audio_port}"
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port)
    )
    try:
        info = await asyncio.to_thread(
            start_browser,
            url=url,
            audio_ws_url=audio_ws_url,
            cdp_port=cdp_port,
            headless=headless,
        )
    except Exception:
        stop_pipecat_process()
        raise
    return info


@mcp.tool()
async def listen(timeout: float = 30.0) -> str:
    """Listen for the next utterance and return the transcribed text.

    Blocks until the other party finishes an utterance (VAD-segmented).
    A long reply produces multiple utterances — call listen() in a loop
    to keep the conversation flowing.

    Args:
        timeout: Max seconds to wait. Returns "" on timeout.

    """
    result = await send_command("listen", timeout=timeout)
    return result.get("text", "")


@mcp.tool()
async def speak(text: str) -> bool:
    """Speak the given text to the user using text-to-speech.

    Returns true if the agent spoke the text, false otherwise.
    """
    await send_command("speak", text=text)
    return True


@mcp.tool()
async def stop() -> bool:
    """Stop the voice pipeline and clean up resources.

    Call this when the voice conversation is complete to gracefully shut
    down the voice agent. Also closes the Playwright-controlled browser
    if one is active (started via ``start_browser_session``).

    Returns true if the agent was stopped successfully, false otherwise.
    """
    try:
        await send_command("stop")
    finally:
        # Best-effort browser teardown — never block stopping pipecat on it.
        try:
            await asyncio.to_thread(stop_browser)
        except Exception as e:
            logger.warning(f"stop_browser failed: {e}")
    return True


def main():
    """Start the MCP server (streamable-http on localhost:9090).

    When the server exits, any running Pipecat agent and Chromium are
    cleaned up.
    """
    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("Ctrl-C detected, exiting!")
    finally:
        stop_pipecat_process()
        stop_browser()


if __name__ == "__main__":
    main()
