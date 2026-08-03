# Round 3 — blind live verification (stretched scenario)

*Captured 2026-08-03. Fix commit `63dd58d` (code), decision `BUILDLOG.md` D12.
Session artefacts: `temp/verify-round-3/` (gitignored). Fresh spec-blind tester; stretched
scenario: a requested long narration, a deliberate 10 s silence, two barge-ins, and a
deliberately prompt `stop()`. ~17 min session, including an app-side outage mid-way.*

## Round-2 fixes verified live

* **`turn_started_at` exact to the millisecond on every checked turn** — five checks
  including late chunks (e.g. philosopher turn `10:08:01.167` vs `speech_started`
  …51681.1673). The D10 stamping holds; the only mismatch left is the *merged-utterance*
  case (below), which is an aggregation artefact, not a stamping one.
* **`transcription_lag_secs` behaves in normal operation** — climbing 55.9 → 109.4 across
  empty polls while a long transcript was pending; 0.054–0.125 on fresh
  `app_bot_speech_stopped` events (the D11 settle working).
* Barge-in timers: 1.503 s and 1.504 s vs configured 1.5 — exact, twice.
* `wait_for_playout` span consistent with the event log "to the microsecond".
* The 10 s deliberate silence was booked as tester think time (correct bucket, correct
  arithmetic — 28.36 s measured, the surplus being the tester's own tool-loop + TTS time).

## Fixed this round (commit `63dd58d`, D12)

**Outage pollution of conversation metrics.** Ember said goodbye on its own, the client
wedged on "Waking up…", and the tester's page reload took 107.051 s door-to-door. That span
was booked as a dead-air gap AND a 107.05 s response latency (mean 17.776 s vs honest
2.9–7.0 s values). Second occurrence across rounds (round 1: 431 s). Gaps containing a
`client_disconnected` now land in `outage_gaps` / `total_outage_secs`, and a disconnect
disarms the pending-latency timer. Replay of this round's log:

```
outage_gaps       : [{'start': …51890.948, 'end': …51997.999, 'duration_secs': 107.051}]
latencies         : [2.938, 6.377, 6.924, 7.023, 3.995, 3.33, 4.569]
mean latency      : 5.022        (was 17.776)
total_outage_secs : 107.051
```

Pinned by `test_gap_spanning_disconnect_is_an_outage` /
`test_gap_without_disconnect_still_attributed_normally`. Suite 50 → 52 passing.

## Confirmed for the next tasks (deliberate reproductions, not fixed here)

* **Task F — prompt `stop()` lost four app-bot turns (~100 s of speech).** The tester
  stopped 11.3 s after Ember's last speech end; with observed transcript lags of 60–140 s,
  every un-decoded segment was cancelled. Nothing in `stop()`'s return warns of the loss.
  **Design note for F:** the spec's "bounded (~15 s)" drain is undersized against measured
  backlogs (up to 140.2 s); the bound needs to scale with the pending backlog or be much
  larger — record the adaptation in BUILDLOG when F lands (D3/D5 precedent).
* **Task F — lag proxy blind spot.** `transcription_lag_secs` read 0.0 while two finished
  turns were still untranscribed: Whisper had *decoded* them but the aggregator was holding
  the text because a new VAD start kept the turn open (the stop strategy resets
  `_turn_complete` on VAD start). The STT-queue-age proxy cannot see held text. The honest
  event-log-based lag (age of oldest unmatched speech stop) becomes safe once F emits
  empty-transcript events — implement together (revisits D11's rejection).
* **Task G — the splits now break conversations.** 3 of 7 speaks fragmented; the worst
  (4.2 s mid-sentence gap) made Ember take the turn, get talked over by the resuming
  fragment, and scold the tester ("I appreciate your enthusiasm, but…"). One run-invalidating
  event, live. Also: 3 phantom `transcript_missing` tester turns again.
* **Task G-adjacent — barge-in audio lands 3.0–4.7 s after firing** (Kokoro synthesis), so
  `timer_secs=1.5` really lands ~4.5–6.2 s into the bot's turn; barge-in 1 missed Ember's
  speech entirely (she'd finished ~0.7 s before the audio started). Candidate: synthesize at
  arm time, play on fire.
* **Merged-utterance attribution.** The aggregator merged a 1.2 s and a 127.6 s interval
  into one transcript, which claimed the 1.2 s interval; the 2-minute narration shows as
  `transcript_missing`. Known positional-matching bias (D2) aggravated by aggregation;
  full fix would need per-segment transcripts (the out-of-scope aggregator redesign).
* **Ergonomics for Task H:** armed barge-ins survive a page reload and fire on the wrong
  turn (no disarm API); a dangling `app_bot_speech_started` (VAD flicker at teardown,
  0.29 s before stop) closes the log unpaired.

## Not covered

* No empty Whisper result occurred, so the D10 deque's shift-bias under empty segments is
  still only reasoning, not observation — F's tests must cover it.
* The app-side quirks recurred as known (auto-flip once, harmless; the wedge once,
  recovered by reload); `enumerateDevices` slowness was not re-measured this round.
