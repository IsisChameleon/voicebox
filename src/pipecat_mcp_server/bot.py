#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Bot entry point for the Pipecat MCP server.

Spawned in a child process by ``agent_ipc.start_pipecat_process()``; reads
commands from the parent over multiprocessing queues and drives the
``PipecatMCPAgent``.
"""

import asyncio

from loguru import logger
from pipecat.runner.types import RunnerArguments

from pipecat_mcp_server.agent import create_agent
from pipecat_mcp_server.agent_ipc import read_request, send_response


async def bot(runner_args: RunnerArguments):
    """Start the Pipecat agent and run the command loop.

    Supported commands:
        listen: Wait for an utterance, respond with ``{"text": "..."}``.
        speak:  Speak the provided text, respond with ``{"ok": True}``.
        stop:   Stop the agent and exit the loop, respond with ``{"ok": True}``.
    """
    agent = await create_agent(runner_args)
    await agent.start()

    logger.info("Voice agent started, processing commands...")

    while True:
        request = await read_request()
        cmd = request.get("cmd")
        logger.debug(f"Command '{cmd}' received, processing...")

        try:
            if cmd == "listen":
                timeout = request.get("timeout", 30.0)
                try:
                    text = await asyncio.wait_for(agent.listen(), timeout=timeout)
                except asyncio.TimeoutError:
                    text = ""
                await send_response({"text": text})
            elif cmd == "speak":
                await agent.speak(request["text"])
                await send_response({"ok": True})
            elif cmd == "stop":
                await agent.stop()
                await send_response({"ok": True})
                break
            else:
                await send_response({"error": f"Unknown command: {cmd}"})
            logger.debug(f"Command '{cmd}' finished")
        except Exception as e:
            logger.warning(f"Error processing command '{cmd}': {e}")
            await send_response({"text": str(e)})
            break
