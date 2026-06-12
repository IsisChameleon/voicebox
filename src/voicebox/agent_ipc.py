#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Inter-process communication for the Pipecat MCP server.

This module manages the IPC queues and child process lifecycle for communication
between the MCP server (parent) and the Pipecat voice agent (child). The child
process runs separately to avoid stdio collisions with the MCP protocol.
"""

import asyncio
import multiprocessing
import queue as queue_module
import uuid
from typing import Optional

from loguru import logger

# Use spawn to avoid issues with forking from async context Fork copies the
# parent's state (event loop, file descriptors, locks) which can cause
# issues. Spawn creates a fresh Python interpreter.
multiprocessing.set_start_method("spawn", force=True)

_cmd_queue: Optional[multiprocessing.Queue] = None
_response_queue: Optional[multiprocessing.Queue] = None
_pipecat_process: Optional[multiprocessing.Process] = None

# Parent-side full-duplex state: requests carry a correlation id and a single
# router task resolves each response into the matching future, so several
# commands (e.g. a speak during a pending listen) can be in flight at once.
_pending: dict[str, "asyncio.Future[dict]"] = {}
_router_task: Optional[asyncio.Task] = None


def _cleanup():
    """Clean up the pipecat child process."""
    global _pipecat_process, _cmd_queue, _response_queue

    logger.debug(f"Checking if Pipecat MCP Agent process is actually running...")
    if _pipecat_process:
        # Force terminate if still alive
        if _pipecat_process.is_alive():
            logger.debug(f"Terminating Pipecat MCP Agent process (PID {_pipecat_process.ident})")
            _pipecat_process.terminate()
            _pipecat_process.join(timeout=5.0)

        # Kill if terminate didn't work
        if _pipecat_process.is_alive():
            logger.debug(f"Killing Pipecat MCP Agent process (PID {_pipecat_process.ident})")
            _pipecat_process.kill()
            _pipecat_process.join(timeout=5.0)

        _pipecat_process = None

    # Close the queues so their internal semaphores are released
    if _cmd_queue is not None:
        _cmd_queue.close()
        _cmd_queue.join_thread()
        _cmd_queue = None

    if _response_queue is not None:
        _response_queue.close()
        _response_queue.join_thread()
        _response_queue = None


def start_pipecat_process(runner_args):
    """Start the Pipecat child process for the given runner configuration.

    Creates IPC queues and spawns a new process to run the Pipecat voice agent.
    Cleans up any existing process before starting a new one.

    Args:
        runner_args: A ``BrowserShimRunnerArguments`` instance (WebSocket
            server for the in-browser shim).

    """
    global _cmd_queue, _response_queue, _pipecat_process

    # Clean up any existing process first
    _cleanup()

    # Create IPC queues using spawn context
    _cmd_queue = multiprocessing.Queue()
    _response_queue = multiprocessing.Queue()

    # Start pipecat as separate process
    logger.debug(f"Starting Pipecat MCP Agent process...")
    _pipecat_process = multiprocessing.Process(
        target=run_pipecat_process,
        args=(_cmd_queue, _response_queue, runner_args),
    )
    _pipecat_process.start()
    logger.debug(f"Started Pipecat MCP Agent process (PID {_pipecat_process.ident})")


def stop_pipecat_process():
    """Stop the pipecat child process (explicit cleanup)."""
    logger.debug(f"Stopping Pipecat MCP Agent process...")
    _cleanup()
    logger.debug(f"Stopped Pipecat MCP Agent")


def run_pipecat_process(
    cmd_queue: multiprocessing.Queue,
    response_queue: multiprocessing.Queue,
    runner_args,
):
    """Entry point for the Pipecat child process.

    Runs the bot command loop bound to whatever transport ``runner_args``
    selects. This function is called in a separate process to avoid stdio
    collisions with the MCP protocol.

    Args:
        cmd_queue: Queue for receiving commands from the MCP server.
        response_queue: Queue for sending responses back to the MCP server.
        runner_args: A ``BrowserShimRunnerArguments`` instance —
            dispatched by ``create_agent``.

    """
    global _cmd_queue, _response_queue

    import asyncio

    from voicebox.bot import bot as bot_fn

    _cmd_queue = cmd_queue
    _response_queue = response_queue

    logger.debug(f"Pipecat MCP Agent starting with {type(runner_args).__name__}")
    asyncio.run(bot_fn(runner_args))

    logger.debug("Pipecat runner is done...")


async def send_response(response: dict):
    """Send a response from the child process to the MCP server.

    Args:
        response: Response dictionary to send.

    Raises:
        RuntimeError: If the Pipecat process has not been started.

    """
    if _response_queue is None:
        raise RuntimeError("Pipecat process not started")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _response_queue.put, response)


async def read_request() -> dict:
    """Read a request from the MCP server in the child process.

    Blocks until a command is available in the queue.

    Returns:
        Request dictionary containing the command and arguments.

    Raises:
        RuntimeError: If the Pipecat process has not been started.

    """
    if _cmd_queue is None:
        raise RuntimeError("Pipecat process not started")
    loop = asyncio.get_event_loop()
    request = await loop.run_in_executor(None, _cmd_queue.get)
    return request


def _get_with_timeout(queue: multiprocessing.Queue, timeout: float = 0.5):
    """Get from queue with timeout to allow cancellation.

    Args:
        queue: The queue to read from.
        timeout: Timeout in seconds.

    Returns:
        Item from the queue.

    Raises:
        TimeoutError: If the timeout expires before an item is available.

    """
    try:
        return queue.get(timeout=timeout)
    except queue_module.Empty:
        raise TimeoutError("Queue get timed out")


def _check_process_alive():
    """Check if the pipecat process is still alive."""
    if _pipecat_process and not _pipecat_process.is_alive():
        raise RuntimeError("Voice agent process has stopped")


def _fail_pending(exc: Exception):
    """Fail every pending command future with ``exc`` (router-loop only)."""
    pending = dict(_pending)
    _pending.clear()
    for future in pending.values():
        if not future.done():
            future.set_exception(exc)


async def _response_router(response_queue: multiprocessing.Queue):
    """Read responses off ``response_queue`` and resolve the matching futures.

    One instance runs per child session. It exits (failing any pending
    futures) when the session's queue is replaced by a new session, the
    child process dies, or the queue is closed by cleanup.
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            response = await loop.run_in_executor(None, _get_with_timeout, response_queue, 0.5)
        except TimeoutError:
            if _response_queue is not response_queue:
                _fail_pending(RuntimeError("Voice agent session was replaced"))
                return
            try:
                _check_process_alive()
            except RuntimeError as e:
                _fail_pending(e)
                return
            continue
        except Exception as e:
            # Queue closed under us (cleanup) or otherwise unusable.
            _fail_pending(RuntimeError(f"Voice agent response queue closed: {e}"))
            return

        request_id = response.pop("id", None)
        future = _pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(response)
        else:
            # Late response for a deadline-expired or cancelled command —
            # harmless by design, nobody is awaiting it anymore.
            logger.debug(f"Dropping unmatched response (id={request_id}): {response}")


def _ensure_router():
    """Start (or restart) the response-router task for the current session."""
    global _router_task
    response_queue = _response_queue
    if response_queue is None:
        raise RuntimeError("Pipecat process not started")
    if _router_task is None or _router_task.done():
        _router_task = asyncio.get_event_loop().create_task(_response_router(response_queue))


async def send_command(cmd: str, deadline: Optional[float] = None, **kwargs) -> dict:
    """Send a command to the Pipecat child process and wait for its response.

    Requests carry a correlation id, so any number of commands can be in
    flight concurrently — a ``speak`` issued while a ``listen`` is pending
    executes immediately, and out-of-order responses resolve the right
    callers.

    Args:
        cmd: Command name (e.g., "listen", "speak", "stop").
        deadline: Overall max seconds to wait for the child's response, so a
            hung child can't block the calling MCP tool forever. ``None``
            waits indefinitely.
        **kwargs: Additional arguments for the command.

    Returns:
        Response dictionary from the child process (without the ``id`` key).

    Raises:
        RuntimeError: If the child reports a failure (``error`` key in the
            response). Surfaces to the MCP client as a tool error instead of
            leaking into the result payload.
        TimeoutError: If ``deadline`` elapses without a response.

    """
    if _cmd_queue is None or _response_queue is None:
        raise RuntimeError("Pipecat process not started")

    _ensure_router()

    request_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    future: "asyncio.Future[dict]" = loop.create_future()
    _pending[request_id] = future

    request = {"id": request_id, "cmd": cmd, **kwargs}
    try:
        await loop.run_in_executor(None, _cmd_queue.put, request)
        response = await asyncio.wait_for(future, timeout=deadline)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"No response from the voice agent within {deadline}s for '{cmd}'"
        ) from None
    except asyncio.CancelledError:
        logger.info(f"Command '{cmd}' was cancelled")
        raise
    finally:
        _pending.pop(request_id, None)

    if "error" in response:
        error_message = response["error"]
        logger.error(f"Error running command '{cmd}': {error_message}")
        raise RuntimeError(f"voicebox command '{cmd}' failed: {error_message}")

    return response
