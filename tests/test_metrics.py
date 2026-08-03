import json
from pathlib import Path

import pytest

# Imported as a module (not by name) so pytest doesn't try to collect the
# event classes whose names start with "Test" (e.g. TesterTranscriptEvent).
import voicebox.events as ev
from voicebox.metrics import compute_metrics

FIXTURES = Path(__file__).parent / "fixtures"

# A synthetic conversation with known boundaries. Times in seconds.
#
#   tester  T1: [2, 4]            "I'd like a refund"
#   app_bot A1: [5, 9]            "Sure, what's your order number?"   latency 5-4 = 1.0
#   tester  T2: [10, 11]          "Order 123"
#   app_bot A2: [15, 18]          "Thanks, processed."                latency 15-11 = 4.0
#
# Speech-interval union: [2,4] [5,9] [10,11] [15,18]
#   silent gaps between them: [4,5]=1.0, [9,10]=1.0, [11,15]=4.0
#   attributed by who owed the next turn:
#     [4,5]  follows tester T1 -> app dead air      1.0
#     [9,10] follows app    A1 -> tester think time 1.0
#     [11,15] follows tester T2 -> app dead air     4.0
#   no talk-over (no tester/app overlap)
VAD_STOP_SECS = 1.0
EVENTS = [
    {"type": "session_started", "t": 0.0, "vad_stop_secs": VAD_STOP_SECS, "note": "..."},
    {"type": "client_connected", "t": 1.0},
    {"type": "tester_speech_started", "t": 2.0},
    {"type": "tester_speech_stopped", "t": 4.0},
    {"type": "tester_transcript", "t": 4.0, "text": "I'd like a refund"},
    {"type": "app_bot_speech_started", "t": 5.0},
    {"type": "app_bot_speech_stopped", "t": 9.0},
    {
        "type": "app_bot_transcript",
        "t": 9.3,
        "text": "Sure, what's your order number?",
        "turn_started_at": "2026-06-15T00:00:05",
    },
    {"type": "tester_speech_started", "t": 10.0},
    {"type": "tester_speech_stopped", "t": 11.0},
    {"type": "tester_transcript", "t": 11.0, "text": "Order 123"},
    {"type": "app_bot_speech_started", "t": 15.0},
    {"type": "app_bot_speech_stopped", "t": 18.0},
    {
        "type": "app_bot_transcript",
        "t": 18.5,
        "text": "Thanks, processed.",
        "turn_started_at": "2026-06-15T00:00:15",
    },
    {"type": "session_stopped", "t": 20.0},
]


@pytest.fixture
def metrics():
    return compute_metrics(EVENTS, VAD_STOP_SECS)


def test_app_response_latencies(metrics):
    assert metrics["app_response_latencies_secs"] == [1.0, 4.0]
    assert metrics["summary"]["mean_app_response_latency_secs"] == 2.5
    assert metrics["summary"]["max_app_response_latency_secs"] == 4.0


def test_latency_only_for_first_app_turn_after_tester():
    # One tester utterance, then the app keeps talking in 3 segments (reading
    # on). Only the FIRST app start is the reply; the continuation segments get
    # no latency (else they'd grow against the stale tester stop).
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "tester_speech_started", "t": 2.0},
        {"type": "tester_speech_stopped", "t": 4.0},  # arm @4
        {"type": "app_bot_speech_started", "t": 5.0},  # reply: 1.0
        {"type": "app_bot_speech_stopped", "t": 9.0},
        {"type": "app_bot_speech_started", "t": 12.0},  # continuation: none
        {"type": "app_bot_speech_stopped", "t": 20.0},
        {"type": "app_bot_speech_started", "t": 25.0},  # continuation: none
        {"type": "app_bot_speech_stopped", "t": 30.0},
        {"type": "session_stopped", "t": 31.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["app_response_latencies_secs"] == [1.0]
    # CHANGED: this log has no transcripts at all, so the old transcript-driven
    # _turns() produced nothing. Turns now come from speech intervals, so all
    # three app utterances are reported (each flagged transcript_missing) and
    # only the first carries the latency.
    app_turns = [t for t in m["turns"] if t["speaker"] == "app_bot"]
    assert app_turns == [
        {"speaker": "app_bot", "t": 5.0, "transcript_missing": True, "response_latency_secs": 1.0},
        {"speaker": "app_bot", "t": 12.0, "transcript_missing": True},
        {"speaker": "app_bot", "t": 25.0, "transcript_missing": True},
    ]


def test_latency_rearms_on_repeated_tester_stop():
    # Tester stops twice before the app speaks -> measure from the LATEST stop;
    # then a continuation app segment gets no latency.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "tester_speech_started", "t": 2.0},
        {"type": "tester_speech_stopped", "t": 4.0},  # arm @4
        {"type": "tester_speech_started", "t": 5.0},
        {"type": "tester_speech_stopped", "t": 7.0},  # re-arm @7 (app stayed silent)
        {"type": "app_bot_speech_started", "t": 9.0},  # reply: 9-7 = 2.0
        {"type": "app_bot_speech_stopped", "t": 12.0},
        {"type": "app_bot_speech_started", "t": 15.0},  # continuation: none
        {"type": "app_bot_speech_stopped", "t": 20.0},
        {"type": "session_stopped", "t": 21.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["app_response_latencies_secs"] == [2.0]


def test_no_talk_over_in_clean_conversation(metrics):
    assert metrics["talk_over_windows"] == []
    assert metrics["summary"]["total_talk_over_secs"] == 0.0


def test_dead_air_gaps(metrics):
    # Speech union [2,4][5,9][10,11][15,18] -> gaps [4,5],[9,10],[11,15].
    # CHANGED: dead_air_gaps used to be all three (total 6.0). Gaps are now
    # attributed to whoever owed the next turn, so [9,10] — the tester
    # deciding what to say after the app finished — moves to tester think
    # time and total_dead_air_secs drops to 1.0 + 4.0 = 5.0. The two lists
    # still partition the same silence: 5.0 + 1.0 == the old 6.0.
    assert metrics["dead_air_gaps"] == [
        {"start": 4.0, "end": 5.0, "duration_secs": 1.0},
        {"start": 11.0, "end": 15.0, "duration_secs": 4.0},
    ]
    assert metrics["summary"]["total_dead_air_secs"] == 5.0
    assert metrics["tester_think_time_gaps"] == [
        {"start": 9.0, "end": 10.0, "duration_secs": 1.0},
    ]
    assert metrics["summary"]["total_tester_think_time_secs"] == 1.0


def test_talk_time_and_ratio(metrics):
    # tester 2.0+1.0 = 3.0 ; app_bot 4.0+3.0 = 7.0 ; ratio 3/7
    assert metrics["talk_time"]["tester_secs"] == 3.0
    assert metrics["talk_time"]["app_bot_secs"] == 7.0
    assert metrics["talk_time"]["ratio_tester_over_app_bot"] == round(3.0 / 7.0, 3)


def test_utterance_counts(metrics):
    assert metrics["utterances"] == {"tester": 2, "app_bot": 2}


def test_turns_merge_transcripts_with_app_latency(metrics):
    # CHANGED: a turn is now a speech interval, not a transcript, so each `t`
    # is the interval's START (2, 5, 10, 15) rather than the moment the
    # transcript landed (4, 9.3, 11, 18.5). The text is unchanged — every
    # interval here did get a transcript.
    assert metrics["turns"] == [
        {"speaker": "tester", "t": 2.0, "text": "I'd like a refund"},
        {
            "speaker": "app_bot",
            "t": 5.0,
            "text": "Sure, what's your order number?",
            "response_latency_secs": 1.0,
        },
        {"speaker": "tester", "t": 10.0, "text": "Order 123"},
        {
            "speaker": "app_bot",
            "t": 15.0,
            "text": "Thanks, processed.",
            "response_latency_secs": 4.0,
        },
    ]


def test_session_header_and_biases(metrics):
    session = metrics["session"]
    assert session["started_at"] == 0.0
    assert session["stopped_at"] == 20.0
    assert session["duration_secs"] == 20.0
    assert session["biases"]["vad_stop_secs"] == 1.0
    assert len(session["biases"]["notes"]) >= 1


def test_empty_session_does_not_crash():
    m = compute_metrics([], 1.0)
    assert m["turns"] == []
    assert m["app_response_latencies_secs"] == []
    assert m["talk_over_windows"] == []
    assert m["dead_air_gaps"] == []
    assert m["tester_think_time_gaps"] == []
    assert m["utterances"] == {"tester": 0, "app_bot": 0}
    assert m["talk_time"]["ratio_tester_over_app_bot"] is None
    assert m["summary"]["mean_app_response_latency_secs"] is None
    assert m["summary"]["total_dead_air_secs"] == 0.0
    assert m["session"]["duration_secs"] is None
    assert m["session"]["biases"]["vad_stop_secs"] == 1.0


def test_unclosed_interval_closes_at_session_stop():
    # app starts but never stops before teardown -> close at session_stopped
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "..."},
        {"type": "app_bot_speech_started", "t": 5.0},
        {"type": "session_stopped", "t": 10.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["utterances"]["app_bot"] == 1
    assert m["talk_time"]["app_bot_secs"] == 5.0


def test_app_speaks_first_has_no_latency():
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "..."},
        {"type": "app_bot_speech_started", "t": 1.0},
        {"type": "app_bot_speech_stopped", "t": 3.0},
        {"type": "app_bot_transcript", "t": 3.2, "text": "Hi!", "turn_started_at": "x"},
        {"type": "session_stopped", "t": 4.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["app_response_latencies_secs"] == []
    assert "response_latency_secs" not in m["turns"][0]


def test_runs_on_real_serialized_events():
    # Guard the production contract: compute_metrics consumes VoiceboxEvent
    # .model_dump() output, where `type` is a plain string (use_enum_values).
    evs = [
        ev.SessionStartedEvent(t=0.0, vad_stop_secs=1.0, note="n"),
        ev.VoiceboxEvent(type=ev.EventType.TESTER_SPEECH_STARTED, t=2.0),
        ev.VoiceboxEvent(type=ev.EventType.TESTER_SPEECH_STOPPED, t=4.0),
        ev.TesterTranscriptEvent(t=4.0, text="hello"),
        ev.VoiceboxEvent(type=ev.EventType.APP_BOT_SPEECH_STARTED, t=5.0),
        ev.VoiceboxEvent(type=ev.EventType.APP_BOT_SPEECH_STOPPED, t=8.0),
        ev.TranscriptEvent(t=8.2, text="hi there", turn_started_at="iso"),
        ev.VoiceboxEvent(type=ev.EventType.SESSION_STOPPED, t=10.0),
    ]
    m = compute_metrics([e.model_dump() for e in evs], 1.0)
    assert m["app_response_latencies_secs"] == [1.0]
    assert m["utterances"] == {"tester": 1, "app_bot": 1}
    assert m["turns"][1]["response_latency_secs"] == 1.0


def test_talk_over_window_when_tester_overlaps_app():
    # app_bot [5,10], tester [8,12] -> overlap [8,10] = 2.0s
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "..."},
        {"type": "app_bot_speech_started", "t": 5.0},
        {"type": "tester_speech_started", "t": 8.0},
        {"type": "app_bot_speech_stopped", "t": 10.0},
        {"type": "tester_speech_stopped", "t": 12.0},
        {"type": "session_stopped", "t": 13.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["talk_over_windows"] == [{"start": 8.0, "end": 10.0, "duration_secs": 2.0}]
    assert m["summary"]["total_talk_over_secs"] == 2.0


# --- turns reconcile with utterances (B1) ------------------------------------


def test_turns_count_matches_utterances():
    # B1: the app bot spoke four times but Whisper returned text for only
    # three — the fourth came back empty (STT ran and recovered nothing). All
    # four utterances must still be reported, the empty one flagged rather
    # than dropped, and the count must equal utterances.app_bot.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "app_bot_speech_started", "t": 1.0},
        {"type": "app_bot_speech_stopped", "t": 3.0},
        {"type": "app_bot_transcript", "t": 4.0, "text": "one", "turn_started_at": "x"},
        {"type": "app_bot_speech_started", "t": 5.0},
        {"type": "app_bot_speech_stopped", "t": 7.0},
        {"type": "app_bot_transcript", "t": 8.0, "text": "two", "turn_started_at": "x"},
        {"type": "app_bot_speech_started", "t": 9.0},
        {"type": "app_bot_speech_stopped", "t": 11.0},
        {"type": "app_bot_transcript", "t": 12.0, "text": "three", "turn_started_at": "x"},
        {"type": "app_bot_speech_started", "t": 13.0},
        {"type": "app_bot_speech_stopped", "t": 15.0},
        {"type": "app_bot_transcript", "t": 16.0, "text": "", "turn_started_at": "x"},
        {"type": "session_stopped", "t": 18.0},
    ]
    m = compute_metrics(events, 1.0)
    app_turns = [t for t in m["turns"] if t["speaker"] == "app_bot"]
    assert len(app_turns) == m["utterances"]["app_bot"] == 4
    # An empty transcript claims nothing: STT ran and recovered nothing, which
    # is indistinguishable from never having run as far as the text goes.
    assert [t.get("text") for t in app_turns] == ["one", "two", "three", None]
    assert [t.get("transcript_missing") for t in app_turns] == [None, None, None, True]
    assert all("text" not in t for t in app_turns if t.get("transcript_missing"))


def test_dropped_transcript_shifts_text_onto_the_next_turn():
    # The documented cost of matching by arrival order rather than by content
    # (see the third entry in metrics._BIAS_NOTES). The second utterance's
    # transcript never arrived, so "three" — which really describes the third
    # utterance — is claimed by the second, and the LAST utterance is the one
    # left flagged. Pinned deliberately: this is the bias the report warns
    # about, and it must not change silently.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "app_bot_speech_started", "t": 1.0},
        {"type": "app_bot_speech_stopped", "t": 3.0},
        {"type": "app_bot_transcript", "t": 4.0, "text": "one", "turn_started_at": "x"},
        {"type": "app_bot_speech_started", "t": 5.0},
        {"type": "app_bot_speech_stopped", "t": 7.0},  # transcript never arrived
        {"type": "app_bot_speech_started", "t": 9.0},
        {"type": "app_bot_speech_stopped", "t": 11.0},
        {"type": "app_bot_transcript", "t": 12.0, "text": "three", "turn_started_at": "x"},
        {"type": "session_stopped", "t": 14.0},
    ]
    m = compute_metrics(events, 1.0)
    app_turns = [t for t in m["turns"] if t["speaker"] == "app_bot"]
    assert len(app_turns) == m["utterances"]["app_bot"] == 3
    assert [t.get("text") for t in app_turns] == ["one", "three", None]
    # The count still reconciles — the shift moves text between turns, it never
    # loses a turn. That is what makes the bias tolerable.
    assert [t.get("transcript_missing") for t in app_turns] == [None, None, True]


@pytest.mark.parametrize(
    "fixture_name",
    ["session-20260728-121027-events.json", "session-20260728-cyoa-choice-events.json"],
)
def test_turns_count_matches_utterances_on_captured_sessions(fixture_name):
    # Real event logs captured from two dogfood runs against the readme app
    # (2026-07-28). Both lost transcripts — session 1 lost a 44.8 s app-bot
    # turn entirely — which is exactly what used to make turns and utterances
    # disagree (3 turns vs 3+1 utterances; 11 vs 6+11).
    events = json.loads((FIXTURES / fixture_name).read_text())
    m = compute_metrics(events, 1.0)
    for speaker in ("app_bot", "tester"):
        assert len([t for t in m["turns"] if t["speaker"] == speaker]) == m["utterances"][speaker]


def test_latency_survives_missing_transcript():
    # Captured session 1: the tester asked for the next chapter, the app bot
    # replied 3.754 s later and talked for 44.8 s — and that transcript never
    # arrived. The measured latency used to vanish with the text because it
    # was attached to the transcript; it now rides on the turn.
    events = json.loads((FIXTURES / "session-20260728-121027-events.json").read_text())
    m = compute_metrics(events, 1.0)
    last_app_turn = [t for t in m["turns"] if t["speaker"] == "app_bot"][-1]
    assert last_app_turn["transcript_missing"] is True
    assert "text" not in last_app_turn
    assert last_app_turn["response_latency_secs"] == 3.754
    assert m["app_response_latencies_secs"] == [3.754]


# --- gap attribution (B3) ----------------------------------------------------


def test_gap_after_app_is_tester_think_time():
    # The app finished at t=10; the tester took 12 s to decide what to say.
    # That is the driving agent thinking, not the app being slow.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "app_bot_speech_started", "t": 5.0},
        {"type": "app_bot_speech_stopped", "t": 10.0},
        {"type": "tester_speech_started", "t": 22.0},
        {"type": "tester_speech_stopped", "t": 24.0},
        {"type": "session_stopped", "t": 25.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["tester_think_time_gaps"] == [{"start": 10.0, "end": 22.0, "duration_secs": 12.0}]
    assert m["summary"]["total_tester_think_time_secs"] == 12.0
    assert m["dead_air_gaps"] == []
    assert m["summary"]["total_dead_air_secs"] == 0.0


def test_gap_after_tester_is_app_dead_air():
    # The tester finished at t=4; the app took 12 s to start replying. That is
    # the app being slow — the thing total_dead_air_secs is meant to measure.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "tester_speech_started", "t": 2.0},
        {"type": "tester_speech_stopped", "t": 4.0},
        {"type": "app_bot_speech_started", "t": 16.0},
        {"type": "app_bot_speech_stopped", "t": 18.0},
        {"type": "session_stopped", "t": 19.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["dead_air_gaps"] == [{"start": 4.0, "end": 16.0, "duration_secs": 12.0}]
    assert m["summary"]["total_dead_air_secs"] == 12.0
    assert m["tester_think_time_gaps"] == []


def test_gap_attribution_partitions_the_same_silence(metrics):
    # Neither list invents nor drops silence: together they still cover the
    # 6.0 s the pre-split _gaps() reported for the synthetic conversation.
    total = (
        metrics["summary"]["total_dead_air_secs"]
        + metrics["summary"]["total_tester_think_time_secs"]
    )
    assert total == 6.0


# --- the report says what it cannot know (B4) --------------------------------


def test_biases_note_covers_gap_attribution(metrics):
    notes = " ".join(metrics["session"]["biases"]["notes"])
    assert "dead_air_gaps" in notes
    assert "tester_think_time_gaps" in notes
    assert "transcript_missing" in notes


# --- outages are quarantined, not averaged in (round 3) ----------------------


def test_gap_spanning_disconnect_is_an_outage():
    # Round 3: the app's bot said goodbye and the client wedged; 107 s passed
    # before a page reload revived it. That silence was booked as dead air and
    # its span as a 107 s "response latency" (dragging the mean from ~4.6 s to
    # 17.8 s). A gap containing a client_disconnected is an outage: the call
    # was down, nobody was being slow.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "tester_speech_started", "t": 2.0},
        {"type": "tester_speech_stopped", "t": 4.0},
        {"type": "client_disconnected", "t": 10.0},
        {"type": "client_connected", "t": 80.0},
        {"type": "app_bot_speech_started", "t": 111.0},
        {"type": "app_bot_speech_stopped", "t": 113.0},
        {"type": "session_stopped", "t": 114.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["outage_gaps"] == [{"start": 4.0, "end": 111.0, "duration_secs": 107.0}]
    assert m["summary"]["total_outage_secs"] == 107.0
    assert m["dead_air_gaps"] == [] and m["summary"]["total_dead_air_secs"] == 0.0
    # The app's first words after the reconnect are a fresh session, not a
    # 107 s reply: no latency is recorded for that turn.
    assert m["app_response_latencies_secs"] == []
    assert m["summary"]["mean_app_response_latency_secs"] is None
    app_turn = next(t for t in m["turns"] if t["speaker"] == "app_bot")
    assert "response_latency_secs" not in app_turn


def test_gap_without_disconnect_still_attributed_normally():
    # The quarantine must not leak: an ordinary silence with the link up keeps
    # its dead-air attribution and its measured latency.
    events = [
        {"type": "session_started", "t": 0.0, "vad_stop_secs": 1.0, "note": "n"},
        {"type": "tester_speech_started", "t": 2.0},
        {"type": "tester_speech_stopped", "t": 4.0},
        {"type": "app_bot_speech_started", "t": 16.0},
        {"type": "app_bot_speech_stopped", "t": 18.0},
        {"type": "session_stopped", "t": 19.0},
    ]
    m = compute_metrics(events, 1.0)
    assert m["outage_gaps"] == [] and m["summary"]["total_outage_secs"] == 0.0
    assert m["dead_air_gaps"] == [{"start": 4.0, "end": 16.0, "duration_secs": 12.0}]
    assert m["app_response_latencies_secs"] == [12.0]
