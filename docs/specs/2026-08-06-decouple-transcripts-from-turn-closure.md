# Decouple app-bot transcript delivery from turn closure

*2026-08-06. Follows up the deferred item at the bottom of
[2026-07-29-audio-path-and-reporting-fixes.md](2026-07-29-audio-path-and-reporting-fixes.md)
("Follow-up, not in this plan"), now that Phases 1–5 have shown which of pipecat's
conversational-agent machinery was load-bearing. Answer: for transcript delivery, none of it.*

---

## Read this first — who is who

Two bots share one pipeline, and pipecat's names for them are inverted relative to intuition.
**pipecat labels parties by direction, not identity:**

* **"user"** = whoever's audio comes *in*. In voicebox that is the **app bot** — the voice app under test.
* **"bot" / "assistant"** = whoever the pipeline *speaks as*. In voicebox that is the **tester** — us.

So `user_aggregator` belongs to the **app bot**, and `assistant_aggregator` belongs to the **tester**.
This is recorded at `agent.py:16-20` and in the observer docstring (`agent.py:391-394`).

```
                                                     party served
  speak()  ─ tester text enters the task queue        TESTER    (injected above stage 1)
      │
      ▼
  transport.input()   app bot audio arrives            APP BOT
      │               from the browser tap
      ▼
  vad                 marks app bot speech             APP BOT
      │               start / stop — instant
      ▼
  stt (Whisper)       transcribes the app bot          APP BOT   ◀── transcript EXISTS here
      │               slow; speed varies by machine
      ▼
  user_aggregator     assembles the app bot's turn     APP BOT   ◀── transcript REPORTED here,
      │               ("user" = the app bot)                          when a timer closes the turn
      ▼
  [ LLM ]             DELETED — Claude is the LLM,       —
      │               out of process over MCP
      ▼
  tts (Kokoro)        synthesizes tester speech        TESTER
      │
      ▼
  assistant_aggregator records what the tester said    TESTER
      │                ("assistant" = the tester)
      ▼
  transport.output()  tester audio → browser fake mic  TESTER
```

Stage order is `agent.py:1003-1011`.

**Two consequences of that diagram matter for this spec.**

**1. The LLM stage is gone.** In a normal pipecat app the aggregator accumulates the user's
transcript and hands a context to an LLM, which emits `LLMFullResponseStartFrame` /
`LLMTextFrame` / `LLMFullResponseEndFrame`, which the TTS speaks. voicebox deleted that stage —
Claude is the LLM, over MCP. `speak()` hand-builds the exact frame triplet the LLM would have
emitted (`agent.py:912-917`). **The app bot's aggregator therefore assembles a context that nothing
consumes.** It is vestigial; the only thing voicebox still uses it for is one side-effect, its
`on_user_turn_stopped` event, borrowed as a courier for the app bot's transcript.

**2. Tester frames traverse app-bot stages without being aggregated by them.**
`queue_frames()` enqueues at the pipeline *source*; there is no way to inject partway down. So the
tester's `LLMTextFrame` walks past `vad`, `stt` and `user_aggregator` to reach the TTS. Those stages
inspect it, match nothing, and forward it untouched. **Traversing is not aggregating** — the app
bot's aggregator never accumulates the tester's words. This is why Q3 below had to be checked, and
why its answer is "no gating".

A rendered version of this diagram:
<https://claude.ai/code/artifact/43a32559-22a9-47f8-ad4c-06c2c40d28b2>

---

## The problem

**The app bot's transcript cannot reach `listen()` until the app bot's turn aggregator closes a turn,
and that closure is decided by a timer.** The timer must therefore outlast the slowest Whisper
decode, or the turn closes empty and the real transcript arrives late, mistimed and orphaned.

That timer is `TURN_STOP_TIMEOUT_SECS = 240.0` (`agent.py:126`) against a pipecat default of 5.0.
It is not a tuning choice; it is a bet that no machine decodes slower than 240 s per utterance.
The bet was measured on one Linux CPU box.

**Why this cannot be fixed by picking a better number.** Decode speed varies by OS, CPU/GPU and
Whisper backend — Apple Silicon runs MLX, Linux runs faster-whisper pinned to CPU int8, a CUDA host
could run either. Any constant that must be ≥ worst-case decode time is wrong on hardware we do not
control: too generous on fast machines, still too short on slow ones. Calibrating it per-machine
(startup probe, warm-up benchmark) would make the number adaptive but keep the coupling, and charges
every user a measurement they never asked for.

**The fix is to remove the dependency, not size it.** voicebox already knows *when* the app bot
spoke, from the VAD — instantly, and identically on every machine. Only *what it said* needs Whisper.
Turn timing and comprehension are separate signals, currently coupled for no reason: a vestigial
aggregator's timer is being used as a transcript courier.

Once the app bot's transcript is emitted where it is produced, no value anywhere in voicebox is
compared against decode speed. A slow machine reports its lag through the existing
`transcription_lag_secs` field instead of encoding it in a constant.

The codebase already applied this principle once. `_emit_app_bot_transcript` (`agent.py:357-380`)
throws away the aggregator's timestamp and re-derives the app bot's turn start from voicebox's own
VAD log, because the aggregator's stamp was "observed live off by up to 103 s". The decision to stop
trusting turn machinery for *timing* was made then. This change carries it through to *delivery*.

**The tester side is not involved in the problem and is not changed by the fix.**

---

## Verification of the open questions

All were open at design time and are now answered against the code, not assumed.

### Q1 — Does anything besides app-bot transcript delivery consume `on_user_turn_stopped`?

**No.** The handler is registered exactly once (`agent.py:505-511`) and its whole body is
`await self._emit_app_bot_transcript(message.content or "", message.timestamp)`.

`metrics.py` never touches turn machinery — it reads the serialized event log by `type` string
(`metrics.py:28, 97-99, 214-221`). It is insulated as long as `app_bot_transcript` events keep their
`t` and `text`.

Note `_spoken_transcripts` filters on `e.get("text")` (`metrics.py:221`), so **empty transcripts are
already excluded from every metric**. The empty event is a readability signal for `listen()` only.

### Q2 — Is the smart-turn analyzer load-bearing?

**No, once transcript delivery moves.** `LocalSmartTurnAnalyzerV3` is wired only as
`TurnAnalyzerUserTurnStopStrategy` in the app-bot aggregator's stop strategies (`agent.py:977-979`).
Its sole effect is deciding when `on_user_turn_stopped` fires — which, per Q1, only delivers the app
bot's transcript.

`speak(wait_for_turn=True)` does **not** use it. It waits on
`self._event_cond.wait_for(lambda: not self._app_bot_speaking)` (`agent.py:883`), and
`_app_bot_speaking` is toggled only by app-bot VAD frames in the observer (`agent.py:398, 404`).

### Q3 — Does app-bot turn state gate when the *tester* can speak?

**No.** This was the sharpest risk in the proposal, because the tester's frames physically traverse
the app bot's aggregator (see "who is who" above).

`speak()` injects `[LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame]` via
`self._pipeline_task.queue_frames(...)` (`agent.py:912-917`), at the pipeline source, so the frames
pass through `user_aggregator` on their way to the TTS.

In `LLMUserAggregator.process_frame`
(`.venv/.../pipecat/processors/aggregators/llm_response_universal.py:667-722`), `LLMTextFrame`
matches no branch and falls through to the final `else: await self.push_frame(frame, direction)` —
forwarded unconditionally, with no reference to turn state. The two early-return gates ahead of it do
not apply: `_maybe_mute_frame` (mute is never enabled) and `_vad_controller` (`None`, because the
pair is deliberately built without a `vad_analyzer`, `agent.py:956-958`).

**So app-bot turn closure has no influence on when the tester speaks — before or after this change.**
`TURN_STOP_TIMEOUT_SECS` never gated tester speech; it only ever gated app-bot transcript *reporting*.

### Q4 (discovered during verification) — will the observer actually see the transcript?

**Yes, but it needs one addition.** The app bot's aggregator *consumes* `TranscriptionFrame` and does
not push it downstream (`llm_response_universal.py:696-700`, with an explicit comment that final
`TranscriptionFrame`s are consumed there). However, the observer watches processor→processor
**pushes**, and the `stt`→`user_aggregator` hop is a downstream push, so the frame is observable at
that hop.

`TranscriptionFrame` is simply not in `_PipelineEventObserver._WATCHED` today (`agent.py:179-186`).
Adding it is the enabling change.

---

## Scenarios

1. **Slow decode, correct report.** The app bot speaks; Whisper takes longer than any turn timer.
   `listen()` returns `app_bot_transcript` as soon as the decode finishes, stamped with the VAD start
   of the app-bot utterance it belongs to — not an empty event followed by a mistimed orphan.
2. **Fast decode, unchanged behaviour.** On a machine where Whisper beats the timer today, the event
   stream is identical to current output.
3. **Silent segment.** Whisper recovers nothing from an app-bot segment; `listen()` still shows
   `transcription_empty: true`, so a reader can tell "we tried and got nothing" from "the app bot
   never spoke".
4. **Any hardware.** No constant in voicebox is compared against decode speed; a slower machine
   reports a larger `transcription_lag_secs` and nothing else changes.

### View impact (4+1 delta)

| View | Impact |
|---|---|
| **Logical** | App-bot transcript delivery moves from the aggregator's turn-stop event to the pipeline observer. The observer becomes the single source of app-bot events — speech spans *and* text. Tester side untouched. |
| **Process** | One coupling removed: the STT worker's completion no longer races a turn timer. No new tasks, no new locks. |
| **Development** | `agent.py` (observer `_WATCHED`, handler, constant), `nonblocking_whisper_stt.py` (empty-segment signal). No new module. |
| **Physical** | None. |
| **Scenarios** | The four above; acceptance tests derived per phase. |

**Architecture shape:** no new pattern. A relocation of responsibility inside the existing pipeline —
the same shape as the 2026-07-29 plan's Phases 1–2.

---

## Phase 1 — Emit the app bot's transcript where it is produced

| # | Change | Where |
|---|---|---|
| 1.1 | Add `TranscriptionFrame` to `_WATCHED` | `agent.py:179-186` |
| 1.2 | In `_on_pipeline_frame`, branch on `TranscriptionFrame` → `_emit_app_bot_transcript(frame.text)` | `agent.py:389+` |
| 1.3 | Drop the `aggregator_turn_started_at` parameter and its fallback — with delivery on the frame, the app-bot VAD log is the only source of turn start | `agent.py:357-380` |
| 1.4 | Remove the `on_user_turn_stopped` handler | `agent.py:505-511` |

The frame-id dedup already in the observer (`agent.py:200-202`) keeps a single emission even though
the same frame instance may be observed at more than one hop.

**Verify.** Unit: feed the observer a `TranscriptionFrame` after a `VADUserStartedSpeakingFrame` and
assert one `app_bot_transcript` stamped with that VAD start. Regression: existing event-ordering
tests must still pass. Live: a dogfood session where a transcript takes longer than 5 s must show the
transcript on its own utterance's timestamp.

## Phase 2 — Re-home the empty-transcript signal

Task F's "we tried and got nothing" currently rides on the app-bot aggregator firing with
`content=None`. After Phase 1 there is no frame for a silent segment, so the signal must come from
the component that knows: the STT worker, which sees a segment go in and no `TranscriptionFrame` come
out (`nonblocking_whisper_stt.py:180-194`).

**Decision to settle before coding** (surfaced here rather than improvised): the worker needs a way
to tell the agent. Recommended: an optional `on_empty_segment` callback attribute on
`NonBlockingSegmentedSTT`, set by `agent.py`. Single-use, no new frame type, no new module.
Rejected alternative: a custom `EmptySegmentFrame` — more machinery than a one-consumer signal earns.

Counting requires the worker to see what `process_generator` pushed, so `_transcribe_worker` iterates
the generator itself and counts `TranscriptionFrame`s before forwarding.

**Verify.** Unit: a stubbed `run_stt` yielding nothing produces one `transcription_empty: true`
event, and the app-bot VAD start it claims is its own.

**Alternative if this proves fiddly:** drop the empty event entirely. `metrics.py` already ignores it
(Q1), so the cost is only readability in `listen()`. A conscious call, not a silent regression.

## Phase 3 — Retire the constant

| # | Change | Where |
|---|---|---|
| 3.1 | Delete `TURN_STOP_TIMEOUT_SECS` and its `user_turn_stop_timeout` argument; pipecat's default applies to a turn nothing consumes | `agent.py:117-126, 966` |
| 3.2 | Update the `session_started` note, which warns that `app_bot_transcript` arrives late because of batch STT — still true, but no longer tied to turn closure | `agent.py:516-521` |
| 3.3 | Refresh `CLAUDE.md`'s "Non-obvious facts" — the batch-STT / turn-watchdog interaction is gone | `CLAUDE.md` |

**Verify.** Full `pytest`, `ruff`, `pyright`, plus one live dogfood session against a talkative app.

## Phase 4 (optional, gated on Phases 1–3) — Remove the vestigial turn machinery

With Q1 and Q2 answered, `LLMContextAggregatorPair`, the user-turn strategies and
`LocalSmartTurnAnalyzerV3` have no consumer on the app-bot side. They exist to decide when a human
has finished speaking so an LLM should reply — and there is no LLM. Removing them would also delete
the `_TimedSmartTurnAnalyzer` wrapper and the party-name inversion that `agent.py:16-20` has to
explain.

**Deliberately not bundled.** Phases 1–3 are a behaviour fix with a small diff. Phase 4 is a
structural change whose blast radius includes the tester side (`tts`, `assistant_aggregator`, playout
events) — note that tester frames currently rely on passing *through* the app-bot aggregator, so
removing it changes their route. It gets its own design pass once Phases 1–3 are live.

---

## What is deliberately not changing

* **`VAD_STOP_SECS = 1.0`** — about the app bot's WebRTC speech pacing, not decode speed. Costs the
  same second on every platform and should. The most likely thing to get "fixed" by mistake here.
* **`device="cpu"` / `compute_type="int8"`** — the install-simplicity pin stands (no
  libcublas/libcudnn dependency). Making it overridable is a separate ergonomics change.
* **`PLAYOUT_*` and `DRAIN_*` constants** — they reference decode speed but are *ceilings*: they decide
  only when to give up, so an oversized value costs nothing on the success path. Out of scope;
  revisit only if a real session hits them.
* **`tester_transcript` emitted at `speak()` time** — ground truth, not STT. Unaffected.
* **`enable_interruptions=False`** — correct for a tester that must be able to talk over the app bot.

## Not being built

The startup probe / warm-up calibration discussed while framing this problem is **dropped**. It
existed only to choose a correct value for `TURN_STOP_TIMEOUT_SECS`. Phase 3 deletes the constant, so
there is nothing left to calibrate — and it would have charged every user a benchmark at session
start, against voicebox's "easy to use" goal.

Model load (Whisper + Kokoro + Silero) remains a genuine startup cost. Whether it already overlaps
the browser launch in `start_browser_session` is **not verified** and is a separate ergonomics
question.

## On landing

Append `BUILDLOG.md` **D24** when the design is signed off (the decision, not the code): the app
bot's transcript is delivered on frame arrival, not turn closure; the calibration approach was
considered and rejected. Latest entry is D23.
