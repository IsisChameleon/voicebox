import asyncio
import contextlib
import multiprocessing

import pytest

import voicebox.agent_ipc as ipc


def _reset_ipc():
    """Reset the module-global mailbox so each test starts from a clean slate."""
    ipc._cmd_queue = None
    ipc._response_queue = None
    ipc._pipecat_process = None
    ipc._router_task = None
    ipc._ready_future = None
    ipc._pending.clear()


async def _teardown():
    """Cancel the router task and close the queues opened by a test."""
    if ipc._router_task is not None:
        ipc._router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ipc._router_task
    for q in (ipc._cmd_queue, ipc._response_queue):
        if q is not None:
            q.close()
            q.join_thread()
    _reset_ipc()


def _arm_with_queues():
    """Install fresh queues (no real child) and arm the readiness handshake.

    ``_pipecat_process`` stays None, so the router's liveness check is a no-op —
    the router simply waits for whatever the fake child posts on the response
    queue, exactly as it would for a real child.
    """
    _reset_ipc()
    ipc._cmd_queue = multiprocessing.Queue()
    ipc._response_queue = multiprocessing.Queue()
    ipc._arm_ready()


def test_ready_success():
    # The fake child posts the readiness message; wait_for_pipecat_ready returns
    # without raising and the reserved future is cleaned up.
    async def run():
        _arm_with_queues()
        try:
            ipc._response_queue.put({"id": ipc._READY_ID, "ready": True})
            await ipc.wait_for_pipecat_ready(timeout=5.0)
            assert ipc._ready_future is None
            assert ipc._READY_ID not in ipc._pending
        finally:
            await _teardown()

    asyncio.run(run())


def test_ready_startup_error_surfaces_child_text():
    # A startup failure is reported on the readiness id; the child's exception
    # text must appear verbatim in the RuntimeError the parent raises.
    async def run():
        _arm_with_queues()
        try:
            ipc._response_queue.put(
                {"id": ipc._READY_ID, "error": "Kokoro model file not found: /bad/path"}
            )
            with pytest.raises(RuntimeError, match="Kokoro model file not found: /bad/path"):
                await ipc.wait_for_pipecat_ready(timeout=5.0)
            assert ipc._ready_future is None
        finally:
            await _teardown()

    asyncio.run(run())


def test_ready_timeout_names_model_cache():
    # No readiness message ever arrives: the wait times out with a message that
    # names the first-run model-download cause and the Kokoro cache dir.
    async def run():
        _arm_with_queues()
        try:
            with pytest.raises(TimeoutError, match="kokoro-onnx"):
                await ipc.wait_for_pipecat_ready(timeout=0.3)
            assert ipc._ready_future is None
        finally:
            await _teardown()

    asyncio.run(run())


def test_wait_without_start_raises():
    # Calling the wait with nothing armed is a clear programming error.
    async def run():
        _reset_ipc()
        try:
            with pytest.raises(RuntimeError, match="not started"):
                await ipc.wait_for_pipecat_ready(timeout=1.0)
        finally:
            await _teardown()

    asyncio.run(run())
