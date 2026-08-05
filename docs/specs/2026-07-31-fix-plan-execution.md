# Execution spec — audio path and reporting fixes

*2026-07-31. Turns [2026-07-29-audio-path-and-reporting-fixes.md](2026-07-29-audio-path-and-reporting-fixes.md)
into executable tasks. Each task below is one commit, one subagent, one set of pass/fail criteria.
Root-cause evidence lives in [2026-07-29-field-report-triage.md](2026-07-29-field-report-triage.md).*

Branch: `fix/audio-path-and-reporting` (off `docs/field-report-triage`).

## How to read this

Every task has:

* **Success criteria** — mechanically checkable. A task is done when all of them hold.
* **Test user stories** — Given/When/Then, written from the point of view of *the agent driving
  voicebox*, because that is who the bugs hurt. Each story names the test that proves it.
* **Live-only** stories are marked 🔴. They need a running voice app on `localhost:3000` and cannot
  be automated here; they are the acceptance list for the next dogfood session.

Universal criteria, every task:

* `uv run pytest -q` green (baseline at branch start: **14 passed**; the count only grows).
* `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/` clean.
* `uv run pyright src/` reports no *new* errors vs. the pre-task baseline.
* One commit, scoped to that task. No unrelated edits, no drive-by refactors.

## Ordering and file ownership

`agent.py` is touched by five tasks, so those run **serially**. Tasks A and B touch nothing the
chain touches and run first, in parallel.

| Wave | Task | Phase | Owns |
|---|---|---|---|
| 1 (parallel) | **A** | 6 | `browser_session.py`, `server.py` (`start_browser_session` docstring), `CLAUDE.md` |
| 1 (parallel) | **B** | 5.1–5.3 | `metrics.py`, `tests/test_metrics.py` |
| 2 (serial) | **C** | 0 | `agent.py` (timing logs only) |
| 2 (serial) | **D** | 1 | `agent.py` (VAD placement) |
| 2 (serial) | **E** | 2 | `processors/nonblocking_whisper_stt.py` (new), `agent.py`, `server.py` (deadline) |
| 2 (serial) | **F** | 3 | `agent.py` (drain + transcript gate) |
| 2 (serial) | **G** | 4 | `processors/kokoro_tts.py`, `agent.py` (`_Playout`) |
| 2 (serial) | **H** | 5.4 | `agent.py` (`listen_events` sort), `events.py`, `server.py` docstrings |

---

## Task A — Session startup fails loudly; attach stops destroying the session

*Phase 6. Fixes field-report item 11.*

Today `page.goto` failure is caught, logged, and followed by `ready_event.set()`
(`browser_session.py:181-187`), so `start_browser_session` returns a success payload for a browser
sitting on `about:blank`. Separately, `attach_hint` (`browser_session.py:82`) tells the caller to run
bare `playwright-cli`, whose `open` subcommand navigates the shim tab to `about:blank` — killing the
audio shim on the tab voicebox owns.

### Success criteria

1. A failed `page.goto` propagates to the parent: `start_browser_session` **raises**, with the child's
   error text in the message. It never returns a payload for a page that did not navigate.
2. After a successful `goto`, the child polls until *both* the page URL is non-`about:blank` and
   `window.__voiceShim.installed === true`, with a bounded timeout (10 s). Expiry raises.
3. `attach_hint` is `playwright-cli attach --cdp <cdp_endpoint>`. The `close-all` + two-env-var
   recipe is gone from `attach_hint`, from `start_browser_session`'s docstring, and from `CLAUDE.md`'s
   "Driving the UI from another agent" section.
4. `playwright_mcp_env` is removed too, or kept only if something still reads it — grep first.
5. The "**Do not open new tabs**" warning survives in `CLAUDE.md`, unchanged in meaning.
6. Teardown still works when startup raises: no orphaned Chromium process.

### Test user stories

**A1 — I find out immediately when my app is not running.**
Given the URL I passed refuses connections,
When I call `start_browser_session`,
Then it raises an error naming the navigation failure — rather than returning a `cdp_endpoint` that
leads to a blank tab I will waste ten minutes debugging.
→ `tests/test_browser_session.py::test_navigation_failure_propagates` (point at
`http://localhost:1/` — a port nothing binds).

**A2 — the hint I am given does not destroy the session.**
Given a started session,
When I read `attach_hint`,
Then it contains `attach --cdp` and contains neither `close-all` nor `PLAYWRIGHT_MCP_ISOLATED`.
→ `tests/test_browser_session.py::test_attach_hint_does_not_navigate` (assert on the returned dict;
mock or monkeypatch the process start — this must not launch a browser).

**A3 🔴 — the shim is alive when the tool returns.**
Given a real app on `localhost:3000`,
When `start_browser_session` returns,
Then `window.__voiceShim.installed` is already `true` — I never have to poll for it myself.

**A4 🔴 — attaching leaves the session intact.**
Given a started session, When I run `playwright-cli attach --cdp <endpoint>` then `tab-select 1` and
`snapshot`, Then `listen()` shows `client_connected` with no subsequent `client_disconnected`.

---

## Task B — `metrics.json` reconciles with itself

*Phase 5.1–5.3. Fixes field-report items 7, 8, 9.*

`_turns()` (`metrics.py:183-207`) builds turns from **transcript** events while `utterances`
(`metrics.py:59`) counts **speech intervals**, so the two disagree whenever a transcript is missing —
and `response_latency_secs` is attached to the transcript, so a missing transcript silently drops a
measured latency. `_gaps()` (`metrics.py:210-221`) merges all intervals with no notion of who owed
the next turn, so the tester's own thinking time is reported as the app's dead air.

`metrics.py` is a pure function over event dicts — all of this is unit-testable, no browser needed.

### Success criteria

1. `_turns()` is built from **speech intervals**, not transcripts. Every interval yields exactly one
   turn. A turn with no matching transcript carries `"transcript_missing": true` and no `text` key.
2. `len([t for t in turns if t["speaker"] == "app_bot"]) == utterances["app_bot"]` holds **by
   construction**; same for `tester`. This is asserted against both captured session logs.
3. `response_latency_secs` is attached to the app-bot **turn**, so it survives a missing transcript.
4. Gaps are attributed by who owed the next turn: a gap following a **tester** interval is app dead
   air; a gap following an **app** interval is tester think time. Reported as separate fields.
   `total_dead_air_secs` keeps its current meaning — app dead air only — and its value therefore
   changes; that is the fix, not a regression.
5. A `biases` note is added naming exactly what the new gap fields do and do not mean.
6. The existing 14 tests are updated where the new semantics change an expected value, and each such
   change is justified in the test's comment. No test is deleted to make a failure go away.

### Test user stories

**B1 — every utterance I heard appears in the report.**
Given a session where the app bot spoke four times but Whisper returned text for only three,
When I read `metrics.json`,
Then `turns` has four `app_bot` rows — one flagged `transcript_missing` — and that count equals
`utterances.app_bot`.
→ `test_turns_count_matches_utterances`, plus the same assertion over both files in
`docs/artefacts/`… (see criterion 2).

**B2 — a latency I measured is not lost with the text.**
Given the app bot replied 3.754 s after the tester stopped, and its transcript never arrived,
When I read `turns`,
Then the app-bot turn still carries `response_latency_secs: 3.754`.
→ `test_latency_survives_missing_transcript`.

**B3 — dead air means the app was slow, not that I was thinking.**
Given the tester waited 12 s before speaking again after the app finished,
When I read the report,
Then that 12 s is counted as tester think time, **not** as app dead air, and
`total_dead_air_secs` excludes it.
→ `test_gap_after_app_is_tester_think_time` and `test_gap_after_tester_is_app_dead_air`.

**B4 — the report tells me what it cannot know.**
Given any session, When I read `session.biases.notes`, Then a note explains the split gap semantics.
→ `test_biases_note_covers_gap_attribution`.

---

## Task C — Phase 0 instrumentation (timing only, no behaviour change)

*Phase 0. The one thing the triage could not attribute.*

Session 2 showed a steady **~24 s per-turn transcript lag**, larger than warm Whisper throughput
(0.40× realtime) accounts for. `LocalSmartTurnAnalyzerV3` (`agent.py:299`) is the suspect. It is a
hypothesis, and the fix plan says: measure before assuming.

This task lands the measurement so the next live session attributes it. It changes **no behaviour** —
if it does, the task is wrong.

### Success criteria

1. Wall-clock duration is logged at `DEBUG` for each of: `run_stt`, the turn analyzer's
   `analyze_end_of_turn`, and the `on_user_turn_stopped` handler.
2. Each log line is greppable by a single stable prefix (e.g. `voicebox.timing`) and carries the
   measured seconds as a parseable number.
3. At the default log level, output is unchanged. No new frames, no new awaits on the hot path, no
   ordering change.
4. The wrapping is done without vendoring pipecat code — subclass or wrap, do not copy.

### Test user stories

**C1 — turning on debug logging tells me where the seconds went.**
Given a session run at `DEBUG`,
When I grep the log for `voicebox.timing`,
Then I get one line per STT call and per turn-analyzer call, each with a duration I can sum.
→ `tests/test_timing_instrumentation.py::test_timing_lines_emitted` (drive a stub STT through a
pipeline with `caplog` at DEBUG; assert the prefix and a float appear).

**C2 — instrumentation costs me nothing when I don't ask for it.**
Given the default log level, When a session runs, Then no `voicebox.timing` line is emitted.
→ `test_timing_silent_at_default_level`.

**C3 🔴 — the ~24 s is attributed to a named call.** One live session against a talkative app; read
the split. *This is the deliverable that unblocks the conditional follow-up in Task D.*

---

## Task D — Move the VAD upstream of the STT

*Phase 1. Fixes field-report items 2 and 3 — the top-severity pair.*

The VAD is a parameter of the **user aggregator** (`agent.py:305`), which sits **downstream of the
STT** in the pipeline (`agent.py:318-325`). So `VADUserStartedSpeakingFrame` cannot reach the STT
until the audio it describes has already passed through it. While `run_stt` blocks the STT's frame
task, incoming audio queues; when it drains, `SegmentedSTTService` appends it with `_user_speaking`
still `False` and trims the buffer to the last second on every frame
(`pipecat/services/stt_service.py:804-807`).

Measured in `docs/artefacts/field-report-triage/probe_audio_trim.py`: **10 s of speech in, 1 s
reached Whisper — 90 % discarded.** `probe_vad_upstream.py` measures **0 %** with the VAD upstream,
because `FrameProcessorQueue` gives `SystemFrame`s priority.

This is why transcripts start mid-sentence, why a 21 s utterance measured 5.1 s, and why Whisper
hallucinated "Thank you." — it was handed a one-second stub.

### Success criteria

1. `WebsocketServerParams` (`agent.py:709-716`, in `create_agent`) carries
   `vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_SECS))`.
2. `vad_analyzer=` is removed from `LLMUserAggregatorParams` (`agent.py:305`).
3. `VAD_STOP_SECS` remains a single constant read by both the transport and the
   `session_started` event header. No duplicated literal.
4. `_PipelineEventObserver` is **unchanged** — it matches frame types, not producers, and both VAD
   frames still cross the pipeline downstream. If it needs changing, stop and report why.
5. The comment currently at `agent.py:302-304` explaining *why* 1.0 s (not pipecat's 0.2 s default)
   moves with the parameter. That rationale must not be lost.

### Test user stories

**D1 — the app bot's words reach me whole.**
Given the STT is busy transcribing a previous segment for 4 s,
And 10 s of app-bot speech arrives during that stall,
When the STT finally runs,
Then it is handed all 10 s — not the last 1 s.
→ `tests/test_vad_placement.py::test_audio_survives_stalled_stt`. Promote
`docs/artefacts/field-report-triage/probe_vad_upstream.py` — it already demonstrates this. Assert
`>= 9.5` seconds reached `run_stt`. **Add the negative control** (`probe_audio_trim.py`'s topology,
VAD downstream) as a second test asserting the loss, so the test proves the *placement* is what
matters and will fail loudly if someone moves it back.

**D2 — the VAD config did not silently change.**
Given a built agent, When I inspect the transport params, Then `vad_analyzer` is present with
`stop_secs == VAD_STOP_SECS == 1.0`, and the aggregator has none.
→ `test_vad_lives_on_transport_not_aggregator`.

**D3 🔴 — reported speech duration matches reality.**
`app_bot_speech_stopped - app_bot_speech_started` matches the app's own logged speech duration to
within `vad_stop_secs`, and transcripts start at sentence boundaries.

**Watch for:** VAD timestamps become acoustically true rather than post-STT, so every
`app_bot_speech_*` value shifts earlier. That is the point, but it moves every number in
`metrics.json` — land this before Task G.

---

## Task E — Take transcription off the frame path

*Phase 2. Fixes field-report items 5, 6 and 10.*

`SegmentedSTTService._handle_user_stopped_speaking` awaits transcription inline
(`pipecat/services/stt_service.py:780`), reached from `FrameProcessor.__input_frame_task_handler`
(`frame_processor.py:1014-1015`) — the same task that handles system frames. **Nothing gets past the
STT while Whisper runs.** Measured: a 5 s stall delayed a queued `speak()` frame triplet by 4.7 s.
Live, this made a `speak()` play **51 s late** and the caller believe it had deadlocked.

Note what this is *not*: Whisper CPU int8 runs at **0.40× realtime warm** (2.5× faster than
realtime). Raw throughput is fine. The `device="cpu"` / `compute_type="int8"` pin
(`agent.py:685-689`) **stays** — it is not the problem, and changing it is out of scope.

### Success criteria

1. New `src/voicebox/processors/nonblocking_whisper_stt.py` overrides
   `_handle_user_stopped_speaking` to enqueue the segment and return immediately.
2. **One** background worker, not a pool: segments must stay in order, and two concurrent Whisper
   runs would contend for the same CPU.
3. Transcription frames are pushed from the worker in segment order.
4. The worker is started and stopped with the processor's lifecycle; teardown does not leak a task
   or hang.
5. Wired in `_create_stt_service` (`agent.py:676-689`) for the non-Darwin path. Whether the Darwin
   MLX path gets the same treatment is the subagent's call — state the decision in the commit
   message either way.
6. `PLAYOUT_TIMEOUT_SECS` drops from 120 s to 30 s, and on expiry `speak(wait_for_playout=True)`
   returns `{"queued": True, "played": False, "reason": <str>}` instead of raising. `server.py`'s
   `deadline = 150.0` drops in step so the MCP layer outlives the agent-side timeout, not the reverse.
7. `listen()`'s response envelope gains `transcription_lag_secs` (or equivalent) so a caller can
   distinguish "still transcribing" from "nothing happened". Adding a key is backward-compatible;
   removing or renaming `events`/`cursor` is not — don't.

### Test user stories

**E1 — my speech starts when I ask for it, not when Whisper finishes.**
Given the STT is mid-transcription of a 5 s segment,
When I call `speak("hello")`,
Then the audio frames reach the transport within ~100 ms — not after the transcription completes.
→ `tests/test_nonblocking_stt.py::test_speak_not_blocked_by_transcription`. Promote
`probe_stt_blocking.py`; assert the downstream processor sees the queued frame in `< 0.5 s` while a
4 s `run_stt` is in flight.

**E2 — a hung playout gives me a diagnosis, not an exception.**
Given audio that never plays out,
When I call `speak(wait_for_playout=True)`,
Then after ~30 s I get `{"queued": true, "played": false, "reason": ...}` and can decide what to do —
rather than an exception that tells me nothing about which half failed.
→ `test_playout_timeout_returns_diagnostic` (monkeypatch the timeout down; assert the shape, and
assert **no** exception).

**E3 — I can tell "slow" from "broken".**
Given three segments queued for transcription,
When I call `listen()`,
Then the response reports a non-zero transcription lag, so I know to wait rather than conclude the
app said nothing.
→ `test_listen_reports_transcription_backlog`.

**E4 — transcripts still arrive in the order they were spoken.**
Given segments A then B queued while the worker is busy,
When both transcribe,
Then A's transcript is pushed before B's.
→ `test_transcripts_preserve_segment_order`.

**E5 🔴 — barge-in timing is real.** Issue `speak()` while the app bot is mid-sentence; audio starts
within ~2 s (Kokoro synthesis), not after the transcript lands.

---

## Task F — Close the transcript-loss holes

*Phase 3. Fixes field-report item 1.*

Session 1 lost a **44.8 s** app-bot turn entirely. The cause is teardown ordering, not the gate the
field report blamed: `stop()` writes `events.json` and `metrics.json` at `agent.py:426` and only
*then* sends `EndFrame` at `agent.py:431`, so any transcription still in flight never reaches the
artefacts. Two independent holes, both worth closing.

### Success criteria

1. `stop()` awaits the STT worker's queue to drain — bounded (~15 s) — **before** `_dump_artifacts`.
   On expiry it logs and proceeds; it must not hang teardown forever.
2. The `if message.content:` gate (`agent.py:359`) no longer swallows an empty result. The event is
   emitted either way, carrying `transcription_empty: true` when the text is blank, so the gap is
   visible in `events.json` and reconcilable against `metrics.json`.
3. `metrics.py` tolerates a transcript event with empty text — it must not count as a real transcript
   in Task B's turn matching. Coordinate with Task B's shape; do not duplicate its logic.
4. Ordering inside `stop()` is otherwise untouched: `SESSION_STOPPED` is still emitted first so a
   pending `listen_events()` returns cleanly (the comment at `agent.py:411-412` explains why).

### Test user stories

**F1 — nothing I heard is missing from the artefacts.**
Given the app bot's last turn is still being transcribed,
When I call `stop()`,
Then that transcript is in `events.json` — the report is complete, not silently 44 s short.
→ `tests/test_stop_drains_stt.py::test_pending_transcript_reaches_artifacts`.

**F2 — an empty transcript is visible, not invisible.**
Given Whisper returns `""` for one segment,
When I read the event log,
Then there is an `app_bot_transcript` event flagged `transcription_empty: true` — I can see that
voicebox tried and got nothing, instead of guessing whether the bot spoke.
→ `test_empty_transcription_still_emits_event`.

**F3 — teardown cannot hang forever.**
Given a transcription that never completes,
When I call `stop()`,
Then it returns within the bounded drain window and still writes artefacts.
→ `test_stop_bounded_when_drain_stalls`.

---

## Task G — Kokoro plays one utterance, not several

*Phase 4. Fixes field-report item 4 — both halves.*

`run_tts` yields each chunk as `create_stream` produces it (`kokoro_tts.py:171-181`). The CPU
synthesis gap between chunks becomes **real silence in the synthetic microphone** — an observed
**1.634 s** of it. pipecat's output transport declares the bot stopped after
`BOT_VAD_STOP_SECS = 0.35` of outgoing silence.

So two things happened at once and the field report caught only the first: `PLAYOUT_SETTLE_SECS`
expired during the gap and `wait_for_playout` resolved early (a reporting bug), **and the app under
test genuinely heard two user turns** (not a reporting bug — it changes the app's behaviour and
invalidates the run).

### Success criteria

1. `run_tts` buffers the whole utterance before yielding audio. Time-to-first-byte is irrelevant for
   a synthetic tester — nobody is waiting for a natural-sounding response — and gap-free playout is
   the entire point.
2. `TTSStartedFrame` / `TTSStoppedFrame` still bracket the audio, and the error path still yields
   `ErrorFrame` without leaving `TTSStoppedFrame` unsent (the current `finally` guarantees this —
   keep that guarantee).
3. `_Playout` resolves on the first `BotStoppedSpeakingFrame` following the utterance's
   `TTSStoppedFrame`, not on a silence timer.
4. `PLAYOUT_SETTLE_SECS` (`agent.py:99`) and the settle-timer machinery in `_Playout`
   (`agent.py:154, 160-169, 180-182`) are **deleted**, not left dead. Their docstrings go too.
5. Interruption still resolves the playout immediately (`_Playout.on_interrupted`).

### Test user stories

**G1 — one utterance is one turn.**
Given I speak a three-sentence sentence,
When I read the event log,
Then there is exactly **one** `tester_speech_started` / `tester_speech_stopped` pair — because the app
under test must hear one user turn, not three, or my whole run is invalid.
→ `tests/test_kokoro_playout.py::test_utterance_yields_single_audio_span` (assert `run_tts` emits its
audio with no synthesis gap between frames — e.g. all audio frames are produced after the stream is
fully consumed).

**G2 — `wait_for_playout` returns when the audio actually finished.**
Given a multi-segment utterance,
When I call `speak(wait_for_playout=True)`,
Then `finished_at - started_at` covers the whole utterance, and the call did not return at the first
segment boundary.
→ `test_playout_resolves_on_bot_stopped_after_tts_stopped`.

**G3 — barge-in still cuts me off promptly.**
Given a playout in flight, When an `InterruptionFrame` arrives, Then the call returns immediately with
`interrupted: true`.
→ `test_interruption_resolves_immediately`.

**G4 🔴 — the app logs one user turn, not two.**

---

## Task H — Ordered `listen()` batches, honest docstrings

*Phase 5.4. Fixes field-report item 6.*

`app_bot_speech_*` events take `t` from `frame.timestamp` — set when the VAD **constructs** the frame
— while log position is when the observer **saw** it. Any stall between the two reorders the log; a
5 s stall was measured to separate them by 4.5 s.

Tasks D and E shrink that skew from tens of seconds to milliseconds, which is the real fix. This is
belt-and-braces, and deliberately the weak form: sorting a *batch* cannot reorder across a cursor
boundary.

### Success criteria

1. `listen_events` (`agent.py:536`) sorts the returned slice by `t`. **Append order stays
   authoritative for the cursor** — `cursor + len(events)` semantics are untouched, and no event is
   ever skipped or repeated.
2. The sort is stable, so equal-`t` events keep their append order.
3. Docstrings state plainly that (a) the returned batch is time-ordered but late events can still land
   after a cursor has moved on, and (b) `tester_transcript` is stamped at `speak()` time by design —
   it is ground truth, not an STT result (`events.py:85-94`).

### Test user stories

**H1 — the transcript I read is in the order it happened.**
Given events appended out of `t` order,
When I call `listen()`,
Then the batch is sorted by `t` and I can read it as a conversation.
→ `tests/test_listen_ordering.py::test_batch_sorted_by_t`.

**H2 — sorting never loses an event.**
Given a session with out-of-order events, When I page through with successive cursors,
Then the concatenation of all batches contains every event exactly once.
→ `test_cursor_paging_lossless_under_sort`.

**H3 — the docs tell me what the timestamps mean.**
Given I am reading `tester_transcript.t`, Then the docstring tells me it is the `speak()` call time,
so I do not mistake it for a playout or STT timestamp.
→ reviewer check, not a test.

---

## Out of scope — recorded, not done

* **Dropping `LLMContextAggregatorPair` + `LocalSmartTurnAnalyzerV3` entirely.** They exist to decide
  *when a human has finished their turn so an LLM should reply*. voicebox has no LLM in the loop and
  replies only when Claude calls `speak()`. A dedicated observer processor would remove the
  aggregator, the turn strategies, and the party-name inversion `agent.py:16-20` has to explain.
  Worth a separate design once Tasks D–H show which of that machinery was load-bearing.
* **Conditional on Task C's live result:** if the ~24 s is attributed to smart-turn inference, drop
  `TurnAnalyzerUserTurnStopStrategy` in favour of plain VAD endpointing. Not done speculatively.
* `device="cpu"` / `compute_type="int8"` — measured at 0.40× realtime warm. The pin is right.
* `enable_interruptions=False` — correct for a synthetic user that must be able to talk over the bot.
