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
| **F** | Transcript-loss holes closed (drain STT before writing artefacts) | — | — | ⬜ |
| **G** | Kokoro plays one utterance as one turn (no mid-utterance silence) | — | — | ⬜ |
| **H** | `listen()` batches time-ordered; docstrings state what timestamps mean | — | — | ⬜ |

Live-only (🔴) acceptance stories across all tasks need a running voice app on
`localhost:3000` and are collected in the execution spec; they are the checklist for the next
dogfood session, not part of any task's ✅.

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
