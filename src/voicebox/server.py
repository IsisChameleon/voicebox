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

from voicebox.agent_ipc import (
    send_command,
    start_pipecat_process,
    stop_pipecat_process,
    wait_for_pipecat_ready,
)
from voicebox.browser_session import start_browser, stop_browser
from voicebox.runner_args import BrowserShimRunnerArguments

logger.remove()
logger.add(sys.stderr, level="DEBUG")

# Generous ceiling for the audio child's readiness handshake: a first run has to
# download Kokoro (~300 MB) and Whisper (~1.5 GB) before the child can serve
# commands. Warm-cache starts return in well under a second.
AUDIO_READY_TIMEOUT = 300.0

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
    record_dir: str | None = None,
) -> dict:
    """Launch a Playwright-controlled Chromium with the browser audio shim injected.

    The shim hijacks the browser's microphone (fed by Kokoro TTS from the MCP
    server) and tees the bot's remote WebRTC audio back to Whisper, so an
    MCP-driven Claude can play the role of the user in any browser-based
    voice app — without the app being aware of the indirection.

    Returns only once BOTH children are ready: the browser page has loaded and
    the pipecat audio child has completed its readiness handshake (transport
    bound and STT/TTS constructed, which on a first run includes downloading the
    Kokoro and Whisper models — watch the server's stderr for progress). A
    startup failure in the audio child surfaces here with the child's exception
    text; a readiness timeout names the likely first-run model download. On any
    failure both children are torn down, so no orphan process or bound port is
    left behind.

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
        record_dir: If set, ``stop()`` writes a reviewable test report into this
            directory and returns the paths:
              * ``kokoro_voice.wav`` — mono, the tester's (our) voice.
              * ``ember_voice.wav`` — mono, the app bot's voice.
              * ``merged.wav`` — stereo, tester on the left, app bot on the right.
              * ``events.json`` — the full conversation event log (same objects
                ``listen()`` returns).
              * ``metrics.json`` — the computed report (per-turn response
                latency, talk-over windows, dead-air gaps, talk ratio,
                transcripts; see ``stop()`` for the schema).

    Returns:
        ``{cdp_endpoint, audio_ws_url, playwright_mcp_env, attach_hint}``.

    """
    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")
    audio_ws_url = f"ws://localhost:{audio_port}"

    # Spawn the audio child first: it returns immediately and the child loads
    # its models in the background while the browser launches, so the two
    # children come up concurrently. Readiness is armed synchronously inside
    # start_pipecat_process, so the handshake can't be missed.
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port, record_dir=record_dir)
    )

    # Browser first (fast, ~seconds): on failure, reap the audio child.
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

    # Then block on the audio child's readiness handshake (may already be
    # resolved by now on a warm cache). On startup error / timeout, tear down
    # both children so no orphan is left and the ports are released.
    logger.info("Waiting for the voice agent to become ready (first run downloads models)...")
    try:
        await wait_for_pipecat_ready(AUDIO_READY_TIMEOUT)
    except Exception:
        stop_pipecat_process()
        try:
            await asyncio.to_thread(stop_browser)
        except Exception as e:
            logger.warning(f"stop_browser during readiness-failure cleanup failed: {e}")
        raise
    logger.info("Voice agent ready.")
    return info


@mcp.tool()
async def listen(timeout: float = 30.0, cursor: int = 0) -> dict:
    """Stream timestamped conversation events from the voice session.

    Blocks until at least one event exists past ``cursor`` (or ``timeout``
    elapses), then returns every event from ``cursor`` onward plus the next
    cursor. Pass the returned ``cursor`` to the next call to resume without
    missing or re-reading anything; ``cursor=0`` replays the whole session.

    Two parties: ``app_bot`` is the app's voice agent under test; ``tester``
    is our synthetic human (Kokoro TTS). Event types (each has ``"t"``,
    wall-clock seconds):
      * ``session_started`` — log header; carries ``vad_stop_secs``.
      * ``session_stopped`` — the session is tearing down (``stop()`` was
        called); a pending ``listen()`` returns this instead of being cancelled.
      * ``client_connected`` / ``client_disconnected`` — the in-page audio
        link came up / dropped (a drop is a status event, NOT speech).
      * ``app_bot_speech_started`` / ``app_bot_speech_stopped`` — the app
        bot's voice activity. NOTE: ``app_bot_speech_stopped.t`` lands about
        ``vad_stop_secs`` (~1 s) after it truly stopped talking.
      * ``app_bot_transcript`` — a finished app-bot utterance: ``text`` plus
        ``turn_started_at`` (ISO timestamp of the turn start). Arrives after
        the corresponding ``app_bot_speech_stopped`` (batch STT).
      * ``tester_speech_started`` / ``tester_speech_stopped`` /
        ``tester_speech_interrupted`` — OUR synthetic voice starting /
        finishing / being cut off at playout.
      * ``tester_transcript`` — the exact text WE spoke (``text``); the
        ground-truth ``speak()`` input, emitted at speak time (not via STT).

    To simply wait for the next thing the app bot says: call in a loop with
    the advancing cursor and act on ``app_bot_transcript`` events.

    Args:
        timeout: Max seconds to wait for a new event past ``cursor``.
        cursor: Event-log position from the previous call (0 = from start).

    Returns:
        ``{"events": [...], "cursor": <next cursor>}`` — ``events`` is empty
        if the timeout elapsed with nothing new.

    """
    # Parent-side deadline: the child enforces `timeout` on the event wait,
    # the margin covers IPC latency and a long transcription flush.
    return await send_command("listen", timeout=timeout, cursor=cursor, deadline=timeout + 30.0)


@mcp.tool()
async def speak(
    text: str,
    wait_for_playout: bool = False,
    wait_for_turn: bool = False,
    when: str | None = None,
    timer_secs: float = 0.0,
) -> dict:
    """Speak the given text into the page's microphone via text-to-speech.

    Barge-in testing means TIMING our speech relative to the app bot's and then
    observing the bot's reaction via ``listen()`` — we never interrupt the bot
    directly (it is reached only through its microphone).

    ``wait_for_turn`` and ``when`` control WHEN we start speaking;
    ``wait_for_playout`` controls WHEN this call returns. They are independent.

    Args:
        text: The text to speak.
        wait_for_playout: When false (default), returns as soon as the speech is
            queued (``{"queued": True}``). When true, returns only after OUR OWN
            audio has finished playing out, with ``started_at`` / ``finished_at``
            wall-clock seconds and an ``interrupted`` flag — useful for
            timing-sensitive scripts. This waits for our Kokoro audio to finish;
            it says nothing about the app bot. Ignored when ``when`` is set (the
            call returns immediately).
        wait_for_turn: When true, wait until the app bot is not currently
            speaking, then speak (the polite path). Speaks immediately if it is
            already silent.
        when: An event type to arm a ONE-SHOT barge-in trigger on. When set,
            returns ``{"armed": True}`` immediately, then in the background
            waits for the NEXT occurrence of that event, sleeps ``timer_secs``,
            and speaks. Canonical use:
            ``when="app_bot_speech_started", timer_secs=1.5``.
        timer_secs: Seconds to wait after the ``when`` event fires before
            speaking. Only meaningful with ``when``.

    Returns:
        ``{"armed": True}`` when ``when`` is set; otherwise ``{"queued": True}``,
        plus the playout timing fields when ``wait_for_playout`` is true.

    """
    if when is not None:
        deadline = 60.0  # armed, returns immediately
    elif wait_for_playout or wait_for_turn:
        deadline = 150.0
    else:
        deadline = 60.0
    return await send_command(
        "speak",
        text=text,
        wait_for_playout=wait_for_playout,
        wait_for_turn=wait_for_turn,
        when=when,
        timer_secs=timer_secs,
        deadline=deadline,
    )


@mcp.tool()
async def stop() -> dict:
    """Stop the voice pipeline and clean up resources.

    Call this when the voice conversation is complete to gracefully shut
    down the voice agent. Also closes the Playwright-controlled browser
    if one is active (started via ``start_browser_session``).

    Returns:
        ``{"stopped": true}``. When the session ran with ``record_dir``, also
        ``"artifacts"`` with the absolute paths written for review:

          * ``events`` — ``events.json``, the full conversation event log.
          * ``metrics`` — ``metrics.json``, the computed test report. Top-level
            keys: ``session`` (span + ``biases`` to read the numbers correctly),
            ``turns`` (transcript per turn; app-bot turns carry
            ``response_latency_secs``), ``app_response_latencies_secs``,
            ``talk_over_windows``, ``dead_air_gaps``, ``talk_time``,
            ``utterances``, ``summary``.
          * ``merged_wav`` / ``tester_wav`` / ``app_bot_wav`` — recordings, if
            audio was captured.

    """
    artifacts = None
    try:
        response = await send_command("stop", deadline=30.0)
        artifacts = response.get("artifacts")
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
    result: dict = {"stopped": True}
    if artifacts:
        result["artifacts"] = artifacts
    return result


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
