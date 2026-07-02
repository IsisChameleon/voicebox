#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Shared deadline/timeout constants for the parent and child processes.

The MCP parent (``server.py``) and the pipecat child (``agent.py``) both enforce
time limits on ``speak``, and they must stay coherent: the child has to give up
slightly *before* the parent's deadline, or the parent surfaces an error while
the child is still waiting and later produces "ghost" audio (see finding F4 in
``docs/design/2026-07-02-architecture-review-and-plan.md``).

This module is the single home for those numbers so the two sides can never
drift. It imports nothing from the package, so both processes can import it
without any circular-import risk. The child enforces the per-wait budgets
(``CONNECT_GRACE_SECS``, ``APP_BOT_SILENCE_TIMEOUT_SECS``, ``PLAYOUT_TIMEOUT_SECS``);
the parent derives its ``speak`` deadline from the same budgets via
``speak_parent_deadline`` — always the child's total budget plus ``IPC_MARGIN_SECS``.
"""

# How long the child waits for a browser client to (re)connect to the audio
# WebSocket before refusing a speak. Covers a page reload / brief WS drop
# without waiting so long that the caller thinks the tool hung.
CONNECT_GRACE_SECS = 10.0

# Max the child waits, in speak(wait_for_turn=True), for the app bot to fall
# silent before giving up. Without this the child would wait forever and speak
# long after the parent already reported the call failed (finding F4).
APP_BOT_SILENCE_TIMEOUT_SECS = 120.0

# Max the child waits, in speak(wait_for_playout=True), for our own Kokoro audio
# to finish playing out before giving up.
PLAYOUT_TIMEOUT_SECS = 120.0

# Kokoro's playout reaches the transport in segments separated by sub-second
# gaps, so pipecat emits a BotStarted/StoppedSpeakingFrame PAIR per segment.
# speak(wait_for_playout=True) treats the playout as finished only after the bot
# has stayed silent this long, so the reported span covers the whole utterance
# instead of clipping at the first segment.
PLAYOUT_SETTLE_SECS = 1.0

# The parent's deadline exceeds the child's total budget by this margin, which
# covers IPC latency and response serialization. It guarantees the child always
# resolves the command (with a result or an ``error``) before the parent's
# deadline fires — so the two sides always agree on whether an utterance
# happened.
IPC_MARGIN_SECS = 15.0

# Deadline for arming a one-shot ``when=`` trigger. Arming returns
# ``{"armed": True}`` immediately, so this only needs to cover the round trip;
# the trigger itself is exempt from parent deadlines by design.
ARM_ACK_DEADLINE_SECS = IPC_MARGIN_SECS


def speak_child_budget(wait_for_turn: bool, wait_for_playout: bool) -> float:
    """Return the child's worst-case total wait budget for a ``speak`` shape.

    The child enforces each sub-wait independently; this is the sum of those
    per-wait ceilings, i.e. the longest the child could take before it must
    resolve the command.

    Args:
        wait_for_turn: Whether the speak waits for the app bot to fall silent.
        wait_for_playout: Whether the speak waits for our audio to finish.

    Returns:
        The child's total wait budget in seconds.

    """
    budget = CONNECT_GRACE_SECS
    if wait_for_turn:
        budget += APP_BOT_SILENCE_TIMEOUT_SECS
    if wait_for_playout:
        budget += PLAYOUT_TIMEOUT_SECS + PLAYOUT_SETTLE_SECS
    return budget


def speak_parent_deadline(wait_for_turn: bool, wait_for_playout: bool) -> float:
    """Return the parent's ``speak`` deadline for a given command shape.

    Always the child's budget plus ``IPC_MARGIN_SECS``, so the child gives up
    strictly before the parent does.

    Args:
        wait_for_turn: Whether the speak waits for the app bot to fall silent.
        wait_for_playout: Whether the speak waits for our audio to finish.

    Returns:
        The parent-side deadline in seconds.

    """
    return speak_child_budget(wait_for_turn, wait_for_playout) + IPC_MARGIN_SECS
