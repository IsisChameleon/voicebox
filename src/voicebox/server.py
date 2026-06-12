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
import socket
import sys

from loguru import logger
from mcp.server.fastmcp import FastMCP

from voicebox.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process
from voicebox.browser_session import start_browser, stop_browser
from voicebox.runner_args import BrowserShimRunnerArguments

logger.remove()
logger.add(sys.stderr, level="DEBUG")

# Create MCP server. Stateless + json_response per the MCP 2025-11-25 recommended
# config for streamable-http servers — no session bookkeeping, no SSE.
mcp = FastMCP(
    name="voicebox",
    host="localhost",
    port=9090,
    stateless_http=True,
    json_response=True,
)


def _assert_port_free(port: int, name: str):
    """Raise a clear error if ``port`` is already bound on localhost.

    Catches the common "another voicebox session is already running" case. The
    raised message is surfaced to the calling LLM as the tool's error result
    (FastMCP wraps it into an ``isError`` ``CallToolResult``), so it names the
    exact ``start_browser_session`` argument to retry with — not a human-facing
    config knob.

    Args:
        port: TCP port to probe on localhost.
        name: The ``start_browser_session`` parameter this port came from —
            ``"audio_port"`` or ``"cdp_port"``. Used verbatim in the error
            message so the LLM knows which argument to change on retry.

    Raises:
        RuntimeError: If ``port`` is already bound (e.g. a prior session is
            still listening on it).

    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("localhost", port))
    except OSError as e:
        raise RuntimeError(
            f"{name} port {port} is already in use — another voicebox session "
            f"may be running. Pass a different {name} to start_browser_session."
        ) from e
    finally:
        sock.close()


@mcp.tool()
async def start_browser_session(
    url: str = "http://localhost:3000",
    headless: bool = False,
    cdp_port: int = 9222,
    audio_port: int = 9091,
    user_data_dir: str | None = None,
) -> dict:
    """Launch a Playwright-controlled Chromium with the browser audio shim injected.

    The shim hijacks the browser's microphone (fed by Kokoro TTS from the MCP
    server) and tees the bot's remote WebRTC audio back to Whisper, so an
    MCP-driven Claude can play the role of the user in any browser-based
    voice app — without the app being aware of the indirection.

    The returned ``attach_hint`` is the exact shell command to paste to wire
    up ``playwright-cli``. Two env vars are required together — omitting either
    silently breaks attach:

    - ``PLAYWRIGHT_MCP_CDP_ENDPOINT`` — points the client at voicebox's
      Chromium instead of launching its own.
    - ``PLAYWRIGHT_MCP_ISOLATED=false`` — without this, playwright-cli defaults
      ``isolated=true`` and calls ``browser.newContext()`` even over CDP,
      giving a fresh unauthenticated context instead of the existing voicebox
      tab. Verified in playwright-core ``index.js`` and ``config.js``.

    The ``close-all`` step in ``attach_hint`` is required when a daemon already
    exists for the session name — reusing an existing daemon ignores env vars.

    Do not open new tabs once attached; the audio shim lives only in the
    original tab and a second tab connecting to the audio server causes a
    reconnect storm.

    To skip logging in every run, pass ``user_data_dir`` (a persistent Chrome
    profile): log in once and the profile keeps you authenticated on every later
    run with the same dir — no save step.

    Args:
        url: Initial URL to open (e.g. the app's home page).
        headless: Run Chromium headless. Default false so you can watch. The
            audio path works headless too.
        cdp_port: Chromium remote-debugging port.
        audio_port: Local port the WebSocket audio transport listens on.
        user_data_dir: Persistent Chrome profile dir to reuse an authenticated
            session across runs.

    Returns:
        ``{cdp_endpoint, audio_ws_url, playwright_mcp_env, attach_hint}``.

    """
    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")
    audio_ws_url = f"ws://localhost:{audio_port}"
    start_pipecat_process(BrowserShimRunnerArguments(host="localhost", port=audio_port))
    try:
        info = await asyncio.to_thread(
            start_browser,
            url=url,
            audio_ws_url=audio_ws_url,
            cdp_port=cdp_port,
            headless=headless,
            user_data_dir=user_data_dir,
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

    Returns:
        The transcribed utterance, "" on timeout, or the literal marker
        "[voicebox event] audio client disconnected" if the in-page audio
        connection dropped (this is a status event, NOT bot speech).

    """
    # Parent-side deadline: child enforces `timeout` on the utterance wait,
    # the margin covers transcription of a long final utterance.
    result = await send_command("listen", timeout=timeout, deadline=timeout + 30.0)
    if result.get("event") == "client_disconnected":
        return "[voicebox event] audio client disconnected"
    return result.get("text", "")


@mcp.tool()
async def speak(text: str) -> bool:
    """Speak the given text to the user using text-to-speech.

    Returns true if the agent spoke the text, false otherwise.
    """
    await send_command("speak", text=text, deadline=60.0)
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
        await send_command("stop", deadline=30.0)
    except Exception as e:
        # A hung/dead child still gets reaped below — that's a stop too.
        logger.warning(f"graceful stop failed ({e}); forcing child shutdown")
    finally:
        # Reap the child process and release the IPC queues, then tear down
        # the browser — best-effort, never block one on the other.
        await asyncio.to_thread(stop_pipecat_process)
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
