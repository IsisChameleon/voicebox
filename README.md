# voicebox

**Give your coding agent a voice and ears so it can test your voice agent for you.**

Testing a browser voice app means *being* the user: open the tab, join the call, talk, listen, try to break it — by hand, every time you touch a prompt. voicebox automates that loop. It's an MCP server that turns Claude into a synthetic voice user: Claude drives a real Chromium, speaks into the page's mic via local Kokoro TTS, and hears the agent's replies back via local Whisper.
Hand it a task — *"start the story, interrupt halfway, then ask something off-topic"* — and it holds the conversation against your app and reports what happened.

Works against anything using `getUserMedia` + WebRTC (Daily, LiveKit, plain `RTCPeerConnection`), with the app none the wiser. Local models by default — no API keys to run it.

**Use it if** you're building a browser-based voice agent and you're tired of being its only tester.
**Skip it (for now) if** your app isn't web-based or runs voice entirely server-side with no browser mic.

When your coding agent starts a browser session in Voicebox, it drives a Playwright-controlled Chromium with an audio shim injected; the shim hijacks the page's microphone (fed by Kokoro TTS from this server) and tees the bot's remote WebRTC audio back to Whisper. The LLM can then act as a synthetic voice user against any web voice app — Daily, LiveKit, plain `RTCPeerConnection`, anything that uses `getUserMedia` + WebRTC — without the app being aware of the indirection.

## Topology

```
Claude (LLM) ─HTTP/JSON-RPC─► voicebox ────multiprocessing.Queue──► Pipecat child (WebsocketServerTransport)
                                    │                                       ▲
                                    │                                       │ raw 16-bit PCM
                                    │                                       │ (Kokoro 48 kHz out,
                                    │                                       │  Whisper 16 kHz in)
                                    │                                       ▼
                                    └─CDP─► Playwright-driven Chromium ◄── shim.js injected
                                                    │
                                                    │ WebRTC
                                                    ▼
                                            the target voice app
                                            (Daily, LiveKit, plain RTCPeerConnection, …)
```

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A browser-based voice app to point it at (e.g. a locally-running Next.js / Svelte app on `localhost:3000`)

By default the agent uses local models — Whisper for STT, Kokoro for TTS — so no API keys are needed.

## Install

```bash
git clone https://github.com/IsisChameleon/voicebox.git
uv tool install -e /path/to/voicebox
```

## Run

```bash
voicebox
```

The server listens on `http://localhost:9090/mcp` (streamable-HTTP transport).

## Register with your MCP client

Claude Code:

```bash
claude mcp add voicebox --transport http http://localhost:9090/mcp --scope user
```

Cursor (`~/.cursor/mcp.json`):

```json
{ "mcpServers": { "voicebox": { "url": "http://localhost:9090/mcp" } } }
```

## MCP tools

There are four tools. `listen`'s and `speak`'s signatures below match `src/voicebox/server.py`
exactly — that file is the source of truth if this drifts.

| Tool | Purpose |
|---|---|
| `start_browser_session(url, headless?, cdp_port?, audio_port?, user_data_dir?, record_dir?)` | Launch a Playwright Chromium with the audio shim injected, navigate to `url`, expose CDP. The shim hijacks the page's mic (fed by Kokoro) and tees the page's WebRTC remote audio into Whisper. Pass `user_data_dir` (a persistent Chrome profile) to reuse an authenticated session across runs. Pass `record_dir` to have `stop()` write a reviewable test report (WAVs, `events.json`, `metrics.json`) into that directory. Returns `{cdp_endpoint, audio_ws_url, playwright_mcp_env, attach_hint}` — `attach_hint` is the exact shell command to wire up an external Playwright client (see "Driving the UI" below). |
| `speak(text, wait_for_playout=False, wait_for_turn=False, when=None, timer_secs=0.0)` | Synthesize `text` with Kokoro TTS and stream it into the shim's synthetic mic. By default returns as soon as the speech is queued: `{"queued": true}`. `wait_for_playout=True` blocks until OUR OWN audio finishes playing and adds `{started_at, finished_at, interrupted}` — it says nothing about the app bot, only about our Kokoro playout. `wait_for_turn=True` waits until the app bot is silent, then speaks (the polite path). `when=<event type>, timer_secs=N` arms a ONE-SHOT barge-in trigger: returns `{"armed": true}` immediately, then `N` seconds after the *next* occurrence of `when` (e.g. `"app_bot_speech_started"`) speaks over the bot. `wait_for_turn`/`when` control WHEN we start speaking; `wait_for_playout` controls WHEN the call returns — they're independent. |
| `listen(timeout=30, cursor=0)` | Block until at least one event exists past `cursor` (or `timeout` elapses), then return every event from `cursor` onward plus the next cursor: `{"events": [...], "cursor": <next cursor>}`. Pass the returned `cursor` back on the next call to resume without missing or re-reading anything; `cursor=0` replays the whole session. `events` is empty if the timeout elapsed with nothing new. See the event vocabulary below. |
| `stop()` | Tear down the pipecat agent and close the Chromium session. Returns `{"stopped": true}`, plus `"artifacts"` (`events`, `metrics`, `merged_wav`, `tester_wav`, `app_bot_wav` — absolute paths) when the session ran with `record_dir`. |

### Event vocabulary

`listen()` returns timestamped conversation events; every event carries `type` and `t` (wall-clock
seconds). Two parties, named from the test's point of view: **`app_bot`** is the app's voice agent
under test; **`tester`** is our synthetic human, voiced by Kokoro TTS.

- `session_started` / `session_stopped` — log header (carries `vad_stop_secs`) at session start; a
  teardown marker so a pending `listen()` returns instead of being cancelled when `stop()` runs.
- `client_connected` / `client_disconnected` — the in-page audio link came up / dropped (a drop is
  a status event, NOT speech).
- `app_bot_speech_started` / `app_bot_speech_stopped` — the app bot's voice activity.
  `app_bot_speech_stopped.t` lands ~`vad_stop_secs` (~1 s) after the bot actually stopped talking.
- `app_bot_transcript` — a finished app-bot utterance (`text`, `turn_started_at`); arrives after the
  corresponding `app_bot_speech_stopped` (batch STT). To just wait for the next thing the app bot
  says, call `listen()` in a loop with the advancing cursor and act on this event.
- `tester_speech_started` / `tester_speech_stopped` / `tester_speech_interrupted` — OUR synthetic
  voice starting / finishing / being cut off at playout.
- `tester_transcript` — the exact text WE spoke (`text`) — the ground-truth `speak()` input,
  emitted at speak time, not recovered via STT.
- `tester_barge_in_armed` / `tester_barge_in_fired` — a one-shot `speak(when=...)` trigger being
  registered / firing.

The full field-level definitions (including which subclass carries which extra fields) live in
[`src/voicebox/events.py`](src/voicebox/events.py) — the single source of truth for this vocabulary.

### Example session

```jsonc
// 1. launch a Playwright Chromium with the audio shim injected, navigate to the app,
//    and ask stop() to write a test report to temp/my_run
{"name": "start_browser_session",
 "arguments": {"url": "http://localhost:3000", "record_dir": "temp/my_run"}}
// → {"cdp_endpoint": "http://localhost:9222",
//    "audio_ws_url": "ws://localhost:9091",
//    "playwright_mcp_env": "PLAYWRIGHT_MCP_CDP_ENDPOINT=http://localhost:9222 PLAYWRIGHT_MCP_ISOLATED=false",
//    "attach_hint": "playwright-cli close-all && PLAYWRIGHT_MCP_CDP_ENDPOINT=http://localhost:9222 PLAYWRIGHT_MCP_ISOLATED=false playwright-cli"}

// 2. paste attach_hint verbatim (or use it as a template for your own Playwright
//    client) to attach to that same Chromium over CDP — see "Driving the UI" below.
//    Use the attached client to log in / navigate to the app's call screen. The page
//    then calls getUserMedia() → the shim returns a synthetic mic stream fed by Kokoro.

// 3. speak — the page's WebRTC peer sends OUR Kokoro audio to the app bot
{"name": "speak", "arguments": {"text": "Hi! Tell me about this book."}}
// → {"queued": true}

// 4. listen in a loop, advancing the cursor, until the bot's reply transcript shows up
{"name": "listen", "arguments": {"timeout": 45, "cursor": 0}}
// → {"events": [
//      {"type": "app_bot_speech_started", "t": 1234.1},
//      {"type": "app_bot_speech_stopped", "t": 1236.4},
//      {"type": "app_bot_transcript", "t": 1236.6,
//       "text": "Hello, welcome. I'm so excited to have you...",
//       "turn_started_at": "2026-07-02T12:00:34.1Z"}
//    ], "cursor": 3}

// 5. (optional) barge-in: arm a reply that fires 1.5s after the bot next starts talking,
//    then confirm it fired via listen()
{"name": "speak",
 "arguments": {"text": "Wait, actually —", "when": "app_bot_speech_started", "timer_secs": 1.5}}
// → {"armed": true}
{"name": "listen", "arguments": {"timeout": 30, "cursor": 3}}
// → events include tester_barge_in_armed, then — once the bot next speaks and 1.5s
//   elapse — tester_barge_in_fired followed by tester_speech_started

// 6. end the call, either by saying "goodbye" or driving the UI's End-call button
//    via the attached Playwright client, then:
{"name": "stop", "arguments": {}}
// → {"stopped": true,
//    "artifacts": {
//      "events": "/abs/temp/my_run/events.json",
//      "metrics": "/abs/temp/my_run/metrics.json",
//      "merged_wav": "/abs/temp/my_run/merged.wav",
//      "tester_wav": "/abs/temp/my_run/kokoro_voice.wav",
//      "app_bot_wav": "/abs/temp/my_run/ember_voice.wav"}}
```

There's no scripted end-to-end driver checked into the repo (it depended on a private app); the
runnable references for the audio path are the smoke scripts under `scripts/` — see "Dev workflow"
below.

### Driving the UI

voicebox has no `navigate`/`click`/`snapshot` tools of its own and holds no Playwright page handle
in the parent process — UI driving is delegated entirely to an **external** Playwright client that
attaches over CDP to the Chromium `start_browser_session` launched. `attach_hint` in that tool's
response is the exact recipe for `playwright-cli`:

```bash
playwright-cli close-all && \
  PLAYWRIGHT_MCP_CDP_ENDPOINT=http://localhost:9222 \
  PLAYWRIGHT_MCP_ISOLATED=false \
  playwright-cli
```

Both env vars are required together:

- `PLAYWRIGHT_MCP_CDP_ENDPOINT` — attach to voicebox's Chromium instead of launching a new one.
- `PLAYWRIGHT_MCP_ISOLATED=false` — without this, `playwright-cli` defaults `isolated=true` and
  calls `browser.newContext()` even over CDP, handing you a fresh, unauthenticated context instead
  of the existing voicebox tab.

`close-all` is mandatory when a daemon already exists for the session name — reusing an existing
daemon ignores the env vars entirely.

Any Playwright client that can attach over CDP works the same way — e.g. your own script via
`chromium.connect_over_cdp("http://localhost:9222")` (`scripts/smoke_browser_shim.py` is the
reference for that pattern).

**Do not open new tabs** once attached: the audio shim is page-scoped to the voicebox-owned tab. A
second tab connecting to the audio WebSocket triggers pipecat's "only one client" kick and a 1 Hz
reconnect storm.

## Test report

When `record_dir` is set on `start_browser_session`, `stop()` writes a self-contained, reviewable
report into that directory and returns the paths in `artifacts`. Claude can't hear audio, so this
report — not the recording — is how it judges a call: was the app slow to respond, did it yield
when interrupted, did it actually answer the question. `metrics.json` is derived purely from the
event log; its top-level keys:

- `session` — start/stop timestamps and duration, plus a `biases` block (`vad_stop_secs` and notes)
  documenting known measurement skew so the numbers aren't misread.
- `turns` — every transcript (tester + app-bot) merged and sorted by time; each app-bot turn carries
  `response_latency_secs` — the reply-onset latency the tester experienced: time from finishing an
  utterance to hearing the app bot start replying.
- `app_response_latencies_secs` — that same latency as a flat list.
- `talk_over_windows` — intervals where tester and app-bot speech overlapped (barge-in behavior).
- `dead_air_gaps` — silent spans between speech.
- `talk_time` — total seconds each party spoke, plus their ratio.
- `utterances` — utterance counts per party.
- `summary` — headline numbers: mean/max response latency, total talk-over time, total dead air.

Full schema, edge cases and the exact latency-computation rules:
[`docs/superpowers/specs/2026-06-15-stage4-metrics-artifacts-design.md`](docs/superpowers/specs/2026-06-15-stage4-metrics-artifacts-design.md).

## Architecture notes

- The MCP server (parent process) hosts the FastMCP HTTP endpoint. A separate Pipecat child process runs the audio pipeline — they communicate over `multiprocessing.Queue`. A second child runs Playwright/Chromium. This keeps Pipecat's event loop and audio threads off the MCP request path.
- Audio never crosses the MCP boundary. MCP carries text and control only; audio flows out-of-band over a raw-PCM WebSocket between the shim and pipecat.
- The shim taps the **page's playout audio path** via Web Audio (`MediaStreamAudioSourceNode → AudioWorkletNode`), not the WebCodecs path. Web Audio is pulled at a fixed sample rate so silence in the source becomes literal zero samples — preserving real-time pacing for the recording and STT.
- We disable Pipecat's default RTVI processor (`enable_rtvi=False`). We're a headless synthetic user; transcripts reach Claude via `listen()`'s return value, not RTVI data-channel notifications.

### File map

| File | Role |
|---|---|
| `server.py` | FastMCP HTTP surface — the four tools Claude calls. Lives in the parent process. No audio code. |
| `agent_ipc.py` | The shared mailbox between parent and child. Owns the multiprocessing queues and the pipecat-child lifecycle. |
| `bot.py` | The pipecat child's tiny command loop: `read → dispatch → respond`. |
| `agent.py` | `PipecatMCPAgent` — the wrapper that owns the Pipecat pipeline behind a `WebsocketServerTransport`. |
| `events.py` | The conversation-event vocabulary `listen()` emits — single source of truth for event types and fields. |
| `metrics.py` | `compute_metrics()` — the pure function that turns an event log into `metrics.json`. No I/O, no pipecat imports. |
| `runner_args.py` | The `BrowserShimRunnerArguments` dataclass (host, port, mic_rate, tap_rate, record_dir) — pipecat doesn't ship one for plain WebSocket-server transports. |
| `raw_pcm_serializer.py` | Tiny `FrameSerializer` that exchanges raw 16-bit LE mono PCM with the browser shim — no protobuf, no envelope. |
| `shim.js` | The browser shim. Injected via Playwright `addInitScript` so it runs before any page code. Overrides `getUserMedia` to return a synthetic mic stream backed by `MediaStreamTrackGenerator`, and wraps `RTCPeerConnection` to tap every inbound audio track via Web Audio (`MediaStreamAudioSourceNode → AudioWorkletNode`) back to the server. |
| `browser_session.py` | Manages the Playwright child process: launches Chromium with `--remote-debugging-port=<cdp_port>` + `--use-fake-ui-for-media-stream`, registers `shim.js` via `add_init_script`, navigates to the user-supplied URL, parks until told to stop. Supports `user_data_dir` for a persistent, CDP-coherent authenticated profile. |

### What happens on `start_browser_session`

```
1.  Claude → MCP                   tools/call start_browser_session(url="http://localhost:3000")
2.  server.py:start_browser_session()
                                   audio_ws_url = "ws://localhost:9091"
                                   start_pipecat_process(BrowserShimRunnerArguments(port=9091, …))
                                   start_browser(url, audio_ws_url, cdp_port=9222, …)
3.  [CHILD-1: pipecat]             create_agent → WebsocketServerTransport with RawPCMSerializer
                                     audio_in_sample_rate  = 16000 (Whisper-MLX requires 16 kHz)
                                     audio_out_sample_rate = 48000 (Kokoro → page mic)
                                   pipeline: transport.input → Whisper → aggregator → Kokoro → transport.output
                                   websocket listening on :9091
4.  [CHILD-2: browser]             read shim.js, prepend window.__VOICE_SHIM_WS_URL__
                                   chromium.launch(args=[--remote-debugging-port=9222,
                                                         --use-fake-ui-for-media-stream])
                                   context.add_init_script(shim) ; page.goto(url) ; ready
5.  [page]                         shim runs before any page code:
                                     - opens WebSocket to ws://localhost:9091
                                     - overrides navigator.mediaDevices.getUserMedia
                                     - wraps window.RTCPeerConnection
                                   when the page calls getUserMedia({audio}), the shim returns
                                   MediaStream([MediaStreamTrackGenerator]). When the page creates
                                   an RTCPeerConnection, the wrapper subscribes to its `track` events.
6.  External Playwright client     connect_over_cdp("http://localhost:9222") (or playwright-cli,
    (per "Driving the UI" above)   see above) — drives the UI: login, navigate, click "start call".
                                   → the app calls getUserMedia (shim returns synthetic mic)
                                   → the app creates RTCPeerConnection to its SFU
                                   → bot starts streaming audio → shim's track-event hook
                                     pipes it via Web Audio worklet → WebSocket → pipecat
7.  [Claude] speak("hi")           Kokoro renders audio → WebsocketServerTransport writes Int16 PCM
                                   over the WS → shim writes AudioData chunks into the
                                   MediaStreamTrackGenerator → the page's WebRTC peer encodes Opus
8.  [Claude] listen()              a pipeline observer turns VAD/turn frames and transcripts into
                                   timestamped events; listen() returns {events, cursor}
9.  [Claude] stop()                terminates pipecat child + browser child; if record_dir was set,
                                   writes events.json + metrics.json + WAVs first
```

### Tradeoffs and known sharp edges

- **Whisper-MLX requires 16 kHz on input.** `mlx_whisper.transcribe()` has no `sample_rate` parameter and hard-assumes 16 kHz. The shim's outbound `AudioContext` runs at 16 kHz so the browser does the 48→16 resample natively; Kokoro stays at 48 kHz so the synthetic mic into the page is full-quality.
- **Sample-rate split is asymmetric on the wire:** `audio_in_sample_rate=16000` (browser → pipecat), `audio_out_sample_rate=48000` (pipecat → browser). The `AudioBufferProcessor` resamples internally so the recorded WAVs come out at 48 kHz regardless.
- **VAD `stop_secs=1.0s`** captures complete utterances over WebRTC with natural pauses; pipecat's default 0.2 s (tuned for clean TTS sources) chops remote speech mid-sentence. Consequence: `app_bot_speech_stopped.t` — and anything derived from it in `metrics.json` — lands about 1 s after the bot truly stopped talking.
- **The shim taps audio via Web Audio, not WebCodecs**, because `MediaStreamTrackProcessor` only emits chunks during active speech on a remote WebRTC track — silence is dropped, so a sparse byte stream reaches pipecat and the recorded WAV plays back several times faster than real time. Web Audio is pulled by the AudioContext clock and fills silence with zero samples.
- **Headless Chromium works, audio path included** — `headless=true` still captures the bot's audio and feeds the synthetic mic (the tap is Web Audio, not a visible window). The shim relies on Web Audio + `MediaStreamTrackGenerator` (modern Chromium-only). Tested with Playwright 1.50 + bundled Chromium.
- **`RTCPeerConnection` wrap can't reach peer connections inside cross-origin iframes or Web Workers** — a real limitation for apps using Daily Prebuilt's `<DailyIframe>` (workaround: hook `<audio>` elements via `MutationObserver` + `captureStream()`).
- **One session at a time.** The server pins ports 9090 (MCP), 9091 (audio WS), 9222 (CDP). `start_browser_session` checks `audio_port`/`cdp_port` are free first and fails with a clear message if not — pass overrides to run a second session in parallel.
- **Session reuse uses `user_data_dir`.** A persistent profile lives in the browser's *default* context, which is exactly what a CDP-attached client sees — so logging in once persists across runs with no save step. (Playwright's `storage_state` JSON is deliberately *not* supported: it loads into a separate non-default context that a CDP-attached client can't save back, so it can't be generated from within a session — a persistent profile does the job without that footgun.)
- We pre-grant `microphone` permission via `--use-fake-ui-for-media-stream`. No permission prompt to dismiss.
- **STT is batch + VAD-segmented, not streaming.** Transcripts are utterance-level and arrive after the utterance ends — per-word receive timestamps aren't obtainable.

## Dev workflow

```bash
uv sync                                        # install deps (uv, not pip; deps in pyproject.toml)
uv run python scripts/smoke_browser_shim.py    # audio-path smoke test (no app needed)
uv run python scripts/smoke_full_duplex.py     # speak-during-pending-listen smoke test
uv run python scripts/smoke_barge_in.py        # barge-in scheduling logic test (no browser)
uv run pytest                                  # unit tests (tests/), e.g. compute_metrics
```

There is no bundled full-e2e target app (it previously depended on a private app removed for
credential hygiene); verification is via the smoke scripts above (need a real browser) plus the
unit tests in `tests/`.

## License

BSD-2-Clause (inherited from upstream).
