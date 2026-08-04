# Walkthrough — `fix/audio-path-and-reporting`

*Status: **in progress**. Started 2026-07-29, walkthrough opened 2026-08-01 (see `BUILDLOG.md` D1).
Branched from `eb89647`.*

Fixes the audio-path and reporting defects found in a dogfood session. Root causes:
[`docs/specs/2026-07-29-field-report-triage.md`](../specs/2026-07-29-field-report-triage.md).
Design: [`docs/specs/2026-07-29-audio-path-and-reporting-fixes.md`](../specs/2026-07-29-audio-path-and-reporting-fixes.md).
Task breakdown and success criteria:
[`docs/specs/2026-07-31-fix-plan-execution.md`](../specs/2026-07-31-fix-plan-execution.md).

## Status

| Task | What it fixes | Commits | Evidence | Done |
|---|---|---|---|---|
| **A** | Session startup fails loudly; `attach_hint` stops destroying the shim tab | `1df7e51` | [t-a-startup-fails-loudly.md](../artefacts/fix-audio-path-and-reporting/t-a-startup-fails-loudly.md) | ✅ |
| **B** | `metrics.json` reconciles with itself (turns from intervals, split gap attribution) | `df7f353` | [t-b-metrics-reconcile.md](../artefacts/fix-audio-path-and-reporting/t-b-metrics-reconcile.md) | ✅ |
| **C** | Phase-0 timing instrumentation; attributes the unexplained ~24 s per-turn lag | `11090da` | [t-c-timing-instrumentation.md](../artefacts/fix-audio-path-and-reporting/t-c-timing-instrumentation.md) | ✅ |
| **D** | VAD moves upstream of the STT (90 % of speech was trimmed before Whisper) | `ad93dd4` | [t-d-vad-upstream.md](../artefacts/fix-audio-path-and-reporting/t-d-vad-upstream.md) | ✅ |
| **E** | Transcription leaves the frame path (non-blocking Whisper worker) | `32b1d6c` | [t-e-nonblocking-stt.md](../artefacts/fix-audio-path-and-reporting/t-e-nonblocking-stt.md) | ✅ |
| **F** | Transcript-loss holes closed (drain STT before writing artefacts) | `5d67578` | [t-f-transcript-loss-holes.md](../artefacts/fix-audio-path-and-reporting/t-f-transcript-loss-holes.md) | ✅ |
| **G** | Kokoro plays one utterance as one turn (no mid-utterance silence) | `57d9527` | [t-g-kokoro-single-turn.md](../artefacts/fix-audio-path-and-reporting/t-g-kokoro-single-turn.md) | ✅ |
| **H** | `listen()` batches time-ordered; docstrings state what timestamps mean | `61aa484` | [t-h-listen-ordering-docstrings.md](../artefacts/fix-audio-path-and-reporting/t-h-listen-ordering-docstrings.md) | ✅ |

Live-only (🔴) acceptance stories across all tasks need a running voice app on
`localhost:3000` and are collected in the execution spec; they are the checklist for the next
dogfood session, not part of any task's ✅.

## Blind verification rounds (live, between Task E and Task F)

Fresh, spec-blind tester agents drive the MCP tools against EmberTales; each round's findings
are diagnosed, fixed and committed before the next.

| Round | Fix commits | Evidence | Done |
|---|---|---|---|
| **1** | `f1bd16c` (D7), `861cf3e` (D8+D9: eager Whisper decode, 90 s turn-stop watchdog) | [r1-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r1-blind-verification.md) | ✅ |
| **2** | `2d7646d` (D10+D11: turn starts stamped from own VAD log, lag-sampling settle) | [r2-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r2-blind-verification.md) | ✅ |
| **3** (stretched scenario) | `63dd58d` (D12: outage gaps quarantined in metrics) | [r3-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r3-blind-verification.md) | ✅ |
| **4** (post-F+G) | `f90c358` (D15: stop deadline 210 s, watchdog 240 s, per-session debug log) | [r4-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r4-blind-verification.md) | ✅ |
| **5** (F1 prompt-stop live: PASS) | `69a716f` (D16: Kokoro warm-up, debug log in artifacts) | [r5-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r5-blind-verification.md) | ✅ |
| **6** (multi-sentence stress) | `248446c` (D17: TOKEN aggregation — one speak() = one synthesis) | [r6-blind-verification.md](../artefacts/fix-audio-path-and-reporting/r6-blind-verification.md) | ✅ |

Round 1 confirmed A3/A4 and D3 live, confirmed the specced F and G holes, answered **C3**
(the ~24 s lag is Whisper's lazy decode freezing the event loop — not smart-turn inference,
so the conditional follow-up stays off), and surfaced the 5 s turn-stop watchdog corrupting
`turn_started_at`. Details and probe output in the evidence artefact.

## Try it

```bash
uv run pytest -q                     # whole suite
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run pyright src/
```

---

## Task A — startup fails loudly; attach no longer destroys the session

*Commit `1df7e51`. Criteria: execution spec § "Task A".*

- `page.goto` failure now propagates to the parent instead of being caught, logged and followed
  by `ready_event.set()` — `start_browser_session` raises with the child's error text rather than
  returning a `cdp_endpoint` pointing at `about:blank`
  (`src/voicebox/browser_session.py`).
- After a successful `goto` the child polls until the URL is non-`about:blank` **and**
  `window.__voiceShim.installed === true`, bounded at 10 s; expiry raises.
- `attach_hint` is now `playwright-cli attach --cdp <endpoint>`. The old `close-all` +
  two-env-var recipe is gone from the hint, from `start_browser_session`'s docstring and from
  `CLAUDE.md`. `playwright-cli open` navigates the current page to `about:blank`, which over CDP
  is voicebox's shim tab — the hint was telling callers to destroy their own audio path.
- The "**Do not open new tabs**" warning survives in `CLAUDE.md`, unchanged in meaning.

**Not covered:** stories A3 and A4 are 🔴 live-only — that the shim is already installed when the
tool returns, and that a real `playwright-cli attach` + `snapshot` leaves the session connected.
Both are asserted by construction (the poll, the hint text) but unproven against a real app.

---

## Task B — `metrics.json` reconciles with itself

*Commit `df7f353`. Criteria: execution spec § "Task B". Decision: `BUILDLOG.md` D2.*

- `_turns()` (`metrics.py:262-289`) is built from **speech intervals**, not transcripts, so the
  turn count equals `utterances` by construction. A turn nothing transcribed carries
  `transcript_missing: true` and no `text` key.
- `response_latency_secs` now rides on the app-bot **turn** rather than on its transcript, so a
  measured latency survives when the text never arrives.
- `_gaps()` (`metrics.py:292-318`) splits silence by who owed the next turn: after a tester
  utterance it is **app dead air**, after an app utterance it is **tester think time**.
  `total_dead_air_secs` keeps its name and means app dead air only — so its value changes.
- Empty transcripts (`text: ""`) no longer count as transcripts for matching
  (`_spoken_transcripts`, `metrics.py:200-207`); the interval is reported as `transcript_missing`.

**What it was reporting before**, on the two captured dogfood logs — same input, old code:

| | session 1 | session 2 |
|---|---|---|
| app-bot turns / utterances **before** | 2 / 3 | 8 / 11 |
| app-bot turns / utterances **after** | 3 / 3 | 11 / 11 |
| `total_dead_air_secs` **before** | 98.123 | 302.276 |
| **after** (app dead air + tester think time) | 3.754 + 94.369 | 11.407 + 290.869 |

The old dead-air figure read as "the app under test was silent for 98 seconds". Nearly all of it
was the driving agent thinking between `speak()` calls. The new fields sum to the old number
exactly, so this is a re-attribution of the same silence.

**Not covered:** matching is positional, not semantic — a dropped transcript shifts text onto a
neighbouring turn (pinned by `test_dropped_transcript_shifts_text_onto_the_next_turn`, reasoning
in D2). Every number here still carries the ~1 s/turn VAD bias that Task D will change. Nothing
is live-verified — these are replays of captured logs. Full list in the evidence artefact.

---

## Task C — Phase 0 timing instrumentation

*Commit `11090da`. Criteria: execution spec § "Task C". No behaviour change by design.*

- `src/voicebox/timing.py` adds `log_duration()` plus two mixins — `TimedSTTMixin` (times
  `run_stt`) and `TimedTurnAnalyzerMixin` (times `analyze_end_of_turn`). They list **first** in the
  bases so they precede the concrete service in the MRO, and delegate via `super()`, so the same
  mixin composes with a subclass of a concrete service without changing.
- `agent.py` builds the pipeline from `_TimedWhisperSTTService`, `_TimedWhisperSTTServiceMLX` and
  `_TimedSmartTurnAnalyzer` (`agent.py:103-116`), and wraps `on_user_turn_stopped` in
  `log_duration` (`agent.py:376`). Nothing is vendored from pipecat.
- Every line is `voicebox.timing name=<call> secs=<float>` at DEBUG — one grep splits a session
  log by call.

**Why it exists:** a dogfood session showed a steady ~24 s per-turn transcript lag, larger than
warm Whisper throughput (0.40× realtime) accounts for. `LocalSmartTurnAnalyzerV3` is the suspect,
but that is a hypothesis — this task lands the measurement so the next live session attributes it
by name instead of guessing.

**Not covered:** C3 🔴, the live session that actually attributes the 24 s, has not run. Until it
does, the Task D follow-up (dropping `TurnAnalyzerUserTurnStopStrategy`) stays unjustified.
`on_user_turn_stopped` is instrumented but untested. Full list in the evidence artefact.

---

## Task D — the VAD moves upstream of the STT

*Commit `ad93dd4`. Criteria: execution spec § "Task D". Criterion 1 adapted — see BUILDLOG D3.*

- A `VADProcessor` stage now sits between `transport.input()` and the STT
  (`agent.py:_build_stages`); the aggregator no longer carries a `vad_analyzer`. The spec asked
  for the analyzer on `WebsocketServerParams`, but pipecat 1.3.0 has no such field — the VAD is
  a pipeline processor in this version. Same intent, only place it can live.
- **Why it matters:** `SegmentedSTTService` trims its buffer to the last second on every audio
  frame that arrives while it believes the user is silent
  (`pipecat/services/stt_service.py:805-807`), and `_user_speaking` flips only when the STT
  *receives* a `VADUserStartedSpeakingFrame`. With the VAD downstream that frame could not
  arrive until the audio it describes had already been trimmed away.
- Measured on the same 10 s flood through both topologies: **10.20 s reaches `run_stt` with the
  VAD upstream, 1.50 s with it downstream** — 85 % of the app bot's speech was being thrown away
  before Whisper ever saw it. This is why transcripts started mid-sentence, why a 21 s utterance
  measured 5.1 s, and why Whisper hallucinated "Thank you." from a one-second stub.
- `tests/test_vad_placement.py` keeps a positive test and a **negative control**, so moving the
  stage back fails the suite rather than silently regressing.
- Stage assembly and two config factories moved out of `start()` into `_build_stages`,
  `_create_vad_processor` and `_create_context_aggregators` — the ordering and the 1.0 s
  `stop_secs` are now assertable without booting a pipeline.

**Watch out:** VAD timestamps are now acoustically true rather than post-STT, so every
`app_bot_speech_*` value shifts earlier and every number in `metrics.json` moves. Reports from
before `ad93dd4` are not comparable with reports after it.

**Not covered:** D3 🔴 — that reported speech duration matches the app's own logs to within
`vad_stop_secs` — needs a live app. Silero itself is not exercised by the placement tests; they
use a deterministic stub analyzer. Full list in the evidence artefact.

---

## Task E — transcription leaves the frame path

*Commit `32b1d6c`. Criteria: execution spec § "Task E". Criterion 1 adapted — see BUILDLOG D5.*

- `SegmentedSTTService` awaited transcription inline, from the same task that carries system
  frames, so **nothing got past the STT while Whisper ran**. With a 4 s transcription in
  flight, a queued `speak()` reached the transport in **3503 ms** before this commit and
  **97 ms** after. Live, that was a `speak()` playing 51 s late and a caller who thought
  voicebox had deadlocked.
- `NonBlockingSegmentedSTT` intercepts `run_stt`: the frame task queues the segment and yields
  nothing; one worker calls the wrapped service's real `run_stt` and pushes from there. One
  worker, never a pool — segments must come back in the order they were spoken, and two Whisper
  runs would only fight over the same CPU.
- **Why not the override the spec named:** `_handle_user_stopped_speaking` also frames the WAV
  segment, so overriding it means copying that framing here — and pipecat 1.6.0 has already
  changed those exact lines (`wants_wav_segments`). The copy would rot silently.
- Both platforms are wired, not just the non-Darwin path: the inline await is in the shared base
  class, and fixing one path would leave the other broken with no test able to see it
  (BUILDLOG D6).
- `speak(wait_for_playout=True)` no longer raises on timeout. It returns
  `{queued: true, played: false, reason: ...}` after 30 s (was 120 s), because an exception said
  nothing about *which* half failed. `listen()` gained `transcription_lag_secs`, so an empty
  `events` list can be told apart from a transcript still in the oven.

**Not covered:** E5 🔴 needs a live app. The MLX path is wired but unexercised on Linux. Nothing
drains the queue at teardown yet — a transcription in flight when `stop()` runs is still lost;
that is Task F, next. Full list in the evidence artefact.

---

## Task F — the transcript-loss holes are closed

*Commit `5d67578`. Criteria: execution spec § "Task F". Criterion 1 adapted — BUILDLOG D13.*

- `stop()` drains the STT queue (`NonBlockingSegmentedSTT.drain`) plus a ≤2 s event-log
  settle **before** `_dump_artifacts`, so in-flight transcriptions reach `events.json`. The
  spec's flat ~15 s bound is adapted: round 3 measured a 140 s real backlog, so the budget
  scales with queued audio (15 s base + 1 s per audio second, 180 s cap — D13). A wedged
  Whisper logs a warning and teardown proceeds (F3).
- The `if message.content:` gate is gone: an empty Whisper result emits an
  `app_bot_transcript` event flagged `transcription_empty: true`, and claims its VAD start so
  the D10 turn-start deque cannot drift by one for the rest of the session.
- `metrics.py` needed no change — Task B's `_spoken_transcripts` already refuses empty text
  for matching (criterion 3 held by construction).
- `SESSION_STOPPED` is still emitted first; drained transcripts land after it in the log but
  inside the artifacts (asserted in the F1 test).

**Not covered:** live re-verification of a prompt stop; failed (vs empty) segments still
surface only as a log line; aggregator-merged utterances remain one event. Full list in the
evidence artefact.

---

## Task G — Kokoro plays one utterance, not several

*Commit `57d9527`. Criteria: execution spec § "Task G".*

- `run_tts` consumes the whole synthesis stream before yielding any audio, back-to-back —
  the CPU gap between chunks (1.6–4.2 s live) can no longer become silence in the synthetic
  mic, so the app hears one user turn per `speak()`. Round 3's worst case (Ember endpointed
  on a 4.2 s gap, took the turn, and scolded the tester) is the reproduction this kills.
- `_Playout` resolves on the first `BotStoppedSpeakingFrame` after the utterance's
  `TTSStoppedFrame` (the observer now watches `TTSStoppedFrame`, no event emitted) — round 2
  caught the old 1.0 s settle timer resolving 4 s before the audio ended.
  `PLAYOUT_SETTLE_SECS` and the settle machinery are deleted. Interruption still resolves
  immediately.
- Side effect at the source: the phantom `transcript_missing` tester turns (3 per session in
  rounds 2–3) disappear — they were the splits' second intervals.

**Not covered:** G4 🔴 (the app logs one user turn) and the speak-latency delta are queued
for the post-G blind round; the barge-in synthesize-at-arm idea stays a recorded follow-up.
Full list in the evidence artefact.

---

## Task H — ordered `listen()` batches, honest docstrings

*Commit `61aa484`. Criteria: execution spec § "Task H". Decision: `BUILDLOG.md` D18.*

- `listen_events` returns each batch **sorted by `t`** (stable — equal-`t` events keep append
  order) while the cursor stays `cursor + len(events)` on the append-ordered log, so paging
  never skips or repeats an event. Deliberately the weak form: a late event whose `t` predates
  an already-read batch still arrives in a later batch; the docstrings say to merge on `t`.
- Docstring pass pays the rounds-1–6 debt: `tester_transcript.t` = `speak()` call time;
  `transcription_lag_secs` 0.0 ≠ "nothing pending" (open-turn blind spot, D11/D14); barge-in
  arming is instant server-side (~1 ms, D17) so arm early, one-shot, no disarm, audio at
  `timer_secs` + synthesis; `stop()`'s drained transcripts land after `session_stopped` (read
  `events.json`).
- Round-1's judged-scope item resolved per **D18**: `speak(wait_for_turn=True)` results now
  carry `waited_for_turn_secs` (how long the silence gate blocked); the ungated shape is
  unchanged, so the key's presence marks the gated path.

**Not covered:** sort exercised on synthetic logs only (post-D/E skew is ms-scale by design);
new `server.py` docstrings reach clients only after the pending server restart; the new key is
not yet observed live — all on round 7's checklist. Full list in the evidence artefact.
