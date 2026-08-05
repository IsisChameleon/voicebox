#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The parent's IPC deadlines must outlive the child work they wait for (D15).

server.py mirrors agent-side timing constants as literals (the parent must
never import pipecat), so nothing but these tests stops the two sides from
drifting apart — regressing either side used to leave the whole suite green
while re-introducing the round-4 reap-mid-drain artifact loss.
"""

import voicebox.agent as agent_module
import voicebox.server as server
from voicebox.processors.nonblocking_whisper_stt import DRAIN_CAP_SECS


def test_playout_deadline_outlives_agent_window_for_every_gate_combo():
    # The gates compose agent-side, so the deadline must compose too — the
    # old if/elif chain gave wait_for_turn + wait_for_playout a flat 150 s
    # that a ~115-word text alone could exceed.
    text = "word " * 150
    agent_window = agent_module.PLAYOUT_TIMEOUT_SECS + agent_module.PLAYOUT_SECS_PER_WORD * 150
    for wait_for_turn in (False, True):
        deadline = server._speak_deadline(
            text, wait_for_playout=True, wait_for_turn=wait_for_turn, when=None
        )
        assert deadline >= agent_window + 30.0


def test_per_word_mirror_matches_agent_constant():
    assert server.PLAYOUT_DEADLINE_SECS_PER_WORD == agent_module.PLAYOUT_SECS_PER_WORD


def test_stop_deadline_outlives_drain_cap():
    # Drain (<=180) + <=2 s event settle + bot's 2 s in-flight drain +
    # artifact writing must all fit before the parent reaps the child.
    assert server.STOP_DEADLINE_SECS >= DRAIN_CAP_SECS + 15.0


def test_armed_speak_deadline_is_flat():
    # An armed trigger returns immediately; its deadline must not scale with
    # the (possibly long) barge-in text.
    deadline = server._speak_deadline(
        "word " * 200, wait_for_playout=True, wait_for_turn=False, when="app_bot_speech_started"
    )
    assert deadline == server.SPEAK_DEADLINE_BASE_SECS
