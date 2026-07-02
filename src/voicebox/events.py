#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Conversation-event vocabulary for the voicebox event stream.

Single source of truth for the events ``listen()`` returns. Two parties, named
from the test's point of view (NOT pipecat's, which are inverted here):

  * ``app_bot`` — the app's voice agent under test (e.g. Ember). Its audio is
    our INPUT, so in the pipecat pipeline it is the "user".
  * ``tester``  — our synthetic human, voiced by Kokoro TTS. Our audio is the
    OUTPUT, so in the pipecat pipeline it is the "bot".

All events carry ``type`` and ``t`` (wall-clock seconds). ``t`` defaults to the
moment the event is constructed; only the ``app_bot_speech_*`` events override
it with pipecat's VAD emission timestamp (see ``agent.py`` for why that value
already trails the true acoustic boundary by the VAD's start/stop window).
"""

import time
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """The complete set of conversation-event names ``listen()`` can emit."""

    SESSION_STARTED = "session_started"
    SESSION_STOPPED = "session_stopped"
    CLIENT_CONNECTED = "client_connected"
    CLIENT_DISCONNECTED = "client_disconnected"
    APP_BOT_SPEECH_STARTED = "app_bot_speech_started"
    APP_BOT_SPEECH_STOPPED = "app_bot_speech_stopped"
    APP_BOT_TRANSCRIPT = "app_bot_transcript"
    TESTER_SPEECH_STARTED = "tester_speech_started"
    TESTER_SPEECH_STOPPED = "tester_speech_stopped"
    TESTER_SPEECH_INTERRUPTED = "tester_speech_interrupted"
    TESTER_TRANSCRIPT = "tester_transcript"
    TESTER_BARGE_IN_ARMED = "tester_barge_in_armed"
    TESTER_BARGE_IN_FIRED = "tester_barge_in_fired"
    TESTER_BARGE_IN_DROPPED = "tester_barge_in_dropped"


class VoiceboxEvent(BaseModel):
    """A single conversation event. Serialized to a plain dict over IPC.

    Used directly for the no-payload events (speech start/stop, connect/
    disconnect); the events carrying extra fields use the subclasses below.
    """

    # use_enum_values: store/emit the plain string ("session_started"), not the
    # EventType member, so the IPC payload and JSON artifacts stay stringly.
    # validate_default: the subclasses set ``type`` via a default, and without
    # this that default would NOT be coerced — leaking the enum member into
    # model_dump() for exactly those events.
    model_config = ConfigDict(use_enum_values=True, validate_default=True)

    type: EventType
    t: float = Field(default_factory=time.time)


class SessionStartedEvent(VoiceboxEvent):
    """Log header. ``vad_stop_secs`` lets consumers correct the stop lag."""

    type: EventType = EventType.SESSION_STARTED
    vad_stop_secs: float
    note: str


class TranscriptEvent(VoiceboxEvent):
    """A finished app-bot utterance.

    ``turn_started_at`` is pipecat's ISO timestamp for when the bot's turn
    began; the event's own ``t`` is when the batch transcript became ready.
    """

    type: EventType = EventType.APP_BOT_TRANSCRIPT
    text: str
    turn_started_at: str


class TesterTranscriptEvent(VoiceboxEvent):
    """The exact text the tester (us) spoke via ``speak()``.

    Unlike ``app_bot_transcript`` (recovered from audio via STT), this is the
    ground-truth input string — exact, and emitted at speak time rather than
    after batch STT.
    """

    type: EventType = EventType.TESTER_TRANSCRIPT
    text: str


class TesterBargeInArmedEvent(VoiceboxEvent):
    """A one-shot barge-in trigger was armed via ``speak(when=...)``.

    Emitted immediately when the trigger is registered, before the agent
    returns ``{"armed": True}``. The trigger waits for the next ``when`` event,
    sleeps ``timer_secs``, then speaks ``text``.
    """

    type: EventType = EventType.TESTER_BARGE_IN_ARMED
    when: str
    timer_secs: float
    text: str


class TesterBargeInFiredEvent(VoiceboxEvent):
    """An armed barge-in trigger fired and is about to speak.

    Emitted right before the tester speaks, after the ``timer_secs`` delay
    elapsed. ``triggered_by_t`` is the ``t`` of the ``when`` event that fired
    the trigger.
    """

    type: EventType = EventType.TESTER_BARGE_IN_FIRED
    when: str
    triggered_by_t: float


class TesterBargeInDroppedEvent(VoiceboxEvent):
    """An armed barge-in trigger reached its fire moment but could not speak.

    Emitted instead of ``tester_barge_in_fired`` when the trigger's ``when``
    event occurred and its ``timer_secs`` delay elapsed, but no browser client
    was connected to the audio WebSocket within the connection grace period. The
    utterance is dropped rather than queued into a dead transport, so no
    ``tester_transcript`` and no audio follow. ``triggered_by_t`` is the ``t`` of
    the ``when`` event that fired the trigger; ``reason`` names why it was
    dropped.
    """

    type: EventType = EventType.TESTER_BARGE_IN_DROPPED
    when: str
    triggered_by_t: float
    reason: str = "no_client_connected"
