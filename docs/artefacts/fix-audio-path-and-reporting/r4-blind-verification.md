# Round 4 — blind live verification (F + G)

*Captured 2026-08-03. Fix commit `f90c358` (code), decision `BUILDLOG.md` D15.
No session artefacts on disk — that absence is itself this round's headline finding. Fresh
spec-blind tester; long sentences, one barge-in, and a deliberately immediate `stop()`.*

## Task G verified live

* **One `tester_speech_started/stopped` pair per `speak()`, all six calls** — no splits, no
  mid-utterance gaps (rounds 2–3 had 3-of-7 utterances splitting, worst gap 4.2 s).
* **Ember never answered a fragment, never interjected mid-sentence, never complained about
  interruptions** the tester didn't make. G4's intent observed from the app's behaviour.
* `wait_for_playout` return agreed with the event log **to the microsecond** on both
  `started_at` and `finished_at` — the settle-timer early-return (round 2) is gone.
* The phantom `transcript_missing` tester turns are gone from the stream (6 speaks → 6
  transcripts → 6 pairs; metrics reconciliation itself was unverifiable, see below).
* Cost, as predicted in the G artefact: `speak()`→audio is now full-synthesis-bound —
  7.0–8.5 s for ~30-word sentences. Recorded in issue #13; the synthesize-at-arm idea for
  barge-ins remains the candidate mitigation.

## The headline: `record_dir` produced nothing (fixed, D15)

`stop()` blocked ~50 s, returned `{"stopped": true}` with **no artifacts key**, and no
`temp/verify-round-4/` existed anywhere on disk. Root cause (read directly from
`server.py`): the IPC stop deadline was still 30 s from before Task F; the parent timed out
while the child was legitimately draining the final 51 s utterance's transcription, then
**reaped the child mid-drain — before `_dump_artifacts` ran** — and reported success anyway.
Task F's own criterion worked (the drain ran); the layer above killed it. Fix: stop deadline
30 → 210 s, above the 180 s drain cap. **Requires an MCP server restart to take effect.**

## Also found

1. **Watchdog vs slow decode, second round.** A 116 s narration decoded for ~108 s
   (~0.93× realtime under load — the idle probe's 0.53× is not the worst case). The 90 s
   watchdog closed the turn empty (`transcription_empty: true`, correct `turn_started_at`),
   then the real text re-emitted 17.9 s later as an orphan event stamped at its own arrival
   time with no speech pair to join to. `TURN_STOP_TIMEOUT_SECS` 90 → 240 s (outlives the
   drain cap). Note: metrics' positional matcher would still attach the orphan text to the
   right interval; the oddity is events.json-level.
2. **~25 s transcript-lag floor on even 4 s utterances** (25.3–29.1 s on short turns) —
   unexplained by decode alone (~2 s) and NOT attributable without the child's DEBUG log,
   which dies with the server's terminal. Landed: `agent-debug.log` written into
   `record_dir` per session, so the next round can read the `voicebox.timing` split.
3. **Barge-in "slow arm" — rejected as a defect** (BUILDLOG D15). The tester's own
   timestamps show the arm registered ~1.5 s after its MCP call; the missed utterance was
   armed *after* Ember had already started speaking (tester turnaround), so the trigger
   correctly waited for the next start. The debug log will falsify this if wrong.
4. Suspicious first-utterance VAD span: greeting pair covers 3.6 s but its transcript is
   ~28 words. Audio was fully captured (transcript complete) but the interval under-covers.
   Watch next round (needs the WAVs that this round lost).

## Quality gates

63 passed; ruff + format clean; pyright at the 2-error baseline. (No new tests this round:
the deadline fix has no parent-side test seam — the comment carries the constraint — and the
watchdog constant's test already asserts equality with the constant.)

## Not covered

* Everything `events.json`/`metrics.json`-dependent for this session: tester/app utterance
  reconciliation, `transcript_missing` accounting, whether the final utterance's transcript
  survived the prompt stop. **Round 5 must repeat the prompt-stop check after the server
  restart.**
* The lag-floor attribution (item 2) is landed-but-unread until a round runs with the new
  debug sink.
