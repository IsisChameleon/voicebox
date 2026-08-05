# Round 5 — blind live verification (prompt-stop / F1 live)

*Captured 2026-08-03, after the MCP server restart that activated the D15 stop deadline.
Fix commit `69a716f` (code), decision `BUILDLOG.md` D16. Session artefacts:
`temp/verify-round-5/` (gitignored), including the first captured `agent-debug.log`.*

## The key test — Task F's F1, live: PASS

The tester called `stop()` 3.7 s after Ember's final `app_bot_speech_stopped`, with that
utterance's transcription still in flight:

* `stop()` blocked **~32.2 s** (the drain), then returned `{"stopped": true}` with all five
  artifact paths — every file present on disk.
* The final transcript **is in `events.json`** (`t` 16.5 s after `session_stopped`, exactly
  the documented ordering) and lands as the last turn in `metrics.json` with its measured
  `response_latency_secs: 4.035` — not `transcript_missing`.
* 9 of 9 app-bot speech intervals got transcripts; zero `transcription_empty` events; no
  orphan transcripts (the 240 s watchdog was never outrun).

## Also verified

* Response latencies in a tight 3.6–4.4 s cluster, matching `metrics.json` exactly;
  `outage_gaps: []`; the arrival-order matcher kept all 9 texts on the right turns despite
  lags up to 106.8 s.
* Barge-in: fire at trigger + 1.505 s (configured 1.5), real overlap with Ember's speech,
  and Ember answered the interjection. The tester's "arm registered late" impression is again
  its own turnaround — the armed event is emitted synchronously inside `speak()` before the
  call returns (`agent.py`), so the trigger is live from call-return.
* `listen()`'s `transcription_lag_secs` tracked the backlog visibly (49.5 → 100.8 → 0.0).

## The lag floor, attributed (closes the round-4 question)

`agent-debug.log`'s `voicebox.timing` lines decompose every turn: `analyze_end_of_turn`
0.27–0.43 s, `on_user_turn_stopped` ≤ 1 ms, and **`run_stt` 22–28 s regardless of segment
length** (6.3 s audio → 27.6 s; 23.1 s → 26.6 s; 57 s → 59.7 s; 93 s → 106.8 s). The entire
floor is inside `model.transcribe`: ~`max(25 s, 1.1× duration)` live. Posted with hypotheses
(temperature-fallback ladder on WebRTC-quality audio, CPU contention) to issue #13; the
optimization is deliberately out of this branch's scope.

## Fixed this round (commit `69a716f`, D16)

**Cold-start synthesis split.** The session's *first* `speak()` fragmented into two speech
pairs with a 5.33 s silent gap — Ember took the turn on the fragment, got talked over, and a
misleading `transcript_missing` tester row appeared. pipecat's TTS layer synthesizes per
sentence, so Task G's buffering (per `run_tts` call) cannot bridge a *between-sentences* gap;
the gap was the first ONNX inference's one-time cost (all 13 later utterances across rounds
4–5 were single-span). Fix: `KokoroTTSService.warm_up()` — a discarded throwaway synthesis
fired at agent start, concurrent with browser startup. Pinned by
`test_warm_up_consumes_one_synthesis`. Also: `agent-debug.log` now appears in `stop()`'s
artifacts dict (`debug_log` key).

## Quality gates

Suite 63 → 64 passing; ruff + format clean; pyright at the 2-error baseline.

## Not covered

* The warm-up fix is unit-tested but not yet live-verified — round 6 must show the first
  `speak()` of a fresh session as a single span.
* `speak()`→audio latency ran 5.7–16.4 s this round (worst under decode contention) —
  issue #13 territory, no fix planned on this branch.
* Whisper quality nits on WebRTC audio ("met a night", "with spirit, at your back") —
  utterance-level model output, as documented.
