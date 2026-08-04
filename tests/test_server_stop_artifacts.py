#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""stop() merges the browser child's shim artifacts into the report set (D22).

The shim artifacts are written by the BROWSER child and returned by
``stop_browser()``, while the rest of the report comes from the pipecat child
over IPC — nothing but these tests pins the parent-side merge, or that the
shim paths survive a pipecat child too wedged to answer ``stop``.
"""

import voicebox.server as server


async def test_stop_merges_shim_artifacts(monkeypatch):
    async def fake_send_command(cmd, deadline=None, **kwargs):
        return {"artifacts": {"events": "/run/events.json"}}

    monkeypatch.setattr(server, "send_command", fake_send_command)
    monkeypatch.setattr(server, "stop_pipecat_process", lambda: None)
    monkeypatch.setattr(server, "stop_browser", lambda: {"shim_log": "/run/shim.log"})

    result = await server.stop()

    assert result["stopped"] is True
    assert result["artifacts"] == {"events": "/run/events.json", "shim_log": "/run/shim.log"}


async def test_shim_artifacts_survive_failed_pipecat_stop(monkeypatch):
    async def failing_send_command(cmd, deadline=None, **kwargs):
        raise TimeoutError("no response from the voice agent")

    monkeypatch.setattr(server, "send_command", failing_send_command)
    monkeypatch.setattr(server, "stop_pipecat_process", lambda: None)
    monkeypatch.setattr(server, "stop_browser", lambda: {"shim_diag": "/run/shim_diag.json"})

    result = await server.stop()

    assert result["stopped"] is True
    assert result["artifacts"] == {"shim_diag": "/run/shim_diag.json"}


async def test_stop_without_any_artifacts_has_no_artifacts_key(monkeypatch):
    async def fake_send_command(cmd, deadline=None, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(server, "send_command", fake_send_command)
    monkeypatch.setattr(server, "stop_pipecat_process", lambda: None)
    monkeypatch.setattr(server, "stop_browser", lambda: None)

    result = await server.stop()

    assert result == {"stopped": True}
