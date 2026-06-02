#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""qz-mcp-server: MCP server that drives a synthetic voice user against a Toocan bot.

Exposes voice tools via the MCP protocol so an LLM can start a Toocan call,
speak into the Daily room, and read transcribed bot replies back.

Tools:
    start_call: Create a Toocan call (Daily room + bot) and join it.
    speak:      TTS some text into the room.
    listen:     Block until the next bot utterance, return its transcript.
    stop:       Gracefully shut down the voice pipeline.

Screen-sharing tools (list_windows / screen_capture / capture_screenshot) are
inherited from upstream and not part of the Toocan test loop.
"""

import asyncio
import sys

import requests
from loguru import logger
from mcp.server.fastmcp import FastMCP

from pipecat_mcp_server.agent_ipc import send_command, start_pipecat_process, stop_pipecat_process

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
async def start_call(
    deployment_id: str,
    toocan_url: str = "http://localhost:8765",
    user_id: str = "claude-test-user",
) -> dict:
    """Start a test call against a Toocan bot.

    Calls the Toocan backend to create a Daily room, starts the bot,
    then joins that room as the synthetic test user. After this, use
    listen() and speak() to converse with the bot.

    Args:
        deployment_id: The Toocan deployment ID to test.
        toocan_url: Base URL of the Toocan backend.
        user_id: User ID to pass in Toocan-User-Id header.

    Returns:
        Dict with room_id, room_url, and joined status.

    """
    url = f"{toocan_url}/call/pipecat/start"
    # Toocan's /start enforces same-origin (or a workspace token). Setting
    # Origin to the local client domain satisfies the home-domain check.
    headers = {
        "Toocan-User-Id": user_id,
        "Content-Type": "application/json",
        "Origin": "http://localhost:5173",
    }
    body = {"deployment_id": deployment_id}

    resp = await asyncio.to_thread(requests.post, url, json=body, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to start call: {resp.status_code} {resp.text}")

    data = resp.json()
    room_url = data["dailyRoomUrl"]
    token = data["dailyToken"]
    room_id = data["roomId"]

    start_pipecat_process(room_url=room_url, token=token)

    return {"room_id": room_id, "room_url": room_url, "joined": True}


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
async def list_windows() -> list[dict]:
    """List all open windows visible to the screen capture backend.

    Returns a list of objects with title, app_name, and window_id fields.

    Note: Multiple windows may appear for the same app (e.g., tabs, child
    frames). When in doubt about which window the user wants, ask for
    clarification before capturing.
    """
    result = await send_command("list_windows")
    return result.get("windows", [])


@mcp.tool()
async def screen_capture(window_id: int | None = None) -> int | None:
    """Start or switch screen capture to a window or full screen.

    Captures are streamed through the Pipecat pipeline. Use list_windows()
    to find available window IDs.

    Args:
        window_id: Window ID to capture (from list_windows()). If not provided,
            captures the full screen.

    Returns the window ID if the window was found, or None if it was not found
    or capturing full screen.

    """
    result = await send_command("screen_capture", window_id=window_id)
    return result.get("window_id")


@mcp.tool()
async def capture_screenshot() -> str:
    """Take a look at what's on screen.

    Use this when the user asks what you can see. Screen capture must
    already be started via screen_capture().

    Returns the absolute path to the saved image file.
    """
    result = await send_command("capture_screenshot")
    return result.get("path", "No screen capture available.")


@mcp.tool()
async def stop() -> bool:
    """Stop the voice pipeline and clean up resources.

    Call this when the voice conversation is complete to gracefully
    shut down the voice agent.

    Returns true if the agent was stopped successfully, false otherwise.
    """
    await send_command("stop")
    return True


def main():
    """Start the Pipecat MCP server.

    Runs the MCP server using stdio for communication with the MCP client.
    When the server exits, any running Pipecat agent process is cleaned up.
    """
    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("Ctrl-C detected, exiting!")
    finally:
        stop_pipecat_process()


if __name__ == "__main__":
    main()
