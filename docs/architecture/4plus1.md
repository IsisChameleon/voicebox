# voicebox — 4+1 architecture views

Living core doc (Kruchten 4+1). Every element cites the code that grounds it (`path:line`);
citations are refreshable via review mode, the symbol names are the durable anchors.

- System boundary: the `voicebox` MCP server and everything it spawns (pipecat child, Chromium child, in-page shim).
- Last full describe pass: 2026-08-03 (branch `fix/audio-path-and-reporting`).

## Logical view

*What are the domain concepts and their relationships?*

The domain is a **synthetic voice user**: an LLM plays a human against a browser voice app it
cannot see, through four MCP tools. Two parties, named from the test's point of view and
deliberately inverted from pipecat's: `app_bot` (the app's voice agent under test; its audio is
pipeline *input*, pipecat's "user") and `tester` (us, Kokoro-voiced; pipeline *output*, pipecat's
"bot"). The inversion mapping lives in exactly two places: the events module preamble
(`src/voicebox/events.py:9-15`) and the pipeline observer (`src/voicebox/agent.py:339-346`).

| Element | Role | Evidence |
|---|---|---|
| MCP tool surface (`start_browser_session`, `speak`, `listen`, `stop`) | The four verbs the LLM drives; text/control only, no audio | `src/voicebox/server.py:75-321` |
| `PipecatMCPAgent` | Owns the STT→aggregator→TTS pipeline and the session's event log; exposes `listen_events()`/`speak()`/`stop()` | `src/voicebox/agent.py:252-914` |
| Event log + `EventType` vocabulary | Monotonic, timestamped conversation record; the single source of truth `listen()` streams by cursor | `src/voicebox/events.py:29-127`, `src/voicebox/agent.py:279-305` |
| `_PipelineEventObserver` | Translates pipecat frames (VAD user start/stop, bot start/stop, interruption, TTS stopped) into log events without touching the pipeline | `src/voicebox/agent.py:159-191`, `src/voicebox/agent.py:339-377` |
| `_Playout` | Tracks one in-flight `speak(wait_for_playout=True)` until its audio truly ends (first `BotStoppedSpeakingFrame` after `TTSStoppedFrame`), or an interruption | `src/voicebox/agent.py:194-249` |
| `NonBlockingSegmentedSTT` + `EagerSegmentsWhisperModel` | Moves Whisper off pipecat's frame task onto one ordered worker; makes faster-whisper's lazy decode eager inside `to_thread` | `src/voicebox/processors/nonblocking_whisper_stt.py:96-223`, `src/voicebox/processors/nonblocking_whisper_stt.py:58-93` |
| `KokoroTTSService` | Local TTS (`af_heart`); buffers the *whole* utterance before yielding so the synthetic mic is gap-free | `src/voicebox/processors/kokoro_tts.py:85-194`, buffering at `src/voicebox/processors/kokoro_tts.py:171-188` |
| Browser shim (`shim.js`) | Hook 1: `getUserMedia` returns a synthetic mic fed by WS frames. Hook 2: wraps `RTCPeerConnection`, tees remote audio back over the same WS. Diagnostics on `window.__voiceShim` | `src/voicebox/shim.js:153-175` (hook 1), `src/voicebox/shim.js:177-349` (hook 2), `src/voicebox/shim.js:41-65` (diag) |
| `RawPCMSerializer` | Wire format: raw 16-bit LE mono PCM, no envelope | `src/voicebox/raw_pcm_serializer.py:22-45` |
| `BrowserShimRunnerArguments` | Transport config dataclass (host/port, asymmetric `mic_rate`/`tap_rate`, `record_dir`) | `src/voicebox/runner_args.py:20-47` |
| `compute_metrics` | Pure function: event log in → `metrics.json` report out (latencies, talk-over, dead air vs think time vs outage, turns) with explicit bias notes | `src/voicebox/metrics.py:15-76`, biases `src/voicebox/metrics.py:166-192` |
| Timing instrumentation | `log_duration` + mixins bracketing `run_stt` / `analyze_end_of_turn`, greppable `voicebox.timing` DEBUG lines | `src/voicebox/timing.py:39-106` |

Key relationships: `server.py` (parent) knows only IPC and browser lifecycle — it imports no
pipecat (`src/voicebox/server.py:24-26`, `src/voicebox/server.py:299-301`). `bot.py` bridges IPC
to `PipecatMCPAgent` (`src/voicebox/bot.py:44-45`). UI driving is *not* in this system: an
external Playwright client attaches over CDP (`src/voicebox/browser_session.py:14-17`).

## Process view

*What runs concurrently, and where are the sync points?*

Three OS processes plus the page's own threads:

```
parent (MCP server, asyncio)          CHILD 1: pipecat agent (asyncio)      CHILD 2: Chromium runner
 FastMCP streamable-http :9090         WebsocketServerTransport :9091        Playwright + Chromium (CDP :9222)
 send_command / _response_router  ◄──► bot.py command loop                   parks on stop_event
        multiprocessing.Queue ×2              │ per-command asyncio task            │
                                              │ pipeline worker + STT worker        │ shim.js in-page:
                                              └── raw-PCM WebSocket ◄──────────────── WS client, AudioWorklet
```

| Concern | Mechanism | Evidence |
|---|---|---|
| Process creation | `multiprocessing` with `spawn` forced (fork from async context copies loop/fds/locks) | `src/voicebox/agent_ipc.py:22-25`, `src/voicebox/agent_ipc.py:92-96`; browser child `src/voicebox/browser_session.py:66-80` |
| Parent↔child IPC | Two `multiprocessing.Queue`s; full-duplex: each request carries a correlation `id`, one router task resolves per-id futures, so responses may return out of order | `src/voicebox/agent_ipc.py:31-35`, `src/voicebox/agent_ipc.py:210-253`, `src/voicebox/agent_ipc.py:256-310` |
| Child command loop | Every command runs as its own asyncio task — a `speak` arriving during a blocked `listen` executes immediately; `stop` breaks the loop | `src/voicebox/bot.py:80-106` |
| Deadlines | Parent-side per-command deadlines sized to outlive the child's internal budgets: `stop`=210 s (> drain cap 180 s), `speak` waits=60 s (> playout timeout 30 s), `listen`=timeout+30 s | `src/voicebox/server.py:296-305`, `src/voicebox/server.py:251-262`, `src/voicebox/server.py:200-202` |
| Audio plane | Out-of-band raw-PCM WebSocket between shim and pipecat — audio never crosses the MCP boundary | `src/voicebox/agent.py:931-943`, `src/voicebox/shim.js:94-140` |
| STT concurrency | One worker, never a pool (segments must stay in spoken order; Whisper decode runs eagerly inside `asyncio.to_thread`) | `src/voicebox/processors/nonblocking_whisper_stt.py:99-106`, `src/voicebox/processors/nonblocking_whisper_stt.py:180-194` |
| Event-log sync | `asyncio.Condition`; `listen_events` blocks until the log grows past the cursor; `speak(wait_for_turn)` blocks on the same condition until `_app_bot_speaking` clears | `src/voicebox/agent.py:279-305`, `src/voicebox/agent.py:642-657`, `src/voicebox/agent.py:777-780` |
| Armed barge-in | Background task per `speak(when=...)`; log position snapshotted synchronously at arm time so only later events trigger; all armed tasks cancelled on stop | `src/voicebox/agent.py:725-735`, `src/voicebox/agent.py:782-802`, `src/voicebox/agent.py:500-504` |
| In-page audio | Shim's outbound tap runs on an `AudioWorklet` at a 16 kHz `AudioContext` (worklet posts Int16 buffers to the main thread, which does `ws.send`); inbound Kokoro frames fan out to every live synthetic-mic writer | `src/voicebox/shim.js:209-254`, `src/voicebox/shim.js:319-333`, `src/voicebox/shim.js:72-92` |
| Teardown ordering | `session_stopped` emitted first (wakes pending listens) → cancel armed tasks → STT drain (budget scales with backlog) → settle log → dump artifacts → `EndFrame` → runner returns → parent reaps both children | `src/voicebox/agent.py:482-535`, `src/voicebox/bot.py:85-102`, `src/voicebox/server.py:296-321` |

## Development view

*How is the code organized, built, and checked?*

Single Python package, `src/` layout, `uv`-managed; one JS file (the shim) shipped as package
data and injected as text (`src/voicebox/browser_session.py:196-197`).

| Layer | Contents | Evidence |
|---|---|---|
| MCP surface | `server.py` — parent-only; deliberately never imports pipecat | `src/voicebox/server.py:24-26` |
| IPC / lifecycle | `agent_ipc.py`, `browser_session.py`, `bot.py` | file docstrings `src/voicebox/agent_ipc.py:7-12`, `src/voicebox/browser_session.py:7-18` |
| Voice pipeline | `agent.py`, `runner_args.py`, `raw_pcm_serializer.py`, `processors/` (Kokoro TTS, non-blocking Whisper), `timing.py` | `src/voicebox/agent.py:7-21` |
| Pure/report | `events.py`, `metrics.py` — no I/O, no pipecat imports; unit-testable without a browser | `src/voicebox/metrics.py:7-12` |
| Browser side | `shim.js` — self-contained IIFE, defensive (every hook gated on the API existing) | `src/voicebox/shim.js:20-27` |
| Unit tests | `tests/` — pytest-asyncio auto mode; pin the traps: VAD placement (`tests/test_vad_placement.py`), non-blocking STT (`tests/test_nonblocking_stt.py`), stop-drains-STT (`tests/test_stop_drains_stt.py:1-6`), gap-free Kokoro playout (`tests/test_kokoro_playout.py:1-9`), speak surface (`tests/test_agent_surface.py`), metrics (`tests/test_metrics.py`), browser startup (`tests/test_browser_session.py`), timing (`tests/test_timing_instrumentation.py`) | `pyproject.toml:79-81` |
| Live smoke drivers | `scripts/smoke_browser_shim.py:1-17` (audio plumbing, no app), `scripts/smoke_full_duplex.py:1-18` (speak during pending listen), `scripts/smoke_barge_in.py:7-16` (barge-in scheduling, real event log) | script docstrings as cited |
| Build/QA | `uv sync`; `voicebox` console script; ruff (D+I rules, line 100), pyright, pytest | `pyproject.toml:21-32`, `pyproject.toml:50-51`, `pyproject.toml:60-81` |
| Branch discipline | `BUILDLOG.md` (append-only decisions), `docs/walkthroughs/`, `docs/artefacts/<branch>/` | `CLAUDE.md` "Branch discipline" section; `BUILDLOG.md` |

External dependency worth naming: **pipecat-ai ≥ 1.3.0** with `local-smart-turn`, `silero`,
`websocket`, and platform-split Whisper extras (`mlx-whisper` on macOS, `whisper`/faster-whisper
elsewhere) — the platform split is mirrored in `_create_stt_service`
(`src/voicebox/agent.py:816-833`, `pyproject.toml:26-28`).

## Physical view

*What deploys where?*

Everything runs on one developer machine; there is no deploy config (no Docker/Terraform/CI
deploy in the repo). Topology is three local processes plus downloaded model weights:

| Node | Listens / connects | Evidence |
|---|---|---|
| MCP server (parent) | HTTP `localhost:9090/mcp`, stateless streamable-http | `src/voicebox/server.py:33-39` |
| pipecat child | WebSocket server `localhost:9091` (raw PCM) | `src/voicebox/server.py:132-135`, `src/voicebox/runner_args.py:43-44` |
| Chromium child | CDP `localhost:9222` for the external UI-driving client; page connects out to `:9091` | `src/voicebox/browser_session.py:199-205`, `src/voicebox/shim.js:28` |
| Model artifacts | Kokoro ONNX auto-downloaded to `~/.cache/kokoro-onnx`; Whisper `Systran/faster-distil-whisper-large-v3` on CPU int8 (CUDA deliberately not required) | `src/voicebox/processors/kokoro_tts.py:30-56`, `src/voicebox/agent.py:825-833` |
| Run artifacts | `record_dir` (repo convention: `temp/<run>/`) — WAVs, `events.json`, `metrics.json`, `agent-debug.log` | `src/voicebox/agent.py:391-399`, `src/voicebox/agent.py:552-619` |

Ports are preflighted so a second session fails with a retryable error instead of colliding
(`src/voicebox/server.py:42-72`, `src/voicebox/server.py:130-131`). Single session at a time is
the operating model.

## Invariants

Ordering/consistency rules the system depends on. A delta touching one of these flows must check
this list.

| # | Invariant | Why / consequence of violating | Evidence | Protected by |
|---|---|---|---|---|
| I1 | The VAD stage sits BETWEEN transport input and STT | `SegmentedSTTService` trims its buffer while it believes the user is silent; VAD downstream ⇒ 85–90 % of audio discarded during a slow decode | `src/voicebox/agent.py:880-897` | `tests/test_vad_placement.py`, S2 |
| I2 | Whisper never runs on pipecat's frame task (worker + eager decode) | Inline decode freezes the loop (measured 21 s on a 40 s utterance); a queued `speak` played 51 s late | `src/voicebox/processors/nonblocking_whisper_stt.py:7-37`, `:58-93` | `tests/test_nonblocking_stt.py`, S2 |
| I3 | User-turn start strategies have `enable_interruptions=False` | The "user" is the app bot; defaults would cancel our in-flight Kokoro TTS the moment the bot makes a sound — the tester must be able to talk over it | `src/voicebox/agent.py:863-876` | S3 |
| I4 | Kokoro yields one buffered, gap-free utterance (no per-chunk streaming) | Synthesis gaps became 1.6–4.2 s of real mic silence; the app heard one utterance as several turns | `src/voicebox/processors/kokoro_tts.py:171-188` | `tests/test_kokoro_playout.py`, S3 |
| I5 | Every parent IPC deadline outlives the child budget it wraps: stop 210 s > drain cap 180 s; speak 60 s > playout 30 s | At stop=30 s the parent reaped the child mid-drain, before `events.json`/`metrics.json` were written (round 4 lost its artifact set) | `src/voicebox/server.py:296-305`, `src/voicebox/server.py:251-262`, `src/voicebox/processors/nonblocking_whisper_stt.py:53-55` | `tests/test_stop_drains_stt.py`, S4 |
| I6 | `TURN_STOP_TIMEOUT_SECS` (240) > drain cap (180) | If the aggregator watchdog fires before a slow decode lands, the turn closes empty and the transcript re-emits as an orphan stamped at arrival time | `src/voicebox/agent.py:106-115` | S2 |
| I7 | Asymmetric wire rates: tap 16 kHz in (Whisper hard-assumes 16 k), mic 48 kHz out (page-native); the shim's `AudioContext` does the 48→16 resample | Wrong rate ⇒ chipmunk/slow audio or Whisper garbage | `src/voicebox/agent.py:927-937`, `src/voicebox/shim.js:31-37`, `src/voicebox/runner_args.py:43-47` | S2, S3 |
| I8 | Children are started with `spawn`, never fork | Fork from the async MCP context copies the event loop/fds/locks and breaks | `src/voicebox/agent_ipc.py:22-25` | S1 |
| I9 | Exactly one WS client: the shim is injected page-scoped, never context-wide; attached CDP clients must not open tabs | A second tab connecting to :9091 triggers pipecat's one-client kick → 1 Hz reconnect storm | `src/voicebox/browser_session.py:223-227` | S1 |
| I10 | The tap dedupes `track` events by `track.id` and sinks each remote track into a muted `<audio>` element | Daily surfaces the same logical audio on multiple peer connections (duplicate tap = doubled audio); Chromium decodes a remote track only while a media element renders it (no sink = pure-silence capture) | `src/voicebox/shim.js:256-268`, `src/voicebox/shim.js:271-292` | S2 |
| I11 | Artifact dump snapshots `AudioBufferProcessor` buffers BEFORE `stop_recording()` and runs BEFORE `EndFrame` | `stop_recording()` resets the buffers; the processor closes after `EndFrame` — either ordering error loses the WAVs | `src/voicebox/agent.py:519-528`, `src/voicebox/agent.py:596-603` | S4 |
| I12 | Teardown emits `session_stopped` before anything else so pending `listen()`s return it instead of being cancelled; the pipeline ends via `EndFrame`, never `worker.stop()` | `BaseWorker.stop()` never ends the run — awaiting the runner hangs forever | `src/voicebox/agent.py:494-498`, `src/voicebox/agent.py:527-531`, `src/voicebox/bot.py:85-90` | S4 |
| I13 | `speak(when=...)` snapshots the event-log position synchronously at arm time | Capturing it inside the spawned task races the trigger event and can drop it | `src/voicebox/agent.py:725-731` | S3 |
| I14 | App-bot transcripts claim the earliest *unclaimed VAD start* for `turn_started_at`, not the aggregator's stamp | Under batch STT the aggregator stamps arrival time — observed 103 s off | `src/voicebox/agent.py:307-337`, `src/voicebox/agent.py:347-352` | S2 |

Quality budgets baked into the design (not aspirations — measured constants): VAD stop lag
~1.0 s on every `app_bot_speech_stopped` (`src/voicebox/agent.py:94-98`); Whisper CPU int8
≈0.4–0.55× realtime warm (`src/voicebox/processors/nonblocking_whisper_stt.py:48-55`);
`listen(timeout≤45)` to stay under MCP clients' ~60 s HTTP cap (`src/voicebox/server.py:184-188`).

## Scenarios

### S1: Bring up a session against a voice app

The LLM calls `start_browser_session(url)`; both children come up, the shim installs, the audio
link connects. A blank tab is never reported as a started session.

| # | Hop | View | Evidence |
|---|-----|------|----------|
| 1 | MCP tool entry; audio/CDP ports preflighted | development | `src/voicebox/server.py:75-131` |
| 2 | pipecat child spawned with queue pair | process | `src/voicebox/agent_ipc.py:87-96` |
| 3 | Child builds agent + `WebsocketServerTransport` on :9091 | logical | `src/voicebox/bot.py:44-45`, `src/voicebox/agent.py:939-943` |
| 4 | Browser child spawned; Chromium launched with CDP port, mic permission, shim init-script on the one page | physical | `src/voicebox/browser_session.py:66-80`, `:199-227` |
| 5 | `goto(url)` then poll until shim installed; failure → `{"ok": false}` → parent raises and tears down the pipecat child | process | `src/voicebox/browser_session.py:229-241`, `src/voicebox/server.py:136-147`, `src/voicebox/browser_session.py:152-180` |
| 6 | Shim opens WS to :9091 (1 s reconnect loop covers pipecat starting late) | process | `src/voicebox/shim.js:94-140` |
| 7 | `on_client_connected` → `client_connected` event in the log | logical | `src/voicebox/agent.py:433-437` |
| 8 | Returns `{cdp_endpoint, audio_ws_url, attach_hint}` for the external UI driver | physical | `src/voicebox/browser_session.py:93-98` |

### S2: The app bot speaks; the LLM reads the transcript

The riskiest path — it crosses all three processes and both known performance traps (frame-task
stalls, batch-STT lag).

| # | Hop | View | Evidence |
|---|-----|------|----------|
| 1 | App's `RTCPeerConnection` fires `track`; deduped by `track.id` | logical | `src/voicebox/shim.js:256-268` |
| 2 | Muted `<audio>` sink forces Chromium to decode the remote track | process | `src/voicebox/shim.js:271-292` |
| 3 | Web Audio graph at 16 kHz: source → `pcm-capture` worklet → Int16 → `ws.send` (real-time paced, silence as zeros) | process | `src/voicebox/shim.js:294-333`, rationale `:177-193` |
| 4 | Transport deserializes raw PCM to `InputAudioRawFrame` | logical | `src/voicebox/raw_pcm_serializer.py:37-45` |
| 5 | VAD stage (`stop_secs=1.0`) ahead of the STT (I1); start frame → `app_bot_speech_started` + unclaimed-start queued (I14) | logical | `src/voicebox/agent.py:838-847`, `:899-907`, `:347-352` |
| 6 | STT `run_stt` enqueues the segment and yields nothing — frame task freed (I2) | process | `src/voicebox/processors/nonblocking_whisper_stt.py:160-178` |
| 7 | Single worker transcribes in order; eager decode inside `to_thread` | process | `src/voicebox/processors/nonblocking_whisper_stt.py:180-194`, `:58-93` |
| 8 | Aggregator `on_user_turn_stopped` → `app_bot_transcript` event stamped from the claimed VAD start; empty text flagged, not dropped | logical | `src/voicebox/agent.py:444-450`, `:307-337`, `src/voicebox/events.py:73-88` |
| 9 | `listen_events(cursor)` wakes on the condition, returns events + next cursor + `transcription_lag_secs` (tells "still transcribing" from "silence") | logical | `src/voicebox/agent.py:621-662` |
| 10 | Response routed by correlation id back to the MCP `listen` tool | process | `src/voicebox/bot.py:51-58`, `src/voicebox/agent_ipc.py:236-243`, `src/voicebox/server.py:151-202` |

### S3: Timed barge-in — `speak(when="app_bot_speech_started", timer_secs=1.5)`

The tester talks over the bot on a trigger and the LLM observes the bot's reaction. Also covers
plain and `wait_for_turn` speaks (hops 5–9 are shared).

| # | Hop | View | Evidence |
|---|-----|------|----------|
| 1 | MCP `speak` tool → IPC with mode-dependent deadline | development | `src/voicebox/server.py:205-271` |
| 2 | Child runs it as its own task — a pending `listen` is not blocked | process | `src/voicebox/bot.py:104-106` |
| 3 | Arm: snapshot log position synchronously (I13), emit `tester_barge_in_armed`, return `{"armed": true}` | logical | `src/voicebox/agent.py:725-735` |
| 4 | Trigger task waits for the next matching event, sleeps `timer_secs`, emits `tester_barge_in_fired` + `tester_transcript` (ground truth, not STT) | process | `src/voicebox/agent.py:782-802`, `src/voicebox/events.py:90-127` |
| 5 | LLM-response frame triplet queued → Kokoro synthesizes, buffers the whole utterance, yields gap-free (I4) | logical | `src/voicebox/agent.py:804-814`, `src/voicebox/processors/kokoro_tts.py:167-189` |
| 6 | Our TTS is not cancelled by the bot's voice (I3) | process | `src/voicebox/agent.py:863-876` |
| 7 | Transport output serializes at 48 kHz over the WS (I7) | process | `src/voicebox/agent.py:931-937`, `src/voicebox/raw_pcm_serializer.py:31-35` |
| 8 | Shim wraps bytes as `AudioData`, fans out to every live synthetic-mic writer; the app's own WebRTC carries our voice out | logical | `src/voicebox/shim.js:114-139`, `:72-92`, `:153-175` |
| 9 | Observer logs `tester_speech_started/stopped/interrupted`; `_Playout` resolves `wait_for_playout` on real audio end, timeout returns a diagnosis (`played: false` + reason), not an exception | logical | `src/voicebox/agent.py:358-377`, `:194-249`, `:753-775` |

### S4: Stop with `record_dir` — the session becomes a reviewable report

| # | Hop | View | Evidence |
|---|-----|------|----------|
| 1 | MCP `stop` tool, 210 s deadline (I5); reap runs in `finally` even if the graceful path fails | development | `src/voicebox/server.py:274-321` |
| 2 | Child loop calls `agent.stop()` first, then drains/cancels in-flight command tasks | process | `src/voicebox/bot.py:85-102` |
| 3 | `session_stopped` emitted first — pending `listen()`s return it (I12); armed barge-ins cancelled | logical | `src/voicebox/agent.py:494-504` |
| 4 | STT drained with backlog-scaled budget (15 s + 1 s/audio-s, cap 180 s); incomplete drain logged, never hangs | process | `src/voicebox/agent.py:512-516`, `src/voicebox/processors/nonblocking_whisper_stt.py:196-223` |
| 5 | Event log settled (transcript frames still hop tasks after the queue empties) | process | `src/voicebox/agent.py:537-550` |
| 6 | Artifacts dumped before `EndFrame` (I11): `events.json`, `metrics.json` via `compute_metrics`, WAVs (buffers snapshotted, tester-left/bot-right stereo merge) | logical | `src/voicebox/agent.py:519-528`, `:552-619`, `src/voicebox/metrics.py:15-76` |
| 7 | `EndFrame` queued; `WorkerRunner.run()` returns (I12) | process | `src/voicebox/agent.py:527-531` |
| 8 | Parent reaps the pipecat child and the browser child, returns `{stopped, artifacts}` | physical | `src/voicebox/server.py:310-321`, `src/voicebox/agent_ipc.py:38-67`, `src/voicebox/browser_session.py:101-124` |

## Gaps

| Gap | Evidence | Consequence |
|---|---|---|
| The `RTCPeerConnection` wrap only patches the top page's `window` — peer connections inside cross-origin iframes or Web Workers are invisible | `src/voicebox/shim.js:195-197`, `:342` | S2 silently captures nothing against iframe-embedded apps (e.g. Daily Prebuilt); the session looks like the bot never spoke. Known, documented limitation (CLAUDE.md); no issue open. |
| Shim runtime errors accumulate in `window.__voiceShim.errors` but are never shipped to the event log — only a CDP-attached client can see them | `src/voicebox/shim.js:66-70` | A broken tap (e.g. worklet failure at S2 hop 3) surfaces to the LLM as pure silence; diagnosis requires a second tool. Endangers S2 debuggability. |
| The audio path itself has no pytest coverage — it is verified only by live smoke scripts and dogfood sessions | `scripts/smoke_browser_shim.py:1-17`; CLAUDE.md "Quality checks" | A regression at S1 hops 4–7 or S2 hops 1–4 is invisible to `uv run pytest`. Accepted trade-off (needs a real browser); the smoke scripts are the compensating control. |
| Playwright `storage_state` session reuse is deliberately not offered — CDP clients cannot save cookies from the non-default context; only `user_data_dir` is exposed | `src/voicebox/browser_session.py:38-60`, `:207-221`; CLAUDE.md CDP-context-split trap | Constrains S1 session reuse to persistent profiles. Verified design decision, recorded here so nobody re-adds `storage_state`. |
| Physical view is thin by construction: single host, pinned ports, no deploy/CI-deploy config in the repo | `src/voicebox/server.py:33-39` | None today (local dev tool). Becomes a real gap only if voicebox is ever hosted for remote use. |
