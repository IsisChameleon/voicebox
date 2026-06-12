# Upgrade roadmap — voicebox as a human-impersonating voice tester

*2026-06-11. Staged plan derived from [architecture-review.md](architecture-review.md) and
[voice-testing-landscape.md](voice-testing-landscape.md). Diagram: `architecture-stages.html`.*

Ordering principle: each stage is independently shippable and verified by the existing `scripts/`
drivers (there is no unit-test suite; verification is end-to-end by design). Stages 0–1 are
prerequisites for everything else; 2–4 build the testing value; 5 is prompt-side.

---

## Stage 0 — Correctness fixes (no API change)

Fix the verified bugs so the existing 4-tool surface behaves as documented.

| # | Change | Files | Verify |
|---|--------|-------|--------|
| 0.1 | Disable interruption-on-VAD: configure `start` turn strategies with `enable_interruptions=False` so the bot's speech no longer cancels our in-flight Kokoro TTS | `agent.py` | e2e: `speak()` a long utterance while the bot is talking; `record_dir` WAV shows Kokoro audio complete, not truncated |
| 0.2 | Error responses keyed `"error"`, and the command loop survives a failed command instead of `break`ing | `bot.py` | drive an induced failure (e.g. malformed command); next `listen`/`speak` still works; error surfaces as MCP tool error, not as a transcript |
| 0.3 | `stop()` reaps the child: call `stop_pipecat_process()` after the `stop` command; add a parent-side deadline to `send_command` so a hung child can't block an MCP call forever | `server.py`, `agent_ipc.py` | `stop()` then `start_browser_session()` again in one server lifetime; ports free, no zombie process |
| 0.4 | shim: support repeated `getUserMedia(audio)` calls — keep all live generators, fan inbound Kokoro frames out to each (drop ended tracks) | `shim.js` | smoke script extended: call `getUserMedia` twice, assert both tracks receive audio (`__voiceShim.perTrackBytes`) |
| 0.5 | Remove the fake-speech disconnect sentinel (`"I just disconnected…"`); interim: return it via a distinguishable key until Stage 2 events exist | `agent.py` | kill the page mid-listen; `listen` result is identifiable as a disconnect, not a transcript |

**Exit criteria:** all of `scripts/smoke_browser_shim.py` and `scripts/e2e_readme_call.py` pass;
talk-over audio is physically possible.

## Stage 1 — Full-duplex IPC (foundation)

Make the parent↔child mailbox concurrent so `speak` can happen during `listen`.

- Correlation IDs: every request `{"id", "cmd", ...}`; every response `{"id", ...}`.
- Child: dispatch each command as an `asyncio.create_task`; the loop only reads and spawns.
- Parent: a single response-router task reads the response queue and resolves per-id
  `asyncio.Future`s — eliminates the two-waiters race from review bug 3, and makes cancelled MCP
  calls harmless (their late response resolves a future nobody awaits).

**Verify:** script that issues `listen(timeout=30)` and, 2 s later, `speak(...)` concurrently —
the speak must execute immediately (audible in the WAV) while the listen stays pending.

## Stage 2 — Event-stream `listen` + speech completion

Replace "transcript string" with a timestamped conversation event log (the single highest-value
change — gives streaming-ish awareness, proper disconnect signaling, and latency data for free).

- Child keeps a monotonic event log: `bot_speech_started` / `bot_speech_stopped` (from
  `VADUserStarted/StoppedSpeakingFrame`, which carry wall-clock `time.time()`), `transcript`
  (text + turn-span timestamps), `tts_started` / `tts_finished` / `tts_interrupted` (from TTS
  frames + the assistant aggregator's `interrupted` flag), `client_connected` / `client_disconnected`.
- `listen(timeout, cursor=None)` → `{events: [...], cursor}` — blocks until ≥1 new event or
  timeout; cursor lets Claude resume without missing or re-reading events. Old behavior (wait for
  next transcript) remains expressible: loop until a `transcript` event arrives.
- `speak(text, wait=False)` — with `wait=True`, resolve when our audio finished playing out
  (observe output transport / bot-speaking frames on our side), and report
  `{started_at, finished_at, interrupted}`.
- Document the timing bias: utterance-stop wall-clock lands ~`stop_secs` (1.0 s) after true speech
  end; record the configured value in the event log header so consumers can subtract it.

**Verify:** e2e readme call; assert event ordering and that
`bot_speech_started − tts_finished` ≈ the response gap measured by eye in the merged WAV.

## Stage 3 — Declarative barge-in primitives

Real-time interruption can't live in the LLM loop (seconds of latency); the child executes it at
audio-rate, Claude scripts it — the "Interrupter persona" pattern every commercial tool uses.

- `speak(text, interrupt=True)` — start TTS immediately even mid-bot-utterance (possible since 0.1).
- `speak(text, when="bot_speaking", after_secs=1.5)` — child arms a one-shot trigger on the next
  `bot_speech_started`, waits `after_secs`, then speaks. Returns the armed/fired timing in the
  event log.
- Derived metric: **interruption stoppage timing** — from the event log,
  `bot_speech_stopped − tts_started` when we barge in (industry budget: ~60 ms for the app under
  test to stop talking).

**Verify:** scripted barge-in against the readme app: assert overlap window exists in the stereo
recording and the stoppage-timing number is plausible vs. the waveform.

## Stage 4 — Metrics & artifacts (the "test report")

Make every session produce an offline-analyzable artifact, like EVA/Coval/Hamming do.

- Stereo WAV: our voice on one channel, bot on the other (extend the existing
  `AudioBufferProcessor` merge in `agent.py:_dump_recordings`).
- `events.json` (the Stage 2 log) + `metrics.json` per session: per-turn response latency
  (our-speech-end → bot-first-audio), dead-air gaps, talk-over windows, talk ratio,
  utterance count, transcripts.
- Note known biases in the file itself (VAD `stop_secs`, batch-Whisper delay).

**Verify:** run e2e with `record_dir`; cross-check `metrics.json` latency for one turn against the
waveform in an audio editor.

## Stage 5 — Scenario layer (prompt-side, minimal server code)

- A `SCENARIOS.md` / Claude skill documenting the convention: persona + goal + behavior +
  success criteria; run N times; judge the transcript + `metrics.json` afterwards (LLM judge).
- Optional later: scenario replay from a recorded session (Hamming's pattern); background-noise
  mixing into the synthetic mic (Cekura/Coval personas); network impairment via CDP emulation.

**Verify:** one written scenario ("interrupting customer asks for a refund") executed end-to-end
by Claude against the readme app, producing a pass/fail judgment with cited metrics.

---

## Out of scope (explicitly)

- Streaming STT / partial transcripts inside an utterance — batch Whisper + VAD segmentation is
  good enough once events stream; revisit only if a scenario needs word-level reaction.
- True real-time *reactive* interruption driven by the LLM — physically impossible at LLM latency;
  the declarative primitives in Stage 3 are the correct substitute.
- Multi-session support (parallel calls) — the single-session, pinned-ports design stays.
