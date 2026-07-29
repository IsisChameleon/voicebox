# Field-report triage — two MCP-driven sessions against a reading bot

*2026-07-29. Verdicts on the 11 findings reported from sessions
`test-session-20260728-121027` and `test-session-20260728-cyoa-choice`. Evidence:
[../artefacts/field-report-triage/probe-output.md](../artefacts/field-report-triage/probe-output.md).
Fix plan: [2026-07-29-audio-path-and-reporting-fixes.md](2026-07-29-audio-path-and-reporting-fixes.md).*

The report was accurate about **what** went wrong in every case. It was wrong about **why** in four
of them, and in two of those the correct cause is more serious than the one proposed.

## Verdict table

| # | Reported | Verdict | Actual cause |
|---|---|---|---|
| 1 | Transcripts silently dropped | **Confirmed, cause refuted** | `stop()` dumps artifacts before the STT drains — not the `if message.content` gate |
| 2 | Whisper hallucinating "Thank you." | **Confirmed, cause confirmed** | Downstream of R1: Whisper is handed ~1 s of a multi-second utterance |
| 3 | App-bot speech chopped by a human turn strategy | **Confirmed, cause refuted** | Audio is **discarded**, not mis-segmented (R1) |
| 4 | `wait_for_playout` returns early | **Confirmed** | `PLAYOUT_SETTLE_SECS` too short — *and* a real audio-path defect underneath |
| 5 | A `speak()` never played; call hung | **Partly refuted** | It played 51 s late, not never. Root cause R1 |
| 6 | `listen()` events out of chronological order | **Confirmed** | `t` is frame-construction time, log position is observation time (R1 widens the skew) |
| 7 | `metrics.json` internally inconsistent | **Confirmed, cause confirmed** | `_turns()` built from transcripts, `utterances` from intervals |
| 8 | `response_latency_secs` never appeared | **Confirmed, cause confirmed** | Falls out of fixing #7 |
| 9 | `dead_air_gaps` counts the tester's thinking time | **Confirmed, cause confirmed** | `_gaps()` has no notion of who owed the next turn |
| 10 | ~27 s transcript lag from the CPU pin | **Confirmed, cause refuted** | Not throughput — Whisper runs at 0.4× realtime. It is serialisation (R1) |
| 11 | `attach_hint`'s procedure destroys the session | **Confirmed** | `playwright-cli open` runs `goto about:blank` on the shim tab |

## Two root causes explain seven of the eleven

### R1 — The STT sits in the frame path, and the VAD sits behind it

voicebox builds pipecat's *conversational agent* pipeline shape for a job that is not a
conversational agent (`agent.py:318-325`):

```
transport.input() → stt → user_aggregator → tts → assistant_aggregator → transport.output()
```

The VAD is a parameter of the **user aggregator** (`agent.py:305`), which is **downstream of the
STT**. Two consequences follow, both measured.

**R1a — `run_stt` blocks every later frame.** `SegmentedSTTService._handle_user_stopped_speaking`
awaits the transcription inline
(`pipecat/services/stt_service.py:780`), and it is reached from
`FrameProcessor.__input_frame_task_handler` (`frame_processor.py:1014-1015`) — the task that also
handles system frames. Nothing gets past the STT while Whisper runs. Probe 1 measured a 5 s stall
delaying a queued `speak()` triplet by 4.7 s and a downstream VAD frame by 4.5 s.

**R1b — audio arriving during the stall is trimmed to 1 s and thrown away.** While the STT's frame
task is blocked, audio queues at the STT. When it drains, each frame is appended to `_audio_buffer`
— and because `_user_speaking` is still `False` (the VAD that would set it lives downstream and its
`VADUserStartedSpeakingFrame` cannot arrive until the audio has passed through), the buffer is
trimmed to the last second on every frame:

```python
# pipecat/services/stt_service.py:804-807
if not self._user_speaking and len(self._audio_buffer) > self._audio_buffer_size_1s:
    discarded = len(self._audio_buffer) - self._audio_buffer_size_1s
    self._audio_buffer = self._audio_buffer[discarded:]
```

Probe 2: **10 s of speech fed in, 1 s reached Whisper — 90 % discarded.** This is why transcripts
start mid-sentence ("here. We're going to have a wonderful time…"), why a 21 s utterance measures
5.1 s, and why Whisper hallucinates "Thank you." — it is being handed a one-second stub, which is
exactly the near-silence input that produces that artefact.

Probe 3 confirms the fix: with the VAD upstream of the STT, the same stall loses **0 %**, because
`FrameProcessorQueue` gives system frames priority (`frame_processor.py:148-154`) and
`_user_speaking` is set before the backlog is appended.

R1 explains items **2, 3, 5, 6, 10** and part of **1**.

### R2 — Kokoro playout has real, audible gaps

`KokoroTTSService.run_tts` yields each chunk as `create_stream` produces it
(`processors/kokoro_tts.py:171-181`). Between chunks nothing is written to the mic. pipecat's output
transport declares the bot stopped after `BOT_VAD_STOP_SECS = 0.35` of outgoing silence
(`transports/base_output.py:55, 714-719`).

The observed gap was **1.634 s** (`tester_speech_stopped` 1785206264.66 → `tester_speech_started`
1785206266.29). So two things happen at once, and the report caught only the first:

1. `PLAYOUT_SETTLE_SECS = 1.0` (`agent.py:99`) expires during the gap and `wait_for_playout`
   resolves on the first segment. A reporting bug.
2. **1.6 s of real silence goes into the synthetic microphone**, which the app under test endpoints
   on. The app genuinely heard two user turns. That is not a reporting bug — it changes the app's
   behaviour under test and invalidates the run.

R2 explains item **4**.

## Item-by-item detail where the report's diagnosis needs correcting

### 1 — Transcripts dropped: it is teardown, not the `if message.content` gate

Session 1's 44.8 s block (`app_bot_speech_started` 1785204878.24 → `stopped` 1785204923.01) has no
transcript. `session_stopped` is at 1785204982.76 — 59.7 s later. `agent.stop()` emits
`SESSION_STOPPED`, then **dumps `events.json` and `metrics.json` at `agent.py:426`, before sending
`EndFrame` at `agent.py:431`**. Any transcription still in flight — and after R1b's backlog, one
was — never reaches the log or the artefacts.

The `if message.content:` gate at `agent.py:359` is a real hazard and worth closing, but it is not
what happened here. Closing it alone would have produced an empty-text event, not the missing
44 s turn.

### 5 — The `speak()` was late, not lost

The event log contradicts "the audio was queued and never played". `speak()` was called at
1785206446.00; the log then shows four `tester_speech_started/stopped` pairs between 1785206497.29
and 1785206507.07 — two utterances of two Kokoro segments each: the original **and** the retry.
The original played 51 s late.

The 51 s is R1a, and the correlation is exact in all three cases:

| `speak()` at | audio started | last `app_bot_transcript` before it | delay after STT freed |
|---|---|---|---|
| 1785206446.00 | 1785206497.29 | 1785206494.94 | 2.35 s |
| 1785206251.47 | 1785206263.81 | 1785206262.31 | 1.50 s |
| 1785204870.06 | 1785204872.65 | (no backlog) | 2.59 s |

In every case our audio starts ~2 s (Kokoro synthesis) after the STT stops blocking. The report's
"deadlock" hypothesis is refuted; the "bounded timeout with a diagnostic" suggestion is still right,
and is in the fix plan.

The MCP call did not hang forever, either: `send_command` enforces `deadline=150.0`
(`server.py:248`) over the child's `PLAYOUT_TIMEOUT_SECS = 120.0` (`agent.py:92`). What the caller
experienced was almost certainly their own client timeout firing first. The values are too generous
to be useful.

### 6 — Ordering: the mechanism, confirmed

`app_bot_speech_*` events take their `t` from `frame.timestamp` (`agent.py:251, 256`), which is set
when the VAD constructs the frame (`pipecat/frames/frames.py:1046`). The log position is the
moment the observer sees the frame. Probe 1 measured those two instants 4.5 s apart under a stall.
Any stall between construction and observation reorders the log. R1 makes stalls tens of seconds
long, which is why it was visible.

Note this also means the `t` values are *trustworthy* and only the ordering is wrong — sorting is
safe.

### 10 — Not the CPU pin

Whisper CPU int8 runs at **0.40× realtime warm** (probe 4): 60 s of audio in 23.7 s. The pin is
fine and should stay. The one-off costs are a 13.5 s model load and a ~12 s cold first inference,
which is most of session 1's first 27.3 s lag. The rest is R1a serialisation.

One thing I could **not** attribute from the artefacts alone: the steady ~24 s per-turn transcript
lag in session 2 is larger than warm Whisper throughput accounts for. `LocalSmartTurnAnalyzerV3`
inference (`agent.py:299`) runs per turn in the aggregator's frame task and is the obvious
suspect, but I have no measurement. **This is the first verification step in the fix plan, not an
assumption to build on.**

### 11 — Confirmed against playwright-cli's own source

Both halves check out.

*The attach procedure.* `playwright-cli open` with no URL runs `goto about:blank`
(`@playwright/cli/.../playwright-core/lib/tools/cli-client/program.js:128`). With
`PLAYWRIGHT_MCP_ISOLATED=false`, "the current page" over CDP is voicebox's shim tab. The documented
`attach_hint` (`browser_session.py:82`) therefore leads directly to blanking the shim tab. The
proposed replacement is valid — `attach --cdp <url>` is a documented subcommand that performs no
navigation:

```
$ playwright-cli attach --help
Attach to a running Playwright browser
Options:
  --cdp    connect to an existing browser via cdp endpoint url.
```

*The silent `about:blank` start.* Confirmed by code. `browser_session.py:182-184` catches a failed
`page.goto`, logs it, and falls through to `ready_event.set()`. `start_browser_session` returns a
success payload with the page never navigated.

## Working as designed — document, don't change

* **`tester_transcript` is emitted at `speak()` time, not at playout.** Deliberate: it is the
  ground-truth input string, not an STT result (`events.py:85-94`). Keep the emission point; fix the
  log ordering (item 6) and say so in the docstring.
* **`device="cpu"` / `compute_type="int8"`.** The rationale in `agent.py:681-684` holds and the
  measured throughput vindicates it.
* **`enable_interruptions=False` on the start strategies** (`agent.py:294-297`). Correct for a
  synthetic user that must be able to talk over the bot.
* **`session.biases`.** Genuinely good, and the right home for the dead-air caveat (item 9).

## My severity ranking

Where it differs from the report's, the reason is *plausible wrong data beats obviously missing
data* — a consumer can see a gap, but cannot see a truncated transcript that reads fine.

| Rank | Item | Why |
|---|---|---|
| 1 | **2 + 3** (audio discarded) | voicebox reports the app said something it did not say. Silent corruption, and it poisons every downstream metric. The report ranked these 2nd/3rd |
| 2 | **4** (utterance split in the mic) | Changes the app's behaviour under test. A split turn invalidates the run, not just the report |
| 3 | **1** (transcripts dropped) | Agreed — high, and visible |
| 4 | **11** (attach destroys the session) | Agreed — high, but a documentation fix |
| 5 | **5** (speak 51 s late) | Real, but a symptom of R1 |
| 6 | **7 + 8 + 9 + 6** | Reporting correctness |
| 7 | **10** | Ergonomics; largely dissolves once R1 is fixed |
