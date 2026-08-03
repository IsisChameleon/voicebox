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

    disconnects = [e["t"] for e in events if e["type"] == "client_disconnected"]

    app_latencies = _app_response_latencies(events, app_intervals)
    latencies = [latency for latency in app_latencies if latency is not None]

    talk_over = _overlaps(tester_intervals, app_intervals)
    total_talk_over = round(sum(w["duration_secs"] for w in talk_over), 3)

    dead_air, think_time, outages = _gaps(tester_intervals, app_intervals, disconnects)
    total_dead_air = round(sum(g["duration_secs"] for g in dead_air), 3)
    total_think_time = round(sum(g["duration_secs"] for g in think_time), 3)
    total_outage = round(sum(g["duration_secs"] for g in outages), 3)

    tester_secs = round(sum(stop - start for start, stop in tester_intervals), 3)
    app_bot_secs = round(sum(stop - start for start, stop in app_intervals), 3)

    turns = _turns(events, app_intervals, app_latencies, tester_intervals)
    session = _session_header(events, vad_stop_secs)

    return {
        "session": session,
        "turns": turns,
        "app_response_latencies_secs": latencies,
        "talk_over_windows": talk_over,
        "dead_air_gaps": dead_air,
        "tester_think_time_gaps": think_time,
        "outage_gaps": outages,
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
            "total_tester_think_time_secs": total_think_time,
            "total_outage_secs": total_outage,
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


def _app_response_latencies(
    events: list[dict], app_intervals: list[tuple[float, float]]
) -> list[float | None]:
    """Response latency per app interval, aligned to ``app_intervals``.

    A latency is the reply onset: the gap from a tester utterance's end to the
    NEXT app-bot speech start. Only the first app start after each tester stop
    counts — later app intervals are the bot reading/talking on (continuation)
    and get ``None``. The timer is (re)armed on every ``tester_speech_stopped``
    /``_interrupted``: if the tester stops again before the bot speaks, the
    latest stop wins; once the bot speaks it disarms until the tester stops
    again. A ``client_disconnected`` in between disarms it too: the reply
    never came in that connection, and the app's first words after a
    reconnect are a fresh session, not a 100 s "latency".
    """
    by_start: dict[float, float] = {}
    pending_tester_stop: float | None = None
    for e in events:
        if e["type"] in ("tester_speech_stopped", "tester_speech_interrupted"):
            pending_tester_stop = e["t"]
        elif e["type"] == "client_disconnected":
            pending_tester_stop = None
        elif e["type"] == "app_bot_speech_started" and pending_tester_stop is not None:
            by_start[e["t"]] = round(e["t"] - pending_tester_stop, 3)
            pending_tester_stop = None
    return [by_start.get(start) for start, _stop in app_intervals]


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
    "talk time is overestimated by ~that much and the tester think-time gap that "
    "follows a bot turn is understated by ~that much.",
    "dead_air_gaps / total_dead_air_secs count ONLY the silence that follows a "
    "tester utterance — the app owing a reply. Silence following an app utterance "
    "is in tester_think_time_gaps: it is how long the driving agent took to call "
    "speak() again, not an app defect. Neither field says WHY a silence happened, "
    "and a gap is attributed to whoever's speech ends furthest right when the two "
    "parties overlap.",
    "turns come one-per-speech-interval, so an utterance still appears when its "
    "transcript never arrived (flagged transcript_missing, no text key). Text is "
    "matched to intervals by arrival order, not by content: an app transcript "
    "goes to the earliest unmatched interval that had already finished when it "
    "arrived; a tester transcript to the earliest unmatched interval starting at "
    "or after its speak() call. A dropped or out-of-order transcript therefore "
    "shifts text onto a neighbouring turn.",
    "app_response_latency_secs measures the user-perceived wait (tester speech end "
    "to app speech start): the app's endpointing + STT/LLM/TTS think time, not pure "
    "server compute. tester_speech_stopped is playout-accurate; app_bot_speech_started "
    "is VAD-onset (~tens of ms lag, unaffected by vad_stop_secs).",
    "app_bot_transcript text arrives after batch Whisper and is utterance-level, not "
    "word-accurate.",
    "a silent gap containing a client_disconnected is an outage_gap, not dead air or "
    "think time, and a tester utterance answered only after a disconnect contributes "
    "no response latency — the call was down, not slow.",
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


def _spoken_transcripts(events: list[dict], event_type: str) -> list[dict]:
    """Transcript events of one type that actually carry text, in log order.

    An empty ``text`` means STT ran and recovered nothing. That is not a
    transcript for matching purposes — the interval it belongs to is still
    reported as ``transcript_missing``.
    """
    return [e for e in events if e["type"] == event_type and e.get("text")]


def _match_app_transcripts(
    intervals: list[tuple[float, float]], transcripts: list[dict]
) -> list[dict | None]:
    """Pair each app speech interval with the transcript that reports it.

    Batch STT emits transcripts in segment order, well after the speech ended,
    so a transcript is claimed by the earliest still-unclaimed interval that
    had already finished when the transcript arrived. An interval nothing
    claims keeps ``None``: its transcript never arrived.
    """
    matched: list[dict | None] = [None] * len(intervals)
    next_unclaimed = 0
    for transcript in transcripts:
        if next_unclaimed >= len(intervals):
            break
        if intervals[next_unclaimed][1] > transcript["t"]:
            continue  # nothing had finished yet; a later transcript may claim it
        matched[next_unclaimed] = transcript
        next_unclaimed += 1
    return matched


def _match_tester_transcripts(
    intervals: list[tuple[float, float]], transcripts: list[dict]
) -> list[dict | None]:
    """Pair each tester speech interval with the ``speak()`` text behind it.

    ``tester_transcript`` is ground truth stamped at ``speak()`` time, so its
    playout cannot be an interval that had already ended: a transcript is
    claimed by the earliest still-unclaimed interval ending at or after it.
    One ``speak()`` whose playout fragments into several intervals therefore
    labels only the first of them.
    """
    matched: list[dict | None] = [None] * len(intervals)
    next_unclaimed = 0
    for transcript in transcripts:
        while next_unclaimed < len(intervals) and intervals[next_unclaimed][1] < transcript["t"]:
            next_unclaimed += 1  # already over when speak() ran: no transcript can claim it
        if next_unclaimed >= len(intervals):
            break
        matched[next_unclaimed] = transcript
        next_unclaimed += 1
    return matched


def _turn(speaker: str, start: float, transcript: dict | None) -> dict:
    """One turn row, stamped at the speech interval's start."""
    if transcript is None:
        return {"speaker": speaker, "t": start, "transcript_missing": True}
    return {"speaker": speaker, "t": start, "text": transcript["text"]}


def _turns(
    events: list[dict],
    app_intervals: list[tuple[float, float]],
    app_latencies: list[float | None],
    tester_intervals: list[tuple[float, float]],
) -> list[dict]:
    """One turn per speech interval, both speakers merged in time order.

    Turns come from speech intervals rather than transcripts so the turn count
    reconciles with ``utterances`` by construction, and so a missing transcript
    cannot take a measured ``response_latency_secs`` down with it — the latency
    rides on the app turn, not on its text.
    """
    app_texts = _match_app_transcripts(
        app_intervals, _spoken_transcripts(events, "app_bot_transcript")
    )
    tester_texts = _match_tester_transcripts(
        tester_intervals, _spoken_transcripts(events, "tester_transcript")
    )

    turns = []
    for (start, _stop), transcript, latency in zip(app_intervals, app_texts, app_latencies):
        turn = _turn("app_bot", start, transcript)
        if latency is not None:
            turn["response_latency_secs"] = latency
        turns.append(turn)
    for (start, _stop), transcript in zip(tester_intervals, tester_texts):
        turns.append(_turn("tester", start, transcript))
    turns.sort(key=lambda turn: turn["t"])
    return turns


def _gaps(
    tester_intervals: list[tuple[float, float]],
    app_intervals: list[tuple[float, float]],
    disconnects: list[float],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split the silent spans by who owed the next turn.

    Returns ``(app_dead_air, tester_think_time, outages)``. Silence after a
    tester utterance is the app being slow to answer; silence after an app
    utterance is the driving agent deciding what to say next. A gap containing
    a ``client_disconnected`` is neither — the call was down — and is
    quarantined as an outage so it cannot pollute the conversational totals.
    Overlapping speech is merged as before, and the gap belongs to whoever's
    interval ends furthest right.
    """
    ordered = sorted(
        [(start, stop, "tester") for start, stop in tester_intervals]
        + [(start, stop, "app_bot") for start, stop in app_intervals]
    )
    if not ordered:
        return [], [], []
    app_dead_air: list[dict] = []
    tester_think_time: list[dict] = []
    outages: list[dict] = []
    cursor, owed_by_app = ordered[0][1], ordered[0][2] == "tester"
    for start, stop, speaker in ordered[1:]:
        if start > cursor:
            gap = {"start": cursor, "end": start, "duration_secs": round(start - cursor, 3)}
            if any(cursor <= t < start for t in disconnects):
                outages.append(gap)
            else:
                (app_dead_air if owed_by_app else tester_think_time).append(gap)
        if stop > cursor:
            cursor, owed_by_app = stop, speaker == "tester"
    return app_dead_air, tester_think_time, outages
