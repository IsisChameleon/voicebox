import pytest

# Imported as a module (not by name) so pytest doesn't try to collect the
# event classes whose names start with "Test" (e.g. TesterTranscriptEvent).
import voicebox.events as ev
from voicebox.metrics import compute_metrics

# A synthetic conversation with known boundaries. Times in seconds.
#
#   tester  T1: [2, 4]            "I'd like a refund"
#   app_bot A1: [5, 9]            "Sure, what's your order number?"   latency 5-4 = 1.0
#   tester  T2: [10, 11]          "Order 123"
#   app_bot A2: [15, 18]          "Thanks, processed."                latency 15-11 = 4.0
#
# Speech-interval union: [2,4] [5,9] [10,11] [15,18]
#   dead-air gaps between them: [4,5]=1.0, [9,10]=1.0, [11,15]=4.0
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
    # only the first app turn carries the latency
    app_turns = [t for t in m["turns"] if t["speaker"] == "app_bot"]
    # (no transcripts here, so just assert the latency array is right)
    assert app_turns == []


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
    # Speech union [2,4][5,9][10,11][15,18] -> gaps [4,5],[9,10],[11,15]
    assert metrics["dead_air_gaps"] == [
        {"start": 4.0, "end": 5.0, "duration_secs": 1.0},
        {"start": 9.0, "end": 10.0, "duration_secs": 1.0},
        {"start": 11.0, "end": 15.0, "duration_secs": 4.0},
    ]
    assert metrics["summary"]["total_dead_air_secs"] == 6.0


def test_talk_time_and_ratio(metrics):
    # tester 2.0+1.0 = 3.0 ; app_bot 4.0+3.0 = 7.0 ; ratio 3/7
    assert metrics["talk_time"]["tester_secs"] == 3.0
    assert metrics["talk_time"]["app_bot_secs"] == 7.0
    assert metrics["talk_time"]["ratio_tester_over_app_bot"] == round(3.0 / 7.0, 3)


def test_utterance_counts(metrics):
    assert metrics["utterances"] == {"tester": 2, "app_bot": 2}


def test_turns_merge_transcripts_with_app_latency(metrics):
    assert metrics["turns"] == [
        {"speaker": "tester", "t": 4.0, "text": "I'd like a refund"},
        {
            "speaker": "app_bot",
            "t": 9.3,
            "text": "Sure, what's your order number?",
            "response_latency_secs": 1.0,
        },
        {"speaker": "tester", "t": 11.0, "text": "Order 123"},
        {
            "speaker": "app_bot",
            "t": 18.5,
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
