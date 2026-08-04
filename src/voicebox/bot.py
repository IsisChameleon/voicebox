#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Bot entry point for the Pipecat MCP server.

Spawned in a child process by ``agent_ipc.start_pipecat_process()``; reads
commands from the parent over multiprocessing queues and drives the
``PipecatMCPAgent``.

The command loop is full-duplex: every request carries a correlation ``id``
(echoed back in the response), and each command runs as its own task — so a
``speak`` arriving while a ``listen`` is blocked executes immediately, and
responses may return out of request order.
"""

import asyncio

from loguru import logger
from pipecat.runner.types import RunnerArguments

from voicebox.agent import create_agent
from voicebox.agent_ipc import read_request, send_response


async def bot(runner_args: RunnerArguments):
    """Start the Pipecat agent and run the command loop.

    Supported commands (all requests/responses carry a correlation ``id``;
    the response shapes are ``PipecatMCPAgent``'s — see its docstrings):
        listen: Wait for conversation events past ``cursor``, respond with
            ``{"events": [...], "cursor": <next>, "transcription_lag_secs"}``
            (empty events on timeout).
        speak:  Speak the provided text; with ``wait_for_playout`` respond
            after playout with ``{"queued", "played", "started_at",
            "finished_at", "interrupted"}`` (or ``{"queued", "played": False,
            "reason"}`` on window expiry), otherwise respond
            ``{"queued": True}`` immediately. ``wait_for_turn`` (speak once
            the app bot is silent) adds ``waited_for_turn_secs``; a one-shot
            ``when``/``timer_secs`` barge-in trigger responds
            ``{"armed": True}``.
        stop:   Cancel in-flight commands, stop the agent and exit the loop,
            respond with ``{"ok": True, "artifacts": <paths or None>}``.

    Failures respond on the ``error`` key and the loop keeps serving commands.
    """
    agent = await create_agent(runner_args)
    await agent.start()

    logger.info("Voice agent started, processing commands...")

    in_flight: set[asyncio.Task] = set()

    async def run_command(request: dict):
        cmd = request.get("cmd")
        try:
            if cmd == "listen":
                response = await agent.listen_events(
                    timeout=request.get("timeout", 30.0),
                    cursor=request.get("cursor", 0),
                )
            elif cmd == "speak":
                response = await agent.speak(
                    request["text"],
                    wait_for_playout=request.get("wait_for_playout", False),
                    wait_for_turn=request.get("wait_for_turn", False),
                    when=request.get("when"),
                    timer_secs=request.get("timer_secs", 0.0),
                )
            else:
                response = {"error": f"Unknown command: {cmd}"}
        except asyncio.CancelledError:
            # Session is stopping — the parent no longer awaits this id.
            raise
        except Exception as e:
            # Report the failure on the error key (never as a transcript) and
            # keep serving commands — one bad command must not end the session.
            logger.warning(f"Error processing command '{cmd}': {e}")
            response = {"error": str(e)}
        await send_response({"id": request.get("id"), **response})
        logger.debug(f"Command '{cmd}' finished")

    while True:
        request = await read_request()
        cmd = request.get("cmd")
        logger.debug(f"Command '{cmd}' received, dispatching...")

        if cmd == "stop":
            # Stop the agent FIRST: it emits SESSION_STOPPED and wakes any
            # pending listen_events(), so those return cleanly instead of being
            # cancelled mid-wait. Then drain in-flight briefly (let the woken
            # listens send their responses) and cancel any stragglers (e.g. a
            # speak still awaiting playout) before acknowledging.
            try:
                artifacts = await agent.stop()
                if in_flight:
                    await asyncio.wait(in_flight, timeout=2.0)
                    for task in in_flight:
                        task.cancel()
                    await asyncio.gather(*in_flight, return_exceptions=True)
                await send_response({"id": request.get("id"), "ok": True, "artifacts": artifacts})
            except Exception as e:
                logger.warning(f"Error stopping the agent: {e}")
                await send_response({"id": request.get("id"), "error": str(e)})
            break

        task = asyncio.create_task(run_command(request))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)
