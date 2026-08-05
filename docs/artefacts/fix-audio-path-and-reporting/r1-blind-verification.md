# Round 1 — blind live verification

*Captured 2026-08-03. Fix commit `861cf3e` (code), decisions `BUILDLOG.md` D7–D9.
Session artefacts: `temp/verify-round-1/` (gitignored). Tester: a fresh agent with no access
to specs, walkthrough, BUILDLOG or git history, driving voicebox's MCP tools against
EmberTales ("The Cave of Time") on `localhost:3000`.*

First live exercise of the branch: Tasks A–E landed, F–H not started. The round's job was to
confirm or refute the specced expectations and surface unknowns.

## Blocked start → D7

The checked-in test password (lowercase e) had drifted from the app's local database:
`400 invalid_credentials` on the Supabase password grant, while signup returned
`422 user_already_exists`. The user supplied the correct password (`Embertales456`) and
directed removal of the stale `scripts/e2e_readme_call.py` (commit `f1bd16c`, D7).

## What the round confirmed (specced, expected)

* **Task A live stories A3/A4 pass.** `start_browser_session` returned first try with the new
  `attach_hint`; the shim survived SPA navigations and a full `page.reload()` (clean
  `client_disconnected`/`client_connected` pair 40 ms apart, `wsReady: true` throughout).
* **Task D lives.** Every Ember utterance got a transcript starting at a sentence boundary; a
  barged-in turn was captured up to the cut ("…but I'm..."). No "Thank you." hallucinations
  (round-1's probe reproduced that exact hallucination on a *silent* slice — it is a
  silence artefact, consistent with D4's diagnosis).
* **Task B reconciles live.** `utterances {tester: 15, app_bot: 13}` matched the turn list
  exactly.
* **Task E's return shapes work live.** `wait_for_playout=True` returned
  `{queued, played, started_at, finished_at, interrupted}` matching the event log; the armed
  barge-in fired 1.502 s after its trigger (configured 1.5 s).
* **Task G's hole confirmed.** One `speak()` surfaces as 2–3 speech-start/stop pairs; two
  mid-utterance gaps of 3.6–4.3 s made Ember endpoint early and interject ("It really does.")
  — the specced "app hears one utterance as two turns", observed live. Also produced 6 bogus
  `transcript_missing` tester turns and two 0.0-duration `dead_air_gaps` entries.
* **Task F's pressure confirmed.** The final transcript survived only because the tester
  idled ~26 s before `stop()`; a prompt stop would have lost it.

## What the round found (not specced)

Ranked as reported by the blind tester:

1. **`listen(timeout>≈45)` hard-errors** with `The operation timed out.` — the MCP client's
   ~60 s per-request cap kills the call before voicebox's own deadline (`timeout + 30 s`)
   matters. Docstring now warns (needs server restart); a real keepalive is out of scope here.
2. **`turn_started_at` reported transcript-arrival time** (off 25–34 s, verified on three
   turns). → watchdog interplay, D9 below.
3. **`transcription_lag_secs` pinned at 0.0** while callers waited 25–59 s for transcripts.
   → loop freeze, D8 below.
4. **`speak()` audio started 4.3–12.0 s after the call** (tester timed `tester_transcript`
   emit → `tester_speech_started`). Partly Kokoro synthesis, partly the same loop freeze.
5. **Metrics don't quarantine outages**: a 431 s app outage rode into
   `mean_app_response_latency_secs` (65.078 s vs honest per-turn 0.6–7 s). Deferred — see
   "Not covered".

## The diagnosis (probe output, verbatim)

`probe_lazy_whisper.py` (this directory), on the speech-densest 40 s of the session's own
`ember_voice.wav`, same model/settings as production
(`Systran/faster-distil-whisper-large-v3`, cpu, int8):

```
speech-densest 40s slice starts at 1516s (mean RMS 1882)
model load: 2.74s
LAZY : call  0.035s, iterate 21.203s, max loop stall 21.238s
       text: You take a deep breath and decide to trust the moonlight to guide you home...
EAGER: to_thread 22.754s total, max loop stall  0.055s
       text: You take a deep breath and decide to trust the moonlight to guide you home...
```

faster-whisper's `transcribe()` is lazy; pipecat threads only the call and iterates on the
event loop. The whole decode (~0.5× realtime here) froze the loop — which is (a) the 25–59 s
transcript lag, (b) why `transcription_lag_secs` could never be observed non-zero, (c) why
`speak()` still stalled despite Task E, and (d) **Task C's answer**: the ~24 s per-turn mystery
lag is Whisper decode, not `LocalSmartTurnAnalyzerV3` — the spec's conditional follow-up
(dropping the turn-analyzer strategy) is not triggered.

The `turn_started_at` corruption is the 5 s `user_turn_stop_timeout` watchdog
(`pipecat/turns/user_turn_controller.py`) force-closing textless turns long before batch
Whisper delivers, then `TranscriptionUserTurnStartStrategy` re-opening a turn at
transcript-arrival time. The force-closed turn's empty `UserTurnStoppedMessage` is swallowed
by the `if message.content:` gate — independent live confirmation of Task F's F2 hole.

## Fixes landed (commit `861cf3e`)

* `EagerSegmentsWhisperModel` materializes segments inside pipecat's `to_thread` (D8);
  wired via `_load()` override; MLX path checked — already eager (dict), not wrapped.
* `user_turn_stop_timeout` 5 s → `TURN_STOP_TIMEOUT_SECS` 90 s (D9).
* `listen()` docstring documents the ~60 s client-cap ceiling (restart needed to serve it).
* Tests: `test_eager_model_decodes_inside_the_transcribe_call`,
  `test_whisper_model_is_wrapped_eager`, `test_turn_stop_timeout_outlives_batch_stt`.
  Suite 44 → 47 passing; ruff/format clean; pyright at the 2-error baseline.

## Not covered / deferred

* **Round 2 must verify live**: transcript lag drops to ~decode time with the loop free;
  `transcription_lag_secs` reads non-zero mid-decode; `turn_started_at` ≈ acoustic start;
  `speak()` start latency ≈ Kokoro synthesis only.
* **Metrics outage quarantine and 0.0-duration gap noise** — deferred; the zero-gaps are
  Kokoro chunk-split artefacts Task G removes at the source. Revisit after G.
* **`wait_for_turn=True` returns plain `{queued: true}`** — indistinguishable from the
  ungated path. Ergonomics; queued for Task H's docstring/return pass.
* **App-side bugs** (mic-toggle race, bot idle-timeout wedge, 57–116 s `enumerateDevices`)
  recorded for a later fix in the app repo; testers work around them.
