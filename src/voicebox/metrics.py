#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Compute a test-report metrics dict from a voicebox conversation event log.

Pure function: in = the serialized event log (the same dicts ``listen()``
returns), out = the ``metrics.json`` payload. No I/O, no pipecat imports, so the
interval arithmetic is unit-testable without a browser.
"""


def compute_metrics(events: list[dict], vad_stop_secs: float) -> dict:
    """Derive conversation metrics from a list of event dicts.

    Args:
        events: The session event log, each ``{"type": ..., "t": ..., ...}``.
        vad_stop_secs: The VAD stop window, recorded so consumers can correct
            the app-bot stop lag.

    Returns:
        The metrics payload (see the Stage 4 design spec for the schema).

    """
    close_at = _close_at(events)
    app_intervals = _intervals(events, "app_bot_speech_started", "app_bot_speech_stopped", close_at)
    tester_intervals = _tester_intervals(events, close_at)
    tester_stops = [stop for _start, stop in tester_intervals]

    # Latency per app interval (None when the app spoke first / no preceding
    # tester turn); the array drops the Nones.
    app_latencies: list[float | None] = []
    for start, _stop in app_intervals:
        preceding = [t for t in tester_stops if t < start]
        app_latencies.append(round(start - max(preceding), 3) if preceding else None)
    latencies = [latency for latency in app_latencies if latency is not None]

    talk_over = _overlaps(tester_intervals, app_intervals)
    total_talk_over = round(sum(w["duration_secs"] for w in talk_over), 3)

    dead_air = _gaps(tester_intervals + app_intervals)
    total_dead_air = round(sum(g["duration_secs"] for g in dead_air), 3)

    tester_secs = round(sum(stop - start for start, stop in tester_intervals), 3)
    app_bot_secs = round(sum(stop - start for start, stop in app_intervals), 3)

    turns = _turns(events, app_intervals, app_latencies)
    session = _session_header(events, vad_stop_secs)

    return {
        "session": session,
        "turns": turns,
        "app_response_latencies_secs": latencies,
        "talk_over_windows": talk_over,
        "dead_air_gaps": dead_air,
        "talk_time": {
            "tester_secs": tester_secs,
            "app_bot_secs": app_bot_secs,
            "ratio_tester_over_app_bot": (
                round(tester_secs / app_bot_secs, 3) if app_bot_secs else None
            ),
        },
        "utterances": {"tester": len(tester_intervals), "app_bot": len(app_intervals)},
        "summary": {
            "mean_app_response_latency_secs": (
                round(sum(latencies) / len(latencies), 3) if latencies else None
            ),
            "max_app_response_latency_secs": max(latencies) if latencies else None,
            "total_talk_over_secs": total_talk_over,
            "total_dead_air_secs": total_dead_air,
        },
    }


def _close_at(events: list[dict]) -> float | None:
    """When to close a speech interval still open at teardown."""
    stopped = next((e["t"] for e in reversed(events) if e["type"] == "session_stopped"), None)
    if stopped is not None:
        return stopped
    return events[-1]["t"] if events else None


def _intervals(
    events: list[dict], start_type: str, stop_type: str, close_at: float | None
) -> list[tuple[float, float]]:
    """Pair start/stop events into (start, stop) intervals, in time order.

    An interval still open at the end of the log is closed at ``close_at``.
    """
    intervals = []
    open_start = None
    for e in events:
        if e["type"] == start_type:
            open_start = e["t"]
        elif e["type"] == stop_type and open_start is not None:
            intervals.append((open_start, e["t"]))
            open_start = None
    if open_start is not None and close_at is not None and close_at > open_start:
        intervals.append((open_start, close_at))
    return intervals


def _tester_intervals(events: list[dict], close_at: float | None) -> list[tuple[float, float]]:
    """Tester speech intervals; an interruption closes an interval like a stop."""
    intervals = []
    open_start = None
    for e in events:
        if e["type"] == "tester_speech_started":
            open_start = e["t"]
        elif e["type"] in ("tester_speech_stopped", "tester_speech_interrupted"):
            if open_start is not None:
                intervals.append((open_start, e["t"]))
                open_start = None
    if open_start is not None and close_at is not None and close_at > open_start:
        intervals.append((open_start, close_at))
    return intervals


def _overlaps(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[dict]:
    """Return the intersection windows between two interval lists."""
    windows = []
    for a_start, a_stop in a:
        for b_start, b_stop in b:
            start = max(a_start, b_start)
            stop = min(a_stop, b_stop)
            if stop > start:
                windows.append(
                    {"start": start, "end": stop, "duration_secs": round(stop - start, 3)}
                )
    windows.sort(key=lambda w: w["start"])
    return windows


_BIAS_NOTES = [
    "app_bot_speech_stopped trails true speech end by ~vad_stop_secs, so app-bot "
    "talk time and the dead-air gap after a bot turn are overestimated by ~that much.",
    "app_response_latency_secs measures the user-perceived wait (tester speech end "
    "to app speech start): the app's endpointing + STT/LLM/TTS think time, not pure "
    "server compute. tester_speech_stopped is playout-accurate; app_bot_speech_started "
    "is VAD-onset (~tens of ms lag, unaffected by vad_stop_secs).",
    "app_bot_transcript text arrives after batch Whisper and is utterance-level, not "
    "word-accurate.",
]


def _session_header(events: list[dict], vad_stop_secs: float) -> dict:
    """Build the session span + biases header from the log's bookends."""
    started = next((e["t"] for e in events if e["type"] == "session_started"), None)
    stopped = next((e["t"] for e in reversed(events) if e["type"] == "session_stopped"), None)
    configured = next(
        (e["vad_stop_secs"] for e in events if e["type"] == "session_started"),
        vad_stop_secs,
    )
    duration = round(stopped - started, 3) if started is not None and stopped is not None else None
    return {
        "started_at": started,
        "stopped_at": stopped,
        "duration_secs": duration,
        "biases": {"vad_stop_secs": configured, "notes": _BIAS_NOTES},
    }


def _turns(
    events: list[dict],
    app_intervals: list[tuple[float, float]],
    app_latencies: list[float | None],
) -> list[dict]:
    """Merge tester/app transcripts in time order; app turns carry their latency.

    An app transcript is associated with the app speech interval whose stop is
    the latest at or before the transcript's ``t`` (batch STT arrives after the
    speech), and inherits that interval's response latency.
    """
    turns = []
    for e in events:
        if e["type"] == "tester_transcript":
            turns.append({"speaker": "tester", "t": e["t"], "text": e["text"]})
        elif e["type"] == "app_bot_transcript":
            turn = {"speaker": "app_bot", "t": e["t"], "text": e["text"]}
            matched = [i for i, (_s, stop) in enumerate(app_intervals) if stop <= e["t"]]
            if matched:
                latency = app_latencies[matched[-1]]
                if latency is not None:
                    turn["response_latency_secs"] = latency
            turns.append(turn)
    turns.sort(key=lambda turn: turn["t"])
    return turns


def _gaps(intervals: list[tuple[float, float]]) -> list[dict]:
    """Silent spans between the merged union of all speech intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    gaps = []
    cursor = ordered[0][1]
    for start, stop in ordered[1:]:
        if start > cursor:
            gaps.append({"start": cursor, "end": start, "duration_secs": round(start - cursor, 3)})
        cursor = max(cursor, stop)
    return gaps
