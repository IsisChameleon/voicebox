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

# IPC deadlines. These MIRROR agent-side timing constants as literals — never
# import them: the parent must not load pipecat (hot-reload contract). The
# mirrors are pinned against the agent's constants by
# tests/test_server_deadlines.py, so drift fails the suite instead of
# re-introducing the round-4 reap-mid-drain bug (D15).
SPEAK_DEADLINE_BASE_SECS = 60.0
# Heuristic bound on speak(wait_for_turn=True): the agent-side wait for the
# app bot to fall silent is unbounded (the app decides), so this caps how long
# the parent will hold the HTTP call open for it.
TURN_WAIT_DEADLINE_SECS = 150.0
# Mirrors agent.PLAYOUT_SECS_PER_WORD (0.8): the agent's playout window is
# 30 s + 0.8 s/word, and the base already carries a 30 s margin over it.
PLAYOUT_DEADLINE_SECS_PER_WORD = 0.8
# Must outlive the agent's STT drain (DRAIN_CAP_SECS = 180 in
# processors/nonblocking_whisper_stt.py) plus settle + artifact writing. At
# the old 30 s this timed out mid-drain and the child was reaped BEFORE it
# wrote events.json/metrics.json — verification round 4 lost its entire
# artifact set that way.
STOP_DEADLINE_SECS = 210.0


def _speak_deadline(
    text: str, wait_for_playout: bool, wait_for_turn: bool, when: str | None
) -> float:
    """Pick the IPC deadline for a ``speak`` command.

    The gates compose on the agent side — an armed trigger returns
    immediately; a turn wait blocks first; a playout wait then holds the call
    for a window that scales with text length — so the deadline must compose
    the same way (a flat per-gate value silently under-budgeted the combined
    ``wait_for_turn`` + ``wait_for_playout`` path).

    Args:
        text: The text to speak (the word count scales the playout window).
        wait_for_playout: Whether the call holds until playout is observed.
        wait_for_turn: Whether the call first waits for app-bot silence.
        when: Barge-in trigger event type, if arming (returns immediately).

    Returns:
        Deadline in seconds for ``send_command``.

    """
    if when is not None:
        return SPEAK_DEADLINE_BASE_SECS  # armed, returns immediately
    deadline = TURN_WAIT_DEADLINE_SECS if wait_for_turn else SPEAK_DEADLINE_BASE_SECS
    if wait_for_playout:
        deadline += PLAYOUT_DEADLINE_SECS_PER_WORD * len(text.split())
    return deadline


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

    The returned ``attach_hint`` is the exact shell command to paste to wire
    up ``playwright-cli``: ``playwright-cli attach --cdp <cdp_endpoint>``.
    ``attach`` takes a snapshot of the current page and navigates nowhere, so
    it leaves voicebox's shim tab intact — unlike ``playwright-cli open``,
    which runs ``goto about:blank`` and destroys the audio shim.

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
              * ``agent-debug.log`` — the session's DEBUG log (timing lines).
              * ``shim.log`` — the in-page audio shim's tagged console lines
                (install notes, WS drops, tap errors), timestamped as they
                happen; survives page navigations.
              * ``shim_diag.json`` — the final ``window.__voiceShim``
                diagnostics snapshot (hook flags, chunk counters, per-track
                bytes, errors).

    Returns:
        ``{cdp_endpoint, audio_ws_url, attach_hint}``. Raises if the page did
        not navigate or the shim did not install — a session is never reported
        as started for a blank tab.

    """
    _assert_port_free(audio_port, "audio_port")
    _assert_port_free(cdp_port, "cdp_port")
    audio_ws_url = f"ws://localhost:{audio_port}"
    start_pipecat_process(
        BrowserShimRunnerArguments(host="localhost", port=audio_port, record_dir=record_dir)
    )
    try:
        info = await asyncio.to_thread(
            start_browser,
            url=url,
            audio_ws_url=audio_ws_url,
            cdp_port=cdp_port,
            headless=headless,
            user_data_dir=user_data_dir,
            record_dir=record_dir,
        )
    except Exception:
        stop_pipecat_process()
        raise
    return info


@mcp.tool()
async def listen(timeout: float = 30.0, cursor: int = 0) -> dict:
    """Stream timestamped conversation events from the voice session.

    Blocks until at least one event exists past ``cursor`` (or ``timeout``
    elapses), then returns every event from ``cursor`` onward plus the next
    cursor. Pass the returned ``cursor`` to the next call to resume without
    missing or re-reading anything; ``cursor=0`` replays the whole session.

    Each batch is sorted by ``t``, so it reads as a conversation — but the
    cursor tracks arrival order, and a slow transcript arrives long after the
    speech events it belongs to. An event whose ``t`` predates a batch you
    already read can therefore still show up in a LATER batch; when
    reconstructing a timeline across batches, merge on ``t``, not on batch
    order.

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
        ``transcription_empty: true`` flags an utterance Whisper recovered no
        text for — the bot spoke, its words are unknown.
      * ``tester_speech_started`` / ``tester_speech_stopped`` /
        ``tester_speech_interrupted`` — OUR synthetic voice starting /
        finishing / being cut off at playout.
      * ``tester_transcript`` — the exact text WE spoke (``text``); the
        ground-truth ``speak()`` input. Its ``t`` is the ``speak()`` CALL
        time — not playout and not an STT result (playout start/end are the
        ``tester_speech_*`` events).
      * ``tester_barge_in_armed`` / ``tester_barge_in_fired`` — a
        ``speak(when=...)`` trigger was registered / just fired (``fired``
        carries ``triggered_by_t``, the ``t`` of the event that tripped it).

    To simply wait for the next thing the app bot says: call in a loop with
    the advancing cursor and act on ``app_bot_transcript`` events.

    Args:
        timeout: Max seconds to wait for a new event past ``cursor``. Keep it
            at or below 45: MCP clients commonly cap a tool call's HTTP
            request at ~60 s, and a longer ``listen`` hits that cap and
            surfaces as "The operation timed out." instead of the documented
            empty-``events`` return. Prefer polling in a cursor loop.
        cursor: Event-log position from the previous call (0 = from start).

    Returns:
        ``{"events": [...], "cursor": <next cursor>, "transcription_lag_secs"}``
        — ``events`` is empty if the timeout elapsed with nothing new.
        ``transcription_lag_secs`` is how long the oldest un-transcribed
        utterance has been waiting on Whisper: non-zero with no events means a
        transcript is still coming, so call again rather than concluding the
        app bot said nothing. The converse does NOT hold: it measures the STT
        queue only, and text already decoded but held by a still-open turn
        reads 0.0 — treat 0.0 as "no evidence", never as proof that nothing
        is pending.

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
            it says nothing about the app bot. Expect the call to block for
            roughly TWICE the utterance's audio duration (the whole text is
            synthesized before any audio plays); the observation window scales
            with text length, so long texts are fine. Ignored when ``when`` is
            set (the call returns immediately).
        wait_for_turn: When true, wait until the app bot is not currently
            speaking, then speak (the polite path). Speaks immediately if it is
            already silent. The result carries ``waited_for_turn_secs`` — how
            long the gate actually blocked (0.0 = already silent). Mutually
            exclusive with ``when`` (an armed trigger already times its own
            start): combining them is an error.
        when: An event type to arm a ONE-SHOT barge-in trigger on. When set,
            returns ``{"armed": True}`` immediately, then in the background
            waits for the NEXT occurrence of that event, sleeps ``timer_secs``,
            and speaks. Canonical use:
            ``when="app_bot_speech_started", timer_secs=1.5``. TIMING: arming
            is instantaneous server-side (~1 ms measured) — any delay you
            observe before the arm is your own turnaround, so arm EARLY, even
            while a previous utterance is still playing. The trigger fires on
            the next matching event AFTER the arm and there is no disarm short
            of ``stop()``. Expect audible speech ``timer_secs`` + TTS
            synthesis (~3–9 s measured) after the trigger event.
        timer_secs: Seconds to wait after the ``when`` event fires before
            speaking. Only meaningful with ``when``.

    Returns:
        ``{"armed": True}`` when ``when`` is set; otherwise ``{"queued": True}``,
        plus, when ``wait_for_playout`` is true, ``played`` and either the
        timing fields (``started_at`` / ``finished_at`` / ``interrupted``) or a
        ``reason`` explaining why the playout was never observed. A
        ``played: false`` result is a diagnosis, not an error: the speech was
        queued and may still be playing. ``wait_for_turn`` adds
        ``waited_for_turn_secs`` to whichever shape applies.

    """
    return await send_command(
        "speak",
        text=text,
        wait_for_playout=wait_for_playout,
        wait_for_turn=wait_for_turn,
        when=when,
        timer_secs=timer_secs,
        deadline=_speak_deadline(text, wait_for_playout, wait_for_turn, when),
    )


@mcp.tool()
async def stop() -> dict:
    """Stop the voice pipeline and clean up resources.

    Call this when the voice conversation is complete to gracefully shut
    down the voice agent. Also closes the Playwright-controlled browser
    if one is active (started via ``start_browser_session``).

    Teardown drains any transcriptions still in flight before writing the
    artifacts, so on a long session it can block for a minute or more — if
    your MCP client caps a tool call at ~60 s, this call may time out on
    your side while the teardown still completes: the artifact files land
    in ``record_dir`` regardless, so poll that directory instead of
    retrying ``stop()``.

    Drained transcripts are appended AFTER the ``session_stopped`` event, so
    a listen loop that exits on ``session_stopped`` never sees them — read
    the final utterances from the ``events.json`` artifact, which is written
    after the drain and contains them.

    Returns:
        ``{"stopped": true}``. When the session ran with ``record_dir``, also
        ``"artifacts"`` with the absolute paths written for review:

          * ``events`` — ``events.json``, the full conversation event log.
          * ``metrics`` — ``metrics.json``, the computed test report. Top-level
            keys: ``session`` (span + ``biases`` to read the numbers correctly),
            ``turns`` (transcript per turn; app-bot turns carry
            ``response_latency_secs``), ``app_response_latencies_secs``,
            ``talk_over_windows``, ``dead_air_gaps``,
            ``tester_think_time_gaps`` (silence owed by the DRIVING agent, not
            the app — read it alongside ``dead_air_gaps``), ``outage_gaps``
            (silence spanning a disconnect; quarantined from the
            conversational numbers), ``talk_time``, ``utterances``,
            ``summary``.
          * ``merged_wav`` / ``tester_wav`` / ``app_bot_wav`` — recordings, if
            audio was captured.
          * ``debug_log`` — ``agent-debug.log``, the per-session DEBUG sink
            (``voicebox.timing`` lines live here).
          * ``shim_log`` / ``shim_diag`` — the in-page shim's console log and
            final ``window.__voiceShim`` snapshot: how a broken audio tap is
            told apart from an app bot that never spoke.

    """
    artifacts = None
    shim_artifacts = None
    try:
        response = await send_command("stop", deadline=STOP_DEADLINE_SECS)
        artifacts = response.get("artifacts")
    except Exception as e:
        # A hung/dead child still gets reaped below — that's a stop too.
        logger.warning(f"graceful stop failed ({e}); forcing child shutdown")
    finally:
        # Reap the child process and release the IPC queues, then tear down
        # the browser — best-effort, never block one on the other. The shim
        # artifacts ride on the browser teardown, so they survive a wedged
        # pipecat child.
        await asyncio.to_thread(stop_pipecat_process)
        try:
            shim_artifacts = await asyncio.to_thread(stop_browser)
        except Exception as e:
            logger.warning(f"stop_browser failed: {e}")
    result: dict = {"stopped": True}
    if artifacts or shim_artifacts:
        result["artifacts"] = {**(artifacts or {}), **(shim_artifacts or {})}
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
