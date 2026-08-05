# Fix plan — audio path and reporting

*2026-07-29. Derived from [2026-07-29-field-report-triage.md](2026-07-29-field-report-triage.md).
Ordering principle: fix the two root causes first, because six of the eleven reported symptoms
dissolve when they land. Each phase is independently shippable, has its own verification, and gets
its own commit.*

## Architecture shape

No new pattern. voicebox keeps the pipecat pipeline and the four-tool MCP surface. The change is a
**relocation of responsibility inside the existing pipeline**: voice-activity detection moves from
the user aggregator to the transport, and transcription moves off the frame-processing task onto a
serial worker. Everything else is arithmetic in `metrics.py` and text in docstrings.

The strategic question this raises — whether voicebox should carry pipecat's conversational-agent
machinery (`LLMContextAggregatorPair`, smart-turn) at all, when it has no LLM in the loop and only
needs "observe the far end, speak on command" — is **out of scope here** and recorded at the bottom
as a follow-up.

---

## Phase 0 — Measure before changing (no code)

The triage left exactly one thing unattributed: the steady ~24 s per-turn transcript lag in
session 2 is larger than warm Whisper throughput accounts for. `LocalSmartTurnAnalyzerV3`
(`agent.py:299`) is the suspect but that is a hypothesis, not a finding.

Add temporary timing around `run_stt`, `analyze_end_of_turn`, and the aggregator's
`on_user_turn_stopped`, run one live session against a talkative app, and read the split.

**Exit criterion:** the ~24 s is attributed to a named call. If it is smart-turn, Phase 1 grows a
step (drop `TurnAnalyzerUserTurnStopStrategy` in favour of plain VAD endpointing — voicebox does not
need semantic turn prediction to decide when to *listen*). If it is Whisper cold-start, Phase 1 is
unchanged and Phase 5 gains a warm-up call at session start.

**Do not skip this phase.** Phases 1 and 2 are correct regardless of the answer, but the answer
changes whether they are sufficient.

---

## Phase 1 — Move the VAD upstream of the STT

*Fixes items 2 and 3. Root cause R1b.*

Pass the VAD to the transport instead of the aggregator, so `VADUserStartedSpeakingFrame` reaches
the STT ahead of the audio it describes.

| Change | File |
|---|---|
| `WebsocketServerParams(..., vad_analyzer=SileroVADAnalyzer(VADParams(stop_secs=VAD_STOP_SECS)))` | `agent.py` (`create_agent`) |
| Drop `vad_analyzer=` from `LLMUserAggregatorParams` | `agent.py:305` |
| Move `VAD_STOP_SECS` so both sites read the one constant | `agent.py:89` |

The `_PipelineEventObserver` needs no change: it watches frame types, not producers, and both VAD
frames still cross the pipeline downstream.

**Verify.** `docs/artefacts/field-report-triage/probe_vad_upstream.py` already demonstrates the
mechanism (0 % loss vs 90 %). Promote it to a checked-in regression test that asserts a segment fed
through a stalled STT reaches `run_stt` whole. Then one live session: `app_bot_speech_stopped -
app_bot_speech_started` must match the app's own logged speech duration within `vad_stop_secs`, and
transcripts must start at sentence boundaries.

**Watch for.** VAD timestamps become acoustically true rather than post-STT, so `app_bot_speech_*`
values will shift earlier. That is the point, but it changes every number in `metrics.json` — land
this before Phase 4.

---

## Phase 2 — Take transcription off the frame path

*Fixes items 5, 6 and 10. Root cause R1a.*

Subclass `WhisperSTTService` in voicebox to override `_handle_user_stopped_speaking` so it hands the
segment to a single background worker and returns immediately. One worker, not a pool: Whisper on
CPU is faster than realtime (0.40×) but two concurrent runs would contend, and segments must stay in
order.

```
_handle_user_stopped_speaking()  ->  put segment on an asyncio.Queue, return
_transcribe_worker()             ->  get segment, await run_stt, push frames, repeat
```

New file `src/voicebox/processors/nonblocking_whisper_stt.py`, wired in `_create_stt_service`
(`agent.py:676-689`). The `device="cpu"` / `compute_type="int8"` pin stays — it is not the problem.

Also in this phase:

* **Bound `wait_for_playout`.** `PLAYOUT_TIMEOUT_SECS = 120.0` (`agent.py:92`) and the MCP-side
  `deadline = 150.0` (`server.py:248`) are both longer than any MCP client will wait. Drop the agent
  timeout to ~30 s and, on expiry, return `{"queued": True, "played": False, "reason": ...}` rather
  than raising — the caller needs a diagnostic, not a timeout.
* **Surface the backlog.** Add `pending_transcriptions` to the worker and report it on
  `session_started`'s sibling — a `transcription_lag_secs` field on `listen()`'s response envelope,
  so an agent can tell "still transcribing" from "nothing happened".

**Verify.** Promote `probe_stt_blocking.py`: assert a frame queued during a stalled `run_stt`
reaches the downstream processor within ~100 ms instead of after the stall. Live: issue `speak()`
while the app bot is mid-sentence and confirm audio starts within ~2 s (Kokoro synthesis) rather
than after the transcript lands.

---

## Phase 3 — Close the transcript-loss holes

*Fixes item 1.*

Two independent holes, both worth closing.

1. **Drain before dumping.** `agent.stop()` writes `events.json` / `metrics.json` at `agent.py:426`
   and only then sends `EndFrame` (`agent.py:431`). Await the STT worker's queue (bounded, ~15 s)
   before the dump. With Phase 2 the backlog is small; without the drain it is still lossy.
2. **Never emit silence as the signal.** `agent.py:359`'s `if message.content:` swallows an empty
   batch result. Emit the event either way, adding `transcription_empty: true` when the text is
   blank, so the gap is visible in `events.json` and reconcilable in `metrics.json`.

**Verify.** Stub the STT to return `""` for one segment and assert an `app_bot_transcript` event
appears with the flag. Live: `stop()` immediately after a long bot turn; its transcript must be in
`events.json`.

---

## Phase 4 — Kokoro plays one utterance, not several

*Fixes item 4 — both halves.*

**4a — the audio-path half (the one that matters).** `run_tts` yields each chunk as
`create_stream` produces it (`kokoro_tts.py:171-181`), and the CPU synthesis gap between chunks
becomes real silence in the synthetic microphone. Buffer the whole utterance before yielding any
audio. Time-to-first-byte is irrelevant for a synthetic tester — nobody is listening for a natural
response — and gap-free playout is the entire point.

**4b — the reporting half.** With 4a landed, `PLAYOUT_SETTLE_SECS = 1.0` (`agent.py:99`) becomes
dead weight. Replace the silence heuristic with the deterministic signal: resolve `_Playout` on the
first `BotStoppedSpeakingFrame` that follows the utterance's `TTSStoppedFrame`. Delete the settle
timer.

**Verify.** Speak a three-sentence utterance and assert exactly one
`tester_speech_started`/`tester_speech_stopped` pair, with `finished_at - started_at` matching the
WAV's duration. Live: the app under test logs one user turn, not two.

---

## Phase 5 — Make `metrics.json` reconcile

*Fixes items 7, 8, 9 and 6. Pure `metrics.py` + docstrings, no pipeline changes.*

| # | Change | Where |
|---|---|---|
| 5.1 | Build `turns` from **speech intervals**, attaching a transcript when one exists and `transcript_missing: true` when it does not. `utterances.app_bot` and the app-bot row count then agree by construction, and `response_latency_secs` survives a missing transcript | `metrics.py:183-207` |
| 5.2 | Attribute each gap to whoever owed the next turn: a gap after a **tester** utterance is app dead air; a gap after an **app** utterance is tester think time. Report them as separate fields; keep `total_dead_air_secs` meaning only the first | `metrics.py:210-221` |
| 5.3 | Add a `biases` note naming what the new gap fields do and do not mean | `metrics.py:154-163` |
| 5.4 | Sort each batch `listen()` returns by `t`. Append order stays authoritative for the cursor (so cursor semantics are untouched); only the returned slice is ordered. Document that `tester_transcript` is stamped at `speak()` time by design | `agent.py:536`, `server.py`, `events.py:85-94` |

5.4 is deliberately the weak form: sorting a *batch* cannot reorder across a cursor boundary, so a
late event can still arrive after a consumer has moved on. Phases 1 and 2 shrink the construction-
to-observation skew from tens of seconds to milliseconds, which is the real fix; the sort is
belt-and-braces.

**Verify.** `metrics.py` is a pure function over event dicts — unit-test it directly against the two
captured `events.json` files. Assert `len([t for t in turns if t["speaker"] == "app_bot"]) ==
utterances["app_bot"]` on both, and that session 1's 3.754 s latency now appears on a turn.

---

## Phase 6 — Session startup and attach

*Fixes item 11. Independent of everything above; can land first if convenient.*

| # | Change | Where |
|---|---|---|
| 6.1 | Stop swallowing a failed navigation. `page.goto` failure is caught, logged and followed by `ready_event.set()` — propagate it to the parent so `start_browser_session` raises instead of returning a success payload for an `about:blank` tab | `browser_session.py:181-187` |
| 6.2 | After `goto`, poll until the page URL matches and the shim reports `installed`, with a timeout; fail loudly on expiry | `browser_session.py` |
| 6.3 | Replace `attach_hint` with `playwright-cli attach --cdp <endpoint>` | `browser_session.py:82` |
| 6.4 | Rewrite the `start_browser_session` docstring and `CLAUDE.md`'s "Driving the UI" section: `attach` does not navigate, so the `close-all` + env-var dance and its `open` follow-up are gone. Keep the "do not open new tabs" warning — it is correct and now consistent | `server.py:91-111`, `CLAUDE.md` |

**Verify.** Point `start_browser_session` at an unreachable URL and confirm it raises. Then a live
attach: `attach --cdp`, `tab-select 1`, `snapshot` — and `listen()` shows `client_connected` with no
`client_disconnected`.

---

## What is deliberately not being changed

* **`device="cpu"` / `compute_type="int8"`** — measured at 0.40× realtime warm. The pin is right.
* **`tester_transcript` emitted at `speak()` time** — it is ground truth, not STT. Documented, not moved.
* **`enable_interruptions=False`** — correct for a synthetic user that must be able to talk over the bot.

## Follow-up, not in this plan

Phases 1 and 2 relocate responsibilities inside a pipeline whose shape voicebox may not need.
`LLMContextAggregatorPair` + `LocalSmartTurnAnalyzerV3` exist to decide *when a human has finished
their turn so an LLM should reply*. voicebox has no LLM in the loop and replies only when Claude
calls `speak()`. A dedicated observer processor — VAD segmentation plus a transcription worker,
emitting voicebox events directly — would remove the aggregator, the turn strategies and the
party-name inversion that `agent.py:16-20` has to explain. Worth a separate design once Phases 1–5
have shown which of that machinery was load-bearing.
