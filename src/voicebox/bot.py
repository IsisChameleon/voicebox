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

from voicebox.agent import CLIENT_DISCONNECTED, create_agent
from voicebox.agent_ipc import read_request, send_response


async def bot(runner_args: RunnerArguments):
    """Start the Pipecat agent and run the command loop.

    Supported commands (all requests/responses carry a correlation ``id``):
        listen: Wait for an utterance, respond with ``{"text": "..."}``; if the
            shim's WebSocket dropped instead, respond with
            ``{"text": "", "event": "client_disconnected"}``.
        speak:  Speak the provided text, respond with ``{"ok": True}``.
        stop:   Cancel in-flight commands, stop the agent and exit the loop,
            respond with ``{"ok": True}``.

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
                timeout = request.get("timeout", 30.0)
                try:
                    text = await asyncio.wait_for(agent.listen(), timeout=timeout)
                except asyncio.TimeoutError:
                    text = ""
                if text == CLIENT_DISCONNECTED:
                    response = {"text": "", "event": "client_disconnected"}
                else:
                    response = {"text": text}
            elif cmd == "speak":
                await agent.speak(request["text"])
                response = {"ok": True}
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
            # Cancel in-flight commands first (a pending listen would block
            # forever once the pipeline is gone), then stop and acknowledge.
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            try:
                await agent.stop()
                await send_response({"id": request.get("id"), "ok": True})
            except Exception as e:
                logger.warning(f"Error stopping the agent: {e}")
                await send_response({"id": request.get("id"), "error": str(e)})
            break

        task = asyncio.create_task(run_command(request))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)
